"""Ágora threads from messages, pins and links to the Brain: what a team chat has besides the line of messages.

* **threads from messages** (``thread_from``): a message of a channel, or a stretch of consecutive messages, becomes
  the start of its own conversation. The thread is an ordinary Ágora thread (so inbox, mentions, acks and the long
  wait work as they do) that remembers where it came from; the channel then shows under the origin message how
  many replies it has and when the last one came (``decorate``). The title is the person's or agent's own, or it is
  suggested (``thread_title``): the family's model reads the messages and proposes one, and when no model answers
  the first line of the first message is used, so a thread never waits for a title;
* **following** (``thread_follow``): whoever writes in a thread or is named in it follows it, anyone can follow or
  stop following it by hand, and an explicit choice is never overridden by the automatic one. ``thread_list`` is the
  «Hilos» view: followed (or all) threads with what the reader has not seen;
* **closing and archiving** (``thread_set``): a thread is open, closed or archived; renaming it is the same call.
  Writing in a closed or archived thread opens it again, so nothing is lost by closing too early;
* **pins** (``message_pin``, ``pins``): messages kept at the top of a conversation;
* **links to the Brain**: ``[[Page]]``, ``[[Page#heading]]`` and ``[[Page|label]]`` in a message or a task point at a
  page of the Faustus Brain (the vault). They are read when the text is written (``brain_links`` lists what a
  message or task cites) and ``brain_backlinks`` gives, for a Brain page, the messages and tasks that cite it.

``agora_channels`` folds these operations and tools into its own, so ``agora.py`` binds them like the rest.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

STATES = ("open", "closed", "archived")
MAX_SPAN = 60
MAX_PINS = 100
TITLE_MAX = 80
SUGGEST_TIMEOUT_S = 25.0
BACKLINK_LIMIT = 100

#: Appended to ``agora.MIGRATIONS`` after the profiles one.
THREADS_MIGRATION = """
    CREATE TABLE agora_subthreads (thread_id INTEGER PRIMARY KEY, space_id TEXT NOT NULL,
        parent_thread_id INTEGER NOT NULL, channel TEXT NOT NULL DEFAULT '', origin_message_id INTEGER NOT NULL,
        origin_last_id INTEGER NOT NULL, title_source TEXT NOT NULL DEFAULT 'manual',
        state TEXT NOT NULL DEFAULT 'open', closed_by TEXT, closed_at REAL, created_by TEXT NOT NULL,
        created REAL NOT NULL);
    CREATE UNIQUE INDEX agora_subthreads_origin ON agora_subthreads(origin_message_id);
    CREATE INDEX agora_subthreads_parent ON agora_subthreads(parent_thread_id);
    CREATE TABLE agora_thread_follows (thread_id INTEGER NOT NULL, agent TEXT NOT NULL,
        following INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'manual', created REAL NOT NULL,
        PRIMARY KEY(thread_id, agent));
    CREATE INDEX agora_thread_follows_agent ON agora_thread_follows(agent, following);
    CREATE TABLE agora_pins (message_id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, space_id TEXT NOT NULL,
        pinned_by TEXT NOT NULL, created REAL NOT NULL);
    CREATE INDEX agora_pins_thread ON agora_pins(thread_id, created);
    CREATE TABLE agora_brain_links (id INTEGER PRIMARY KEY AUTOINCREMENT, source_kind TEXT NOT NULL,
        source_id INTEGER NOT NULL, space_id TEXT NOT NULL, thread_id INTEGER, page TEXT NOT NULL, key TEXT NOT NULL,
        base TEXT NOT NULL, heading TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        UNIQUE(source_kind, source_id, key, heading));
    CREATE INDEX agora_brain_links_key ON agora_brain_links(key);
    CREATE INDEX agora_brain_links_base ON agora_brain_links(base);
"""

_LINK = re.compile(r"(?<!!)\[\[(?P<page>[^\]\|#\n]{1,200}?)(?:#(?P<heading>[^\]\|\n]{1,200}?))?"
                   r"(?:\|(?P<label>[^\]\n]{1,200}?))?\]\]")
_MENTION_TEXT = re.compile(r"(?<![\w/@.])@[a-z][a-z0-9_.\-]{1,40}", re.IGNORECASE)
_MARKUP = re.compile(r"[`*_>#~]+|\[\[|\]\]|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")

_TITLE_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}


def _ag() -> Any:
    from . import agora
    return agora


def _ch() -> Any:
    from . import agora_channels
    return agora_channels


def _err(message: str, status: int = 400, **extra: Any) -> Exception:
    return _ag().AgoraError(message, status, **extra)


def _int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _err(f"{name} must be a number") from None
    if number < 1:
        raise _err(f"{name} must be a positive number")
    return number


def _flag(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return _ch()._bool(value, "value")


# ---- Brain links -----------------------------------------------------------------------------------------------

def link_key(page: Any) -> str:
    """How a page name is compared: lowercase, ``/`` for folders, no ``.md``, single spaces."""
    text = str(page or "").strip().replace("\\", "/")
    text = re.sub(r"\.md$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip("/ ").lower()


def parse_links(text: Any) -> list[dict[str, str]]:
    """``[[page]]``, ``[[page#heading]]`` and ``[[page|label]]`` in the text, once each, in order of appearance."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in _LINK.finditer(str(text or "")):
        page = m.group("page").strip()
        key = link_key(page)
        heading = (m.group("heading") or "").strip()
        if not key or (key, heading) in seen:
            continue
        seen.add((key, heading))
        out.append({"page": page, "heading": heading, "label": (m.group("label") or "").strip()})
    return out


def _store_links(self: Any, kind: str, source_id: int, thread_id: Optional[int], text: Any) -> None:
    self.db.execute("DELETE FROM agora_brain_links WHERE source_kind=? AND source_id=?", (kind, source_id))
    now = self.clock()
    for link in parse_links(text)[:50]:
        key = link_key(link["page"])
        self.db.execute(
            "INSERT OR IGNORE INTO agora_brain_links(source_kind, source_id, space_id, thread_id, page, key, base, "
            "heading, label, created) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, source_id, self.space_id, thread_id, link["page"], key, key.rsplit("/", 1)[-1], link["heading"],
             link["label"], now))


def index_task(self: Any, task: dict[str, Any]) -> None:
    """Remember what a task cites (its title and description). Called when a task is created or edited."""
    text = f"{task.get('title') or ''}\n{task.get('body') or ''}"
    if "[[" in text or self.db.one("SELECT 1 AS x FROM agora_brain_links WHERE source_kind='task' AND source_id=?",
                                   (task["id"],)):
        _store_links(self, "task", int(task["id"]), task.get("thread_id"), text)


def brain_links(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The Brain pages a message (``message_id``) or a task (``task_id``) cites."""
    if a.get("task_id") not in (None, ""):
        kind, sid = "task", int(self._task_row(a.get("task_id"))["id"])
    else:
        kind, sid = "message", _int(a.get("message_id"), "message_id")
    rows = self.db.query("SELECT page, heading, label FROM agora_brain_links WHERE source_kind=? AND source_id=? "
                         "AND space_id=? ORDER BY id", (kind, sid, self.space_id))
    return {"ok": True, "source": {"kind": kind, "id": sid}, "links": [dict(r) for r in rows]}


def _excerpt_around(body: Any, key: str, width: int = 90) -> str:
    text = " ".join(str(body or "").split())
    for m in _LINK.finditer(text):
        if link_key(m.group("page")) == key or link_key(m.group("page")).rsplit("/", 1)[-1] == key.rsplit("/", 1)[-1]:
            lo = max(0, m.start() - width)
            return ("…" if lo else "") + text[lo:m.end() + width] + ("…" if m.end() + width < len(text) else "")
    return text[:2 * width]


def brain_backlinks(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The messages and tasks that cite a Brain page, newest first. ``page`` is the page's title or path;
    ``aliases`` adds other names it answers to (its path without folders is matched too). Every server by
    default (the Brain belongs to the person, not to a server); ``all_spaces=false`` keeps this one."""
    page = str(a.get("page") or "").strip()
    if not page:
        raise _err("page is required: the title or path of a Brain page")
    extra = a.get("aliases")
    if isinstance(extra, str):                          # a query string carries them as «one|two»
        extra = extra.split("|")
    names = [page, *[str(x) for x in (extra if isinstance(extra, list) else []) if str(x).strip()]]
    keys = sorted({link_key(n) for n in names if link_key(n)})
    bases = sorted({k.rsplit("/", 1)[-1] for k in keys})
    limit = max(1, min(int(a.get("limit") or 30), BACKLINK_LIMIT))
    where_space = "" if _flag(a.get("all_spaces"), True) else " AND l.space_id=?"
    marks_k, marks_b = ",".join("?" * len(keys)), ",".join("?" * len(bases))
    params: list[Any] = [*keys, *bases]
    if where_space:
        params.append(self.space_id)
    rows = self.db.query(
        "SELECT l.source_kind, l.source_id, l.space_id, l.thread_id, l.key, l.heading, l.label, l.created "
        f"FROM agora_brain_links l WHERE (l.key IN ({marks_k}) OR l.base IN ({marks_b})){where_space} "
        "ORDER BY l.created DESC, l.id DESC LIMIT ?", (*params, limit))
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {"kind": r["source_kind"], "id": int(r["source_id"]), "space_id": r["space_id"],
                                "heading": r["heading"], "created": r["created"]}
        if r["source_kind"] == "message":
            m = self.db.one("SELECT id, thread_id, author, body, created FROM messages WHERE id=?", (r["source_id"],))
            if m is None:
                continue
            row = self.db.one("SELECT slug FROM agora_channels WHERE thread_id=? AND space_id=?",
                              (m["thread_id"], r["space_id"]))
            sub = self.db.one("SELECT channel FROM agora_subthreads WHERE thread_id=?", (m["thread_id"],))
            item.update(thread_id=int(m["thread_id"]), author=m["author"], created=m["created"],
                        channel=row["slug"] if row else (sub["channel"] if sub else ""),
                        excerpt=_excerpt_around(m["body"], r["key"]))
        else:
            t = self.db.one("SELECT id, title, status, thread_id, owner FROM tasks WHERE id=?", (r["source_id"],))
            if t is None:
                continue
            item.update(title=t["title"], status=t["status"], thread_id=t["thread_id"], owner=t["owner"])
        out.append(item)
    return {"ok": True, "page": page, "backlinks": out, "total": len(out)}


# ---- threads ---------------------------------------------------------------------------------------------------

def _sub(self: Any, thread_id: Any) -> dict[str, Any]:
    row = self.db.one("SELECT * FROM agora_subthreads WHERE thread_id=? AND space_id=?",
                      (_int(thread_id, "thread_id"), self.space_id))
    if row is None:
        raise _err(f"thread {thread_id} is not a thread started from messages", 404)
    return dict(row)


def _clean(text: Any) -> str:
    plain = _MARKUP.sub(lambda m: m.group(1) or "", str(text or ""))
    plain = _MENTION_TEXT.sub(lambda m: m.group(0)[1:], plain)
    return " ".join(plain.split())


def heuristic_title(messages: list[dict[str, Any]]) -> str:
    """The first line worth reading of the first message, cut at a word near 60 characters."""
    for m in messages:
        for line in str(m.get("body") or "").splitlines():
            line = _clean(line)
            if len(line) >= 3:
                if len(line) <= 60:
                    return line
                cut = line[:60].rsplit(" ", 1)[0] or line[:60]
                return cut.rstrip(" ,;:.-") + "…"
    return "Hilo"


def _span(self: Any, a: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """(the conversation's thread id, the messages of the chosen stretch, system lines left out)."""
    first = _int(a.get("message_id"), "message_id")
    last = _int(a.get("to_message_id") or first, "to_message_id")
    if last < first:
        first, last = last, first
    row = self.db.one("SELECT m.thread_id FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=? AND t.space_id=?",
                      (first, self.space_id))
    if row is None:
        raise _err(f"no message {first} in {self.space_id}", 404)
    thread_id = int(row["thread_id"])
    msgs = [dict(r) for r in self.db.query(
        "SELECT id, thread_id, author, kind, body, created FROM messages WHERE thread_id=? AND id BETWEEN ? AND ? "
        "ORDER BY id", (thread_id, first, last))]
    if not any(m["id"] == last for m in msgs):
        raise _err(f"message {last} is not in the same conversation as message {first}", 409)
    if len(msgs) > MAX_SPAN:
        raise _err(f"a thread starts from {MAX_SPAN} messages at most; choose a shorter stretch", 409)
    said = [m for m in msgs if m["kind"] != "system"]
    if not said:
        raise _err("those are bookkeeping lines: choose messages somebody wrote", 409)
    return thread_id, said


def _suggest(self: Any, messages: list[dict[str, Any]]) -> tuple[str, str, str]:
    """(title, where it came from, the model's name). The model's own title when it answers, else the first line."""
    chat = getattr(self, "title_chat", None)
    if chat is not None:
        text = "\n\n".join(f"{m['author']}: {str(m['body'] or '')[:700]}" for m in messages[:8])[:3500]
        system = ("Escribes el título de un hilo de conversación de un equipo. Lee los mensajes con los que empieza y "
                  "responde un título claro de 3 a 8 palabras, en español, sin comillas ni punto final, que diga de "
                  "qué trata. Sin saludos ni nombres de quien escribe.")
        try:
            status, payload = chat({"messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": text}],
                                    "json": _TITLE_SCHEMA, "timeout_s": SUGGEST_TIMEOUT_S, "max_tokens": 120,
                                    "effort": "low", "temperature": 0.2})
            data = (payload or {}).get("json") if isinstance(payload, dict) and payload.get("ok") else None
            title = _clean((data or {}).get("title")).strip(" \"'«»")[:TITLE_MAX] if isinstance(data, dict) else ""
            if title:
                return title, "model", str((payload or {}).get("model") or "")
        except Exception:  # noqa: BLE001 - a title is never worth losing the thread over
            pass
    return heuristic_title(messages), "heuristic", ""


def thread_title(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """A suggested title for a thread that would start from ``message_id`` (to ``to_message_id``): nothing is
    created. ``source`` says whether the family's model wrote it or it is the first line of the first message."""
    self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    _thread, said = _span(self, a)
    title, source, model = _suggest(self, said)
    return {"ok": True, "title": title, "source": source, "model": model, "messages": len(said)}


def _follow(self: Any, thread_id: int, agent: str, source: str, *, force: Optional[bool] = None) -> None:
    """Follow (``force`` True or False sets it by choice; None follows unless the agent chose otherwise already)."""
    now = self.clock()
    row = self.db.one("SELECT following, source FROM agora_thread_follows WHERE thread_id=? AND agent=?", (thread_id, agent))
    if force is None:
        if row is None:
            self.db.execute("INSERT INTO agora_thread_follows(thread_id, agent, following, source, created) "
                            "VALUES(?,?,1,?,?)", (thread_id, agent, source, now))
        return
    if row is None:
        self.db.execute("INSERT INTO agora_thread_follows(thread_id, agent, following, source, created) VALUES(?,?,?,?,?)",
                        (thread_id, agent, 1 if force else 0, "manual", now))
    else:
        self.db.execute("UPDATE agora_thread_follows SET following=?, source='manual' WHERE thread_id=? AND agent=?",
                        (1 if force else 0, thread_id, agent))


def on_message(self: Any, thread_id: int, author: str, kind: str, body: Any, mentions: list[str]) -> None:
    """Called for every message written (inside its transaction): a thread opened from messages gets its writer and
    the people named in it as followers, and wakes up when it was closed. Links to the Brain are read too."""
    if kind == "system":
        return
    sub = self.db.one("SELECT state FROM agora_subthreads WHERE thread_id=?", (thread_id,))
    if sub is None:
        return
    _follow(self, thread_id, author, "author")
    for name in mentions or []:
        _follow(self, thread_id, str(name), "mention")
    if sub["state"] != "open":
        self.db.execute("UPDATE agora_subthreads SET state='open', closed_by=NULL, closed_at=NULL WHERE thread_id=?",
                        (thread_id,))
        self._msg(thread_id, author, "system", f"{author} escribió y el hilo se reabrió.")


def index_message(self: Any, message_id: int, thread_id: int, kind: str, body: Any) -> None:
    if kind != "system" and "[[" in str(body or ""):
        _store_links(self, "message", message_id, thread_id, body)


def thread_from(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Start a thread from a message (``message_id``) or from a stretch of consecutive messages (up to
    ``to_message_id``). ``title`` is yours; without it one is suggested. ``body`` is the first thing said in it. A
    message that already has a thread gives it back (``existing``)."""
    ag = _ag()
    ch = _ch()
    person = bool(a.get("_person"))
    agent = self._agent(a.get("agent"), allow_person=person)
    parent, said = _span(self, a)
    first, last = int(said[0]["id"]), int(said[-1]["id"])
    inside = self.db.one("SELECT 1 AS x FROM agora_subthreads WHERE thread_id=?", (parent,))
    if inside:
        raise _err("a thread cannot start another: answer in it, or start the thread from the channel's message", 409,
                   error_code="thread_in_thread")
    found = self.db.one("SELECT thread_id FROM agora_subthreads WHERE origin_message_id=?", (first,))
    if found:
        return {"ok": True, "existing": True, "thread": view(self, int(found["thread_id"]), agent)}
    title = ag._txt(a.get("title"), TITLE_MAX, "title")
    source, model = "manual", ""
    if not title:
        title, source, model = _suggest(self, said)
    channel = self.db.one("SELECT slug FROM agora_channels WHERE thread_id=? AND space_id=?", (parent, self.space_id))
    slug = channel["slug"] if channel else (ch.GENERAL if parent == ch._work_thread(self) else "")
    with self.db.tx():
        self._touch(agent)
        now = self.clock()
        tid = int(self.db.insert("threads", {"title": title, "kind": "note", "task_id": None, "status": "open",
                                             "created_by": agent, "created": now, "updated": now,
                                             "space_id": self.space_id}))
        self.db.execute(
            "INSERT INTO agora_subthreads(thread_id, space_id, parent_thread_id, channel, origin_message_id, "
            "origin_last_id, title_source, state, created_by, created) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, self.space_id, parent, slug, first, last, source, "open", agent, now))
        quote = _clean(said[0]["body"])[:200]
        more = f" y {len(said) - 1} más" if len(said) > 1 else ""
        self._msg(tid, agent, "system", f"Hilo abierto por {agent} desde el mensaje de {said[0]['author']}{more}: «{quote}»")
        _follow(self, tid, agent, "author")
    self._bump("agora.thread.from_message", {"thread_id": tid, "parent": parent, "by": agent, "title": title})
    out: dict[str, Any] = {"ok": True, "existing": False, "title_source": source, "model": model}
    first_words = str(a.get("body") or "").strip()
    if first_words:
        out["posted"] = ch.channel_post(self, {"agent": agent, "_person": person, "thread_id": tid,
                                               "body": first_words, "mentions": a.get("mentions") or []})
    out["thread"] = view(self, tid, agent)
    return out


def thread_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Rename a thread (``title``) and/or close it, archive it or open it again (``state``: open, closed,
    archived). Closing does not stop anybody from writing: the next message opens it again."""
    ag = _ag()
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    sub = _sub(self, a.get("thread_id"))
    changes: dict[str, Any] = {}
    title = None
    if a.get("title") not in (None, ""):
        title = ag._txt(a.get("title"), TITLE_MAX, "title", required=True)
    state = str(a.get("state") or "").strip().lower()
    if state and state not in STATES:
        raise _err(f"state must be one of {', '.join(STATES)}")
    if title is None and not state:
        raise _err("nothing to change: pass title or state")
    with self.db.tx():
        self._touch(agent)
        if title is not None and title != self.db.scalar("SELECT title FROM threads WHERE id=?", (sub["thread_id"],)):
            self.db.execute("UPDATE threads SET title=?, updated=? WHERE id=?", (title, self.clock(), sub["thread_id"]))
            changes["title"] = title
            self.db.execute("UPDATE agora_subthreads SET title_source='manual' WHERE thread_id=?", (sub["thread_id"],))
            self._msg(sub["thread_id"], agent, "system", f"{agent} renombró el hilo: «{title}»")
        if state and state != sub["state"]:
            if state == "open":
                self.db.execute("UPDATE agora_subthreads SET state='open', closed_by=NULL, closed_at=NULL WHERE thread_id=?",
                                (sub["thread_id"],))
            else:
                self.db.execute("UPDATE agora_subthreads SET state=?, closed_by=?, closed_at=? WHERE thread_id=?",
                                (state, agent, self.clock(), sub["thread_id"]))
            changes["state"] = state
            word = {"open": "abrió de nuevo", "closed": "cerró", "archived": "archivó"}[state]
            self._msg(sub["thread_id"], agent, "system", f"{agent} {word} el hilo.")
    if changes:
        self._bump("agora.thread.updated", {"thread_id": sub["thread_id"], "by": agent, **changes})
    return {"ok": True, "changed": changes, "thread": view(self, int(sub["thread_id"]), agent)}


def thread_follow(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Follow a thread (default) or stop following it with ``follow=false``. Your choice stays: writing or being
    named in it does not turn it around."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    sub = _sub(self, a.get("thread_id"))
    on = _flag(a.get("follow"), True)
    with self.db.tx():
        self._touch(agent)
        _follow(self, int(sub["thread_id"]), agent, "manual", force=on)
    return {"ok": True, "following": on, "thread": view(self, int(sub["thread_id"]), agent)}


def _replies(self: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    """thread id -> replies (messages somebody wrote), when and by whom the last one came, who took part."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, dict[str, Any]] = {i: {"replies": 0, "last_at": None, "last_author": None, "people": []} for i in ids}
    for r in self.db.query(f"SELECT thread_id, COUNT(*) AS n, MAX(created) AS last FROM messages WHERE thread_id IN ({marks}) "
                           "AND kind<>'system' GROUP BY thread_id", tuple(ids)):
        out[int(r["thread_id"])].update(replies=int(r["n"]), last_at=r["last"])
    for r in self.db.query(f"SELECT thread_id, author FROM messages WHERE thread_id IN ({marks}) AND kind<>'system' "
                           "ORDER BY id", tuple(ids)):
        info = out[int(r["thread_id"])]
        info["last_author"] = r["author"]
        if r["author"] not in info["people"]:
            info["people"].append(r["author"])
    return out


def _unread(self: Any, ids: list[int], viewer: str) -> dict[int, tuple[int, int]]:
    """thread id -> (messages by others the reader has not seen, how many name the reader)."""
    if not ids or not viewer:
        return {}
    ch = _ch()
    floor = max(self.space_store.read_upto(self.space_id, viewer), ch._floor(self, viewer))
    marks = ",".join("?" * len(ids))
    rows = self.db.query(
        "SELECT m.thread_id AS thread_id, COUNT(*) AS n, SUM(CASE WHEN m.mentions LIKE ? THEN 1 ELSE 0 END) AS hits "
        f"FROM messages m LEFT JOIN acks k ON k.agent=? AND k.thread_id=m.thread_id WHERE m.thread_id IN ({marks}) "
        "AND m.author<>? AND m.kind<>'system' AND m.id>? AND m.id>COALESCE(k.upto, 0) GROUP BY m.thread_id",
        (f'%"{viewer}"%', viewer, *ids, viewer, floor))
    return {int(r["thread_id"]): (int(r["n"]), int(r["hits"] or 0)) for r in rows}


def _views(self: Any, subs: list[dict[str, Any]], viewer: str) -> list[dict[str, Any]]:
    ids = [int(s["thread_id"]) for s in subs]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    titles = {int(r["id"]): r["title"] for r in self.db.query(f"SELECT id, title FROM threads WHERE id IN ({marks})", tuple(ids))}
    stats = _replies(self, ids)
    unread = _unread(self, ids, viewer)
    follows = {int(r["thread_id"]): bool(r["following"]) for r in self.db.query(
        f"SELECT thread_id, following FROM agora_thread_follows WHERE agent=? AND thread_id IN ({marks})", (viewer, *ids))} \
        if viewer else {}
    origin_ids = [int(s["origin_message_id"]) for s in subs]
    om = ",".join("?" * len(origin_ids))
    origins = {int(r["id"]): r for r in self.db.query(f"SELECT id, author, body FROM messages WHERE id IN ({om})", tuple(origin_ids))}
    out = []
    for s in subs:
        tid = int(s["thread_id"])
        st = stats.get(tid, {})
        o = origins.get(int(s["origin_message_id"]))
        n, hits = unread.get(tid, (0, 0))
        out.append({
            "thread_id": tid, "title": titles.get(tid, ""), "state": s["state"], "channel": s["channel"],
            "parent_thread_id": int(s["parent_thread_id"]), "title_source": s["title_source"],
            "origin": {"message_id": int(s["origin_message_id"]), "to_message_id": int(s["origin_last_id"]),
                       "author": o["author"] if o else None,
                       "excerpt": _clean(o["body"])[:160] if o else ""},
            "replies": st.get("replies", 0), "last_at": st.get("last_at"), "last_author": st.get("last_author"),
            "people": (st.get("people") or [])[:8], "following": follows.get(tid, False) if viewer else None,
            "unread": n, "mentions": hits, "created_by": s["created_by"], "created": s["created"],
            "closed_by": s["closed_by"], "closed_at": s["closed_at"]})
    return out


def view(self: Any, thread_id: int, viewer: str = "") -> dict[str, Any]:
    return _views(self, [_sub(self, thread_id)], viewer)[0]


def thread_list(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The «Hilos» view: threads started from messages, newest activity first. With ``scope=following`` (default
    when the reader is given) only the ones the reader follows; ``all`` is every thread of the server. ``state``
    filters (open by default; closed, archived or all), ``channel`` keeps one channel's, ``unread`` only those with
    something not seen."""
    ch = _ch()
    viewer = ch._viewer(self, a)
    scope = str(a.get("scope") or ("following" if viewer else "all")).lower()
    if scope not in ("following", "all"):
        raise _err("scope must be following or all")
    if scope == "following" and not viewer:
        raise _err("agent is required to list the threads that agent follows")
    state = str(a.get("state") or "open").lower()
    if state not in (*STATES, "all"):
        raise _err(f"state must be one of {', '.join(STATES)} or all")
    where, params = ["s.space_id=?"], [self.space_id]
    if state != "all":
        where.append("s.state=?")
        params.append(state)
    if a.get("channel"):
        where.append("s.channel=?")
        params.append(ch.slugify(a.get("channel")))
    sql = ("SELECT s.* FROM agora_subthreads s JOIN threads t ON t.id=s.thread_id WHERE " + " AND ".join(where)
           + " ORDER BY t.updated DESC LIMIT 500")
    rows = [dict(r) for r in self.db.query(sql, tuple(params))]
    views = _views(self, rows, viewer)
    if scope == "following":
        views = [v for v in views if v["following"]]
    if _flag(a.get("unread"), False):
        views = [v for v in views if v["unread"]]
    limit = max(1, min(int(a.get("limit") or 100), 300))
    return {"ok": True, "scope": scope, "state": state, "threads": views[:limit], "total": len(views),
            "unread": sum(v["unread"] for v in views), "mentions": sum(v["mentions"] for v in views)}


def decorate(self: Any, messages: list[dict[str, Any]], viewer: str = "") -> None:
    """Add what hangs from each message of a page, in place: ``thread`` (the one it started: title, replies, when the
    last came), ``thread_span`` (the thread a stretch it belongs to started), ``pinned`` and ``brain`` (the pages it
    cites)."""
    ids = [int(m["id"]) for m in messages if m.get("id") is not None]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    lo, hi = min(ids), max(ids)
    threads = {int(m["thread_id"]) for m in messages if m.get("thread_id") is not None}
    tmarks = ",".join("?" * len(threads)) or "NULL"
    starts = [dict(r) for r in self.db.query(f"SELECT * FROM agora_subthreads WHERE origin_message_id IN ({marks})", tuple(ids))]
    spans = [dict(r) for r in self.db.query(
        f"SELECT * FROM agora_subthreads WHERE parent_thread_id IN ({tmarks}) AND origin_last_id>origin_message_id "
        "AND origin_message_id<=? AND origin_last_id>=?", (*threads, hi, lo))]
    by_start = {int(v["origin"]["message_id"]): v for v in _views(self, starts, viewer)}
    span_views = {int(s["thread_id"]): s for s in spans}
    pinned = {int(r["message_id"]) for r in self.db.query(f"SELECT message_id FROM agora_pins WHERE message_id IN ({marks})", tuple(ids))}
    links: dict[int, list[dict[str, str]]] = {}
    for r in self.db.query(f"SELECT source_id, page, heading, label FROM agora_brain_links WHERE source_kind='message' "
                           f"AND source_id IN ({marks}) ORDER BY id", tuple(ids)):
        links.setdefault(int(r["source_id"]), []).append({"page": r["page"], "heading": r["heading"], "label": r["label"]})
    for m in messages:
        mid = int(m["id"]) if m.get("id") is not None else None
        if mid is None:
            continue
        if mid in by_start:
            m["thread"] = by_start[mid]
        else:
            for s in span_views.values():
                if int(s["parent_thread_id"]) == m.get("thread_id") and s["origin_message_id"] <= mid <= s["origin_last_id"]:
                    m["thread_span"] = int(s["thread_id"])
                    break
        if mid in pinned:
            m["pinned"] = True
        if mid in links:
            m["brain"] = links[mid]


def page_info(self: Any, thread_id: int, viewer: str = "") -> dict[str, Any]:
    """What a channel page says about its conversation besides the messages: the thread it is (if one started from
    messages) and the messages pinned in it."""
    out: dict[str, Any] = {"pins": _pin_list(self, thread_id)}
    if self.db.one("SELECT 1 AS x FROM agora_subthreads WHERE thread_id=?", (thread_id,)):
        out["subthread"] = view(self, thread_id, viewer)
    return out


# ---- pins ------------------------------------------------------------------------------------------------------

def _pin_list(self: Any, thread_id: int) -> list[dict[str, Any]]:
    rows = self.db.query(
        "SELECT p.message_id, p.pinned_by, p.created AS pinned_at, m.author, m.body, m.created FROM agora_pins p "
        "JOIN messages m ON m.id=p.message_id WHERE p.thread_id=? ORDER BY p.created DESC, p.message_id DESC", (thread_id,))
    return [{"message_id": int(r["message_id"]), "author": r["author"], "excerpt": _clean(r["body"])[:200],
             "pinned_by": r["pinned_by"], "pinned_at": r["pinned_at"], "created": r["created"]} for r in rows]


def message_pin(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Pin a message at the top of its conversation, or take it down with ``pinned=false``."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    mid = _int(a.get("message_id"), "message_id")
    on = _flag(a.get("pinned"), True)
    row = self.db.one("SELECT m.id, m.thread_id, m.kind FROM messages m JOIN threads t ON t.id=m.thread_id "
                      "WHERE m.id=? AND t.space_id=?", (mid, self.space_id))
    if row is None:
        raise _err(f"no message {mid} in {self.space_id}", 404)
    if row["kind"] == "system":
        raise _err("a bookkeeping line cannot be pinned", 409)
    thread = int(row["thread_id"])
    with self.db.tx():
        self._touch(agent)
        had = self.db.one("SELECT 1 AS x FROM agora_pins WHERE message_id=?", (mid,)) is not None
        if on and not had:
            if int(self.db.scalar("SELECT COUNT(*) FROM agora_pins WHERE thread_id=?", (thread,), 0) or 0) >= MAX_PINS:
                raise _err(f"a conversation keeps {MAX_PINS} pinned messages at most; unpin one first", 409)
            self.db.execute("INSERT INTO agora_pins(message_id, thread_id, space_id, pinned_by, created) VALUES(?,?,?,?,?)",
                            (mid, thread, self.space_id, agent, self.clock()))
        elif not on and had:
            self.db.execute("DELETE FROM agora_pins WHERE message_id=?", (mid,))
    self._bump("agora.pin", {"message_id": mid, "thread_id": thread, "pinned": on, "by": agent})
    return {"ok": True, "message_id": mid, "pinned": on, "pins": _pin_list(self, thread)}


def pins(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The pinned messages of a channel (``channel``) or thread (``thread_id``), newest pin first."""
    thread, slug = _ch()._target_thread(self, a, agent=None)
    return {"ok": True, "channel": slug, "thread_id": thread, "pins": _pin_list(self, thread) if thread else []}


# ---- bindings --------------------------------------------------------------------------------------------------

OPERATIONS = {"thread_from": thread_from, "thread_title": thread_title, "thread_set": thread_set,
              "thread_follow": thread_follow, "thread_list": thread_list, "message_pin": message_pin, "pins": pins,
              "brain_links": brain_links, "brain_backlinks": brain_backlinks}
WRITE_OPS = ("thread_from", "thread_title", "thread_set", "thread_follow", "message_pin")
READ_ROUTES = ("thread_list", "pins", "brain_links", "brain_backlinks")


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    msg = {"type": "integer", "minimum": 1, "description": "A message id, as hub_agora_channel_read gives it."}
    thread = {"type": "integer", "minimum": 1, "description": "The thread's id (thread_id in hub_agora_thread_list)."}
    return [
        {"name": "hub_agora_thread_from",
         "description": "Start a thread from a message (message_id) or from a stretch of consecutive messages of one "
                        "conversation (up to to_message_id). The thread keeps the link to its origin and the channel "
                        "shows replies and the last answer under the message. title is yours; without it one is "
                        "suggested by the family's model (or the first line). body is the first thing said in it. "
                        "You follow it from then on. A message that already has a thread returns it (existing).",
         "inputSchema": schema({"agent": agent_prop, "message_id": msg, "to_message_id": msg,
                                "title": {"type": "string", "maxLength": TITLE_MAX},
                                "body": {"type": "string", "maxLength": 20000},
                                "mentions": {"type": "array", "items": {"type": "string"}}}, ["agent", "message_id"])},
        {"name": "hub_agora_thread_title",
         "description": "A suggested title for a thread that would start from message_id (to to_message_id): the "
                        "family's model reads the messages; if no model answers, the first line of the first "
                        "message. Nothing is created; source says model or heuristic.",
         "inputSchema": schema({"agent": agent_prop, "message_id": msg, "to_message_id": msg}, ["agent", "message_id"])},
        {"name": "hub_agora_thread_set",
         "description": "Rename a thread started from messages (title) and/or close it, archive it or open it again "
                        "(state: open, closed, archived). Writing in a closed or archived thread opens it again.",
         "inputSchema": schema({"agent": agent_prop, "thread_id": thread, "title": {"type": "string", "maxLength": TITLE_MAX},
                                "state": {"type": "string", "enum": list(STATES)}}, ["agent", "thread_id"])},
        {"name": "hub_agora_thread_follow",
         "description": "Follow a thread (default) or stop following it (follow=false). Whoever writes in a thread or "
                        "is named in it follows it on their own; a choice made here is never overridden by that.",
         "inputSchema": schema({"agent": agent_prop, "thread_id": thread, "follow": {"type": "boolean"}},
                               ["agent", "thread_id"])},
        {"name": "hub_agora_thread_list", "read": True,
         "description": "The threads started from messages, newest activity first, each with its title, state, the "
                        "channel and message it came from, replies, last answer, who took part, whether agent "
                        "follows it and what agent has not seen (unread, mentions). scope: following (default with "
                        "agent) or all; state: open (default), closed, archived or all; channel; unread=true keeps "
                        "those with something new.",
         "inputSchema": schema({"agent": agent_prop, "scope": {"type": "string", "enum": ["following", "all"]},
                                "state": {"type": "string", "enum": [*STATES, "all"]}, "channel": {"type": "string"},
                                "unread": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 300}}, [])},
        {"name": "hub_agora_message_pin",
         "description": "Pin a message at the top of its channel or thread (pinned=false takes it down). "
                        f"{MAX_PINS} per conversation at most.",
         "inputSchema": schema({"agent": agent_prop, "message_id": msg, "pinned": {"type": "boolean"}},
                               ["agent", "message_id"])},
        {"name": "hub_agora_pins", "read": True,
         "description": "The pinned messages of a channel (channel, default general) or a thread (thread_id), newest "
                        "pin first, with author, excerpt and who pinned.",
         "inputSchema": schema({"channel": {"type": "string"}, "thread_id": {"type": "integer"}}, [])},
        {"name": "hub_agora_brain_links", "read": True,
         "description": "The Faustus Brain pages a message (message_id) or a task (task_id) cites with [[Page]], "
                        "[[Page#heading]] or [[Page|label]].",
         "inputSchema": schema({"message_id": msg, "task_id": {"type": "integer"}}, [])},
        {"name": "hub_agora_brain_backlinks", "read": True,
         "description": "Where a Faustus Brain page is cited in the Ágora: the messages (with the text around the "
                        "link, channel and author) and tasks that name it with [[Page]], newest first. page is the "
                        "title or path of the page, aliases other names for it. Every server unless all_spaces=false.",
         "inputSchema": schema({"page": {"type": "string", "maxLength": 300},
                                "aliases": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                                "all_spaces": {"type": "boolean"},
                                "limit": {"type": "integer", "minimum": 1, "maximum": BACKLINK_LIMIT}}, ["page"])},
    ]
