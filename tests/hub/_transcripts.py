"""Synthetic transcripts of the three coding-agent engines for the observer's tests: builders for the Codex, Cursor and
Claude Code line formats, a writer that sets file mtimes, an injectable clock and the shared fixtures."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hoard_link.hub.agent_watch import AgentWatch

NOW = 1_800_000_000.0
CODEX_ID = "019d1111-2222-7333-8444-555566667777"
CLAUDE_ID = "7c1e2d3f-aaaa-4bbb-8ccc-0123456789ab"


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def put(path: Path, lines: list, mtime: float | None = None, *, append: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join((ln if isinstance(ln, str) else json.dumps(ln)) + "\n" for ln in lines)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(body)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---- builders for the three formats ----------------------------------------------------------------------------

def cx(t: float, kind: str, payload: dict) -> dict:
    return {"timestamp": iso(t), "ordinal": 1, "type": kind, "payload": payload}


def cx_msg(t: float, role: str, text: str) -> dict:
    ptype = "output_text" if role == "assistant" else "input_text"
    return cx(t, "response_item", {"type": "message", "role": role, "content": [{"type": ptype, "text": text}]})


def cx_head(t: float, cwd: str = "C:\\Users\\luism\\Desktop\\Faustus", prompt: str = "Arregla el bucle del agente") -> list:
    return [cx(t, "session_meta", {"session_id": CODEX_ID, "id": CODEX_ID, "timestamp": iso(t), "cwd": cwd,
                                    "originator": "codex_work_desktop", "cli_version": "0.1"}),
            cx(t, "response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]}),
            cx_msg(t, "user", "<environment_context>\n<cwd>x</cwd>\n</environment_context>"),
            cx_msg(t, "user", prompt)]


def cx_start(t: float, turn: str = "t1") -> dict:
    return cx(t, "event_msg", {"type": "task_started", "turn_id": turn, "started_at": t})


def cx_done(t: float, turn: str = "t1", text: str = "Hecho.") -> dict:
    return cx(t, "event_msg", {"type": "task_complete", "turn_id": turn, "last_agent_message": text, "completed_at": t,
                               "duration_ms": 1000})


def cx_exec(t: float, call: str, cmd: str, name: str = "exec") -> dict:
    return cx(t, "response_item", {"type": "custom_tool_call", "name": name, "input": cmd, "status": "completed", "call_id": call})


def cx_out(t: float, call: str) -> dict:
    return cx(t, "response_item", {"type": "custom_tool_call_output", "call_id": call, "output": "ok"})


def codex_file(root: Path, lines: list, mtime: float, uuid: str = CODEX_ID) -> Path:
    return put(root / "2026" / "10" / "08" / f"rollout-2026-10-08T10-00-00-{uuid}.jsonl", lines, mtime)


def cur_user(text: str) -> dict:
    return {"role": "user", "message": {"content": [{"type": "text", "text": f"<user_query>\n{text}\n</user_query>"}]}}


def cur_text(text: str) -> dict:
    return {"role": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def cur_tool(name: str = "Shell", **inp) -> dict:
    return {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


CUR_END = {"type": "turn_ended", "status": "success"}


def cursor_file(root: Path, lines: list, mtime: float, cid: str = "abc-123") -> Path:
    return put(root / "c-Users-luism-Desktop-Faustus" / "agent-transcripts" / cid / f"{cid}.jsonl", lines, mtime)


def cl_user(t: float, text: str) -> dict:
    return {"type": "user", "timestamp": iso(t), "sessionId": CLAUDE_ID, "cwd": "/home/claude/w/HoardLink",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def cl_asst(t: float, *parts: dict, stop: str | None = None) -> dict:
    return {"type": "assistant", "timestamp": iso(t), "sessionId": CLAUDE_ID,
            "message": {"role": "assistant", "content": list(parts), "stop_reason": stop}}


def cl_text(text: str) -> dict:
    return {"type": "text", "text": text}


def cl_use(call: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": call, "name": name, "input": inp}


def cl_result(t: float, call: str) -> dict:
    return {"type": "user", "timestamp": iso(t), "sessionId": CLAUDE_ID,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": "ok"}]}}


def claude_file(root: Path, lines: list, mtime: float, sid: str = CLAUDE_ID) -> Path:
    return put(root / "-home-claude-w-HoardLink" / f"{sid}.jsonl", lines, mtime)


@pytest.fixture
def roots(tmp_path):
    return {e: tmp_path / "t" / e for e in ("codex", "cursor", "claude")}


@pytest.fixture
def clock():
    return Clock()


def watch(roots, clock, tmp_path=None, **kw) -> AgentWatch:
    return AgentWatch(roots=roots, clock=clock, store_path=(tmp_path / "agent_watch.json") if tmp_path else None, **kw)


def one(w: AgentWatch, **kw) -> dict:
    v = w.view(force=True, **kw)
    assert len(v["sessions"]) == 1, v["sessions"]
    return v["sessions"][0]
