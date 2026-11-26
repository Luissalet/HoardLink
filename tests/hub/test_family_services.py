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


def test_slow_voice_catalogue_is_retried_and_available_fallback_wins(monkeypatch):
    app_ids = ("boltagon", "funes", "prospero")
    apps = {app: SimpleNamespace(id=app, name=app, url="http://fixture.invalid") for app in app_ids}
    apps["cyrano"] = apps["boltagon"]
    hub = SimpleNamespace(get=apps.get, facet=lambda key: None)
    slow = {"boltagon", "funes"}

    def catalogue(app, timeout):
        assert timeout == 2
        if app.id in slow:
            return {"ok": False, "transport": "timeout"}
        tools = services.SERVICE_TOOLS["stt" if app.id == "funes" else "tts"]
        return {"ok": True, "tools": [{"name": name} for name in tools]}

    monkeypatch.setattr(services.contract, "app_tools", catalogue)
    facet = services.ServicesFacet(hub)

    def stt():
        return next(row for row in facet.status()["services"] if row["service"] == "stt")

    timed_out = stt()
    assert timed_out["state"] == "timed_out" and not timed_out["available"]
    slow.remove("funes")
    found = stt()
    assert found["state"] == "ready" and found["available"]
    assert found["owner"] == "funes" and found["registered_owner"] == "boltagon"
