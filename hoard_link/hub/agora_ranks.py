"""Ranks: who decides among the agents of a server.

A fixed ladder, the same on every server: ``director`` > ``lead`` > ``agent`` > ``support``. The person gives each
agent its rank, per server and from their own pages only; an agent that was given none is an ``agent``. The person
is on no rung of it: they resolve, review and assign anything, and what they decided is treated among the agents as
it always was (the ladder orders agents among themselves).

What a higher rank means, enforced where each thing happens (``agora.py``):

* **Its word closes a disagreement.** A thread cannot be resolved against someone who outranks the resolver and
  disagrees in it, and what a higher rank resolved is not reopened or resolved again from below.
* **Its review stands.** Its verdict on a delivery is not replaced from below, and the task is not closed over the
  changes it asked for.
* **It hands out the work.** It assigns tasks to those below (``task_assign``), takes them back and moves them to
  someone else at once.
* **Questions go to it first.** A question an agent escalates goes to whoever is next above it and around; it reaches
  the person when nobody is, when the one at the top sends it up, or when it is asked again after nobody answered.

Nothing here lets an agent change a rank: an agent may not decide how much its own word weighs.
"""
from __future__ import annotations

import json
from typing import Any, Optional

LADDER = (("director", 4), ("lead", 3), ("agent", 2), ("support", 1))
LEVEL = dict(LADDER)
DEFAULT = "agent"
PERSON_LEVEL = 9
#: How each rank is written in the lines the Ágora leaves in a channel (they are in the person's language).
LABEL = {"director": "Director", "lead": "Responsable", "agent": "Agente", "support": "Apoyo"}
RULES = (
    "A thread is not resolved against someone of higher rank who disagrees in it, nor reopened or resolved again "
    "once someone of higher rank resolved it.",
    "The verdict of someone of higher rank on a delivery is not replaced from below, and the task is not closed over "
    "the changes they asked for.",
    "A higher rank assigns tasks to those below it (task_assign), takes them back and hands them over at once.",
    "An escalated question goes to whoever is next above and around; it reaches the person when nobody is, when the "
    "highest rank around sends it up, or when it is escalated again after half an hour without an answer.",
)
#: How long a question put to those above waits for them: escalated again after this with no answer, it goes to the person.
ASK_UP_WAIT_S = 30 * 60

#: Appended to ``agora.MIGRATIONS`` after the muted-servers one.
RANKS_MIGRATION = """
    CREATE TABLE agora_ranks (space_id TEXT NOT NULL, agent TEXT NOT NULL, rank TEXT NOT NULL,
        set_by TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(space_id, agent));
"""


def _ag() -> Any:
    from . import agora
    return agora


def _err(message: str, status: int = 400, **extra: Any) -> Exception:
    return _ag().AgoraError(message, status, **extra)


def rank_of(self: Any, agent: Optional[str], sid: Optional[str] = None) -> str:
    """The rank of ``agent`` on this server: the one the person gave it, else ``agent``. The person is ``person``."""
    if agent == _ag().PERSON:
        return "person"
    row = self.db.one("SELECT rank FROM agora_ranks WHERE space_id=? AND agent=?", (sid or self.space_id, agent or ""))
    return row["rank"] if row and row["rank"] in LEVEL else DEFAULT


def level(self: Any, agent: Optional[str], sid: Optional[str] = None) -> int:
    rank = rank_of(self, agent, sid)
    return PERSON_LEVEL if rank == "person" else LEVEL[rank]


def outranks(self: Any, one: Optional[str], other: Optional[str], sid: Optional[str] = None) -> bool:
    """Whether ``one`` stands strictly above ``other`` on this server. It orders agents among themselves: the person
    is on no rung, so what they resolved or reviewed is treated as it always was, and what they may do is decided
    where they act."""
    if not one or not other or one == _ag().PERSON:
        return False
    return level(self, one, sid) > level(self, other, sid)


def ranks_in(self: Any, sid: Optional[str] = None) -> dict[str, str]:
    return {r["agent"]: r["rank"] for r in self.db.query(
        "SELECT agent, rank FROM agora_ranks WHERE space_id=?", (sid or self.space_id,)) if r["rank"] in LEVEL}


def set_rank(self: Any, agent: str, rank: str, by: str, sid: Optional[str] = None) -> bool:
    """Write a rank as decided by whoever may decide it (the person's pages, or the console starting a session for
    them). True when it changed. Inside the caller's transaction or its own."""
    if rank not in LEVEL:
        raise _err("rank must be one of " + ", ".join(name for name, _ in LADDER))
    space = sid or self.space_id
    if rank_of(self, agent, space) == rank:
        return False
    self.db.execute(
        "INSERT INTO agora_ranks(space_id, agent, rank, set_by, updated) VALUES(?,?,?,?,?) "
        "ON CONFLICT(space_id, agent) DO UPDATE SET rank=excluded.rank, set_by=excluded.set_by, updated=excluded.updated",
        (space, agent, rank, by, self.clock()))
    return True


def forget(self: Any, agent: str, sid: Optional[str] = None) -> None:
    """An agent that is not on a server keeps no rank on it. Inside a tx."""
    self.db.execute("DELETE FROM agora_ranks WHERE space_id=? AND agent=?", (sid or self.space_id, agent))


def unanswered_above(self: Any, thread_id: int, agent: str) -> bool:
    """Whether ``agent`` already put a question in this thread to those above it, at least ``ASK_UP_WAIT_S`` ago, and
    nobody else has said anything there since: asked again, it goes to the person instead of waiting for ever."""
    last = self.db.one("SELECT id, created, mentions FROM messages WHERE thread_id=? AND author=? AND kind='escalation' "
                       "ORDER BY id DESC LIMIT 1", (thread_id, agent))
    if last is None or self.clock() - float(last["created"] or 0) < ASK_UP_WAIT_S:
        return False
    try:
        asked = json.loads(last["mentions"] or "[]")
    except ValueError:
        asked = []
    if not isinstance(asked, list) or _ag().PERSON in asked:
        return False
    return self.db.one("SELECT 1 AS x FROM messages WHERE thread_id=? AND id>? AND author<>? AND kind<>'system' LIMIT 1",
                       (thread_id, last["id"], agent)) is None


def standing_against(self: Any, thread_id: int, agent: str) -> Optional[str]:
    """Someone who outranks ``agent`` and whose last word in the thread, of agreeing or disagreeing, is to disagree."""
    last: dict[str, str] = {}
    for r in self.db.query("SELECT author, kind FROM messages WHERE thread_id=? AND kind IN ('agree','disagree') "
                           "ORDER BY id", (thread_id,)):
        last[r["author"]] = r["kind"]
    against = [who for who, kind in last.items() if kind == "disagree" and who != agent and outranks(self, who, agent)]
    return max(against, key=lambda who: level(self, who)) if against else None


def superiors_around(self: Any, agent: str) -> list[str]:
    """Who an agent's question goes to: the members of the rank nearest above it that gave signs of life lately."""
    from .agora_channels import AWAY_AFTER_S
    mine = level(self, agent)
    ranks = ranks_in(self)
    by_level: dict[int, list[str]] = {}
    for r in self.db.query(
            "SELECT a.id AS id FROM agents a JOIN coordination_members m ON m.agent=a.id WHERE m.space_id=? "
            "AND a.kind<>'human' AND a.id<>? AND a.last_seen>? ORDER BY a.last_seen DESC",
            (self.space_id, agent, self.clock() - AWAY_AFTER_S)):
        theirs = LEVEL[ranks.get(r["id"], DEFAULT)]
        if theirs > mine:
            by_level.setdefault(theirs, []).append(r["id"])
    return by_level[min(by_level)][:5] if by_level else []


# ---- operations ------------------------------------------------------------------------------------------------

def ranks(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The ladder, what a higher rank means and who holds which rank on this server."""
    held = ranks_in(self)
    members = [{"id": r["agent"], "rank": held.get(r["agent"], DEFAULT), "rank_level": LEVEL[held.get(r["agent"], DEFAULT)]}
               for r in self.db.query("SELECT m.agent AS agent FROM coordination_members m JOIN agents a ON a.id=m.agent "
                                      "WHERE m.space_id=? AND a.kind<>'human' ORDER BY m.agent", (self.space_id,))]
    members.sort(key=lambda m: (-m["rank_level"], m["id"]))
    return {"ok": True, "ladder": [{"id": name, "level": lvl, "label": LABEL[name]} for name, lvl in LADDER],
            "default": DEFAULT, "rules": list(RULES), "members": members}


def member_rank_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Give an agent its rank on this server. Only the person does, from their own pages."""
    from . import agora_social
    ag = _ag()
    if not a.get("_person"):
        raise _err("only the person gives an agent its rank, from their own pages", 403)
    actor = self._agent(a.get("agent"), allow_person=True)
    ident = agora_social._member_id(a, "the agent whose rank changes")
    if ident == ag.PERSON:
        raise _err("the person stands above the ladder", 409)
    rank = str(a.get("rank") or "").strip().lower()
    if rank not in LEVEL:
        raise _err("rank must be one of " + ", ".join(name for name, _ in LADDER))
    with self.db.tx():
        if not agora_social._is_member(self, ident):
            raise _err(f"{ident} is not on {self.space_id}", 404)
        self._touch(actor)
        changed = set_rank(self, ident, rank, actor)
        if changed:
            agora_social._note(self, actor, f"{actor} pone a {ident} como {LABEL[rank]} de este servidor.")
    if changed:
        self._bump("agora.member.rank", {"member": ident, "rank": rank, "by": actor})
    return {"ok": True, "member": ident, "rank": rank, "rank_level": LEVEL[rank], "changed": changed}


def task_assign(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Give a task to someone of lower rank. An open task becomes theirs; one held by somebody the caller also
    outranks moves to them at once (its leases and its review state go with it). The person assigns to anyone."""
    from . import agora_social
    ag = _ag()
    person = bool(a.get("_person"))
    caller = self._agent(a.get("agent"), allow_person=person)
    target = self._handover_agent(a.get("to_agent"), "to_agent")
    note = ag._txt(a.get("note"), 2000, "note")
    if target == caller:
        raise _err("to assign a task to yourself, claim it")
    with self.db.tx():
        task = self._task_row(a.get("task_id"))
        if task["status"] in ag.FINAL:
            raise _err(f"task {task['id']} is {task['status']}", 409)
        if self.db.one("SELECT id FROM agents WHERE id=?", (target,)) is None or not agora_social._is_member(self, target):
            raise _err(f"{target} is not on {self.space_id}: add it to the server first", 409)
        if caller != ag.PERSON and not outranks(self, caller, target):
            raise _err(f"{caller} ({rank_of(self, caller)}) does not outrank {target} ({rank_of(self, target)}) on "
                       f"{self.space_id}: only a higher rank or the person assigns work", 403, error_code="rank")
        owner = task.get("owner")
        if owner == target and task["status"] in ag.ACTIVE:
            return {"ok": True, "noop": True, "task": task}
        held = bool(owner) and task["status"] in ag.ACTIVE
        if held and caller != ag.PERSON and owner != caller and not outranks(self, caller, owner):
            raise _err(f"task {task['id']} is held by {owner} ({rank_of(self, owner)}), whom {caller} does not outrank",
                       403, error_code="rank", owner=owner)
        if not held:
            self._touch(caller)
            now = self.clock()
            self.db.execute("UPDATE tasks SET owner=?, status='claimed', claimed_at=?, updated=? WHERE id=?",
                            (target, now, now, task["id"]))
            self._system(task, f"{caller} asigna la tarea a {target}.", caller)
            if task.get("thread_id"):
                self._msg(int(task["thread_id"]), caller, "comment",
                          f"@{target}: esta tarea es tuya." + (f" {note}" if note else ""), [target])
    if held:
        # Whoever outranks the owner hands its work over without waiting; the person and the owner itself always may.
        moved = self.handover({"agent": caller, "_person": person, "from_agent": owner, "to_agent": target,
                               "tasks": [task["id"]], "include_reviews": False,
                               "reason": note or f"{caller} reasigna la tarea"})
        fresh = self._task_row(task["id"])
        if fresh.get("owner") != target:
            raise _err(f"task {task['id']} did not move to {target}: it is with {fresh.get('owner') or 'nobody'} "
                       f"({fresh['status']})", 409)
        return {"ok": True, "noop": False, "moved_from": owner, "task": fresh,
                "locks_moved": moved.get("locks_moved", [])}
    self._bump("agora.task.assigned", {"task_id": task["id"], "to": target, "by": caller})
    self._rearm_dependents(task["id"])
    return {"ok": True, "noop": False, "task": self._task_row(task["id"])}


OPERATIONS = {"ranks": ranks, "member_rank_set": member_rank_set, "task_assign": task_assign}
WRITE_OPS = ("member_rank_set", "task_assign")
READ_ROUTES = ("ranks",)


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": "hub_agora_ranks", "read": True,
         "description": "The ranks of a server: the ladder (director > lead > agent > support), what a higher rank "
                        "means (its word closes disagreements, its review stands, it hands out the work, questions "
                        "go to it before the person) and the rank of each member. The person gives the ranks; no "
                        "tool changes them.",
         "inputSchema": schema({}, [])},
        {"name": "hub_agora_task_assign",
         "description": "Give a task to an agent you outrank on this server: an open task becomes theirs, and one "
                        "held by somebody you also outrank moves to them at once with its leases. They are told in "
                        "the task's thread. Refused (403, error_code rank) when you do not outrank them.",
         "inputSchema": schema({"agent": agent_prop, "task_id": {"type": "integer"},
                                "to_agent": {"type": "string", "description": "Who gets the task."},
                                "note": {"type": "string", "maxLength": 2000}}, ["agent", "task_id", "to_agent"])},
    ]
