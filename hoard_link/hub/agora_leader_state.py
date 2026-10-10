"""Live state of each Ágora leader (working, waiting, idle, login expired, failed) from its CLI's own session files.

The Ágora console shows each leader's quota, but a leader whose CLI sat idle, or whose login expired (a 401 nobody
noticed), looked exactly like a healthy one. This module answers «what is that leader's CLI doing right now?» by
reading, READ-ONLY, the files each CLI already writes:

* **Claude Code** — the newest ``~/.claude/projects/<slug>/<uuid>.jsonl`` plus ``~/.claude/.credentials.json``.
* **Codex** — the newest ``~/.codex/sessions/**/rollout-*.jsonl``.
* **Cursor** — the managed runner's ``launch-state.json`` and ``logs/*.jsonl`` (``D:\\LocalAI\\cursor`` by default).
* **Faustus** — has no session file of its own here; the console passes the native session state it already knows.

Privacy: only event kinds and timestamps leave a file. Message bodies, tool inputs and outputs, reasoning and file
paths are never copied into the result (the parsers only look at ``type``-like fields, ``timestamp`` fields and the
``type`` of content blocks), and from the credentials file only ``claudeAiOauth.expiresAt`` is read — the token
fields are never touched. Every ``detail`` is a fixed Spanish sentence, never text taken from a file.

Each leader gets ``{state, label, last_activity_at, last_activity_ts, source, detail, ...}`` where ``state`` is one of
:data:`STATES`. :func:`build_leader_states` additionally flags ``idle_with_work`` when a leader that is idle, has an
expired login or failed still owns open Ágora tasks.

Paths come from the arguments of :func:`read_leader_states`, else the ``HOARD_LEADER_*`` variables, else the user's
home; tests pass temporary directories. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

STATES = ("working", "waiting", "idle", "auth_expired", "error", "unknown")
#: Spanish label shown by the CLI and the MCP tool (the page builds its own, with the viewer's local time).
LABELS = {"working": "Trabajando", "waiting": "Esperando", "idle": "Inactivo", "auth_expired": "Sesión caducada",
          "error": "Error", "unknown": "Sin datos"}
#: Task statuses that mean «the owner should be working on it now».
OPEN_TASK_STATUSES = ("claimed", "in_progress", "changes")
#: States that, while the leader owns an open task, are worth flagging.
NEEDS_ATTENTION = ("idle", "auth_expired", "error")
#: Activity this recent is «working» when the file has no turn markers.
WORKING_S = 120.0
#: A turn left open with nothing written for this long means the process is probably gone.
STALE_TURN_S = 30 * 60.0
#: A tool call with no result for this long (Claude Code) is probably a permission prompt.
TOOL_WAIT_S = 60.0
TAIL_BYTES = 256 * 1024
MAX_LINE = 4 * 1024 * 1024

_ENV = {"claude_projects": "HOARD_LEADER_CLAUDE_PROJECTS", "claude_credentials": "HOARD_LEADER_CLAUDE_CREDENTIALS",
        "codex_sessions": "HOARD_LEADER_CODEX_SESSIONS", "cursor_dir": "HOARD_LEADER_CURSOR_DIR"}
_CLAUDE_TURN_END = ("end_turn", "stop_sequence", "max_tokens", "refusal")
#: Claude Code tools that stop and wait for the person.
_CLAUDE_ASK_TOOLS = ("askuserquestion", "exitplanmode")
_CODEX_ASK_EVENTS = ("exec_approval_request", "apply_patch_approval_request", "request_user_input",
                     "elicitation_request", "request_permissions", "permissions_request", "approval_request")
_CODEX_FAIL_EVENTS = ("error", "stream_error", "task_failed", "turn_failed")
_CURSOR_FAILED = ("failed", "error", "crashed", "timeout", "timed_out")
_CURSOR_RUNNING = ("running", "started", "starting", "launching", "in_progress")
_KIND_RE = re.compile(r"^[a-z0-9_\-]{1,40}$")


# ---- paths ---------------------------------------------------------------------------------------------------------

def default_paths(home: Optional[Any] = None) -> dict[str, Path]:
    """Where each CLI keeps its files: the ``HOARD_LEADER_*`` variable when set, else under ``home``."""
    base = Path(home) if home else Path.home()
    paths = {"claude_projects": base / ".claude" / "projects",
             "claude_credentials": base / ".claude" / ".credentials.json",
             "codex_sessions": base / ".codex" / "sessions",
             "cursor_dir": Path(r"D:\LocalAI\cursor")}
    for key, var in _ENV.items():
        value = os.environ.get(var)
        if value:
            paths[key] = Path(value).expanduser()
    return paths


def resolve_paths(overrides: Optional[dict[str, Any]] = None) -> dict[str, Path]:
    paths = default_paths()
    for key, value in (overrides or {}).items():
        if key in paths and value:
            paths[key] = Path(value)
    return paths


# ---- small helpers -------------------------------------------------------------------------------------------------

def parse_ts(value: Any) -> Optional[float]:
    """An ISO timestamp (``…Z`` or with an offset) or an epoch number (seconds or milliseconds) as epoch seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str) and value.strip():
        try:
            d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    return None


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _label(state: str, ts: Optional[float]) -> str:
    if state == "idle" and ts is not None:
        return "Inactivo desde " + datetime.fromtimestamp(ts).strftime("%H:%M")
    return LABELS.get(state, state)


def _result(state: str, source: str, detail: str, last_ts: Optional[float], **extra: Any) -> dict[str, Any]:
    out = {"state": state, "label": _label(state, last_ts), "last_activity_at": _iso(last_ts),
           "last_activity_ts": last_ts, "source": source, "detail": detail}
    out.update(extra)
    return out


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _newest(paths: Iterable[Path]) -> Optional[tuple[Path, float]]:
    best: Optional[tuple[Path, float]] = None
    for p in paths:
        mt = _mtime(p)
        if mt is not None and (best is None or mt > best[1]):
            best = (p, mt)
    return best


def _walk_jsonl(root: Path, prefix: str = "", max_depth: int = 6) -> Iterable[Path]:
    """Every ``<prefix>*.jsonl`` under ``root`` (bounded depth, errors skipped)."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        stack.append((Path(e.path), depth + 1))
                elif e.name.endswith(".jsonl") and e.name.startswith(prefix):
                    yield Path(e.path)
            except OSError:
                continue


def _tail_objects(path: Path) -> list[dict[str, Any]]:
    """The JSON objects of the last ``TAIL_BYTES`` of a JSON-lines file (a cut first line is dropped)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    out: list[dict[str, Any]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw or len(raw) > MAX_LINE or raw[:1] != b"{":
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _kind(value: Any) -> str:
    """A short machine-like kind (``authentication_failed``); anything that looks like prose becomes ``""``."""
    return value if isinstance(value, str) and _KIND_RE.match(value) else ""


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _verdict(*, turn_open: Optional[bool], pending: bool, silence: float, ended_ts: Optional[float],
             last_ts: Optional[float], source: str, ended: str, stale: str) -> dict[str, Any]:
    """Shared working / waiting / idle decision once a source reduced its lines to turn facts."""
    if pending:
        return _result("waiting", source, "Espera una respuesta o un permiso tuyo.", last_ts)
    if turn_open is None:
        turn_open = silence <= WORKING_S
        if not turn_open:
            return _result("idle", source, ended, last_ts)
    if not turn_open:
        return _result("idle", source, ended, ended_ts or last_ts)
    if silence > STALE_TURN_S:
        return _result("idle", source, stale, last_ts)
    return _result("working", source, "Turno en curso.", last_ts)


# ---- Claude Code ---------------------------------------------------------------------------------------------------

def read_expires_at(path: Path) -> Optional[float]:
    """``claudeAiOauth.expiresAt`` (epoch seconds) from the credentials file; no other field is looked at."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    value = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    return parse_ts(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _reduce_claude(events: list[dict[str, Any]]) -> dict[str, Any]:
    last_ts: Optional[float] = None
    turn_open: Optional[bool] = None
    ended_ts: Optional[float] = None
    open_tools: dict[str, tuple[str, float]] = {}
    error_kind = ""
    error_ts: Optional[float] = None
    for obj in events:
        kind = obj.get("type")
        ts = parse_ts(obj.get("timestamp"))
        if ts is not None and kind not in ("last-prompt", "queue-operation"):
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if obj.get("isSidechain") or ts is None:
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = message.get("content")
        if kind == "user":
            blocks = _blocks(content)
            for b in blocks:
                if b.get("type") == "tool_result":
                    open_tools.pop(str(b.get("tool_use_id") or ""), None)
            if obj.get("isMeta"):
                continue
            only_results = bool(blocks) and all(b.get("type") == "tool_result" for b in blocks)
            if not only_results:                      # a prompt starts a turn; tool results belong to the running one
                open_tools.clear()
                error_kind, error_ts = "", None
            turn_open, ended_ts = True, None
        elif kind == "assistant":
            turn_open, ended_ts = True, None
            for b in _blocks(content):
                if b.get("type") == "tool_use":
                    open_tools[str(b.get("id") or f"anon-{len(open_tools)}")] = (str(b.get("name") or "").lower(), ts)
            if obj.get("isApiErrorMessage") or obj.get("error"):
                error_kind, error_ts = _kind(obj.get("error")) or "api_error", ts
                turn_open, ended_ts = False, ts
                open_tools.clear()
            elif message.get("stop_reason") in _CLAUDE_TURN_END and not open_tools:
                turn_open, ended_ts = False, ts
        elif kind == "system" and obj.get("subtype") == "turn_duration" and not open_tools:
            turn_open, ended_ts = False, ts
    return {"last_ts": last_ts, "turn_open": turn_open, "ended_ts": ended_ts, "open_tools": open_tools,
            "error_kind": error_kind, "error_ts": error_ts}


def claude_state(projects: Path, credentials: Path, now: float) -> dict[str, Any]:
    newest = _newest(p for p in _walk_jsonl(Path(projects), max_depth=1))
    expires = read_expires_at(credentials) if credentials else None
    cred_mtime = _mtime(Path(credentials)) if credentials else None
    extra: dict[str, Any] = {"auth_expires_at": _iso(expires)} if expires is not None else {}
    if newest is None:
        detail = "No hay sesiones de Claude Code en este equipo."
        if expires is not None and expires <= now:
            detail = "No hay sesiones de Claude Code y el acceso caducó; se renueva al abrir el CLI."
        return _result("unknown", "claude_session", detail, None, **extra)
    path, mtime = newest
    info = _reduce_claude(_tail_objects(path))
    last_ts = info["last_ts"] if info["last_ts"] is not None else mtime
    silence = max(0.0, now - last_ts)
    err, err_ts = info["error_kind"], info["error_ts"]
    if err and ("auth" in err or "401" in err or "oauth" in err or "unauthor" in err):
        # A login renewed after the failed call (credentials file rewritten later) is no longer a problem.
        if cred_mtime is None or err_ts is None or cred_mtime <= err_ts + 1:
            return _result("auth_expired", "claude_session",
                           "La última llamada falló por autenticación (401); vuelve a iniciar sesión en Claude Code.",
                           err_ts or last_ts, **extra)
    elif err:
        return _result("error", "claude_session", "El último turno terminó con un error de la API.", err_ts or last_ts,
                       **extra)
    if expires is not None and expires <= now and last_ts > expires:
        # The CLI ran after the access token expired and it is still expired: the refresh did not work.
        return _result("auth_expired", "claude_credentials",
                       "El acceso caducó y el CLI no logró renovarlo; vuelve a iniciar sesión.", last_ts, **extra)
    tools = info["open_tools"]
    pending = any(name in _CLAUDE_ASK_TOOLS for name, _ in tools.values())
    if not pending and tools and info["turn_open"] and silence <= STALE_TURN_S:
        oldest = min(since for _, since in tools.values())
        pending = now - oldest > TOOL_WAIT_S               # a tool without result for a while: a permission prompt
    result = _verdict(turn_open=info["turn_open"], pending=pending, silence=silence, ended_ts=info["ended_ts"],
                      last_ts=last_ts, source="claude_session", ended="El último turno terminó.",
                      stale="Turno abierto sin actividad; el proceso probablemente ya no existe.")
    result.update(extra)
    if result["state"] == "idle" and expires is not None and expires <= now:
        result["detail"] += " El acceso caducó pero se renueva al usarlo."
    return result


# ---- Codex ---------------------------------------------------------------------------------------------------------

def _reduce_codex(events: list[dict[str, Any]]) -> dict[str, Any]:
    last_ts: Optional[float] = None
    turn_open: Optional[bool] = None
    ended_ts: Optional[float] = None
    pending = False
    failed = False
    for obj in events:
        ts = parse_ts(obj.get("timestamp"))
        if ts is not None:
            last_ts = ts if last_ts is None else max(last_ts, ts)
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ptype = str(payload.get("type") or "").lower()
        if obj.get("type") == "event_msg":
            if ptype == "task_started":
                turn_open, ended_ts, pending, failed = True, None, False, False
            elif ptype == "task_complete":
                turn_open, ended_ts, pending = False, ts, False
            elif ptype in ("turn_aborted", "task_aborted"):
                turn_open, ended_ts, pending = False, ts, False
            elif ptype in _CODEX_FAIL_EVENTS:
                turn_open, ended_ts, pending, failed = False, ts, False, True
            elif ptype in _CODEX_ASK_EVENTS:
                pending = True
        elif obj.get("type") == "response_item" and (ptype.endswith("_call_output") or ptype == "message"):
            pending = False                               # the agent went on: the question was answered
    return {"last_ts": last_ts, "turn_open": turn_open, "ended_ts": ended_ts, "pending": pending, "failed": failed}


def codex_state(sessions: Path, now: float) -> dict[str, Any]:
    newest = _newest(_walk_jsonl(Path(sessions), prefix="rollout-", max_depth=5))
    if newest is None:
        return _result("unknown", "codex_session", "No hay sesiones de Codex en este equipo.", None)
    path, mtime = newest
    info = _reduce_codex(_tail_objects(path))
    last_ts = info["last_ts"] if info["last_ts"] is not None else mtime
    if info["failed"] and info["turn_open"] is False:
        return _result("error", "codex_session", "El último turno terminó con un error.", info["ended_ts"] or last_ts)
    return _verdict(turn_open=info["turn_open"], pending=info["pending"], silence=max(0.0, now - last_ts),
                    ended_ts=info["ended_ts"], last_ts=last_ts, source="codex_session",
                    ended="El último turno terminó.",
                    stale="Turno abierto sin actividad; el proceso probablemente ya no existe.")


# ---- Cursor --------------------------------------------------------------------------------------------------------

def default_pid_alive(pid: Any) -> bool:
    """Whether process ``pid`` is running. Never signals the process (``os.kill`` would terminate it on Windows)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _cursor_log(logs: Path) -> dict[str, Any]:
    newest = _newest(p for p in _walk_jsonl(Path(logs), max_depth=0))
    if newest is None:
        return {}
    path, mtime = newest
    last_ts: Optional[float] = None
    last_type = ""
    result_error = False
    for obj in _tail_objects(path):
        ts = parse_ts(obj.get("timestamp_ms"))
        if ts is not None:
            last_ts = ts if last_ts is None else max(last_ts, ts)
        last_type = str(obj.get("type") or last_type)
        if obj.get("type") == "result":
            result_error = bool(obj.get("is_error")) or obj.get("subtype") not in (None, "success")
    # Most runner events carry no timestamp (the final ``result`` has none): the file's last write is the clock.
    return {"last_ts": max(t for t in (last_ts, mtime) if t is not None), "last_type": last_type,
            "result_error": result_error}


def cursor_state(cursor_dir: Path, now: float, pid_alive: Callable[[Any], bool] = default_pid_alive) -> dict[str, Any]:
    cursor_dir = Path(cursor_dir)
    doc: Optional[dict[str, Any]] = None
    try:
        raw = json.loads((cursor_dir / "launch-state.json").read_text(encoding="utf-8-sig"))
        doc = raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        doc = None
    log = _cursor_log(cursor_dir / "logs")
    log_ts = log.get("last_ts")
    log_recent = log_ts is not None and now - log_ts <= WORKING_S
    log_running = log_recent and log.get("last_type") != "result"
    if doc is None:
        if not log:
            return _result("unknown", "cursor_launch_state", "No hay estado de lanzamiento ni registros de Cursor.", None)
        if log_running:
            return _result("working", "cursor_log", "Ejecución en curso según el registro.", log_ts)
        if log.get("result_error"):
            return _result("error", "cursor_log", "La última ejecución terminó con error.", log_ts)
        return _result("idle", "cursor_log", "La última ejecución terminó.", log_ts)

    status = str(doc.get("status") or "").strip().lower()
    started = parse_ts(doc.get("started_at_utc"))
    completed = parse_ts(doc.get("completed_at_utc"))
    exit_code = doc.get("exit_code")
    last_ts = max([t for t in (started, completed, log_ts) if t is not None], default=None)
    extra = {"run_status": _kind(status) or None, "exit_code": exit_code if isinstance(exit_code, int) else None}
    if any(w in status for w in ("wait", "await", "input", "approval")):
        return _result("waiting", "cursor_launch_state", "La ejecución espera una respuesta o un permiso.", last_ts, **extra)
    finished = completed is not None or status not in _CURSOR_RUNNING
    if finished and log_running and (completed is None or (log_ts or 0) > completed + 5):
        return _result("working", "cursor_log", "Hay una ejecución nueva en curso en el registro.", log_ts, **extra)
    if not finished:
        pid = doc.get("cli_pid")
        alive = pid_alive(pid) if pid else None
        if alive or (alive is None and last_ts is not None and now - last_ts <= STALE_TURN_S) or log_recent:
            return _result("working", "cursor_launch_state", "Ejecución en curso.", last_ts, **extra)
        return _result("error", "cursor_launch_state",
                       "La ejecución figura en curso pero su proceso ya no existe.", last_ts, **extra)
    failed = (status in _CURSOR_FAILED or bool(doc.get("error"))
              or (isinstance(exit_code, int) and exit_code != 0))
    if failed:
        code = f" (código {exit_code})" if isinstance(exit_code, int) else ""
        return _result("error", "cursor_launch_state", f"La última ejecución falló{code}.", completed or last_ts, **extra)
    if log.get("result_error") and log_ts is not None and (completed is None or log_ts >= completed - 5):
        return _result("error", "cursor_log", "La última ejecución terminó con error.", log_ts, **extra)
    return _result("idle", "cursor_launch_state", "La última ejecución terminó.", completed or last_ts, **extra)


# ---- Faustus -------------------------------------------------------------------------------------------------------

def faustus_state(native_session: Optional[dict[str, Any]], provider_state: str = "") -> dict[str, Any]:
    """Faustus has no CLI session file here: use the native session state the console already verified."""
    session_state = str((native_session or {}).get("state") or "").lower()
    if session_state == "working":
        return _result("working", "faustus_native_session", "Turno nativo en curso.", None)
    if session_state == "idle":
        return _result("idle", "faustus_native_session", "Sesión nativa sin turno activo.", None)
    if provider_state in ("unavailable",):
        return _result("unknown", "faustus_provider", "Faustus no responde en el Hub.", None)
    return _result("unknown", "faustus_native_session", "No hay una sesión nativa verificada.", None)


# ---- public API ----------------------------------------------------------------------------------------------------

def read_leader_states(now: Optional[float] = None, *, paths: Optional[dict[str, Any]] = None,
                       pid_alive: Callable[[Any], bool] = default_pid_alive) -> dict[str, dict[str, Any]]:
    """``{claude, codex, cursor}`` live states from the session files (see the module docstring)."""
    now = time.time() if now is None else float(now)
    p = resolve_paths(paths)
    out: dict[str, dict[str, Any]] = {}
    for leader, reader in (("codex", lambda: codex_state(p["codex_sessions"], now)),
                           ("claude", lambda: claude_state(p["claude_projects"], p["claude_credentials"], now)),
                           ("cursor", lambda: cursor_state(p["cursor_dir"], now, pid_alive))):
        try:
            out[leader] = reader()
        except Exception:  # noqa: BLE001 - a strange file must not break the console
            out[leader] = _result("unknown", f"{leader}_session", "No se pudieron leer los ficheros de sesión.", None)
    return out


def open_task_ids(tasks: Iterable[dict[str, Any]], leader_id: str, agent_ids: Iterable[str]) -> list[int]:
    """Ids of the open tasks (claimed / in progress / changes requested) owned by the leader's Ágora agents."""
    own = {str(a).lower() for a in agent_ids}
    prefix = leader_id.lower() + "-"
    ids: list[int] = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        owner = str(t.get("owner") or "").lower()
        status = str(t.get("status") or t.get("state") or "").lower()
        if status in OPEN_TASK_STATUSES and owner and (owner in own or owner.startswith(prefix)):
            try:
                ids.append(int(t["id"]))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(ids)


#: An Ágora heartbeat or post this recent means the leader is working, even
#: when its local session files say otherwise (a session that runs on another
#: machine or in the cloud writes nothing here).
AGORA_ACTIVE_S = 15 * 60


def agora_last_seen(agents: Iterable[dict[str, Any]], leader_id: str, agent_ids: Iterable[str]) -> Optional[float]:
    """The newest Ágora ``last_seen`` among the leader's agents (its id, or ``<leader>-*``)."""
    own = {str(a).lower() for a in agent_ids}
    prefix = leader_id.lower() + "-"
    best: Optional[float] = None
    for a in agents or []:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").lower()
        if not aid or not (aid in own or aid.startswith(prefix)):
            continue
        try:
            seen = float(a.get("last_seen") or 0)
        except (TypeError, ValueError):
            continue
        if seen and (best is None or seen > best):
            best = seen
    return best


def build_leader_states(leaders: Iterable[tuple[str, str, str]], *, tasks: Iterable[dict[str, Any]] = (),
                        faustus_native_session: Optional[dict[str, Any]] = None, faustus_provider_state: str = "",
                        now: Optional[float] = None, paths: Optional[dict[str, Any]] = None,
                        pid_alive: Callable[[Any], bool] = default_pid_alive,
                        agents: Iterable[dict[str, Any]] = ()) -> dict[str, dict[str, Any]]:
    """Live state of every ``(leader_id, name, agent_id)`` plus its open tasks and the ``idle_with_work`` flag."""
    now = time.time() if now is None else float(now)
    states = read_leader_states(now, paths=paths, pid_alive=pid_alive)
    states["faustus"] = faustus_state(faustus_native_session, faustus_provider_state)
    task_list = list(tasks or [])
    agent_list = list(agents or [])
    out: dict[str, dict[str, Any]] = {}
    for leader_id, name, agent_id in leaders:
        state = dict(states.get(leader_id) or _result("unknown", "none", "Sin lector para este líder.", None))
        open_ids = open_task_ids(task_list, leader_id, (agent_id,))
        state["open_tasks"] = open_ids
        seen = agora_last_seen(agent_list, leader_id, (agent_id,))
        state["agora_last_seen_at"] = _iso(seen)
        if seen is not None and now - seen <= AGORA_ACTIVE_S and state["state"] in ("idle", "unknown"):
            last_ts = state.get("last_activity_ts")
            if last_ts is None or seen > float(last_ts):
                state = dict(state, **_result(
                    "working", "agora",
                    "Activo en el Ágora en los últimos 15 min (latido o mensaje), aunque su sesión local no muestra actividad.", seen),
                    open_tasks=open_ids, agora_last_seen_at=_iso(seen))
        # Recently active in the Ágora means somebody IS on it, whatever the
        # local files say (a logged-out local CLI next to a working cloud one).
        agora_active = seen is not None and now - seen <= AGORA_ACTIVE_S
        state["idle_with_work"] = bool(open_ids) and state["state"] in NEEDS_ATTENTION and not agora_active
        if state["idle_with_work"]:
            state["detail"] += f" Tiene {len(open_ids)} tarea(s) abierta(s) sin que nadie trabaje en ellas."
        out[leader_id] = state
    return out
