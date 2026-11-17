"""Agent loop of an observed session: phases from transcript lines, steps, practices and the submit checklist."""
import time

from hoard_link.hub import tools
from hoard_link.hub.agent_loop import Loop, classify, judge_practices
from hoard_link.hub.agora_live import board_practices, practices_text, project

from ._hub_fakes import make_hub
from ._transcripts import (claude_file, cl_asst, cl_result, cl_text, cl_use, cl_user, clock, codex_file,  # noqa: F401
                           cx_done, cx_exec, cx_head, cx_out, cx_start, one, roots, watch)


def _marks(practices):
    return {p["id"]: p["state"] for p in practices}


def test_phases_follow_transcript_lines(roots, clock):
    now = clock.t
    claude_file(roots["claude"], [
        cl_user(now - 60, "Arregla el test"),
        cl_asst(now - 55, cl_text("Miro el fallo primero.")),
        cl_asst(now - 50, cl_use("u1", "Bash", command="python -m pytest tests -q")),
    ], now - 50)
    w = watch(roots, clock)
    loop = one(w)["loop"]
    assert (loop["phase"], loop["lap"], loop["tool"]) == ("act", 1, "Bash")
    assert [s["kind"] for s in loop["steps"]] == ["plan", "tool"]
    claude_file(roots["claude"], [
        cl_user(now - 60, "Arregla el test"),
        cl_asst(now - 55, cl_text("Miro el fallo primero.")),
        cl_asst(now - 50, cl_use("u1", "Bash", command="python -m pytest tests -q")),
        cl_result(now - 40, "u1"),
    ], now - 40)
    loop = one(w)["loop"]
    assert loop["phase"] == "observe" and loop["tool"] is None
    assert _marks(loop["practices"])["tests"] == "ok"
    claude_file(roots["claude"], [
        cl_user(now - 60, "Arregla el test"),
        cl_asst(now - 55, cl_text("Miro el fallo primero.")),
        cl_asst(now - 50, cl_use("u1", "Bash", command="python -m pytest tests -q")),
        cl_result(now - 40, "u1"),
        cl_asst(now - 30, cl_text("Pasa. Hago commit.")),
        cl_asst(now - 20, cl_use("u2", "Bash", command='git commit --author="Luissalet <l@x>" -m "Fix"')),
        cl_result(now - 10, "u2"),
        cl_asst(now - 5, cl_text("Hecho."), stop="end_turn"),
    ], now - 5)
    loop = one(w)["loop"]
    assert loop["phase"] == "done" and loop["lap"] == 3
    kinds = [s["kind"] for s in loop["steps"]]
    assert kinds == ["plan", "tool", "result", "plan", "tool", "result", "commit", "plan", "end"]
    assert _marks(loop["practices"])["commit"] == "ok"
    assert _marks(loop["practices"])["no_evidence"] == "ok"


def test_codex_results_are_not_graded_so_tests_stay_unconfirmed():
    loop = Loop()
    loop.turn_start(1)
    loop.tool_start("a", "exec", {"command": ["npm", "test"]}, 2)
    loop.tool_end("a", 3)
    assert _marks(loop.view()["practices"])["tests"] == "unknown"
    loop.text(4)
    assert loop.view()["phase"] == "plan" and loop.view()["lap"] == 2


def test_practices_fail_and_pending():
    marks = _marks(judge_practices([
        {"tool": "Bash", "command": "git add shot.png && git commit -m wip", "ok": True, "ended": True},
        {"tool": "Edit", "command": "E:/repo/FAUSTUS.md", "ok": True, "ended": True},
        {"tool": "mcp__hub__hub_agora_task_submit", "command": "", "ok": None, "ended": False},
    ]))
    assert marks == {"tests": "pending", "browser": "pending", "docs": "ok", "commit": "unknown",
                     "no_evidence": "fail", "agora": "ok"}


def test_live_projection_carries_loops_board_practices_and_task_summary():
    board = {"space": {"id": "s", "title": "S"},
             "agents": [{"id": "claude-one", "state": "working", "last_seen": 900}],
             "tasks": [{"id": 7, "owner": "claude-one", "status": "review", "updated": 800}],
             "locks": [{"resource": "path:Hub/a.py", "owner": "claude-one", "task_id": 7, "acquired": 810}]}
    loop = Loop()
    loop.turn_start(900)
    loop.tool_start("x", "Bash", {"command": "pytest -q"}, 901)
    watch_view = {"agents": {"claude-one": {"state": "tool", "session_key": "claude:1", "last_activity": 901}},
                  "sessions": [{"key": "claude:1", "loop": loop.view(), "tokens": 1234}]}
    view = project(board, watch_view, [], now=905)
    actor = view["actors"][0]
    assert actor["loop"]["phase"] == "act" and actor["loop"]["tool"] == "Bash" and actor["loop"]["tokens"] == 1234
    marks = _marks(actor["loop"]["practices"])
    assert marks["agora"] == "ok" and marks["claim"] == "ok"
    task = view["tasks"][0]
    assert task["loop"]["phase"] == "act" and task["loop"]["practices_summary"]["ok"] >= 2
    text = practices_text("claude-one", actor["loop"])
    assert text.startswith("Buenas prácticas observadas de claude-one") and "Tarea reclamada" in text


def test_submit_posts_the_checklist_in_the_task_thread(tmp_path, roots):
    now = time.time()
    codex_file(roots["codex"], cx_head(now - 100) + [
        cx_start(now - 90),
        cx_exec(now - 80, "a", "agora --as codex-one hb"), cx_out(now - 79, "a"),
        cx_exec(now - 60, "b", "python -m pytest -q"), cx_out(now - 50, "b"),
        cx_exec(now - 20, "c", 'git commit --author="Luissalet <l@x>" -m "Add"'), cx_out(now - 15, "c"),
    ], now - 15)
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"roots": {e: str(p) for e, p in roots.items()}}
    try:
        ag = hub.facet("agora").agora
        ag.heartbeat({"agent": "codex-one", "doing": "Working", "state": "working"})
        task = ag.task_add({"agent": "codex-one", "title": "Loop ring", "repo": "Hub"})["task"]
        ag.task_claim({"agent": "codex-one", "task_id": task["id"], "locks": ["path:Hub/a.py"]})
        read = tools.call(hub, "hub_agora_practices", {"agent": "codex-one"})
        assert read["observed"] and _marks(read["loop"]["practices"])["commit"] == "ok"
        out = tools.call(hub, "hub_agora_task_submit", {"agent": "codex-one", "task_id": task["id"],
                                                         "summary": "Ring and checklist", "commits": ["abc1234"]})
        assert _marks(out["practices"])["commit"] == "ok"
        thread = ag.thread({"thread_id": task["thread_id"], "agent": "codex-one"})
        bodies = [m["body"] for m in thread["messages"]]
        assert any(b.startswith("Buenas prácticas observadas de codex-one") for b in bodies)
        spec = next(t for t in tools.all_tools() if t["name"] == "hub_agora_practices")
        assert not spec["annotations"]["readOnlyHint"]
    finally:
        hub.close()


def test_a_web_research_session_gets_research_practices_and_readable_steps(roots, clock):
    now = clock.t
    claude_file(roots["claude"], [
        cl_user(now - 60, "¿Cuál es el precio del bitcoin hoy?"),
        cl_asst(now - 55, cl_use("u1", "WebSearch", query="precio del bitcoin hoy")),
        {**cl_result(now - 52, "u1"), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "1. https://www.coindesk.com/price/bitcoin"}]}},
        cl_asst(now - 50, cl_use("u2", "WebFetch", url="https://www.coindesk.com/price/bitcoin", prompt="precio")),
        cl_result(now - 47, "u2"),
        cl_asst(now - 40, cl_text("El bitcoin cotiza a 91.000 $ el 17 de noviembre de 2026 según "
                                  "[CoinDesk](https://www.coindesk.com/price/bitcoin)."), stop="end_turn"),
    ], now - 40)
    loop = one(watch(roots, clock))["loop"]
    assert loop["kind"] == "research" and loop["kind_label"] == "Investigación"
    assert _marks(loop["practices"]) == {"sources_opened": "ok", "sources_cited": "ok", "dated": "ok",
                                         "verified": "ok", "language": "ok"}
    tools_ = [s for s in loop["steps"] if s["kind"] == "tool"]
    assert tools_[0]["label"] == "Buscar en la web: precio del bitcoin hoy" and "{" not in tools_[0]["label"]
    assert tools_[1]["title"] == "Abrir URL" and tools_[1]["detail"] == "https://www.coindesk.com/price/bitcoin"
    result = next(s for s in loop["steps"] if s["kind"] == "result")
    assert result["ok"] is True and result["ms"] == 3000 and result["label"] == "Buscar en la web: ok · 3.0 s"
    practices = board_practices(loop, "claude-one", [{"id": 7, "status": "claimed"}],
                                [{"resource": "path:Hub/a.py", "owner": "claude-one"}])
    assert "claim" not in {p["id"] for p in practices}       # the board's proofs belong to code work


def test_kinds_and_a_plain_conversation():
    assert classify([]) == "chat"
    assert classify([{"tool": "Read", "command": "src/a.py", "ok": True, "ended": True}]) == "chat"
    assert classify([{"tool": "Write", "command": "out/informe.docx", "ok": True, "ended": True}]) == "files"
    assert classify([{"tool": "Bash", "command": "docker restart web", "ok": True, "ended": True}]) == "ops"
    assert classify([{"tool": "apply_patch", "command": "*** Begin Patch\n*** Update File: src/a.py\n", "ok": None,
                      "ended": True}]) == "code"
    loop = Loop()
    loop.turn_start(1, "hola, ¿qué tal?")
    loop.text(2, "¡Hola! Todo bien.")
    loop.turn_end(3)
    view = loop.view()
    assert view["kind"] == "chat" and view["practices"] == [] and view["practices_summary"]["ok"] == 0


def test_codex_web_search_counts_as_research():
    from hoard_link.hub.agent_watch import Session
    s = Session("codex", "x", "x.jsonl")
    s.feed({"type": "event_msg", "payload": {"type": "task_started"}}, 100.0)
    s.feed({"type": "response_item", "payload": {"type": "web_search_call",
                                                 "action": {"type": "search", "query": "gold price"}}}, 101.0)
    view = s.loop.view()
    assert view["kind"] == "research"
    assert [x["label"] for x in view["steps"] if x["kind"] == "tool"] == ["Buscar en la web: gold price"]
