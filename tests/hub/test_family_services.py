import threading
from types import SimpleNamespace
from collections import defaultdict

import pytest

from hoard_link.hub.core import Hub
from hoard_link.hub.registry import scan

from .conftest import write_manifest
from hoard_link.hub import services


@pytest.mark.parametrize(("installed", "alias"), [("boltagon", "cyrano"), ("cyrano", "boltagon")])
def test_voice_app_alias_uses_one_catalogue_entry_and_start_lock(tmp_path, monkeypatch, caplog, installed, alias):
    write_manifest(tmp_path / "Voice", installed, 5253, name="Boltagon's Hoard")
    hub = Hub.__new__(Hub)
    hub._lock = threading.RLock()
    hub._start_locks = {}
    hub._stopping_all = threading.Event()
    hub._manual_stops = {installed}
    hub._apps = {app.id: app for app in scan([str(tmp_path)])}
    hub.config = SimpleNamespace(isolated=False)
    hub.placement = SimpleNamespace(local_block=lambda _: None)

    [voice] = hub.apps
    assert voice.id == installed and hub.get(alias) is voice
    assert hub.get("unknown") is None
    assert f"App ID {alias} resolved to {installed}" in caplog.text
    assert hub.automation_blocked(alias)
    assert hub.start(alias, automatic=True) == {
        "ok": False, "app": installed, "error": "manually stopped; start it explicitly to resume",
    }
    assert list(hub._start_locks) == [installed]

    stopped = []
    monkeypatch.setattr(hub, "_manual_stop", lambda ids: stopped.extend(ids))
    monkeypatch.setattr(hub, "_stop_app", lambda app: {"ok": True, "app": app.id})
    assert hub.stop(alias) == {"ok": True, "app": installed}
    assert stopped == [installed, installed] and list(hub._start_locks) == [installed]

    monkeypatch.setattr(services.contract, "app_tools", lambda app: {"id": app.id})
    assert hub.app_tools(alias) == {"id": installed}
    proxy_calls = []
    monkeypatch.setattr(services.contract, "call_app",
                        lambda app, *args, **kwargs: {"ok": True, "app": app.id})
    monkeypatch.setattr(hub, "_safe_emit", lambda _, data: proxy_calls.append(data))
    assert hub.call_app(alias, "dictation_understand") == {"ok": True, "app": installed}
    assert proxy_calls[0]["app"] == installed


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
