from types import SimpleNamespace
from collections import defaultdict
from hoard_link.hub import services


def test_owner_catalogues_and_missing_services_do_not_start_models(monkeypatch):
    owners = {owner for _, owner, _ in services.SERVICES} - {"borges", "hub"}
    apps = {owner: SimpleNamespace(id=owner, name=owner.title(), url="http://fixture.invalid") for owner in owners | {"funes", "prospero"}}
    expected = defaultdict(set)
    for key, owner, required in services.SERVICES:
        if owner in owners and key not in ("stt", "tts"):
            expected[owner].update(required)
    expected["funes"].update(services.SERVICE_TOOLS["stt"])
    calls = []
    def catalogue(app, timeout):
        calls.append(app)
        names = expected[app.id]
        return {"ok": True, "tools": [{"name": name} for name in names]}
    monkeypatch.setattr(services.contract, "app_tools", catalogue)
    hub = SimpleNamespace(get=apps.get, facet=lambda key: SimpleNamespace(settings=lambda: {"enabled": True},
                          mail_accounts=lambda: {"ok": True, "available": True}))
    facet = services.ServicesFacet(hub)
    rows = {r["service"]: r for r in facet.status()["services"]}
    assert rows["media"]["available"] and rows["docs"]["available"] and rows["web"]["available"]
    assert rows["stt"]["available"] and rows["stt"]["owner"] == "funes"
    assert rows["stt"]["registered_owner"] == "boltagon" and rows["stt"]["fallback"] is True
    assert rows["dictation"]["available"] and rows["dictation"]["owner"] == "boltagon"
    assert rows["dictation"]["tools"] == [
        "dictation_understand", "dictation_audio", "dictation_session_open", "dictation_session_get",
        "dictation_session_turn", "dictation_session_fix", "dictation_session_cancel", "dictation_session_undo",
    ]
    assert rows["tts"]["state"] == "incompatible" and rows["tts"]["missing_tools"] == ["voice_tts"]
    assert rows["embed"]["state"] == "missing"
    assert rows["storage"]["available"] and rows["storage"]["owner"] == "atlas"
    assert rows["mail"]["available"] and rows["mail"]["owner"] == "hub"
    calls_at_refresh = len(calls)
    assert facet.status()["services"] == list(rows.values()) and len(calls) == calls_at_refresh
    facet.status(refresh=True)
    assert len(calls) == 2 * calls_at_refresh
