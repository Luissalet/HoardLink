"""The pending queue and the dated production record share the existing gate state."""

import base64
import json
from pathlib import Path

import pytest

from hoard_link.hub.agora import TOOLS
from ._hub_fakes import http, make_hub, serve


def test_pending_queue_opens_exact_gate_and_uses_the_production_record(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    hub = make_hub(tmp_path)
    root = hub.facet("agora").agora
    root.space_create({"agent": "builder", "id": "test-studio", "title": "Test Studio"})
    ag = root.for_space("test-studio")
    order = ag.order_create({"agent": "builder", "title": "Test order", "words": "Draw a map", "folder": str(tmp_path)})["order"]
    (tmp_path / "map.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII="))
    piece = ag.piece_add({"agent": "builder", "order_id": order["id"], "title": "Map", "kind": "image", "path": "map.png"})["piece"]
    gate = ag.gate_open({"agent": "builder", "order_id": order["id"], "title": "Choose the map", "piece_ids": [piece["id"]]})["gate"]

    server = serve(hub)
    url = hub.config.url
    try:
        headers = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        status, _ = http(url + "/api/agora/gate_decide", {"agent": "builder", "space_id": "test-studio",
                         "gate_id": gate["id"], "decision": "keep", "_person": True}, headers)
        assert status == 403
        assert not {"hub_agora_gate_decide", "hub_agora_draft_decide"} & {tool["name"] for tool in TOOLS}

        with playwright.sync_playwright() as runtime:
            try:
                browser = runtime.chromium.launch(channel="chrome", headless=True)
            except playwright.Error as error:
                if "not found" in str(error).lower() or "executable doesn't exist" in str(error).lower():
                    pytest.skip("Installed Chrome is required for the headless browser test")
                raise
            page = browser.new_page(viewport={"width": 390, "height": 844}, locale="en-GB")
            page.goto(url + "/#hub/pending_decisions")
            row = page.locator(".hub-decision-row")
            playwright.expect(row).to_have_count(1)
            playwright.expect(row.get_by_text("Choose the map")).to_be_visible()
            playwright.expect(row.get_by_text("Test order")).to_be_visible()
            playwright.expect(row.locator("img")).to_have_attribute("alt", "Map")
            playwright.expect(page.locator("#hub-global-decision-count")).to_have_text("1")
            row.click()
            playwright.expect(page.locator(f'[data-gate="{gate["id"]}"] h3')).to_be_focused()
            playwright.expect(page.get_by_role("button", name="Keep", exact=True)).to_be_visible()

            # A fictitious receipt for this test's order, independent of any person's live decision.
            with ag.db.tx():
                ag.db.execute("UPDATE agora_gates SET status='decided' WHERE id=?", (gate["id"],))
                ag.db.insert("agora_order_decisions", {
                    "space_id": "test-studio", "order_id": order["id"], "gate_id": gate["id"], "piece_id": piece["id"],
                    "decision": "keep", "note": "The folded corner stays", "created": 1795104000,
                    "situation": json.dumps({"gate": {"id": gate["id"], "title": gate["title"]},
                                             "order": {"id": order["id"], "title": order["title"]}}),
                })
            status, log = http(url + "/api/agora/decisions?space_id=test-studio", headers=headers)
            assert status == 200
            assert log["decisions"] == []
            assert [item["note"] for item in log["production_decisions"]] == ["The folded corner stays"]
            page.goto(url + "/?space_id=test-studio#agora-decisions")
            record = page.locator(".hub-decision-record")
            playwright.expect(record).to_contain_text("Luis’s decision (")
            playwright.expect(record).to_contain_text("The folded corner stays")
            playwright.expect(page.locator("#hub-agora-decisions .hub-decision-row")).to_have_count(0)
            playwright.expect(page.locator("#hub-global-decision-count")).to_have_text("")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
