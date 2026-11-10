"""Ágora channels: every space as a server with channels, task threads by board column and a member list.

The Ágora already has spaces (one per project), threads, tasks and agents. This module adds the shape a person
reads at a glance, the one of a chat server:

* **servers**: every space with what waits for the person, how much moves in it and who is around;
* **channels**: named conversations inside a space. ``general`` always exists (it is the space's work thread: 32 in
  ``ecosystem``, the work conversation elsewhere) and anyone may create more (``ideas``, ``diseno``…). A channel is
  backed by an ordinary thread, so the inbox, mentions, acks and the long wait keep working as they are;
* **guild**: one read with everything a server view draws: channels with what the reader has not seen, open
  threads, task threads grouped by board column (``open``, ``in_progress``, ``review``, ``done``) so a task moves
  from one group to the next when its status changes, and the members with presence, provider and quota;
* **feed**: what was delivered (submitted, approved, closed, with commits and result), what waits for review, and
  the latest messages of the whole space;
* **pulse**: a long wait on the Ágora's change counter, so a page refreshes when something happened.

* **search**: where something was said, in one space or in all of them;
* **folders**: the folders of this machine, one level at a time, so a page that runs somewhere else can still choose
  the working folder and the context folders of a server (the agents work here).

``agora.py`` binds the operations to :class:`Agora`, the REST routes (``GET /api/agora/servers|guild|channels|
channels/<slug>|feed|search|pulse|folders``, ``POST /api/agora/channel_<op>``) and the MCP tools ``hub_agora_*``.
"""

from __future__ import annotations

import json
import os
import re
import string
import time
import unicodedata
from typing import Any, Optional

from . import agora_social

GENERAL = "general"
BOARD_COLUMNS = ("open", "in_progress", "review", "done")
#: Same grouping as the Hub's own board.
COLUMN_OF = {"open": "open", "claimed": "in_progress", "in_progress": "in_progress", "blocked": "in_progress",
             "changes": "in_progress", "review": "review", "approved": "review", "done": "done", "dropped": "done"}
#: Names the page uses for its own views; a channel may not take them.
RESERVED = ("tablero", "board", "entregas", "deliveries", "revisiones", "reviews", "decisiones", "decisions",
            "bloqueos", "locks", "actividad", "activity", "inicio", "home", "miembros", "members")
MAX_PULSE_WAIT_S = 25.0
#: An agent whose last heartbeat is older than this is shown as away even if it said «working».
AWAY_AFTER_S = 45 * 60.0
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_MENTION = re.compile(r"(?<![\w/@.])@([a-z][a-z0-9_.\-]{1,40})", re.IGNORECASE)
_EVERYONE = ("here", "todos", "all", "everyone", "equipo")
_EPOCH = int(time.time())

#: Appended to ``agora.MIGRATIONS`` in order.
CHANNELS_MIGRATION = """
    CREATE TABLE agora_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, space_id TEXT NOT NULL, slug TEXT NOT NULL,
        title TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'chat',
        position INTEGER NOT NULL DEFAULT 0, thread_id INTEGER, created_by TEXT NOT NULL, created REAL NOT NULL,
        updated REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0, UNIQUE(space_id, slug));
    CREATE TABLE agora_space_meta (space_id TEXT PRIMARY KEY, icon TEXT NOT NULL DEFAULT '',
        color TEXT NOT NULL DEFAULT '', folders TEXT NOT NULL DEFAULT '[]', position INTEGER,
        hidden INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
    CREATE TABLE agora_seen_floor (agent TEXT NOT NULL, space_id TEXT NOT NULL, msg_id INTEGER NOT NULL,
        updated REAL NOT NULL, PRIMARY KEY(agent, space_id));
"""

#: Appended to ``agora.MIGRATIONS`` after the social one: a muted server tells the person nothing outside the page.
MUTED_MIGRATION = """
    ALTER TABLE agora_space_meta ADD COLUMN muted INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE agora_space_meta ADD COLUMN heard_since REAL NOT NULL DEFAULT 0;
"""


def _ag() -> Any:
    from . import agora
    return agora


def _err(message: str, status: int = 400, **extra: Any) -> Exception:
    return _ag().AgoraError(message, status, **extra)


def slugify(value: Any) -> str:
    """``Diseño de UI`` -> ``diseno-de-ui``: lowercase ASCII, digits and dashes, 40 characters at most."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40].strip("-")


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, name: str) -> bool:
    """True/False as JSON gives them, or the words a query string carries; anything else is a mistake, not a yes."""
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    word = str(value).strip().lower()
    if word in ("true", "1", "yes", "si", "sí"):
        return True
    if word in ("false", "0", "no", ""):
        return False
    raise _err(f"{name} must be true or false")


_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|~)")


def _folder(value: Any) -> str:
    """A context folder as written: an absolute path (a drive, a share, / or ~) on one line."""
    text = str(value or "").strip()
    if not text or len(text) > 2048 or any(ord(ch) < 32 for ch in text) or not _ABSOLUTE.match(text):
        raise _err(f"a folder must be an absolute path on one line: {text[:80]!r}")
    return text


def _viewer(self: Any, a: dict[str, Any]) -> str:
    who = str(a.get("agent") or "").strip().lower()
    return who if who and _ag()._AGENT_RE.match(who) else ""


def _floor(self: Any, viewer: str) -> int:
    """The message id from which this reader counts «new» in this space: set the first time it looks, so a first
    visit does not show years of history as unread."""
    row = self.db.one("SELECT msg_id FROM agora_seen_floor WHERE agent=? AND space_id=?", (viewer, self.space_id))
    if row:
        return int(row["msg_id"])
    top = int(self.db.scalar("SELECT MAX(id) FROM messages", (), 0) or 0)
    # Remembered only for who the Agora knows (the person, an agent that has been here): a read with a made-up id
    # counts from now and leaves no row behind.
    if viewer == _ag().PERSON or self.db.one("SELECT 1 AS known FROM agents WHERE id=?", (viewer,)):
        self.db.execute("INSERT OR IGNORE INTO agora_seen_floor(agent, space_id, msg_id, updated) VALUES(?,?,?,?)",
                        (viewer, self.space_id, top, self.clock()))
    return top


def _unread(self: Any, viewer: str) -> dict[int, tuple[int, int]]:
    """thread id -> (messages by others the reader has not seen, how many of them mention it)."""
    if not viewer:
        return {}
    base = max(self.space_store.read_upto(self.space_id, viewer), _floor(self, viewer))
    rows = self.db.query(
        "SELECT m.thread_id AS thread_id, COUNT(*) AS n, SUM(CASE WHEN m.mentions LIKE ? THEN 1 ELSE 0 END) AS hits "
        "FROM messages m JOIN threads t ON t.id=m.thread_id LEFT JOIN acks k ON k.agent=? AND k.thread_id=m.thread_id "
        "WHERE t.space_id=? AND m.id>? AND m.author<>? AND m.kind<>'system' AND m.id>COALESCE(k.upto, 0) "
        "GROUP BY m.thread_id", (f'%"{viewer}"%', viewer, self.space_id, base, viewer))
    return {int(r["thread_id"]): (int(r["n"]), int(r["hits"] or 0)) for r in rows}


# ---- channels --------------------------------------------------------------------------------------------------

def _work_thread(self: Any) -> Optional[int]:
    """The thread ``general`` speaks in when no channel row says otherwise: 32 in ecosystem, the space's work
    conversation elsewhere."""
    ag = _ag()
    found = None
    if self.space_id == ag.DEFAULT_SPACE_ID:
        found = self.db.one("SELECT id FROM threads WHERE id=? AND space_id=?", (ag.HANDOVER_THREAD_ID, self.space_id))
    if not found:
        found = self.db.one(
            "SELECT id FROM threads WHERE space_id=? AND kind='handoff' AND (title LIKE 'Conversaci_n de trabajo%' "
            "OR title LIKE 'Chat de trabajo%' OR lower(title) IN ('general', '#general')) ORDER BY id LIMIT 1",
            (self.space_id,))
    return int(found["id"]) if found else None


def _new_thread(self: Any, slug: str, title: str, agent: str) -> int:
    now = self.clock()
    name = f"#{slug}" if slugify(title) == slug else f"#{slug} · {title}"
    th = self.db.insert("threads", {"title": name[:200], "kind": "handoff" if slug == GENERAL else "note",
                                    "task_id": None, "status": "open", "created_by": agent, "created": now,
                                    "updated": now, "space_id": self.space_id})
    self._msg(th, agent, "system", f"Canal #{slug} abierto por {agent}.")
    return int(th)


def _row(self: Any, slug: str) -> Optional[dict[str, Any]]:
    row = self.db.one("SELECT * FROM agora_channels WHERE space_id=? AND slug=?", (self.space_id, slug))
    return dict(row) if row else None


def _rows(self: Any, *, archived: bool = False) -> list[dict[str, Any]]:
    rows = [dict(r) for r in self.db.query(
        "SELECT * FROM agora_channels WHERE space_id=?" + ("" if archived else " AND archived=0") +
        " ORDER BY position, id", (self.space_id,))]
    general = next((r for r in rows if r["slug"] == GENERAL), None)
    if general is None:
        rows.insert(0, {"id": None, "space_id": self.space_id, "slug": GENERAL, "title": GENERAL, "topic": "",
                        "category": "chat", "position": -1, "thread_id": _work_thread(self),
                        "created_by": "system", "created": None, "updated": None, "archived": 0})
    elif not general.get("thread_id"):
        general["thread_id"] = _work_thread(self)
    for r in rows:
        r["builtin"] = r["slug"] == GENERAL
    return rows


def _ensure_thread(self: Any, slug: str, agent: str) -> int:
    """The thread of a channel, opening it (and remembering it) when the channel has none yet. Inside a tx."""
    row = _row(self, slug)
    if row and row.get("archived"):
        raise _err(f"#{slug} is archived; create it again to bring it back with its messages", 409,
                   error_code="channel_archived")
    if row and row.get("thread_id"):
        return int(row["thread_id"])
    if row is None and slug != GENERAL:
        raise _err(f"no channel #{slug} in {self.space_id}; create it with channel_create", 404)
    thread = (_work_thread(self) if slug == GENERAL else None) or _new_thread(self, slug, slug, agent)
    now = self.clock()
    if row is None:
        self.db.insert("agora_channels", {"space_id": self.space_id, "slug": slug, "title": slug, "topic": "",
                                          "category": "chat", "position": -1 if slug == GENERAL else 0,
                                          "thread_id": thread, "created_by": agent, "created": now, "updated": now})
    else:
        self.db.execute("UPDATE agora_channels SET thread_id=?, updated=? WHERE id=?", (thread, now, row["id"]))
    return int(thread)


def _channel_view(self: Any, row: dict[str, Any], unread: dict[int, tuple[int, int]]) -> dict[str, Any]:
    tid = row.get("thread_id")
    stats = self.db.one("SELECT COUNT(*) AS n, MAX(created) AS last_at FROM messages WHERE thread_id=? AND kind<>'system'",
                        (tid,)) if tid else None
    last = self.db.one("SELECT author, body FROM messages WHERE thread_id=? AND kind<>'system' ORDER BY id DESC LIMIT 1",
                       (tid,)) if tid else None
    n, hits = unread.get(int(tid), (0, 0)) if tid else (0, 0)
    return {"slug": row["slug"], "title": row["title"], "topic": row.get("topic") or "", "category": row.get("category") or "chat",
            "position": row.get("position") or 0, "thread_id": tid, "builtin": bool(row.get("builtin")),
            "archived": bool(row.get("archived")), "created_by": row.get("created_by"),
            "messages": int(stats["n"]) if stats else 0, "last_at": stats["last_at"] if stats else None,
            "last_author": last["author"] if last else None,
            "last_text": (str(last["body"] or "")[:140] if last else ""), "unread": n, "mentions": hits}


def channels(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    a = a or {}
    unread = _unread(self, _viewer(self, a))
    rows = _rows(self, archived=bool(a.get("archived")))
    return {"ok": True, "channels": [_channel_view(self, r, unread) for r in rows]}


def channel_create(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    ag = _ag()
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    title = ag._txt(a.get("title") or a.get("slug"), 80, "title", required=True)
    slug = slugify(a.get("slug") or title)
    if not _SLUG.match(slug):
        raise _err("the channel needs a name with letters or digits")
    if slug in RESERVED:
        raise _err(f"#{slug} is the name of a built-in view; choose another", 409)
    topic = ag._txt(a.get("topic"), 300, "topic")
    category = (ag._txt(a.get("category"), 40, "category") or "chat").lower()
    with self.db.tx():
        self._touch(agent)
        existing = _row(self, slug)
        if existing and not existing["archived"]:
            raise _err(f"#{slug} already exists in {self.space_id}", 409)
        now = self.clock()
        if existing:   # an archived channel comes back with its history, its topic and its category unless given anew
            self.db.execute("UPDATE agora_channels SET archived=0, title=?, topic=?, category=?, updated=? WHERE id=?",
                            (title if a.get("title") else existing["title"],
                             topic if "topic" in a else existing["topic"],
                             category if "category" in a else existing["category"], now, existing["id"]))
            if not existing.get("thread_id"):
                _ensure_thread(self, slug, agent)
        else:
            thread = (_work_thread(self) if slug == GENERAL else None) or _new_thread(self, slug, title, agent)
            position = int(self.db.scalar("SELECT COALESCE(MAX(position), 0) + 1 FROM agora_channels WHERE space_id=?",
                                          (self.space_id,), 1) or 1)
            self.db.insert("agora_channels", {"space_id": self.space_id, "slug": slug, "title": title, "topic": topic,
                                              "category": category, "position": -1 if slug == GENERAL else position,
                                              "thread_id": thread, "created_by": agent, "created": now, "updated": now})
    self._bump("agora.channel.created", {"channel": slug, "by": agent})
    row = next(r for r in _rows(self) if r["slug"] == slug)
    return {"ok": True, "channel": _channel_view(self, row, {})}


def channel_update(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    ag = _ag()
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    slug = slugify(a.get("channel") or a.get("slug"))
    changes: dict[str, Any] = {}
    if "title" in a:
        changes["title"] = ag._txt(a.get("title"), 80, "title", required=True)
    if "topic" in a:
        changes["topic"] = ag._txt(a.get("topic"), 300, "topic")
    if "category" in a:
        changes["category"] = (ag._txt(a.get("category"), 40, "category") or "chat").lower()
    if "position" in a:
        changes["position"] = _int(a.get("position"), 0, -1000, 100000)
    if "archived" in a:
        archived = _bool(a.get("archived"), "archived")
        if slug == GENERAL and archived:
            raise _err("#general cannot be archived", 409)
        changes["archived"] = 1 if archived else 0
    if not changes:
        raise _err("nothing to change: pass title, topic, category, position or archived")
    with self.db.tx():
        self._touch(agent)
        if slug == GENERAL and _row(self, slug) is None:
            _ensure_thread(self, slug, agent)
        row = _row(self, slug)
        if row is None:
            raise _err(f"no channel #{slug} in {self.space_id}", 404)
        changes["updated"] = self.clock()
        self.db.execute("UPDATE agora_channels SET " + ", ".join(f"{k}=?" for k in changes) + " WHERE id=?",
                        (*changes.values(), row["id"]))
    self._bump("agora.channel.updated", {"channel": slug, "by": agent})
    rows = _rows(self, archived=True)
    return {"ok": True, "channel": _channel_view(self, next(r for r in rows if r["slug"] == slug), {})}


def _parse_mentions(self: Any, body: str, author: str) -> list[str]:
    """``@id`` in the text for ids the Ágora knows; ``@here`` / ``@todos`` reach whoever has been around lately."""
    ag = _ag()
    names = {m.group(1).rstrip(".-").lower() for m in _MENTION.finditer(str(body or ""))}
    if not names:
        return []
    out: list[str] = []
    if names & set(_EVERYONE):
        since = self.clock() - ag.STALE_AGENT_S
        out.extend(r["id"] for r in self.db.query(
            "SELECT a.id FROM agents a JOIN coordination_members m ON m.agent=a.id WHERE m.space_id=? AND a.last_seen>? "
            "AND a.kind<>'human' ORDER BY a.last_seen DESC LIMIT 40", (self.space_id, since)))
    known = [n for n in names if n not in _EVERYONE]
    if known:
        marks = ",".join("?" * len(known))
        out.extend(r["id"] for r in self.db.query(f"SELECT id FROM agents WHERE id IN ({marks})", tuple(known)))
        if ag.PERSON in known:
            out.append(ag.PERSON)
    seen: list[str] = []
    for name in out:
        if name != author and name not in seen:
            seen.append(name)
    return seen


def _target_thread(self: Any, a: dict[str, Any], *, agent: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """(thread id, channel slug) for ``thread_id`` or ``channel``. With ``agent`` a channel without thread gets one."""
    if a.get("thread_id") not in (None, ""):
        return int(self._thread_row(a.get("thread_id"))["id"]), None
    slug = slugify(a.get("channel") or GENERAL)
    if not slug:
        raise _err("channel is required: general or the name of a channel of this space")
    if agent:
        return _ensure_thread(self, slug, agent), slug
    row = next((r for r in _rows(self, archived=True) if r["slug"] == slug), None)
    if row is None:
        raise _err(f"no channel #{slug} in {self.space_id}", 404)
    return (int(row["thread_id"]) if row.get("thread_id") else None), slug


def channel_post(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Say something in a channel (``channel``) or in any thread (``thread_id``). ``@id`` in the text mentions."""
    ag = _ag()
    person = bool(a.get("_person"))
    agent = self._agent(a.get("agent"), allow_person=person)
    body = ag._txt(a.get("body"), 20000, "body")
    with self.db.tx():
        self._touch(agent)
        thread, slug = _target_thread(self, a, agent=agent)
        carried = agora_social.prepare(self, thread, a)       # reply_to and files, checked before anything is written
        if not body:
            if not carried["files"]:
                raise _err("body is required")
            body = " · ".join(f["name"] for f in carried["files"])
        mentions = list(ag._list(a.get("mentions"), "mentions"))
        for name in _parse_mentions(self, body, agent):
            if name not in mentions:
                mentions.append(name)
        quoted = carried["reply"]
        if quoted and quoted["kind"] != "system" and quoted["author"] not in (agent, None, "system", *mentions):
            mentions.append(quoted["author"])                 # quoting someone tells them, as naming them does
    out = self.post({"agent": agent, "_person": person, "thread_id": thread, "body": body,
                     "kind": a.get("kind") or "comment", "mentions": mentions})
    agora_social.attach(self, out["message_id"], carried)
    out["channel"] = slug
    out["mentions"] = mentions
    # Naming the person in a channel also tells them outside the page (the hub's notifications decide how and when).
    hook = getattr(self, "mention_hook", None)
    if hook and ag.PERSON in mentions and agent != ag.PERSON and not muted(self, self.space_id):
        try:
            hook({"id": thread, "space_id": self.space_id, "channel": slug, "title": (out.get("thread") or {}).get("title") or ""},
                 agent, body)
        except Exception:  # noqa: BLE001 - a notification that fails never loses the message
            pass
    return out


def channel_read(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The latest messages of a channel or thread, oldest first. With ``agent`` it counts as seen (``peek`` keeps
    it unseen) and ``seen_upto`` says up to where that reader had read before."""
    viewer = _viewer(self, a)
    # The person's reads count as read only from the person's own pages; anyone else looking as the person peeks.
    peek = _bool(a.get("peek", False), "peek") or (viewer == _ag().PERSON and not a.get("_person"))
    thread, slug = _target_thread(self, a, agent=None)
    limit = _int(a.get("limit"), 50, 1, 200)
    before = a.get("before_id")
    before = _int(before, 0, 1, 2 ** 62) if before not in (None, "", 0, "0") else None
    if thread is None:
        return {"ok": True, "channel": slug, "thread": None, "messages": [], "seen_upto": 0,
                "pagination": {"limit": limit, "has_more": False, "next_before_id": None}}
    args: dict[str, Any] = {"thread_id": thread, "limit": limit}
    if before:
        args["before_id"] = before
    page = self.thread_recent(args)
    seen = 0
    if viewer:
        ack = self.db.one("SELECT upto FROM acks WHERE agent=? AND thread_id=?", (viewer, thread))
        seen = max(int(ack["upto"]) if ack else 0, self.space_store.read_upto(self.space_id, viewer), _floor(self, viewer))
        if not peek and not before:
            reader = self._agent(viewer, allow_person=True)
            with self.db.tx():
                self._ack(reader, thread)
    page.update(channel=slug, seen_upto=seen)
    page.pop("hub_acknowledged", None)
    agora_social.decorate(self, page.get("messages") or [], viewer)   # reactions, the quoted message, files
    return page


def seen(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Mark the whole space as read for this reader: nothing older than now counts as new."""
    viewer = _viewer(self, a)
    if not viewer:
        raise _err("agent is required")
    if viewer == _ag().PERSON and not a.get("_person"):
        raise _err("only the person's own pages mark the person's reads", 403)
    if viewer != _ag().PERSON and not self.db.one("SELECT 1 AS known FROM agents WHERE id=?", (viewer,)):
        raise _err(f"the Agora does not know {viewer}", 404)
    top = int(self.db.scalar("SELECT MAX(id) FROM messages", (), 0) or 0)
    self.db.execute("INSERT INTO agora_seen_floor(agent, space_id, msg_id, updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(agent, space_id) DO UPDATE SET msg_id=excluded.msg_id, updated=excluded.updated",
                    (viewer, self.space_id, top, self.clock()))
    return {"ok": True, "seen_upto": top}


# ---- spaces as servers -----------------------------------------------------------------------------------------

def _meta(self: Any, sid: str) -> dict[str, Any]:
    row = self.db.one("SELECT * FROM agora_space_meta WHERE space_id=?", (sid,))
    out = {"icon": "", "color": "", "folders": [], "position": None, "hidden": False, "muted": False}
    if row:
        try:
            folders = json.loads(row["folders"] or "[]")
        except ValueError:
            folders = []
        out.update(icon=row["icon"] or "", color=row["color"] or "", position=row["position"],
                   hidden=bool(row["hidden"]), muted=bool(row["muted"]),
                   folders=[str(f) for f in folders if isinstance(f, str)])
    return out


def muted(self: Any, sid: str) -> bool:
    """Whether the person silenced this server: what happens in it is not announced to them outside the page."""
    return bool(self.db.scalar("SELECT muted FROM agora_space_meta WHERE space_id=?", (sid,), 0))


def heard_since(self: Any, sid: str) -> float:
    """When the person last took the silence off this server (0 when it never was silenced): what was said in it
    before that was said while nobody was to be told, and is not announced afterwards either."""
    return float(self.db.scalar("SELECT heard_since FROM agora_space_meta WHERE space_id=?", (sid,), 0) or 0)


def space_meta_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """How a space looks and what it reads: ``icon`` (an app id, a letter or an emoji), ``color``, ``folders``
    (context folders the agents of this space should know besides its workspace), ``position``, ``hidden`` and
    ``muted`` (nothing that happens in it is announced to the person outside the page). Only the person silences a
    server or hears it again, from their own pages: an agent may not decide what the person is told."""
    ag = _ag()
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    self.space_store.get(self.space_id)
    current = _meta(self, self.space_id)
    was_muted = current["muted"]
    if "icon" in a:
        current["icon"] = ag._txt(a.get("icon"), 120, "icon")
        if not current["icon"].startswith("img:"):
            agora_social.drop_icon(self)         # another icon forgets the server's own picture
    if "color" in a:
        color = ag._txt(a.get("color"), 9, "color")
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise _err("color must be #rrggbb")
        current["color"] = color
    if "folders" in a:
        folders = a.get("folders")
        if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders) or len(folders) > 40:
            raise _err("folders must be a list of up to 40 paths")
        clean: list[str] = []
        for folder in folders:
            path = _folder(folder)
            if path not in clean:
                clean.append(path)
        current["folders"] = clean
    if "position" in a:
        current["position"] = None if a.get("position") in (None, "") else _int(a.get("position"), 0, -1000, 100000)
    if "hidden" in a:
        current["hidden"] = _bool(a.get("hidden"), "hidden")
    if "muted" in a:
        if not a.get("_person"):
            raise _err("only the person silences a server or hears it again, from their own pages", 403)
        current["muted"] = _bool(a.get("muted"), "muted")
    heard = heard_since(self, self.space_id)
    if was_muted and not current["muted"]:
        heard = self.clock()                      # from now on it is heard; what was said meanwhile stays untold
    with self.db.tx():
        self._touch(agent)
        self.db.execute(
            "INSERT INTO agora_space_meta(space_id, icon, color, folders, position, hidden, muted, heard_since, updated) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(space_id) DO UPDATE SET icon=excluded.icon, color=excluded.color, folders=excluded.folders, "
            "position=excluded.position, hidden=excluded.hidden, muted=excluded.muted, heard_since=excluded.heard_since, "
            "updated=excluded.updated",
            (self.space_id, current["icon"], current["color"], json.dumps(current["folders"], ensure_ascii=False),
             current["position"], 1 if current["hidden"] else 0, 1 if current["muted"] else 0, heard, self.clock()))
    self._bump("agora.space.updated", {"by": agent})
    return {"ok": True, "meta": _meta(self, self.space_id)}


def _token(self: Any) -> str:
    return f"{_EPOCH}-{self._version[0]}"


def servers(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Every space as a server: counts, what waits for the person, who is around and what the reader has not seen."""
    a = a or {}
    ag = _ag()
    viewer = _viewer(self, a)
    listing = self.spaces()["spaces"]
    now = self.clock()
    metas = {r["space_id"]: _meta(self, r["space_id"]) for r in self.db.query("SELECT space_id FROM agora_space_meta")}
    online = {r["space_id"]: int(r["n"]) for r in self.db.query(
        "SELECT m.space_id AS space_id, COUNT(*) AS n FROM coordination_members m JOIN agents a ON a.id=m.agent "
        "WHERE a.last_seen>? AND a.kind<>'human' GROUP BY m.space_id", (now - AWAY_AFTER_S,))}
    unread: dict[str, tuple[int, int]] = {}
    if viewer:
        top = int(self.db.scalar("SELECT MAX(id) FROM messages", (), 0) or 0)
        with self.db.tx():
            for sp in listing:   # a space seen for the first time starts counting from now
                self.db.execute("INSERT OR IGNORE INTO agora_seen_floor(agent, space_id, msg_id, updated) VALUES(?,?,?,?)",
                                (viewer, sp["id"], top, now))
        lowest = int(self.db.scalar("SELECT MIN(msg_id) FROM agora_seen_floor WHERE agent=?", (viewer,), 0) or 0)
        for r in self.db.query(
                "SELECT t.space_id AS space_id, COUNT(*) AS n, SUM(CASE WHEN m.mentions LIKE ? THEN 1 ELSE 0 END) AS hits "
                "FROM messages m JOIN threads t ON t.id=m.thread_id "
                "JOIN agora_seen_floor f ON f.agent=? AND f.space_id=t.space_id "
                "LEFT JOIN acks k ON k.agent=? AND k.thread_id=m.thread_id "
                "LEFT JOIN coordination_reads r ON r.agent=? AND r.space_id=t.space_id "
                "WHERE m.id>? AND m.author<>? AND m.kind<>'system' AND m.id>f.msg_id AND m.id>COALESCE(k.upto, 0) "
                "AND m.id>COALESCE(r.last_msg_id, 0) GROUP BY t.space_id",
                (f'%"{viewer}"%', viewer, viewer, viewer, lowest, viewer)):
            unread[r["space_id"]] = (int(r["n"]), int(r["hits"] or 0))
    out = []
    for sp in listing:
        meta = metas.get(sp["id"]) or {"icon": "", "color": "", "folders": [], "position": None, "hidden": False,
                                       "muted": False}
        n, hits = unread.get(sp["id"], (0, 0))
        out.append({"id": sp["id"], "title": sp["title"], "description": sp.get("description") or "",
                    "project_id": sp.get("project_id") or "", "workspace": sp.get("workspace") or "",
                    "revision": sp.get("revision"), "members": len(sp.get("members") or []),
                    "online": online.get(sp["id"], 0), "counts": sp.get("counts") or {},
                    "last_activity": sp.get("last_activity"), "unread": n, "mentions": hits, **meta})
    home = ag.DEFAULT_SPACE_ID
    out.sort(key=lambda s: (0 if s["id"] == home else 1,
                            s["position"] if s["position"] is not None else 10 ** 6,
                            -(s["last_activity"] or 0), s["title"].lower()))
    return {"ok": True, "servers": out, "default_space_id": home, "version": _token(self)}


def _members(self: Any, tasks_by_owner: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    ag = _ag()
    now = self.clock()
    pools = self._quota_pools()
    locks = {r["owner"]: int(r["n"]) for r in self.db.query(
        "SELECT owner, COUNT(*) AS n FROM locks WHERE expires>? GROUP BY owner", (now,))}
    out = []
    for r in self.db.query("SELECT a.* FROM agents a JOIN coordination_members m ON m.agent=a.id "
                           "WHERE m.space_id=? ORDER BY a.last_seen DESC", (self.space_id,)):
        d = dict(r)
        ident = d["id"]
        person = ident == ag.PERSON
        quiet = now - float(d.get("last_seen") or 0)
        quota = self._quota_state_from(ident, pools)
        state = str(d.get("state") or "idle")
        if person:
            presence = "person"
        elif not d.get("last_seen"):
            presence = "invited"         # put on the server by name (member_add); it has not said anything yet
        elif quiet > ag.STALE_AGENT_S:
            presence = "offline"
        elif quota["state"] == "exhausted":
            presence = "no_quota"
        elif state == "working" and quiet > AWAY_AFTER_S:
            presence = "away"
        else:
            presence = state if state in ag.AGENT_STATES else "idle"
        out.append({"id": ident, "name": d.get("name") or ident, "kind": d.get("kind") or "agent",
                    "role": "person" if person else (quota.get("provider") or "other"),
                    "presence": presence, "state": state, "doing": d.get("doing") or "", "note": d.get("note") or "",
                    "last_seen": d.get("last_seen"), "locks": locks.get(ident, 0),
                    "tasks": tasks_by_owner.get(ident, [])[:6],
                    "quota": {k: quota.get(k) for k in ("state", "remaining_percent", "reset_at", "note", "pool_label")}})
    return out


def guild(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Everything one server view draws, in one read."""
    a = a or {}
    viewer = _viewer(self, a)
    done_limit = _int(a.get("done_limit"), 25, 0, 200)
    space = self.space_store.get(self.space_id)
    unread = _unread(self, viewer)
    chans = [_channel_view(self, r, unread) for r in _rows(self)]
    channel_threads = {c["thread_id"] for c in chans if c["thread_id"]}

    cols = "id, title, status, owner, reviewer, kind, priority, repo, thread_id, created, updated, submitted_at, closed_at, review_state, created_by"
    active = [dict(r) for r in self.db.query(
        f"SELECT {cols} FROM tasks WHERE space_id=? AND status NOT IN ('done','dropped') "
        "ORDER BY priority, updated DESC, id DESC LIMIT 400", (self.space_id,))]
    finished = [dict(r) for r in self.db.query(
        f"SELECT {cols} FROM tasks WHERE space_id=? AND status IN ('done','dropped') "
        "ORDER BY COALESCE(closed_at, updated) DESC LIMIT ?", (self.space_id, done_limit))]
    waits: dict[int, list[int]] = {}
    for r in self.db.query(
            "SELECT d.task_id AS task_id, d.blocker_id AS blocker_id FROM agora_task_deps d JOIN tasks b ON b.id=d.blocker_id "
            "JOIN tasks t ON t.id=d.task_id WHERE t.space_id=? AND b.status NOT IN ('approved','done','dropped')",
            (self.space_id,)):
        waits.setdefault(int(r["task_id"]), []).append(int(r["blocker_id"]))
    groups: dict[str, list[dict[str, Any]]] = {c: [] for c in BOARD_COLUMNS}
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for t in [*active, *finished]:
        n, hits = unread.get(int(t["thread_id"]), (0, 0)) if t.get("thread_id") else (0, 0)
        t.update(unread=n, mentions=hits, waiting_on=waits.get(int(t["id"]), []), column=COLUMN_OF.get(t["status"], "open"))
        groups[t["column"]].append(t)
        if t.get("owner") and t["status"] not in ("done", "dropped"):
            by_owner.setdefault(t["owner"], []).append({"id": t["id"], "title": t["title"], "status": t["status"]})
    counts = {s: 0 for s in _ag().TASK_STATUSES}
    for r in self.db.query("SELECT status, COUNT(*) AS n FROM tasks WHERE space_id=? GROUP BY status", (self.space_id,)):
        counts[r["status"]] = int(r["n"])
    column_counts = {c: sum(n for s, n in counts.items() if COLUMN_OF.get(s) == c and s != "dropped") for c in BOARD_COLUMNS}

    threads = []
    for r in self.db.query(
            "SELECT t.id, t.title, t.kind, t.status, t.created_by, t.created, t.updated, "
            "(SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id AND m.kind<>'system') AS messages, "
            "(SELECT author FROM messages m WHERE m.thread_id=t.id ORDER BY id DESC LIMIT 1) AS last_author "
            "FROM threads t WHERE t.space_id=? AND t.kind<>'task' AND t.status<>'resolved' "
            "ORDER BY CASE t.status WHEN 'escalated' THEN 0 ELSE 1 END, t.updated DESC LIMIT 80", (self.space_id,)):
        d = dict(r)
        if d["id"] in channel_threads:
            continue
        n, hits = unread.get(int(d["id"]), (0, 0))
        d.update(unread=n, mentions=hits)
        threads.append(d)
    waiting = [dict(r) for r in self.db.query(
        "SELECT t.id, t.title, t.kind, t.task_id, t.updated, (SELECT body FROM messages m WHERE m.thread_id=t.id AND "
        "m.kind='escalation' ORDER BY m.id DESC LIMIT 1) AS question, (SELECT author FROM messages m WHERE "
        "m.thread_id=t.id AND m.kind='escalation' ORDER BY m.id DESC LIMIT 1) AS escalated_by FROM threads t "
        "WHERE t.status='escalated' AND t.space_id=? ORDER BY t.updated DESC LIMIT 40", (self.space_id,))]
    now = self.clock()
    views = {
        "board": {"counts": column_counts},
        "deliveries": {"count": int(self.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE space_id=? AND MAX(COALESCE(closed_at, 0), COALESCE(submitted_at, 0))>?",
            (self.space_id, now - 86400.0), 0) or 0)},
        "reviews": {"count": counts.get("review", 0) + counts.get("changes", 0)},
        "decisions": {"count": int(self.db.scalar(
            "SELECT COUNT(*) FROM threads WHERE space_id=? AND status='resolved' AND kind<>'task'", (self.space_id,), 0) or 0)},
        "locks": {"count": int(self.db.scalar("SELECT COUNT(*) FROM locks WHERE expires>?", (now,), 0) or 0)},
        "waiting": {"count": len(waiting)},
    }
    # Everyone who is around, and of those long gone only the latest: a space that many one-off agents passed through
    # would otherwise carry them all on every read. ``members_older`` says how many were left out.
    offline_limit = _int(a.get("offline_limit"), 40, 0, 1000)
    members, gone, older = [], 0, 0
    for member in _members(self, by_owner):                  # newest heartbeat first
        if member["presence"] == "offline":
            gone += 1
            if gone > offline_limit:
                older += 1
                continue
        members.append(member)
    return {"ok": True, "version": _token(self), "viewer": viewer or None,
            "space": {**space, "members": len(space.get("members") or []), "meta": _meta(self, self.space_id)},
            "channels": chans, "views": views, "threads": threads, "waiting": waiting,
            "tasks": groups, "task_counts": counts, "column_counts": column_counts,
            "members": members, "members_older": older, "providers": self.usage().get("providers", [])}


# ---- feeds and pulse -------------------------------------------------------------------------------------------

def feed(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """``kind=deliveries`` (default): what was sent to review, approved or closed, newest first, with commits and the
    result. ``reviews``: what waits for a reviewer or for changes. ``activity``: the latest messages of the space."""
    a = a or {}
    kind = str(a.get("kind") or "deliveries").lower()
    limit = _int(a.get("limit"), 40, 1, 200)
    # ``before`` is what the previous page gave as ``next_before``: a message id for activity, ``<when>:<task id>`` for
    # the others (a bare number still works), so two deliveries of the same instant are never skipped.
    raw_before, before_id = str(a.get("before") if a.get("before") is not None else "").strip(), None
    try:
        if ":" in raw_before:
            head, tail = raw_before.rsplit(":", 1)
            before, before_id = float(head), int(tail)
        else:
            before = float(raw_before) if raw_before else None
    except (TypeError, ValueError):
        raise _err("before must be the next_before of the previous page") from None
    if kind == "activity":
        cursor = int(before) if before is not None else 2 ** 62
        rows = self.db.query(
            "SELECT m.id, m.thread_id, m.author, m.kind, m.body, m.mentions, m.created, t.title AS thread_title, "
            "t.kind AS thread_kind, t.task_id AS task_id FROM messages m JOIN threads t ON t.id=m.thread_id "
            "WHERE t.space_id=? AND m.id<? AND m.kind<>'system' ORDER BY m.id DESC LIMIT ?",
            (self.space_id, cursor, limit + 1))
        items = []
        for r in rows[:limit]:
            d = dict(r)
            d["mentions"] = _ag()._l(d.get("mentions"))
            d["body"] = str(d.get("body") or "")[:4000]
            items.append(d)
        return {"ok": True, "kind": kind, "items": items,
                "next_before": items[-1]["id"] if len(rows) > limit and items else None}
    if kind not in ("deliveries", "reviews"):
        raise _err("kind must be deliveries, reviews or activity")
    when = "MAX(COALESCE(closed_at, 0), COALESCE(submitted_at, 0))"
    where = ["space_id=?"]
    params: list[Any] = [self.space_id]
    if kind == "reviews":
        where.append("status IN ('review','changes','approved')")
    else:
        where.append("(submitted_at IS NOT NULL OR status IN ('review','approved','changes','done'))")
    if before is not None and before_id is not None:
        where.append(f"({when}<? OR ({when}=? AND id<?))")
        params.extend([before, before, before_id])
    elif before is not None:
        where.append(f"{when}<?")
        params.append(before)
    rows = self.db.query(
        "SELECT id, title, status, owner, reviewer, review_state, kind, repo, branch, commits, reviewed_commits, result, "
        f"thread_id, submitted_at, closed_at, updated, priority, submission_revision, {when} AS at FROM tasks "
        "WHERE " + " AND ".join(where) + f" ORDER BY {when} DESC, id DESC LIMIT ?", (*params, limit + 1))
    items = []
    for r in rows[:limit]:
        d = dict(r)
        d["commits"] = _ag()._l(d.get("commits"))
        d["reviewed_commits"] = _ag()._l(d.get("reviewed_commits"))
        text = str(d.pop("result") or "")
        if not text and d.get("thread_id") and d.get("owner"):
            last = self.db.one("SELECT body FROM messages WHERE thread_id=? AND author=? AND kind NOT IN ('system') "
                               "ORDER BY id DESC LIMIT 1", (d["thread_id"], d["owner"]))
            text = str(last["body"] or "") if last else ""
        d["summary"] = text[:2000]
        d["summary_truncated"] = len(text) > 2000
        d["column"] = COLUMN_OF.get(d["status"], "open")
        items.append(d)
    return {"ok": True, "kind": kind, "items": items,
            "next_before": f"{items[-1]['at']!r}:{items[-1]['id']}" if len(rows) > limit and items else None}


def _snippet(text: str, needle: str, around: int = 110) -> str:
    body = " ".join(str(text or "").split())
    at = body.lower().find(needle.lower())
    if at < 0:
        return body[:around * 2]
    start = max(0, at - around)
    end = min(len(body), at + len(needle) + around)
    return ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")


def search(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Find where something was said: messages whose text has ``q``, plus tasks and threads whose title has it
    (``#12`` or ``12`` finds task 12). This space, or every space with ``all_spaces``. Newest first."""
    a = a or {}
    q = " ".join(str(a.get("q") or "").split())
    if len(q) < 2:
        raise _err("q needs at least two characters")
    if len(q) > 200:
        raise _err("q is too long")
    limit = _int(a.get("limit"), 30, 1, 100)
    everywhere = bool(a.get("all_spaces"))
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    scope, scope_args = ("", ()) if everywhere else (" AND {t}.space_id=?", (self.space_id,))
    number = q.lstrip("#")
    task_id = int(number) if number.isdigit() and len(number) <= 9 else -1
    tasks = [dict(r) for r in self.db.query(
        "SELECT k.id, k.title, k.status, k.owner, k.thread_id, k.space_id, k.updated FROM tasks k "
        "WHERE (k.title LIKE ? ESCAPE '\\' OR k.id=?)" + scope.format(t="k") +
        " ORDER BY (k.id=?) DESC, k.updated DESC LIMIT ?", (like, task_id, *scope_args, task_id, min(limit, 20)))]
    for task in tasks:
        task["column"] = COLUMN_OF.get(task["status"], "open")
    threads = [dict(r) for r in self.db.query(
        "SELECT t.id, t.title, t.kind, t.status, t.space_id, t.updated FROM threads t WHERE t.kind<>'task' "
        "AND t.title LIKE ? ESCAPE '\\'" + scope.format(t="t") + " ORDER BY t.updated DESC LIMIT ?",
        (like, *scope_args, min(limit, 20)))]
    messages = []
    for r in self.db.query(
            "SELECT m.id, m.thread_id, m.author, m.kind, m.body, m.created, t.title AS thread_title, t.kind AS thread_kind, "
            "t.task_id AS task_id, t.space_id AS space_id FROM messages m JOIN threads t ON t.id=m.thread_id "
            "WHERE m.kind<>'system' AND m.body LIKE ? ESCAPE '\\'" + scope.format(t="t") + " ORDER BY m.id DESC LIMIT ?",
            (like, *scope_args, limit)):
        d = dict(r)
        d["snippet"] = _snippet(d.pop("body"), q)
        messages.append(d)
    return {"ok": True, "q": q, "all_spaces": everywhere, "tasks": tasks, "threads": threads, "messages": messages}


def pulse(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The Ágora's change token. With ``since`` (the token of the last read) and ``wait_s`` (up to 25) it answers
    as soon as something changes, so a page can wait here instead of asking for everything every few seconds."""
    a = a or {}
    since = str(a.get("since") or "")
    try:
        wait = max(0.0, min(float(a.get("wait_s") or 0), MAX_PULSE_WAIT_S))
    except (TypeError, ValueError):
        wait = 0.0
    deadline = time.monotonic() + wait
    while True:
        with self._changed:
            token = _token(self)
            if token != since or not since or time.monotonic() >= deadline:
                break
            self._changed.wait(timeout=max(0.05, min(5.0, deadline - time.monotonic())))
    return {"ok": True, "version": token, "changed": token != since}


MAX_FOLDERS = 2000


def folders(a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The folders inside ``path`` on this machine, names only and never files: its drives (``/`` elsewhere) when ``path``
    is empty. ``parent`` goes one level up ("" = the drives) and ``home`` is the person's folder, a good place to start."""
    raw = str((a or {}).get("path") or "").strip().strip('"')
    home = os.path.expanduser("~")
    if not raw:
        roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")] if os.name == "nt" else ["/"]
        return {"ok": True, "path": "", "parent": None, "home": home, "sep": os.sep,
                "folders": [{"name": r, "path": r} for r in roots]}
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise _err(f"no existe la carpeta {raw}", 404, error_code="folder_not_found")
    found = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.name.startswith(("$", ".")) and entry.name != "System Volume Information":
                        found.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
    except OSError as exc:
        raise _err(f"no puedo leer {raw}: {exc.strerror or exc}", 403, error_code="folder_unreadable") from exc
    found.sort(key=lambda f: f["name"].lower())
    up = os.path.dirname(path.rstrip("\\/")) if path.rstrip("\\/") else ""
    parent = "" if (not up or up == path or os.path.splitdrive(path)[1] in ("\\", "/", "")) else up
    return {"ok": True, "path": path, "parent": parent, "home": home, "sep": os.sep,
            "folders": found[:MAX_FOLDERS], "truncated": len(found) > MAX_FOLDERS}


# Members, reactions, quoting, files, a server's own picture and the order of its channels live in agora_social.
OPERATIONS = {"channels": channels, "channel_create": channel_create, "channel_update": channel_update,
              "channel_post": channel_post, "channel_read": channel_read, "seen": seen, "servers": servers,
              "guild": guild, "feed": feed, "search": search, "pulse": pulse, "space_meta_set": space_meta_set,
              **agora_social.OPERATIONS}
WRITE_OPS = ("channel_create", "channel_update", "channel_post", "channel_read", "seen", "space_meta_set",
             *agora_social.WRITE_OPS)
READ_ROUTES = ("servers", "guild", "channels", "feed", "search", "pulse", *agora_social.READ_ROUTES)


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    chan = {"type": "string", "description": "Channel name in this space: general or one created with channel_create."}
    return [
        {"name": "hub_agora_servers", "read": True,
         "description": "Every Ágora space as a server: title, project, workspace and context folders, task counts, "
                        "threads waiting for the person, agents around lately and, with agent, what that reader has "
                        "not seen (unread, mentions).",
         "inputSchema": schema({"agent": agent_prop}, [])},
        {"name": "hub_agora_guild", "read": True,
         "description": "One space as a server view in one read: channels with unread counts, open threads, task "
                        "threads grouped by board column (open, in_progress, review, done), threads waiting for the "
                        "person, and members with presence, provider, what they are doing, their open tasks and quota.",
         "inputSchema": schema({"agent": agent_prop, "done_limit": {"type": "integer", "minimum": 0, "maximum": 200},
                                "offline_limit": {"type": "integer", "minimum": 0, "maximum": 1000}}, [])},
        {"name": "hub_agora_channels", "read": True,
         "description": "The channels of a space (general always exists) with their thread, topic, category, last "
                        "message and, with agent, unread and mentions.",
         "inputSchema": schema({"agent": agent_prop, "archived": {"type": "boolean"}}, [])},
        {"name": "hub_agora_channel_create",
         "description": "Create a channel in a space: a named conversation (ideas, diseno, bugs…) backed by a thread, "
                        "so inbox, mentions and waits work as usual. category groups it in the list (chat by default).",
         "inputSchema": schema({"agent": agent_prop, "title": {"type": "string", "maxLength": 80},
                                "slug": {"type": "string", "maxLength": 40}, "topic": {"type": "string", "maxLength": 300},
                                "category": {"type": "string", "maxLength": 40}}, ["agent", "title"])},
        {"name": "hub_agora_channel_update",
         "description": "Rename a channel, change its topic, category or position, or archive it (its messages stay).",
         "inputSchema": schema({"agent": agent_prop, "channel": chan, "title": {"type": "string", "maxLength": 80},
                                "topic": {"type": "string", "maxLength": 300}, "category": {"type": "string", "maxLength": 40},
                                "position": {"type": "integer"}, "archived": {"type": "boolean"}}, ["agent", "channel"])},
        {"name": "hub_agora_channel_post",
         "description": "Say something in a channel (channel, default general) or in any thread (thread_id). @id in "
                        "the text mentions that agent, @luis the person, @here whoever has been around lately; "
                        "mentions adds more. kind: comment (default), proposal, agree or disagree. reply_to quotes "
                        "a message of the same conversation (and tells its author); files carries files sent with "
                        "hub_agora_file_add (then body may be empty).",
         "inputSchema": schema({"agent": agent_prop, "channel": chan, "thread_id": {"type": "integer"},
                                "body": {"type": "string", "maxLength": 20000},
                                "kind": {"type": "string", "enum": ["comment", "proposal", "agree", "disagree"]},
                                "mentions": {"type": "array", "items": {"type": "string"}},
                                "reply_to": {"type": "integer", "minimum": 1},
                                "files": {"type": "array", "items": {"type": "string"}, "maxItems": 10}}, ["agent"])},
        {"name": "hub_agora_channel_read",
         "description": "The latest messages of a channel (channel) or thread (thread_id), oldest first; before_id "
                        "pages back. With agent the thread counts as seen unless peek, and seen_upto says up to where "
                        "that reader had read.",
         "inputSchema": schema({"agent": agent_prop, "channel": chan, "thread_id": {"type": "integer"},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                                "before_id": {"type": "integer", "minimum": 1}, "peek": {"type": "boolean"}}, [])},
        {"name": "hub_agora_seen",
         "description": "Mark a whole space as read for an agent: nothing older than now counts as unread.",
         "inputSchema": schema({"agent": agent_prop}, ["agent"])},
        {"name": "hub_agora_feed", "read": True,
         "description": "What happened in a space, newest first. kind=deliveries: tasks sent to review, approved or "
                        "closed, with commits and result; reviews: what waits for a reviewer or for changes; "
                        "activity: the latest messages of every thread. before pages back with next_before.",
         "inputSchema": schema({"kind": {"type": "string", "enum": ["deliveries", "reviews", "activity"]},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                                "before": {"type": "number"}}, [])},
        {"name": "hub_agora_search", "read": True,
         "description": "Find where something was said: messages whose text contains q (with a snippet around it), "
                        "and tasks and threads whose title does; #12 or 12 finds task 12. This space, or every "
                        "space with all_spaces. Newest first.",
         "inputSchema": schema({"q": {"type": "string", "minLength": 2, "maxLength": 200}, "all_spaces": {"type": "boolean"},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["q"])},
        {"name": "hub_agora_pulse", "read": True,
         "description": "The Ágora's change token. Pass the last token as since and wait_s (up to 25) to be answered "
                        "as soon as anything changes.",
         "inputSchema": schema({"since": {"type": "string"}, "wait_s": {"type": "number", "minimum": 0, "maximum": 25}}, [])},
        {"name": "hub_agora_space_meta_set",
         "description": "How a space looks and what it reads: icon (an app id, a letter or an emoji; img:… is its own "
                        "picture, set with hub_agora_space_icon_set), color (#rrggbb), "
                        "folders (context folders besides its workspace), position in the server list and hidden. "
                        "Whether a server is muted (it announces nothing to the person outside the page) is the "
                        "person's own choice, made from their pages: hub_agora_servers shows it, no tool changes it.",
         "inputSchema": schema({"agent": agent_prop, "icon": {"type": "string", "maxLength": 120},
                                "color": {"type": "string"}, "folders": {"type": "array", "items": {"type": "string"}},
                                "position": {"type": ["integer", "null"]}, "hidden": {"type": "boolean"}}, ["agent"])},
        {"name": "hub_agora_folders", "read": True,
         "description": "The folders inside path on the Hub's machine (its drives when path is empty), names only: to "
                        "choose the working folder or the context folders of a space. parent goes one level up.",
         "inputSchema": schema({"path": {"type": "string", "maxLength": 1000}}, [])},
        *agora_social.tools(schema, agent_prop),
    ]
