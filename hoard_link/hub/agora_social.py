"""Ágora, the social side of a server: who belongs to it, and what a message can carry besides its text.

* **members** — ``member_add`` puts an agent on a server before it has ever said anything (so it can be mentioned
  and given work), ``member_remove`` takes one out (the work it held in the space is freed or handed over and its
  leases dropped), ``members_prune`` takes out everyone who has not come for a long time and holds nothing, and
  ``members`` lists them all, also those long gone (the server view only carries the latest of them);
* **reactions** (``react``), **quoting** (``reply_to`` when posting) and **files** (``file_add``, then ``files``
  when posting; ``GET /api/agora/file/<id>`` gives one back) on the messages of a channel or thread;
* **a picture as the icon of a server** (``space_icon_set``; ``GET /api/agora/space_icon``);
* **the order of the channels** in one call (``channels_reorder``).

``agora_channels`` folds these operations and tools into its own, so ``agora.py`` binds them like the rest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 10
MAX_ICON_BYTES = 300 * 1024
MAX_REACTION_KINDS = 20
PRUNE_DEFAULT_DAYS = 30
#: A file sent and never attached to a message is forgotten after this long.
UNATTACHED_KEEP_S = 24 * 3600.0
#: What a browser may show in place; anything else is handed over as a download.
INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "text/plain", "application/pdf")
#: The statuses of a task somebody is still working on; a delivery waiting for review stays as it is.
_IN_HAND = ("claimed", "in_progress", "blocked", "changes")
_ICON_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png", "image/png"), (b"\xff\xd8\xff", "jpg", "image/jpeg"),
               (b"GIF87a", "gif", "image/gif"), (b"GIF89a", "gif", "image/gif"))
_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+\-]{0,60}/[a-z0-9][a-z0-9.+\-]{0,80}$")
_EMOJI_BAD = re.compile(r"[\s\x00-\x1f\x7f<>\"'&]")
_FILE_ID = re.compile(r"^[0-9a-f]{32}$")

#: Appended to ``agora.MIGRATIONS`` after the channels one.
SOCIAL_MIGRATION = """
    CREATE TABLE agora_reactions (message_id INTEGER NOT NULL, agent TEXT NOT NULL, emoji TEXT NOT NULL,
        created REAL NOT NULL, PRIMARY KEY(message_id, agent, emoji));
    CREATE TABLE agora_message_meta (message_id INTEGER PRIMARY KEY, reply_to INTEGER, files TEXT NOT NULL DEFAULT '[]');
    CREATE TABLE agora_files (id TEXT PRIMARY KEY, space_id TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
        size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_by TEXT NOT NULL, created REAL NOT NULL,
        message_id INTEGER);
    CREATE INDEX agora_files_message ON agora_files(message_id);
"""


def _ag() -> Any:
    from . import agora
    return agora


def _ch() -> Any:
    from . import agora_channels
    return agora_channels


def _err(message: str, status: int = 400, **extra: Any) -> Exception:
    return _ag().AgoraError(message, status, **extra)


def _store(self: Any, name: str) -> Path:
    """A folder next to the Ágora's database (a temporary one when the database lives in memory)."""
    base = Path(str(self.db.path))
    root = Path(tempfile.gettempdir()) / "agora-memory" if str(base) == ":memory:" else base.parent
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _member_id(a: dict[str, Any], what: str) -> str:
    ag = _ag()
    ident = str(a.get("id") or a.get("member") or "").strip().lower()
    if not ag._AGENT_RE.match(ident):
        raise _err(f"id is required: {what} (a short name such as codex-docs: letters, digits, - _ .)")
    if ident in ("hub", "ui", "system"):
        raise _err(f"{ident!r} is reserved", 409)
    return ident


def _in_hand(self: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in self.db.query("SELECT id, title, status, owner FROM tasks WHERE space_id=? AND owner IS NOT NULL "
                           "AND status NOT IN ('done','dropped') ORDER BY priority, updated DESC", (self.space_id,)):
        out.setdefault(r["owner"], []).append({"id": r["id"], "title": r["title"], "status": r["status"]})
    return out


def _is_member(self: Any, ident: str) -> bool:
    return self.db.one("SELECT 1 AS x FROM coordination_members WHERE space_id=? AND agent=?",
                       (self.space_id, ident)) is not None


def _note(self: Any, actor: str, text: str) -> None:
    """A line in the server's general channel, so whoever reads it knows who came and who left. Inside a tx."""
    ch = _ch()
    self._msg(ch._ensure_thread(self, ch.GENERAL, actor), actor, "system", text)


# ---- members ---------------------------------------------------------------------------------------------------

def members(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Everyone on this server, newest sign of life first, also those long gone. ``q`` filters by name."""
    a = a or {}
    rows = _ch()._members(self, _in_hand(self))
    q = str(a.get("q") or "").strip().lower()
    if q:
        rows = [m for m in rows if q in m["id"] or q in str(m.get("doing") or "").lower()]
    return {"ok": True, "members": rows[:1000], "total": len(rows)}


def member_add(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Put an agent on this server by name, before it has said anything here: it can then be mentioned and given
    work, and shows as invited until its first sign of life. The provider is read from the name (``codex-…``,
    ``claude-…``, ``cursor-…``, ``faustus-…``). Adding someone who is already here changes nothing but the note."""
    ag = _ag()
    actor = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    ident = _member_id(a, "the agent to add")
    if ident == ag.PERSON:
        raise _err("the person is on every server already", 409)
    note = ag._txt(a.get("note"), 500, "note")
    with self.db.tx():
        self._touch(actor)
        now = self.clock()
        registered = self.db.one("SELECT id FROM agents WHERE id=?", (ident,)) is None
        if registered:
            self.db.execute("INSERT INTO agents(id, name, kind, note, state, doing, last_seen, created) "
                            "VALUES(?,?,?,?,?,?,?,?)", (ident, ident, "agent", note, "idle", "", None, now))
        elif note:
            self.db.execute("UPDATE agents SET note=? WHERE id=?", (note, ident))
        added = not _is_member(self, ident)
        if added:
            self.space_store.join(self.space_id, ident)
            _note(self, actor, f"{actor} añade a {ident} a este servidor." + (f" {note}" if note else ""))
    if added:
        self._bump("agora.member.added", {"member": ident, "by": actor})
    view = next((m for m in _ch()._members(self, _in_hand(self)) if m["id"] == ident), None)
    return {"ok": True, "member": view, "added": added, "registered": registered}


def _drop_membership(self: Any, ident: str) -> bool:
    """Take ``ident`` off this server; True when it is on no server any more and the Ágora forgets it. Inside a tx."""
    self.db.execute("DELETE FROM coordination_members WHERE space_id=? AND agent=?", (self.space_id, ident))
    self.db.execute("DELETE FROM agora_seen_floor WHERE agent=? AND space_id=?", (ident, self.space_id))
    if self.db.one("SELECT 1 AS x FROM coordination_members WHERE agent=? LIMIT 1", (ident,)) is not None:
        return False
    self.db.execute("DELETE FROM locks WHERE owner=?", (ident,))
    self.db.execute("DELETE FROM agents WHERE id=?", (ident,))
    return True


def member_remove(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Take an agent off this server. What it was working on here is handed to ``to_agent`` when given, otherwise
    it goes back to open; its leases on that work are dropped and the reviews it was doing wait for someone else.
    A delivery of its own that waits for review stays as it is. If it shows up here again it simply joins again.

    The person and the agent itself may always do it; another agent may once it has been silent for half an hour,
    or at once with ``force``."""
    ag, ch = _ag(), _ch()
    person = bool(a.get("_person"))
    actor = self._agent(a.get("agent"), allow_person=person)
    ident = _member_id(a, "the agent to take off this server")
    if ident == ag.PERSON:
        raise _err("the person is not a member that can be removed", 409)
    to = str(a.get("to_agent") or "").strip().lower()
    reason = ag._txt(a.get("reason"), 2000, "reason")
    force = ch._bool(a.get("force", False), "force")
    with self.db.tx():
        if not _is_member(self, ident):
            raise _err(f"{ident} is not on {self.space_id}", 404)
        row = self.db.one("SELECT last_seen FROM agents WHERE id=?", (ident,))
        quiet = self.clock() - float((row["last_seen"] if row else 0) or 0)
        if actor not in (ag.PERSON, ident) and not force and quiet < self.handover_idle_s:
            raise _err(f"{ident} gave signs of life {int(quiet // 60)} min ago: before "
                       f"{int(self.handover_idle_s // 60)} min of silence only the person, {ident} itself or "
                       "force may take it off", 409, error_code="member_active")
    handed: list[int] = []
    if to:
        if to == ident:
            raise _err("to_agent must be another agent")
        moved = self.handover({"agent": actor, "_person": person, "from_agent": ident, "to_agent": to, "force": True,
                               "reason": reason or f"{ident} sale de {self.space_id}"})
        handed = sorted({*moved.get("owner_moved", []), *moved.get("reviewer_moved", [])})
    freed: list[int] = []
    waiting: list[int] = []
    with self.db.tx():
        self._touch(actor)
        now = self.clock()
        for t in self.db.query("SELECT id, thread_id, status FROM tasks WHERE space_id=? AND owner=? "
                               "AND status NOT IN ('done','dropped') ORDER BY id", (self.space_id, ident)):
            if t["status"] not in _IN_HAND:
                waiting.append(int(t["id"]))
                continue
            self.db.execute("UPDATE tasks SET status='open', owner=NULL, updated=?, closed_at=NULL WHERE id=?",
                            (now, t["id"]))
            self.db.execute("DELETE FROM locks WHERE task_id=?", (t["id"],))
            if t["thread_id"]:
                self._msg(int(t["thread_id"]), actor, "system", f"{ident} sale del servidor: la tarea queda libre.")
            freed.append(int(t["id"]))
        self.db.execute("UPDATE tasks SET reviewer=NULL, updated=? WHERE space_id=? AND reviewer=? "
                        "AND status IN ('review','changes')", (now, self.space_id, ident))
        gone = _drop_membership(self, ident)
        _note(self, actor, f"{actor} da de baja a {ident} en este servidor." + (f" {reason}" if reason else ""))
    for tid in freed:
        try:
            self._rearm_dependents(tid)
        except Exception:  # noqa: BLE001 - waking dependents is a nicety of the release, never its condition
            pass
    self._bump("agora.member.removed", {"member": ident, "by": actor, "freed": freed, "handed": handed})
    return {"ok": True, "removed": ident, "freed_tasks": freed, "handed_tasks": handed, "handed_to": to or None,
            "left_in_review": waiting, "forgotten": gone}


def members_prune(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Take off this server every agent that has given no sign of life for ``older_than_days`` (30) and holds
    neither work nor leases here. ``dry_run`` only says who would go. Whoever holds something is kept and named."""
    a = a or {}
    ag, ch = _ag(), _ch()
    actor = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    days = ch._int(a.get("older_than_days"), PRUNE_DEFAULT_DAYS, 1, 3650)
    dry = ch._bool(a.get("dry_run", False), "dry_run")
    removed: list[str] = []
    kept: list[dict[str, str]] = []
    with self.db.tx():
        now = self.clock()
        cutoff = now - days * 86400.0
        busy = {r["owner"] for r in self.db.query(
            "SELECT DISTINCT owner FROM tasks WHERE space_id=? AND owner IS NOT NULL AND status NOT IN ('done','dropped')",
            (self.space_id,))}
        reviewing = {r["reviewer"] for r in self.db.query(
            "SELECT DISTINCT reviewer FROM tasks WHERE space_id=? AND reviewer IS NOT NULL AND status IN ('review','changes')",
            (self.space_id,))}
        leasing = {r["owner"] for r in self.db.query("SELECT DISTINCT owner FROM locks WHERE expires>?", (now,))}
        for r in self.db.query("SELECT a.id AS id, a.kind AS kind, a.last_seen AS last_seen, a.created AS created "
                               "FROM agents a JOIN coordination_members m ON m.agent=a.id WHERE m.space_id=? "
                               "ORDER BY a.id", (self.space_id,)):
            ident = r["id"]
            if ident in (ag.PERSON, actor) or r["kind"] == "human":
                continue
            if float(r["last_seen"] or r["created"] or 0) >= cutoff:
                continue
            why = "tasks" if ident in busy else "reviews" if ident in reviewing else "leases" if ident in leasing else ""
            if why:
                kept.append({"id": ident, "why": why})
            else:
                removed.append(ident)
        if removed and not dry:
            self._touch(actor)
            for ident in removed:
                _drop_membership(self, ident)
            _note(self, actor, f"{actor} da de baja a {len(removed)} agentes que no venían desde hace más de {days} días.")
    if removed and not dry:
        self._bump("agora.member.pruned", {"by": actor, "count": len(removed)})
    return {"ok": True, "removed": removed, "kept": kept, "dry_run": dry, "older_than_days": days}


# ---- what a message carries ------------------------------------------------------------------------------------

def _message(self: Any, value: Any, what: str = "message_id") -> dict[str, Any]:
    """A message of this space, or 404."""
    try:
        mid = int(value)
    except (TypeError, ValueError):
        raise _err(f"{what} must be the number of a message") from None
    row = self.db.one("SELECT m.id AS id, m.thread_id AS thread_id, m.author AS author, m.kind AS kind, m.body AS body "
                      "FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=? AND t.space_id=?",
                      (mid, self.space_id))
    if row is None:
        raise _err(f"no message {mid} in {self.space_id}", 404)
    return dict(row)


def react(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Put a reaction on a message, or take it back: ``on`` true or false; without it the reaction is toggled."""
    actor = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    emoji = str(a.get("emoji") or "").strip()
    if not emoji or len(emoji) > 16 or _EMOJI_BAD.search(emoji):
        raise _err("emoji is one emoji or a short word without spaces (16 characters at most)")
    with self.db.tx():
        msg = _message(self, a.get("message_id"))
        self._touch(actor)
        has = self.db.one("SELECT 1 AS x FROM agora_reactions WHERE message_id=? AND agent=? AND emoji=?",
                          (msg["id"], actor, emoji)) is not None
        on = (not has) if a.get("on") is None else _ch()._bool(a.get("on"), "on")
        if on and not has:
            kinds = {r["emoji"] for r in self.db.query("SELECT DISTINCT emoji FROM agora_reactions WHERE message_id=?",
                                                       (msg["id"],))}
            if emoji not in kinds and len(kinds) >= MAX_REACTION_KINDS:
                raise _err(f"that message already has {MAX_REACTION_KINDS} different reactions", 409)
            self.db.execute("INSERT INTO agora_reactions(message_id, agent, emoji, created) VALUES(?,?,?,?)",
                            (msg["id"], actor, emoji, self.clock()))
        elif not on and has:
            self.db.execute("DELETE FROM agora_reactions WHERE message_id=? AND agent=? AND emoji=?",
                            (msg["id"], actor, emoji))
    self._bump("agora.message.reaction", {"thread_id": msg["thread_id"], "message_id": msg["id"], "by": actor,
                                          "emoji": emoji, "on": on})
    shown = [{"id": msg["id"]}]
    decorate(self, shown, actor)
    return {"ok": True, "message_id": msg["id"], "on": on, "reactions": shown[0]["reactions"]}


def _file_view(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "type": row["type"], "size": int(row["size"]),
            "inline": row["type"] in INLINE_TYPES}


def decorate(self: Any, messages: list[dict[str, Any]], viewer: str = "") -> None:
    """Add ``reactions``, ``reply`` (the message it quotes) and ``files`` to each message, in place."""
    ids = [int(m["id"]) for m in messages if m.get("id") is not None]
    for m in messages:
        m.setdefault("reactions", [])
        m.setdefault("files", [])
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    by_message: dict[int, dict[str, list[str]]] = {}
    for r in self.db.query(f"SELECT message_id, emoji, agent FROM agora_reactions WHERE message_id IN ({marks}) "
                           "ORDER BY created, rowid", tuple(ids)):
        by_message.setdefault(int(r["message_id"]), {}).setdefault(r["emoji"], []).append(r["agent"])
    meta = {int(r["message_id"]): r for r in self.db.query(
        f"SELECT message_id, reply_to, files FROM agora_message_meta WHERE message_id IN ({marks})", tuple(ids))}
    quoted_ids = sorted({int(r["reply_to"]) for r in meta.values() if r["reply_to"]})
    quoted: dict[int, Any] = {}
    if quoted_ids:
        qm = ",".join("?" * len(quoted_ids))
        quoted = {int(r["id"]): r for r in self.db.query(
            f"SELECT id, author, body FROM messages WHERE id IN ({qm})", tuple(quoted_ids))}
    files: dict[int, dict[str, Any]] = {}
    for r in self.db.query(f"SELECT id, name, type, size, message_id FROM agora_files WHERE message_id IN ({marks})",
                           tuple(ids)):
        files.setdefault(int(r["message_id"]), {})[r["id"]] = _file_view(r)
    for m in messages:
        mid = int(m["id"]) if m.get("id") is not None else None
        if mid is None:
            continue
        m["reactions"] = [{"emoji": emoji, "count": len(agents), "agents": agents[:30], "mine": bool(viewer) and viewer in agents}
                          for emoji, agents in by_message.get(mid, {}).items()]
        row = meta.get(mid)
        if row is None:
            continue
        if row["reply_to"]:
            source = quoted.get(int(row["reply_to"]))
            m["reply"] = ({"id": int(row["reply_to"]), "author": source["author"],
                           "excerpt": " ".join(str(source["body"] or "").split())[:160]} if source is not None
                          else {"id": int(row["reply_to"]), "author": None, "excerpt": "", "gone": True})
        mine = files.get(mid, {})
        try:
            order = [f for f in json.loads(row["files"] or "[]") if isinstance(f, str)]
        except ValueError:
            order = []
        m["files"] = [mine[f] for f in order if f in mine] + [v for k, v in mine.items() if k not in order]


def prepare(self: Any, thread: Optional[int], a: dict[str, Any]) -> dict[str, Any]:
    """Check what a post carries before it is written: the quoted message must be of the same conversation and the
    files must be of this space and not yet on another message. Returns what :func:`attach` needs."""
    out: dict[str, Any] = {"reply": None, "files": []}
    if a.get("reply_to") not in (None, "", 0, "0"):
        source = _message(self, a.get("reply_to"), "reply_to")
        if thread is not None and int(source["thread_id"]) != int(thread):
            raise _err("reply_to must be a message of the same channel or thread", 409)
        out["reply"] = source
    wanted = a.get("files")
    if wanted not in (None, "", []):
        if not isinstance(wanted, list) or len(wanted) > MAX_FILES_PER_MESSAGE or not all(isinstance(f, str) for f in wanted):
            raise _err(f"files must be a list of up to {MAX_FILES_PER_MESSAGE} file ids (from file_add)")
        seen: list[str] = []
        for fid in wanted:
            fid = fid.strip().lower()
            if fid in seen:
                continue
            row = self.db.one("SELECT id, name, message_id, space_id FROM agora_files WHERE id=?", (fid,)) \
                if _FILE_ID.match(fid) else None
            if row is None or row["space_id"] != self.space_id:
                raise _err(f"no file {fid[:40]} in {self.space_id}: send it first with file_add", 404)
            if row["message_id"]:
                raise _err(f"{row['name']} is already on another message; send it again to attach it here", 409)
            seen.append(fid)
            out["files"].append({"id": fid, "name": row["name"]})
    return out


def attach(self: Any, message_id: int, prepared: dict[str, Any]) -> None:
    """Remember what the message just written carries."""
    reply, files = prepared.get("reply"), prepared.get("files") or []
    if not reply and not files:
        return
    with self.db.tx():
        self.db.execute("INSERT OR REPLACE INTO agora_message_meta(message_id, reply_to, files) VALUES(?,?,?)",
                        (int(message_id), int(reply["id"]) if reply else None, json.dumps([f["id"] for f in files])))
        for f in files:
            self.db.execute("UPDATE agora_files SET message_id=? WHERE id=? AND message_id IS NULL",
                            (int(message_id), f["id"]))


def _clean_name(value: Any) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".")
    return name[:120]


def file_add(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Send a file to this space so a message can carry it (``files`` of channel_post): ``content_base64`` with its
    ``name``. 10 MB at most. Returns its id.

    The hub never reads a path it is given: whoever sends a file reads it with its own access to the disk and sends
    the bytes (``agora.py say --attach`` does), so nothing that holds the token can make the hub hand over a file."""
    actor = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    self.space_store.get(self.space_id)
    if a.get("path"):
        raise _err("send the file's bytes as content_base64 with its name: the hub does not read paths "
                   "(from a terminal, agora.py say --attach <path> does it)", 400)
    text = str(a.get("content_base64") or "")
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise _err("content_base64 is not valid base64") from None
    name = _clean_name(a.get("name"))
    if not name:
        raise _err("name is required: the file's name")
    if not raw:
        raise _err("the file is empty")
    if len(raw) > MAX_FILE_BYTES:
        raise _err(f"the file is over {MAX_FILE_BYTES // (1024 * 1024)} MB", 413)
    ctype = str(a.get("type") or "").strip().lower() or (mimetypes.guess_type(name)[0] or "application/octet-stream")
    if not _TYPE.match(ctype):
        ctype = "application/octet-stream"
    fid = uuid.uuid4().hex
    folder = _store(self, "agora-files")
    part = folder / (fid + ".part")
    part.write_bytes(raw)
    os.replace(part, folder / fid)
    with self.db.tx():
        self._touch(actor)
        now = self.clock()
        self.db.execute("INSERT INTO agora_files(id, space_id, name, type, size, sha256, created_by, created) "
                        "VALUES(?,?,?,?,?,?,?,?)", (fid, self.space_id, name, ctype, len(raw),
                                                    hashlib.sha256(raw).hexdigest(), actor, now))
        stale = [r["id"] for r in self.db.query(
            "SELECT id FROM agora_files WHERE message_id IS NULL AND created<? LIMIT 50", (now - UNATTACHED_KEEP_S,))]
        for old in stale:
            self.db.execute("DELETE FROM agora_files WHERE id=?", (old,))
    for old in stale:
        try:
            (folder / old).unlink()
        except OSError:
            pass
    return {"ok": True, "file": {"id": fid, "name": name, "type": ctype, "size": len(raw), "inline": ctype in INLINE_TYPES}}


def file_read(self: Any, file_id: Any) -> tuple[dict[str, Any], bytes]:
    """(what the file is, its bytes), from any space; 404 when it is not there."""
    fid = str(file_id or "").strip().lower()
    row = self.db.one("SELECT * FROM agora_files WHERE id=?", (fid,)) if _FILE_ID.match(fid) else None
    if row is None:
        raise _err("no such file", 404)
    try:
        return dict(row), (_store(self, "agora-files") / fid).read_bytes()
    except OSError:
        raise _err("the file is no longer on disk", 404) from None


# ---- how a server looks ----------------------------------------------------------------------------------------

def _icon_file(self: Any) -> Optional[Path]:
    stem = re.sub(r"[^a-z0-9_.\-]", "_", str(self.space_id).lower())
    for ext in ("png", "jpg", "gif", "webp"):
        candidate = _store(self, "agora-icons") / f"{stem}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def drop_icon(self: Any) -> None:
    """Forget the picture of this space (it took another icon)."""
    while True:
        found = _icon_file(self)
        if found is None:
            return
        try:
            found.unlink()
        except OSError:
            return


def space_icon_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """A picture as the icon of this server: ``image_base64`` (PNG, JPEG, GIF or WebP, 300 KB at most; a page
    sends it already made small). The server's ``icon`` becomes ``img:<stamp>``; any other icon forgets the picture."""
    ch = _ch()
    actor = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    self.space_store.get(self.space_id)
    text = str(a.get("image_base64") or "")
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise _err("image_base64 is not valid base64") from None
    if not raw:
        raise _err("image_base64 is required: the picture")
    if len(raw) > MAX_ICON_BYTES:
        raise _err(f"the picture is over {MAX_ICON_BYTES // 1024} KB: send it smaller (128 px a side is plenty)", 413)
    ext = next((e for magic, e, _t in _ICON_MAGIC if raw.startswith(magic)), None)
    if ext is None and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = "webp"
    if ext is None:
        raise _err("the picture must be a PNG, JPEG, GIF or WebP image")
    drop_icon(self)
    stem = re.sub(r"[^a-z0-9_.\-]", "_", str(self.space_id).lower())
    target = _store(self, "agora-icons") / f"{stem}.{ext}"
    part = target.with_suffix(target.suffix + ".part")
    part.write_bytes(raw)
    os.replace(part, target)
    return ch.space_meta_set(self, {"agent": actor, "_person": bool(a.get("_person")), "icon": f"img:{int(self.clock())}"})


def icon_read(self: Any) -> tuple[bytes, str]:
    """(bytes, content type) of this space's picture; 404 when it has none."""
    found = _icon_file(self)
    if found is None:
        raise _err("this server has no picture", 404)
    kinds = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
    return found.read_bytes(), kinds[found.suffix.lstrip(".")]


def channels_reorder(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The order of the channels in one call: ``order`` lists channel names as they should read; the ones it leaves
    out follow in the order they had. ``general`` always comes first."""
    ch = _ch()
    actor = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    order = a.get("order")
    if not isinstance(order, list) or not order or len(order) > 200 or not all(isinstance(s, str) for s in order):
        raise _err("order must be a list of channel names")
    wanted: list[str] = []
    for name in order:
        slug = ch.slugify(name)
        if slug and slug != ch.GENERAL and slug not in wanted:
            wanted.append(slug)
    with self.db.tx():
        self._touch(actor)
        current = [r["slug"] for r in self.db.query(
            "SELECT slug FROM agora_channels WHERE space_id=? AND slug<>? ORDER BY position, id",
            (self.space_id, ch.GENERAL))]
        unknown = [s for s in wanted if s not in current]
        if unknown:
            raise _err(f"no channel #{unknown[0]} in {self.space_id}", 404)
        final = wanted + [s for s in current if s not in wanted]
        now = self.clock()
        for index, slug in enumerate(final, start=1):
            self.db.execute("UPDATE agora_channels SET position=?, updated=? WHERE space_id=? AND slug=?",
                            (index, now, self.space_id, slug))
    self._bump("agora.channel.reordered", {"by": actor})
    return {"ok": True, "order": [ch.GENERAL, *final]}


OPERATIONS = {"members": members, "member_add": member_add, "member_remove": member_remove,
              "members_prune": members_prune, "react": react, "file_add": file_add,
              "space_icon_set": space_icon_set, "channels_reorder": channels_reorder}
WRITE_OPS = ("member_add", "member_remove", "members_prune", "react", "file_add", "space_icon_set", "channels_reorder")
READ_ROUTES = ("members",)
#: POST routes that take more than the hub's usual small body (a file, a picture), with their limits in bytes.
BIG_BODIES = {"/api/agora/file_add": 15 * 1024 * 1024, "/api/agora/space_icon_set": 1024 * 1024}


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    member = {"type": "string", "description": "The agent's id, such as codex-docs."}
    return [
        {"name": "hub_agora_members", "read": True,
         "description": "Everyone on a server, newest sign of life first, also those long gone, with presence "
                        "(working, waiting, idle, away, invited, offline, no_quota), provider, open tasks and leases. "
                        "q filters by name.",
         "inputSchema": schema({"q": {"type": "string", "maxLength": 80}}, [])},
        {"name": "hub_agora_member_add",
         "description": "Put an agent on a server by name before it has said anything there, so it can be mentioned "
                        "and given work; it shows as invited until its first sign of life. The provider is read from "
                        "the name (codex-…, claude-…, cursor-…, faustus-…).",
         "inputSchema": schema({"agent": agent_prop, "id": member, "note": {"type": "string", "maxLength": 500}},
                               ["agent", "id"])},
        {"name": "hub_agora_member_remove",
         "description": "Take an agent off a server: the work it held there goes to to_agent, or back to open, its "
                        "leases on it are dropped and its reviews wait for someone else. The agent itself may always "
                        "leave; another agent may remove it after 30 min of silence, or at once with force. If it "
                        "shows up again it joins again.",
         "inputSchema": schema({"agent": agent_prop, "id": member, "to_agent": {"type": "string"},
                                "reason": {"type": "string", "maxLength": 2000}, "force": {"type": "boolean"}},
                               ["agent", "id"])},
        {"name": "hub_agora_members_prune",
         "description": "Take off a server every agent silent for older_than_days (30) that holds neither work nor "
                        "leases there; dry_run only lists who would go. Those holding something are kept and named.",
         "inputSchema": schema({"agent": agent_prop, "older_than_days": {"type": "integer", "minimum": 1, "maximum": 3650},
                                "dry_run": {"type": "boolean"}}, ["agent"])},
        {"name": "hub_agora_react",
         "description": "React to a message with an emoji or a short word (👍, ✅, visto), or take the reaction back: "
                        "on true/false; without it the reaction is toggled.",
         "inputSchema": schema({"agent": agent_prop, "message_id": {"type": "integer", "minimum": 1},
                                "emoji": {"type": "string", "minLength": 1, "maxLength": 16},
                                "on": {"type": "boolean"}}, ["agent", "message_id", "emoji"])},
        {"name": "hub_agora_file_add",
         "description": "Send a file to a space so a message can carry it: content_base64 with its name; 10 MB at "
                        "most. Pass the returned id in files of hub_agora_channel_post. GET /api/agora/file/<id> "
                        "gives it back. For a file on disk, agora.py say --attach <path> reads and sends it.",
         "inputSchema": schema({"agent": agent_prop, "name": {"type": "string", "maxLength": 120},
                                "content_base64": {"type": "string"}, "type": {"type": "string", "maxLength": 140}},
                               ["agent", "name", "content_base64"])},
        {"name": "hub_agora_space_icon_set",
         "description": "A picture as the icon of a server: image_base64 (PNG, JPEG, GIF or WebP, 300 KB at most).",
         "inputSchema": schema({"agent": agent_prop, "image_base64": {"type": "string"}}, ["agent", "image_base64"])},
        {"name": "hub_agora_channels_reorder",
         "description": "The order of a server's channels in one call: order lists channel names as they should "
                        "read; the rest follow as they were. general always comes first.",
         "inputSchema": schema({"agent": agent_prop, "order": {"type": "array", "items": {"type": "string"}}},
                               ["agent", "order"])},
    ]
