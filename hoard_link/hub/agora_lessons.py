"""Shared studio lessons, with compatibility for the original apprentice files."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .agora_faustus import APPRENTICE

CORRECTION_LIMIT = 300
TRIGGER_LIMIT = 200
ROUTE = "/api/agora/studio_lessons"
_LOCK = RLock()
_FIELDS = ("author", "message_id", "thread_id", "trigger", "action", "created")


def _folders(data_dir):
    root = Path(data_dir) / "agora-console"
    return root / "studio-lessons", root / "faustus" / APPRENTICE / "instincts" / "agora-tutor"


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _lesson(record, message_id):
    if (isinstance(record, dict) and record.get("message_id") == message_id
            and all(key in record for key in _FIELDS)):
        return {key: record[key] for key in _FIELDS}
    return None


def _state(data_dir, message_id):
    shared, legacy = _folders(data_dir)
    name = f"{message_id}.json"
    state = _read(shared / name)
    if state is not None:
        return state
    return {"lesson": _lesson(_read(legacy / name), message_id)}


def _write(data_dir, message_id, lesson, *, previous=None, reversible=False):
    shared, _ = _folders(data_dir)
    shared.mkdir(parents=True, exist_ok=True)
    state = {"lesson": lesson}
    token = None
    if reversible:
        revision = uuid4().hex
        token = f"{message_id}.{revision}"
        state.update(revision=revision, previous=previous)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=shared, suffix=".tmp",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(state, stream, ensure_ascii=False)
            stream.close()
            os.replace(temporary, shared / f"{message_id}.json")
        finally:
            temporary.unlink(missing_ok=True)
    return token


def list_lessons(data_dir):
    with _LOCK:
        ids = {int(path.stem) for folder in _folders(data_dir) for path in folder.glob("*.json")
               if re.fullmatch(r"[1-9][0-9]*", path.stem)}
        lessons = []
        for message_id in sorted(ids, reverse=True):
            try:
                lesson = _lesson(_state(data_dir, message_id).get("lesson"), message_id)
            except (OSError, ValueError):
                continue
            if lesson:
                lessons.append(lesson)
        return {"ok": True, "lessons": lessons, "correction_limit": CORRECTION_LIMIT}


def change(data_dir, message_id, lesson):
    with _LOCK:
        state = _state(data_dir, message_id)
        old = state.get("lesson")
        if old == lesson or (old and lesson and old["trigger"] == lesson["trigger"]
                             and old["action"] == lesson["action"]):
            revision = state.get("revision")
            return f"{message_id}.{revision}" if revision else None
        return _write(data_dir, message_id, lesson, previous=old, reversible=True)


def undo(data_dir, token, *, author=None):
    match = re.fullmatch(r"([1-9][0-9]{0,18})\.([0-9a-f]{32})", token or "") if isinstance(token, str) else None
    if not match:
        return {"ok": False, "status": 400, "error": "Invalid lesson undo token."}
    message_id = int(match[1])
    with _LOCK:
        state = _state(data_dir, message_id)
        if state.get("revision") != match[2]:
            return {"ok": False, "status": 409, "error": "The lesson has changed since this action."}
        record = state.get("lesson") or state["previous"]
        if author is not None and record["author"] != author:
            return {"ok": False, "status": 403, "error": "This lesson belongs to another teacher."}
        _write(data_dir, message_id, state["previous"])
    return {"ok": True, "status": "undone", "message_id": message_id}
