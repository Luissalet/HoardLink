import json

import pytest

from ._hub_fakes import FakeApp, http, make_hub, serve


def test_select_native_dossiers_from_orders(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    engagement = {"id": "eng-a", "name": "Presentation", "items": {}, "deliverables": []}
    scene = {"schema": "hoard.production-dossier/1", "subject": {"id": "scene-b"},
             "preserveDetails": ["Tube on the left"]}
    packet = {"job_id": "job-c", "packet_hash": "native-revision", "case": {"id": "case-c"}, "sources": []}
    drucker = FakeApp("drucker", handlers={
        "drucker_engagements": lambda args: [engagement],
        "drucker_engagement_get": lambda args: engagement,
    })
    writer = FakeApp("writer", handlers={
        "wh_list_projects": lambda args: {"projects": [{"id": "project-b", "title": "Cartographer"}]},
        "wh_list_writings": lambda args: {"writings": [{"id": "chapter-b", "title": "Rain"}]},
        "wh_production_dossier": lambda args: scene,
    })
    hub = make_hub(tmp_path, [drucker, writer])
    hub.config.isolated_catalog = True
    hub.config.faustus_urls = []

    def forbidden(*args, **kwargs):
        pytest.fail("A shared catalogue or copilot endpoint was probed")

    monkeypatch.setattr(hub, "backends", forbidden)
    monkeypatch.setattr(hub, "services", forbidden)
    monkeypatch.setattr("hoard_link.hub.copilot.host_relay", forbidden)
    server = serve(hub)
    try:
        assert http(hub.config.url + "/api/backends?force=1")[1]["isolated"]
        assert http(hub.config.url + "/api/services")[1]["isolated"]
        assert http(hub.config.url + "/api/copilot/launcher.js")[0] == 204
        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except playwright.Error as exc:
                if "is not found" not in str(exc):
                    raise
                pytest.skip("Chrome is unavailable")
            try:
                page = browser.new_page(locale="en-GB", viewport={"width": 390, "height": 844})
                page.goto(hub.config.url + "/#hub/orders")
                source = page.get_by_label("Dossier source")

                def create(title, expected):
                    page.get_by_label("Title", exact=True).fill(title)
                    with page.expect_response(lambda r: r.url.endswith("/api/agora/order_create")) as saved:
                        page.get_by_role("button", name="Create order").click()
                    native = saved.value.json()["order"]["dossier"]["native"]
                    assert native["payload"] == expected
                    assert len(native["sha256"]) == 64
                    page.get_by_label("Dossier source").select_option("")
                    return native

                source.select_option("drucker")
                page.get_by_label("Engagement").select_option("eng-a")
                assert create("Drucker source", engagement)["tool"] == "drucker_engagement_get"
                assert drucker.calls[-1]["arguments"] == {"engagement_id": "eng-a"}

                source.select_option("writer")
                page.get_by_label("Writer project").select_option("project-b")
                page.get_by_label("Chapter").select_option("chapter-b")
                assert create("Writer source", scene)["object"] == "scene-b"
                assert writer.calls[-1]["arguments"] == {"projectId": "project-b", "writingId": "chapter-b"}

                source.select_option("file")
                page.get_by_label("Dossier file", exact=True).set_input_files({
                    "name": "evidence.json", "mimeType": "application/json",
                    "buffer": json.dumps({"tool": "coase_backend_packet", "app": "coase", "result": packet}).encode(),
                })
                assert create("Coase source", packet)["source_rev"] == "native-revision"
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
        drucker.stop()
        writer.stop()
