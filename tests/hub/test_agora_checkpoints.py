"""Durable declared checkpoints, CAS, lease warnings and transport/UI safety."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from hoard_link.hub.agora import Agora, AgoraError, MIGRATIONS, TOOLS
from hoard_link.hub import tools
from hoard_link.sqlkit import Database
from ._hub_fakes import http, make_hub, serve
from .test_agora_sync import Clock


def evidence_path(tmp_path, name):
    directory = Path(os.environ.get("CHECKPOINT_EVIDENCE_DIR", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def claimed(ag):
    return ag.task_add({"agent": "codex", "title": "Durable work", "repo": "Example", "kind": "chore",
                        "paths": ["src"], "claim": True})["task"]["id"]


def write(ag, task_id, revision=0, payload=None, agent="codex"):
    return ag.checkpoint({"agent": agent, "task_id": task_id, "expected_revision": revision,
                          "payload": payload if payload is not None else {"summary": "Continue here"}})


def test_migration_preserves_existing_reviewed_tasks(tmp_path):
    path = tmp_path / "agora.db"
    old = Database(path, migrations=MIGRATIONS[:-1])
    old.migrate()
    old.execute("INSERT INTO tasks(id,title,status,owner,commits,reviewed,review_state,reviewed_commits) VALUES(1,?,?,?,?,?,?,?)",
                ("Existing approval", "approved", "codex", '["original"]', 1, "approved", '["original"]'))
    before = dict(old.one("SELECT * FROM tasks WHERE id=1"))
    old.close()
    ag = Agora(path)
    try:
        migrated = dict(ag.db.one("SELECT * FROM tasks WHERE id=1"))
        assert {key: migrated[key] for key in before} == before
        assert ag.db.schema_version == len(MIGRATIONS)
        assert write(ag, 1)["checkpoint"]["revision"] == 1
        assert dict(ag.db.one("SELECT * FROM tasks WHERE id=1")) == migrated
    finally:
        ag.close()


def test_restart_latest_history_pagination_and_sync(tmp_path):
    db = tmp_path / "agora.db"
    ag = Agora(db)
    task_id = claimed(ag)
    payload = {"summary": "Résumé <script>alert(1)</script>", "workspace": "Z:/not-a-real-checkout",
               "branch": "topic", "base_head": "a" * 40, "head": "B" * 64, "next_steps": ["Continue"],
               "tests": [{"name": "pytest", "status": "not_run", "evidence": "Not executed"}],
               "artifacts": ["file:///untrusted"]}
    first = write(ag, task_id, payload=payload)["checkpoint"]
    assert first["payload"] == payload
    assert first["lock_snapshot"][0]["task_id"] == task_id
    write(ag, task_id, 1, {"summary": "Second"})
    ag.close()
    ag = Agora(db)
    try:
        latest = ag.task({"task_id": task_id})["task"]["latest_checkpoint"]
        assert latest["revision"] == 2
        assert ag.tasks()["tasks"][0]["checkpoint_summary"]["revision"] == 2
        assert ag.sync({"agent": "codex"})["leased_tasks"][0]["latest_checkpoint"]["revision"] == 2
        page = ag.checkpoints({"task_id": task_id, "limit": 1})
        assert page["has_more"] and page["checkpoints"][0]["payload"] == payload
        rest = ag.checkpoints({"task_id": task_id, "after_revision": page["next_after_revision"], "limit": 1})
        assert not rest["has_more"] and rest["checkpoints"][0]["revision"] == 2
        assert ag.checkpoints({"task_id": task_id, "after_revision": 2})["next_after_revision"] == 2
    finally:
        ag.close()


def test_lists_and_foreign_sync_keep_payload_compact(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    try:
        task_id = claimed(ag)
        payload = {"summary": "s" * 8000, "artifacts": ["a" * 2000] * 20}
        write(ag, task_id, payload=payload)
        for listed in (ag.tasks()["tasks"], ag.board()["tasks"]):
            task = next(t for t in listed if t["id"] == task_id)
            assert "latest_checkpoint" not in task
            assert task["checkpoint_summary"]["summary"] == "s" * 300
            assert task["checkpoint_summary"]["truncated"]
            assert len(json.dumps(task)) < 3000
        foreign = ag.sync({"agent": "claude"})
        assert foreign["leased_tasks"] == []
        # A compact task row does not suffice if its notification still copies
        # the full summary into every reader's inbox and sync messages.
        for response in (ag.inbox({"agent": "claude", "peek": True}), foreign):
            encoded = json.dumps(response)
            assert "s" * 1000 not in encoded
            assert "a" * 2000 not in encoded
        assert "latest_checkpoint" not in next(t for t in foreign["board"]["tasks"] if t["id"] == task_id)
        own = ag.sync({"agent": "codex"})
        assert own["leased_tasks"][0]["latest_checkpoint"]["payload"] == payload
        assert ag.task({"task_id": task_id})["task"]["latest_checkpoint"]["payload"] == payload
    finally:
        ag.close()


def test_duplicate_stale_and_atomic_rollback(tmp_path, monkeypatch):
    events = []
    ag = Agora(tmp_path / "agora.db", emit=lambda *args: events.append(args))
    try:
        task_id = claimed(ag)
        result = write(ag, task_id)
        events_before = len(events)
        messages_before = ag.db.scalar("SELECT count(*) FROM messages")
        assert write(ag, task_id)["duplicate"] is True
        assert len(events) == events_before
        assert ag.db.scalar("SELECT count(*) FROM messages") == messages_before
        assert ag.db.scalar("SELECT count(*) FROM task_checkpoints") == 1
        with pytest.raises(AgoraError) as exc:
            write(ag, task_id, payload={"summary": "Different"})
        assert exc.value.status == 409 and exc.value.extra["current_revision"] == 1
        with pytest.raises(AgoraError):
            write(ag, task_id, 3)
        before = dict(ag.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)))
        def fail(*args):
            raise RuntimeError("message failure")
        monkeypatch.setattr(ag, "_system", fail)
        with pytest.raises(RuntimeError):
            write(ag, task_id, 1, {"summary": "Would be rolled back"})
        assert ag.db.scalar("SELECT count(*) FROM task_checkpoints") == 1
        assert dict(ag.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))) == before
        assert len(events) == events_before
        assert result["checkpoint"]["revision"] == 1
    finally:
        ag.close()


def test_two_database_connections_race_cas(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    other = Agora(tmp_path / "agora.db")
    try:
        task_id = claimed(ag)
        barrier = threading.Barrier(2)
        def race(store, summary):
            barrier.wait(timeout=5)
            try:
                return write(store, task_id, payload={"summary": summary})
            except AgoraError as exc:
                return {"status": exc.status}
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(race, ag, "A")
            b = pool.submit(race, other, "B")
            results = [a.result(timeout=10), b.result(timeout=10)]
        assert sum(r.get("ok") is True for r in results) == 1
        assert sum(r.get("status") == 409 for r in results) == 1
        assert ag.db.scalar("SELECT count(*) FROM task_checkpoints") == 1
    finally:
        other.close()
        ag.close()


def test_owner_change_final_tasks_and_review_state_independence(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    try:
        task_id = claimed(ag)
        ag.task_submit({"agent": "codex", "task_id": task_id, "summary": "Review", "branch": "review-branch",
                        "commits": ["c" * 40]})
        ag.task_review({"agent": "claude", "task_id": task_id, "verdict": "approve", "body": "Checked", "expected_submission_revision": 1})
        before = dict(ag.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)))
        write(ag, task_id, payload={"summary": "Ready", "branch": "different", "head": "d" * 40})
        assert dict(ag.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))) == before
        with pytest.raises(AgoraError) as exc:
            write(ag, task_id, 1, agent="claude")
        assert exc.value.status == 403
        with pytest.raises(AgoraError) as exc:
            write(ag, task_id, 1, agent="luis")
        assert exc.value.status == 403
        ag.task_release({"agent": "codex", "task_id": task_id, "reason": "Transfer"})
        ag.task_claim({"agent": "claude", "task_id": task_id})
        old = ag.task({"task_id": task_id})["task"]["latest_checkpoint"]
        assert old["author"] == "codex"
        assert old["current_lock_status"]["owner"] == "claude"
        assert old["current_lock_status"]["owner_changed"]
        assert old["current_lock_status"]["missing"]
        with pytest.raises(AgoraError) as exc:
            write(ag, task_id, 0, {"summary": "Ready", "branch": "different", "head": "d" * 40}, agent="claude")
        assert exc.value.status == 409  # The new owner cannot duplicate the previous owner's append.
        with pytest.raises(AgoraError):
            write(ag, task_id, agent="codex")
        assert write(ag, task_id, 1, agent="claude")["checkpoint"]["author"] == "claude"
        ag.task_done({"agent": "claude", "task_id": task_id, "result": "Done"})
        with pytest.raises(AgoraError) as exc:
            write(ag, task_id, 2, agent="claude")
        assert exc.value.status == 409
        with pytest.raises(AgoraError):
            write(ag, task_id, 1, agent="claude")  # Even an exact duplicate is refused after finalization.
        dropped = claimed(ag)
        ag.task_release({"agent": "codex", "task_id": dropped, "drop": True})
        # A dropped task may have no owner, which independently forbids writing.
        with pytest.raises(AgoraError):
            write(ag, dropped)
        assert len(ag.checkpoints({"task_id": task_id})["checkpoints"]) == 2
    finally:
        ag.close()


def test_expired_conflicting_and_reacquired_leases_are_warnings_not_renewals(tmp_path):
    clock = Clock()
    ag = Agora(tmp_path / "agora.db", clock=clock)
    try:
        task_id = claimed(ag)
        first = write(ag, task_id)["checkpoint"]
        resource = first["lock_snapshot"][0]["resource"]
        expires = first["lock_snapshot"][0]["expires"]
        clock.t += 30
        write(ag, task_id, 1)
        assert ag.db.scalar("SELECT expires FROM locks WHERE resource=?", (resource,)) == expires
        clock.t = expires + 1
        status = ag.task({"task_id": task_id})["task"]["latest_checkpoint"]["current_lock_status"]
        assert status["missing"] == [resource] and not status["held"]
        ag.lock({"agent": "claude", "resources": [resource]})
        status = ag.checkpoints({"task_id": task_id})["checkpoints"][0]["current_lock_status"]
        assert status["conflicts"][0]["owner"] == "claude"
        ag.unlock({"agent": "claude", "resources": [resource]})
        ag.lock({"agent": "codex", "task_id": task_id, "resources": [resource]})
        status = ag.task({"task_id": task_id})["task"]["latest_checkpoint"]["current_lock_status"]
        assert status["missing"] == [resource] and not status["held"]
        # Fresh snapshot records the new lease. Reading never claims or renews it.
        fresh = write(ag, task_id, 2)["checkpoint"]
        assert not fresh["current_lock_status"]["missing"]
        acquired = fresh["lock_snapshot"][0]["acquired"]
        assert acquired != first["lock_snapshot"][0]["acquired"]
    finally:
        ag.close()


@pytest.mark.parametrize("payload", [None, [], {}, {"summary": 5}, {"summary": " "},
    {"summary": "x", "unknown": True}, {"summary": "x", "workspace": None},
    {"summary": "x", "head": "abc"}, {"summary": "x", "base_head": "z" * 40},
    {"summary": "x", "branch": False}, {"summary": "x", "next_steps": "one"},
    {"summary": "x", "next_steps": [1]}, {"summary": "x", "artifacts": [float("nan")]},
    {"summary": "x", "tests": [{"name": "x", "status": "passed"}]},
    {"summary": "x", "tests": [{"name": "x", "status": "verified", "evidence": "x"}]},
    {"summary": "x", "tests": [{"name": "x", "status": "passed", "evidence": "x", "extra": "x"}]},
    {"summary": "x", "tests": [{"name": "x", "status": [], "evidence": "x"}]},
    {"summary": "x", "tests": [{"name": "x", "status": "passed", "evidence": 1}]},
    {"summary": "x" * 8001}, {"summary": "x", "artifacts": ["x"] * 51},
    {"summary": "x", "tests": [{"name": "x", "status": "passed", "evidence": "x"}] * 51},
    {"summary": "x", "artifacts": ["x" * 2001]}, {"summary": "\ud800"},
    {"summary": "é" * 8000, "artifacts": ["é" * 2000] * 20}])
def test_payload_validation_before_any_mutation(tmp_path, payload):
    ag = Agora(tmp_path / "agora.db")
    try:
        task_id = claimed(ag)
        before = ag.db.scalar("SELECT count(*) FROM messages")
        seen = ag.db.scalar("SELECT last_seen FROM agents WHERE id='codex'")
        with pytest.raises(AgoraError):
            ag.checkpoint({"agent": "codex", "task_id": task_id, "expected_revision": 0, "payload": payload})
        assert ag.db.scalar("SELECT count(*) FROM task_checkpoints") == 0
        assert ag.db.scalar("SELECT count(*) FROM messages") == before
        assert ag.db.scalar("SELECT last_seen FROM agents WHERE id='codex'") == seen
    finally:
        ag.close()


@pytest.mark.parametrize("bad", [None, True, False, -1, "0", 1.0, float("nan"), 9_223_372_036_854_775_807])
def test_required_strict_cas_type(tmp_path, bad):
    ag = Agora(tmp_path / "agora.db")
    try:
        task_id = claimed(ag)
        with pytest.raises(AgoraError):
            write(ag, task_id, bad)
        with pytest.raises(AgoraError):
            ag.checkpoint({"agent": "codex", "task_id": task_id, "payload": {"summary": "x"}})
        with pytest.raises(AgoraError):
            ag.checkpoint({"agent": "codex", "task_id": task_id, "expected_revision": 0,
                           "payload": {"summary": "x"}, "author": "claude"})
    finally:
        ag.close()


@pytest.mark.parametrize("args", [{"task_id": True}, {"task_id": "1"}, {"task_id": 1, "after_revision": -1},
    {"task_id": 1, "after_revision": True}, {"task_id": 1, "limit": 0}, {"task_id": 1, "limit": 501},
    {"task_id": 1, "limit": 1.5}, {"task_id": 1, "limit": "1"}, {"task_id": 1, "unexpected": 1}])
def test_history_strict_validation(tmp_path, args):
    ag = Agora(tmp_path / "agora.db")
    try:
        claimed(ag)
        with pytest.raises(AgoraError):
            ag.checkpoints(args)
    finally:
        ag.close()


def test_http_cli_and_real_stdio_mcp(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    token = Path(hub.config.token_file).read_text(encoding="utf-8").strip()
    headers = {"Authorization": "Bearer " + token}
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update({"HOARD_HUB_URL": base, "HOARD_HUB_TOKEN_FILE": str(hub.config.token_file),
                "HOARD_HUB_DATA_DIR": str(hub.config.data_dir), "HOARD_HUB_AUTOSTART": "0",
                "PYTHONPATH": str(root) + os.pathsep + env.get("PYTHONPATH", "")})
    try:
        task_id = claimed(hub.facet("agora").agora)
        body = {"agent": "codex", "task_id": task_id, "expected_revision": 0, "payload": {"summary": "HTTP"}}
        assert http(base + "/api/agora/checkpoint", body)[0] == 401
        status, result = http(base + "/api/agora/checkpoint", body, headers=headers)
        assert status == 200 and result["checkpoint"]["revision"] == 1
        assert http(base + "/api/agora/checkpoint", {**body, "payload": {"summary": "stale"}}, headers=headers)[0] == 409
        assert http(base + "/api/agora/checkpoint", {**body, "agent": "luis"}, headers=headers)[0] == 403
        assert http(base + f"/api/agora/checkpoints?task_id={task_id}&after_revision=0&limit=1")[1]["checkpoints"][0]["revision"] == 1
        assert http(base + f"/api/agora/checkpoints?task_id={task_id}&limit=oops")[0] == 400
        file = tmp_path / "checkpoint.json"
        file.write_text(json.dumps({"summary": "CLI UTF-8: aún aquí"}, ensure_ascii=False), encoding="utf-8")
        def cli(*args):
            return subprocess.run([sys.executable, str(root / "scripts/agora.py"), "--as", "codex", "--json", *args],
                                  cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        result = cli("checkpoint", str(task_id), "--data-file", str(file), "--expected-revision", "1")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["checkpoint"]["revision"] == 2
        result = cli("checkpoints", str(task_id), "--after", "1", "--limit", "1")
        assert result.returncode == 0 and json.loads(result.stdout)["checkpoints"][0]["payload"]["summary"] == "CLI UTF-8: aún aquí"
        file.write_text('{"summary": NaN}', encoding="utf-8")
        assert cli("checkpoint", str(task_id), "--data-file", str(file), "--expected-revision", "2").returncode != 0
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_agora_checkpoint",
             "arguments": {"agent": "codex", "task_id": task_id, "expected_revision": 2, "payload": {"summary": "MCP"}}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hub_agora_checkpoints",
             "arguments": {"task_id": task_id, "after_revision": 2, "limit": 1}}},
        ]
        mcp = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"],
                             input="\n".join(json.dumps(r) for r in requests) + "\n", cwd=root, env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert mcp.returncode == 0, mcp.stderr
        replies = {r["id"]: r for r in map(json.loads, mcp.stdout.splitlines()) if "id" in r}
        names = [tool["name"] for tool in replies[2]["result"]["tools"]]
        assert names.count("hub_agora_checkpoint") == names.count("hub_agora_checkpoints") == 1
        for id in (3, 4):
            assert not replies[id]["result"].get("isError")
        assert json.loads(replies[3]["result"]["content"][0]["text"])["checkpoint"]["revision"] == 3
        assert json.loads(replies[4]["result"]["content"][0]["text"])["checkpoints"][0]["payload"]["summary"] == "MCP"
        evidence_path(tmp_path, "stdio-mcp.json").write_text(
            json.dumps({"requests": requests, "responses": replies, "returncode": mcp.returncode}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        assert len(TOOLS) == 27
        assert len([t for t in tools.all_tools() if t["name"].startswith("hub_agora_")]) == 29
    finally:
        server.shutdown()
        server.server_close()
        hub.close()


def test_checkpoint_renderer_uses_dom_text_and_localized_labels(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the UI fixture")
    source = Path(__file__).resolve().parents[2] / "hoard_link/hub/ui/agora.js"
    fixture = tmp_path / "renderer.cjs"
    fixture.write_text(r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ""; }
  appendChild(node) { this.children.push(node); return node; }
  set innerHTML(value) { throw new Error("Unsafe HTML sink"); }
}
for (const language of ["es", "en"]) {
  const context = {window: {HubFacets: {ctx: {el: (tag) => new Element(tag),
    L: (labels) => labels[language], fmtWhen: () => "now"}, register: () => {}}}};
  vm.createContext(context);
  const code = fs.readFileSync(process.argv[2], "utf8").replace('  H.register("agora", {',
    '  window.fixtureCheckpoint = checkpointView; H.register("agora", {');
  vm.runInContext(code, context);
  const hostile = '<img src=x onerror="throw 1"><script>alert(1)</script>&';
  const cp = {revision: 1, author: hostile, created: 1,
    payload: {summary: hostile, workspace: hostile, branch: hostile, base_head: hostile, head: hostile,
      next_steps: [hostile], artifacts: [hostile], tests: [{name: hostile, status: "passed", evidence: hostile}]},
    lock_snapshot: [{resource: hostile, owner: hostile, expires: 1}],
    current_lock_status: {owner_changed: true, missing: [hostile], conflicts: [{resource: hostile,
      held: hostile, owner: hostile, task_id: 2}]}};
  const tree = context.window.fixtureCheckpoint(cp);
  const nodes = [];
  function walk(node) { nodes.push(node); node.children.forEach(walk); } walk(tree);
  assert(!nodes.some(n => ["img", "script", "a", "input", "textarea", "button"].includes(n.tag)));
  assert(nodes.filter(n => n.textContent.includes(hostile)).length >= 10);
  assert(nodes.some(n => n.textContent.includes(language === "es" ? "Pruebas declaradas" : "Declared tests")));
  assert(nodes.some(n => n.textContent.includes(language === "es" ? "Revisa los bloqueos" : "Check locks")));
}
console.log("ES/EN checkpoint renderer safely preserves hostile strings as DOM text");
''', encoding="utf-8")
    result = subprocess.run([node, str(fixture), str(source)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("language", ["es", "en"])
def test_headless_browser_task_drawer_and_live_warning_refresh(tmp_path, language):
    """Unmodified UI and real HTTP against an isolated Hub, without touching the desktop."""
    browser_api = pytest.importorskip("playwright.sync_api")
    source = Path(__file__).resolve().parents[2] / "hoard_link/hub/ui/agora.js"
    hostile = '<img src=x onerror="window.executed=true"><script>window.executed=true</script>'
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        clock = Clock()
        ag.clock = clock
        task_id = claimed(ag)
        cp = write(ag, task_id, payload={"summary": hostile, "workspace": hostile, "branch": hostile,
                   "next_steps": [hostile], "artifacts": [hostile],
                   "tests": [{"name": hostile, "status": "failed", "evidence": hostile}]})["checkpoint"]
        base = hub.config.url.rstrip("/")
        with browser_api.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Headless Chromium is not installed")
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1080, "height": 1000})
                errors = []
                responses = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("response", lambda response: responses.append({"url": response.url, "status": response.status}))
                # An API document establishes the isolated Hub's origin without running the whole app shell.
                page.goto(base + f"/api/agora/tasks/{task_id}")
                page.set_content('<html><head><style>[hidden]{display:none!important}body{font:14px Arial;padding:20px;--muted:#555;--line:#aaa;--card-2:#eee;--mono:monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><div id="root"></div></body></html>')
                page.evaluate("""language => {
                  window.executed = false;
                  window.HubFacets = {ctx: {
                    el: (tag,cls='',text='') => { const n=document.createElement(tag); n.className=cls; n.textContent=text; return n; },
                    L: labels => labels[language], fmtWhen: () => '08-10 12:00', toast: () => {},
                    api: async path => (await fetch(path)).json()}, register: (name,facet) => window.fixtureFacet=facet};
                }""", language)
                page.add_script_tag(content=source.read_text(encoding="utf-8"))
                page.evaluate("async () => {window.fixtureFacet.mount(document.getElementById('root')); await window.fixtureFacet.load();}")
                page.locator(".ag-card").click()
                drawer = page.locator(".ag-checkpoint")
                drawer.wait_for()
                assert hostile in drawer.inner_text()
                assert drawer.locator("script,img,a,input,textarea,button").count() == 0
                assert ("Pruebas declaradas" if language == "es" else "Declared tests") in drawer.inner_text()
                assert not page.evaluate("window.executed") and not errors
                clock.t = cp["lock_snapshot"][0]["expires"] + 1
                page.evaluate("async () => {await window.fixtureFacet.tick();}")
                assert ("Revisa los bloqueos" if language == "es" else "Check locks") in drawer.inner_text()
                page.screenshot(path=str(evidence_path(tmp_path, f"checkpoint-drawer-{language}.png")), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                evidence_path(tmp_path, f"browser-{language}.json").write_text(json.dumps({
                    "language": language, "http_responses": responses, "drawer_text": drawer.inner_text(),
                    "hostile_elements": drawer.locator("script,img,a,input,textarea,button").count(),
                    "executed": page.evaluate("window.executed"), "page_errors": errors,
                    "viewport": {"width": 1080, "height": 1000}}, ensure_ascii=False, indent=2), encoding="utf-8")
                assert any(r["url"].endswith(f"/api/agora/tasks/{task_id}") and r["status"] == 200 for r in responses)
            finally:
                browser.close()
