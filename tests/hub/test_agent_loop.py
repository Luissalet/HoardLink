"""Agent loop of an observed session: phases from transcript lines, steps, practices and the submit checklist."""
import time

from hoard_link.hub import tools
from hoard_link.hub.agent_loop import Loop, judge_practices
from hoard_link.hub.agora_live import practices_text, project

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
