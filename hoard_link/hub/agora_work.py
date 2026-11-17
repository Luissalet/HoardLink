"""What an Ágora message says its author did: the tools used since their last message, and the routine behind it.

* **Work.** A message of an agent can carry ``work``: the tools it used, in order, each with its name, result
  (``ok``), duration (``ms``) and a short summary, and when the work ``started`` and ``ended``. The author may
  declare it when posting (``work`` on ``hub_agora_post`` / ``hub_agora_channel_post``); without it, the Hub looks at
  the observed transcript of the author's session and tells the finished tool calls that no earlier message has
  told yet and that began after the author's previous message. The call that is writing the message is still
  open, so it is never counted. The summaries go through the observer's masking (secret-looking text is dropped
  or masked) and are cut short.
* **Routine.** A message written by a routine or scheduled task carries ``routine`` ``{id, name, schedule}``: the
  agent's page lists its declared routines; naming one of them fills its schedule in.

Both are stored apart from the message, travel with it in ``channel_read``, ``thread`` and ``feed``, and are read in
full with ``work``. Only agents have them: what the person writes carries neither.

The same module tells how far a task's owner is in the turn it is working on (``live``): calls made, calls
finished and since when, read from the observed session, for the cards of the board.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Optional

from .agent_watch import snippet

MAX_TOOLS = 200
#: A page of messages carries at most this many tools per message; ``work`` has all of them.
PAGE_TOOLS = 60
MAX_MS = 7 * 24 * 3600 * 1000
KINDS = ("comment", "proposal", "agree", "disagree")

WORK_MIGRATION = """
    CREATE TABLE agora_message_work (message_id INTEGER PRIMARY KEY, space_id TEXT NOT NULL, thread_id INTEGER NOT NULL,
        author TEXT NOT NULL, tools TEXT NOT NULL DEFAULT '[]', started REAL, ended REAL,
        source TEXT NOT NULL DEFAULT 'observed', created REAL NOT NULL);
    CREATE INDEX agora_message_work_space ON agora_message_work(space_id, message_id);
    CREATE TABLE agora_message_routine (message_id INTEGER PRIMARY KEY, space_id TEXT NOT NULL, routine_id TEXT NOT NULL,
        name TEXT NOT NULL, schedule TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
    CREATE INDEX agora_message_routine_space ON agora_message_routine(space_id, routine_id);
"""


def _ag() -> Any:
    from . import agora
    return agora


def _err(message: str, status: int = 400) -> Exception:
    return _ag().AgoraError(message, status)


def _number(value: Any, what: str, *, low: float = 0.0, high: float = 4e12) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise _err(f"{what} must be a number")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise _err(f"{what} must be a number") from None
    if out != out or not low <= out <= high:
        raise _err(f"{what} is out of range")
    return out


def _slug(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40]


# ---- what is carried ----------------------------------------------------------------------------------------------

def clean_tool(raw: Any) -> dict[str, Any]:
    """One tool of a ``work``: ``{name, ok, ms, summary}``; a bare string is a tool name."""
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise _err("each tool of work is an object {name, ok, ms, summary}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise _err("each tool of work needs a name")
    ok = raw.get("ok")
    if ok is not None and not isinstance(ok, bool):
        raise _err("ok of a tool is true, false or absent")
    ms = _number(raw.get("ms"), "ms of a tool", high=MAX_MS)
    return {"name": name[:80], "ok": ok, "ms": None if ms is None else int(ms), "summary": snippet(raw.get("summary"), 160)}


def clean_work(raw: Any) -> Optional[dict[str, Any]]:
    """The work an agent declares: ``{tools: [...], started, ended}`` or just the list of tools."""
    if raw in (None, "", {}, []):
        return None
    if isinstance(raw, list):
        raw = {"tools": raw}
    if not isinstance(raw, dict):
        raise _err("work is an object {tools: [{name, ok, ms, summary}], started, ended}")
    tools = raw.get("tools")
    if not isinstance(tools, list):
        raise _err("work.tools is a list of tools")
    if len(tools) > MAX_TOOLS:
        raise _err(f"work.tools carries at most {MAX_TOOLS} tools")
    started, ended = _number(raw.get("started"), "work.started"), _number(raw.get("ended"), "work.ended")
    if started is not None and ended is not None and ended < started:
        raise _err("work.ended is before work.started")
    cleaned = [clean_tool(t) for t in tools]
    if not cleaned:
        return None
    return {"tools": cleaned, "started": started, "ended": ended, "source": "declared"}


def from_calls(calls: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The work of the finished calls an observed session ran, oldest first."""
    calls = calls[-MAX_TOOLS:]
    if not calls:
        return None
    tools = []
    for call in calls:
        began, ended = call.get("started") or 0.0, call.get("ended") or 0.0
        ms = int(max(0.0, ended - began) * 1000) if began and ended else None
        tools.append({"name": str(call.get("name") or "tool")[:80], "ok": call.get("ok"),
                      "ms": None if ms is None or ms > MAX_MS else ms, "summary": snippet(call.get("summary"), 160)})
    starts = [c["started"] for c in calls if c.get("started")]
    ends = [c["ended"] for c in calls if c.get("ended")]
    return {"tools": tools, "started": min(starts) if starts else None, "ended": max(ends) if ends else None,
            "source": "observed"}


def clean_routine(self: Any, agent: str, raw: Any) -> Optional[dict[str, str]]:
    """``routine`` of a message: a name, or ``{id, name, schedule}``. A declared routine of the agent fills the schedule."""
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise _err("routine is a name or an object {id, name, schedule}")
    name = " ".join(str(raw.get("name") or raw.get("id") or "").split())[:80]
    if not name:
        raise _err("routine needs a name")
    ident = _slug(raw.get("id") or name)
    if not ident:
        raise _err("routine needs an id or a name made of letters or digits")
    schedule = " ".join(str(raw.get("schedule") or "").split())[:80]
    if not schedule:
        row = self.db.one("SELECT schedule FROM agora_agent_routines WHERE agent=? AND name=?", (agent, name))
        schedule = str(row["schedule"]) if row else ""
    return {"id": ident, "name": name, "schedule": schedule}


# ---- writing ------------------------------------------------------------------------------------------------------

def collect(self: Any, agent: str, a: dict[str, Any], *, person: bool) -> tuple[Optional[dict[str, Any]], Optional[dict[str, str]]]:
    """``(work, routine)`` of a message about to be written, checked before anything is written. What the person
    writes carries neither. Without a declared ``work`` the observed session of the author is asked."""
    if person:
        return None, None
    work = clean_work(a.get("work"))
    routine = clean_routine(self, agent, a.get("routine"))
    hook = getattr(self, "work_hook", None)
    if work is None and hook is not None:
        marks = ",".join("?" * len(KINDS))
        since = float(self.db.scalar(f"SELECT MAX(created) FROM messages WHERE author=? AND kind IN ({marks})",
                                     (agent, *KINDS), 0) or 0)
        try:
            got = hook(agent, since)
        except Exception:  # noqa: BLE001 - a message never fails because the observer could not be read
            got = None
        work = from_calls((got or {}).get("calls") or [])
    return work, routine


def record(self: Any, message_id: int, thread_id: int, agent: str, work: Optional[dict[str, Any]],
           routine: Optional[dict[str, str]]) -> dict[str, Any]:
    """Store what ``collect`` found for a message just written. Returns it as the message will show it."""
    now = self.clock()
    out: dict[str, Any] = {}
    if work:
        self.db.execute(
            "INSERT OR REPLACE INTO agora_message_work(message_id, space_id, thread_id, author, tools, started, ended, source, created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (message_id, self.space_id, thread_id, agent, json.dumps(work["tools"], ensure_ascii=False),
             work["started"], work["ended"], work["source"], now))
        out["work"] = _view(work["tools"], work["started"], work["ended"], work["source"], full=False)
    if routine:
        self.db.execute(
            "INSERT OR REPLACE INTO agora_message_routine(message_id, space_id, routine_id, name, schedule, created) "
            "VALUES(?,?,?,?,?,?)", (message_id, self.space_id, routine["id"], routine["name"], routine["schedule"], now))
        out["routine"] = dict(routine)
    return out


# ---- reading ------------------------------------------------------------------------------------------------------

def _view(tools: list[dict[str, Any]], started: Optional[float], ended: Optional[float], source: str, *, full: bool) -> dict[str, Any]:
    failed = sum(1 for t in tools if t.get("ok") is False)
    spent = sum(int(t["ms"]) for t in tools if t.get("ms"))
    if started and ended and ended >= started:
        duration = float(ended - started)
    else:
        duration = spent / 1000.0
    shown = tools if full else tools[:PAGE_TOOLS]
    return {"count": len(tools), "ok": sum(1 for t in tools if t.get("ok") is True), "failed": failed,
            "duration_s": round(duration, 1), "started": started, "ended": ended, "source": source,
            "tools": shown, "more": len(tools) - len(shown)}


def _rows(self: Any, ids: list[int]) -> tuple[dict[int, Any], dict[int, Any]]:
    marks = ",".join("?" * len(ids))
    work = {int(r["message_id"]): r for r in self.db.query(
        f"SELECT * FROM agora_message_work WHERE message_id IN ({marks})", tuple(ids))}
    routine = {int(r["message_id"]): r for r in self.db.query(
        f"SELECT * FROM agora_message_routine WHERE message_id IN ({marks})", tuple(ids))}
    return work, routine


def _loads(text: Any) -> list[dict[str, Any]]:
    try:
        out = json.loads(text) if text else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def decorate(self: Any, messages: list[dict[str, Any]], *, full: bool = False, light: bool = False) -> None:
    """Add ``work`` and ``routine`` to the messages that carry them, in place. ``full`` keeps every tool of a work;
    otherwise a page carries the first ones and says how many more there are. ``light`` carries only the counts."""
    ids = [int(m["id"]) for m in messages if m.get("id") is not None]
    if not ids:
        return
    work, routine = _rows(self, ids)
    for m in messages:
        mid = int(m["id"]) if m.get("id") is not None else 0
        row = work.get(mid)
        if row is not None:
            m["work"] = _view(_loads(row["tools"]), row["started"], row["ended"], row["source"], full=full)
            if light:
                m["work"].update(tools=[], more=m["work"]["count"])
        row = routine.get(mid)
        if row is not None:
            m["routine"] = {"id": row["routine_id"], "name": row["name"], "schedule": row["schedule"]}


def work(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The work and routine of one message in full (``message_id``), or the latest messages of this space that carry
    work, newest first (optionally of one ``agent`` or one ``routine``)."""
    a = a or {}
    if a.get("message_id") not in (None, ""):
        try:
            mid = int(a["message_id"])
        except (TypeError, ValueError):
            raise _err("message_id must be a number") from None
        msg = self.db.one("SELECT m.id, m.thread_id, m.author, m.created FROM messages m JOIN threads t ON t.id=m.thread_id "
                          "WHERE m.id=? AND t.space_id=?", (mid, self.space_id))
        if msg is None:
            raise _err(f"no message {mid} in {self.space_id}", 404)
        item = {"id": int(msg["id"]), "thread_id": int(msg["thread_id"]), "author": msg["author"], "created": msg["created"]}
        decorate(self, [item], full=True)
        return {"ok": True, "message": item, "has_work": "work" in item, "routine": item.get("routine")}
    try:
        limit = max(1, min(int(a.get("limit") or 20), 100))
    except (TypeError, ValueError):
        raise _err("limit must be a number") from None
    where, params = ["w.space_id=?"], [self.space_id]
    if a.get("for_agent"):
        where.append("w.author=?")
        params.append(str(a["for_agent"]).strip().lower())
    rows = self.db.query(
        "SELECT w.message_id AS id, w.thread_id, w.author, w.created FROM agora_message_work w WHERE " + " AND ".join(where)
        + " ORDER BY w.message_id DESC LIMIT ?", (*params, limit))
    items = [dict(r) for r in rows]
    decorate(self, items)
    if a.get("routine"):
        key = _slug(a["routine"])
        by_routine = [dict(r) for r in self.db.query(
            "SELECT message_id AS id, thread_id FROM agora_message_routine r JOIN messages m ON m.id=r.message_id "
            "WHERE r.space_id=? AND r.routine_id=? ORDER BY r.message_id DESC LIMIT ?", (self.space_id, key, limit))]
        out = []
        for row in by_routine:
            msg = self.db.one("SELECT id, thread_id, author, created FROM messages WHERE id=?", (row["id"],))
            if msg is not None:
                out.append(dict(msg))
        decorate(self, out)
        items = out
    return {"ok": True, "messages": items}


# ---- how far an owner is in the turn ------------------------------------------------------------------------------

def live(self: Any, owners: list[str]) -> dict[str, dict[str, Any]]:
    """``{agent: {steps, done, since, elapsed_s, phase, tool}}`` for the owners whose observed session has a turn
    running; the hook is the observer's. Nothing for the rest."""
    hook = getattr(self, "live_hook", None)
    if hook is None or not owners:
        return {}
    try:
        found = hook(sorted(set(owners))) or {}
    except Exception:  # noqa: BLE001 - the board never depends on the observer
        return {}
    now = self.clock()
    out = {}
    for agent, item in found.items():
        since = item.get("since")
        out[agent] = {**item, "elapsed_s": round(max(0.0, now - since), 1) if since else None}
    return out


# ---- operations ---------------------------------------------------------------------------------------------------

OPERATIONS = {"work": work}
WRITE_OPS: tuple[str, ...] = ()
READ_ROUTES = ("work",)

WORK_PROP = {
    "type": "object",
    "description": "What you did for this message: tools [{name, ok, ms, summary}] in order, and started/ended "
                   "(seconds since the epoch). Without it the Hub tells the finished tool calls of your observed "
                   "session since your last message.",
    "properties": {"tools": {"type": "array", "maxItems": MAX_TOOLS, "items": {"type": "object", "properties": {
        "name": {"type": "string", "maxLength": 80}, "ok": {"type": "boolean"},
        "ms": {"type": "integer", "minimum": 0}, "summary": {"type": "string", "maxLength": 200}}, "required": ["name"]}},
        "started": {"type": "number"}, "ended": {"type": "number"}},
    "required": ["tools"],
}
ROUTINE_PROP = {
    "type": "object",
    "description": "The routine or scheduled task that writes this message: {name, id, schedule}. Naming a routine "
                   "declared on your agent page fills its schedule in.",
    "properties": {"name": {"type": "string", "maxLength": 80}, "id": {"type": "string", "maxLength": 40},
                   "schedule": {"type": "string", "maxLength": 80}},
    "required": ["name"],
}


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": "hub_agora_work", "read": True,
         "description": "What a message says its author did: with message_id, that message's work (every tool in "
                        "order with its result, duration and summary, and when it started and ended) and routine; "
                        "without it, the latest messages of this server that carry work, newest first, of one agent "
                        "(for_agent) or one routine (routine). The Hub fills work in from the observed session of "
                        "an agent that posts without declaring it.",
         "inputSchema": schema({"message_id": {"type": "integer", "minimum": 1}, "for_agent": {"type": "string"},
                                "routine": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, [])},
    ]
