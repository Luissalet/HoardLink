"""Task progress must not equate reachability with work, or fabricate lock history."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub import tools
from hoard_link.sqlkit import Database
from hoard_link.hub.agora import MIGRATIONS
from ._hub_fakes import http, make_hub, serve


@pytest.fixture
def ag(tmp_path):
    now = [1_800_000_000.0]
    store = Agora(tmp_path / "agora.db", clock=lambda: now[0], task_idle_s=60)
    store.now = now
    yield store
    store.close()


def claimed(ag):
    task = ag.task_add({"agent": "requester", "title": "Verify a long task"})["task"]
    return ag.task_claim({"agent": "builder", "task_id": task["id"]})["task"]


def test_heartbeat_does_not_reset_task_progress_and_new_note_recovers(ag):
    task = claimed(ag)
    ag.now[0] += 61
    ag.heartbeat({"agent": "builder", "doing": "still reachable"})
    assert ag.activity({"task_id": task["id"]})["activity"]["state"] == "stalled"
    assert ag.task({"task_id": task["id"]})["task"]["status"] == "claimed"
    inbox = ag.inbox({"agent": "requester", "peek": True})
    assert [t["id"] for t in inbox["progress_alerts"]] == [task["id"]]
    assert inbox["counts"]["progress_alerts"] == 1
    assert len(ag.digest()["waiting"]["progress"]) == 1
    ag.post({"agent": "reviewer", "thread_id": task["thread_id"], "body": "Do not count my question as your progress"})
    assert ag.activity({"task_id": task["id"]})["activity"]["state"] == "stalled"
    ag.task_update({"agent": "builder", "task_id": task["id"], "note": "First fixture passed"})
    activity = ag.activity({"task_id": task["id"]})["activity"]
    assert activity["state"] == "recent" and activity["idle_s"] == 0
    assert ag.inbox({"agent": "requester", "peek": True})["progress_alerts"] == []


@pytest.mark.parametrize("status", ["review", "approved", "blocked", "done", "dropped"])
def test_expected_waits_and_final_tasks_are_never_stalled(ag, status):
    task = claimed(ag)
    ag.db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task["id"]))
    ag.now[0] += 1000
    ag.heartbeat({"agent": "builder", "state": "working"})
    state = ag.activity({"task_id": task["id"]})["activity"]["state"]
    assert state == ("final" if status in ("done", "dropped") else "waiting")


def test_waiting_agent_unknown_heartbeat_and_task_specific_progress(ag):
    first = claimed(ag)
    second = claimed(ag)
    ag.now[0] += 61
    ag.post({"agent": "builder", "thread_id": second["thread_id"], "body": "Second task made progress"})
    assert ag.activity({"task_id": first["id"]})["activity"]["state"] == "stalled"
    assert ag.activity({"task_id": second["id"]})["activity"]["state"] == "recent"
    ag.heartbeat({"agent": "builder", "state": "waiting"})
    assert ag.activity({"task_id": first["id"]})["activity"]["state"] == "waiting"
    ag.heartbeat({"agent": "builder", "state": "working"})
    ag.now[0] += 10801
    assert ag.activity({"task_id": first["id"]})["activity"]["state"] == "quiet"


def test_observed_running_is_agent_scope_not_task_progress(ag):
    task = claimed(ag)
    ag.now[0] += 61
    ag.heartbeat({"agent": "builder"})
    activity = ag._task_activity(task, observed={"state": "tool", "last_activity": ag.now[0], "session_key": "synthetic"})
    assert activity["state"] == "unconfirmed"
    assert activity["idle_s"] == 61
    assert activity["owner_observed"]["scope"] == "agent"
    assert ag._task_activity(task, observed={"state": "waiting"})["state"] == "waiting"


def test_timeline_enriches_checkpoints_with_stable_ids_and_associated_debates(ag):
    task = claimed(ag)
    ag.checkpoint({"agent": "builder", "task_id": task["id"], "expected_revision": 0,
                   "payload": {"summary": "First evidence", "tests": [{"name": "parser", "status": "passed", "evidence": "synthetic"}]}})
    debate = ag.thread_open({"agent": "reviewer", "title": "Contract", "body": "Discuss adoption", "task_id": task["id"]})["thread"]
    ag.checkpoint({"agent": "builder", "task_id": task["id"], "expected_revision": 1, "payload": {"summary": "Second evidence"}})
    events, cursor = [], 0
    while True:
        page = ag.timeline({"task_id": task["id"], "after_id": cursor, "limit": 2})
        events.extend(page["events"])
        cursor = page["next_after_id"]
        if not page["has_more"]:
            break
    ids = [e["event_id"] for e in events]
    assert ids == sorted(set(ids))
    assert [e["checkpoint"]["revision"] for e in events if e["event_kind"] == "checkpoint"] == [1, 2]
    assert any(e["thread_id"] == debate["id"] for e in events)
    assert ag.timeline({"task_id": task["id"], "after_id": cursor})["events"] == []
    assert ag.activity({"task_id": task["id"]})["activity"]["checkpoint_revision"] == 2
    assert ag.task({"task_id": task["id"]})["timeline"]["events"] == events


@pytest.mark.parametrize("field,value", [("idle_after_s", True), ("idle_after_s", 59), ("idle_after_s", 604801), ("task_id", 0)])
def test_strict_activity_parameters(ag, field, value):
    task = claimed(ag)
    with pytest.raises(AgoraError):
        ag.activity({"task_id": task["id"], field: value})
    assert ag.activity({"task_id": task["id"], "idle_after_s": 120})["activity"]["idle_after_s"] == 120


def test_task_reads_do_not_drift_with_elapsed_seconds_or_mutate_rows(ag):
    task = claimed(ag)
    before = ag.task({"task_id": task["id"]})
    rows = ag.db.conn.total_changes
    ag.now[0] += 0.25
    assert ag.task({"task_id": task["id"]}) == before
    assert "checked_at" not in before["task"]["activity"]
    assert "idle_s" not in before["task"]["activity"]
    assert ag.activity({"task_id": task["id"]})["activity"]["idle_s"] == 0.25
    assert ag.db.conn.total_changes == rows


def test_checkpoint_message_binding_upgrade_and_reopen(tmp_path):
    path = tmp_path / "legacy.db"
    old = Database(path, migrations=MIGRATIONS[:6])
    old.execute("INSERT INTO tasks(id,title,owner,status,thread_id,created,updated) VALUES(1,'Old','builder','claimed',1,10,10)")
    old.execute("INSERT INTO threads(id,title,kind,task_id,status,created_by,created,updated) VALUES(1,'Old','task',1,'open','builder',10,10)")
    old.execute("INSERT INTO task_checkpoints(task_id,revision,author,created,payload) VALUES(1,1,'builder',10,?)",
                (json.dumps({"payload": {"summary": "Legacy evidence"}, "task_owner": "builder", "lock_snapshot": []}),))
    old.execute("INSERT INTO messages(thread_id,author,kind,body,created) VALUES(1,'builder','system',?,11)",
                ("Checkpoint r1: Legacy evidence [hub_agora_checkpoints task_id=1]",))
    old.close()
    for turn in range(2):
        store = Agora(path)
        try:
            assert store.db.schema_version == 7
            event = store.timeline({"task_id": 1})["events"][0]
            assert event["checkpoint_link"] == "legacy_inferred"
            assert event["checkpoint"]["payload"]["summary"] == "Legacy evidence"
            if turn == 0:
                store.checkpoint({"agent": "builder", "task_id": 1, "expected_revision": 1, "payload": {"summary": "Recorded evidence"}})
            recorded = store.timeline({"task_id": 1})["events"][-1]
            assert recorded["checkpoint_link"] == "recorded"
            assert recorded["checkpoint"]["message_id"] == recorded["event_id"]
        finally:
            store.close()


def test_observer_progress_is_consistent_across_transport_views(tmp_path):
    hub = make_hub(tmp_path, [])
    try:
        facet = hub.facet("agora")
        store = facet.agora
        now = [1_800_000_000.0]
        store.clock = lambda: now[0]
        store.task_idle_s = 60
        task = claimed(store)
        now[0] += 61
        view = {"agents": {"builder": {"state": "tool", "last_activity": now[0], "session_key": "fixture"}},
                "questions": [], "unbound": [], "sessions": []}
        class Observer:
            def view(self):
                return view
        facet._watch = Observer()
        sync = facet._sync({"agent": "builder"})
        for item in [facet._board()["tasks"][0], facet._tasks({})["tasks"][0],
                     facet._task({"task_id": task["id"]})["task"], sync["leased_tasks"][0], sync["board"]["tasks"][0]]:
            assert item["activity"]["state"] == "unconfirmed"
            assert "checked_at" not in item["activity"]
        assert facet._activity({"task_id": task["id"]})["activity"]["idle_s"] == 61
        assert facet._inbox({"agent": "requester", "peek": True})["progress_alerts"] == []
        assert facet._sync({"agent": "requester"})["inbox"]["progress_alerts"] == []
    finally:
        facet._watch = None
        hub.close()


def test_http_cli_and_real_stdio_mcp_share_new_contracts(tmp_path):
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"enabled": False, "task_idle_s": 60}
    server = serve(hub)
    try:
        ag = hub.facet("agora").agora
        task = claimed(ag)
        ag.checkpoint({"agent": "builder", "task_id": task["id"], "expected_revision": 0, "payload": {"summary": "Native MCP evidence"}})
        base = hub.config.url.rstrip("/")
        status, page = http(base + f"/api/agora/timeline?task_id={task['id']}&limit=2")
        assert status == 200 and page["has_more"]
        assert http(base + f"/api/agora/activity?task_id={task['id']}")[1]["activity"]["state"] == "recent"
        assert http(base + f"/api/agora/timeline?task_id={task['id']}&after_id=-1")[1]["ok"] is False
        root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env.update({"HOARD_HUB_URL": base, "HOARD_HUB_TOKEN_FILE": str(hub.config.token_file),
                    "HOARD_HUB_DATA_DIR": str(hub.config.data_dir), "HOARD_HUB_AUTOSTART": "0", "PYTHONPATH": str(root)})
        cli = subprocess.run([sys.executable, str(root / "scripts/agora.py"), "--json", "timeline", str(task["id"]), "--limit", "2"],
                             cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["next_after_id"] == page["next_after_id"]
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_agora_activity", "arguments": {"task_id": task["id"]}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hub_agora_timeline", "arguments": {"task_id": task["id"]}}}]
        mcp = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], input="\n".join(json.dumps(r) for r in requests) + "\n",
                             cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
        assert mcp.returncode == 0, mcp.stderr
        replies = {r["id"]: r for r in map(json.loads, mcp.stdout.splitlines()) if "id" in r}
        for name in ("hub_agora_activity", "hub_agora_timeline"):
            descriptor = next(t for t in replies[2]["result"]["tools"] if t["name"] == name)
            assert descriptor["annotations"]["readOnlyHint"] is True
            assert descriptor["inputSchema"]["additionalProperties"] is False
        assert json.loads(replies[3]["result"]["content"][0]["text"])["activity"]["state"] == "recent"
        events = json.loads(replies[4]["result"]["content"][0]["text"])["events"]
        assert any(e["event_kind"] == "checkpoint" for e in events)
    finally:
        server.shutdown()
        server.server_close()
        hub.close()


@pytest.mark.parametrize("width,language", [(1280, "es"), (390, "en")])
def test_real_browser_progress_paging_recovery_and_review_draft(tmp_path, width, language):
    browser_api = pytest.importorskip("playwright.sync_api")
    from .test_agora_checkpoints import evidence_path
    source = Path(__file__).resolve().parents[2] / "hoard_link/hub/ui/agora.js"
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"enabled": False}
    server = serve(hub)
    ag = hub.facet("agora").agora
    now = [1_800_000_000.0]
    ag.clock = lambda: now[0]
    ag.task_idle_s = 60
    task = claimed(ag)
    ag.checkpoint({"agent": "builder", "task_id": task["id"], "expected_revision": 0,
                   "payload": {"summary": "First reproducible evidence", "tests": [{"name": "parser", "status": "passed", "evidence": "isolated fixture"}]}})
    for number in range(110):
        ag.post({"agent": "builder", "thread_id": task["thread_id"], "body": f"Chronology evidence {number:03}"})
    now[0] += 61
    ag.heartbeat({"agent": "builder", "doing": "Long running work"})
    base = hub.config.url.rstrip("/")
    try:
        with browser_api.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Headless Chromium is not installed")
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + f"/api/agora/tasks/{task['id']}")
                page.set_content('<html><head><style>[hidden]{display:none!important}*{box-sizing:border-box}body{font:14px Arial;margin:0;padding:16px;--muted:#555;--line:#aaa;--card-2:#f4f4f4;--hoard-sunken:#eee;--text:#222;--amber:#775000;--font:Arial;--mono:monospace}button,textarea{font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><div id="root"></div></body></html>')
                page.evaluate("""language => {
                  window.HubFacets = {ctx: {
                    el: (tag,cls='',text='') => {const n=document.createElement(tag);n.className=cls;n.textContent=text;return n},
                    L: labels => labels[language], fmtWhen: n => new Date(n*1000).toISOString(), toast: () => {},
                    api: async (path,body) => (await fetch(path,body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})).json()},
                    register: (name,facet) => window.fixtureFacet=facet};
                }""", language)
                page.add_script_tag(content=source.read_text(encoding="utf-8"))
                page.evaluate("async () => {window.fixtureFacet.mount(document.getElementById('root'));await window.fixtureFacet.load()}")
                page.locator(".ag-card").click()
                assert ("Revisar progreso" if language == "es" else "Check progress") in page.locator(".ag-progress").inner_text()
                assert page.locator(".ag-checkpoint").count() == 1
                assert page.locator(".ag-checkpoint-history").count() == 1
                more = page.get_by_role("button", name="Cargar más actividad" if language == "es" else "Load more activity")
                more.click()
                page.get_by_text("Chronology evidence 109", exact=True).wait_for()
                assert more.count() == 0
                assert page.locator(".ag-msgs").get_by_text("Chronology evidence 000", exact=True).count() == 1
                draft = page.locator("textarea")
                draft.fill("Review draft stays while progress refreshes")
                ag.checkpoint({"agent": "builder", "task_id": task["id"], "expected_revision": 1,
                               "payload": {"summary": "Recovered after a new test", "artifacts": ["isolated artifact"]}})
                page.evaluate("async () => {await window.fixtureFacet.tick()}")
                assert ("Progreso registrado" if language == "es" else "Progress reported") in page.locator(".ag-progress").inner_text()
                assert draft.input_value() == "Review draft stays while progress refreshes"
                assert page.locator(".ag-checkpoint-history").count() == 2
                assert "r2" in page.locator(".ag-checkpoint h4").inner_text()
                assert page.get_by_text("Chronology evidence 109", exact=True).count() == 1
                debate = ag.thread_open({"agent": "reviewer", "title": "Cross review", "task_id": task["id"], "body": "Late associated debate"})["thread"]
                page.evaluate("async () => {await window.fixtureFacet.tick()}")
                page.get_by_text("Late associated debate", exact=True).wait_for()
                assert draft.input_value() == "Review draft stays while progress refreshes"
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not errors
                page.screenshot(path=str(evidence_path(tmp_path, f"activity-{width}-{language}.png")), full_page=True)
                evidence_path(tmp_path, f"activity-{width}-{language}.json").write_text(json.dumps({
                    "viewport": {"width": width, "height": 900}, "language": language, "page_errors": errors,
                    "timeline_checkpoints": page.locator(".ag-checkpoint-history").count(), "associated_thread_id": debate["id"],
                    "draft_preserved": True, "overflow": False, "paging_recovered": True}, indent=2), encoding="utf-8")
                page.evaluate("language => localStorage.setItem('hub.lang', language)", language)
                page.goto(base + f"/#agora-{task['thread_id']}")
                page.locator("#tab-agora .ag-checkpoint").wait_for()
                assert "Recovered after a new test" in page.locator("#tab-agora .ag-checkpoint").inner_text()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(evidence_path(tmp_path, f"activity-shell-{width}-{language}.png")), full_page=True)
                page.get_by_role("button", name="Cargar más actividad" if language == "es" else "Load more activity").click()
                page.get_by_text("Late associated debate", exact=True).wait_for()
                assert page.locator("#tab-agora .ag-checkpoint-history").count() == 2
                page.locator("#tab-agora .ag-msgs").evaluate("element => element.scrollTop = element.scrollHeight")
                page.locator("#tab-agora textarea").scroll_into_view_if_needed()
                assert not errors
                page.screenshot(path=str(evidence_path(tmp_path, f"activity-shell-chronology-{width}-{language}.png")), full_page=True)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
