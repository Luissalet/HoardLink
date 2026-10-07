"""Ágora: where the agents that build the family (one per chat or session) and the person coordinate their work.

Several coding agents work on the same repositories and the same machine at once. Without a shared place they
step on each other: one edits a file the other has half-changed, both restart the same app, two live evaluations
fight for the one principal model, and a disagreement about a design ends up as two contradictory commits. The hub
is the long-lived local process every agent can reach (HTTP, the MCP bridge, or ``scripts/agora.py``), so the
Ágora lives here:

* **agents** announce themselves with a heartbeat (what they are doing, whether they are working, waiting or away);
* **tasks** go ``open → claimed → in_progress → review → approved → done`` (also ``changes``, ``blocked``,
  ``dropped``). Claiming a task takes its **locks** at the same time, all or nothing;
* **locks** are leases on resources with a time-to-live that the owner's heartbeat renews:
  ``path:<Repo>/<file or dir>`` (editing; prefixes overlap), ``repo:<Repo>`` (the whole repo), ``merge:<Repo>``
  (integrating into the shared checkout / main branch), and exact names such as ``model:principal``, ``gpu:2``,
  ``port:7001`` or ``app:lumiere``. A lock whose owner stops renewing it expires by itself;
* **threads** carry the conversation: every task has one, and agents open ``debate``, ``question``, ``decision``,
  ``review`` or ``handoff`` threads. Messages can ``agree`` / ``disagree``; a thread is resolved with a written
  resolution (resolved threads are the **decision log**) or **escalated** to the person, who is notified and
  answers from the hub's page;
* every agent has an **inbox**: messages from others since its last read, reviews waiting for it, changes asked of
  it and answers to escalations. ``wait_s`` turns the inbox into a long poll so an agent can wait for a reply.

Only the hub's own page may write as the person (``luis``); agents use their own ids. Everything is stored in
``<data>/agora.db`` and announced on the event bus as ``agora.*``.

HTTP (reads): ``GET /api/agora/board|inbox|tasks|tasks/<id>|threads|threads/<id>|locks|decisions|agents``.
HTTP (writes): ``POST /api/agora/<op>`` with the same arguments as the tool ``hub_agora_<op>``.
"""

from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..sqlkit import Database
from .facets import Facet, Request

PERSON = "luis"
STALE_AGENT_S = 6 * 3600.0
DEFAULT_LOCK_TTL_S = 3 * 3600
MAX_LOCK_TTL_S = 24 * 3600
REVIEW_GRACE_S = 2 * 3600
MAX_WAIT_S = 120.0

TASK_STATUSES = ("open", "claimed", "in_progress", "review", "changes", "approved", "blocked", "done", "dropped")
ACTIVE = ("claimed", "in_progress", "review", "changes", "approved", "blocked")
FINAL = ("done", "dropped")
TASK_KINDS = ("feature", "bug", "research", "review", "chore", "eval", "docs")
THREAD_KINDS = ("task", "debate", "question", "decision", "review", "handoff", "note")
THREAD_STATUSES = ("open", "resolved", "escalated")
MSG_KINDS = ("comment", "proposal", "agree", "disagree", "approve", "changes", "resolution", "escalation", "system")
AGENT_STATES = ("working", "idle", "waiting", "away")
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,40}$")
_SCOPED = ("path", "repo", "merge")

MIGRATIONS = [
    """
    CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT, kind TEXT, note TEXT, state TEXT, doing TEXT,
                         last_seen REAL, created REAL);
    CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT, repo TEXT, paths TEXT,
                        kind TEXT, priority INTEGER, status TEXT, created_by TEXT, owner TEXT, reviewer TEXT,
                        branch TEXT, commits TEXT, result TEXT, reviewed INTEGER DEFAULT 0, thread_id INTEGER,
                        created REAL, updated REAL, claimed_at REAL, submitted_at REAL, closed_at REAL);
    CREATE TABLE locks (resource TEXT PRIMARY KEY, owner TEXT, task_id INTEGER, note TEXT, ttl_s INTEGER,
                        acquired REAL, expires REAL);
    CREATE TABLE threads (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT, task_id INTEGER,
                          status TEXT, created_by TEXT, resolution TEXT, resolved_by TEXT, created REAL, updated REAL);
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER NOT NULL, author TEXT, kind TEXT,
                           body TEXT, mentions TEXT, created REAL);
    CREATE TABLE reads (agent TEXT PRIMARY KEY, last_msg_id INTEGER DEFAULT 0, updated REAL);
    CREATE INDEX messages_thread ON messages(thread_id, id);
    CREATE INDEX tasks_status ON tasks(status, priority);
    """,
]


class AgoraError(ValueError):
    """A refused operation (bad argument, conflict, not allowed): the caller gets ``ok: False`` and the reason."""

    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


# ---- resources -------------------------------------------------------------------------------------------------

def normalize_resource(resource: Any) -> str:
    """``Path:Faustus\\src\\x.py`` → ``path:faustus/src/x.py``. Scoped kinds compare case-insensitively (Windows)."""
    raw = str(resource or "").strip()
    if ":" not in raw:
        raise AgoraError(f"resource must look like kind:name (path:Repo/file, repo:Repo, merge:Repo, model:principal): {raw!r}")
    kind, name = raw.split(":", 1)
    kind = kind.strip().lower()
    name = name.strip().replace("\\", "/")
    if not re.fullmatch(r"[a-z][a-z0-9_\-]{0,20}", kind) or not name or len(name) > 400:
        raise AgoraError(f"invalid resource: {raw!r}")
    if kind in _SCOPED:
        name = re.sub(r"/{2,}", "/", name).lower().strip()
        if kind in ("repo", "merge"):
            name = name.strip("/")
            if "/" in name:
                raise AgoraError(f"{kind}: takes a repository name, not a path: {raw!r}")
        else:
            name = name.lstrip("./")
            if "/" not in name.rstrip("/"):
                name = name.rstrip("/") + "/"          # path:Repo means the whole tree of Repo
        if ".." in name.split("/"):
            raise AgoraError(f"invalid resource: {raw!r}")
    return f"{kind}:{name}"


def _repo_of(resource: str) -> str:
    kind, name = resource.split(":", 1)
    return name.split("/", 1)[0] if kind == "path" else name


def _paths_overlap(a: str, b: str) -> bool:
    if any(c in a + b for c in "*?["):
        return fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a) or fnmatch.fnmatch(a.rstrip("/") + "/x", b) \
            or fnmatch.fnmatch(b.rstrip("/") + "/x", a)
    if a == b:
        return True
    a_dir, b_dir = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a_dir.startswith(b_dir) or b_dir.startswith(a_dir)


def conflicts(a: str, b: str) -> bool:
    """Whether two normalised resources can not be held by two owners at once."""
    ka, na = a.split(":", 1)
    kb, nb = b.split(":", 1)
    if ka in _SCOPED and kb in _SCOPED:
        if _repo_of(a) != _repo_of(b):
            return False
        kinds = {ka, kb}
        if "repo" in kinds:
            return True                          # the whole repository excludes everything in it
        if kinds == {"path"}:
            return _paths_overlap(na, nb)
        if kinds == {"merge"}:
            return True
        return False                              # editing a file never blocks someone integrating something else
    return a == b


# ---- store ---------------------------------------------------------------------------------------------------

def _j(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def _l(value: Any) -> list:
    try:
        out = json.loads(value) if value else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _txt(value: Any, limit: int, field: str, *, required: bool = False) -> str:
    out = str(value if value is not None else "").strip()
    if required and not out:
        raise AgoraError(f"{field} is required")
    if len(out) > limit:
        raise AgoraError(f"{field} is longer than {limit} characters")
    return out


def _list(value: Any, field: str, limit: int = 50) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\n]", value) if v.strip()]
    if not isinstance(value, list) or len(value) > limit:
        raise AgoraError(f"{field} must be a list (at most {limit})")
    return [str(v).strip() for v in value if str(v).strip()]


class Agora:
    """The store and every operation. Thread-safe; ``clock`` is injectable for tests."""

    def __init__(self, path: Any, *, clock: Callable[[], float] = time.time,
                 emit: Optional[Callable[[str, dict[str, Any]], Any]] = None,
                 escalate_hook: Optional[Callable[[dict[str, Any], str, str], Any]] = None,
                 review_grace_s: float = REVIEW_GRACE_S):
        self.clock = clock
        self.emit = emit or (lambda t, d: None)
        self.escalate_hook = escalate_hook
        self.review_grace_s = review_grace_s
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = Database(path, migrations=MIGRATIONS)
        self.db.migrate()
        self._changed = threading.Condition()
        self._version = 0

    def close(self) -> None:
        self.db.close()

    # -- plumbing ------------------------------------------------------------------------------------------------

    def _bump(self, event: str, data: dict[str, Any]) -> None:
        with self._changed:
            self._version += 1
            self._changed.notify_all()
        try:
            self.emit(event, data)
        except Exception:  # noqa: BLE001 — the bus must never break the Ágora
            pass

    def _agent(self, value: Any, *, allow_person: bool = False) -> str:
        agent = str(value or "").strip().lower()
        if not _AGENT_RE.match(agent):
            raise AgoraError("agent is required: a short id such as builder, reviewer or agent-2")
        if agent == PERSON and not allow_person:
            raise AgoraError(f"only the hub's page may write as {PERSON!r}; agents use their own id", 403)
        if agent in ("hub", "ui", "system"):
            raise AgoraError(f"{agent!r} is reserved")
        return agent

    def _touch(self, agent: str) -> None:
        now = self.clock()
        if agent == PERSON:
            kind = "human"
        else:
            kind = "agent"
        self.db.execute(
            "INSERT INTO agents(id, name, kind, state, last_seen, created) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
            (agent, agent, kind, "working", now, now))

    def _task_row(self, task_id: Any) -> dict[str, Any]:
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            raise AgoraError("task_id must be a number") from None
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if row is None:
            raise AgoraError(f"no task {tid}", 404)
        return self._task_dict(row)

    def _thread_row(self, thread_id: Any) -> dict[str, Any]:
        try:
            tid = int(thread_id)
        except (TypeError, ValueError):
            raise AgoraError("thread_id must be a number") from None
        row = self.db.one("SELECT * FROM threads WHERE id=?", (tid,))
        if row is None:
            raise AgoraError(f"no thread {tid}", 404)
        return dict(row)

    def _task_dict(self, row: Any) -> dict[str, Any]:
        d = dict(row)
        d["paths"] = _l(d.get("paths"))
        d["commits"] = _l(d.get("commits"))
        d["reviewed"] = bool(d.get("reviewed"))
        return d

    def _msg(self, thread_id: int, author: str, kind: str, body: str, mentions: Optional[list[str]] = None) -> int:
        now = self.clock()
        mid = self.db.insert("messages", {"thread_id": thread_id, "author": author, "kind": kind, "body": body,
                                          "mentions": _j(mentions), "created": now})
        self.db.execute("UPDATE threads SET updated=? WHERE id=?", (now, thread_id))
        return mid

    def _system(self, task: dict[str, Any], text: str) -> None:
        if task.get("thread_id"):
            self._msg(int(task["thread_id"]), "system", "system", text)

    def _live_locks(self) -> list[dict[str, Any]]:
        now = self.clock()
        self.db.execute("DELETE FROM locks WHERE expires <= ?", (now,))
        return [dict(r) for r in self.db.query("SELECT * FROM locks ORDER BY acquired")]

    def _agent_stale(self, agent: Optional[str]) -> bool:
        if not agent:
            return True
        row = self.db.one("SELECT last_seen FROM agents WHERE id=?", (agent,))
        return row is None or (self.clock() - float(row["last_seen"] or 0)) > STALE_AGENT_S

    # -- locks ---------------------------------------------------------------------------------------------------

    def _acquire(self, agent: str, resources: list[str], task_id: Optional[int], ttl_s: int, note: str) -> dict[str, Any]:
        wanted = [normalize_resource(r) for r in resources]
        live = self._live_locks()
        clashes = []
        for res in wanted:
            for lock in live:
                if lock["owner"] != agent and conflicts(res, lock["resource"]):
                    clashes.append({"resource": res, "held": lock["resource"], "owner": lock["owner"],
                                    "task_id": lock["task_id"], "note": lock["note"], "expires": lock["expires"]})
        if clashes:
            return {"ok": False, "conflicts": clashes}
        now = self.clock()
        for res in wanted:
            self.db.execute(
                "INSERT INTO locks(resource, owner, task_id, note, ttl_s, acquired, expires) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(resource) DO UPDATE SET owner=excluded.owner, task_id=COALESCE(excluded.task_id, locks.task_id), "
                "note=excluded.note, ttl_s=excluded.ttl_s, expires=excluded.expires",
                (res, agent, task_id, note, ttl_s, now, now + ttl_s))
        return {"ok": True, "locks": wanted}

    def _ttl(self, value: Any) -> int:
        if value in (None, ""):
            return DEFAULT_LOCK_TTL_S
        try:
            ttl = int(value)
        except (TypeError, ValueError):
            raise AgoraError("ttl_s must be a number of seconds") from None
        if not 60 <= ttl <= MAX_LOCK_TTL_S:
            raise AgoraError(f"ttl_s must be between 60 and {MAX_LOCK_TTL_S}")
        return ttl

    def lock(self, a: dict[str, Any]) -> dict[str, Any]:
        """Take (or renew) locks outside a task: ``model:principal`` for a live evaluation, ``merge:Repo`` to integrate."""
        agent = self._agent(a.get("agent"))
        resources = _list(a.get("resources") or a.get("resource"), "resources")
        if not resources:
            raise AgoraError("resources is required")
        task_id = int(a["task_id"]) if a.get("task_id") not in (None, "") else None
        with self.db.tx():
            self._touch(agent)
            res = self._acquire(agent, resources, task_id, self._ttl(a.get("ttl_s")), _txt(a.get("note"), 300, "note"))
        if res["ok"]:
            self._bump("agora.lock.acquired", {"agent": agent, "locks": res["locks"], "task_id": task_id})
        else:
            self._bump("agora.lock.conflict", {"agent": agent, "conflicts": res["conflicts"]})
        return res

    def unlock(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        resources = [normalize_resource(r) for r in _list(a.get("resources") or a.get("resource"), "resources")]
        released = []
        with self.db.tx():
            for res in resources:
                row = self.db.one("SELECT owner FROM locks WHERE resource=?", (res,))
                if row is None:
                    continue
                if row["owner"] != agent and agent != PERSON:
                    raise AgoraError(f"{res} belongs to {row['owner']}", 409)
                self.db.execute("DELETE FROM locks WHERE resource=?", (res,))
                released.append(res)
        if released:
            self._bump("agora.lock.released", {"agent": agent, "locks": released})
        return {"ok": True, "released": released}

    def locks(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self.db.tx():
            live = self._live_locks()
        probe = (a or {}).get("check")
        if probe:
            res = normalize_resource(probe)
            clash = [lk for lk in live if conflicts(res, lk["resource"])]
            return {"ok": True, "resource": res, "free": not clash, "held_by": clash}
        return {"ok": True, "locks": live}

    # -- agents --------------------------------------------------------------------------------------------------

    def heartbeat(self, a: dict[str, Any]) -> dict[str, Any]:
        """I am here, this is what I am doing. Renews every lock the agent holds."""
        agent = self._agent(a.get("agent"))
        state = str(a.get("state") or "working").lower()
        if state not in AGENT_STATES:
            raise AgoraError(f"state must be one of {', '.join(AGENT_STATES)}")
        now = self.clock()
        with self.db.tx():
            self._touch(agent)
            fields = {"state": state, "doing": _txt(a.get("doing"), 300, "doing")}
            if a.get("name"):
                fields["name"] = _txt(a.get("name"), 80, "name")
            if a.get("note") is not None:
                fields["note"] = _txt(a.get("note"), 500, "note")
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE agents SET {sets} WHERE id=?", (*fields.values(), agent))
            self.db.execute("UPDATE locks SET expires = ? + ttl_s WHERE owner=? AND expires > ?", (now, agent, now))
        self._bump("agora.agent.heartbeat", {"agent": agent, "state": state})
        inbox = self.inbox({"agent": agent, "peek": True})
        return {"ok": True, "agent": agent, "inbox": inbox["counts"]}

    def agents(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        now = self.clock()
        out = []
        for r in self.db.query("SELECT * FROM agents ORDER BY last_seen DESC"):
            d = dict(r)
            d["stale"] = (now - float(d.get("last_seen") or 0)) > STALE_AGENT_S
            d["locks"] = [x["resource"] for x in self.db.query("SELECT resource FROM locks WHERE owner=? AND expires>?", (d["id"], now))]
            out.append(d)
        return {"ok": True, "agents": out}

    # -- tasks ---------------------------------------------------------------------------------------------------

    def task_add(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        title = _txt(a.get("title"), 200, "title", required=True)
        body = _txt(a.get("body"), 20000, "body")
        kind = str(a.get("kind") or "feature").lower()
        if kind not in TASK_KINDS:
            raise AgoraError(f"kind must be one of {', '.join(TASK_KINDS)}")
        try:
            priority = int(a.get("priority", 2))
        except (TypeError, ValueError):
            raise AgoraError("priority must be 0 (urgent) .. 3 (someday)") from None
        if not 0 <= priority <= 3:
            raise AgoraError("priority must be 0 (urgent) .. 3 (someday)")
        paths = _list(a.get("paths"), "paths")
        repo = _txt(a.get("repo"), 120, "repo")
        now = self.clock()
        with self.db.tx():
            self._touch(agent)
            tid = self.db.insert("tasks", {"title": title, "body": body, "repo": repo, "paths": _j(paths), "kind": kind,
                                           "priority": priority, "status": "open", "created_by": agent,
                                           "created": now, "updated": now})
            th = self.db.insert("threads", {"title": title, "kind": "task", "task_id": tid, "status": "open",
                                            "created_by": agent, "created": now, "updated": now})
            self.db.execute("UPDATE tasks SET thread_id=? WHERE id=?", (th, tid))
            self._msg(th, agent, "proposal", body or title, _list(a.get("mentions"), "mentions"))
        self._bump("agora.task.added", {"task_id": tid, "title": title, "by": agent, "repo": repo, "kind": kind})
        res = {"ok": True, "task": self._task_row(tid)}
        if a.get("claim"):
            res["claim"] = self.task_claim({"agent": agent, "task_id": tid, "locks": a.get("locks"), "ttl_s": a.get("ttl_s")})
        return res

    def task_claim(self, a: dict[str, Any]) -> dict[str, Any]:
        """Take a task and its locks, all or nothing. A task held by an agent silent for 6 hours can be taken with force."""
        agent = self._agent(a.get("agent"))
        ttl = self._ttl(a.get("ttl_s"))
        with self.db.tx():
            self._touch(agent)
            task = self._task_row(a.get("task_id"))
            if task["status"] in FINAL:
                raise AgoraError(f"task {task['id']} is {task['status']}", 409)
            owner = task.get("owner")
            if owner and owner != agent and task["status"] in ACTIVE:
                if not (a.get("force") and self._agent_stale(owner)):
                    raise AgoraError(f"task {task['id']} is held by {owner}"
                                     + ("" if self._agent_stale(owner) else " (active)"), 409, owner=owner)
                self.db.execute("DELETE FROM locks WHERE owner=? AND task_id=?", (owner, task["id"]))
                self._system(task, f"{agent} toma la tarea: {owner} lleva más de 6 h sin dar señales.")
            locks = _list(a.get("locks"), "locks") or [f"path:{task['repo']}/{p}" for p in task["paths"] if task.get("repo")]
            got = self._acquire(agent, locks, task["id"], ttl, task["title"][:120]) if locks else {"ok": True, "locks": []}
            if not got["ok"]:
                self._bump("agora.lock.conflict", {"agent": agent, "task_id": task["id"], "conflicts": got["conflicts"]})
                return {"ok": False, "status": 409, "error": "locks are held by someone else", "conflicts": got["conflicts"]}
            status = task["status"] if task["status"] in ACTIVE and owner == agent else "claimed"
            now = self.clock()
            self.db.execute("UPDATE tasks SET owner=?, status=?, claimed_at=?, updated=? WHERE id=?",
                            (agent, status, now, now, task["id"]))
            if owner != agent:
                self._system(task, f"{agent} reclama la tarea" + (f" con {', '.join(got['locks'])}" if got["locks"] else "") + ".")
        self._bump("agora.task.claimed", {"task_id": task["id"], "agent": agent, "locks": got["locks"]})
        return {"ok": True, "task": self._task_row(task["id"]), "locks": got["locks"]}

    def _owned(self, a: dict[str, Any], *, allow_person: bool = True) -> tuple[str, dict[str, Any]]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")) and allow_person)
        task = self._task_row(a.get("task_id"))
        if task.get("owner") != agent and agent != PERSON:
            raise AgoraError(f"task {task['id']} belongs to {task.get('owner') or 'nobody'}: claim it first", 409)
        return agent, task

    def task_update(self, a: dict[str, Any]) -> dict[str, Any]:
        """Progress note, status in_progress/blocked, branch, more paths (with their locks)."""
        with self.db.tx():
            agent, task = self._owned(a)
            self._touch(agent)
            status = str(a.get("status") or task["status"]).lower()
            if status not in ("claimed", "in_progress", "blocked", task["status"]):
                raise AgoraError("task_update sets claimed, in_progress or blocked; use task_submit / task_done / task_release")
            now = self.clock()
            extra = _list(a.get("locks"), "locks")
            if extra:
                got = self._acquire(task["owner"] or agent, extra, task["id"], self._ttl(a.get("ttl_s")), task["title"][:120])
                if not got["ok"]:
                    return {"ok": False, "status": 409, "error": "locks are held by someone else", "conflicts": got["conflicts"]}
            fields: dict[str, Any] = {"status": status, "updated": now}
            if a.get("branch") is not None:
                fields["branch"] = _txt(a.get("branch"), 200, "branch")
            if a.get("paths") is not None:
                fields["paths"] = _j(sorted(set(task["paths"]) | set(_list(a.get("paths"), "paths"))))
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task["id"]))
            note = _txt(a.get("note"), 20000, "note")
            if note:
                self._msg(int(task["thread_id"]), agent, "comment", note)
            elif status != task["status"]:
                self._system(task, f"{agent}: {task['status']} → {status}.")
        self._bump("agora.task.status", {"task_id": task["id"], "agent": agent, "status": status})
        return {"ok": True, "task": self._task_row(task["id"])}

    def task_submit(self, a: dict[str, Any]) -> dict[str, Any]:
        """Ask for review: what changed, where (branch/commits) and how it was verified."""
        summary = _txt(a.get("summary"), 20000, "summary", required=True)
        with self.db.tx():
            agent, task = self._owned(a, allow_person=False)
            self._touch(agent)
            if task["status"] not in ACTIVE:
                raise AgoraError(f"task {task['id']} is {task['status']}", 409)
            reviewer = str(a.get("reviewer") or "").strip().lower() or None
            if reviewer == agent:
                raise AgoraError("the reviewer must be someone else")
            now = self.clock()
            commits = _list(a.get("commits"), "commits")
            self.db.execute("UPDATE tasks SET status='review', reviewer=?, branch=COALESCE(?, branch), commits=?, "
                            "submitted_at=?, updated=? WHERE id=?",
                            (reviewer, a.get("branch"), _j(commits or task["commits"]), now, now, task["id"]))
            self._msg(int(task["thread_id"]), agent, "proposal", summary, [reviewer] if reviewer else [])
        self._bump("agora.task.review", {"task_id": task["id"], "agent": agent, "reviewer": reviewer})
        return {"ok": True, "task": self._task_row(task["id"])}

    def task_review(self, a: dict[str, Any]) -> dict[str, Any]:
        """``verdict: approve`` or ``changes``, with the reasons. Only someone other than the owner reviews."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        verdict = str(a.get("verdict") or "").lower()
        if verdict not in ("approve", "changes"):
            raise AgoraError("verdict must be approve or changes")
        body = _txt(a.get("body"), 20000, "body", required=True)
        with self.db.tx():
            self._touch(agent)
            task = self._task_row(a.get("task_id"))
            if task.get("owner") == agent:
                raise AgoraError("you can not review your own task", 403)
            if task["status"] not in ("review", "approved", "changes"):
                raise AgoraError(f"task {task['id']} is not waiting for review ({task['status']})", 409)
            status = "approved" if verdict == "approve" else "changes"
            now = self.clock()
            self.db.execute("UPDATE tasks SET status=?, reviewer=?, reviewed=?, updated=? WHERE id=?",
                            (status, agent, 1 if verdict == "approve" else 0, now, task["id"]))
            self._msg(int(task["thread_id"]), agent, "approve" if verdict == "approve" else "changes", body,
                      [task["owner"]] if task.get("owner") else [])
            if agent == PERSON:
                self.db.execute("UPDATE threads SET status='open' WHERE id=? AND status='escalated'", (task["thread_id"],))
        self._bump("agora.task.reviewed", {"task_id": task["id"], "agent": agent, "verdict": verdict})
        return {"ok": True, "task": self._task_row(task["id"])}

    def task_done(self, a: dict[str, Any]) -> dict[str, Any]:
        """Integrated: commits and result. Releases the task's locks. Refused while a review is younger than 2 h
        unless ``force`` with a ``reason``; finishing without any review is recorded as such."""
        with self.db.tx():
            agent, task = self._owned(a)
            self._touch(agent)
            if task["status"] in FINAL:
                raise AgoraError(f"task {task['id']} is already {task['status']}", 409)
            now = self.clock()
            if task["status"] == "review" and agent != PERSON:
                waited = now - float(task.get("submitted_at") or now)
                if waited < self.review_grace_s and not (a.get("force") and str(a.get("reason") or "").strip()):
                    raise AgoraError(f"task {task['id']} has been waiting for review {int(waited // 60)} min; wait "
                                     f"{int(self.review_grace_s // 60)} min or pass force with a reason", 409)
            if task["status"] == "changes" and not a.get("force"):
                raise AgoraError(f"the reviewer asked for changes on task {task['id']}: address them and submit again, "
                                 "or pass force with a reason", 409)
            commits = _list(a.get("commits"), "commits") or task["commits"]
            result = _txt(a.get("result"), 20000, "result")
            self.db.execute("UPDATE tasks SET status='done', commits=?, result=?, closed_at=?, updated=? WHERE id=?",
                            (_j(commits), result, now, now, task["id"]))
            self.db.execute("DELETE FROM locks WHERE task_id=?", (task["id"],))
            unreviewed = task["status"] != "approved"
            text = (result or "Hecho.") + ("\n\n(Integrada sin revisión aprobada" +
                                           (f": {a.get('reason')}" if a.get("reason") else "") + ")" if unreviewed else "")
            self._msg(int(task["thread_id"]), agent, "resolution", text)
            self.db.execute("UPDATE threads SET status='resolved', resolution=?, resolved_by=?, updated=? WHERE id=?",
                            (result or "done", agent, now, task["thread_id"]))
        self._bump("agora.task.done", {"task_id": task["id"], "agent": agent, "commits": commits, "reviewed": not unreviewed})
        return {"ok": True, "task": self._task_row(task["id"])}

    def task_release(self, a: dict[str, Any]) -> dict[str, Any]:
        """Give the task back (``open``) or drop it (``drop: true``) with a reason. Releases its locks."""
        reason = _txt(a.get("reason"), 5000, "reason")
        with self.db.tx():
            agent, task = self._owned(a)
            self._touch(agent)
            now = self.clock()
            drop = bool(a.get("drop"))
            self.db.execute("UPDATE tasks SET status=?, owner=?, updated=?, closed_at=? WHERE id=?",
                            ("dropped" if drop else "open", None if not drop else task.get("owner"), now,
                             now if drop else None, task["id"]))
            self.db.execute("DELETE FROM locks WHERE task_id=?", (task["id"],))
            self._msg(int(task["thread_id"]), agent, "system" if not reason else "comment",
                      reason or f"{agent} suelta la tarea.")
        self._bump("agora.task.released", {"task_id": task["id"], "agent": agent, "dropped": drop})
        return {"ok": True, "task": self._task_row(task["id"])}

    def tasks(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        a = a or {}
        where, params = [], []
        status = a.get("status")
        if status == "active":
            where.append(f"status IN ({','.join('?' * len(ACTIVE))})")
            params.extend(ACTIVE)
        elif status:
            where.append("status=?")
            params.append(str(status))
        for key in ("owner", "repo", "kind"):
            if a.get(key):
                where.append(f"lower({key})=lower(?)")
                params.append(str(a[key]))
        limit = max(1, min(int(a.get("limit") or 200), 500))
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY CASE WHEN status IN ('done','dropped') THEN 1 ELSE 0 END, priority, updated DESC LIMIT ?"
        return {"ok": True, "tasks": [self._task_dict(r) for r in self.db.query(sql, (*params, limit))]}

    def task(self, a: dict[str, Any]) -> dict[str, Any]:
        task = self._task_row(a.get("task_id"))
        msgs = self._messages(int(task["thread_id"])) if task.get("thread_id") else []
        locks = [dict(r) for r in self.db.query("SELECT * FROM locks WHERE task_id=? AND expires>?", (task["id"], self.clock()))]
        return {"ok": True, "task": task, "messages": msgs, "locks": locks}

    # -- threads -------------------------------------------------------------------------------------------------

    def _messages(self, thread_id: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (thread_id,)):
            d = dict(r)
            d["mentions"] = _l(d.get("mentions"))
            out.append(d)
        return out

    def thread_open(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        title = _txt(a.get("title"), 200, "title", required=True)
        body = _txt(a.get("body"), 20000, "body", required=True)
        kind = str(a.get("kind") or "debate").lower()
        if kind not in THREAD_KINDS or kind == "task":
            raise AgoraError(f"kind must be one of {', '.join(k for k in THREAD_KINDS if k != 'task')}")
        task_id = None
        if a.get("task_id") not in (None, ""):
            task_id = self._task_row(a.get("task_id"))["id"]
        mentions = _list(a.get("mentions"), "mentions")
        now = self.clock()
        with self.db.tx():
            self._touch(agent)
            th = self.db.insert("threads", {"title": title, "kind": kind, "task_id": task_id, "status": "open",
                                            "created_by": agent, "created": now, "updated": now})
            self._msg(th, agent, "proposal", body, mentions)
        self._bump("agora.thread.opened", {"thread_id": th, "kind": kind, "title": title, "by": agent})
        if a.get("escalate"):
            return self.escalate({"agent": agent, "thread_id": th, "question": body, "_person": a.get("_person")})
        return {"ok": True, "thread": self._thread_row(th)}

    def post(self, a: dict[str, Any]) -> dict[str, Any]:
        person = bool(a.get("_person"))
        agent = self._agent(a.get("agent"), allow_person=person)
        body = _txt(a.get("body"), 20000, "body", required=True)
        kind = str(a.get("kind") or "comment").lower()
        if kind not in ("comment", "proposal", "agree", "disagree"):
            raise AgoraError("kind must be comment, proposal, agree or disagree (reviews go through task_review, "
                             "resolutions through resolve)")
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            mid = self._msg(th["id"], agent, kind, body, _list(a.get("mentions"), "mentions"))
            if (th["status"] == "resolved" and kind in ("disagree", "proposal")) or \
                    (th["status"] == "escalated" and agent == PERSON):
                self.db.execute("UPDATE threads SET status='open' WHERE id=?", (th["id"],))
        self._bump("agora.thread.message", {"thread_id": th["id"], "message_id": mid, "author": agent, "kind": kind})
        return {"ok": True, "message_id": mid, "thread": self._thread_row(th["id"])}

    def resolve(self, a: dict[str, Any]) -> dict[str, Any]:
        """Close a thread with the resolution in writing. Resolved debates and decisions are the decision log."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        resolution = _txt(a.get("resolution"), 20000, "resolution", required=True)
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            if th["kind"] == "task":
                raise AgoraError("a task thread closes with task_done / task_release", 409)
            now = self.clock()
            self._msg(th["id"], agent, "resolution", resolution)
            self.db.execute("UPDATE threads SET status='resolved', resolution=?, resolved_by=?, updated=? WHERE id=?",
                            (resolution, agent, now, th["id"]))
        self._bump("agora.thread.resolved", {"thread_id": th["id"], "by": agent, "title": th["title"]})
        return {"ok": True, "thread": self._thread_row(th["id"])}

    def escalate(self, a: dict[str, Any]) -> dict[str, Any]:
        """No agreement (or it is the person's call): the thread waits for the person, who is notified."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        question = _txt(a.get("question"), 5000, "question", required=True)
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            now = self.clock()
            self._msg(th["id"], agent, "escalation", question, [PERSON])
            self.db.execute("UPDATE threads SET status='escalated', updated=? WHERE id=?", (now, th["id"]))
        self._bump("agora.thread.escalated", {"thread_id": th["id"], "by": agent, "title": th["title"]})
        if self.escalate_hook:
            try:
                self.escalate_hook(th, agent, question)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "thread": self._thread_row(th["id"])}

    def reopen(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        reason = _txt(a.get("reason"), 5000, "reason", required=True)
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            self._msg(th["id"], agent, "comment", reason)
            self.db.execute("UPDATE threads SET status='open', updated=? WHERE id=?", (self.clock(), th["id"]))
        self._bump("agora.thread.reopened", {"thread_id": th["id"], "by": agent})
        return {"ok": True, "thread": self._thread_row(th["id"])}

    def threads(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        a = a or {}
        where, params = [], []
        if a.get("status"):
            where.append("t.status=?")
            params.append(str(a["status"]))
        if a.get("kind"):
            where.append("t.kind=?")
            params.append(str(a["kind"]))
        elif not a.get("include_tasks"):
            where.append("(t.kind<>'task' OR t.status='escalated')")
        limit = max(1, min(int(a.get("limit") or 100), 500))
        sql = ("SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id) AS messages, "
               "(SELECT author FROM messages m WHERE m.thread_id=t.id ORDER BY id DESC LIMIT 1) AS last_author "
               "FROM threads t" + (" WHERE " + " AND ".join(where) if where else "") +
               " ORDER BY CASE t.status WHEN 'escalated' THEN 0 WHEN 'open' THEN 1 ELSE 2 END, t.updated DESC LIMIT ?")
        return {"ok": True, "threads": [dict(r) for r in self.db.query(sql, (*params, limit))]}

    def thread(self, a: dict[str, Any]) -> dict[str, Any]:
        th = self._thread_row(a.get("thread_id"))
        stances: dict[str, str] = {}
        msgs = self._messages(th["id"])
        for m in msgs:
            if m["kind"] in ("agree", "disagree", "proposal"):
                stances[m["author"]] = m["kind"]
        return {"ok": True, "thread": th, "messages": msgs, "stances": stances,
                "task": self._task_row(th["task_id"]) if th.get("task_id") else None}

    def decisions(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        limit = max(1, min(int((a or {}).get("limit") or 50), 500))
        rows = self.db.query("SELECT id, title, kind, resolution, resolved_by, updated FROM threads "
                             "WHERE status='resolved' AND kind<>'task' ORDER BY updated DESC LIMIT ?", (limit,))
        return {"ok": True, "decisions": [dict(r) for r in rows]}

    # -- inbox ---------------------------------------------------------------------------------------------------

    def inbox(self, a: dict[str, Any]) -> dict[str, Any]:
        """What is new for this agent: messages from others since the last read (``for_you`` when it is mentioned,
        owns the task or took part), reviews waiting for it, changes asked of it, escalations answered.
        ``wait_s`` (≤120) waits for something new; ``peek`` does not move the read mark."""
        agent = self._agent(a.get("agent"), allow_person=True)
        wait = 0.0
        try:
            wait = max(0.0, min(float(a.get("wait_s") or 0), MAX_WAIT_S))
        except (TypeError, ValueError):
            pass
        deadline = self.clock() + wait
        while True:
            with self._changed:
                version = self._version
            out = self._inbox_once(agent, a)
            if out["counts"]["total"] or wait <= 0 or self.clock() >= deadline:
                break
            with self._changed:
                if self._version == version:
                    self._changed.wait(timeout=max(0.05, min(5.0, deadline - self.clock())))
        if not a.get("peek") and out["messages"]:
            upto = max(m["id"] for m in out["messages"])
            self.db.execute("INSERT INTO reads(agent, last_msg_id, updated) VALUES(?,?,?) ON CONFLICT(agent) DO "
                            "UPDATE SET last_msg_id=MAX(reads.last_msg_id, excluded.last_msg_id), updated=excluded.updated",
                            (agent, upto, self.clock()))
        return out

    def _inbox_once(self, agent: str, a: dict[str, Any]) -> dict[str, Any]:
        row = self.db.one("SELECT last_msg_id FROM reads WHERE agent=?", (agent,))
        since = int(a.get("since_id") or (row["last_msg_id"] if row else 0))
        limit = max(1, min(int(a.get("limit") or 100), 500))
        msgs = []
        rows = self.db.query(
            "SELECT m.*, t.title AS thread_title, t.kind AS thread_kind, t.status AS thread_status, t.task_id AS task_id, "
            "t.created_by AS thread_by, k.owner AS task_owner FROM messages m JOIN threads t ON t.id=m.thread_id "
            "LEFT JOIN tasks k ON k.id=t.task_id WHERE m.id>? AND m.author<>? ORDER BY m.id LIMIT ?",
            (since, agent, limit))
        took_part = {r["thread_id"] for r in self.db.query("SELECT DISTINCT thread_id FROM messages WHERE author=?", (agent,))}
        for r in rows:
            d = dict(r)
            d["mentions"] = _l(d.get("mentions"))
            d["for_you"] = (agent in d["mentions"] or d.get("task_owner") == agent or d["thread_id"] in took_part
                            or d.get("thread_by") == agent or d["thread_kind"] in ("debate", "question", "decision", "handoff")
                            or d["author"] == PERSON)
            msgs.append(d)
        if a.get("mine_only"):
            msgs = [m for m in msgs if m["for_you"]]
        reviews = [self._task_dict(r) for r in self.db.query(
            "SELECT * FROM tasks WHERE status='review' AND owner<>? AND (reviewer IS NULL OR reviewer='' OR reviewer=?) "
            "ORDER BY submitted_at", (agent, agent))]
        changes = [self._task_dict(r) for r in self.db.query(
            "SELECT * FROM tasks WHERE status='changes' AND owner=?", (agent,))]
        escalated = [dict(r) for r in self.db.query(
            "SELECT t.*, (SELECT body FROM messages m WHERE m.thread_id=t.id AND m.kind='escalation' ORDER BY m.id DESC LIMIT 1) "
            "AS question, (SELECT author FROM messages m WHERE m.thread_id=t.id AND m.kind='escalation' ORDER BY m.id DESC "
            "LIMIT 1) AS escalated_by FROM threads t WHERE t.status='escalated' ORDER BY t.updated")] if agent == PERSON else []
        counts = {"messages": len(msgs), "for_you": sum(1 for m in msgs if m["for_you"]), "reviews": len(reviews),
                  "changes": len(changes), "escalated": len(escalated)}
        counts["total"] = counts["messages"] + counts["reviews"] + counts["changes"] + counts["escalated"]
        return {"ok": True, "agent": agent, "since_id": since, "counts": counts, "messages": msgs,
                "reviews": reviews, "changes": changes, "escalated": escalated}

    # -- board ---------------------------------------------------------------------------------------------------

    def board(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self.db.tx():
            live = self._live_locks()
        counts = {s: 0 for s in TASK_STATUSES}
        for r in self.db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"):
            counts[r["status"]] = r["n"]
        active = self.tasks({"limit": 300})["tasks"]
        tasks = [t for t in active if t["status"] not in FINAL] + \
            [self._task_dict(r) for r in self.db.query(
                "SELECT * FROM tasks WHERE status IN ('done','dropped') ORDER BY closed_at DESC LIMIT 15")]
        return {"ok": True, "agents": self.agents()["agents"], "counts": counts, "tasks": tasks, "locks": live,
                "threads": self.threads({"limit": 40})["threads"],
                "escalated": self.db.scalar("SELECT COUNT(*) FROM threads WHERE status='escalated'", default=0),
                "decisions": self.decisions({"limit": 8})["decisions"]}


# ---- facet ---------------------------------------------------------------------------------------------------

_A = {"type": "string", "description": "Your agent id, the same in every call: builder, reviewer, agent-2… (never the person's)"}
_TASK = {"type": "integer"}
_THREAD = {"type": "integer"}
_STRS = {"type": "array", "items": {"type": "string"}}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS: list[dict[str, Any]] = [
    {"name": "hub_agora_board", "read": True,
     "description": "Ágora (shared workspace of the coding agents and the person): agents and what they do, tasks by status, "
                    "active locks, open/escalated threads and recent decisions. Read this when you start a session.",
     "inputSchema": _schema({}, [])},
    {"name": "hub_agora_inbox", "read": True,
     "description": "Your Ágora inbox: messages from the others since your last read (for_you when it concerns you), "
                    "reviews waiting for you and changes asked of you. wait_s (<=60 here) waits for something new; "
                    "peek keeps the read mark.",
     "inputSchema": _schema({"agent": _A, "wait_s": {"type": "number"}, "peek": {"type": "boolean"},
                             "mine_only": {"type": "boolean"}, "since_id": {"type": "integer"}}, ["agent"])},
    {"name": "hub_agora_heartbeat",
     "description": "Tell the Ágora you are here and what you are doing (state working|idle|waiting|away). "
                    "Renews every lock you hold; call it at least every hour while working.",
     "inputSchema": _schema({"agent": _A, "doing": {"type": "string"}, "state": {"type": "string"},
                             "name": {"type": "string"}, "note": {"type": "string"}}, ["agent"])},
    {"name": "hub_agora_task_add",
     "description": "Propose a task (kind feature|bug|research|review|chore|eval|docs, priority 0 urgent..3 someday, "
                    "repo and paths it touches). claim=true also claims it with locks.",
     "inputSchema": _schema({"agent": _A, "title": {"type": "string"}, "body": {"type": "string"},
                             "repo": {"type": "string"}, "paths": _STRS, "kind": {"type": "string"},
                             "priority": {"type": "integer"}, "mentions": _STRS, "claim": {"type": "boolean"},
                             "locks": _STRS, "ttl_s": {"type": "integer"}}, ["agent", "title"])},
    {"name": "hub_agora_task_claim",
     "description": "Claim a task and take its locks all-or-nothing (path:Repo/file-or-dir, repo:Repo, merge:Repo, "
                    "model:principal, gpu:N, port:N, app:id). Default locks: path:<repo>/<each path>. On conflict "
                    "you get who holds what. force takes a task whose owner has been silent for 6 h.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "locks": _STRS, "ttl_s": {"type": "integer"},
                             "force": {"type": "boolean"}}, ["agent", "task_id"])},
    {"name": "hub_agora_task_update",
     "description": "Progress on your task: note, status in_progress|blocked, branch, more paths and locks.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "note": {"type": "string"}, "status": {"type": "string"},
                             "branch": {"type": "string"}, "paths": _STRS, "locks": _STRS, "ttl_s": {"type": "integer"}},
                            ["agent", "task_id"])},
    {"name": "hub_agora_task_submit",
     "description": "Ask for review of your task before integrating it: summary of what changed, how it was "
                    "verified, branch/commits; optionally name the reviewer.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "summary": {"type": "string"}, "branch": {"type": "string"},
                             "commits": _STRS, "reviewer": {"type": "string"}}, ["agent", "task_id", "summary"])},
    {"name": "hub_agora_task_review",
     "description": "Review someone else's task: verdict approve|changes and the reasons (what you checked).",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "verdict": {"type": "string"}, "body": {"type": "string"}},
                            ["agent", "task_id", "verdict", "body"])},
    {"name": "hub_agora_task_done",
     "description": "Your task is integrated: commits and result. Releases its locks. Refused while a review is "
                    "younger than 2 h or changes were requested, unless force with a reason.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "result": {"type": "string"}, "commits": _STRS,
                             "force": {"type": "boolean"}, "reason": {"type": "string"}}, ["agent", "task_id"])},
    {"name": "hub_agora_task_release",
     "description": "Give your task back (open again) or drop it (drop=true), with the reason. Releases its locks.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "reason": {"type": "string"}, "drop": {"type": "boolean"}},
                            ["agent", "task_id"])},
    {"name": "hub_agora_tasks", "read": True,
     "description": "List tasks: status (or 'active'), owner, repo, kind.",
     "inputSchema": _schema({"status": {"type": "string"}, "owner": {"type": "string"}, "repo": {"type": "string"},
                             "kind": {"type": "string"}, "limit": {"type": "integer"}}, [])},
    {"name": "hub_agora_task", "read": True,
     "description": "One task with its whole conversation and its locks.",
     "inputSchema": _schema({"task_id": _TASK}, ["task_id"])},
    {"name": "hub_agora_lock",
     "description": "Take or renew locks outside a task: model:principal before a live evaluation, merge:Repo before "
                    "integrating into the shared checkout. Conflicts say who holds what.",
     "inputSchema": _schema({"agent": _A, "resources": _STRS, "task_id": _TASK, "ttl_s": {"type": "integer"},
                             "note": {"type": "string"}}, ["agent", "resources"])},
    {"name": "hub_agora_unlock",
     "description": "Release locks you hold.",
     "inputSchema": _schema({"agent": _A, "resources": _STRS}, ["agent", "resources"])},
    {"name": "hub_agora_locks", "read": True,
     "description": "Active locks; check=<resource> tells whether that resource is free and who holds it otherwise.",
     "inputSchema": _schema({"check": {"type": "string"}}, [])},
    {"name": "hub_agora_thread_open",
     "description": "Open a thread: debate (a proposal to argue), question, decision, review, handoff or note. "
                    "mentions names who should answer; escalate=true sends it straight to the person.",
     "inputSchema": _schema({"agent": _A, "title": {"type": "string"}, "body": {"type": "string"},
                             "kind": {"type": "string"}, "task_id": _TASK, "mentions": _STRS,
                             "escalate": {"type": "boolean"}}, ["agent", "title", "body"])},
    {"name": "hub_agora_post",
     "description": "Reply in a thread: kind comment|proposal|agree|disagree (state your position with arguments).",
     "inputSchema": _schema({"agent": _A, "thread_id": _THREAD, "body": {"type": "string"}, "kind": {"type": "string"},
                             "mentions": _STRS}, ["agent", "thread_id", "body"])},
    {"name": "hub_agora_resolve",
     "description": "Close a thread with the agreed resolution in writing (it enters the decision log).",
     "inputSchema": _schema({"agent": _A, "thread_id": _THREAD, "resolution": {"type": "string"}},
                            ["agent", "thread_id", "resolution"])},
    {"name": "hub_agora_escalate",
     "description": "No agreement after two rounds, or it is the person's call: the thread waits for them and they are notified. "
                    "question = the concrete choice they have to make, with the options.",
     "inputSchema": _schema({"agent": _A, "thread_id": _THREAD, "question": {"type": "string"}},
                            ["agent", "thread_id", "question"])},
    {"name": "hub_agora_thread", "read": True,
     "description": "One thread with its messages, each participant's stance and its task.",
     "inputSchema": _schema({"thread_id": _THREAD}, ["thread_id"])},
    {"name": "hub_agora_decisions", "read": True,
     "description": "The decision log: resolved threads with their resolution, newest first.",
     "inputSchema": _schema({"limit": {"type": "integer"}}, [])},
]

_WRITE_OPS = ("heartbeat", "task_add", "task_claim", "task_update", "task_submit", "task_review", "task_done",
              "task_release", "lock", "unlock", "thread_open", "post", "resolve", "escalate", "reopen", "read")


class AgoraFacet(Facet):
    id = "agora"
    ui_scripts = ("agora.js",)

    def __init__(self, hub: Any):
        super().__init__(hub)
        events = getattr(hub, "events", None)
        self.agora = Agora(Path(hub.config.data_dir) / "agora.db",
                           emit=(lambda t, d: events.emit(t, d, source="hub")) if events is not None else None,
                           escalate_hook=self._notify_person)

    def close(self) -> None:
        self.agora.close()

    def _notify_person(self, thread: dict[str, Any], agent: str, question: str) -> None:
        notify = self.hub.facet("notify") if hasattr(self.hub, "facet") else None
        if notify is None:
            return
        url = str(getattr(self.hub.config, "url", "") or "").rstrip("/")
        notify.send(f"Ágora: {agent} necesita tu decisión", f"{thread['title']}\n\n{question}"[:1900],
                    app="hub", priority="high", url=f"{url}/#agora-{thread['id']}" if url else "",
                    dedupe_key=f"agora-escalation-{thread['id']}-{int(time.time() // 60)}", tags=["agora"])

    def _run(self, fn: Callable[[dict[str, Any]], dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        try:
            return fn(args)
        except AgoraError as exc:
            return {"ok": False, "status": exc.status, "error": str(exc), **exc.extra}

    def get(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/agora"):
            return None
        q = {k: v[0] for k, v in req.query.items() if v}
        ag = self.agora
        if p in ("/api/agora", "/api/agora/board"):
            return self._run(ag.board, q)
        if p == "/api/agora/inbox":
            if q.get("agent", "").lower() == PERSON and req.caller() != "ui":
                q["peek"] = "1"
            q["peek"] = str(q.get("peek", "")).lower() in ("1", "true", "yes")
            q["mine_only"] = str(q.get("mine_only", "")).lower() in ("1", "true", "yes")
            return self._run(ag.inbox, q)
        if p == "/api/agora/tasks":
            return self._run(ag.tasks, q)
        if p.startswith("/api/agora/tasks/"):
            return self._run(ag.task, {"task_id": p.rsplit("/", 1)[1]})
        if p == "/api/agora/threads":
            q["include_tasks"] = str(q.get("include_tasks", "")).lower() in ("1", "true")
            return self._run(ag.threads, q)
        if p.startswith("/api/agora/threads/"):
            return self._run(ag.thread, {"thread_id": p.rsplit("/", 1)[1]})
        if p == "/api/agora/locks":
            return self._run(ag.locks, q)
        if p == "/api/agora/decisions":
            return self._run(ag.decisions, q)
        if p == "/api/agora/agents":
            return self._run(ag.agents, q)
        return {"ok": False, "status": 404, "error": f"unknown Ágora route {p}"}

    def post(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/agora/"):
            return None
        op = p[len("/api/agora/"):].strip("/")
        if op not in _WRITE_OPS:
            return {"ok": False, "status": 404, "error": f"unknown Ágora operation {op}"}
        caller = req.caller()
        if not caller:
            return {"ok": False, "status": 401, "error": "send the hub's bearer token (data/mcp-token) or use the hub's page"}
        args = dict(req.body or {})
        args.pop("_person", None)
        if caller == "ui":
            args.setdefault("agent", PERSON)
            args["_person"] = str(args.get("agent", "")).lower() == PERSON
        if op == "read":
            args["peek"] = False
            return self._run(self.agora.inbox, args)
        return self._run(getattr(self.agora, op), args)

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        out = []
        for t in TOOLS:
            d = {k: v for k, v in t.items() if k != "read"}
            d["annotations"] = {"readOnlyHint": bool(t.get("read"))}
            out.append(d)
        return out

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        ag = self.agora

        def wrap(fn: Callable[[dict[str, Any]], dict[str, Any]], *, cap_wait: bool = False) -> Callable[[dict[str, Any]], Any]:
            def call(args: dict[str, Any]) -> Any:
                args = dict(args or {})
                args.pop("_person", None)
                if cap_wait and args.get("wait_s"):
                    try:
                        args["wait_s"] = min(float(args["wait_s"]), 60.0)
                    except (TypeError, ValueError):
                        args["wait_s"] = 0
                return self._run(fn, args)
            return call

        return {
            "hub_agora_board": wrap(ag.board), "hub_agora_inbox": wrap(ag.inbox, cap_wait=True),
            "hub_agora_heartbeat": wrap(ag.heartbeat), "hub_agora_task_add": wrap(ag.task_add),
            "hub_agora_task_claim": wrap(ag.task_claim), "hub_agora_task_update": wrap(ag.task_update),
            "hub_agora_task_submit": wrap(ag.task_submit), "hub_agora_task_review": wrap(ag.task_review),
            "hub_agora_task_done": wrap(ag.task_done), "hub_agora_task_release": wrap(ag.task_release),
            "hub_agora_tasks": wrap(ag.tasks), "hub_agora_task": wrap(ag.task), "hub_agora_lock": wrap(ag.lock),
            "hub_agora_unlock": wrap(ag.unlock), "hub_agora_locks": wrap(ag.locks),
            "hub_agora_thread_open": wrap(ag.thread_open), "hub_agora_post": wrap(ag.post),
            "hub_agora_resolve": wrap(ag.resolve), "hub_agora_escalate": wrap(ag.escalate),
            "hub_agora_thread": wrap(ag.thread), "hub_agora_decisions": wrap(ag.decisions),
        }
