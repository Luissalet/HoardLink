from types import SimpleNamespace

from hoard_link.hub.agora import Agora
from hoard_link.hub.order_app_references import resolve
from hoard_link.hub.production import ProductionFacet


class AppTools:
    def __init__(self):
        self.projects = [{"id": "proj_demo", "title": "Demo"}]
        self.codex = {"proj_demo": [{"id": "codex_card", "title": "Odila Brenn", "type": "character"}]}
        self.chapters = {"proj_demo": [{"id": "writing_map", "title": "El mapa al revés", "chapter": 1}]}
        self.down = False
        self.calls = []

    def call_app(self, app, tool, arguments, *, timeout):
        assert app == "writer" and 0 < timeout <= 2
        self.calls.append((tool, arguments))
        if self.down:
            return {"ok": False, "status": None}
        if tool == "wh_list_projects":
            return {"ok": True, "result": {"projects": self.projects}}
        project = arguments["projectId"]
        if tool == "wh_list_codex":
            return {"ok": True, "result": {"entries": self.codex[project]}}
        assert tool == "wh_list_writings"
        return {"ok": True, "result": {"writings": self.chapters[project]}}


def test_writer_confirms_exact_sources_and_checks_the_chapter_in_the_same_project():
    app = AppTools()
    named = {"app": "writer", "object": "ODILA BRENN", "place": "ficha del personaje y capítulo 1"}
    refs = [{"path": "source.txt"}, named, named,
            {"app": "writer", "object": "hoard://writer/writing/writing_map", "place": "proj_demo"},
            {"app": "writer", "object": "capítulo 1", "place": "Demo"},
            {"app": "writer", "object": "Odila", "place": "character"},
            {"app": "writer", "object": "writing_map", "place": "párrafo 3"}]
    checks = resolve(app, refs)
    assert [r["index"] for r in checks] == list(range(1, 7))
    assert [r["status"] for r in checks] == ["resolved", "resolved", "resolved", "resolved", "missing", "unverified"]
    assert len(app.calls) == 3
    assert checks[4]["es"] == "Referencia no encontrada"
    assert checks[4]["en"] == "Reference not found"

    # A different project's chapter does not certify the character's reference.
    app.projects.append({"id": "proj_other", "title": "Other"})
    app.codex["proj_other"] = []
    app.chapters["proj_other"] = app.chapters["proj_demo"]
    app.chapters["proj_demo"] = []
    assert resolve(app, [named])[0]["status"] == "missing"
    app.chapters["proj_demo"] = [*app.chapters["proj_other"], {"id": "writing_other", "chapter": 1}]
    assert resolve(app, [named])[0]["status"] == "ambiguous"


def test_live_reference_checks_reach_create_plan_job_and_undo(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    app = AppTools()
    app.facet = lambda name: SimpleNamespace(agora=ag) if name == "agora" else None
    production = ProductionFacet(app)
    production.catalog = lambda *args, **kwargs: {"stations": []}
    production.station_repo = lambda station: ""
    production.start()

    ref = {"app": "writer", "object": "codex_card", "place": "proj_demo"}
    agent = "reference-worker"
    try:
        created = ag.order_create({"agent": agent, "title": "Scene", "refs": [ref]})
        oid = created["order"]["id"]
        assert created["order"]["app_references"][0]["status"] == "resolved"
        assert created["order"]["refs"] == [ref]
        plan = ag.plan_set({"agent": agent, "order_id": oid, "jobs": [{"key": "brief", "title": "Brief"}]})
        assert plan["app_references"][0]["status"] == "resolved"

        app.down = True
        job = ag.job({"task_id": plan["keys"]["brief"]})["job"]
        assert job["order"]["refs"] == [ref]
        assert job["app_references"][0]["status"] == "unavailable"
        app.down = False

        changed = ag.order_update({"agent": agent, "order_id": oid, "refs": [{**ref, "object": "codex_missing"}]})
        assert changed["order"]["app_references"][0]["status"] == "missing"
        ag.order_undo({"agent": agent, "token": changed["undo"]})
        assert ag.order({"order_id": oid})["order"]["app_references"][0]["status"] == "resolved"

        app.codex["proj_demo"].clear()
        reread = ag.order({"order_id": oid})["order"]
        assert reread["app_references"][0]["status"] == "missing"
        assert reread["refs"] == [ref] and len(reread["plan"]) == 1 and not reread["gates"]
    finally:
        ag.close()


def test_generic_search_verifies_an_exact_hit_without_guessing_about_absence():
    result = {"groups": [{"app": "catalog", "results": [{"id": "item-1", "title": "Source", "url": ""}]}], "skipped": []}
    hub = SimpleNamespace(get=lambda app: True, facet=lambda name: SimpleNamespace(search=lambda *a, **kw: result))
    refs = [{"app": "catalog", "object": "Source"}, {"app": "catalog", "object": "Another source"},
            {"app": "catalog", "object": "Source", "place": "Unmapped folder"}]
    assert [r["status"] for r in resolve(hub, refs)] == ["resolved", "unverified", "unverified"]
    result["groups"] = []
    result["skipped"] = [{"app": "catalog", "reason": "not running"}]
    assert resolve(hub, refs)[0]["status"] == "unavailable"
