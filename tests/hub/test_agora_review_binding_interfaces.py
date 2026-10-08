"""Observed submission revisions across real HTTP, CLI, stdio MCP and browser."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ._hub_fakes import http, make_hub, serve


def evidence(tmp_path, name):
    out = Path(os.environ.get("REVIEW_BINDING_EVIDENCE_DIR", str(tmp_path)))
    out.mkdir(parents=True, exist_ok=True)
    return out / name


def submit(ag, task_id, commits):
    return ag.task_submit({"agent": "codex", "task_id": task_id,
                           "summary": "Revised implementation and checks", "commits": commits})["task"]


def create(ag):
    task = ag.task_add({"agent": "codex", "title": "Review <img src=x onerror=window.executed=true>",
                        "kind": "feature", "repo": "Example", "claim": True})["task"]
    submit(ag, task["id"], ["aaaa1111"])
    return task["id"]


def test_http_cli_and_stdio_mcp_bind_observed_revision(tmp_path):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        task_id = create(ag)
        latest = submit(ag, task_id, ["bbbb2222"])
        base = hub.config.url.rstrip("/")
        headers = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        vote = {"agent": "claude", "task_id": task_id, "verdict": "approve", "body": "Reviewed A",
                "expected_submission_revision": 1}
        before = ag.task({"task_id": task_id})
        assert http(base + "/api/agora/task_review", vote)[0] == 401
        status, refusal = http(base + "/api/agora/task_review", vote, headers=headers)
        assert status == 409 and refusal["current_revision"] == latest["submission_revision"]
        assert refusal["current_commits"] == ["bbbb2222"]
        assert ag.task({"task_id": task_id}) == before
        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, HOARD_HUB_URL=base, HOARD_HUB_TOKEN_FILE=str(hub.config.token_file),
                   HOARD_HUB_DATA_DIR=str(hub.config.data_dir), HOARD_HUB_AUTOSTART="0", PYTHONPATH=str(root))

        def cli(*args):
            return subprocess.run([sys.executable, str(root / "scripts/agora.py"), "--as", "claude", "--json", *args],
                                  cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)

        result = cli("review", str(task_id), "approve", "--revision", "1", "--body", "A checked")
        assert result.returncode == 1 and json.loads(result.stdout)["status"] == 409
        assert cli("review", str(task_id), "approve", "--body", "No observed revision").returncode == 2
        result = cli("review", str(task_id), "approve", "--revision", "2", "--body", "B checked")
        assert result.returncode == 0, result.stderr
        approved = json.loads(result.stdout)["task"]
        assert approved["reviewed_submission_revision"] == 2 and approved["reviewed_commits"] == ["bbbb2222"]
        submit(ag, task_id, ["bbbb2222"])
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_agora_task_review", "arguments": vote}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hub_agora_task_review",
             "arguments": {**vote, "expected_submission_revision": 3, "verdict": "changes", "body": "Current B needs a test"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
        tool = next(t for t in replies[2]["result"]["tools"] if t["name"] == "hub_agora_task_review")
        assert "expected_submission_revision" in tool["inputSchema"]["required"]
        assert replies[3]["result"]["isError"] is True
        assert not replies[4]["result"].get("isError")
        changed = ag.task({"task_id": task_id})["task"]
        assert changed["status"] == "changes" and changed["reviewed_submission_revision"] == 3
        evidence(tmp_path, "interfaces.json").write_text(json.dumps({"http_refusal": refusal, "cli_approval": approved,
                    "mcp_requests": requests, "mcp_responses": replies, "final_task": changed}, indent=2), encoding="utf-8")


@pytest.mark.parametrize("language", ["es", "en"])
def test_browser_preserves_draft_revision_and_rejects_racing_submit(tmp_path, language):
    playwright = pytest.importorskip("playwright.sync_api")
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        task_id = create(ag)
        base = hub.config.url.rstrip("/")
        source = Path(__file__).resolve().parents[2] / "hoard_link/hub/ui/agora.js"
        with playwright.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Headless Chromium is not installed")
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1080, "height": 950})
                errors, calls = [], []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("request", lambda r: calls.append(json.loads(r.post_data)) if r.url.endswith("/task_review") else None)
                page.goto(base + f"/api/agora/tasks/{task_id}")
                page.set_content('<html><head><style>[hidden]{display:none!important}body{font:14px Arial;padding:20px;--muted:#555;--line:#aaa;--card-2:#eee;--mono:monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><div id="root"></div></body></html>')
                page.evaluate("""language => {
                  window.executed = false; window.toasts = [];
                  window.HubFacets = {ctx: {
                    el: (tag,cls='',text='') => { const n=document.createElement(tag); n.className=cls; n.textContent=text; return n; },
                    L: labels => labels[language], fmtWhen: () => '08-10 12:00', toast: text => window.toasts.push(text),
                    api: async (path,body) => (await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})).json()
                  }, register: (name,facet) => window.fixtureFacet=facet};
                }""", language)
                page.add_script_tag(content=source.read_text(encoding="utf-8"))
                page.evaluate("async () => {window.fixtureFacet.mount(document.getElementById('root')); await window.fixtureFacet.load();}")
                page.locator(".ag-card").click()
                drawer = page.locator(".ag-detail")
                note = drawer.locator("textarea")
                note.fill("Checked submission A; keep this note when polling")
                submit(ag, task_id, ["bbbb2222"])
                approve = drawer.get_by_role("button", name="Aprobar" if language == "es" else "Approve", exact=True)
                with page.expect_response(lambda r: r.url.endswith("/task_review")) as response:
                    approve.click()
                assert response.value.status == 409
                assert ag.task({"task_id": task_id})["task"]["status"] == "review"
                assert calls[-1]["expected_submission_revision"] == 1
                page.evaluate("async () => {await window.fixtureFacet.tick();}")
                assert note.input_value() == "Checked submission A; keep this note when polling"
                assert note.get_attribute("data-review-revision") == "1"
                assert approve.is_disabled()
                assert drawer.get_by_role("button", name="Pedir cambios" if language == "es" else "Ask for changes", exact=True).is_disabled()
                assert "r2" in drawer.inner_text() and not page.evaluate("window.executed")
                page.screenshot(path=str(evidence(tmp_path, f"stale-draft-{language}.png")), full_page=True)
                current = drawer.get_by_role("button", name="Revisar la entrega actual r2" if language == "es" else "Review the current submission r2", exact=True)
                current.click()
                assert note.input_value().startswith("Checked submission A")
                assert note.get_attribute("data-review-revision") == "2" and approve.is_enabled()
                note.fill("Current B inspected: ready")
                with page.expect_response(lambda r: r.url.endswith("/task_review")) as response:
                    approve.click()
                assert response.value.status == 200
                task = ag.task({"task_id": task_id})["task"]
                assert task["status"] == "approved" and task["reviewed_submission_revision"] == 2
                assert task["reviewed_commits"] == ["bbbb2222"] and not errors
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                evidence(tmp_path, f"browser-{language}.json").write_text(json.dumps({"language": language,
                    "requests": calls, "final_task": task, "page_errors": errors, "hostile_markup_executed": False}, indent=2), encoding="utf-8")
            finally:
                browser.close()
