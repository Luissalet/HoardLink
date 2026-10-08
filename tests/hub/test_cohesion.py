from types import SimpleNamespace

from hoard_link.hub import services, tools
from hoard_link.hub.facets import Request
from ._hub_fakes import make_hub, http, serve


def test_embeddings_ready_does_not_mean_library_import_is_ready(monkeypatch):
    apps = {owner: SimpleNamespace(name=owner, url="http://127.0.0.1:1") for _, owner, _ in services.SERVICES}
    expected = {id(apps[owner]): list(required) for _, owner, required in services.SERVICES}
    def catalogue(app, timeout):
        return {"ok": True, "tools": [{"name": t} for t in expected[id(app)]]}
    monkeypatch.setattr(services.contract, "app_tools", catalogue)
    hub = SimpleNamespace(get=apps.get, apps=[], facet=lambda k: None)
    facet = services.ServicesFacet(hub)
    result = facet.status()
    assert next(r for r in result["services"] if r["service"] == "embed")["available"]
    assert result["workflows"][0]["missing"] == [{"owner": "borges", "tool": "library_add_collection"},
                                                 {"owner": "borges", "tool": "library_reindex"}]
    expected[id(apps["borges"])].extend(["library_add_collection", "library_reindex"])
    assert facet.status(refresh=True)["workflows"][0]["available"]
    facet.close()


def test_cohesion_tool_and_http_share_actual_registry_and_overlap_metadata(tmp_path):
    hub = make_hub(tmp_path, extra_apps=["links", "funes"])
    try:
        for app in hub.apps:
            app.capabilities = ["local_search"]
        report = tools.call(hub, "hub_cohesion", {})
        assert report["ok"]
        assert report["capability_overlaps"][0]["apps"] == sorted(a.id for a in hub.apps)
        assert report["capability_overlaps"][0]["interpretation"] == "review_responsibilities"
        assert all(r["state"] == "missing" for r in report["services"] if r["service"] in ("tts", "docs", "embed"))
        server = serve(hub)
        try:
            status, body = http(f"http://127.0.0.1:{hub.config.port}/api/services/cohesion")
            assert status == 200 and body["apps"] == report["apps"]
        finally:
            server.shutdown()
            server.server_close()
        assert {"hub_cohesion", "hub_media_import_resume", "hub_media_imports"} <= {t["name"] for t in tools.all_tools()}
    finally:
        hub.close()


def test_import_inspection_and_reconciliation_require_hub_operator(monkeypatch):
    facet = services.ServicesFacet(SimpleNamespace())
    req = Request(method="GET", path="/api/services/imports", query={}, body={}, caller=lambda: "links", agent=lambda: False)
    assert facet.get(req)["status"] == 403
    req.method, req.path = "POST", "/api/services/imports/resume"
    assert facet.post(req)["status"] == 403
    facet.close()
