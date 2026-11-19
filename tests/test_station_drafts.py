"""Each station's draft mode: the manifest's block wins, the hub's starting declaration fills in, chains add up."""
import json
from types import SimpleNamespace

from hoard_link.hub import station_drafts
from hoard_link.hub.production import ProductionFacet
from hoard_link.hub.station_drafts import StationDraftsFacet


def _check():
    return {"en": "Look at it.", "es": "Míralo."}


def _app(app_id, handoffs):
    return SimpleNamespace(id=app_id, name=app_id.title(), family={"production": {"handoffs": handoffs}}, kind="app")


class FakeHub:
    def __init__(self, apps):
        self.apps = apps
        self._production = ProductionFacet.__new__(ProductionFacet)
        self._production.hub = self
        self._production._switch_locks = {}

    def facet(self, name):
        return self._production if name == "production" else None

    def get(self, app_id):
        return next((app for app in self.apps if app.id == app_id), None)


def _facet(apps, defaults_path=None, monkeypatch=None):
    facet = StationDraftsFacet.__new__(StationDraftsFacet)
    facet.hub = FakeHub(apps)
    if defaults_path and monkeypatch:
        monkeypatch.setattr(station_drafts, "DATA", str(defaults_path))
    return facet


def _chain_apps(own_draft=None):
    sheet = {"accepts": "concept-image", "produces": ["character-sheet"], "skill": "s", "check": _check()}
    if own_draft:
        sheet["draft"] = own_draft
    return [
        _app("prospero", [{"accepts": "scene-brief", "produces": ["concept-image"], "skill": "p", "check": _check()}, sheet]),
        _app("vulcan", [{"accepts": "character-sheet", "produces": ["reference-mesh"], "skill": "v", "check": _check()}]),
    ]


def _defaults(tmp_path):
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps({"drafts": [
        {"app": "prospero", "accepts": "scene-brief", "how": {"es": "Pocos pasos", "en": "Few steps"}, "seconds": 70,
         "gpu": True, "anchors": ["composition"]},
        {"app": "prospero", "accepts": "concept-image", "how": "Frente y espalda", "seconds": 140, "gpu": True},
        {"app": "vulcan", "accepts": "nothing", "how": "x", "seconds": 0},          # invalid: no time
    ]}), encoding="utf-8")
    return path


def test_normalize_needs_how_and_positive_seconds():
    assert station_drafts.normalize({"how": "x", "seconds": 5})["how"] == {"es": "x", "en": "x"}
    assert station_drafts.normalize({"how": {"es": "a", "en": "b"}, "seconds": 1.5, "anchors": ["depth", 3]})["anchors"] == ["depth"]
    for bad in (None, "draft", {"how": "x"}, {"seconds": 3}, {"how": "x", "seconds": 0}, {"how": "x", "seconds": True},
                {"how": {"es": "solo"}, "seconds": 3}):
        assert station_drafts.normalize(bad) is None


def test_manifest_wins_hub_fills_in_and_missing_is_said(tmp_path, monkeypatch):
    own = {"how": {"es": "Solo frente", "en": "Front only"}, "seconds": 30, "gpu": True}
    facet = _facet(_chain_apps(own_draft=own), _defaults(tmp_path), monkeypatch)
    out = facet.drafts({})
    assert out["ok"]
    by = {(s["app"], h["accepts"]): h["draft"] for s in out["stations"] for h in s["handoffs"]}
    assert by[("prospero", "concept-image")]["declared_in"] == "manifest" and by[("prospero", "concept-image")]["seconds"] == 30
    assert by[("prospero", "scene-brief")]["declared_in"] == "hub" and by[("prospero", "scene-brief")]["anchors"] == ["composition"]
    assert by[("vulcan", "character-sheet")] is None
    assert out["summary"] == {"handoffs": 3, "with_draft": 2, "in_manifest": 1}


def test_a_chain_adds_its_draft_time_and_lists_steps_without_draft(tmp_path, monkeypatch):
    facet = _facet(_chain_apps(), _defaults(tmp_path), monkeypatch)
    out = facet.drafts({"from_type": "scene-brief", "to_type": "reference-mesh"})
    chain = out["chains"][0]
    assert [s["produces"] for s in chain["steps"]] == ["concept-image", "character-sheet", "reference-mesh"]
    assert chain["draft_seconds"] == 210.0 and chain["draft_gpu_seconds"] == 210.0
    assert chain["without_draft"] == ["vulcan: character-sheet → reference-mesh"]


def test_the_shipped_declarations_are_valid_and_name_real_handoffs():
    raw = json.loads(open(station_drafts.DATA, encoding="utf-8").read())["drafts"]
    loaded = station_drafts.load_defaults()
    assert len(loaded) == len(raw)
    assert all(d["how"]["es"] and d["how"]["en"] for d in loaded.values())
    assert ("vulcan", "character-sheet") in loaded and "depth" in loaded[("vulcan", "character-sheet")]["anchors"]


def test_the_tool_is_read_only_and_short():
    tool = StationDraftsFacet.tools()[0]
    assert tool["name"] == "hub_station_drafts" and tool["annotations"]["readOnlyHint"] is True
    assert len(tool["description"]) < 200
