"""Ágora agent pages: who an agent is, what it did, what it was told, and the notes on how it should work.

* **profile** (``agent_profile``): one read with everything an agent's page draws. The header (name, provider, model
  and effort, rank, presence and since when), its recent activity (messages, tasks, deliveries and commits), its
  instructions, what it has learned (decisions it closed, corrections it received, notes it kept), the routines it
  declared, its use per day and the instruction changes waiting for the person;
* **tell** (``agent_tell``): a direct message to one agent. It lands in a private-looking channel of the server (one
  per pair, category ``dm``) and names the agent, so it shows in the agent's inbox and in the mentions every
  heartbeat reports, also while the agent is busy; a leader of the console is also given a turn. ``dm_list`` lists
  the conversations of one reader;
* **instructions**: the text an agent works by. Only the person edits it (``agent_instructions_set``). When the
  person corrects an agent or sends a delivery back, the agent proposes a change to its own instructions
  (``agent_instruction_propose``) and the person accepts, edits or discards it (``agent_instruction_decide``);
* **memory** and **routines**: notes an agent keeps about what it learned and the recurring work it has.

``agora_channels`` folds these operations and tools into its own, so ``agora.py`` binds them like the rest.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any, Optional

MAX_INSTRUCTIONS = 20000
MAX_MEMORY_TEXT = 2000
MEMORY_KINDS = ("learned", "decision", "correction", "note")
PROPOSAL_STATES = ("pending", "accepted", "discarded")
USAGE_DAYS = 14
DM = "dm"

#: Appended to ``agora.MIGRATIONS`` after the review one.
PROFILES_MIGRATION = """
    CREATE TABLE agora_agent_instructions (agent TEXT PRIMARY KEY, body TEXT NOT NULL DEFAULT '',
        updated_by TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE agora_instruction_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,
        space_id TEXT NOT NULL, body TEXT NOT NULL, base_body TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL,
        trigger TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
        created REAL NOT NULL, decided_by TEXT, decided_at REAL, decision_note TEXT NOT NULL DEFAULT '');
    CREATE INDEX agora_instruction_proposals_agent ON agora_instruction_proposals(agent, status);
    CREATE TABLE agora_agent_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, space_id TEXT NOT NULL,
        kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL,
        created REAL NOT NULL);
    CREATE INDEX agora_agent_memory_agent ON agora_agent_memory(agent, id);
    CREATE TABLE agora_agent_routines (agent TEXT NOT NULL, name TEXT NOT NULL, schedule TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, updated_by TEXT NOT NULL,
        updated REAL NOT NULL, PRIMARY KEY(agent, name));
"""


def _ag() -> Any:
    from . import agora
    return agora


def _ch() -> Any:
    from . import agora_channels
    return agora_channels


def _err(message: str, status: int = 400, **extra: Any) -> Exception:
    return _ag().AgoraError(message, status, **extra)


def _ident(value: Any, what: str) -> str:
    ag = _ag()
    ident = str(value or "").strip().lower()
    if not ag._AGENT_RE.match(ident) or ident in ("hub", "ui", "system"):
        raise _err(f"{what} must be an agent id such as codex-docs (letters, digits, - _ .)")
    return ident


def _known(self: Any, ident: str) -> bool:
    return ident == _ag().PERSON or self.db.one("SELECT 1 AS x FROM agents WHERE id=?", (ident,)) is not None


def _list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _person_only(a: dict[str, Any], what: str) -> None:
    if not a.get("_person"):
        raise _err(f"{what} is for the person: it is done from their pages", 403, error_code="person_only")


def _caller(self: Any, a: dict[str, Any]) -> str:
    return self._agent(a.get("agent"), allow_person=bool(a.get("_person")))


# ---- direct messages --------------------------------------------------------------------------------------------

def dm_slug(first: str, second: str) -> str:
    pair = sorted((first, second))
    return DM + "-" + hashlib.sha1("|".join(pair).encode()).hexdigest()[:10]


def _dm_title(first: str, second: str) -> str:
    person = _ag().PERSON
    pair = sorted((first, second), key=lambda x: (x != person, x))
    return f"{pair[0]} · {pair[1]}"


def _dm_thread(self: Any, first: str, second: str, actor: str) -> tuple[int, str]:
    """The thread of the conversation between two agents, opening it the first time. Inside a tx."""
    ch = _ch()
    slug = dm_slug(first, second)
    row = ch._row(self, slug)
    if row and row.get("thread_id"):
        if row.get("archived"):
            self.db.execute("UPDATE agora_channels SET archived=0, updated=? WHERE id=?", (self.clock(), row["id"]))
        return int(row["thread_id"]), slug
    title = _dm_title(first, second)
    thread = ch._new_thread(self, slug, title, actor)
    now = self.clock()
    if row is None:
        position = int(self.db.scalar("SELECT COALESCE(MAX(position), 0) + 1 FROM agora_channels WHERE space_id=?",
                                      (self.space_id,), 1) or 1)
        self.db.insert("agora_channels", {"space_id": self.space_id, "slug": slug, "title": title,
                                          "topic": "Mensajes directos", "category": DM, "position": position,
                                          "thread_id": thread, "created_by": actor, "created": now, "updated": now})
    else:
        self.db.execute("UPDATE agora_channels SET thread_id=?, archived=0, updated=? WHERE id=?",
                        (thread, now, row["id"]))
    return thread, slug


def dm_note(self: Any, first: str, second: str, author: str, body: str, kind: str = "comment",
            mentions: Optional[list[str]] = None) -> int:
    """A message in the conversation between two agents, naming ``mentions``. Inside a tx."""
    thread, _slug = _dm_thread(self, first, second, author)
    return self._msg(thread, author, kind, body, mentions or [])


def agent_tell(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Say something to one agent: it goes to the conversation of the two (one per pair and server) and names the
    agent, so it reaches its inbox and the mentions its next heartbeat reports, also in the middle of its work. With
    ``correction`` the person marks it as a correction: the agent is also asked to propose a change to its own
    instructions (``agent_instruction_propose``) and the correction is kept in its memory."""
    ag = _ag()
    person = bool(a.get("_person"))
    sender = _caller(self, a)
    target = _ident(a.get("to") or a.get("target"), "to")
    if target == sender:
        raise _err("an agent cannot tell itself something here: keep a note in its memory instead")
    if not _known(self, target):
        raise _err(f"unknown agent {target!r} (it has not been seen in any server)", 404)
    text = ag._txt(a.get("text") or a.get("body"), 20000, "text", required=True)
    correction = bool(a.get("correction")) and person
    with self.db.tx():
        self._touch(sender)
        mid = dm_note(self, sender, target, sender, text, "comment", [target])
        thread, slug = _dm_thread(self, sender, target, sender)
        if correction:
            self.db.insert("agora_agent_memory", {"agent": target, "space_id": self.space_id, "kind": "correction",
                                                  "text": text[:MAX_MEMORY_TEXT], "source": f"message {mid}",
                                                  "created_by": sender, "created": self.clock()})
            self._msg(thread, "system", "comment",
                      f"{target}: si esta corrección cambia cómo debes trabajar, propón un cambio de tus "
                      f"instrucciones (hub_agora_agent_instruction_propose) y Luis lo acepta o lo descarta.",
                      [target])
    self._bump("agora.agent.tell", {"to": target, "by": sender, "message_id": mid})
    delivery: dict[str, Any] = {"status": "queued",
                                "detail": "The message names the agent: it is in its inbox and in the mentions its "
                                          "next heartbeat reports. A mention does not wake an idle agent."}
    hook = getattr(self, "tell_hook", None)
    if hook:
        try:
            woke = hook(target, mid, text, self.space_id)
            if isinstance(woke, dict):
                delivery["wake"] = woke
        except Exception as exc:  # noqa: BLE001 - a wake-up that fails never loses the message
            delivery["wake"] = {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    return {"ok": True, "message_id": mid, "channel": slug, "thread_id": thread, "to": target, "from": sender,
            "correction": correction, "delivery": delivery}


def dm_list(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The direct conversations of the reader (``agent``; the person's pages read as the person): the other one,
    the last thing said and what the reader has not seen."""
    a = a or {}
    ch = _ch()
    viewer = ch._viewer(self, a) or _ag().PERSON
    unread = ch._unread(self, viewer)
    out = []
    for row in ch._rows(self):
        if row.get("category") != DM:
            continue
        pair = [p.strip() for p in str(row["title"]).split("·")]
        if viewer not in pair:
            continue
        other = next((p for p in pair if p != viewer), viewer)
        view = ch._channel_view(self, row, unread)
        view.update(with_agent=other, participants=pair)
        out.append(view)
    out.sort(key=lambda v: float(v.get("last_at") or 0), reverse=True)
    return {"ok": True, "viewer": viewer, "dms": out}


# ---- instructions -----------------------------------------------------------------------------------------------

def instructions_of(self: Any, agent: str) -> dict[str, Any]:
    row = self.db.one("SELECT body, updated_by, updated FROM agora_agent_instructions WHERE agent=?", (agent,))
    return {"body": row["body"], "updated_by": row["updated_by"], "updated": row["updated"]} if row else \
        {"body": "", "updated_by": None, "updated": None}


def agent_instructions_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The person writes the instructions of an agent (all of them, replacing what was there)."""
    _person_only(a, "writing the instructions of an agent")
    ag = _ag()
    target = _ident(a.get("id"), "id")
    body = ag._txt(a.get("body"), MAX_INSTRUCTIONS, "body")
    now = self.clock()
    with self.db.tx():
        self.db.execute("INSERT INTO agora_agent_instructions(agent, body, updated_by, updated) VALUES(?,?,?,?) "
                        "ON CONFLICT(agent) DO UPDATE SET body=excluded.body, updated_by=excluded.updated_by, "
                        "updated=excluded.updated", (target, body, ag.PERSON, now))
        if _known(self, target) and target != ag.PERSON:
            dm_note(self, ag.PERSON, target, "system", f"Luis ha cambiado tus instrucciones ({len(body)} caracteres).",
                    "system")
    self._bump("agora.agent.instructions", {"agent": target})
    return {"ok": True, "id": target, "instructions": instructions_of(self, target)}


def _proposal_view(row: Any, current: str) -> dict[str, Any]:
    d = dict(row)
    d["stale"] = d["status"] == "pending" and d["base_body"] != current
    lines = difflib.unified_diff(current.splitlines(), d["body"].splitlines(), "actuales", "propuestas", lineterm="", n=2)
    d["diff"] = "\n".join(list(lines)[:400])
    return d


def agent_instruction_proposals(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Changes to instructions that agents proposed, newest first. ``id`` filters by agent, ``status`` by
    pending, accepted or discarded (default: all); across every server, because instructions are the agent's own."""
    a = a or {}
    where, params = [], []
    if a.get("id"):
        where.append("agent=?")
        params.append(_ident(a.get("id"), "id"))
    status = str(a.get("status") or "").strip().lower()
    if status:
        if status not in PROPOSAL_STATES:
            raise _err(f"status must be one of {', '.join(PROPOSAL_STATES)}")
        where.append("status=?")
        params.append(status)
    limit = _ch()._int(a.get("limit"), 50, 1, 200)
    rows = self.db.query("SELECT * FROM agora_instruction_proposals" + (" WHERE " + " AND ".join(where) if where else "")
                         + " ORDER BY id DESC LIMIT ?", (*params, limit))
    current: dict[str, str] = {}
    out = []
    for r in rows:
        if r["agent"] not in current:
            current[r["agent"]] = instructions_of(self, r["agent"])["body"]
        out.append(_proposal_view(r, current[r["agent"]]))
    pending = int(self.db.scalar("SELECT COUNT(*) FROM agora_instruction_proposals WHERE status='pending'"
                                 + (" AND agent=?" if a.get("id") else ""),
                                 (_ident(a.get("id"), "id"),) if a.get("id") else (), 0) or 0)
    return {"ok": True, "proposals": out, "pending": pending}


def agent_instruction_propose(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """An agent proposes a change to its own instructions: ``body`` is the whole new text, or ``add`` a few lines
    appended to the current ones. ``reason`` says what happened (a correction, a delivery sent back) and ``source``
    where (task 123, message 456). The person accepts, edits or discards it from the agent's page."""
    ag = _ag()
    agent = _caller(self, a)
    if a.get("_person"):
        raise _err("the person writes instructions directly (agent_instructions_set); proposals are the agents' own", 400)
    target = _ident(a.get("id") or agent, "id")
    if target != agent:
        raise _err("an agent proposes changes to its own instructions only", 403, error_code="own_instructions")
    current = instructions_of(self, agent)["body"]
    if a.get("body") not in (None, ""):
        body = ag._txt(a.get("body"), MAX_INSTRUCTIONS, "body", required=True)
    elif a.get("add") not in (None, ""):
        added = ag._txt(a.get("add"), MAX_INSTRUCTIONS, "add", required=True)
        body = (current.rstrip() + "\n" + added) if current.strip() else added
    else:
        raise _err("body (the whole new text) or add (lines to append) is required")
    if body.strip() == current.strip():
        raise _err("the proposal does not change the instructions", 409)
    reason = ag._txt(a.get("reason"), 2000, "reason", required=True)
    source = ag._txt(a.get("source"), 200, "source")
    trigger = str(a.get("trigger") or "").strip().lower()
    if trigger and trigger not in ("correction", "changes", "decision", "other"):
        raise _err("trigger must be correction, changes, decision or other")
    with self.db.tx():
        self._touch(agent)
        dup = self.db.one("SELECT id FROM agora_instruction_proposals WHERE agent=? AND status='pending' AND body=?",
                          (agent, body))
        if dup:
            raise _err(f"the same change is already waiting as proposal {dup['id']}", 409)
        pid = self.db.insert("agora_instruction_proposals", {
            "agent": agent, "space_id": self.space_id, "body": body, "base_body": current, "reason": reason,
            "trigger": trigger or "other", "source": source, "status": "pending", "created": self.clock()})
        dm_note(self, agent, ag.PERSON, agent,
                f"Propongo un cambio de mis instrucciones (propuesta {pid}): {reason}", "comment", [ag.PERSON])
    self._bump("agora.agent.proposal", {"agent": agent, "proposal_id": pid})
    return {"ok": True, "proposal": _proposal_view(self.db.one("SELECT * FROM agora_instruction_proposals WHERE id=?",
                                                               (pid,)), current)}


def agent_instruction_decide(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The person accepts a proposal (``decision`` accept; ``body`` may carry their own edit of the text) or
    discards it. The agent is told in their conversation and an accepted change is kept in its memory."""
    _person_only(a, "deciding on a change of instructions")
    ag = _ag()
    decision = str(a.get("decision") or "").strip().lower()
    if decision not in ("accept", "discard"):
        raise _err("decision must be accept or discard")
    try:
        pid = int(a.get("proposal_id"))
    except (TypeError, ValueError):
        raise _err("proposal_id is required") from None
    note = ag._txt(a.get("note"), 2000, "note")
    with self.db.tx():
        row = self.db.one("SELECT * FROM agora_instruction_proposals WHERE id=?", (pid,))
        if row is None:
            raise _err(f"no proposal {pid}", 404)
        if row["status"] != "pending":
            raise _err(f"proposal {pid} was already {row['status']}", 409)
        now = self.clock()
        if decision == "accept":
            body = ag._txt(a.get("body"), MAX_INSTRUCTIONS, "body") if a.get("body") not in (None, "") else row["body"]
            self.db.execute("INSERT INTO agora_agent_instructions(agent, body, updated_by, updated) VALUES(?,?,?,?) "
                            "ON CONFLICT(agent) DO UPDATE SET body=excluded.body, updated_by=excluded.updated_by, "
                            "updated=excluded.updated", (row["agent"], body, ag.PERSON, now))
            self.db.insert("agora_agent_memory", {"agent": row["agent"], "space_id": row["space_id"],
                                                  "kind": "correction", "text": row["reason"][:MAX_MEMORY_TEXT],
                                                  "source": f"instructions, proposal {pid}",
                                                  "created_by": ag.PERSON, "created": now})
        self.db.execute("UPDATE agora_instruction_proposals SET status=?, decided_by=?, decided_at=?, decision_note=? "
                        "WHERE id=?", ("accepted" if decision == "accept" else "discarded", ag.PERSON, now, note, pid))
        verb = "ha aceptado" if decision == "accept" else "ha descartado"
        dm_note(self, ag.PERSON, row["agent"], ag.PERSON,
                f"Luis {verb} tu propuesta {pid} de cambio de instrucciones." + (f" {note}" if note else ""),
                "comment", [row["agent"]])
    self._bump("agora.agent.proposal", {"agent": row["agent"], "proposal_id": pid, "decision": decision})
    fresh = self.db.one("SELECT * FROM agora_instruction_proposals WHERE id=?", (pid,))
    return {"ok": True, "proposal": _proposal_view(fresh, instructions_of(self, row["agent"])["body"]),
            "instructions": instructions_of(self, row["agent"])}


def note_correction(self: Any, task: dict[str, Any], reviewer: str, body: str) -> None:
    """The person sent a delivery back (``task_review`` with changes): the owner learns it in its conversation with
    the person, with the way to propose a change of instructions, and it is kept in its memory. Inside a tx."""
    ag = _ag()
    owner = task.get("owner")
    if reviewer != ag.PERSON or not owner or owner == ag.PERSON:
        return
    summary = " ".join(str(body or "").split())[:600]
    self.db.insert("agora_agent_memory", {"agent": owner, "space_id": self.space_id, "kind": "correction",
                                          "text": f"Entrega de la tarea {task['id']} devuelta: {summary}"[:MAX_MEMORY_TEXT],
                                          "source": f"task {task['id']}", "created_by": ag.PERSON,
                                          "created": self.clock()})
    dm_note(self, ag.PERSON, owner, "system",
            f"Luis ha pedido cambios en la tarea {task['id']}. Si te corrige cómo debes trabajar, propón un cambio de "
            f"tus instrucciones (hub_agora_agent_instruction_propose, source «task {task['id']}»).", "comment", [owner])


# ---- memory and routines ----------------------------------------------------------------------------------------

def agent_memory_add(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """A note in an agent's memory: what it learned, a decision, a correction. An agent keeps its own; the person
    may write in anyone's (``id``)."""
    ag = _ag()
    who = _caller(self, a)
    target = _ident(a.get("id") or who, "id")
    if target != who and not a.get("_person"):
        raise _err("an agent keeps notes in its own memory only", 403, error_code="own_memory")
    kind = str(a.get("kind") or "learned").strip().lower()
    if kind not in MEMORY_KINDS:
        raise _err(f"kind must be one of {', '.join(MEMORY_KINDS)}")
    text = ag._txt(a.get("text"), MAX_MEMORY_TEXT, "text", required=True)
    with self.db.tx():
        self._touch(who)
        mid = self.db.insert("agora_agent_memory", {"agent": target, "space_id": self.space_id, "kind": kind,
                                                    "text": text, "source": ag._txt(a.get("source"), 200, "source"),
                                                    "created_by": who, "created": self.clock()})
    return {"ok": True, "memory_id": mid, "id": target, "kind": kind}


def agent_memory_remove(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The person removes a note from an agent's memory."""
    _person_only(a, "removing a note from an agent's memory")
    try:
        mid = int(a.get("memory_id"))
    except (TypeError, ValueError):
        raise _err("memory_id is required") from None
    with self.db.tx():
        if self.db.one("SELECT 1 AS x FROM agora_agent_memory WHERE id=?", (mid,)) is None:
            raise _err(f"no memory note {mid}", 404)
        self.db.execute("DELETE FROM agora_agent_memory WHERE id=?", (mid,))
    return {"ok": True, "memory_id": mid}


def agent_routine_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Declare (or update) a recurring job of an agent: ``name``, ``schedule`` in words (every 2 h, daily 08:00),
    ``note`` and ``enabled``. It is the record the page shows; what actually runs it is the agent's own scheduler."""
    ag = _ag()
    who = _caller(self, a)
    target = _ident(a.get("id") or who, "id")
    if target != who and not a.get("_person"):
        raise _err("an agent declares its own routines only", 403, error_code="own_routines")
    name = ag._txt(a.get("name"), 80, "name", required=True)
    now = self.clock()
    with self.db.tx():
        self._touch(who)
        prior = self.db.one("SELECT * FROM agora_agent_routines WHERE agent=? AND name=?", (target, name))
        schedule = ag._txt(a.get("schedule"), 120, "schedule") if "schedule" in a else (prior["schedule"] if prior else "")
        note = ag._txt(a.get("note"), 600, "note") if "note" in a else (prior["note"] if prior else "")
        enabled = 1 if a.get("enabled", True) not in (False, 0, "false", "0") else 0
        self.db.execute("INSERT INTO agora_agent_routines(agent, name, schedule, note, enabled, updated_by, updated) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(agent, name) DO UPDATE SET schedule=excluded.schedule, "
                        "note=excluded.note, enabled=excluded.enabled, updated_by=excluded.updated_by, "
                        "updated=excluded.updated", (target, name, schedule, note, enabled, who, now))
    return {"ok": True, "id": target, "routine": {"name": name, "schedule": schedule, "note": note,
                                                  "enabled": bool(enabled)}}


def agent_routine_remove(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    ag = _ag()
    who = _caller(self, a)
    target = _ident(a.get("id") or who, "id")
    if target != who and not a.get("_person"):
        raise _err("an agent removes its own routines only", 403, error_code="own_routines")
    name = ag._txt(a.get("name"), 80, "name", required=True)
    with self.db.tx():
        if self.db.one("SELECT 1 AS x FROM agora_agent_routines WHERE agent=? AND name=?", (target, name)) is None:
            raise _err(f"{target} has no routine {name!r}", 404)
        self.db.execute("DELETE FROM agora_agent_routines WHERE agent=? AND name=?", (target, name))
    return {"ok": True, "id": target, "name": name}


# ---- the page ---------------------------------------------------------------------------------------------------

_ENGINES = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "faustus": "Faustus"}


def _initials(ident: str) -> str:
    parts = [p for p in ident.replace("_", "-").replace(".", "-").split("-") if p]
    if len(parts) > 1 and not parts[1].isdigit():
        return (parts[0][:1] + parts[1][:1]).upper()
    return ident[:2].upper()


def _day(ts: float) -> str:
    import time
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _activity(self: Any, ident: str, limit: int) -> dict[str, Any]:
    messages = [dict(r) for r in self.db.query(
        "SELECT m.id, m.thread_id, m.kind, substr(m.body, 1, 280) AS body, m.created, t.title AS thread, t.task_id "
        "FROM messages m JOIN threads t ON t.id=m.thread_id WHERE t.space_id=? AND m.author=? AND m.kind<>'system' "
        "ORDER BY m.id DESC LIMIT ?", (self.space_id, ident, limit))]
    tasks = []
    commits: list[str] = []
    for r in self.db.query(
            "SELECT id, title, status, kind, owner, created_by, commits, submitted_at, closed_at, updated, review_state "
            "FROM tasks WHERE space_id=? AND (owner=? OR created_by=?) ORDER BY updated DESC LIMIT ?",
            (self.space_id, ident, ident, limit)):
        d = dict(r)
        d["commits"] = [str(c) for c in _list(d.get("commits"))]
        d["role"] = "owner" if d["owner"] == ident else "creator"
        tasks.append(d)
        if d["owner"] == ident:
            for c in d["commits"]:
                if c not in commits:
                    commits.append(c)
    deliveries = [{"task_id": t["id"], "title": t["title"], "status": t["status"], "at": t["submitted_at"],
                   "commits": t["commits"], "review_state": t["review_state"]}
                  for t in tasks if t["owner"] == ident and t.get("submitted_at")]
    timeline = ([{"at": m["created"], "type": "message", "text": m["body"], "where": m["thread"],
                  "message_id": m["id"], "task_id": m["task_id"]} for m in messages]
                + [{"at": d["at"], "type": "delivery", "text": d["title"], "task_id": d["task_id"],
                    "commits": d["commits"], "status": d["status"]} for d in deliveries]
                + [{"at": t["updated"], "type": "task", "text": t["title"], "task_id": t["id"], "status": t["status"],
                    "role": t["role"]} for t in tasks])
    timeline.sort(key=lambda e: float(e.get("at") or 0), reverse=True)
    return {"messages": messages, "tasks": tasks, "deliveries": deliveries, "commits": commits[:60],
            "timeline": timeline[:limit * 2]}


def _memory(self: Any, ident: str, limit: int) -> list[dict[str, Any]]:
    items = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "source": r["source"], "by": r["created_by"],
              "at": r["created"], "space_id": r["space_id"]} for r in self.db.query(
        "SELECT * FROM agora_agent_memory WHERE agent=? ORDER BY id DESC LIMIT ?", (ident, limit))]
    for r in self.db.query("SELECT t.id, t.title, t.resolution, t.resolved_by, t.updated FROM threads t "
                           "WHERE t.space_id=? AND t.resolved_by=? AND t.resolution IS NOT NULL AND t.resolution<>'' "
                           "ORDER BY t.updated DESC LIMIT ?", (self.space_id, ident, limit)):
        items.append({"id": None, "kind": "decision", "text": f"{r['title']}: {r['resolution']}"[:MAX_MEMORY_TEXT],
                      "source": f"thread {r['id']}", "by": ident, "at": r["updated"], "space_id": self.space_id})
    items.sort(key=lambda e: float(e.get("at") or 0), reverse=True)
    return items[:limit]


def _usage(self: Any, ident: str, extra: dict[str, Any]) -> dict[str, Any]:
    import time
    now = self.clock()
    since = now - USAGE_DAYS * 86400
    days: dict[str, dict[str, Any]] = {}
    for i in range(USAGE_DAYS - 1, -1, -1):
        days[_day(now - i * 86400)] = {"day": _day(now - i * 86400), "messages": 0, "tasks_closed": 0, "run_minutes": 0}
    for r in self.db.query("SELECT m.created FROM messages m JOIN threads t ON t.id=m.thread_id "
                           "WHERE t.space_id=? AND m.author=? AND m.kind<>'system' AND m.created>?",
                           (self.space_id, ident, since)):
        d = days.get(_day(float(r["created"])))
        if d:
            d["messages"] += 1
    for r in self.db.query("SELECT closed_at FROM tasks WHERE space_id=? AND owner=? AND closed_at>?",
                           (self.space_id, ident, since)):
        d = days.get(_day(float(r["closed_at"])))
        if d:
            d["tasks_closed"] += 1
    for run in extra.get("runs") or []:
        start, end = float(run.get("started") or 0), float(run.get("ended") or now)
        if end < since or end <= start:
            continue
        cursor = max(start, since)
        while cursor < end:
            midnight = time.mktime(time.strptime(_day(cursor), "%Y-%m-%d")) + 86400
            slice_end = min(end, midnight)
            d = days.get(_day(cursor))
            if d:
                d["run_minutes"] += round((slice_end - cursor) / 60.0, 1)
            cursor = slice_end
    series = list(days.values())
    has_runs = any(d["run_minutes"] for d in series)
    return {"days": series, "tokens": None,
            "note": "Mensajes y tareas cerradas por día; el tiempo de ejecución sale de las sesiones que arrancó la "
                    "consola. Los tokens no se registran." if has_runs or extra.get("runs") is not None else
                    "Mensajes y tareas cerradas por día. No hay datos de tiempo de ejecución ni de tokens."}


def agent_profile(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Everything one agent's page draws, in one read (see the module's note). ``id`` is the agent; ``limit`` caps
    each list (30)."""
    ag = _ag()
    ch = _ch()
    ident = _ident(a.get("id") or a.get("target"), "id")
    limit = ch._int(a.get("limit"), 30, 1, 100)
    if not _known(self, ident):
        raise _err(f"unknown agent {ident!r} (it has not been seen in any server)", 404)
    member = next((m for m in ch._members(self, ch.agora_social._in_hand(self)) if m["id"] == ident), None)
    row = self.db.one("SELECT * FROM agents WHERE id=?", (ident,))
    person = ident == ag.PERSON
    extra: dict[str, Any] = {}
    hook = getattr(self, "profile_hook", None)
    if hook and not person:
        try:
            extra = hook(ident, self.space_id) or {}
        except Exception:  # noqa: BLE001 - the console's side is optional
            extra = {}
    base = member or {"id": ident, "name": (row["name"] if row else ident) or ident, "kind": "human" if person else "agent",
                      "role": "person" if person else "other", "rank": "person" if person else "agent",
                      "presence": "person" if person else "offline", "state": "idle",
                      "doing": "", "note": "", "last_seen": row["last_seen"] if row else None,
                      "declared_model": None, "declared_effort": None, "tasks": [], "locks": 0, "quota": {}}
    provider = base.get("role") if base.get("role") not in ("person", "other") else None
    if not provider:
        provider = next((k for k in _ENGINES if ident == k or ident.startswith(k + "-")), None)
    model = base.get("declared_model") or extra.get("model")
    effort = base.get("declared_effort") or extra.get("effort")
    header = {"id": ident, "name": base.get("name") or ident, "initials": _initials(ident), "kind": base.get("kind"),
              "provider": provider, "engine": _ENGINES.get(provider or "", provider or ("Persona" if person else "")),
              "model": model, "effort": effort, "model_source": "declared" if base.get("declared_model") else
              ("console" if extra.get("model") else None),
              "rank": base.get("rank"), "presence": base.get("presence"), "state": base.get("state"),
              "doing": base.get("doing") or "", "note": base.get("note") or "", "last_seen": base.get("last_seen"),
              "first_seen": row["created"] if row else None,
              "since": extra.get("since") or base.get("last_seen"), "since_source": "session" if extra.get("since") else "last_seen",
              "color": extra.get("color"), "locks": base.get("locks", 0), "tasks_in_hand": base.get("tasks") or [],
              "quota": base.get("quota") or {}, "in_server": member is not None}
    routines = [{"name": r["name"], "schedule": r["schedule"], "note": r["note"], "enabled": bool(r["enabled"]),
                 "updated_by": r["updated_by"], "updated": r["updated"]} for r in self.db.query(
        "SELECT * FROM agora_agent_routines WHERE agent=? ORDER BY name", (ident,))]
    for r in extra.get("routines") or []:
        routines.append(r)
    instructions = instructions_of(self, ident)
    proposals = agent_instruction_proposals(self, {"id": ident, "limit": 20})
    pairs = None
    if not person:
        dm = ch._row(self, dm_slug(ident, ag.PERSON))
        pairs = {"channel": dm["slug"], "thread_id": dm.get("thread_id")} if dm else {"channel": dm_slug(ident, ag.PERSON),
                                                                                      "thread_id": None}
    return {"ok": True, "profile": header, "activity": _activity(self, ident, limit), "instructions": instructions,
            "memory": _memory(self, ident, limit), "routines": routines, "usage": _usage(self, ident, extra),
            "proposals": proposals["proposals"], "proposals_pending": proposals["pending"], "dm": pairs}


OPERATIONS = {"agent_profile": agent_profile, "agent_tell": agent_tell, "dm_list": dm_list,
              "agent_instructions_set": agent_instructions_set, "agent_instruction_proposals": agent_instruction_proposals,
              "agent_instruction_propose": agent_instruction_propose, "agent_instruction_decide": agent_instruction_decide,
              "agent_memory_add": agent_memory_add, "agent_memory_remove": agent_memory_remove,
              "agent_routine_set": agent_routine_set, "agent_routine_remove": agent_routine_remove}
WRITE_OPS = ("agent_tell", "agent_instructions_set", "agent_instruction_propose", "agent_instruction_decide",
             "agent_memory_add", "agent_memory_remove", "agent_routine_set", "agent_routine_remove")
READ_ROUTES = ("agent_profile", "dm_list", "agent_instruction_proposals")


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    who = {"type": "string", "description": "The agent's id, such as codex-docs."}
    return [
        {"name": "hub_agora_agent_profile", "read": True,
         "description": "One agent's page in one read: header (provider, model, effort, rank, presence, since when), "
                        "activity (messages, tasks, deliveries, commits), instructions, memory (decisions, corrections, "
                        "notes), routines, use per day and the instruction changes waiting for the person.",
         "inputSchema": schema({"id": who, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["id"])},
        {"name": "hub_agora_agent_tell",
         "description": "Say something to one agent directly. It goes to the conversation of the two on this server and "
                        "names the agent, so it is in its inbox and in the mentions its next heartbeat reports, also "
                        "while it works; a console leader also gets a turn. correction (the person only) also asks the "
                        "agent to propose a change to its instructions and keeps the correction in its memory.",
         "inputSchema": schema({"agent": agent_prop, "to": who, "text": {"type": "string", "maxLength": 20000},
                                "correction": {"type": "boolean"}}, ["agent", "to", "text"])},
        {"name": "hub_agora_dm_list", "read": True,
         "description": "The direct conversations of an agent on this server: the other one, the last message and how "
                        "many the reader has not seen.",
         "inputSchema": schema({"agent": agent_prop}, ["agent"])},
        {"name": "hub_agora_agent_instruction_proposals", "read": True,
         "description": "Changes to agents' own instructions that were proposed, newest first, with their diff against "
                        "the current text. id filters by agent, status by pending, accepted or discarded.",
         "inputSchema": schema({"id": who, "status": {"type": "string", "enum": list(PROPOSAL_STATES)},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, [])},
        {"name": "hub_agora_agent_instruction_propose",
         "description": "Propose a change to your own instructions after a correction or a delivery sent back: body is "
                        "the whole new text, or add the lines to append. reason says what happened; source where "
                        "(task 123). The person accepts, edits or discards it from your page. You never write your own "
                        "instructions directly.",
         "inputSchema": schema({"agent": agent_prop, "body": {"type": "string", "maxLength": MAX_INSTRUCTIONS},
                                "add": {"type": "string", "maxLength": MAX_INSTRUCTIONS},
                                "reason": {"type": "string", "maxLength": 2000}, "source": {"type": "string", "maxLength": 200},
                                "trigger": {"type": "string", "enum": ["correction", "changes", "decision", "other"]}},
                               ["agent", "reason"])},
        {"name": "hub_agora_agent_memory_add",
         "description": "Keep a note in your own memory (what you learned, a decision, a correction): kind learned, "
                        "decision, correction or note. It shows on your page.",
         "inputSchema": schema({"agent": agent_prop, "text": {"type": "string", "maxLength": MAX_MEMORY_TEXT},
                                "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
                                "source": {"type": "string", "maxLength": 200}}, ["agent", "text"])},
        {"name": "hub_agora_agent_routine_set",
         "description": "Declare or update one of your recurring jobs on your page: name, schedule in words (every 2 h), "
                        "note, enabled. It records the routine; your own scheduler runs it.",
         "inputSchema": schema({"agent": agent_prop, "name": {"type": "string", "maxLength": 80},
                                "schedule": {"type": "string", "maxLength": 120}, "note": {"type": "string", "maxLength": 600},
                                "enabled": {"type": "boolean"}}, ["agent", "name"])},
        {"name": "hub_agora_agent_routine_remove",
         "description": "Remove one of your declared routines.",
         "inputSchema": schema({"agent": agent_prop, "name": {"type": "string", "maxLength": 80}}, ["agent", "name"])},
    ]
