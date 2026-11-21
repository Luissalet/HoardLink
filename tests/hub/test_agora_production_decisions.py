"""The pending queue and the dated production record share the existing gate state."""

import base64
import json
import re
from pathlib import Path

import pytest

from hoard_link.hub.agora import TOOLS
from ._hub_fakes import http, make_hub, serve


def test_decision_copy_has_no_installer_name():
    root = Path(__file__).resolve().parents[2]
    files = (
        "apps/agora/src/ProductionDecisions.tsx",
        "hoard_link/hub/ui/production_decisions.js",
        "hoard_link/hub/ui/production_decisions_data.js",
        "hoard_link/hub/ui/agora_debate.js",
        "hoard_link/hub/agora_orders.py",
    )
    for path in files:
        assert not re.search(r"\bluis\b", (root / path).read_text(encoding="utf-8"), re.IGNORECASE), path


@pytest.mark.parametrize("display_name", ["Avery Chen", "Núria Serra"])
def test_cancel_uses_the_deciding_member_in_the_receipt_and_task_thread(tmp_path, display_name):
    ag = make_hub(tmp_path).facet("agora").agora
    actor = "fixture-editor"
    ag._touch(actor)
    ag.db.execute("UPDATE agents SET name=? WHERE id=?", (display_name, actor))
    order = ag.order_create({"agent": "builder", "title": "Test order", "words": "Make a map",
                             "folder": str(tmp_path)})["order"]
    plan = ag.plan_set({"agent": "builder", "order_id": order["id"], "jobs": [
        {"key": "map", "title": "Make the map", "station": "mapping", "produces": ["text"]},
    ]})
    task_id = plan["keys"]["map"]
    (tmp_path / "map.txt").write_text("East", encoding="utf-8")
    piece = ag.piece_add({"agent": "builder", "order_id": order["id"], "task_id": task_id,
                          "kind": "text", "title": "Map", "path": "map.txt"})["piece"]
    gate = ag.gate_open({"agent": "builder", "order_id": order["id"], "title": "Map review",
                         "piece_ids": [piece["id"]]})["gate"]

    ag.gate_decide({"agent": actor, "_person": True, "gate_id": gate["id"], "decision": "cancel"})

    record, = ag.order_decisions({"order_id": order["id"]})["decisions"]
    assert record["decided_by"] == {"id": actor, "name": display_name}
    assert record["situation"]["decided_by"] == actor
    thread_id = ag._task_row(task_id)["thread_id"]
    message = ag.db.one("SELECT author, body FROM messages WHERE thread_id=? ORDER BY id DESC", (thread_id,))
    assert message["author"] == actor
    assert message["body"].startswith(f"{display_name} cancela la pieza")


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
            row.get_by_role("link", name="Open order").click()
            playwright.expect(page.locator(f'[data-gate="{gate["id"]}"] h3')).to_be_focused()
            playwright.expect(page.get_by_role("button", name="Keep", exact=True)).to_be_visible()

            # An attributed receipt for this test's order, with a different member from the viewer.
            actor = "fixture-editor"
            ag._touch(actor)
            with ag.db.tx():
                ag.db.execute("UPDATE agents SET name=? WHERE id=?", ("Núria Serra", actor))
                ag.db.execute("UPDATE agora_gates SET status='decided' WHERE id=?", (gate["id"],))
                ag.db.insert("agora_order_decisions", {
                    "space_id": "test-studio", "order_id": order["id"], "gate_id": gate["id"], "piece_id": piece["id"],
                    "decision": "keep", "note": "The folded corner stays", "created": 1795104000,
                    "situation": json.dumps({"decided_by": actor, "gate": {"id": gate["id"], "title": gate["title"]},
                                             "order": {"id": order["id"], "title": order["title"]}}),
                })
            status, log = http(url + "/api/agora/decisions?space_id=test-studio", headers=headers)
            assert status == 200
            assert log["decisions"] == []
            assert [item["note"] for item in log["production_decisions"]] == ["The folded corner stays"]
            page.goto(url + "/?space_id=test-studio#agora-decisions")
            record = page.locator(".hub-decision-record")
            playwright.expect(record).to_contain_text("Decision by Núria Serra (")
            playwright.expect(record).to_contain_text("The folded corner stays")
            playwright.expect(page.locator("#hub-agora-decisions .hub-decision-row")).to_have_count(0)
            playwright.expect(page.locator("#hub-global-decision-count")).to_have_text("")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_gate_shows_registered_views_and_keeps_the_original_file_accessible(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    images = pytest.importorskip("PIL.Image")
    hub = make_hub(tmp_path)
    agora = hub.facet("agora").agora
    order = agora.order_create({"agent": "builder", "title": "Model and notes", "words": "Model a stair",
                                "folder": str(tmp_path)})["order"]
    for i, color in enumerate(("brown", "yellow")):
        images.new("RGB", (16 + i, 12), color).save(tmp_path / f"view-{i}.png")
    piece_ids = []
    for name, previews in [("Stair.stl", ["view-0.png"]), ("Notes.docx", ["view-0.png", "view-1.png"])]:
        (tmp_path / name).write_bytes(name.encode())
        piece = agora.piece_add({"agent": "builder", "order_id": order["id"], "kind": "document",
                                "title": name, "path": name, "previews": previews})["piece"]
        piece_ids.append(piece["id"])
    gate = agora.gate_open({"agent": "builder", "order_id": order["id"], "title": "Review the stair",
                            "piece_ids": piece_ids})["gate"]
    server = serve(hub)
    try:
        with playwright.sync_playwright() as runtime:
            try:
                browser = runtime.chromium.launch(channel="chrome", headless=True)
            except playwright.Error as error:
                if "not found" in str(error).lower() or "executable doesn't exist" in str(error).lower():
                    pytest.skip("Installed Chrome is required for the headless browser test")
                raise
            page = browser.new_page(viewport={"width": 390, "height": 844}, locale="en-GB")
            page.goto(hub.config.url + "/#hub/pending_decisions")
            card = page.locator(f'.production-card[data-decision="gate-{gate["id"]}"]')
            stair = card.get_by_role("listitem").filter(has_text="Stair.stl")
            doc = card.get_by_role("listitem").filter(has_text="Notes.docx")
            playwright.expect(stair.locator("img")).to_have_js_property("naturalWidth", 16)
            doc.get_by_role("button", name="View 2").click()
            playwright.expect(doc.locator("img")).to_have_js_property("naturalWidth", 17)
            playwright.expect(doc.get_by_role("button", name="View 2")).to_have_attribute("aria-pressed", "true")
            zoom = doc.get_by_role("button", name="Enlarge · Notes.docx · 2")
            zoom.click()
            playwright.expect(doc.get_by_role("dialog")).to_be_visible()
            page.keyboard.press("Escape")
            playwright.expect(zoom).to_be_focused()
            with page.expect_download() as download:
                doc.get_by_role("link", name="Download piece · Notes.docx").click()
            assert download.value.suggested_filename == "Notes.docx"
            assert download.value.path().read_bytes() == b"Notes.docx"
            card.get_by_role("link", name="Open order").click()
            full_gate = page.locator(f'[data-gate="{gate["id"]}"]')
            full_stair = full_gate.locator(f'[data-piece="{piece_ids[0]}"]')
            playwright.expect(full_stair.locator("img")).to_have_js_property("naturalWidth", 16)
            playwright.expect(full_stair.get_by_role("link", name="Download piece · Stair.stl")).to_be_visible()

            doc_path = f"/api/agora/piece_file/{piece_ids[1]}"
            page.route(f"**{doc_path}?relay=1", lambda route: route.fulfill(
                body=b"Notes.docx", headers={"Content-Type": "application/octet-stream",
                                            "Content-Disposition": 'attachment; filename="piece-2"'}))
            page.evaluate("""async path => {
                const {renderDecisionCards} = await import('/ui/production_decisions_card.js');
                const container = document.createElement('div');
                document.body.append(container);
                renderDecisionCards(container, [{key: 'relay-test', title: 'Relayed file', artifacts: [
                    {title: 'Notes.docx', filename: 'Notes.docx', path, media: 'file'},
                ]}], {lang: 'en', source: path => path + '?relay=1'});
            }""", doc_path)
            relayed = page.locator('[data-decision="relay-test"]')
            with page.expect_download() as download:
                relayed.get_by_role("link", name="Download piece · Notes.docx").click()
            assert download.value.suggested_filename == "Notes.docx"
            assert download.value.path().read_bytes() == b"Notes.docx"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
