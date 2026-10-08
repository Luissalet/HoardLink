from types import SimpleNamespace
from hoard_link.hub import services


def test_owner_catalogues_and_missing_services_do_not_start_models(monkeypatch):
    apps = {owner: SimpleNamespace(name=owner.title(), url="http://127.0.0.1:1") for _, owner, _ in services.SERVICES if owner != "borges"}
    expected = {id(apps[owner]): required for _, owner, required in services.SERVICES if owner in apps}
    calls = []
    def catalogue(app, timeout):
        calls.append(app)
        if app is apps["funes"]:
            return {"ok": False, "error": "not reachable"}
        names = expected[id(app)] if app is not apps["prospero"] else ()
        return {"ok": True, "tools": [{"name": name} for name in names]}
    monkeypatch.setattr(services.contract, "app_tools", catalogue)
    hub = SimpleNamespace(get=apps.get, facet=lambda key: SimpleNamespace(settings=lambda: {"enabled": True}))
    facet = services.ServicesFacet(hub)
    rows = {r["service"]: r for r in facet.status()["services"]}
    assert rows["media"]["available"] and rows["docs"]["available"] and rows["web"]["available"]
    assert rows["stt"]["state"] == "down"
    assert rows["tts"]["state"] == "incompatible" and rows["tts"]["missing_tools"] == ["voice_tts"]
    assert rows["embed"]["state"] == "missing"
    assert rows["storage"]["available"] and rows["storage"]["owner"] == "atlas"
    assert facet.status()["services"] == list(rows.values()) and len(calls) == len(apps)
    facet.status(refresh=True)
    assert len(calls) == 2 * len(apps)
