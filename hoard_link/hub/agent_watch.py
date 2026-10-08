"""Passive observer of the coding agents: reads each agent's own transcript files on this PC and derives what it is
really doing, so the Ágora can tell «working» from «waiting for an approval» from «dead».

The Ágora is cooperative: an agent shows up as «sin señales» when it stops sending heartbeats, and nobody knows
whether it is busy, blocked on a question for the person, or gone. The transcripts every agent already writes know:

* **Codex** — ``~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl``. ``{"timestamp", "type", "payload"}``
  lines; a thread can live for weeks in one file, so only the TAIL is read.
* **Cursor** — ``~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl``. ``{"role", "message"}`` lines and
  ``{"type": "turn_ended"}``; there are no timestamps, the file's mtime is the activity clock.
* **Claude Code** — ``~/.claude/projects/<slug>/<uuid>.jsonl``. ``{"type", "timestamp", "message"}`` lines.

Per session the observer keeps a small state machine (turn open/closed, tools without a result, pending questions)
fed incrementally by byte offset: a file is never loaded whole (first sight reads the last ``TAIL_BYTES``; the first
``HEAD_BYTES`` are read once for the title). The derived state is one of ``working``, ``tool``, ``waiting``,
``idle`` or ``stale`` (see :func:`derive_state`). A session is bound to an Ágora agent id explicitly (the person, or
a token holder, through ``bind``) or by inference: its own calls to ``agora.py --as <id>`` / ``hub_agora_*`` tools.

Privacy: the view only carries snippets of at most ``SNIPPET`` characters; reasoning / thinking / encrypted
content is never read into a snippet, and lines that look like secrets (``Authorization:``, ``password=``, key
prefixes…) are dropped from every snippet.

Standard library only. No threads: :meth:`AgentWatch.refresh` does the work when a read asks for it, at most every
``REFRESH_S`` seconds.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ENGINES = ("codex", "cursor", "claude")
#: Longest piece of a transcript that ever reaches the view (and so the page, the CLI and the tool answers).
SNIPPET = 200
TITLE_MAX = 120
#: Bytes read from the end of a file the first time it is seen, and from its start for the title.
TAIL_BYTES = 256 * 1024
HEAD_BYTES = 64 * 1024
#: More than this appended between two refreshes: skip ahead to the last ``TAIL_BYTES`` instead of reading it all.
MAX_CATCHUP = 8 * 1024 * 1024
#: A single line bigger than this is skipped unparsed (an inlined screenshot, a huge tool output).
MAX_LINE = 4 * 1024 * 1024
REFRESH_S = 5.0
DISCOVER_S = 30.0
DEFAULT_HOURS = 48.0
DEFAULT_MAX_FILES = 200
#: A tool call without its result for longer than this is «probably waiting for a permission» (Claude Code only).
TOOL_WAIT_S = 60.0
#: A turn left open with no write for longer than this is «stale» (the process is probably gone).
STALE_S = 30 * 60.0
#: With no turn marker in the tail at all, a write this recent means «working».
UNKNOWN_TURN_RECENT_S = 120.0
MAX_OPEN_TOOLS = 50
MAX_SESSIONS_OUT = 200
MAX_UNBOUND_OUT = 100

STATES = ("working", "tool", "waiting", "idle", "stale")
#: Which of a person's sessions to show for one agent when it has several: the one that needs attention first.
_RANK = {"waiting": 5, "tool": 4, "working": 3, "stale": 2, "idle": 1}

_AGENT_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,40}$")
_PERSON = "luis"
_UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")

# ---- privacy ---------------------------------------------------------------------------------------------------

_SECRET_RES = [re.compile(p, re.I) for p in (
    r"\bproxy-authorization\s*[:=]",
    r"\bauthorization\s*[:=]",
    r"\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}",
    r"\b(?:pass(?:word|wd)?|passphrase|secret|client[_-]?secret|credentials?|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|(?:access|auth|refresh|bearer|api|session|id|github|gh|hf|npm)?[_-]?token)\b[\"']?\s*[:=]",
    r"--(?:pass(?:word|wd)?|token|secret|api[_-]?key|auth)\b",
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}",
    r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
    r"\bAKIA[0-9A-Z]{12,}",
    r"\bAIza[0-9A-Za-z_\-]{20,}",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY",
)]


def snippet(text: Any, limit: int = SNIPPET) -> str:
    """One short, single-spaced piece of ``text`` without any line that looks like a secret."""
    if not isinstance(text, str):
        return ""
    kept = [ln for ln in text.splitlines() if ln.strip() and not any(rx.search(ln) for rx in _SECRET_RES)]
    out = " ".join(" ".join(kept).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


# ---- small helpers ---------------------------------------------------------------------------------------------

class WatchError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_ts(value: Any) -> Optional[float]:
    """An ISO timestamp (``…Z`` or with an offset) or an epoch number as epoch seconds; ``None`` when it is neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str) and value:
        try:
            d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    return None


def _first_text(content: Any) -> str:
    """The text of a message ``content`` (a string, or a list of ``{type, text}`` parts); tool results excluded."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(parts)


_BOILERPLATE = ("<environment_context", "<user_instructions", "<command-", "<local-command", "<system-reminder",
                "<ide_", "<INSTRUCTIONS", "# AGENTS.md", "Caveat:", "<user_query", "<permissions", "<collaboration_mode")


def _is_boilerplate(text: str) -> bool:
    t = text.lstrip()
    if t.startswith("<user_query>"):
        return False
    return any(t.startswith(b) for b in _BOILERPLATE)


def _title_from(text: str) -> str:
    t = text.strip()
    m = re.match(r"^<user_query>\s*(.*?)\s*(?:</user_query>|$)", t, re.S)
    if m:
        t = m.group(1)
    return snippet(t, TITLE_MAX)


def _input_summary(value: Any, _depth: int = 0) -> str:
    """A snippet of what a tool call does: its command / path / query, whichever is there (never the whole input)."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[":
            try:
                value = json.loads(s)
            except ValueError:
                return snippet(s)
        else:
            return snippet(s)
    if isinstance(value, list):
        if value and all(isinstance(x, str) for x in value):
            return snippet(" ".join(value))
        for x in value[:3]:
            got = _input_summary(x, _depth + 1) if _depth < 2 and isinstance(x, dict) else ""
            if got:
                return got
        return ""
    if isinstance(value, dict):
        for key in ("command", "cmd", "question", "query", "file_path", "path", "pattern", "url", "description", "prompt",
                    "input", "text", "message", "title", "name"):
            v = value.get(key)
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                v = " ".join(v)
            if isinstance(v, str) and v.strip():
                got = snippet(v)
                if got:
                    return got
        if _depth < 2:
            for v in value.values():
                if isinstance(v, (list, dict)):
                    got = _input_summary(v, _depth + 1)
                    if got:
                        return got
        for v in value.values():
            if isinstance(v, str) and v.strip():
                got = snippet(v)
                if got:
                    return got
    return ""


_ID = r"([a-z][a-z0-9_.\-]{1,40})"
_AS_RE = re.compile(r"--as[ =]+[\"']?" + _ID + r"(?![A-Za-z0-9_.\-])")
_ENV_RE = re.compile(r"AGORA_AGENT\s*[=:]\s*[\"']?" + _ID + r"(?![A-Za-z0-9_.\-])")
_JSON_AGENT_RE = re.compile(r"(?<![A-Za-z0-9_])[\"']?agent[\"']?\s*[:=]\s*[\"']" + _ID + r"[\"']")


def agents_in_call(name: str, args: Any) -> list[str]:
    """Ágora agent ids a tool call speaks as: ``--as <id>`` / ``AGORA_AGENT=<id>`` of an ``agora`` CLI call, or the
    ``agent`` argument of a ``hub_agora_*`` tool / ``/api/agora/`` request."""
    if not isinstance(args, str):
        try:
            args = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            return []
    text = f"{name or ''} {args}"
    low = text.lower()
    if "agora" not in low:
        return []
    found: list[str] = []
    found += _AS_RE.findall(text)
    found += _ENV_RE.findall(text)
    if "hub_agora_" in low or "/api/agora" in low:
        # arguments may be a JSON string inside a JSON string: {\"agent\": \"codex-sparks\"}
        found += _JSON_AGENT_RE.findall(text.replace('\\"', '"'))
    return [a for a in found if _AGENT_RE.match(a) and a != _PERSON]


# ---- per-session state -----------------------------------------------------------------------------------------

class Session:
    """What is known about one transcript; fed line by line, never holds the file."""

    def __init__(self, engine: str, session_id: str, path: str):
        self.engine = engine
        self.session_id = session_id
        self.path = path
        self.cwd = ""
        self.workspace = ""
        self.title = ""
        self.originator = ""
        self.model = ""
        self.started: Optional[float] = None
        self.mtime = 0.0
        self.last_ts: Optional[float] = None       # last line timestamp (None for engines without them)
        self.turn_open: Optional[bool] = None      # None = the tail holds no turn marker
        self.turn_started: Optional[float] = None
        self.turn_ended: Optional[float] = None
        self.open_tools: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.pending: "OrderedDict[str, dict[str, Any]]" = OrderedDict()   # explicit questions / approvals
        self.last_tool = ""
        self.last_text = ""
        self.tokens: Optional[int] = None
        self.token_base: Optional[int] = None
        self.token_total: Optional[int] = None
        self.hints: dict[str, list[float]] = {}    # agent id -> [count, last seq]
        self.seq = 0
        self.lines = 0
        self._real = False                         # does the line being fed carry its own timestamp?

    @property
    def key(self) -> str:
        return f"{self.engine}:{self.session_id}"

    # -- feeding ----------------------------------------------------------------------------------------------

    def hint(self, agents: Iterable[str]) -> None:
        for a in agents:
            self.seq += 1
            rec = self.hints.setdefault(a, [0, 0])
            rec[0] += 1
            rec[1] = self.seq

    def _touch(self, ts: float) -> None:
        if self._real and (self.last_ts is None or ts > self.last_ts):
            self.last_ts = ts

    def _tool_start(self, call_id: str, name: str, args: Any, ts: float, ns: str = "") -> None:
        if not call_id:
            self.seq += 1
            call_id = f"anon-{self.seq}"
        self.open_tools.pop(call_id, None)
        self.open_tools[call_id] = {"name": name or "tool", "since": ts, "input": _input_summary(args)}
        while len(self.open_tools) > MAX_OPEN_TOOLS:
            self.open_tools.popitem(last=False)
        self.last_tool = name or self.last_tool
        self.hint(agents_in_call(f"{ns}{name}", args))
        low = (name or "").lower()
        if any(k in low for k in _QUESTION_KEYS) or low in _ASK_TOOLS:
            self.pending[call_id] = {"kind": "question", "what": _input_summary(args) or name, "since": ts,
                                     "tool": name, "confidence": "explicit", "call_id": call_id}

    def _tool_end(self, call_id: str) -> None:
        self.open_tools.pop(call_id, None)
        self.pending.pop(call_id, None)

    def _turn_start(self, ts: float) -> None:
        self.turn_open = True
        self.turn_started = ts
        self.turn_ended = None
        self.open_tools.clear()
        self.pending.clear()
        self.tokens = None
        self.token_base = self.token_total

    def _turn_end(self, ts: float) -> None:
        self.turn_open = False
        self.turn_ended = ts
        self.open_tools.clear()
        self.pending.clear()

    def meta(self, engine_obj: dict[str, Any], ts: float) -> None:
        """Title / cwd / start from a head or tail line (state untouched)."""
        getattr(self, f"_meta_{self.engine}")(engine_obj, ts)

    def feed(self, obj: dict[str, Any], ts: float, real: bool = True) -> None:
        self.lines += 1
        self._real = real
        getattr(self, f"_feed_{self.engine}")(obj, ts)

    def _set_title(self, text: str) -> None:
        if not self.title and text and not _is_boilerplate(text):
            self.title = _title_from(text)

    def _set_cwd(self, cwd: Any) -> None:
        if isinstance(cwd, str) and cwd and not self.cwd:
            self.cwd = cwd
            self.workspace = re.split(r"[\\/]+", cwd.rstrip("\\/"))[-1] or cwd

    # -- Codex ------------------------------------------------------------------------------------------------

    def _meta_codex(self, obj: dict[str, Any], ts: float) -> None:
        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        t = obj.get("type")
        if t == "session_meta":
            self._set_cwd(p.get("cwd"))
            self.originator = self.originator or str(p.get("originator") or "")
            self.started = self.started or parse_ts(p.get("timestamp")) or ts
        elif t == "turn_context":
            self._set_cwd(p.get("cwd"))
            m = p.get("model")
            if isinstance(m, str):
                self.model = m
        elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
            self._set_title(_first_text(p.get("content")))
        elif t == "event_msg" and p.get("type") == "user_message" and isinstance(p.get("message"), str):
            self._set_title(p["message"])

    def _feed_codex(self, obj: dict[str, Any], ts: float) -> None:
        t = obj.get("type")
        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        pt = str(p.get("type") or "")
        self._meta_codex(obj, ts)
        if t in ("session_meta", "turn_context", "world_state", "compacted", "token_usage_record",
                 "inter_agent_communication_metadata"):
            return
        self._touch(ts)
        if t == "event_msg":
            low = pt.lower()
            if pt == "task_started":
                self._turn_start(parse_ts(p.get("started_at")) or ts)
            elif pt == "task_complete":
                self._turn_end(parse_ts(p.get("completed_at")) or ts)
                msg = p.get("last_agent_message")
                if isinstance(msg, str) and snippet(msg):
                    self.last_text = snippet(msg)
            elif pt in ("turn_aborted", "task_aborted", "task_failed"):
                self._turn_end(ts)
            elif pt == "token_count":
                info = p.get("info") if isinstance(p.get("info"), dict) else {}
                last = (info.get("last_token_usage") or {}).get("total_tokens") if isinstance(info.get("last_token_usage"), dict) else None
                total = (info.get("total_token_usage") or {}).get("total_tokens") if isinstance(info.get("total_token_usage"), dict) else None
                if isinstance(total, int):
                    self.token_total = total
                if isinstance(total, int) and isinstance(self.token_base, int) and total >= self.token_base:
                    self.tokens = total - self.token_base
                elif isinstance(last, int):
                    self.tokens = last
            elif pt == "agent_message" and isinstance(p.get("message"), str):
                self.last_text = snippet(p["message"]) or self.last_text
            elif any(k in low for k in _QUESTION_KEYS):
                self._codex_question(pt, p, ts)
        elif t == "response_item":
            if pt == "message":
                if p.get("role") == "assistant":
                    txt = snippet(_first_text(p.get("content")))
                    if txt:
                        self.last_text = txt
            elif pt == "agent_message" and isinstance(p.get("message"), str):
                self.last_text = snippet(p["message"]) or self.last_text
            elif pt == "reasoning":
                return
            elif pt.endswith("_call_output") or pt.endswith("_call_result"):
                self._tool_end(str(p.get("call_id") or p.get("id") or ""))
                self._codex_resumed()
            elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
                name = str(p.get("name") or pt[: -len("_call")] or "tool")
                args = p.get("arguments") if "arguments" in p else p.get("input", p.get("action"))
                ns = p.get("namespace")
                self._tool_start(str(p.get("call_id") or p.get("id") or ""), name, args, ts, ns if isinstance(ns, str) else "")
            elif pt.endswith("_call"):
                self.last_tool = str(p.get("name") or pt[: -len("_call")])    # web search & co.: no result item follows
            elif any(k in pt.lower() for k in _QUESTION_KEYS):
                self._codex_question(pt, p, ts)

    def _codex_resumed(self) -> None:
        """The agent kept going: an approval event asked before is no longer pending."""
        for k in [k for k, v in self.pending.items() if v.get("event")]:
            self.pending.pop(k, None)

    def _codex_question(self, pt: str, p: dict[str, Any], ts: float) -> None:
        low = pt.lower()
        cid = str(p.get("call_id") or p.get("id") or p.get("approval_id") or p.get("request_id") or pt)
        if any(w in low for w in _RESOLVED_WORDS):
            self.pending.pop(cid, None)
            for k in [k for k, v in self.pending.items() if v.get("event") == pt.rsplit("_", 1)[0]]:
                self.pending.pop(k, None)
            return
        what = ""
        for key in ("question", "command", "message", "prompt", "title", "text", "reason"):
            if isinstance(p.get(key), (str, list)):
                what = _input_summary({key: p[key]})
                if what:
                    break
        if not what:
            what = _input_summary(p) or pt
        kind = "approval" if "approval" in low else "question"
        self.pending[cid] = {"kind": kind, "what": what, "since": ts, "tool": pt, "confidence": "explicit",
                             "call_id": cid, "event": pt.rsplit("_", 1)[0]}

    # -- Cursor -----------------------------------------------------------------------------------------------

    def _meta_cursor(self, obj: dict[str, Any], ts: float) -> None:
        if obj.get("role") == "user":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            self._set_title(_first_text(msg.get("content")))

    def _feed_cursor(self, obj: dict[str, Any], ts: float) -> None:
        self._touch(ts)
        if obj.get("type") == "turn_ended":
            self._turn_end(ts)
            return
        role = obj.get("role")
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")
        if role == "user":
            text = _first_text(content)
            if text and not _is_boilerplate(text):
                self._set_title(text)
            self._turn_start(ts)
        elif role == "assistant":
            if not self.turn_open:
                self.turn_open = True
                self.turn_started = ts
                self.turn_ended = None
            # a new assistant line means the previous tool (if any) returned: Cursor logs no tool results
            self.open_tools.clear()
            self.pending.clear()
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        got = snippet(part["text"])
                        if got:
                            self.last_text = got
                    elif part.get("type") == "tool_use":
                        self._tool_start(str(part.get("id") or ""), str(part.get("name") or "tool"), part.get("input"), ts)
            elif isinstance(content, str):
                self.last_text = snippet(content) or self.last_text

    # -- Claude Code ------------------------------------------------------------------------------------------

    def _meta_claude(self, obj: dict[str, Any], ts: float) -> None:
        self._set_cwd(obj.get("cwd"))
        if obj.get("type") == "user" and not obj.get("isMeta") and not obj.get("isSidechain"):
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            text = _first_text(msg.get("content"))
            if text:
                self._set_title(text)
        m = (obj.get("message") or {}).get("model") if isinstance(obj.get("message"), dict) else None
        if isinstance(m, str) and m and obj.get("type") == "assistant":
            self.model = m

    def _feed_claude(self, obj: dict[str, Any], ts: float) -> None:
        t = obj.get("type")
        self._meta_claude(obj, ts)
        if obj.get("isSidechain"):
            return
        if t == "last-prompt":
            return
        self._touch(ts)
        if t == "attachment":
            att = obj.get("attachment") if isinstance(obj.get("attachment"), dict) else {}
            if att.get("hookEvent") == "Stop":
                self._turn_end(ts)
            return
        if t == "queue-operation":
            return
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")
        if t == "user":
            results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"] if isinstance(content, list) else []
            for r in results:
                self._tool_end(str(r.get("tool_use_id") or ""))
            if obj.get("isMeta"):
                return
            text = _first_text(content)
            if text.startswith("[Request interrupted by user"):
                self._turn_end(ts)
            elif text.strip() and not (_is_boilerplate(text) and self.turn_open is not None):
                self._turn_start(ts)          # a prompt (a tool result alone is part of the running turn)
        elif t == "assistant":
            if not self.turn_open:
                self.turn_open = True         # a reply with no prompt before it (a slash command, a resumed session)
                self.turn_started = ts
                self.turn_ended = None
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        got = snippet(part["text"])
                        if got:
                            self.last_text = got
                    elif part.get("type") == "tool_use":
                        self._tool_start(str(part.get("id") or ""), str(part.get("name") or "tool"), part.get("input"), ts)
            if msg.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens", "refusal"):
                self._turn_end(ts)


_QUESTION_KEYS = ("approval", "request_user_input", "elicitation")
_ASK_TOOLS = ("askuserquestion", "exitplanmode")
_RESOLVED_WORDS = ("response", "resolved", "granted", "denied", "approved", "rejected", "decision", "answer", "submitted",
                   "cancel", "complete", "dismiss")


# ---- state ------------------------------------------------------------------------------------------------------

def last_activity(s: Session) -> float:
    """The best guess of when the agent last wrote: the last line's timestamp, else the file's mtime."""
    return s.last_ts if s.last_ts is not None else s.mtime


def derive_state(s: Session, now: float) -> dict[str, Any]:
    """``{state, since, tool, questions}`` of a session at ``now``.

    * ``waiting`` — an explicit pending question / approval (any age), or, for Claude Code only, a tool call without
      its result for more than ``TOOL_WAIT_S`` (a permission prompt, probably) while the file was written in the last
      ``STALE_S``.
    * ``stale`` — a turn is open but nothing was written for ``STALE_S``.
    * ``tool`` — a turn is open and a tool call has no result yet; ``working`` — a turn is open and active.
    * ``idle`` — the last turn ended. With no turn marker in the tail, a write in the last ``UNKNOWN_TURN_RECENT_S``
      counts as ``working``.
    """
    act = last_activity(s)
    silence = max(0.0, now - act)
    tool = None
    if s.open_tools:
        cid, rec = next(reversed(s.open_tools.items()))
        tool = {"name": rec["name"], "since": rec["since"], "input": rec["input"]}
    questions = [{"kind": q["kind"], "what": q["what"], "since": q["since"], "tool": q.get("tool"),
                  "confidence": q["confidence"]} for q in s.pending.values()]
    if questions:
        first = min(q["since"] for q in questions)
        return {"state": "waiting", "since": first, "tool": tool, "questions": questions}
    open_ = s.turn_open
    if open_ is None:
        open_ = silence <= UNKNOWN_TURN_RECENT_S
    if not open_:
        return {"state": "idle", "since": s.turn_ended or act, "tool": None, "questions": []}
    if silence > STALE_S:
        return {"state": "stale", "since": act, "tool": tool, "questions": []}
    if s.engine == "claude" and s.open_tools:
        old = [r for r in s.open_tools.items() if now - r[1]["since"] > TOOL_WAIT_S]
        if old:
            cid, rec = old[0]
            q = {"kind": "approval", "what": f"{rec['name']}: {rec['input']}".strip(": "), "since": rec["since"],
                 "tool": rec["name"], "confidence": "heuristic"}
            return {"state": "waiting", "since": rec["since"], "tool": {"name": rec["name"], "since": rec["since"],
                                                                         "input": rec["input"]}, "questions": [q]}
    if tool:
        return {"state": "tool", "since": tool["since"], "tool": tool, "questions": []}
    return {"state": "working", "since": s.turn_started or act, "tool": None, "questions": []}


# ---- file tracking ----------------------------------------------------------------------------------------------

class _File:
    def __init__(self, engine: str, path: str):
        self.engine = engine
        self.path = path
        self.offset = 0
        self.size = 0
        self.ino: Any = None
        self.session: Optional[Session] = None


def _session_id(engine: str, path: str) -> str:
    stem = Path(path).stem
    if engine == "codex":
        m = _UUID_RE.search(stem)
        return m.group(1).lower() if m else stem
    return stem


def _key_of(f: "_File") -> str:
    return f"{f.engine}:{_session_id(f.engine, f.path)}"


def _workspace_from_slug(slug: str) -> str:
    """``c--Users-luism-Desktop-Modelos`` → ``Modelos``: the last part of the slug (a lossy path encoding)."""
    parts = [p for p in re.split(r"[-]+", slug) if p]
    return parts[-1] if parts else slug


def _scan_lines(data: bytes) -> Iterable[dict[str, Any]]:
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw or len(raw) > MAX_LINE or raw[:1] != b"{":
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _line_ts(engine: str, obj: dict[str, Any], fallback: float) -> tuple[float, bool]:
    """(timestamp, is it the line's own?): Cursor lines have none, so the file's mtime stands in for them."""
    if engine in ("codex", "claude"):
        ts = parse_ts(obj.get("timestamp"))
        if ts is not None:
            return ts, True
    return fallback, False


def _env_paths(name: str) -> Optional[list[str]]:
    raw = os.environ.get(name)
    if not raw:
        return None
    return [p.strip() for p in raw.replace(";", os.pathsep).split(os.pathsep) if p.strip()]


def default_roots(home: Optional[str] = None) -> dict[str, list[str]]:
    base = Path(home or os.environ.get("HOARD_AGENT_WATCH_HOME") or Path.home())
    roots = {"codex": [str(base / ".codex" / "sessions")],
             "cursor": [str(base / ".cursor" / "projects")],
             "claude": [str(base / ".claude" / "projects")]}
    for engine in ENGINES:
        env = _env_paths(f"HOARD_AGENT_WATCH_{engine.upper()}")
        if env is not None:
            roots[engine] = env
    return roots


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, os.PathLike)):
        return [str(value)]
    return [str(v) for v in value]


class AgentWatch:
    """Observes the transcripts under ``roots`` (``{engine: path | [paths]}``; defaults from the user's home).

    ``clock`` is injectable (tests); ``store_path`` keeps the explicit bindings (and the inferred hints) across
    restarts. Everything is read lazily by :meth:`view` / :meth:`refresh`.
    """

    def __init__(self, roots: Optional[dict[str, Any]] = None, clock: Callable[[], float] = time.time,
                 store_path: Any = None, hours: float = DEFAULT_HOURS, max_files: int = DEFAULT_MAX_FILES,
                 refresh_s: float = REFRESH_S, discover_s: float = DISCOVER_S):
        base = default_roots()
        if roots:
            for engine, value in roots.items():
                if engine in ENGINES:
                    base[engine] = _as_list(value)
        self.roots = {e: [os.path.expanduser(p) for p in base.get(e, [])] for e in ENGINES}
        self.clock = clock
        self.store_path = Path(store_path) if store_path else None
        self.window_s = max(60.0, float(hours) * 3600.0)
        self.max_files = max(1, int(max_files))
        self.refresh_s = refresh_s
        self.discover_s = discover_s
        self.bindings: dict[str, str] = {}
        self._hints: dict[str, dict[str, list[float]]] = {}
        self._files: dict[str, _File] = {}
        self._last_refresh = -1e18
        self._last_discover = -1e18
        self._lock = threading.RLock()
        self._dirty = False
        self._load()

    # -- persistence ------------------------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.store_path:
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        b = raw.get("bindings")
        if isinstance(b, dict):
            self.bindings = {str(k): str(v) for k, v in b.items() if isinstance(v, str) and _AGENT_RE.match(v)}
        h = raw.get("hints")
        if isinstance(h, dict):
            for key, rec in h.items():
                if isinstance(rec, dict):
                    good = {a: [float(n[0]), float(n[1])] for a, n in rec.items()
                            if _AGENT_RE.match(str(a)) and isinstance(n, list) and len(n) == 2
                            and all(isinstance(x, (int, float)) for x in n)}
                    if good:
                        self._hints[str(key)] = good

    def _save(self) -> None:
        if not self.store_path:
            self._dirty = False
            return
        payload = {"version": 1, "bindings": self.bindings, "hints": self._hints}
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.store_path)
            self._dirty = False
        except OSError:
            pass

    # -- discovery --------------------------------------------------------------------------------------------

    def _candidates(self, engine: str) -> list[str]:
        out: list[str] = []
        for root in self.roots.get(engine, []):
            r = Path(root)
            if not r.is_dir():
                continue
            if engine == "codex":
                pats = ("*/*/*/rollout-*.jsonl", "rollout-*.jsonl")
            elif engine == "cursor":
                pats = ("*/agent-transcripts/*/*.jsonl",)
            else:
                pats = ("*/*.jsonl",)
            for pat in pats:
                try:
                    out.extend(str(p) for p in r.glob(pat))
                except OSError:
                    continue
        return out

    def _discover(self, now: float) -> None:
        cutoff = now - self.window_s
        wanted: dict[str, _File] = {}
        for engine in ENGINES:
            recent: list[tuple[float, str]] = []
            for path in self._candidates(engine):
                try:
                    mt = os.stat(path).st_mtime
                except OSError:
                    continue
                if mt >= cutoff:
                    recent.append((mt, path))
            recent.sort(reverse=True)
            for mt, path in recent[: self.max_files]:
                f = self._files.get(path)
                wanted[path] = f if f is not None else _File(engine, path)
        self._files = wanted
        live = {_key_of(f) for f in wanted.values()}
        stale = [k for k in self._hints if k not in live]
        for k in stale:
            del self._hints[k]
        if stale:
            self._dirty = True
        self._last_discover = now

    # -- reading ----------------------------------------------------------------------------------------------

    def _new_session(self, f: _File) -> Session:
        s = Session(f.engine, _session_id(f.engine, f.path), f.path)
        s.hints = {a: list(v) for a, v in self._hints.get(s.key, {}).items()}
        if f.engine in ("cursor", "claude"):
            slug = Path(f.path).parent.name if f.engine == "claude" else Path(f.path).parents[2].name
            s.workspace = _workspace_from_slug(slug)
        return s

    def _read_head(self, f: _File, s: Session, size: int, mtime: float) -> None:
        try:
            with open(f.path, "rb") as fh:
                data = fh.read(min(size, HEAD_BYTES))
        except OSError:
            return
        if size > HEAD_BYTES:
            data = data[: data.rfind(b"\n") + 1]
        for obj in _scan_lines(data):
            s.meta(obj, _line_ts(f.engine, obj, mtime)[0])
            if s.title and s.cwd and f.engine != "cursor":
                break
            if s.title and f.engine == "cursor":
                break

    def _read_file(self, f: _File) -> None:
        try:
            st = os.stat(f.path)
        except OSError:
            return
        size, mtime = st.st_size, st.st_mtime
        ino = getattr(st, "st_ino", None)
        if f.session is not None and (size < f.offset or (f.ino and ino and ino != f.ino)):
            f.session, f.offset = None, 0          # truncated or replaced: start again
        if f.session is None:
            f.session = self._new_session(f)
            f.ino = ino
            self._read_head(f, f.session, size, mtime)
            start = max(0, size - TAIL_BYTES)
            f.offset = start
            first = True
        else:
            first = False
            start = f.offset
            if size - start > MAX_CATCHUP:
                start = max(0, size - TAIL_BYTES)
                first = True
        s = f.session
        s.mtime = mtime
        f.size = size
        if size <= start:
            f.offset = size
            return
        try:
            with open(f.path, "rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return
        base = start
        if first and start > 0:
            nl = data.find(b"\n")
            if nl < 0:
                f.offset = size
                return
            data, base = data[nl + 1:], start + nl + 1
        cut = data.rfind(b"\n")
        complete = data[: cut + 1] if cut >= 0 else b""
        rest = data[cut + 1:]
        consumed = base + len(complete)
        if len(rest) > MAX_LINE:
            consumed = base + len(data)               # an enormous unterminated line: not worth waiting for
        elif rest.strip():
            try:
                tail_obj = json.loads(rest)
            except ValueError:
                tail_obj = None
            if isinstance(tail_obj, dict):                # a complete last line the writer has not terminated yet
                complete += rest
                consumed = base + len(data)
        f.offset = consumed
        before = {a: tuple(v) for a, v in s.hints.items()}
        for obj in _scan_lines(complete):
            try:
                s.feed(obj, *_line_ts(f.engine, obj, mtime))
            except Exception:  # noqa: BLE001 - a strange line must not stop the observer
                continue
        if {a: tuple(v) for a, v in s.hints.items()} != before:
            self._hints[s.key] = {a: list(v) for a, v in s.hints.items()}
            self._dirty = True

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            now = self.clock()
            if not force and now - self._last_refresh < self.refresh_s:
                return
            self._last_refresh = now
            if force or now - self._last_discover >= self.discover_s:
                self._discover(now)
            for f in list(self._files.values()):
                try:
                    self._read_file(f)
                except Exception:  # noqa: BLE001
                    continue
            if self._dirty:
                self._save()

    # -- bindings ---------------------------------------------------------------------------------------------

    def bind(self, session_key: Any, agent: Any) -> dict[str, Any]:
        key = str(session_key or "").strip()
        if not key:
            raise WatchError("session_key is required")
        agent = str(agent or "").strip().lower()
        with self._lock:
            known = key in self.bindings or key in self._hints or any(
                f.session is not None and f.session.key == key for f in self._files.values())
            if not known:
                self.refresh()
                known = any(f.session is not None and f.session.key == key for f in self._files.values())
            if not known:
                raise WatchError(f"unknown session {key}", 404)
            if agent:
                if not _AGENT_RE.match(agent) or agent == _PERSON:
                    raise WatchError("agent must be an Ágora agent id (lowercase letters, digits, - _ .), not the person's")
                self.bindings[key] = agent
            else:
                self.bindings.pop(key, None)
            self._save()
            return {"ok": True, "session_key": key, "agent": agent or None,
                    "binding": "explicit" if agent else None}

    def _binding(self, s: Session) -> tuple[Optional[str], Optional[str]]:
        agent = self.bindings.get(s.key)
        if agent:
            return agent, "explicit"
        hints = s.hints or self._hints.get(s.key) or {}
        if hints:
            best = max(hints.items(), key=lambda kv: (kv[1][0], kv[1][1]))
            return best[0], "inferred"
        return None, None

    # -- view -------------------------------------------------------------------------------------------------

    def _summary(self, s: Session, now: float) -> dict[str, Any]:
        d = derive_state(s, now)
        agent, how = self._binding(s)
        questions = [{**q, "agent": agent, "engine": s.engine, "session_key": s.key, "title": s.title}
                     for q in d["questions"]]
        return {
            "key": s.key, "engine": s.engine, "session_id": s.session_id, "file": s.path, "cwd": s.cwd,
            "workspace": s.workspace, "title": s.title, "state": d["state"], "since": d["since"],
            "last_activity": last_activity(s), "turn_started_at": s.turn_started if s.turn_open else None,
            "tool": d["tool"], "last_tool": s.last_tool, "last_text": s.last_text, "tokens": s.tokens,
            "agent": agent, "binding": how, "questions": questions, "originator": s.originator or None,
        }

    def view(self, *, force: bool = False, agent: Optional[str] = None, limit: int = MAX_SESSIONS_OUT) -> dict[str, Any]:
        """``{sessions, agents, questions, unbound}``; refreshes the transcripts first when the last refresh is old."""
        with self._lock:
            self.refresh(force=force)
            now = self.clock()
            sessions = [self._summary(f.session, now) for f in self._files.values() if f.session is not None]
        sessions.sort(key=lambda x: x["last_activity"], reverse=True)
        best: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for sm in sessions:
            a = sm["agent"]
            if not a:
                continue
            counts[a] = counts.get(a, 0) + 1
            cur = best.get(a)
            if cur is None or (_RANK[sm["state"]], sm["last_activity"]) > (_RANK[cur["state"]], cur["last_activity"]):
                best[a] = sm
        agents = {a: {"state": sm["state"], "since": sm["since"], "tool": sm["tool"], "title": sm["title"],
                      "engine": sm["engine"], "session_key": sm["key"], "last_activity": sm["last_activity"],
                      "last_text": sm["last_text"], "binding": sm["binding"], "sessions": counts[a],
                      "questions": len(sm["questions"])} for a, sm in best.items()}
        questions = sorted((q for sm in sessions for q in sm["questions"]), key=lambda q: q["since"])
        unbound = [sm for sm in sessions if not sm["agent"]][:MAX_UNBOUND_OUT]
        if agent:
            sessions = [sm for sm in sessions if sm["agent"] == agent]
            agents = {k: v for k, v in agents.items() if k == agent}
            questions = [q for q in questions if q["agent"] == agent]
            unbound = []
        return {"now": now, "window_hours": self.window_s / 3600.0, "sessions": sessions[: max(1, int(limit))],
                "agents": agents, "questions": questions, "unbound": unbound,
                "roots": {e: list(r) for e, r in self.roots.items()}}


# ---- tools ------------------------------------------------------------------------------------------------------

_AGENT_PROP = {"type": "string", "description": "Ágora agent id (codex-sparks, cursor, claude…)"}

TOOLS: list[dict[str, Any]] = [
    {"name": "hub_agora_watch", "read": True,
     "description": "Real state of the coding agents from their transcripts / estado real, preguntas pendientes, sesiones.\n"
                    "Per agent: working, tool, waiting (approval/question), idle or stale, with the tool and since when; "
                    "the pending questions for the person; transcripts not bound to an agent. Passive: reads local "
                    "transcript tails only, snippets <=200 chars. Refreshed at most every 5 s (refresh=true forces).",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": [], "properties": {
         "agent": _AGENT_PROP, "refresh": {"type": "boolean"},
         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SESSIONS_OUT}}}},
    {"name": "hub_agora_watch_bind",
     "description": "Bind a watched transcript session to an Ágora agent / asignar sesión a un agente (agent='' unbinds).\n"
                    "session_key comes from hub_agora_watch (engine:session id). The binding is kept across restarts "
                    "and wins over the automatic inference from the session's own agora calls.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["session_key", "agent"], "properties": {
         "session_key": {"type": "string", "description": "engine:session id, e.g. codex:019d… or claude:<uuid>"},
         "agent": {"type": "string", "description": "Ágora agent id; empty string removes the binding"}}}},
]
