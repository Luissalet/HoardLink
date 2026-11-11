"""Live projections retain real source times and never leak another server's sessions."""
import time
import os
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub.agora_live import file_path, project
from hoard_link.hub.agent_watch import _action_details

from ._hub_fakes import http, make_hub, serve
from ._transcripts import codex_file, cx_head, cx_start, cx_exec, cx_out, cx_done, put, roots  # noqa: F401


def snapshot():
    return {"space": {"id": "test", "title": "Test"},
            "agents": [{"id": "codex-one", "state": "working", "last_seen": 900},
                       {"id": "claude-two", "state": "working", "last_seen": 900}],
            "tasks": [{"id": 1, "owner": "codex-one", "updated": 800}],
            "locks": [{"resource": "path:Hub/a.py", "owner": "codex-one", "task_id": 1, "acquired": 810},
                      {"resource": "path:Foreign/b.py", "owner": "codex-one", "task_id": 999, "acquired": 999}]}


def test_no_motion_from_a_refresh_and_idle_threshold():
    first = project(snapshot(), {}, [], now=959)
    second = project(snapshot(), {}, [], now=960)
    actor = first["actors"][0]
    assert actor["target"] == {"kind": "file", "id": "Hub/a.py", "at": 810}
    assert not actor["resting"] and second["actors"][0]["resting"]
    assert actor["target"] == second["actors"][0]["target"]
    assert not any(f["id"].startswith("Foreign") for f in first["files"])


def test_real_tool_message_and_terminal_session():
    watch = {"agents": {"codex-one": {"state": "tool", "session_key": "codex:1", "last_activity": 940},
                        "claude-two": {"state": "idle"}, "foreign": {"state": "tool"}},
             "sessions": [{"key": "codex:1", "cwd": "Hub", "last_action": {
                 "name": "apply_patch", "input": "*** Update File: src/a.py\n+hello()", "since": 940}}]}
    view = project(snapshot(), watch, [], now=950, colors={"codex": "#abcdef"})
    assert len(view["actors"]) == 1
    actor = view["actors"][0]
    assert actor["target"] == {"kind": "file", "id": "src/a.py", "at": 940}
    assert actor["color"] == "#abcdef"
    assert view["files"][-1]["preview"].endswith("+hello()")
    message = {"id": 42, "author": "codex-one", "body": "Listo", "created": 945}
    moved = project(snapshot(), watch, [message], now=950)
    assert moved["actors"][0]["target"] == {"kind": "message", "id": "42", "at": 945}


def test_commands_never_invent_file_paths():
    assert not file_path("pytest -q")
    assert not file_path("search for a.py")
    assert file_path('Get-Content "E:\\My Repo\\src\\a.py"') == "E:\\My Repo\\src\\a.py"
    assert file_path("src/a.py") == "src/a.py"


def test_edit_excerpt_preserves_lines_and_existing_sanitization():
    action = _action_details({"file_path":"src/a.py", "new_string":"def render():\n    return ready\nAuthorization: Bearer secret-value"})
    assert action["path"] == "src/a.py"
    assert action["preview"] == "def render():\n    return ready"
    assert len(_action_details({"path":"a.py", "content":"a"*500})["preview"]) <= 200


def test_http_mcp_and_completed_tool_retention(tmp_path, roots):
    now = time.time()
    codex_file(roots["codex"], cx_head(now-100)+[cx_start(now-90),
        cx_exec(now-80,"a","agora --as codex-one hb"), cx_out(now-79,"a"),
        cx_exec(now-10,"b","*** Update File: src/agent.py\n+run()","apply_patch"),
        cx_out(now-9,"b")], now-9)
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"roots": {e:str(p) for e,p in roots.items()}}
    ag = hub.facet("agora").agora
    ag.heartbeat({"agent":"codex-one","doing":"Editing","state":"working"})
    server = serve(hub)
    try:
        status, view = http(hub.config.url.rstrip('/') + '/api/agora/live')
        assert status == 200 and view["ok"]
        actor = view["actors"][0]
        assert actor["session"]["last_action"]["input"].endswith("+run()")
        assert actor["target"]["kind"] == "file"
        assert tools.call(hub,"hub_agora_live",{})["actors"][0]["id"] == "codex-one"
        spec = next(t for t in tools.all_tools() if t["name"] == "hub_agora_live")
        assert spec["annotations"]["readOnlyHint"]
        ag.space_create({"agent":"codex-one","id":"empty","title":"Empty"})
        other = tools.call(hub,"hub_agora_live",{"space_id":"empty"})
        assert other["space"]["id"] == "empty"
        # The creator is a member there, so an unrelated agent is the scoping assertion.
        ag.heartbeat({"agent":"claude-local","state":"working","space_id":"ecosystem"})
        assert "claude-local" not in [a["id"] for a in tools.call(hub,"hub_agora_live",{"space_id":"empty"})["actors"]]
    finally:
        server.shutdown()
        hub.close()


@pytest.mark.parametrize("width,motion", [(1440,"no-preference"),(390,"reduce")])
def test_live_browser_targets_follow_recovery_and_lifecycle(tmp_path, roots, width, motion):
    browser_api = pytest.importorskip("playwright.sync_api")
    if not (Path(__file__).parents[2]/"apps/agora/dist/index.html").exists():
        pytest.skip("Build apps/agora before browser tests")
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"roots": {e:str(p) for e,p in roots.items()}}
    facet = hub.facet("agora")
    ag = facet.agora
    tick = [time.time()]
    ag.clock = lambda: tick[0]
    tasks = []
    for agent,title in [("codex-one","Show observed file changes"),("claude-two","Review the server navigation"),("faustus-three","Publish the result")]:
        ag.heartbeat({"agent":agent,"state":"working","doing":title})
        task = ag.task_add({"agent":agent,"title":title,"repo":"Live QA"})["task"]
        ag.task_claim({"agent":agent,"task_id":task["id"],"locks":[f"path:Live QA/src/{agent}.py"]})
        tasks.append(task)
    tick[0] += 1
    transcript = codex_file(roots["codex"],cx_head(tick[0]-20,cwd="E:/QA/Live")+[
        cx_start(tick[0]-10),cx_exec(tick[0]-5,"a","agora --as codex-one hb"),
        cx_exec(tick[0],"b","*** Update File: src/live.py\n+def render():\n+    return observed_state","apply_patch")],tick[0])
    facet.watch.clock = lambda: tick[0]
    server = serve(hub)
    evidence = Path(os.environ.get("AGORA_LIVE_EVIDENCE",str(tmp_path)))
    evidence.mkdir(parents=True,exist_ok=True)
    try:
        with browser_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width":width,"height":1000 if width>500 else 844},reduced_motion=motion)
                errors = []
                page.on("pageerror",lambda err:errors.append(str(err)))
                page.goto(hub.config.url+"/agora/?view=live")
                browser_api.expect(page.locator(".aglive-cursor")).to_have_count(3)
                browser_api.expect(page.locator(".aglive-file pre")).to_contain_text("+    return observed_state")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if motion == "reduce":
                    assert page.locator(".aglive-cursor").first.evaluate("el=>getComputedStyle(el).transitionDuration") == "0s"
                page.get_by_role("button",name="Sesión de codex-one",exact=True).click()
                page.get_by_role("button",name="Seguir a codex-one",exact=True).click()
                assert page.get_by_role("button",name="Dejar de seguir").get_attribute("aria-pressed") == "true"
                page.get_by_role("button",name="Cerrar detalle",exact=True).click()
                tick[0] += 10
                ag.channel_post({"agent":"codex-one","channel":"general","body":"Observed result is ready."})
                browser_api.expect(page.locator(".aglive-message p",has_text="Observed result is ready.")).to_be_visible(timeout=10000)
                page.screenshot(path=str(evidence/f"live-active-{width}.png"),full_page=False)
                # A status change moves the existing task and its owner's cursor to the new column.
                tick[0] += 1
                ag.task_submit({"agent":"claude-two","task_id":tasks[1]["id"],"summary":"Ready","commits":["abcdef1"]})
                browser_api.expect(page.locator(".aglive-column").nth(2).get_by_role("button",name="Review the server navigation",exact=False)).to_be_visible(timeout=10000)
                page.route("**/api/agora/live*",lambda route:route.abort())
                browser_api.expect(page.locator(".aglive-error")).to_be_visible(timeout=10000)
                assert page.locator(".aglive-cursor").count() == 3
                page.unroute("**/api/agora/live*")
                browser_api.expect(page.locator(".aglive-error")).to_have_count(0,timeout=10000)
                tick[0] += 61
                browser_api.expect(page.locator('.aglive-cursor[data-resting]')).to_have_count(3,timeout=10000)
                put(transcript,[cx_done(tick[0],text="Finished QA")],tick[0],append=True)
                tick[0] += 6  # The observer refreshes at most every five source-clock seconds.
                browser_api.expect(page.locator('.aglive-cursor[data-agent="codex-one"]')).to_have_count(0,timeout=10000)
                page.screenshot(path=str(evidence/f"live-rest-{width}.png"),full_page=False)
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        hub.close()
