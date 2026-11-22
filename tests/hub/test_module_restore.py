"""A restart puts back the native modules that were on; the ones that were off stay off."""
from __future__ import annotations

import pytest

from hoard_link.hub import production
from hoard_link.hub.core import Hub

from .test_hub_and_server import _http, served  # noqa: F401 - the fixture and the helper

CONTROLS = {"studio": {"list": "mods_list", "set": "mods_set", "name_field": "id"}}


class NativeModules:
    """What a Hoard keeps about its modules: it comes back with every one of them off, as Prospero does."""

    def __init__(self, hub, rows):
        self.rows = {name: dict(enabled=enabled, automatic=automatic)
                     for name, (enabled, automatic) in rows.items()}
        self.calls = []
        self.refuse = set()
        self.unreadable = False
        hub.get("launch").family["module_controls"] = CONTROLS
        self.hub = hub
        real_call, real_start = hub.call_app, hub.start

        def call(app_id, tool, args=None, **kwargs):
            if app_id != "launch":
                return real_call(app_id, tool, args, **kwargs)
            self.calls.append((tool, dict(args or {})))
            if self.unreadable and tool == "mods_list":
                return {"ok": False, "status": 502, "error": "not reachable"}
            if tool == "mods_list":
                return {"ok": True, "result": {"modules": [{"id": name, **row} for name, row in self.rows.items()]}}
            if args["module"] in self.refuse:
                return {"ok": True, "result": {"ok": False, "error": "needs the GPU"}}
            self.rows[args["module"]].update(enabled=args["enabled"])
            if "automatic" in args:
                self.rows[args["module"]]["automatic"] = args["automatic"]
            return {"ok": True, "result": {"ok": True}}

        def start(app_id, *args, **kwargs):
            result = real_start(app_id, *args, **kwargs)
            if app_id == "launch":
                for row in self.rows.values():
                    row.update(enabled=False)
            return result

        hub.call_app, hub.start = call, start

    def sets(self):
        return [args for tool, args in self.calls if tool == "mods_set"]


@pytest.fixture
def running(hub):
    assert hub.start("launch")["ok"]
    return hub


def test_restart_switches_on_again_what_was_on_with_its_automatic_mode(running):
    native = NativeModules(running, {"generation": (True, False), "audio": (False, True), "video": (True, True)})
    result = running.restart("launch")
    assert result["ok"]
    modules = result["modules"]
    assert {(row["module"], row["enabled"]) for row in modules["before"]} == {
        ("generation", True), ("audio", False), ("video", True)}
    assert {(row["module"], row["enabled"]) for row in modules["after"]} == {
        ("generation", True), ("audio", False), ("video", True)}
    assert [row["module"] for row in modules["restored"]] == ["generation", "video"]
    assert modules["failed"] == []
    assert native.rows["generation"] == {"enabled": True, "automatic": False}
    assert native.rows["audio"]["enabled"] is False
    assert {args["module"]: args["automatic"] for args in native.sets()} == {"generation": False, "video": True}
    assert "generation" in modules["message"]["en"] and "generation" in modules["message"]["es"]
    assert "vuelto a encender" in modules["message"]["es"] and "switched on again" in modules["message"]["en"]
    # each restored module carries the request that would switch it off again
    undo = modules["restored"][0]["undo"]
    assert undo["tool"] == "hub_module_set" and undo["arguments"]["enabled"] is False


def test_a_module_that_cannot_be_restored_is_reported_and_the_rest_come_back(running):
    native = NativeModules(running, {"generation": (True, True), "video": (True, True)})
    native.refuse.add("generation")
    result = running.restart("launch")
    assert result["ok"]
    modules = result["modules"]
    assert [row["module"] for row in modules["restored"]] == ["video"]
    assert [(row["module"], row["error"]) for row in modules["failed"]] == [("generation", "needs the GPU")]
    assert "generation" in modules["message"]["es"] and "No se pudo restaurar" in modules["message"]["es"]
    assert "Could not restore" in modules["message"]["en"]
    assert {row["module"]: row["enabled"] for row in modules["after"]} == {"generation": False, "video": True}


def test_modules_that_were_all_off_stay_off_and_nothing_is_written(running):
    native = NativeModules(running, {"generation": (False, True), "audio": (False, False)})
    result = running.restart("launch")
    assert result["modules"]["restored"] == [] and result["modules"]["failed"] == []
    assert native.sets() == []
    assert result["modules"]["message"]["es"] == "Ningún módulo necesitaba restaurarse."
    assert result["modules"]["message"]["en"] == "No module needed restoring."


def test_an_unreadable_app_after_the_restart_is_reported_not_hidden(running, monkeypatch):
    native = NativeModules(running, {"generation": (True, True)})
    monkeypatch.setattr(production.time, "sleep", lambda _seconds: None)
    real_start = running.start

    def start_then_unreadable(app_id, *args, **kwargs):
        result = real_start(app_id, *args, **kwargs)
        native.unreadable = True
        return result

    monkeypatch.setattr(running, "start", start_then_unreadable)
    result = running.restart("launch")
    assert result["ok"]
    modules = result["modules"]
    assert modules["after"] is None and modules["restored"] == []
    assert modules["failed"][0]["error"] == "The modules could not be read after the restart"
    assert "No se pudieron leer los módulos tras el reinicio" in modules["message"]["es"]
    assert [row["module"] for row in modules["before"]] == ["generation"]


def test_an_app_that_did_not_start_again_says_so(running, monkeypatch):
    NativeModules(running, {"generation": (True, True)})
    monkeypatch.setattr(running, "start", lambda app_id, *a, **kw: {"ok": False, "app": app_id, "error": "boom"})
    result = running.restart("launch")
    assert result["ok"] is False
    assert result["modules"]["failed"][0]["error"] == "The app did not start again"
    assert result["modules"]["restored"] == []


def test_an_app_without_declared_modules_restarts_as_before(running):
    result = running.restart("launch")
    assert result["ok"] and "modules" not in result


def test_a_stopped_app_has_nothing_to_remember(hub):
    native = NativeModules(hub, {"generation": (True, True)})
    result = hub.restart("launch")  # it was not running: this is a start
    assert result["ok"] and "modules" not in result
    assert native.calls == []


def test_an_automatic_restart_that_is_held_back_by_work_does_not_touch_modules(running, monkeypatch):
    native = NativeModules(running, {"generation": (True, True)})
    monkeypatch.setattr(Hub, "work_in_progress", staticmethod(lambda app: {"working": True}))
    result = running.restart("launch", automatic=True)
    assert result["skipped"] and "modules" not in result
    assert native.sets() == [] and native.rows["generation"]["enabled"] is True


def test_the_screen_and_the_action_routes_return_the_same_report(served):
    hub, url = served
    assert hub.start("launch")["ok"]
    NativeModules(hub, {"generation": (True, True), "audio": (False, True)})
    status, body = _http(f"{url}/api/apps/launch/restart", {})
    assert status == 200 and body["ok"]
    assert [row["module"] for row in body["modules"]["restored"]] == ["generation"]
    assert set(body["modules"]["message"]) == {"es", "en"}
