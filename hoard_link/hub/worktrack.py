"""Jobs across apps — the ``worktrack`` facet: "what is the family doing right now?"

Apps announce long work on the bus with the canonical events
``<app>.job.queued|started|progress|done|failed|cancelled`` carrying
``{job_id, title, kind, progress (0..1), gpu, eta_s, url, error}``. Older names are mapped onto them by
``events.EVENT_ALIASES`` before they are stored, so this facet only ever sees the canonical ones (it still
canonicalises what it is handed, so a test or a replay may feed it raw events).

The facet subscribes to the event log (never blocking ``emit``: events go through a queue to its own
worker thread), keeps the active jobs and the last 500 finished in ``<data>/work.json``, and joins
``hub.lease.granted|released`` by ``owner`` to show which GPU a job holds. A job still ``queued``/``running``
without news for 6 hours is marked ``stale`` (the app probably died). When a job fails the facet emits
``work.failed {app, job_id, title, error}`` and tells the person through the ``notify`` facet.

HTTP: ``GET /api/work?active=1&app=&limit=&failed=1``, ``GET /api/work/stats``. Tool ``hub_work``.
Python: :meth:`WorkFacet.active` (the Today facet uses it), :meth:`~WorkFacet.recent`, :meth:`~WorkFacet.stats`.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from typing import Any, Callable, Optional

from .events import SOFT_ALIASES, canonicalize, normalize_type
from .facets import Facet, Request

STALE_AFTER_S = 6 * 3600.0
STALE_DROP_S = 7 * 86400.0
KEEP_FINISHED = 500
_JOB_RE = re.compile(r"^(?P<app>[a-z0-9_\-]+)\.job\.(?P<state>queued|started|progress|paused|done|failed|cancelled)$")
_STATUS = {"queued": "queued", "started": "running", "progress": "running", "done": "done", "failed": "failed",
           "cancelled": "cancelled", "paused": "paused"}
_FINAL = ("done", "failed", "cancelled", "stale")


def _lang(hub: Any) -> str:
    """``es`` or ``en`` for the texts the hub writes itself (``language`` auto follows the machine, Spanish by default)."""
    lang = str(getattr(getattr(hub, "config", None), "language", "auto") or "auto").lower()
    if lang in ("es", "en"):
        return lang
    env = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return "en" if env.startswith("en") else "es"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _progress(value: Any) -> Optional[float]:
    v = _num(value)
    if v is None:
        return None
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))


class WorkFacet(Facet):
    id = "worktrack"
    ui_scripts = ("work.js",)

    def __init__(self, hub: Any, *, now: Callable[[], float] = time.time):
        super().__init__(hub)
        self._now = now
        self._lock = threading.RLock()
        self._active: dict[str, dict[str, Any]] = {}
        self._finished: list[dict[str, Any]] = []
        self._leases: dict[str, dict[str, Any]] = {}          # lease_id -> {owner, gpu, vram_mb, purpose}
        self._queue: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._off: Optional[Callable[[], None]] = None
        self._stop = threading.Event()
        self._dirty = False
        self.path = os.path.join(hub.config.data_dir, "work.json")
        self._load()

    # -- persistence -------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        with self._lock:
            self._active = {j["key"]: j for j in raw.get("active", []) if isinstance(j, dict) and j.get("key")}
            self._finished = [j for j in raw.get("finished", []) if isinstance(j, dict)][-KEEP_FINISHED:]

    def _save(self) -> None:
        with self._lock:
            payload = {"active": list(self._active.values()), "finished": self._finished[-KEEP_FINISHED:]}
            self._dirty = False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, default=str)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        self._off = self.hub.events.subscribe(self._on_event)
        self._thread = threading.Thread(target=self._loop, name="hoard-hub-worktrack", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._off:
            try:
                self._off()
            except Exception:  # noqa: BLE001
                pass
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._dirty:
            self._save()

    def _on_event(self, event: dict[str, Any]) -> None:
        t = str(event.get("type") or "")
        if ".job." in t or t.startswith("hub.lease.") or t in SOFT_ALIASES:
            self._queue.put(event)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self._queue.get(timeout=1.0)
            except queue.Empty:
                if self._dirty:
                    self._save()
                continue
            if ev is None:
                self._queue.task_done()
                break
            try:
                self.handle_event(ev)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._queue.task_done()
            if self._queue.empty() and self._dirty:
                self._save()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the worker has processed everything queued so far (tests, tools)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    # -- events --------------------------------------------------------------------------------
    def handle_event(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Apply one event; returns the job it touched (or None). Public so tests and replays can drive it."""
        etype = normalize_type(event.get("type"))
        if etype.startswith("hub.lease."):
            self._lease(etype.rsplit(".", 1)[1], event.get("data") or {})
            return None
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        etype, data = canonicalize(etype, data, soft=True)
        m = _JOB_RE.match(etype)
        if not m:
            return None
        app, state = m.group("app"), m.group("state")
        ts = float(event.get("ts") or self._now())
        title = str(data.get("title") or "").strip()
        kind = str(data.get("kind") or "").strip()
        job_id = data.get("job_id")
        job_id = str(job_id) if job_id not in (None, "") else ""
        if not job_id:
            job_id = f"{kind}:{title}" if (kind or title) else f"event-{event.get('id') or int(ts * 1000)}"
        key = f"{app}:{job_id}"
        failed_job: Optional[dict[str, Any]] = None
        with self._lock:
            job = self._active.get(key)
            if job is None:
                job = {"key": key, "app": app, "job_id": job_id, "title": title or job_id, "kind": kind, "status": "queued",
                       "progress": None, "gpu": None, "eta_s": None, "url": "", "error": "", "queued_ts": ts, "started_ts": None,
                       "updated_ts": ts, "finished_ts": None, "event_id": event.get("id")}
                self._active[key] = job
            if title:
                job["title"] = title
            if kind:
                job["kind"] = kind
            if data.get("url"):
                job["url"] = str(data["url"])
            gpu = data.get("gpu")
            if gpu is not None and gpu != "" and gpu is not False:       # 0 is a GPU too
                job["gpu"] = gpu
            eta = _num(data.get("eta_s"))
            if eta is not None:
                job["eta_s"] = eta
            prog = _progress(data.get("progress"))
            if prog is not None:
                job["progress"] = prog
            job["updated_ts"] = ts
            job["status"] = _STATUS[state]
            if state == "paused":
                job["eta_s"] = None
                job["error"] = str(data.get("error") or "")[:300]
            elif state in ("started", "progress"):
                job["error"] = ""
            if state in ("started", "progress") and not job["started_ts"]:
                job["started_ts"] = ts
            if state in _FINAL:
                job["finished_ts"] = ts
                job["eta_s"] = None
                if state == "done":
                    job["progress"] = 1.0
                if state == "failed":
                    job["error"] = str(data.get("error") or job.get("error") or "")[:300]
                    failed_job = dict(job)
                self._active.pop(key, None)
                self._finished.append(job)
                del self._finished[:-KEEP_FINISHED]
            self._dirty = True
            out = dict(job)
        if failed_job is not None:
            self._on_failed(failed_job)
        return out

    def _lease(self, what: str, data: dict[str, Any]) -> None:
        lid = str(data.get("lease_id") or "")
        if not lid:
            return
        with self._lock:
            if what == "granted":
                self._leases[lid] = {"owner": str(data.get("owner") or ""), "gpu": data.get("gpu"),
                                     "vram_mb": data.get("vram_mb"), "purpose": data.get("purpose")}
            elif what == "released":
                self._leases.pop(lid, None)

    def _lease_of(self, app: str) -> Optional[dict[str, Any]]:
        a = app.lower()
        for lease in self._leases.values():
            owner = lease["owner"].lower()
            if owner == a or owner.startswith(a + ":") or owner.startswith(a + "-") or owner.startswith(a + "/"):
                return lease
        return None

    def _on_failed(self, job: dict[str, Any]) -> None:
        app, title, error = job["app"], job["title"], job.get("error") or ""
        try:
            self.hub.events.emit("work.failed", {"app": app, "job_id": job["job_id"], "title": title, "error": error}, source="hub")
        except Exception:  # noqa: BLE001
            pass
        notify = self.hub.facet("notify")
        if notify is None or not callable(getattr(notify, "send", None)):
            return
        es = _lang(self.hub) == "es"
        head = f"Trabajo fallido en {app}: {title}" if es else f"Job failed in {app}: {title}"
        try:
            notify.send(head, error, app="hub", priority="high", url=job.get("url") or "", group="job",
                        dedupe_key=f"{app}:{job.get('kind') or 'job'}:failed")
        except Exception:  # noqa: BLE001
            pass

    # -- reading ---------------------------------------------------------------------------------
    def _decorate(self, job: dict[str, Any], now: float) -> dict[str, Any]:
        j = dict(job)
        end = j.get("finished_ts") or now
        begin = j.get("started_ts") or j.get("queued_ts") or end
        j["elapsed_s"] = max(0, int(end - begin))
        j["stale"] = j["status"] in ("queued", "running") and now - float(j.get("updated_ts") or 0) > STALE_AFTER_S
        if j["status"] == "running":            # a queued job holds nothing yet
            lease = self._lease_of(j["app"])
            if lease:
                if j.get("gpu") in (None, ""):
                    j["gpu"] = lease.get("gpu")
                j["lease"] = {"gpu": lease.get("gpu"), "vram_mb": lease.get("vram_mb"), "purpose": lease.get("purpose")}
        return j

    def _sweep(self, now: float) -> None:
        """A job stale for a week is archived as ``stale`` instead of lingering in the active list."""
        with self._lock:
            for key, j in list(self._active.items()):
                if j["status"] != "paused" and now - float(j.get("updated_ts") or 0) > STALE_DROP_S:
                    j["status"] = "stale"
                    j["finished_ts"] = j.get("updated_ts")
                    self._active.pop(key, None)
                    self._finished.append(j)
                    self._dirty = True
            del self._finished[:-KEEP_FINISHED]

    def active(self, app: Optional[str] = None, include_stale: bool = False) -> list[dict[str, Any]]:
        """Jobs queued or running now (oldest first). Stale ones (no news for 6 h) are left out unless asked."""
        now = self._now()
        self._sweep(now)
        with self._lock:
            jobs = [self._decorate(j, now) for j in self._active.values() if not app or j["app"] == app]
        if not include_stale:
            jobs = [j for j in jobs if not j["stale"]]
        jobs.sort(key=lambda j: j.get("started_ts") or j.get("queued_ts") or 0)
        return jobs

    def recent(self, limit: int = 50, app: Optional[str] = None, failed_only: bool = False) -> list[dict[str, Any]]:
        """Finished jobs, newest first."""
        now = self._now()
        with self._lock:
            rows = [self._decorate(j, now) for j in reversed(self._finished)
                    if (not app or j["app"] == app) and (not failed_only or j["status"] == "failed")]
        return rows[: max(1, min(int(limit or 50), KEEP_FINISHED))]

    def stats(self) -> dict[str, Any]:
        now = self._now()
        act = self.active(include_stale=True)
        day = [j for j in self.recent(KEEP_FINISHED) if (j.get("finished_ts") or 0) >= now - 86400]
        by_app: dict[str, dict[str, int]] = {}
        for j in act:
            by_app.setdefault(j["app"], {"active": 0, "done_24h": 0, "failed_24h": 0})["active"] += 1
        for j in day:
            row = by_app.setdefault(j["app"], {"active": 0, "done_24h": 0, "failed_24h": 0})
            if j["status"] == "done":
                row["done_24h"] += 1
            elif j["status"] == "failed":
                row["failed_24h"] += 1
        durations: dict[str, list[int]] = {}
        for j in day:
            if j["status"] == "done" and j.get("kind"):
                durations.setdefault(j["kind"], []).append(j["elapsed_s"])
        return {"ok": True, "active": len([j for j in act if not j["stale"]]), "queued": sum(1 for j in act if j["status"] == "queued"),
                "running": sum(1 for j in act if j["status"] == "running"), "stale": sum(1 for j in act if j["stale"]),
                "done_24h": sum(1 for j in day if j["status"] == "done"), "failed_24h": sum(1 for j in day if j["status"] == "failed"),
                "by_app": by_app, "avg_s_by_kind": {k: int(sum(v) / len(v)) for k, v in durations.items()},
                "leases": len(self._leases)}

    # -- HTTP / tools ----------------------------------------------------------------------------------
    def get(self, req: Request) -> Optional[Any]:
        if req.path == "/api/work/stats":
            return self.stats()
        if req.path != "/api/work":
            return None
        app = req.q("app") or None
        limit = req.q_int("limit", 50)
        active = self.active(app, include_stale=req.q("stale") not in ("0", "false"))
        out: dict[str, Any] = {"ok": True, "active": active,
                               "counts": {"active": len([j for j in active if not j["stale"]]),
                                          "stale": len([j for j in active if j["stale"]])}}
        if not req.q_bool("active"):
            out["finished"] = self.recent(limit, app, failed_only=req.q_bool("failed"))
        return out

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [{
            "name": "hub_work",
            "description": "Jobs the apps are running now. Keywords: trabajos en marcha, qué se está haciendo, progress, jobs.\n"
                           "Running or queued jobs with app, title, kind, status, progress 0..1, GPU held, eta_s, elapsed_s, stale. "
                           "Set active=false to include the latest finished and failed ones.",
            "inputSchema": {"type": "object", "properties": {
                "active": {"type": "boolean", "default": True, "description": "Only jobs not finished yet."},
                "app": {"type": "string", "description": "Only this app (id)."},
                "limit": {"type": "integer", "default": 20, "description": "Finished jobs to list when active=false."}},
                "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }]

    def handlers(self) -> dict[str, Any]:
        def hub_work(a: dict[str, Any]) -> dict[str, Any]:
            app = str(a.get("app") or "") or None
            only_active = a.get("active", True) is not False
            out: dict[str, Any] = {"ok": True, "active": self.active(app, include_stale=True), "stats": self.stats()}
            if not only_active:
                out["finished"] = self.recent(int(a.get("limit") or 20), app)
            return out
        return {"hub_work": hub_work}


__all__ = ["WorkFacet"]
