"""Station catalogue, chain planning and native module switches with undo."""
from types import SimpleNamespace

from hoard_link.hub import contract
from hoard_link.hub.production import ProductionFacet


def _check():
    return {"en": "Look at it.", "es": "Míralo."}


def _app(app_id, handoffs=None, modules=None):
    family = {}
    if handoffs is not None:
        family["production"] = {"handoffs": handoffs}
    if modules is not None:
        family["module_controls"] = modules
    return SimpleNamespace(id=app_id, name=app_id.title(), family=family, kind="app")


class FakeHub:
    def __init__(self, apps, modules=None):
        self.apps = apps
        self.modules = modules or {}
        self.calls = []

    def get(self, app_id):
        return next((app for app in self.apps if app.id == app_id), None)

    def facet(self, _name):
        return None

    def call_app(self, app_id, tool, args, **kwargs):
        self.calls.append((app_id, tool, dict(args)))
        if tool == "list_modules":
            return {"ok": True, "result": {"modules": [dict(row) for row in self.modules.values()]}}
        row = self.modules[args["module"]]
        row["enabled"] = args["enabled"]
        if "automatic" in args:
            row["automatic"] = args["automatic"]
        return {"ok": True, "result": {"ok": True, "module": dict(row)}}


def _facet(hub):
    facet = ProductionFacet.__new__(ProductionFacet)
    facet.hub = hub
    facet._switch_locks = {}
    return facet


def test_catalog_plans_the_shortest_chain_first_and_skips_invalid_rows():
    hub = FakeHub([
        _app("writer", [{"accepts": "brief", "produces": ["scene-brief"], "skill": "w", "check": _check()},
                        {"accepts": "broken", "produces": "not-a-list", "skill": "x", "check": _check()}]),
        _app("prospero", [{"accepts": "scene-brief", "produces": ["concept-image"], "skill": "p", "check": _check()},
                          {"accepts": "concept-image", "produces": ["video-clip"], "skill": "v", "check": _check()},
                          {"accepts": "scene-brief", "produces": ["video-clip"], "skill": "d", "check": _check()}]),
        _app("plain"),
    ])
    facet = _facet(hub)
    catalog = facet.catalog({})
    assert [station["app"] for station in catalog["stations"]] == ["prospero", "writer"]
    assert len(catalog["stations"][1]["handoffs"]) == 1

    planned = facet.catalog({"from_type": "brief", "to_type": "video-clip"})
    chains = planned["chains"]
    assert [[step["skill"] for step in chain] for chain in chains] == [["w", "d"], ["w", "p", "v"]]
    assert chains[0][0]["check"]["es"] == "Míralo."
    assert facet.catalog({"from_type": "video-clip", "to_type": "brief"})["chains"] == []
    assert facet.catalog({"from_type": "brief"})["ok"] is False


def test_module_set_switches_natively_and_returns_a_working_undo():
    controls = {"studio": {"list": "list_modules", "set": "set_module", "name_field": "id"}}
    hub = FakeHub([_app("prospero", modules=controls)],
                  {"generation": {"id": "generation", "enabled": False, "automatic": True}})
    facet = _facet(hub)

    listed = facet.modules({"app": "prospero"})
    assert listed["ok"] and listed["modules"][0]["module"] == "generation"

    assert facet.module_set({"app": "prospero", "module": "generation", "enabled": True})["ok"] is False
    changed = facet.module_set({"app": "prospero", "module": "generation", "enabled": True, "reason": "probe"})
    assert changed["changed"] is True
    assert hub.modules["generation"]["enabled"] is True
    assert hub.calls[-1] == ("prospero", "set_module", {"module": "generation", "enabled": True, "automatic": True})

    undo = changed["undo"]
    assert undo["tool"] == "hub_module_set"
    restored = facet.module_set(undo["arguments"])
    assert restored["changed"] is True and hub.modules["generation"]["enabled"] is False
    stale = facet.module_set(undo["arguments"])
    assert stale["ok"] is False and stale["error"] == "Module state changed"


def test_modules_without_declaration_are_refused():
    facet = _facet(FakeHub([_app("writer")]))
    assert facet.modules({"app": "writer"})["ok"] is False


def test_missing_route_only_for_a_missing_endpoint():
    assert contract._missing_route(None)
    assert contract._missing_route({"detail": "Not Found"})
    assert contract._missing_route("<!DOCTYPE html><title>404</title>")
    assert not contract._missing_route({"ok": False, "error": "Subject not found: algebra"})
