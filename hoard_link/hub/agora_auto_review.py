"""Opt-in review and correction dispatch from Agora's committed task events."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from . import agora_social, agora_worker_profiles
from .agora_capacity import codex_observation

ROUTE = "/api/agora/console/auto-review"
TOOL = "hub_agora_auto_review_status"
SERVICE = "agora-automation"
EVENTS = "agora.task.review|agora.task.reviewed"
PROVIDERS = ("claude", "codex", "cursor", "faustus")
PENDING = ("queued", "waiting_quota", "waiting_author")
ACTIVE = ("starting", "running", "stopping", "unknown")
LOG = logging.getLogger(__name__)


def init(db: Any) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS worker_automation (
        id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        kind TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        detail TEXT NOT NULL DEFAULT '', worker_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, UNIQUE(task_id, revision, kind)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS worker_automation_worker ON worker_automation(worker_id)")


def status(console: Any) -> dict[str, Any]:
    with console._db_lock, console._db() as db:
        rows = db.execute("SELECT a.*, w.agent_id, w.parent_id AS provider, w.state AS worker_state "
                          "FROM worker_automation a LEFT JOIN workers w ON w.id=a.worker_id "
                          "ORDER BY a.id DESC LIMIT 50").fetchall()
    return {"ok": True, "space_id": console.space_id, "jobs": [dict(row) for row in rows]}


def cancel(console: Any, job_id: Any) -> dict[str, Any]:
    if type(job_id) is not int or job_id < 1:
        return {"ok": False, "status": 400, "error": "job_id must be a positive integer"}
    with console._db_lock, console._db() as db:
        row = db.execute("SELECT * FROM worker_automation WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return {"ok": False, "status": 404, "error": "No such automatic review job"}
        if row["state"] in (*PENDING, "starting", "handover"):
            db.execute("UPDATE worker_automation SET state='cancelled', detail='' WHERE id=?", (job_id,))
        worker = db.execute("SELECT state FROM workers WHERE id=?", (row["worker_id"],)).fetchone()
    if worker and worker["state"] in ACTIVE:
        result = console._stop_worker(row["worker_id"])
        if not result.get("ok"):
            return result
    return status(console)


def review_receipt(console: Any, worker: dict[str, Any]) -> dict[str, Any] | None:
    if not worker.get("id"):
        return None
    with console._db_lock, console._db() as db:
        job = db.execute("SELECT id,task_id,revision FROM worker_automation WHERE worker_id=? AND kind='review'",
                         (worker["id"],)).fetchone()
    if job is None and worker.get("template") != "review":
        return None
    events = getattr(console.hub, "events", None)
    candidates = events.query(type="agora.task.reviewed", source="hub", since_id=job["id"] if job else 0,
                              since_ts=None if job else worker["created_at"], limit=2000) if events else []
    vote = next((e["data"] for e in candidates if e["data"].get("space_id") == console.space_id
                 and e["data"].get("agent") == worker["agent_id"] and (
                     not job or (e["data"].get("task_id") == job["task_id"]
                                 and e["data"].get("submission_revision") == job["revision"]))), None)
    out: dict[str, Any] = {"task_ids": [], "workspaces": [], "issues": []}
    if vote:
        out.update(reviewed_task_id=vote["task_id"], reviewed_revision=vote["submission_revision"],
                   verdict=vote["verdict"])
    else:
        out["issues"] = [f"Falta el voto de revisión para #{job['task_id']} r{job['revision']}." if job
                         else "No consta ningún voto de revisión."]
    return out


def _quota_available(provider: str, pools: list[dict[str, Any]]) -> bool:
    local = [p for p in pools if f"{provider}-capacity" in p.get("agent_ids", [])]
    if not local and len(pools) != 1:
        return False
    for pool in local or pools:
        if pool.get("stale"):
            continue
        windows = [w["remaining_percent"] for w in pool.get("windows", []) if not w.get("stale")
                   and w.get("measurement") == "measured" and w.get("remaining_percent") is not None]
        if windows and min(windows) > 0:
            return True
    return False


def _choose(console: Any, order: list[str]) -> tuple[str, str] | None:
    pools = {p["provider_id"]: p.get("pools", []) for p in console._agora().usage().get("providers", [])}
    quota = [provider for provider in order if _quota_available(provider, pools.get(provider, []))]
    codex = codex_observation()
    if codex and not codex.get("ok") and "codex" in quota:
        quota.remove("codex")
    if not quota:
        return None
    vendors = console._provider_data()
    preferences = console._preferences()
    for provider in quota:
        vendor = vendors.get(provider, {})
        catalog = vendor.get("catalog") or []
        if vendor.get("state") != "available" or not catalog:
            continue
        ids = {model["id"] for model in catalog}
        preferred = preferences.get(provider, {}).get("model")
        if preferred in ids:
            return provider, preferred
        if provider == "claude" and "haiku" in ids:
            return provider, "haiku"
        current = vendor.get("current_model")
        return provider, current if current in ids else catalog[0]["id"]
    return None


def _owner_inactive(console: Any, agent: str) -> tuple[bool, bool]:
    agora = console._agora()
    presence = agora.db.one("SELECT last_seen FROM agents WHERE id=?", (agent,))
    seen = float(presence["last_seen"] or 0) if presence else 0.0
    with console._db_lock, console._db() as db:
        worker = db.execute("SELECT state,updated_at FROM workers WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
                            (agent,)).fetchone()
    if worker and worker["state"] in ACTIVE:
        return False, False
    if worker and worker["updated_at"] >= seen and worker["state"] in ("completed", "incomplete", "failed", "stopped"):
        return True, agora.clock() - seen < agora.handover_idle_s
    return agora.clock() - seen >= agora.handover_idle_s, False


def _assignment(agora: Any, task: dict[str, Any], kind: str) -> str:
    revision = task["submission_revision"]
    history = agora.thread_recent({"thread_id": task["thread_id"], "limit": 20}).get("messages", [])
    note = next((m["body"] for m in reversed(history) if (
        kind == "review" and m["kind"] == "proposal" and m["author"] == task["owner"]
        and f"\nSubmission revision: {revision}\n" in m["body"]
    ) or (
        kind == "fix" and m["kind"] == "changes" and m["author"] == task["reviewer"]
        and m["body"].startswith(f"Reviewed submission revision: {revision}\n")
    )), "Read the task thread for the submission and its review notes.")
    next_step = ("Independently review this exact revision. Read the whole task and vote on it under your own identity."
                 if kind == "review" else
                 "Read the review and wait for the Agora handover of this exact task after your heartbeat. Confirm you "
                 "are its new owner before editing. Start from the submitted commits in an isolated worktree, retain the "
                 "author's changes, address the review, and submit a new revision of the same task.")
    commits = ", ".join(task["commits"])[:1600] or "see task thread"
    repository = agora.repo_lookup(task["repo"]) if task["repo"] and agora.repo_lookup else None
    repo = task["repo"] + (f" ({repository})" if repository else "")
    return (f"#{task['id']} r{revision}: {task['title']}\n{next_step}\n\n"
            f"Repository: {repo}\nBranch: {task['branch'] or ''}\nCommits: {commits}\n"
            f"Task thread: {task['thread_id']}\n\nTask context:\n{task['body'][:2600]}"
            f"\nAcceptance: {task['acceptance'][:1200]}"
            f"\n\nSubmission or review notes:\n{note[:2800]}")


class AutoReview:
    def __init__(self, root: Any):
        self.root = root
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.unsubscribe = None
        self.thread = None

    def start(self) -> None:
        events = getattr(self.root.hub, "events", None)
        if events is None or self.thread is not None:
            return
        self.unsubscribe = events.subscribe(self._wake_on_task)
        self.thread = threading.Thread(target=self._run, daemon=True, name="agora-automatic-review")
        self.thread.start()

    def close(self) -> None:
        if self.unsubscribe:
            self.unsubscribe()
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _wake_on_task(self, event: dict[str, Any]) -> None:
        if event["type"] in (*EVENTS.split("|"), "agora.usage.reported", "agora.agent.heartbeat"):
            self.wake.set()

    def _run(self) -> None:
        while not self.stop.is_set():
            self.wake.clear()
            with self.root._db_lock:
                views = (self.root, *self.root._space_views.values())
            for console in views:
                if self.stop.is_set():
                    break
                try:
                    self.process(console)
                except Exception:
                    LOG.exception("Automatic review dispatch failed in %s", console.space_id)
            self.wake.wait(10)

    def _events(self, console: Any) -> None:
        with console._db_lock, console._db() as db:
            profile = db.execute("SELECT auto_cursor, json_extract(profile,'$.auto_review') AS enabled "
                                 "FROM worker_profile WHERE id=1").fetchone()
            if not profile or not profile["enabled"]:
                return
            events = console.hub.events.query(since_id=profile["auto_cursor"], type=EVENTS, source="hub",
                                              newest_first=False, limit=200)
            for event in events:
                data = event["data"]
                if data.get("space_id") != console.space_id:
                    continue
                kind = "review" if event["type"] == "agora.task.review" else "fix"
                if kind == "fix" and data.get("verdict") != "changes":
                    continue
                db.execute("INSERT OR IGNORE INTO worker_automation(id,task_id,revision,kind,owner,created_at) "
                           "VALUES(?,?,?,?,?,?)", (event["id"], data["task_id"], data["submission_revision"],
                                                   kind, data["agent"] if kind == "review" else "", event["ts"]))
            if events:
                db.execute("UPDATE worker_profile SET auto_cursor=? WHERE id=1", (events[-1]["id"],))

    @staticmethod
    def _update(console: Any, job: Any, state: str, detail: str = "", **fields: Any) -> bool:
        worker_id = fields.get("worker_id", job["worker_id"])
        owner = fields.get("owner", job["owner"])
        with console._db_lock, console._db() as db:
            return bool(db.execute("UPDATE worker_automation SET state=?,detail=?,worker_id=?,owner=? "
                                   "WHERE id=? AND state=?", (state, detail, worker_id, owner,
                                                              job["id"], job["state"])).rowcount)

    def process(self, console: Any) -> None:
        self._events(console)
        with console._db_lock, console._db() as db:
            jobs = db.execute("SELECT * FROM worker_automation WHERE state IN "
                              "('queued','waiting_quota','waiting_author','starting','handover') ORDER BY id").fetchall()
        for job in jobs:
            if self.stop.is_set():
                return
            try:
                self._job(console, job)
            except Exception:
                LOG.exception("Automatic review job %s failed", job["id"])
                reason = "handover_failed" if job["state"] == "handover" else "dispatch_error"
                self._update(console, job, "failed", reason)

    def _job(self, console: Any, job: Any) -> None:
        agora = console._agora()
        if job["state"] == "starting":
            # A committed start with no worker receipt is ambiguous after a Hub restart.
            with console._db_lock, console._db() as db:
                worker = db.execute("SELECT state FROM workers WHERE id=?", (job["worker_id"],)).fetchone()
            if not worker or worker["state"] == "unavailable":
                self._update(console, job, "failed", "launch_unconfirmed")
            else:
                self._update(console, job, "handover" if job["kind"] == "fix" else "launched")
            return
        task = agora._task_row(job["task_id"])
        review = job["kind"] == "review"
        expected = "review" if review else "changes"
        if job["state"] == "handover" and task["owner"] != job["owner"]:
            with console._db_lock, console._db() as db:
                worker = db.execute("SELECT agent_id FROM workers WHERE id=?", (job["worker_id"],)).fetchone()
            if worker and task["owner"] == worker["agent_id"]:
                self._update(console, job, "launched")
                return
        if (task["submission_revision"] != job["revision"] or task["status"] != expected
                or (job["owner"] and task["owner"] != job["owner"])):
            if self._update(console, job, "superseded", "task_changed") and job["state"] == "handover":
                console._stop_worker(job["worker_id"])
            return
        if job["state"] == "handover":
            self._handover(console, job, task)
            return
        if not agora_worker_profiles.read(console)["auto_review"]:
            self._update(console, job, "cancelled", "profile_disabled")
            return
        owner = task["owner"]
        if not owner or owner == "luis":
            self._update(console, job, "skipped", "no_agent_owner")
            return
        provider = owner.split("-", 1)[0]
        if review and provider not in PROVIDERS:
            self._update(console, job, "skipped", "unknown_author_provider")
            return
        if not review and not _owner_inactive(console, owner)[0]:
            self._update(console, job, "waiting_author", "author_present")
            return
        order = [p for p in PROVIDERS if p != provider]
        if not review and provider in PROVIDERS:
            order.insert(0, provider)
        chosen = _choose(console, order)
        if not chosen:
            self._update(console, job, "waiting_quota", "no_provider_with_quota")
            return
        worker_id = str(uuid.uuid4())
        if not self._update(console, job, "starting", worker_id=worker_id, owner=owner):
            return
        current = {**dict(job), "state": "starting", "owner": owner, "worker_id": worker_id}
        try:
            args = {"leader_id": chosen[0], "model": chosen[1], "rank": "support",
                    "task": _assignment(agora, task, job["kind"]), "template": "review" if review else "build"}
            if self.stop.is_set() or not agora_worker_profiles.read(console)["auto_review"]:
                self._update(console, current, "cancelled", "profile_disabled")
                return
            if agora._task_row(task["id"])["etag"] != task["etag"]:
                self._update(console, current, "queued", worker_id="")
                return
            if not review and not _owner_inactive(console, owner)[0]:
                self._update(console, current, "waiting_author", "author_present", worker_id="")
                return
            with console._db_lock, console._db() as db:
                if db.execute("SELECT state FROM worker_automation WHERE id=?", (job["id"],)).fetchone()["state"] != "starting":
                    return
            result = console._create_worker(args, SERVICE, worker_id=worker_id)
        except Exception:
            self._update(console, current, "failed", "launch_failed")
            raise
        worker = result.get("worker") or {}
        if worker.get("state") in ("running", "completed", "incomplete"):
            if not self._update(console, current, "launched" if review else "handover"):
                console._stop_worker(worker_id)
        else:
            self._update(console, current, "failed", "launch_failed")

    def _handover(self, console: Any, job: Any, task: dict[str, Any]) -> None:
        agora = console._agora()
        with console._db_lock, console._db() as db:
            worker = db.execute("SELECT agent_id,state,created_at FROM workers WHERE id=?", (job["worker_id"],)).fetchone()
        if not worker or worker["state"] not in ACTIVE:
            self._update(console, job, "failed", "worker_ended_before_handover")
            return
        target = worker["agent_id"]
        seen = agora.db.one("SELECT last_seen FROM agents WHERE id=?", (target,))
        if not seen or float(seen["last_seen"] or 0) < worker["created_at"]:
            return
        if not _owner_inactive(console, task["owner"])[0]:
            if self._update(console, job, "superseded", "author_returned"):
                console._stop_worker(job["worker_id"])
            return
        # Serialize against cancellation. An earlier cancellation leaves the task and leases with its author.
        with console._db_lock:
            with console._db() as db:
                if db.execute("SELECT state FROM worker_automation WHERE id=?", (job["id"],)).fetchone()["state"] != "handover":
                    return
            with agora.db.tx():
                current = agora._task_row(task["id"])
                inactive, force = _owner_inactive(console, task["owner"])
                if not inactive or current["etag"] != task["etag"]:
                    return
                agora_social.member_add(agora, {"agent": SERVICE, "member": target})
                agora.handover({"agent": SERVICE, "from_agent": task["owner"], "to_agent": target,
                                "tasks": [task["id"]], "include_reviews": False, "force": force,
                                "reason": f"Relevo automático para recoger los cambios de la entrega r{job['revision']}."})
            self._update(console, job, "launched")
