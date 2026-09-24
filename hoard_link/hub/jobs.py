"""Scheduled jobs: the same action lists as the rules, on a clock.

A job is ``{id, name, every | at, days, then, enabled, note}`` kept in
``<data>/jobs.json``:

* ``every``: ``"15m"``, ``"6h"``, ``"2d"`` — run that often, counted
  from the last run (or from creation);
* ``at``: ``"04:00"`` — run daily at that local time, optionally only on
  ``days`` (``["mon", "tue", ...]`` or ``["weekdays"]``);

A tick thread checks every ``TICK_S`` seconds. A job missed while the hub
was down (a laptop asleep at 04:00) runs at the next tick when
``catch_up`` is true (the default): "nightly" means "once a day", not
"exactly at 04:00 or never". Every run is a ``hub.job.ran`` event plus
the job's ``last`` field, and ``run_now`` runs one on demand. The tick
thread runs one job at a time; a job that takes an hour delays the next,
which is the right thing for backups and rescans that fight for the disk.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from . import actions

TICK_S = 15.0
HISTORY = 100
_EVERY = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_every(text: Any) -> Optional[float]:
    m = _EVERY.match(str(text or ""))
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_at(text: Any) -> Optional[tuple[int, int]]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_days(raw: Any) -> Optional[set[int]]:
    if not raw:
        return None
    out: set[int] = set()
    for d in (raw if isinstance(raw, list) else [raw]):
        d = str(d).strip().lower()[:3]
        if d == "wee":  # weekdays
            out |= {0, 1, 2, 3, 4}
        elif d in _DAYS:
            out.add(_DAYS.index(d))
    return out or None


def validate_job(job: Any) -> list[str]:
    if not isinstance(job, dict):
        return ["job must be an object"]
    problems: list[str] = []
    every, at = job.get("every"), job.get("at")
    if not every and not at:
        problems.append("a job needs 'every' (e.g. '6h') or 'at' (e.g. '04:00')")
    if every and parse_every(every) is None:
        problems.append(f"bad 'every': {every!r} (use 30m, 6h, 1d)")
    if every and parse_every(every) is not None and parse_every(every) < 60:
        problems.append("'every' must be at least 1m")
    if at and parse_at(at) is None:
        problems.append(f"bad 'at': {at!r} (use HH:MM)")
    if job.get("days") and parse_days(job.get("days")) is None:
        problems.append("bad 'days' (use mon..sun or weekdays)")
    problems += actions.validate(job.get("then"))
    return problems


def next_run(job: dict[str, Any], now: float) -> Optional[float]:
    """Epoch seconds of the job's next due time from ``now``."""
    last = float(job.get("last_run_ts") or 0)
    every = parse_every(job.get("every"))
    if every:
        base = last if last else float(job.get("created_ts") or now)
        due = base + every
        return due if due > now else now
    at = parse_at(job.get("at"))
    if not at:
        return None
    days = parse_days(job.get("days"))
    cur = datetime.fromtimestamp(now)
    cand = cur.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    for _ in range(8):
        if cand.timestamp() > now and (days is None or cand.weekday() in days):
            # Skip if today's run already happened.
            if last and datetime.fromtimestamp(last).date() == cand.date():
                cand += timedelta(days=1)
                continue
            return cand.timestamp()
        cand += timedelta(days=1)
        cand = cand.replace(hour=at[0], minute=at[1])
    return None


def is_due(job: dict[str, Any], now: float) -> bool:
    if not job.get("enabled", True):
        return False
    last = float(job.get("last_run_ts") or 0)
    every = parse_every(job.get("every"))
    if every:
        base = last if last else float(job.get("created_ts") or now)
        return now - base >= every
    at = parse_at(job.get("at"))
    if not at:
        return False
    days = parse_days(job.get("days"))
    cur = datetime.fromtimestamp(now)
    today_at = cur.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if days is not None and cur.weekday() not in days:
        return False
    if cur < today_at:
        return False
    ran_today = last and datetime.fromtimestamp(last).date() == cur.date()
    if ran_today:
        return False
    created = float(job.get("created_ts") or 0)
    if not last and created and created > today_at.timestamp():
        return False  # made today after its time: first run is tomorrow, not right now
    # Missed while down: run now if catch_up (default), else only within a tick window.
    if bool(job.get("catch_up", True)):
        return True
    return (now - today_at.timestamp()) < TICK_S * 2


class Scheduler:
    def __init__(self, path: Optional[str], runner: Callable[[list[dict[str, Any]], dict[str, Any], str], list[dict[str, Any]]],
                 *, events: Any = None, now: Callable[[], float] = time.time, tick_s: float = TICK_S):
        self.path = path
        self._run_actions = runner
        self.events = events
        self._now = now
        self.tick_s = tick_s
        self._lock = threading.RLock()
        self.jobs: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running: Optional[str] = None
        self._load()

    # -- persistence ----------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            raw = json.loads(open(self.path, "r", encoding="utf-8-sig").read())
        except (OSError, ValueError):
            raw = []
        jobs = raw.get("jobs") if isinstance(raw, dict) else raw
        with self._lock:
            self.jobs = [j for j in (jobs or []) if isinstance(j, dict) and j.get("id")]

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"jobs": self.jobs}, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, self.path)

    # -- CRUD -------------------------------------------------------------------
    def _view(self, j: dict[str, Any]) -> dict[str, Any]:
        d = dict(j)
        d["next_run_ts"] = next_run(j, self._now())
        d["running"] = self._running == j.get("id")
        return d

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._view(j) for j in self.jobs]

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for j in self.jobs:
                if j.get("id") == job_id:
                    return self._view(j)
        return None

    def add(self, job: dict[str, Any]) -> dict[str, Any]:
        problems = validate_job(job)
        if problems:
            return {"ok": False, "error": "; ".join(problems)}
        with self._lock:
            ids = {j["id"] for j in self.jobs}
            jid = str(job.get("id") or "").strip() or actions.new_id("job", ids)
            if jid in ids:
                return {"ok": False, "error": f"job id already exists: {jid}"}
            rec = {"id": jid, "name": str(job.get("name") or jid), "every": job.get("every"), "at": job.get("at"),
                   "days": job.get("days"), "then": job["then"], "enabled": bool(job.get("enabled", True)),
                   "catch_up": bool(job.get("catch_up", True)), "note": str(job.get("note") or ""),
                   "created_ts": self._now(), "runs": 0, "last": None, "last_run_ts": None}
            self.jobs.append(rec)
            self._save()
            return {"ok": True, "job": self._view(rec)}

    def update(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for j in self.jobs:
                if j.get("id") == job_id:
                    cand = {**j, **{k: v for k, v in patch.items()
                                    if k in ("name", "every", "at", "days", "then", "enabled", "catch_up", "note")}}
                    if "every" in patch and patch["every"]:
                        cand["at"] = None
                    if "at" in patch and patch["at"]:
                        cand["every"] = None
                    problems = validate_job(cand)
                    if problems:
                        return {"ok": False, "error": "; ".join(problems)}
                    j.update(cand)
                    self._save()
                    return {"ok": True, "job": self._view(j)}
        return {"ok": False, "error": f"unknown job: {job_id}"}

    def remove(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.get("id") != job_id]
            if len(self.jobs) == before:
                return {"ok": False, "error": f"unknown job: {job_id}"}
            self._save()
        return {"ok": True, "removed": job_id}

    # -- running ----------------------------------------------------------------
    def due(self) -> list[dict[str, Any]]:
        now = self._now()
        with self._lock:
            return [dict(j) for j in self.jobs if is_due(j, now)]

    def run_now(self, job_id: str, *, manual: bool = True) -> dict[str, Any]:
        with self._lock:
            job = next((dict(j) for j in self.jobs if j.get("id") == job_id), None)
        if job is None:
            return {"ok": False, "error": f"unknown job: {job_id}"}
        return self._run(job, manual=manual)

    def _run(self, job: dict[str, Any], *, manual: bool) -> dict[str, Any]:
        jid = str(job.get("id"))
        with self._lock:
            if self._running == jid:
                return {"ok": False, "error": f"job {jid} is already running"}
            self._running = jid
            # Mark the start so a crash mid-run does not re-trigger every tick.
            for j in self.jobs:
                if j.get("id") == jid:
                    j["last_run_ts"] = self._now()
        ctx = actions.base_context(job={"id": jid, "name": job.get("name")})
        t0 = time.monotonic()
        try:
            results = self._run_actions(list(job.get("then") or []), ctx, "job:" + jid)
        finally:
            with self._lock:
                self._running = None
        ms = int((time.monotonic() - t0) * 1000)
        ok = all(r.get("ok") for r in results)
        summary = {"job": jid, "name": job.get("name"), "ok": ok, "ms": ms, "ts": self._now(), "manual": manual,
                   "results": actions.compact_results(results)}
        with self._lock:
            for j in self.jobs:
                if j.get("id") == jid:
                    j["runs"] = int(j.get("runs", 0)) + 1
                    j["last"] = {k: summary[k] for k in ("ok", "ms", "ts", "manual")}
                    if not ok:
                        j["last"]["error"] = "; ".join(str(x.get("error")) for x in results if x.get("error"))[:300]
            self.history.append(summary)
            del self.history[:-HISTORY]
            self._save()
        if self.events is not None:
            try:
                self.events.emit("hub.job.ran", {k: v for k, v in summary.items() if k != "ts"}, source="hub")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": ok, **summary}

    def tick(self) -> list[dict[str, Any]]:
        """Run every due job (one after another). Returns their summaries."""
        out = []
        for job in self.due():
            out.append(self._run(job, manual=False))
        return out

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hoard-hub-jobs", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # First tick a little after boot, so apps the hub starts are up.
        self._stop.wait(min(self.tick_s, 5.0))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.tick_s)

    def close(self) -> None:
        self._stop.set()


def example_jobs() -> list[dict[str, Any]]:
    return [
        {"name": "Nightly backup", "at": "04:00", "then": [{"kind": "hub", "tool": "hub_backup_run", "args": {}}],
         "note": "A deduplicated snapshot of every app's data folder."},
        {"name": "Prune old backups", "at": "04:30", "days": ["sun"],
         "then": [{"kind": "hub", "tool": "hub_backup_prune", "args": {"keep": 14}}]},
        {"name": "Rescan the 3D library", "every": "12h",
         "then": [{"kind": "tool", "app": "vulcan", "tool": "models_rescan", "args": {}}]},
        {"name": "Morning digest event", "at": "08:00", "days": ["weekdays"],
         "then": [{"kind": "event", "type": "digest.wanted", "data": {"date": "${today}"}}],
         "note": "Something (a rule, the assistant) listens for digest.wanted and writes the digest."},
    ]
