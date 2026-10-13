"""Space isolation, legacy history and globally shared resource coordination."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

import pytest

from hoard_link.hub.agora import Agora, AgoraError


@pytest.fixture
def agora(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    yield ag
    ag.close()


def create(ag, title):
    return ag.space_create({"agent": "builder", "title": title})["space"]["id"]


def task(ag, space, title="work", agent="builder"):
    return ag.task_add({"agent": agent, "title": title, "space_id": space})["task"]


def test_original_space_is_default_and_preserves_ids(agora):
    original = task(agora, "ecosystem")
    other = task(agora, create(agora, "Project"))
    assert original["space_id"] == "ecosystem"
    assert [t["id"] for t in agora.tasks()["tasks"]] == [original["id"]]
    assert other["id"] > original["id"]
    assert agora.task({"task_id": original["id"]})["task"]["thread_id"] == original["thread_id"]


def test_board_tasks_threads_and_decisions_are_scoped(agora):
    sid = create(agora, "Research")
    original, own = task(agora, "ecosystem"), task(agora, sid)
    th = agora.thread_open({"agent": "builder", "title": "Decision", "body": "Choice", "space_id": sid})["thread"]
    agora.resolve({"agent": "builder", "thread_id": th["id"], "resolution": "Chosen", "space_id": sid})
    assert [t["id"] for t in agora.board({"space_id": sid})["tasks"]] == [own["id"]]
    assert agora.board()["counts"]["open"] == 1
    assert not agora.decisions()["decisions"]
    assert agora.decisions({"space_id": sid})["decisions"][0]["id"] == th["id"]
    with pytest.raises(AgoraError):
        agora.task({"task_id": original["id"], "space_id": sid})
    with pytest.raises(AgoraError):
        agora.post({"agent": "builder", "thread_id": original["thread_id"], "body": "wrong space", "space_id": sid})


def test_shared_resource_conflicts_between_spaces(agora):
    sid = create(agora, "Project")
    original, own = task(agora, "ecosystem"), task(agora, sid, agent="reviewer")
    assert agora.task_claim({"agent": "builder", "task_id": original["id"], "locks": ["model:principal"]})["ok"]
    conflict = agora.task_claim({"agent": "reviewer", "task_id": own["id"], "locks": ["model:principal"], "space_id": sid})
    assert not conflict["ok"]
    assert conflict["conflicts"][0]["owner"] == "builder"
    assert agora.locks({"space_id": sid})["locks"][0]["task_id"] == original["id"]


def test_independent_unread_cursors_and_ack_all(agora):
    sid = create(agora, "Project")
    first, second = task(agora, "ecosystem"), task(agora, sid)
    for item in (first, second):
        agora.post({"agent": "builder", "space_id": item["space_id"], "thread_id": item["thread_id"],
                    "body": "Please read", "mentions": ["reader"]})
    project = agora.inbox({"agent": "reader", "space_id": sid})
    assert {m["thread_id"] for m in project["messages"]} == {second["thread_id"]}
    assert agora.inbox({"agent": "reader", "peek": True})["messages"]
    assert agora.ack({"agent": "reader", "all": True, "space_id": sid})["acked"] == [second["thread_id"]]
    assert agora.inbox({"agent": "reader", "peek": True})["pending"]
    assert not agora.inbox({"agent": "reader", "space_id": sid, "peek": True})["messages"]


def test_space_rename_keeps_history_and_rejects_stale_update(agora):
    sid = create(agora, "Project")
    own = task(agora, sid)
    changed = agora.space_update({"agent": "builder", "space_id": sid, "expected_revision": 1, "title": "Renamed"})
    assert changed["space"]["id"] == sid
    assert changed["space"]["revision"] == 2
    assert agora.task({"space_id": sid, "task_id": own["id"]})["task"]["id"] == own["id"]
    with pytest.raises(AgoraError) as error:
        agora.space_update({"agent": "builder", "space_id": sid, "expected_revision": 1, "title": "Stale"})
    assert error.value.status == 409


def test_concurrent_operations_do_not_exchange_space(agora):
    spaces = [create(agora, f"Project {i}") for i in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        created = list(pool.map(lambda i: task(agora, spaces[i % 3], title=f"task {i}"), range(30)))
    for sid in spaces:
        assert {t["id"] for t in agora.tasks({"space_id": sid})["tasks"]} == {
            t["id"] for t in created if t["space_id"] == sid}
    assert not agora.tasks()["tasks"]


def test_restart_retains_spaces_members_and_read_cursor(tmp_path):
    path = tmp_path / "agora.db"
    with_ag = Agora(path)
    sid = create(with_ag, "Project")
    own = task(with_ag, sid)
    with_ag.inbox({"agent": "reader", "space_id": sid})
    with_ag.close()
    reopened = Agora(path)
    try:
        assert sid in {s["id"] for s in reopened.spaces()["spaces"]}
        assert "builder" in reopened.space({"space_id": sid})["space"]["members"]
        assert reopened.task({"space_id": sid, "task_id": own["id"]})["task"]["id"] == own["id"]
        assert not reopened.inbox({"agent": "reader", "space_id": sid, "peek": True})["messages"]
    finally:
        reopened.close()


def test_sync_and_handover_do_not_move_other_spaces(agora):
    sid = create(agora, "Project")
    original, own = task(agora, "ecosystem"), task(agora, sid)
    for item in (original, own):
        agora.task_claim({"agent": "builder", "task_id": item["id"], "space_id": item["space_id"]})
    agora.heartbeat({"agent": "receiver", "space_id": sid})
    changed = agora.handover({"agent": "receiver", "from_agent": "builder", "to_agent": "receiver",
                             "space_id": sid, "force": True, "reason": "Explicit private test handoff"})
    assert [t["id"] for t in changed["tasks"]] == [own["id"]]
    assert agora.task({"task_id": original["id"]})["task"]["owner"] == "builder"
    synced = agora.sync({"agent": "receiver", "space_id": sid})
    assert {m["thread_id"] for m in synced["posts"]} >= {own["thread_id"]}
    assert all(m["thread_id"] != original["thread_id"] for m in synced["posts"])
    assert synced["space_id"] == sid


def test_legacy_migration_preserves_fields_reviews_and_read_cursors(tmp_path):
    from hoard_link.hub.agora import MIGRATIONS
    from hoard_link.sqlkit import Database

    path = tmp_path / "legacy.db"
    old = Database(path, migrations=MIGRATIONS[:-1])
    old.execute("INSERT INTO agents(id,name,kind,last_seen,created) VALUES('builder','Builder','agent',10,10)")
    old.execute("INSERT INTO tasks(id,title,body,kind,status,owner,reviewed,submission_revision,reviewed_submission_revision,"
                "commits,reviewed_commits,thread_id) VALUES(42,'Original','History','feature','approved','builder',1,7,7,"
                "'[\"abc1234\"]','[\"abc1234\"]',32)")
    old.execute("INSERT INTO threads(id,title,kind,task_id,status,created_by) VALUES(32,'Original','task',42,'open','builder')")
    old.execute("INSERT INTO messages(id,thread_id,author,kind,body) VALUES(99,32,'builder','comment','Original message')")
    old.execute("INSERT INTO reads(agent,last_msg_id,updated) VALUES('reader',99,20)")
    snapshot = dict(old.one("SELECT * FROM tasks WHERE id=42"))
    old.close()
    current = Agora(path)
    try:
        stored = dict(current.db.one("SELECT * FROM tasks WHERE id=42"))
        assert {key: stored[key] for key in snapshot} == snapshot
        assert stored["space_id"] == "ecosystem"
        assert current.task({"task_id": 42})["messages"][0]["id"] == 99
        assert current.space_store.read_upto("ecosystem", "reader") == 99
        assert current.space({})["space"]["members"] == ["builder"]
    finally:
        current.close()


def test_native_http_cli_and_stdio_mcp_share_space_authority(tmp_path):
    from ._hub_fakes import make_hub, serve

    root = Path(__file__).resolve().parents[2]
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        env = {**os.environ, "HOARD_HUB_URL": hub.config.url,
               "HOARD_HUB_TOKEN_FILE": str(hub.config.token_file), "HOARD_HUB_DATA_DIR": str(hub.config.data_dir),
               "HOARD_HUB_AUTOSTART": "0", "PYTHONPATH": str(root)}
        created = subprocess.run([sys.executable, str(root / "scripts/agora.py"), "--as", "builder", "--json",
                                  "space-create", "Shared project", "--id", "project"],
                                 cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert created.returncode == 0, created.stderr
        assert json.loads(created.stdout)["space"]["id"] == "project"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_agora_task_add", "arguments": {"agent": "builder", "space_id": "project", "title": "MCP work"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "hub_agora_board", "arguments": {"space_id": "project"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "hub_agora_board", "arguments": {}}},
        ]
        process = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                 input="\n".join(json.dumps(r) for r in messages) + "\n", capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
        assert process.returncode == 0, process.stderr
        replies = {r["id"]: r for r in map(json.loads, process.stdout.splitlines()) if "id" in r}
        tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
        assert "hub_agora_space_update" in tools
        assert "space_id" in tools["hub_agora_task_add"]["inputSchema"]["properties"]
        for i in (3, 4, 5):
            assert not replies[i]["result"].get("isError"), replies[i]
        own = hub.facet("agora").agora.tasks({"space_id": "project"})["tasks"][0]
        with urllib.request.urlopen(hub.config.url + f"/api/agora/tasks/{own['id']}?space_id=project") as response:
            assert json.load(response)["task"]["title"] == "MCP work"
        with urllib.request.urlopen(hub.config.url + "/api/agora/board") as response:
            assert not json.load(response)["tasks"]
        (tmp_path / "native-space-receipt.json").write_text(json.dumps({"cli": json.loads(created.stdout),
            "mcp_requests": messages, "mcp_responses": replies, "canonical_task": own}, indent=2), encoding="utf-8")


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_browser_spaces_preserve_draft_create_rename_and_ignore_late_board(tmp_path, width, height):
    browser_api = pytest.importorskip("playwright.sync_api")
    from ._hub_fakes import make_hub, serve

    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        hub.config.agent_watch = {"enabled": False}
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        first = task(ag, "ecosystem", "Original work")
        sid = create(ag, "Project")
        own = task(ag, sid, "Project work")
        ag.task_claim({'agent': 'builder', 'space_id': sid, 'task_id': own['id']})
        ag.task_submit({'agent': 'builder', 'space_id': sid, 'task_id': own['id'], 'summary': 'Project delivery',
                        'blocks': [{'id': 'reason', 'kind': 'what_why', 'title': 'Project rationale', 'body': 'Only this project'}]})
        with browser_api.sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.fail("Chromium required for the space delivery")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(hub.config.url)
            page.evaluate("localStorage.setItem('hub.lang', 'es')")
            page.reload()
            page.locator('#family-tabs .tab[data-tab="agora"]').click()
            tab = page.locator("#tab-agora")
            picker = tab.locator('.ag-spacebar select')
            picker.locator(f'option[value="{sid}"]').wait_for(state="attached")
            tab.locator('.ag-card', has_text='Original work').click()
            draft = tab.locator('.ag-compose textarea')
            draft.fill('Draft in original space')
            picker.select_option(sid)
            tab.locator('.ag-card', has_text='Project work').wait_for()
            assert not tab.locator('.ag-card', has_text='Original work').count()
            tab.locator('.ag-card', has_text='Project work').click()
            tab.locator('.ag-blocks', has_text='Project rationale').wait_for()
            tab.locator('.ag-block textarea').fill('Note for this project block')
            tab.locator('.ag-compose textarea').fill('Project draft')
            picker.select_option('ecosystem')
            assert tab.locator('.ag-compose textarea').input_value() == 'Draft in original space'
            picker.select_option(sid)
            assert tab.locator('.ag-compose textarea').input_value() == 'Project draft'
            assert 'space_id=' in page.url
            tab.locator('.ag-spacebar button', has_text='Renombrar').click()
            tab.locator('.ag-form input').fill('Renamed project')
            tab.locator('.ag-form button', has_text='Renombrar').click()
            picker.locator('option', has_text='Renamed project').wait_for(state='attached')
            assert ag.space({'space_id': sid})['space']['title'] == 'Renamed project'
            assert ag.task({'space_id': sid, 'task_id': own['id']})['task']['id'] == own['id']
            tab.locator('.ag-compose button', has_text='Aprobar').click()
            page.wait_for_function('([base,sid,id]) => fetch(`${base}/api/agora/tasks/${id}?space_id=${sid}`)'
                                   '.then(r => r.json()).then(r => r.task.status === "approved")',
                                   arg=[hub.config.url.rstrip('/'), sid, own['id']])
            reviewed = ag.task({'space_id': sid, 'task_id': own['id']})
            assert any('Note for this project block' in m['body'] for m in reviewed['messages'])

            # A completed old request cannot replace the new space's board.
            delayed = []
            page.route('**/api/agora/board?space_id=*', lambda route: delayed.append(route))
            picker.select_option('ecosystem')
            picker.select_option(sid)
            for _ in range(50):
                if delayed:
                    break
                page.wait_for_timeout(20)
            assert delayed, "The race must include an actual in-flight old board request"
            picker.select_option('ecosystem')
            tab.locator('.ag-card', has_text='Original work').wait_for(state='attached')
            for route in delayed:
                route.fulfill(json=ag.board({'space_id': sid}))
            page.unroute('**/api/agora/board?space_id=*')
            page.wait_for_timeout(100)
            assert not tab.locator('.ag-card', has_text='Project work').count()
            assert picker.input_value() == 'ecosystem'
            assert not errors
            if width < 1200 and tab.locator('.ag-detail').is_visible():
                tab.locator('.ag-detail .ag-dhead button').first.click()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(tmp_path / f'spaces-{width}.png'), full_page=False)
            browser.close()


def test_escalation_link_keeps_nondefault_space(tmp_path):
    from ._hub_fakes import make_hub
    hub = make_hub(tmp_path, [])
    try:
        facet = hub.facet('agora')
        sid = create(facet.agora, 'Review project')
        opened = facet.agora.thread_open({'agent': 'builder', 'space_id': sid, 'title': 'Choice', 'body': 'A or B'})
        sent = []
        class Notify:
            def send(self, *args, **kwargs):
                sent.append(kwargs)
        original_facet = hub.facet
        hub.facet = lambda name: Notify() if name == 'notify' else original_facet(name)
        facet.agora.escalate({'agent': 'builder', 'space_id': sid, 'thread_id': opened['thread']['id'], 'question': 'A or B?'})
        assert sent and f'?space_id={sid}#agora-' in sent[-1]['url']
    finally:
        hub.close()
