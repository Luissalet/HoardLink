"""The family's event bus: one append-only log every app can write to and
anyone can read or follow.

An event is ``{id, ts, type, source, data}``. ``type`` is dotted and
lower-case (``links.watch.new``, ``hub.app.started``,
``agent.call``), ``source`` is the app id that emitted it (or ``hub``),
``data`` is a small JSON object — an id, a title, a path; never a whole
transcript. The log lives in ``<data>/events.db`` (SQLite, standard
library) so it survives hub restarts and can be queried by type, source,
time and id; ``follow()`` blocks until something newer than a given id
arrives, which is what the SSE stream and the rule engine use.

Only the hub writes here. Apps ``POST /api/events`` (see ``server.py``);
the rule engine (``rules.py``) reacts to what lands.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Optional

MAX_DATA_BYTES = 16 * 1024
DEFAULT_KEEP = 20000        # rows kept after a prune
_TYPE_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def normalize_type(raw: Any) -> str:
    t = str(raw or "").strip().lower().replace(" ", "_")
    t = "".join(ch for ch in t if ch in _TYPE_OK).strip("._")
    return t[:120]


#: Legacy event names -> the canonical job events (``<app>.job.queued|started|progress|done|failed|cancelled``).
#: ``legacy type -> (canonical type, data defaults)``; the defaults only fill keys the event does not carry.
#: Extend it with :func:`register_alias`; ``EventLog.emit`` stores the canonical type and keeps the name the app
#: used in ``data["_orig_type"]``, so rules and queries written against the old name keep working.
EVENT_ALIASES: dict[str, tuple[str, dict[str, Any]]] = {
    "pygmalion.job_queued": ("pygmalion.job.queued", {}),
    "pygmalion.job_done": ("pygmalion.job.done", {}),
    "hypatia.teacher_job.done": ("hypatia.job.done", {"kind": "teacher"}),
    "hypatia.teacher_job.no_model": ("hypatia.job.failed", {"kind": "teacher", "error": "no model available"}),
    "galton.run.done": ("galton.job.done", {"kind": "run"}),
    "lumiere.render.done": ("lumiere.job.done", {"kind": "render"}),
    "lumiere.render.failed": ("lumiere.job.failed", {"kind": "render"}),
    "lumiere.job.failed": ("lumiere.job.failed", {}),
    "links.media.done": ("links.job.done", {"kind": "download"}),
    "links.media.failed": ("links.job.failed", {"kind": "download"}),
    "midas.backtest.finished": ("midas.job.done", {"kind": "backtest"}),
    "vitruvius.render.done": ("vitruvius.job.done", {"kind": "render"}),
    "vitruvius.assay.done": ("vitruvius.job.done", {"kind": "assay"}),
    "hub.backup.done": ("hub.job.done", {"kind": "backup", "title": "backup"}),
    "hub.backup.failed": ("hub.job.failed", {"kind": "backup", "title": "backup"}),
}


def register_alias(legacy: str, canonical: str, **defaults: Any) -> None:
    """Add (or replace) an alias: events emitted as ``legacy`` are stored as ``canonical``."""
    EVENT_ALIASES[normalize_type(legacy)] = (normalize_type(canonical), dict(defaults))


def canonical_type(t: str) -> str:
    """The canonical name of event type ``t`` (itself when it has no alias)."""
    key = normalize_type(t)
    hit = EVENT_ALIASES.get(key)
    return hit[0] if hit else key


def canonicalize(etype: str, data: Optional[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """``(canonical type, data)`` for an event about to be stored: an aliased type is renamed, its
    defaults (``kind``...) fill what the event lacks and the original name is kept in ``_orig_type``."""
    hit = EVENT_ALIASES.get(etype)
    if not hit or hit[0] == etype:
        return etype, dict(data or {})
    out = dict(data or {})
    for k, v in hit[1].items():
        if out.get(k) in (None, ""):
            out[k] = v
    out["_orig_type"] = etype
    return hit[0], out


def event_names(event: dict[str, Any]) -> list[str]:
    """Every name an event answers to: its stored type and, when it was renamed, the one the app used."""
    names = [str(event.get("type") or "")]
    data = event.get("data")
    orig = data.get("_orig_type") if isinstance(data, dict) else None
    if orig and orig not in names:
        names.append(str(orig))
    return names


def matches(pattern: str, event_type: str) -> bool:
    """``links.*`` matches ``links.watch.new``; ``*`` matches all;
    an exact type matches itself. Several patterns may be joined by ``|``."""
    for p in str(pattern or "*").split("|"):
        p = p.strip() or "*"
        if p == "*" or fnmatch.fnmatchcase(event_type, p):
            return True
    return False


class EventLog:
    def __init__(self, path: Optional[str], *, keep: int = DEFAULT_KEEP, now: Callable[[], float] = time.time):
        self.path = path or ":memory:"
        self.keep = keep
        self._now = now
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
            "type TEXT NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
        self._db.execute("CREATE INDEX IF NOT EXISTS events_type ON events(type)")
        self._db.commit()
        row = self._db.execute("SELECT MAX(id) FROM events").fetchone()
        self.last_id = int(row[0] or 0)
        self._writes_since_prune = 0

    # -- writing ------------------------------------------------------------
    def emit(self, type: str, data: Optional[dict[str, Any]] = None, *, source: str = "hub",
             ts: Optional[float] = None) -> dict[str, Any]:
        etype = normalize_type(type)
        if not etype:
            raise ValueError("event type is required (e.g. 'links.watch.new')")
        if data is not None and not isinstance(data, dict):
            raise ValueError("event data must be an object")
        etype, data = canonicalize(etype, data)
        payload = json.dumps(data or {}, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) > MAX_DATA_BYTES:
            raise ValueError(f"event data too large (> {MAX_DATA_BYTES} bytes): send ids, not contents")
        src = str(source or "hub").strip()[:60] or "hub"
        when = float(ts) if ts else self._now()
        with self._cv:
            cur = self._db.execute("INSERT INTO events (ts, type, source, data) VALUES (?, ?, ?, ?)",
                                   (when, etype, src, payload))
            self._db.commit()
            self.last_id = int(cur.lastrowid)
            event = {"id": self.last_id, "ts": when, "type": etype, "source": src, "data": data or {}}
            self._writes_since_prune += 1
            if self._writes_since_prune >= 500:
                self._prune_locked()
            listeners = list(self._listeners)
            self._cv.notify_all()
        for fn in listeners:
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                pass
        return event

    def _prune_locked(self) -> None:
        self._writes_since_prune = 0
        self._db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (int(self.keep),))
        self._db.commit()

    def prune(self, keep: Optional[int] = None) -> int:
        with self._cv:
            if keep is not None:
                self.keep = int(keep)
            before = self.count()
            self._prune_locked()
            return before - self.count()

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Call ``fn(event)`` on every new event (from the emitting thread).
        Returns an unsubscribe function."""
        with self._lock:
            self._listeners.append(fn)

        def off() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)
        return off

    # -- reading ------------------------------------------------------------
    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        try:
            data = json.loads(r[4])
        except ValueError:
            data = {}
        return {"id": int(r[0]), "ts": float(r[1]), "type": r[2], "source": r[3], "data": data}

    def query(self, *, since_id: int = 0, type: Optional[str] = None, source: Optional[str] = None,
              since_ts: Optional[float] = None, until_ts: Optional[float] = None, text: Optional[str] = None,
              limit: int = 100, newest_first: bool = True) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        if since_id:
            where.append("id > ?"); params.append(int(since_id))
        if source:
            where.append("source = ?"); params.append(str(source))
        if since_ts is not None:
            where.append("ts >= ?"); params.append(float(since_ts))
        if until_ts is not None:
            where.append("ts <= ?"); params.append(float(until_ts))
        if text:
            where.append("(type LIKE ? OR data LIKE ?)"); params += [f"%{text}%", f"%{text}%"]
        limit = max(1, min(int(limit or 100), 2000))
        order = "DESC" if newest_first and not since_id else "ASC"
        sql = f"SELECT id, ts, type, source, data FROM events WHERE {' AND '.join(where)} ORDER BY id {order} LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*params, limit * (4 if type and ("*" in type or "|" in type or type in EVENT_ALIASES) else 1))).fetchall()
        out = [self._row(r) for r in rows]
        if type:
            out = [e for e in out if any(matches(type, n) for n in event_names(e))][:limit]
        return out

    def get(self, event_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self._db.execute("SELECT id, ts, type, source, data FROM events WHERE id = ?", (int(event_id),)).fetchone()
        return self._row(r) if r else None

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def stats(self, since_ts: Optional[float] = None) -> dict[str, Any]:
        where, params = ("WHERE ts >= ?", (float(since_ts),)) if since_ts else ("", ())
        with self._lock:
            by_type = self._db.execute(f"SELECT type, COUNT(*) FROM events {where} GROUP BY type ORDER BY 2 DESC LIMIT 50", params).fetchall()
            by_source = self._db.execute(f"SELECT source, COUNT(*) FROM events {where} GROUP BY source ORDER BY 2 DESC", params).fetchall()
            total = self._db.execute(f"SELECT COUNT(*), MIN(ts), MAX(ts) FROM events {where}", params).fetchone()
        return {"total": int(total[0] or 0), "first_ts": total[1], "last_ts": total[2],
                "by_type": [{"type": t, "count": int(c)} for t, c in by_type],
                "by_source": [{"source": s, "count": int(c)} for s, c in by_source], "last_id": self.last_id}

    def follow(self, after_id: int, timeout: float = 25.0) -> list[dict[str, Any]]:
        """Block up to ``timeout`` seconds for events with id > ``after_id``."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while self.last_id <= after_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cv.wait(remaining)
        return self.query(since_id=after_id, limit=500, newest_first=False)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass


def event_types_help() -> list[dict[str, str]]:
    """The conventions, for the UI and the docs (not enforced)."""
    return [
        {"type": "agent.call", "who": "every app", "data": "tool, ok, ms, caller — one per /api/agent/call"},
        {"type": "<app>.<thing>.<verb>", "who": "the app", "data": "ids only: links.watch.new {watch, title, url}"},
        {"type": "hub.app.started|stopped", "who": "hub", "data": "app, pid"},
        {"type": "<app>.job.queued|started|progress|done|failed|cancelled", "who": "the app",
         "data": "job_id, title, kind, progress (0..1), gpu, eta_s, url, error — what the Work tab follows; legacy names are aliased"},
        {"type": "hub.backup.done|failed", "who": "hub", "data": "snapshot, apps, files, bytes (stored as hub.job.done|failed, kind backup)"},
        {"type": "hub.rule.ran", "who": "hub", "data": "rule, event_id, results"},
        {"type": "hub.job.ran", "who": "hub", "data": "job, results"},
        {"type": "hub.lease.granted|released", "who": "hub", "data": "lease_id, owner, gpu, vram_mb"},
        {"type": "hub.repos.scan", "who": "hub", "data": "repos, with_issues, unpushed_total, errors — after each full repo scan"},
        {"type": "hub.repos.issue", "who": "hub", "data": "repo, kind, text — a repository gained an error-level issue"},
        {"type": "hub.repos.fetch", "who": "hub", "data": "repo, ok, error — a git fetch the user asked for"},
    ]


def iter_since(log: EventLog, after_id: int, patterns: Iterable[str]) -> list[dict[str, Any]]:
    pats = list(patterns) or ["*"]
    return [e for e in log.query(since_id=after_id, limit=500, newest_first=False)
            if any(matches(p, n) for p in pats for n in event_names(e))]
