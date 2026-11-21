"""Append-only edits to Agora thread titles and recorded resolutions."""

from __future__ import annotations

import secrets

from . import agora_ranks


MIGRATION = """
CREATE TABLE agora_decision_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    field TEXT NOT NULL CHECK (field IN ('title', 'resolution')),
    action TEXT NOT NULL CHECK (action IN ('retitle', 'amend', 'withdraw', 'undo')),
    author TEXT NOT NULL,
    created REAL NOT NULL,
    before_text TEXT,
    after_text TEXT,
    resolution_id INTEGER REFERENCES messages(id),
    undo_of INTEGER UNIQUE REFERENCES agora_decision_history(id),
    undo_token TEXT UNIQUE,
    message_id INTEGER NOT NULL REFERENCES messages(id)
);
CREATE INDEX agora_decision_history_thread ON agora_decision_history(thread_id, field, id);
"""

WRITES = {
    "thread_retitle": ("title", "retitle", "Retitle a thread and return an undo token.", {"title": {"type": "string"}}),
    "decision_amend": ("resolution", "amend", "Amend a recorded resolution and return an undo token.",
                       {"resolution": {"type": "string"}}),
    "decision_withdraw": ("resolution", "withdraw", "Withdraw a recorded resolution and return an undo token.", {}),
}
WRITE_OPS = (*WRITES, "decision_history_undo")
PUBLIC_FIELDS = "id,thread_id,field,action,author,created,before_text,after_text,undo_of,message_id"

AUDIT_LABELS = {
    "retitle": ("Título actualizado.", "Título anterior"),
    "amend": ("Decisión enmendada.", "Texto anterior"),
    "withdraw": ("Decisión retirada.", "Texto anterior"),
    "undo": ("Cambio deshecho.", "Valor anterior"),
}


def tools(schema, agent_prop, thread_prop):
    base = {"agent": agent_prop, "thread_id": thread_prop}
    return [
        {"name": f"hub_agora_{op}", "description": description,
         "inputSchema": schema({**base, **props}, [*base, *props])}
        for op, (_, _, description, props) in WRITES.items()
    ] + [
        {"name": "hub_agora_decision_history_undo",
         "description": "Undo a retitle, amendment or withdrawal using its token. The history remains.",
         "inputSchema": schema({**base, "undo_token": {"type": "string"}}, [*base, "undo_token"])}
    ]


def thread_state(thread):
    thread["withdrawn"] = bool(
        thread["kind"] != "task" and thread["status"] == "resolved"
        and thread["resolved_by"] and thread["resolution"] is None
    )
    return thread


def histories(store, thread_ids, *, decisions_only=False):
    if not thread_ids:
        return {}
    marks = ",".join("?" for _ in thread_ids)
    fields = " AND field='resolution'" if decisions_only else ""
    rows = store.db.query(
        f"SELECT {PUBLIC_FIELDS} FROM agora_decision_history WHERE thread_id IN ({marks}){fields} ORDER BY id",
        tuple(thread_ids),
    )
    entries = [dict(row) for row in rows]
    if decisions_only:
        entries.extend({
            "id": row["id"], "thread_id": row["thread_id"], "field": "resolution", "action": "recorded",
            "author": row["author"], "created": row["created"], "before_text": None, "after_text": row["body"],
            "undo_of": None, "message_id": row["id"],
        } for row in store.db.query(
            f"SELECT id,thread_id,author,created,body FROM messages "
            f"WHERE thread_id IN ({marks}) AND kind='resolution' ORDER BY id", tuple(thread_ids)
        ))
    by_thread = {}
    for entry in sorted(entries, key=lambda entry: entry["message_id"]):
        by_thread.setdefault(entry["thread_id"], []).append(entry)
    return by_thread


def _check_permission(store, thread, field, agent):
    from .agora import AgoraError

    author = thread["created_by"] if field == "title" else thread["resolved_by"]
    if agent not in (author, store.person_id) and agora_ranks.rank_of(store, agent) != "director":
        raise AgoraError("Only the original author, a director or the person can make this change.", 403)


def _resolution_id(store, thread_id):
    return store.db.scalar("SELECT MAX(id) FROM messages WHERE thread_id=? AND kind='resolution'", (thread_id,))


def _record(store, thread, field, action, agent, value, resolution_id=None, undo_of=None):
    old = thread[field]
    token = secrets.token_urlsafe(24) if undo_of is None else None
    label, old_label = AUDIT_LABELS[action]
    body = f"{label}\n\n{old_label}:\n{old or '—'}"
    store._touch(agent)
    message_id = store._msg(thread["id"], agent, "system", body)
    now = store.clock()
    store.db.execute(f"UPDATE threads SET {field}=?,updated=? WHERE id=?", (value, now, thread["id"]))
    revision = store.db.insert("agora_decision_history", {
        "thread_id": thread["id"], "field": field, "action": action, "author": agent, "created": now,
        "before_text": old, "after_text": value, "resolution_id": resolution_id, "undo_of": undo_of,
        "undo_token": token, "message_id": message_id,
    })
    event = dict(store.db.one(f"SELECT {PUBLIC_FIELDS} FROM agora_decision_history WHERE id=?", (revision,)))
    result = {"ok": True, "thread": store._thread_row(thread["id"]), "change": event}
    if token:
        result["undo_token"] = token
    return result


def edit(store, args, op):
    from .agora import AgoraError, _txt

    agent = store._agent(args.get("agent"), allow_person=bool(args.get("_person")))
    field, action, _, _ = WRITES[op]
    value = None if action == "withdraw" else _txt(
        args.get(field), 200 if field == "title" else 20_000, field, required=True)
    with store.db.tx():
        thread = store._thread_row(args.get("thread_id"))
        _check_permission(store, thread, field, agent)
        resolution_id = None
        if field == "resolution":
            if thread["kind"] == "task" or thread["status"] != "resolved" or not thread["resolution"]:
                raise AgoraError("This thread has no current recorded decision.", 409)
            resolution_id = _resolution_id(store, thread["id"])
        if value == thread[field]:
            return {"ok": True, "thread": thread, "change": None}
        result = _record(store, thread, field, action, agent, value, resolution_id)
    store._bump(f"agora.{op}", {"thread_id": thread["id"], "by": agent})
    return result


def undo(store, args):
    from .agora import AgoraError

    agent = store._agent(args.get("agent"), allow_person=bool(args.get("_person")))
    token = args.get("undo_token")
    if not isinstance(token, str) or not token.strip():
        raise AgoraError("undo_token is required")
    with store.db.tx():
        thread = store._thread_row(args.get("thread_id"))
        target = store.db.one("SELECT * FROM agora_decision_history WHERE thread_id=? AND undo_token=?",
                              (thread["id"], token))
        if target is None:
            raise AgoraError("No change matches that undo token in this thread.", 404)
        field = target["field"]
        _check_permission(store, thread, field, agent)
        if store.db.one("SELECT 1 FROM agora_decision_history WHERE undo_of=?", (target["id"],)):
            raise AgoraError("This change has already been undone.", 409)
        if field == "resolution" and (thread["status"] != "resolved"
                                      or _resolution_id(store, thread["id"]) != target["resolution_id"]):
            raise AgoraError("The thread's recorded decision has changed since this edit.", 409)
        latest = store.db.scalar(
            "SELECT MAX(edit.id) FROM agora_decision_history edit "
            "WHERE edit.thread_id=? AND edit.field=? AND edit.resolution_id IS ? AND edit.undo_of IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM agora_decision_history reversal WHERE reversal.undo_of=edit.id)",
            (thread["id"], field, target["resolution_id"]),
        )
        if latest != target["id"] or thread[field] != target["after_text"]:
            raise AgoraError("Undo the newer change first.", 409)
        result = _record(store, thread, field, "undo", agent, target["before_text"],
                         target["resolution_id"], target["id"])
    store._bump("agora.decision_history_undo", {"thread_id": thread["id"], "by": agent})
    return result
