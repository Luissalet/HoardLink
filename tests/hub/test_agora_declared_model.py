"""External sessions report their model without replacing verified session settings."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hoard_link.hub.agora import Agora, DECLARED_MODEL_MIGRATION, MIGRATIONS
from hoard_link.hub import tools
from hoard_link.sqlkit import Database
from ._hub_fakes import http, make_hub, serve
from .test_agora import ag


DECLARED = ("declared_model", "declared_effort", "declared_at")


def declaration(row):
    return {key: row[key] for key in DECLARED}


def test_declaration_survives_empty_and_partial_heartbeats_in_its_space(ag):
    ag.space_create({"agent": "builder", "id": "project", "title": "Project"})
    ag.heartbeat({"agent": "builder", "model": "  external-model  ", "effort": " high "})
    expected = {"declared_model": "external-model", "declared_effort": "high",
                "declared_at": datetime.fromtimestamp(ag.fake_clock.t, timezone.utc).isoformat()}
    for result, key in ((ag.agents(), "agents"), (ag.board(), "agents"), (ag.members(), "members"),
                        (ag.guild(), "members")):
        assert declaration(next(row for row in result[key] if row["id"] == "builder")) == expected
    ag.fake_clock.t += 20
    for args in ({}, {"model": None, "effort": "  "}, {"model": ""}):
        ag.heartbeat({"agent": "builder", **args})
        assert declaration(ag.agents()["agents"][0]) == expected
    # The same identity in another space has its own independent report.
    other = ag.for_space("project")
    other.heartbeat({"agent": "builder"})
    assert declaration(other.agents()["agents"][0]) == dict.fromkeys(DECLARED)
    other.heartbeat({"agent": "builder", "model": "project-model", "effort": "low"})
    assert declaration(ag.agents()["agents"][0]) == expected
    synced = ag.sync({"agent": "builder", "effort": "medium"})
    expected.update(declared_effort="medium", declared_at=datetime.fromtimestamp(ag.fake_clock.t, timezone.utc).isoformat())
    assert declaration(synced["agent_state"]) == expected


def test_declaration_truncates_after_whitespace_and_is_isolated_by_agent(ag):
    ag.heartbeat({"agent": "builder", "model": "  " + "m" * 90 + "  ", "effort": "e" * 30})
    row = ag.agents()["agents"][0]
    assert row["declared_model"] == "m" * 80
    assert row["declared_effort"] == "e" * 20
    ag.heartbeat({"agent": "other", "effort": "low"})
    row = next(row for row in ag.members()["members"] if row["id"] == "other")
    assert row["declared_model"] is None and row["declared_effort"] == "low"


def test_upgrade_and_reopen_preserve_existing_membership_and_declaration(tmp_path):
    path = tmp_path / "legacy.db"
    old = Database(path, migrations=MIGRATIONS[:MIGRATIONS.index(DECLARED_MODEL_MIGRATION)])
    old.execute("INSERT INTO agents(id,name,state,last_seen,created) VALUES('builder','Builder','idle',10,10)")
    old.execute("INSERT INTO coordination_members(space_id,agent,created) VALUES('ecosystem','builder',10)")
    before = dict(old.one("SELECT * FROM agents WHERE id='builder'"))
    old.close()
    current = Agora(path)
    try:
        assert dict(current.db.one("SELECT * FROM agents WHERE id='builder'")) == before
        assert declaration(current.members()["members"][0]) == dict.fromkeys(DECLARED)
        current.heartbeat({"agent": "builder", "model": "persisted", "effort": "high"})
        expected = declaration(current.agents()["agents"][0])
    finally:
        current.close()
    reopened = Agora(path)
    try:
        reopened.heartbeat({"agent": "builder"})
        assert declaration(reopened.members()["members"][0]) == expected
    finally:
        reopened.close()


def test_http_cli_and_real_stdio_mcp_share_declaration(tmp_path):
    root = Path(__file__).resolve().parents[2]
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        hub.config.agent_watch = {"enabled": False}
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        env = {**os.environ, "HOARD_HUB_URL": hub.config.url,
               "HOARD_HUB_TOKEN_FILE": str(hub.config.token_file), "HOARD_HUB_DATA_DIR": str(hub.config.data_dir),
               "HOARD_HUB_AUTOSTART": "0", "PYTHONPATH": str(root), "AGORA_SPACE": "ecosystem"}
        cli = subprocess.run([sys.executable, str(root / "scripts/agora.py"), "--as", "builder", "--json",
                              "hb", "--model", "cli-model", "--effort", "high"],
                             cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["ok"]
        status, result = http(hub.config.url + "/api/agora/members")
        assert status == 200 and result["members"][0]["declared_model"] == "cli-model"
        status, result = http(hub.config.url + "/api/agora/heartbeat", {"agent": "builder", "effort": "low"},
                              headers={"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()})
        assert status == 200 and result["ok"]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_agora_heartbeat", "arguments": {"agent": "builder", "model": "mcp-model"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "hub_agora_heartbeat", "arguments": {"agent": "builder"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "hub_agora_board", "arguments": {}}},
        ]
        process = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                 input="\n".join(json.dumps(row) for row in messages) + "\n", capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
        assert process.returncode == 0, process.stderr
        replies = {row["id"]: row for row in map(json.loads, process.stdout.splitlines()) if "id" in row}
        spec = next(row for row in replies[2]["result"]["tools"] if row["name"] == "hub_agora_heartbeat")
        assert {"model", "effort"} <= spec["inputSchema"]["properties"].keys()
        for index in (3, 4, 5):
            assert not replies[index]["result"].get("isError"), replies[index]
        result = tools.call(hub, "hub_agora_members", {})
        assert result["members"][0]["declared_model"] == "mcp-model"
        assert result["members"][0]["declared_effort"] == "low"
        assert datetime.fromisoformat(result["members"][0]["declared_at"]).tzinfo is not None
        (tmp_path / "interfaces.json").write_text(json.dumps({"cli": json.loads(cli.stdout), "mcp": replies,
                                                             "members": result}, indent=2), encoding="utf-8")


@pytest.mark.parametrize("width,language", [(1440, "es"), (390, "en")])
def test_hub_agent_cards_show_declared_model_and_preserve_verified_values(tmp_path, width, language):
    browser_api = pytest.importorskip("playwright.sync_api")
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        hub.config.agent_watch = {"enabled": False}
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        hub.facet("agora").agora.heartbeat({"agent": "builder", "model": "external-model", "effort": "high"})
        with browser_api.sync_playwright() as p, ExitStack() as browser_cleanup:
            browser = p.chromium.launch(headless=True)
            browser_cleanup.callback(browser.close)
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(hub.config.url)
            page.evaluate("language => localStorage.setItem('hub.lang', language)", language)
            page.reload()
            page.goto(hub.config.url + "/#hub/agora")
            line = page.locator('.ag-agent [data-model-source="declared"]')
            browser_api.expect(line).to_contain_text("external-model · high")
            browser_api.expect(line).to_contain_text("Declarado por el agente" if language == "es" else "Declared by the agent")
            assert line.get_attribute("title")
            line.scroll_into_view_if_needed()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / f"declared-{width}.png"))
            # The renderer keeps verified session values ahead of the self-report.
            def verified(route):
                response = route.fetch()
                body = response.json()
                body["agents"][0].update(model="verified-model", effort="medium")
                route.fulfill(response=response, json=body)
            page.route("**/api/agora/board*", verified)
            page.reload()
            line = page.locator('.ag-agent [data-model-source="verified"]')
            browser_api.expect(line).to_have_text("verified-model · medium")
            assert page.locator('[data-model-source="declared"]').count() == 0
            assert not errors
