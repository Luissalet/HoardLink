from __future__ import annotations

import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hoard_link.hub import agora_leader_state as ls
from hoard_link.hub.agora_console import LEADERS
from hoard_link.hub.tools import call

from .test_agora_console import _facet, _request

NOW = 1_800_000_000.0
SECRET = "TOP-SECRET-BODY do-not-leak"

CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agora.py"
CLI_SPEC = importlib.util.spec_from_file_location("agora_cli_leaders_under_test", CLI_PATH)
CLI = importlib.util.module_from_spec(CLI_SPEC)
assert CLI_SPEC and CLI_SPEC.loader
CLI_SPEC.loader.exec_module(CLI)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_jsonl(path: Path, lines: list[dict], mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    stamp = mtime if mtime is not None else max(
        [ls.parse_ts(x.get("timestamp")) or ls.parse_ts(x.get("timestamp_ms")) or 0 for x in lines] or [NOW])
    os.utime(path, (stamp, stamp))
    return path


def paths_for(tmp_path: Path) -> dict[str, Path]:
    return {"claude_projects": tmp_path / "claude" / "projects", "claude_credentials": tmp_path / "claude" / "cred.json",
            "codex_sessions": tmp_path / "codex" / "sessions", "cursor_dir": tmp_path / "cursor"}


# ---- Claude Code lines ------------------------------------------------------------------------------------------

def c_user(ts, content=SECRET):
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": content}}


def c_result(ts, tid):
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": SECRET}]}}


def c_assistant(ts, blocks=None, stop=None, **extra):
    return {"type": "assistant", "timestamp": iso(ts), **extra, "message": {
        "role": "assistant", "stop_reason": stop, "content": blocks or [{"type": "text", "text": SECRET}]}}


def c_tool(tid, name, ts=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": {"command": SECRET}}


def claude_session(tmp_path, lines, mtime=None, name="s1.jsonl"):
    return write_jsonl(paths_for(tmp_path)["claude_projects"] / "proj" / name, lines, mtime)


def claude(tmp_path, now=NOW):
    p = paths_for(tmp_path)
    return ls.claude_state(p["claude_projects"], p["claude_credentials"], now)


def credentials(tmp_path, expires_at_s: float, mtime: float | None = None):
    path = paths_for(tmp_path)["claude_credentials"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-SECRET-ACCESS",
                                                  "refreshToken": "sk-ant-ort-SECRET-REFRESH",
                                                  "expiresAt": int(expires_at_s * 1000)}}), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---- Claude Code states -----------------------------------------------------------------------------------------

def test_claude_working_when_a_prompt_or_tool_just_ran(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 300), c_assistant(NOW - 20, [c_tool("t1", "Bash")], stop="tool_use")])
    result = claude(tmp_path)
    assert result["state"] == "working" and result["label"] == "Trabajando"
    assert result["source"] == "claude_session" and result["last_activity_ts"] == pytest.approx(NOW - 20)
    assert result["last_activity_at"] == iso(NOW - 20).replace(".000Z", "Z")


def test_claude_idle_since_the_end_of_the_last_turn_even_with_trailing_hook_lines(tmp_path):
    lines = [c_user(NOW - 900), c_assistant(NOW - 600, stop="end_turn"),
             {"type": "attachment", "timestamp": iso(NOW - 599), "attachment": {"hookEvent": "Stop"}},
             {"type": "last-prompt", "lastPrompt": SECRET}]
    claude_session(tmp_path, lines)
    result = claude(tmp_path)
    assert result["state"] == "idle"
    assert result["last_activity_ts"] == pytest.approx(NOW - 600)
    assert result["label"].startswith("Inactivo desde ")


def test_claude_a_finished_turn_seconds_ago_is_idle_not_working(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 30), c_assistant(NOW - 5, stop="end_turn")])
    assert claude(tmp_path)["state"] == "idle"


def test_claude_waiting_on_an_ask_tool_or_a_long_unanswered_tool_call(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 40), c_assistant(NOW - 10, [c_tool("q1", "AskUserQuestion")], stop="tool_use")])
    ask = claude(tmp_path)
    assert ask["state"] == "waiting" and ask["label"] == "Esperando"

    claude_session(tmp_path, [c_user(NOW - 400), c_assistant(NOW - 300, [c_tool("b1", "Bash")], stop="tool_use")],
                   name="s2.jsonl", mtime=NOW)
    permission = claude(tmp_path)
    assert permission["state"] == "waiting"

    claude_session(tmp_path, [c_user(NOW - 400), c_assistant(NOW - 300, [c_tool("b1", "Bash")], stop="tool_use"),
                              c_result(NOW - 200, "b1"), c_assistant(NOW - 190, stop="end_turn")],
                   name="s3.jsonl", mtime=NOW + 1)
    assert claude(tmp_path)["state"] == "idle"


def test_claude_turn_left_open_for_hours_is_idle_not_working(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 7200)])
    result = claude(tmp_path)
    assert result["state"] == "idle" and "probablemente" in result["detail"]


def test_claude_401_in_the_last_turn_is_auth_expired_until_the_login_is_renewed(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 700),
                              c_assistant(NOW - 600, stop="stop_sequence", isApiErrorMessage=True,
                                          error="authentication_failed")])
    credentials(tmp_path, NOW + 3600, mtime=NOW - 5000)             # not rewritten since the failure
    expired = claude(tmp_path)
    assert expired["state"] == "auth_expired" and expired["label"] == "Sesión caducada"
    credentials(tmp_path, NOW + 3600, mtime=NOW - 100)               # logged in again afterwards
    assert claude(tmp_path)["state"] == "idle"


def test_claude_expires_at_in_the_past_only_counts_when_the_cli_ran_after_it(tmp_path):
    credentials(tmp_path, NOW - 3600)
    claude_session(tmp_path, [c_user(NOW - 7200), c_assistant(NOW - 7100, stop="end_turn")])
    quiet = claude(tmp_path)                                         # idle all night: the token renews on use
    assert quiet["state"] == "idle" and "se renueva" in quiet["detail"]
    assert quiet["auth_expires_at"] == iso(NOW - 3600).replace(".000Z", "Z")

    claude_session(tmp_path, [c_user(NOW - 600), c_assistant(NOW - 590, stop="end_turn")], name="s2.jsonl", mtime=NOW)
    broken = claude(tmp_path)                                        # ran after expiry and still expired
    assert broken["state"] == "auth_expired" and broken["source"] == "claude_credentials"


def test_claude_future_expiry_is_reported_but_harmless(tmp_path):
    credentials(tmp_path, NOW + 3600)
    claude_session(tmp_path, [c_user(NOW - 600), c_assistant(NOW - 590, stop="end_turn")])
    result = claude(tmp_path)
    assert result["state"] == "idle" and result["auth_expires_at"] == iso(NOW + 3600).replace(".000Z", "Z")


def test_claude_other_api_error_is_error(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 100), c_assistant(NOW - 90, stop="stop_sequence", isApiErrorMessage=True,
                                                             error="rate_limit")])
    result = claude(tmp_path)
    assert result["state"] == "error" and result["label"] == "Error"
    assert "rate_limit" not in json.dumps(result)                     # no value from the file reaches the answer


def test_claude_prose_in_the_error_field_is_ignored_not_echoed(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 100), c_assistant(NOW - 90, stop="stop_sequence", isApiErrorMessage=True,
                                                             error=SECRET)])
    result = claude(tmp_path)
    assert result["state"] == "error" and SECRET not in json.dumps(result)


def test_claude_unknown_without_sessions_and_subagent_files_are_not_sessions(tmp_path):
    assert claude(tmp_path)["state"] == "unknown"
    write_jsonl(paths_for(tmp_path)["claude_projects"] / "proj" / "uuid" / "subagents" / "agent.jsonl",
                [c_user(NOW - 5)])
    assert claude(tmp_path)["state"] == "unknown"


def test_claude_newest_file_wins(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 5000), c_assistant(NOW - 4990, stop="end_turn")], name="old.jsonl")
    claude_session(tmp_path, [c_user(NOW - 10)], name="new.jsonl")
    assert claude(tmp_path)["state"] == "working"


def test_credentials_reader_only_returns_expires_at(tmp_path):
    path = credentials(tmp_path, NOW + 10)
    assert ls.read_expires_at(path) == pytest.approx(NOW + 10)
    path.write_text("not json", encoding="utf-8")
    assert ls.read_expires_at(path) is None
    assert ls.read_expires_at(tmp_path / "missing.json") is None


# ---- Codex ------------------------------------------------------------------------------------------------------

def x_event(ts, kind, **payload):
    return {"timestamp": iso(ts), "type": "event_msg", "payload": {"type": kind, **payload}}


def x_item(ts, kind, **payload):
    return {"timestamp": iso(ts), "type": "response_item", "payload": {"type": kind, **payload}}


def codex_session(tmp_path, lines, day="2027/01/15", name="rollout-2027-01-15T10-00-00-aaaa.jsonl", mtime=None):
    return write_jsonl(paths_for(tmp_path)["codex_sessions"] / day / name, lines, mtime)


def codex(tmp_path, now=NOW):
    return ls.codex_state(paths_for(tmp_path)["codex_sessions"], now)


def test_codex_working_idle_and_stale_turns(tmp_path):
    codex_session(tmp_path, [x_event(NOW - 300, "task_started"), x_item(NOW - 20, "function_call", call_id="c1",
                                                                       arguments=SECRET)])
    assert codex(tmp_path)["state"] == "working"

    codex_session(tmp_path, [x_event(NOW - 900, "task_started"), x_event(NOW - 700, "agent_message", message=SECRET),
                             x_event(NOW - 600, "task_complete", last_agent_message=SECRET)],
                  name="rollout-2027-01-15T11-00-00-bbbb.jsonl", mtime=NOW)
    idle = codex(tmp_path)
    assert idle["state"] == "idle" and idle["last_activity_ts"] == pytest.approx(NOW - 600)

    codex_session(tmp_path, [x_event(NOW - 9000, "task_started")], name="rollout-2027-01-15T12-00-00-cccc.jsonl",
                  mtime=NOW + 1)
    stale = codex(tmp_path)
    assert stale["state"] == "idle" and "probablemente" in stale["detail"]


def test_codex_without_turn_markers_depends_on_recency(tmp_path):
    codex_session(tmp_path, [x_item(NOW - 30, "reasoning"), x_event(NOW - 20, "token_count")])
    assert codex(tmp_path)["state"] == "working"
    codex_session(tmp_path, [x_item(NOW - 3000, "reasoning")], name="rollout-2027-01-15T11-00-00-bbbb.jsonl",
                  mtime=NOW + 1)
    assert codex(tmp_path)["state"] == "idle"


def test_codex_waiting_for_approval_until_the_agent_goes_on(tmp_path):
    base = [x_event(NOW - 100, "task_started"), x_item(NOW - 60, "function_call", call_id="c1"),
            x_event(NOW - 50, "exec_approval_request", call_id="c1", command=SECRET)]
    codex_session(tmp_path, base)
    assert codex(tmp_path)["state"] == "waiting"
    codex_session(tmp_path, base + [x_item(NOW - 10, "function_call_output", call_id="c1", output=SECRET)],
                  name="rollout-2027-01-15T11-00-00-bbbb.jsonl", mtime=NOW)
    assert codex(tmp_path)["state"] == "working"


def test_codex_failed_turn_is_error_and_aborted_turn_is_idle(tmp_path):
    codex_session(tmp_path, [x_event(NOW - 100, "task_started"), x_event(NOW - 50, "error", message=SECRET)])
    assert codex(tmp_path)["state"] == "error"
    codex_session(tmp_path, [x_event(NOW - 100, "task_started"), x_event(NOW - 50, "turn_aborted")],
                  name="rollout-2027-01-15T11-00-00-bbbb.jsonl", mtime=NOW)
    assert codex(tmp_path)["state"] == "idle"


def test_codex_picks_the_newest_file_across_day_folders_and_unknown_when_none(tmp_path):
    assert codex(tmp_path)["state"] == "unknown"
    codex_session(tmp_path, [x_event(NOW - 5000, "task_started"), x_event(NOW - 4900, "task_complete")], day="2027/01/10",
                  name="rollout-a.jsonl")
    codex_session(tmp_path, [x_event(NOW - 100, "task_started")], day="2027/01/15", name="rollout-b.jsonl")
    codex_session(tmp_path, [x_event(NOW - 9000, "task_started")], day="2027/01/01", name="other.jsonl", mtime=NOW + 99)
    assert codex(tmp_path)["state"] == "working"                       # "other.jsonl" is not a rollout


# ---- Cursor -----------------------------------------------------------------------------------------------------

def launch_state(tmp_path, **fields):
    doc = {"schema_version": 1, "status": "completed", "started_at_utc": iso(NOW - 700), "completed_at_utc": iso(NOW - 600),
           "cli_pid": 4242, "exit_code": 0, "error": None, "stdout_log": SECRET}
    doc.update(fields)
    path = paths_for(tmp_path)["cursor_dir"] / "launch-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def cursor_log(tmp_path, lines, mtime, name="run.jsonl"):
    return write_jsonl(paths_for(tmp_path)["cursor_dir"] / "logs" / name, lines, mtime)


def u_event(kind, ts=None, **extra):
    event = {"type": kind, "session_id": "s", **extra}
    if ts is not None:
        event["timestamp_ms"] = int(ts * 1000)
    return event


def cursor(tmp_path, now=NOW, alive=lambda pid: pid == 4242):
    return ls.cursor_state(paths_for(tmp_path)["cursor_dir"], now, alive)


def test_cursor_completed_run_is_idle_since_its_completion(tmp_path):
    launch_state(tmp_path)
    cursor_log(tmp_path, [u_event("assistant", message={"content": [{"type": "text", "text": SECRET}]}),
                          u_event("result", subtype="success", is_error=False, result=SECRET)], mtime=NOW - 601)
    result = cursor(tmp_path)
    assert result["state"] == "idle" and result["source"] == "cursor_launch_state"
    assert result["last_activity_ts"] == pytest.approx(NOW - 600) and result["exit_code"] == 0


def test_cursor_running_with_a_live_process_is_working_and_a_dead_one_is_an_error(tmp_path):
    launch_state(tmp_path, status="running", completed_at_utc=None, exit_code=None)
    cursor_log(tmp_path, [u_event("thinking", NOW - 3000, subtype="delta", text=SECRET)], mtime=NOW - 3000)
    assert cursor(tmp_path)["state"] == "working"
    dead = cursor(tmp_path, alive=lambda pid: False)
    assert dead["state"] == "error" and "proceso" in dead["detail"]
    fresh_log = cursor_log(tmp_path, [u_event("tool_call", NOW - 10, subtype="started", tool_call={"x": SECRET})],
                           mtime=NOW - 10, name="run2.jsonl")
    assert fresh_log.exists() and cursor(tmp_path, alive=lambda pid: False)["state"] == "working"


def test_cursor_failed_run_is_error_with_exit_code(tmp_path):
    launch_state(tmp_path, status="failed", exit_code=1, error="boom")
    result = cursor(tmp_path)
    assert result["state"] == "error" and "(código 1)" in result["detail"] and "boom" not in json.dumps(result)
    launch_state(tmp_path, status="completed", exit_code=3)
    assert cursor(tmp_path)["state"] == "error"


def test_cursor_awaiting_input_is_waiting(tmp_path):
    launch_state(tmp_path, status="awaiting_input", completed_at_utc=None, exit_code=None)
    assert cursor(tmp_path)["state"] == "waiting"


def test_cursor_new_log_activity_after_completion_is_working(tmp_path):
    launch_state(tmp_path)
    cursor_log(tmp_path, [u_event("tool_call", NOW - 15, subtype="started", tool_call={"x": SECRET})], mtime=NOW - 15)
    assert cursor(tmp_path)["state"] == "working"


def test_cursor_only_logs_and_nothing_at_all(tmp_path):
    assert cursor(tmp_path)["state"] == "unknown"
    cursor_log(tmp_path, [u_event("tool_call", NOW - 10, subtype="started")], mtime=NOW - 10)
    assert cursor(tmp_path)["state"] == "working"
    cursor_log(tmp_path, [u_event("result", subtype="success", is_error=False)], mtime=NOW - 500)
    assert cursor(tmp_path)["state"] == "idle"
    cursor_log(tmp_path, [u_event("result", subtype="error", is_error=True)], mtime=NOW - 400, name="run2.jsonl")
    assert cursor(tmp_path)["state"] == "error"


# ---- Faustus, paths, helpers ------------------------------------------------------------------------------------

def test_faustus_state_comes_from_the_verified_native_session():
    assert ls.faustus_state({"state": "working"})["state"] == "working"
    assert ls.faustus_state({"state": "idle"})["state"] == "idle"
    assert ls.faustus_state(None, "available")["state"] == "unknown"
    assert ls.faustus_state(None, "unavailable")["state"] == "unknown"


def test_paths_can_be_overridden_by_environment_and_arguments(tmp_path, monkeypatch):
    p = paths_for(tmp_path)
    monkeypatch.setenv("HOARD_LEADER_CLAUDE_PROJECTS", str(p["claude_projects"]))
    monkeypatch.setenv("HOARD_LEADER_CLAUDE_CREDENTIALS", str(p["claude_credentials"]))
    monkeypatch.setenv("HOARD_LEADER_CODEX_SESSIONS", str(p["codex_sessions"]))
    monkeypatch.setenv("HOARD_LEADER_CURSOR_DIR", str(p["cursor_dir"]))
    claude_session(tmp_path, [c_user(NOW - 5)])
    assert ls.read_leader_states(NOW)["claude"]["state"] == "working"
    assert ls.read_leader_states(NOW)["codex"]["state"] == "unknown"
    other = tmp_path / "other"
    assert ls.read_leader_states(NOW, paths={"claude_projects": other})["claude"]["state"] == "unknown"


def test_default_pid_alive_sees_this_process_and_not_a_missing_one():
    assert ls.default_pid_alive(os.getpid()) is True
    assert ls.default_pid_alive(0) is False and ls.default_pid_alive("x") is False
    assert ls.default_pid_alive(2 ** 22 + 12345) is False


def test_a_reader_that_blows_up_degrades_to_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "codex_state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ls.read_leader_states(NOW, paths=paths_for(tmp_path))["codex"]["state"] == "unknown"


# ---- idle with work ---------------------------------------------------------------------------------------------

def tasks_for_flags():
    return [{"id": 1, "owner": "claude", "status": "in_progress"},
            {"id": 2, "owner": "codex-sparks", "status": "claimed"},
            {"id": 3, "owner": "cursor", "status": "review"},
            {"id": 4, "owner": "faustus", "status": "done"},
            {"id": 5, "owner": "Claude", "status": "changes"},
            {"id": 6, "owner": None, "status": "claimed"},
            {"id": 7, "owner": "codex-investiga", "status": "open"}]


def test_open_task_ids_match_the_leader_agent_and_its_prefixed_agents():
    tasks = tasks_for_flags()
    assert ls.open_task_ids(tasks, "claude", ["claude"]) == [1, 5]
    assert ls.open_task_ids(tasks, "codex", ["codex-investiga"]) == [2]
    assert ls.open_task_ids(tasks, "cursor", ["cursor"]) == []            # in review: the reviewer's turn
    assert ls.open_task_ids(tasks, "faustus", ["faustus"]) == []


def test_idle_expired_or_failed_leaders_with_open_tasks_are_flagged(tmp_path):
    claude_session(tmp_path, [c_user(NOW - 900), c_assistant(NOW - 800, stop="end_turn")])
    codex_session(tmp_path, [x_event(NOW - 20, "task_started")])
    launch_state(tmp_path, status="failed", exit_code=2)
    states = ls.build_leader_states(LEADERS, tasks=tasks_for_flags(), now=NOW, paths=paths_for(tmp_path),
                                    faustus_native_session={"state": "idle"}, pid_alive=lambda pid: False)
    assert states["claude"]["state"] == "idle" and states["claude"]["idle_with_work"] is True
    assert states["claude"]["open_tasks"] == [1, 5] and "2 tarea(s) abierta(s)" in states["claude"]["detail"]
    assert states["codex"]["state"] == "working" and states["codex"]["idle_with_work"] is False
    assert states["codex"]["open_tasks"] == [2]
    assert states["cursor"]["state"] == "error" and states["cursor"]["idle_with_work"] is False   # no open task
    assert states["faustus"]["idle_with_work"] is False

    tasks = tasks_for_flags() + [{"id": 8, "owner": "cursor", "status": "in_progress"}]
    again = ls.build_leader_states(LEADERS, tasks=tasks, now=NOW, paths=paths_for(tmp_path),
                                   faustus_native_session=None, pid_alive=lambda pid: False)
    assert again["cursor"]["idle_with_work"] is True                       # failed counts as needing attention


def test_a_leader_active_in_the_agora_is_working_even_with_idle_local_files(tmp_path):
    """A session running elsewhere (another PC, the cloud) writes no local
    files; its Ágora heartbeat or posts are what show it is on the job."""
    claude_session(tmp_path, [c_user(NOW - 9000), c_assistant(NOW - 8900, stop="end_turn")])
    agents = [{"id": "claude", "last_seen": NOW - 120}, {"id": "cursor-agora", "last_seen": NOW - 60},
              {"id": "codex", "last_seen": NOW - 7200}]
    states = ls.build_leader_states(LEADERS, tasks=tasks_for_flags(), now=NOW, paths=paths_for(tmp_path),
                                    agents=agents, pid_alive=lambda pid: False)
    assert states["claude"]["state"] == "working" and states["claude"]["source"] == "agora"
    assert states["claude"]["idle_with_work"] is False and states["claude"]["open_tasks"] == [1, 5]
    assert states["cursor"]["state"] == "working"            # cursor-agora counts for Cursor
    assert states["codex"]["source"] != "agora"              # two hours ago is not "now"
    # without the heartbeat, the same files say idle with work
    quiet = ls.build_leader_states(LEADERS, tasks=tasks_for_flags(), now=NOW, paths=paths_for(tmp_path),
                                   pid_alive=lambda pid: False)
    assert quiet["claude"]["state"] == "idle" and quiet["claude"]["idle_with_work"] is True


def test_unknown_or_working_leaders_are_never_flagged(tmp_path):
    states = ls.build_leader_states(LEADERS, tasks=tasks_for_flags(), now=NOW, paths=paths_for(tmp_path))
    assert states["claude"]["state"] == "unknown" and states["claude"]["idle_with_work"] is False


# ---- privacy ----------------------------------------------------------------------------------------------------

def test_no_message_content_or_token_ever_reaches_any_state(tmp_path):
    credentials(tmp_path, NOW - 3600)
    claude_session(tmp_path, [c_user(NOW - 600), c_assistant(NOW - 590, [c_tool("q", "AskUserQuestion")], stop="tool_use"),
                              c_assistant(NOW - 580, stop="stop_sequence", isApiErrorMessage=True, error=SECRET)])
    codex_session(tmp_path, [x_event(NOW - 50, "task_started"), x_event(NOW - 40, "user_message", message=SECRET),
                             x_item(NOW - 30, "message", role="assistant", content=[{"type": "output_text", "text": SECRET}]),
                             x_event(NOW - 20, "exec_approval_request", command=SECRET)])
    launch_state(tmp_path, status="failed", exit_code=1, error=SECRET)
    cursor_log(tmp_path, [u_event("thinking", NOW - 9, subtype="delta", text=SECRET),
                          u_event("assistant", message={"content": [{"type": "text", "text": SECRET}]}),
                          u_event("result", subtype="success", is_error=False, result=SECRET)], mtime=NOW - 8)
    states = ls.build_leader_states(LEADERS, tasks=tasks_for_flags(), now=NOW, paths=paths_for(tmp_path),
                                    faustus_native_session={"state": "idle"}, pid_alive=lambda pid: True)
    dump = json.dumps(states, ensure_ascii=False)
    assert "TOP-SECRET" not in dump and "do-not-leak" not in dump
    assert "SECRET-ACCESS" not in dump and "SECRET-REFRESH" not in dump and "sk-ant" not in dump
    assert str(tmp_path) not in dump                                       # no file path either
    assert {s["state"] for s in states.values()} <= set(ls.STATES)


# ---- console facet: API, MCP tool, overview ---------------------------------------------------------------------

def _facet_with_files(tmp_path, monkeypatch):
    facet = _facet(tmp_path / "hub")
    now = time.time()
    p = paths_for(tmp_path)
    for key, var in (("claude_projects", "HOARD_LEADER_CLAUDE_PROJECTS"), ("claude_credentials", "HOARD_LEADER_CLAUDE_CREDENTIALS"),
                     ("codex_sessions", "HOARD_LEADER_CODEX_SESSIONS"), ("cursor_dir", "HOARD_LEADER_CURSOR_DIR")):
        monkeypatch.setenv(var, str(p[key]))
    claude_session(tmp_path, [c_user(now - 4000), c_assistant(now - 3900, stop="end_turn")])
    codex_session(tmp_path, [x_event(now - 5, "task_started")])
    monkeypatch.setattr(facet, "_faustus_current_settings", lambda: {"current_model": None, "current_effort": None,
                                                                         "native_session": {"state": "idle"}})
    facet._agora().tasks = lambda a=None: {"ok": True, "tasks": [
        {"id": 31, "owner": "claude", "status": "in_progress"}, {"id": 32, "owner": "codex-investiga", "status": "claimed"}]}
    return facet


def test_overview_rows_carry_the_live_state_and_the_idle_with_work_flag(tmp_path, monkeypatch):
    facet = _facet_with_files(tmp_path, monkeypatch)
    leaders = {row["id"]: row for row in facet.overview()["leaders"]}
    assert leaders["claude"]["live_state"]["state"] == "idle" and leaders["claude"]["idle_with_work"] is True
    assert leaders["claude"]["live_state"]["open_tasks"] == [31]
    assert leaders["codex"]["live_state"]["state"] == "working" and leaders["codex"]["idle_with_work"] is False
    assert leaders["cursor"]["live_state"]["state"] == "unknown"
    assert leaders["faustus"]["live_state"]["state"] == "idle"
    assert "TOP-SECRET" not in json.dumps(leaders["claude"]["live_state"])


def test_leaders_route_and_mcp_tool_return_the_same_states(tmp_path, monkeypatch):
    facet = _facet_with_files(tmp_path, monkeypatch)
    response = facet.get(_request("GET", "/api/agora/console/leaders"))
    assert response["ok"] is True and [row["id"] for row in response["leaders"]] == [x[0] for x in LEADERS]
    claude_row = next(row for row in response["leaders"] if row["id"] == "claude")
    assert claude_row["agent_id"] == "claude" and claude_row["idle_with_work"] is True and claude_row["label"].startswith("Inactivo")

    denied = facet.get(_request("GET", "/api/agora/console/leaders", caller=None))
    assert denied["status"] == 401

    tool = next(t for t in facet.tools() if t["name"] == "hub_agora_console_leaders")
    assert tool["annotations"]["readOnlyHint"] is True
    via_mcp = facet.handlers()["hub_agora_console_leaders"]({})
    assert [r["state"] for r in via_mcp["leaders"]] == [r["state"] for r in response["leaders"]]


def test_mcp_dispatch_reaches_the_leaders_tool(tmp_path, monkeypatch):
    facet = _facet_with_files(tmp_path, monkeypatch)
    facet.hub.facets = [facet]
    response = call(facet.hub, "hub_agora_console_leaders", {})
    assert response["ok"] is True and {row["id"]: row["state"] for row in response["leaders"]}["codex"] == "working"


def test_leaders_work_without_agora_tasks_available(tmp_path, monkeypatch):
    facet = _facet_with_files(tmp_path, monkeypatch)
    facet._agora().tasks = lambda a=None: (_ for _ in ()).throw(RuntimeError("db closed"))
    rows = facet.leader_states()["leaders"]
    assert all(row["idle_with_work"] is False and row["open_tasks"] == [] for row in rows)


# ---- CLI --------------------------------------------------------------------------------------------------------

def test_cli_prints_one_block_per_leader_and_marks_idle_with_work(capsys):
    CLI.print_leaders({"leaders": [
        {"id": "claude", "name": "Claude", "label": "Inactivo desde 17:42", "state": "idle", "open_tasks": [31, 33],
         "idle_with_work": True, "detail": "El último turno terminó.", "source": "claude_session"},
        {"id": "codex", "name": "Codex", "label": "Trabajando", "state": "working", "open_tasks": [],
         "idle_with_work": False, "detail": "Turno en curso.", "source": "codex_session"}]})
    out = capsys.readouterr().out
    assert "Claude" in out and "Inactivo desde 17:42" in out and "#31, #33" in out and "INACTIVO CON TRABAJO" in out
    codex_line = next(line for line in out.splitlines() if line.startswith("Codex"))
    assert "Trabajando" in codex_line and "INACTIVO" not in codex_line


def test_cli_leaders_command_reads_the_console_route(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(CLI, "call", lambda method, path, body=None, timeout=150.0: (
        seen.append((method, path)) or {"ok": True, "leaders": [
            {"id": "cursor", "name": "Cursor", "label": "Error", "state": "error", "open_tasks": [], "idle_with_work": False,
             "detail": "La última ejecución falló (código 1).", "source": "cursor_launch_state"}]}))
    assert CLI.main(["leaders"]) == 0                       # a read: no --as needed
    assert seen == [("GET", "/api/agora/console/leaders")]
    assert "Cursor" in capsys.readouterr().out
