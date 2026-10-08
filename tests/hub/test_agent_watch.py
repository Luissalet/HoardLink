"""The Ágora's passive observer of the coding agents (agent_watch.py): incremental tail parsing of the Codex, Cursor
and Claude Code transcripts, the derived state of each session, binding to Ágora agent ids, the pending-questions
queue and the privacy of the snippets. Every transcript here is synthetic (see _transcripts.py)."""

from __future__ import annotations

import json
import os

import pytest

from hoard_link.hub import agent_watch as aw
from hoard_link.hub.agent_watch import snippet

from ._transcripts import (CLAUDE_ID, CODEX_ID, CUR_END, NOW, clock, claude_file, cl_asst, cl_result, cl_text, cl_use,  # noqa: F401
                           cl_user, codex_file, cursor_file, cur_text, cur_tool, cur_user, cx, cx_done, cx_exec, cx_head,
                           cx_msg, cx_out, cx_start, iso, one, put, roots, watch)

# ---- privacy ----------------------------------------------------------------------------------------------------

def test_snippet_limits_length_and_drops_secret_lines():
    assert snippet("a" * 500).endswith("…") and len(snippet("a" * 500)) == aw.SNIPPET
    text = "run it\nAuthorization: Bearer abcdef123456\nexport API_KEY=sk-live-1234567890abcdef\npassword = hunter2\nnext step"
    assert snippet(text) == "run it next step"
    for secret in ("curl -H 'Authorization: Bearer x1y2z3w4'", "token: abc", "--password hunter2", "ghp_" + "a" * 30,
                   "see AKIA" + "A" * 16, "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "client_secret=abc"):
        assert snippet(secret) == "", secret
    assert snippet("the token count is 5 and password reset was added") != ""    # prose is not a secret
    assert snippet(None) == ""


def test_views_never_carry_reasoning_or_secrets(roots, clock):
    lines = cx_head(NOW - 90, prompt="Quita el bug\nAuthorization: Bearer topsecret999") + [
        cx_start(NOW - 60),
        cx(NOW - 55, "response_item", {"type": "reasoning", "summary": [], "encrypted_content": "ENCRYPTEDBLOB"}),
        cx_exec(NOW - 50, "c1", "curl -H 'Authorization: Bearer abc12345678' https://x"),
        cx_msg(NOW - 40, "assistant", "Mirando.\npassword=hunter2"),
    ]
    codex_file(roots["codex"], lines, NOW - 40)
    claude_file(roots["claude"], [cl_user(NOW - 30, "hola"),
                                  cl_asst(NOW - 20, {"type": "thinking", "thinking": "SECRETTHOUGHT", "signature": "SIGBLOB"},
                                          cl_text("ok"))], NOW - 20)
    blob = json.dumps(watch(roots, clock).view(force=True))
    for leak in ("ENCRYPTEDBLOB", "SECRETTHOUGHT", "SIGBLOB", "topsecret999", "hunter2", "abc12345678"):
        assert leak not in blob
    assert "Quita el bug" in blob


# ---- incremental tail -------------------------------------------------------------------------------------------

def test_tail_by_offset_large_file_and_appended_lines(roots, clock):
    filler = [cx(NOW - 5000 + i, "event_msg", {"type": "token_count", "info": None, "pad": "x" * 280}) for i in range(20000)]
    lines = cx_head(NOW - 6000) + filler + [cx_start(NOW - 100), cx_exec(NOW - 90, "c1", "ls")]
    path = codex_file(roots["codex"], lines, NOW - 90)
    size = path.stat().st_size
    assert size > 5 * 1024 * 1024
    w = watch(roots, clock)
    s = one(w)
    f = w._files[str(path)]
    assert f.offset == size
    assert f.session.lines < (aw.TAIL_BYTES // 100)                 # only the tail was parsed, never the 5 MB
    assert s["title"] == "Arregla el bucle del agente"               # ...but the title came from the head
    assert s["cwd"].endswith("Faustus") and s["workspace"] == "Faustus"
    assert s["state"] == "tool" and s["tool"]["name"] == "exec"
    seen = f.session.lines
    put(path, [cx_out(NOW - 80, "c1"), cx_done(NOW - 70)], NOW - 70, append=True)
    clock.t = NOW + 1
    s = one(w)
    assert f.session.lines == seen + 2 and f.offset == path.stat().st_size      # exactly the appended lines
    assert s["state"] == "idle" and s["last_text"] == "Hecho."


def test_partial_last_line_waits_for_its_newline(roots, clock):
    path = codex_file(roots["codex"], cx_head(NOW - 60) + [cx_start(NOW - 50)], NOW - 50)
    w = watch(roots, clock)
    assert one(w)["state"] == "working"
    line = json.dumps(cx_done(NOW - 10))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[:40])                                         # the writer is half way through a line
    os.utime(path, (NOW - 10, NOW - 10))
    clock.t = NOW
    assert one(w)["state"] == "working"
    f = w._files[str(path)]
    assert f.offset < path.stat().st_size                           # the fragment was not consumed
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[40:] + "\n")
    os.utime(path, (NOW - 9, NOW - 9))
    assert one(w)["state"] == "idle"
    # a complete JSON object the writer has not terminated yet is taken at once
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(cx_start(NOW - 5)))
    os.utime(path, (NOW - 5, NOW - 5))
    assert one(w)["state"] == "working"


def test_truncation_starts_again_and_garbage_lines_are_ignored(roots, clock):
    path = codex_file(roots["codex"], cx_head(NOW - 300) + [cx_start(NOW - 200), "not json", "{broken", "[1,2]", "",
                                                            cx_exec(NOW - 100, "c1", "ls")], NOW - 100)
    w = watch(roots, clock)
    assert one(w)["state"] == "tool"
    put(path, [cx(NOW - 20, "session_meta", {"cwd": "D:\\other", "originator": "x"}), cx_start(NOW - 10)], NOW - 10)
    s = one(w)
    assert s["state"] == "working" and s["tool"] is None and s["cwd"] == "D:\\other"


def test_discovery_window_and_file_cap(roots, clock):
    codex_file(roots["codex"], cx_head(NOW - 9e5), NOW - 3 * 86400)                    # older than 48 h
    for i in range(5):
        u = f"019d0000-0000-7000-8000-00000000000{i}"
        codex_file(roots["codex"], cx_head(NOW - 100) + [cx_start(NOW - 50 + i)], NOW - 50 + i, uuid=u)
    w = watch(roots, clock, max_files=3)
    v = w.view(force=True)
    assert [s["session_id"][-1] for s in v["sessions"]] == ["4", "3", "2"]            # newest three, the old one never
    assert len(watch(roots, clock).view(force=True)["sessions"]) == 5


def test_refresh_is_throttled_but_view_is_cheap(roots, clock):
    path = codex_file(roots["codex"], cx_head(NOW - 60) + [cx_start(NOW - 50)], NOW - 50)
    w = watch(roots, clock)
    assert w.view()["sessions"][0]["state"] == "working"
    put(path, [cx_done(NOW - 1)], NOW - 1, append=True)
    clock.t = NOW + 2                                                # < 5 s since the last refresh: cached
    assert w.view()["sessions"][0]["state"] == "working"
    clock.t = NOW + 6
    assert w.view()["sessions"][0]["state"] == "idle"


# ---- state per engine -------------------------------------------------------------------------------------------

def test_codex_states(roots, clock):
    head = cx_head(NOW - 4000)
    path = codex_file(roots["codex"], head + [cx_start(NOW - 30)], NOW - 30)
    w = watch(roots, clock)
    s = one(w)
    assert s["state"] == "working" and s["since"] == NOW - 30 and s["turn_started_at"] == NOW - 30
    assert s["engine"] == "codex" and s["key"] == f"codex:{CODEX_ID}" and s["originator"] == "codex_work_desktop"

    put(path, [cx_exec(NOW - 20, "c1", "pytest -q tests/hub")], NOW - 20, append=True)
    s = one(w)
    assert s["state"] == "tool" and s["tool"]["name"] == "exec" and s["tool"]["since"] == NOW - 20
    assert s["tool"]["input"] == "pytest -q tests/hub" and s["last_tool"] == "exec"

    put(path, [cx_out(NOW - 10, "c1"), cx_msg(NOW - 9, "assistant", "Va bien")], NOW - 9, append=True)
    s = one(w)
    assert s["state"] == "working" and s["last_text"] == "Va bien"

    put(path, [cx_done(NOW - 5, text="Todo listo")], NOW - 5, append=True)
    s = one(w)
    assert s["state"] == "idle" and s["since"] == NOW - 5 and s["last_text"] == "Todo listo"

    put(path, [cx_start(NOW - 3, "t2")], NOW - 3, append=True)
    assert one(w)["state"] == "working"
    clock.t = NOW + 31 * 60                                           # nothing written for 31 minutes with a turn open
    s = one(w)
    assert s["state"] == "stale" and s["since"] == NOW - 3


def test_codex_tokens_of_the_turn(roots, clock):
    def tc(t, total, last):
        return cx(t, "event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": total},
                                                                     "last_token_usage": {"total_tokens": last}}})
    lines = cx_head(NOW - 100) + [cx_start(NOW - 90, "t1"), tc(NOW - 80, 1000, 1000), cx_done(NOW - 70, "t1"),
                                  cx_start(NOW - 60, "t2"), tc(NOW - 50, 1400, 400), tc(NOW - 40, 2100, 700)]
    codex_file(roots["codex"], lines, NOW - 40)
    assert one(watch(roots, clock))["tokens"] == 1100                # 2100 now - 1000 at the start of turn t2


def test_codex_approvals_and_questions_are_pending_until_answered(roots, clock):
    path = codex_file(roots["codex"], cx_head(NOW - 200) + [
        cx_start(NOW - 100), cx_exec(NOW - 90, "c1", "rm -rf build"),
        cx(NOW - 89, "event_msg", {"type": "exec_approval_request", "call_id": "c1", "command": ["rm", "-rf", "build"],
                                   "reason": "needs to leave the sandbox"})], NOW - 89)
    w = watch(roots, clock)
    s = one(w)
    assert s["state"] == "waiting" and s["since"] == NOW - 89
    q = s["questions"][0]
    assert q["kind"] == "approval" and q["confidence"] == "explicit" and q["what"] == "rm -rf build"
    assert s["tool"]["name"] == "exec"
    clock.t = NOW + 3 * 3600                                          # an approval never goes stale by itself
    assert one(w)["state"] == "waiting"
    clock.t = NOW
    put(path, [cx_out(NOW - 80, "c1")], NOW - 80, append=True)       # the agent went on: it was answered
    assert one(w)["state"] == "working"

    ask = json.dumps({"questions": [{"id": "db", "question": "¿Qué base de datos uso?"}]})
    put(path, [cx(NOW - 70, "response_item", {"type": "function_call", "name": "request_user_input", "arguments": ask,
                                                "call_id": "q1"})], NOW - 70, append=True)
    s = one(w)
    assert s["state"] == "waiting" and s["questions"][0]["kind"] == "question" and "base de datos" in s["questions"][0]["what"]
    put(path, [cx(NOW - 60, "response_item", {"type": "function_call_output", "call_id": "q1", "output": "SQLite"})],
        NOW - 60, append=True)
    assert one(w)["state"] == "working"

    put(path, [cx(NOW - 50, "event_msg", {"type": "elicitation_request", "id": "e1", "message": "Elige una cuenta"})],
        NOW - 50, append=True)
    assert one(w)["questions"][0]["what"] == "Elige una cuenta"
    put(path, [cx(NOW - 40, "event_msg", {"type": "elicitation_response", "id": "e1"})], NOW - 40, append=True)
    assert one(w)["state"] == "working"
    put(path, [cx(NOW - 30, "event_msg", {"type": "request_user_input", "id": "r1", "question": "¿Sigo?"}), cx_done(NOW - 20)],
        NOW - 20, append=True)
    assert one(w)["state"] == "idle"                                  # the turn ended: nothing is pending any more


def test_cursor_states_use_mtime_as_the_clock(roots, clock):
    path = cursor_file(roots["cursor"], [cur_user("Revisa el README"), cur_text("Miro."), cur_tool("Shell", command="git status")],
                       NOW - 45)
    w = watch(roots, clock)
    s = one(w)
    assert s["engine"] == "cursor" and s["key"] == "cursor:abc-123" and s["title"] == "Revisa el README"
    assert s["state"] == "tool" and s["tool"]["name"] == "Shell" and s["tool"]["input"] == "git status"
    assert s["tool"]["since"] == NOW - 45 and s["last_activity"] == NOW - 45 and s["workspace"] == "Faustus"
    assert s["last_text"] == "Miro."

    put(path, [cur_text("Limpio."), CUR_END], NOW - 10, append=True)
    s = one(w)
    assert s["state"] == "idle" and s["last_text"] == "Limpio." and s["tool"] is None

    put(path, [cur_user("Otra cosa"), cur_text("Empiezo")], NOW - 5, append=True)
    s = one(w)
    assert s["state"] == "working" and s["title"] == "Revisa el README"
    clock.t = NOW + 40 * 60
    assert one(w)["state"] == "stale"


def test_claude_states(roots, clock):
    path = claude_file(roots["claude"], [cl_user(NOW - 300, "Implementa el observador"), cl_asst(NOW - 290, cl_text("Voy."))], NOW - 290)
    w = watch(roots, clock)
    s = one(w)
    assert s["engine"] == "claude" and s["key"] == f"claude:{CLAUDE_ID}" and s["cwd"] == "/home/claude/w/HoardLink"
    assert s["workspace"] == "HoardLink" and s["title"] == "Implementa el observador"
    assert s["state"] == "working" and s["since"] == NOW - 300

    put(path, [cl_asst(NOW - 20, cl_use("u1", "Bash", command="pytest -q"), stop="tool_use")], NOW - 20, append=True)
    s = one(w)
    assert s["state"] == "tool" and s["tool"]["name"] == "Bash" and s["tool"]["input"] == "pytest -q"   # 20 s: still running

    clock.t = NOW + 50                                                # 70 s without a result: probably a permission prompt
    s = one(w)
    assert s["state"] == "waiting" and s["questions"][0]["confidence"] == "heuristic"
    assert s["questions"][0]["kind"] == "approval" and s["questions"][0]["what"].startswith("Bash: pytest")
    assert s["since"] == NOW - 20

    put(path, [cl_result(NOW + 55, "u1"), cl_asst(NOW + 60, cl_text("Pasa."), stop="end_turn")], NOW + 60, append=True)
    clock.t = NOW + 70
    s = one(w)
    assert s["state"] == "idle" and s["last_text"] == "Pasa." and s["questions"] == []

    put(path, [cl_user(NOW + 80, "Sigue"), {"type": "attachment", "timestamp": iso(NOW + 90),
                                           "attachment": {"type": "hook_success", "hookEvent": "Stop"}}], NOW + 90, append=True)
    clock.t = NOW + 100
    assert one(w)["state"] == "idle"                                  # a Stop hook ends the turn too

    put(path, [cl_user(NOW + 100, "Otra"), cl_asst(NOW + 101, cl_use("u2", "Edit", file_path="a.py"), stop="tool_use")],
        NOW + 101, append=True)
    clock.t = NOW + 101 + 31 * 60
    assert one(w)["state"] == "stale"                                 # nothing for 31 min, whatever the tool was

    put(path, [{"type": "last-prompt", "lastPrompt": "x"}, cl_user(NOW + 4000, "[Request interrupted by user]")], NOW + 4000, append=True)
    clock.t = NOW + 4010
    assert one(w)["state"] == "idle"


def test_claude_ask_user_question_is_explicit_and_sidechains_are_ignored(roots, clock):
    claude_file(roots["claude"], [cl_user(NOW - 100, "Hazlo"),
                                  {**cl_asst(NOW - 90, cl_use("s1", "Bash", command="sleep 9"), stop="tool_use"), "isSidechain": True},
                                  cl_asst(NOW - 5, cl_use("a1", "AskUserQuestion",
                                                          questions=[{"question": "¿Rama nueva o main?"}]), stop="tool_use")],
                NOW - 5)
    s = one(watch(roots, clock))
    assert s["state"] == "waiting"                                    # no 60 s needed: asking is asking
    q = s["questions"][0]
    assert q["kind"] == "question" and q["confidence"] == "explicit" and q["what"] == "¿Rama nueva o main?"
    assert q["since"] == NOW - 5


def test_unknown_turn_in_the_tail_uses_recency(roots, clock):
    codex_file(roots["codex"], [cx_msg(NOW - 20, "assistant", "sigo")], NOW - 20)
    w = watch(roots, clock)
    assert one(w)["state"] == "working"
    clock.t = NOW + 600
    assert one(w)["state"] == "idle"


# ---- binding ----------------------------------------------------------------------------------------------------

def test_agents_in_call():
    f = aw.agents_in_call
    assert f("exec", "python scripts/agora.py --as codex-sparks board") == ["codex-sparks"]
    assert f("Bash", "AGORA_AGENT=cursor python scripts/agora.py inbox") == ["cursor"]
    assert f("mcp__hoard-hub__hub_agora_heartbeat", {"agent": "claude", "doing": "x"}) == ["claude"]
    assert f("hub_agora_handover", json.dumps({"agent": "a-one", "from_agent": "dead", "to_agent": "b-two"})) == ["a-one"]
    assert f("exec", 'await tools.hub_agora_post({\\"agent\\": \\"codex-relevo\\", \\"body\\": \\"x\\"})') == ["codex-relevo"]
    assert f("Bash", "python scripts/agora.py --as luis board") == []                      # never the person
    assert f("Bash", "python tool.py --as reviewer") == []                                 # not an Ágora call
    assert f("Bash", "python scripts/agora.py --as '$AGENT' board") == []                  # a placeholder is no id


def test_auto_binding_picks_the_most_frequent_agent_and_lists_the_rest(roots, clock):
    lines = cx_head(NOW - 300) + [cx_start(NOW - 200)]
    for i in range(3):
        lines.append(cx_exec(NOW - 190 + i, f"c{i}", "python scripts/agora.py --as codex-sparks hb 'x'"))
    lines.append(cx_exec(NOW - 150, "z", "python scripts/agora.py --as codex-relevo board"))
    codex_file(roots["codex"], lines, NOW - 150)
    claude_file(roots["claude"], [cl_user(NOW - 60, "hola"),
                                  cl_asst(NOW - 50, cl_use("m1", "mcp__hoard-hub__hub_agora_heartbeat", agent="claude", doing="x"),
                                          stop="tool_use")], NOW - 50)
    cursor_file(roots["cursor"], [cur_user("sin agente"), cur_text("hola")], NOW - 40)
    w = watch(roots, clock)
    v = w.view(force=True)
    by = {s["engine"]: s for s in v["sessions"]}
    assert by["codex"]["agent"] == "codex-sparks" and by["codex"]["binding"] == "inferred"
    assert by["claude"]["agent"] == "claude" and by["claude"]["binding"] == "inferred"
    assert by["cursor"]["agent"] is None
    assert set(v["agents"]) == {"codex-sparks", "claude"}
    assert [s["engine"] for s in v["unbound"]] == ["cursor"]
    assert v["agents"]["codex-sparks"]["session_key"] == f"codex:{CODEX_ID}" and v["agents"]["codex-sparks"]["binding"] == "inferred"


def test_explicit_binding_wins_persists_and_can_be_removed(roots, clock, tmp_path):
    codex_file(roots["codex"], cx_head(NOW - 300) + [cx_start(NOW - 200), cx_exec(NOW - 190, "c", "agora.py --as codex-sparks hb")], NOW - 190)
    cursor_file(roots["cursor"], [cur_user("sin agente"), cur_text("hola")], NOW - 40)
    w = watch(roots, clock, tmp_path)
    assert w.view(force=True)["agents"].keys() == {"codex-sparks"}
    r = w.bind(f"codex:{CODEX_ID}", "codex-relevo")
    assert r == {"ok": True, "session_key": f"codex:{CODEX_ID}", "agent": "codex-relevo", "binding": "explicit"}
    w.bind("cursor:abc-123", "cursor")
    v = w.view(force=True)
    assert set(v["agents"]) == {"codex-relevo", "cursor"} and v["agents"]["codex-relevo"]["binding"] == "explicit"
    assert v["unbound"] == []
    saved = json.loads((tmp_path / "agent_watch.json").read_text(encoding="utf-8"))
    assert saved["bindings"] == {f"codex:{CODEX_ID}": "codex-relevo", "cursor:abc-123": "cursor"}

    again = watch(roots, clock, tmp_path)                             # a restart keeps the bindings
    assert set(again.view(force=True)["agents"]) == {"codex-relevo", "cursor"}
    again.bind(f"codex:{CODEX_ID}", "")                               # unbinding falls back to the inference
    assert again.view(force=True)["agents"]["codex-sparks"]["binding"] == "inferred"
    assert json.loads((tmp_path / "agent_watch.json").read_text(encoding="utf-8"))["bindings"] == {"cursor:abc-123": "cursor"}

    for bad in ("Not An Id", "luis", "x"):
        with pytest.raises(aw.WatchError) as exc:
            again.bind("cursor:abc-123", bad)
        assert exc.value.status == 400
    with pytest.raises(aw.WatchError) as exc:
        again.bind("cursor:nope", "cursor")
    assert exc.value.status == 404
    with pytest.raises(aw.WatchError):
        again.bind("", "cursor")


def test_inferred_hints_survive_a_restart_even_if_the_tail_forgot_them(roots, clock, tmp_path):
    lines = cx_head(NOW - 300) + [cx_start(NOW - 200), cx_exec(NOW - 190, "c", "agora.py --as codex-sparks hb")]
    filler = [cx(NOW - 100, "event_msg", {"type": "token_count", "info": None, "pad": "x" * 300}) for _ in range(2000)]
    path = codex_file(roots["codex"], lines + filler, NOW - 100)       # the call is now further back than the 256 KB tail
    assert path.stat().st_size > aw.TAIL_BYTES * 2
    first = watch(roots, clock, tmp_path)
    assert first.view(force=True)["sessions"][0]["agent"] is None      # first sight reads only the tail...
    put(path, [cx_exec(NOW - 50, "d", "agora.py --as codex-sparks hb")], NOW - 50, append=True)
    assert first.view(force=True)["sessions"][0]["agent"] == "codex-sparks"
    put(path, [cx_out(NOW - 40, "d")], NOW - 40, append=True)
    second = watch(roots, clock, tmp_path)
    assert second.view(force=True)["sessions"][0]["agent"] == "codex-sparks"   # ...and what it learned is kept


def test_questions_queue_lists_agent_engine_what_and_since(roots, clock):
    codex_file(roots["codex"], cx_head(NOW - 300) + [
        cx_start(NOW - 200), cx_exec(NOW - 190, "c", "agora.py --as codex-sparks hb"),
        cx(NOW - 100, "event_msg", {"type": "exec_approval_request", "call_id": "x", "command": "git push"})], NOW - 100)
    claude_file(roots["claude"], [cl_user(NOW - 90, "x"), cl_asst(NOW - 80, cl_use("a", "AskUserQuestion", question="¿Sí o no?"), stop="tool_use")],
                NOW - 80)
    qs = watch(roots, clock).view(force=True)["questions"]
    assert [(q["engine"], q["agent"], q["kind"], q["what"], q["since"]) for q in qs] == [
        ("codex", "codex-sparks", "approval", "git push", NOW - 100), ("claude", None, "question", "¿Sí o no?", NOW - 80)]
    assert qs[0]["session_key"] == f"codex:{CODEX_ID}" and qs[0]["title"] == "Arregla el bucle del agente"


def test_agent_with_several_sessions_shows_the_one_that_needs_attention(roots, clock):
    for i, tail in enumerate((cx_done(NOW - 5), cx(NOW - 30, "event_msg", {"type": "exec_approval_request", "id": "a", "command": "ls"}))):
        u = f"019d0000-0000-7000-8000-00000000000{i}"
        codex_file(roots["codex"], cx_head(NOW - 100) + [cx_start(NOW - 50), cx_exec(NOW - 40, "c", "agora.py --as codex-sparks hb"), tail],
                   NOW - 5 if i == 0 else NOW - 30, uuid=u)
    a = watch(roots, clock).view(force=True)["agents"]["codex-sparks"]
    assert a["state"] == "waiting" and a["sessions"] == 2 and a["questions"] == 1
