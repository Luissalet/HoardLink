import hashlib

import pytest

from hoard_link.hub.agora import Agora, AgoraError, MIGRATIONS, TOOLS
from hoard_link.hub.order_dossier import MIGRATION
from hoard_link.sqlkit import Database


@pytest.fixture
def studio(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    yield ag
    ag.close()


def test_file_and_folder_receipts_reach_first_job_and_survive_updates(studio, tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_bytes(b"scope A")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "facts.txt").write_bytes(b"facts")
    (assets / "logo.svg").write_bytes(b"logo")
    order = studio.order_create({"agent": "writer", "title": "Taller Greda", "words": "Preparar la web.",
                                 "folder": str(tmp_path), "refs": [{"path": "brief.md"}, {"path": "assets"}],
                                 "keep": ["Cuatro piezas de taller"], "deliverables": ["Web"],
                                 "dossier": {"audience": "Clientes del taller", "constraints": ["Móvil y escritorio"]}})["order"]
    oid = order["id"]
    plan = studio.plan_set({"agent": "writer", "order_id": oid, "jobs": [{"title": "Diseño"}]})["plan"]
    first_job = lambda: studio.job({"task_id": plan[0]["task_id"]})["job"]["dossier"]
    dossier = studio.order({"order_id": oid})["order"]["dossier"]
    assert dossier == first_job()
    assert (dossier["request"], dossier["audience"], dossier["constraints"], dossier["keep"]) == (
        "Preparar la web.", "Clientes del taller", ["Móvil y escritorio"], ["Cuatro piezas de taller"],
    )
    source, directory = dossier["materials"]
    assert source["sha256"] == hashlib.sha256(b"scope A").hexdigest()
    assert source["resolved_path"] == str(brief)
    assert directory["kind"] == "folder" and directory["files"] == 2 and len(directory["sha256"]) == 64
    brief.write_bytes(b"scope B")
    change = studio.order_update({"agent": "writer", "order_id": oid, "dossier": {"audience": "Ferias"}})
    assert first_job()["materials"] == dossier["materials"]
    assert first_job()["audience"] == "Ferias"
    studio.order_undo({"agent": "writer", "token": change["undo"]})
    assert first_job() == dossier
    change = studio.order_update({"agent": "writer", "order_id": oid, "refs": order["refs"]})
    assert first_job()["materials"][0]["sha256"] == hashlib.sha256(b"scope B").hexdigest()
    studio.order_undo({"agent": "writer", "token": change["undo"]})
    assert first_job() == dossier


@pytest.mark.parametrize(("app", "tool", "payload"), [
    ("writer", "wh_production_dossier", {"schema": "hoard.production-dossier/1", "project": {"id": "novel"}}),
    ("drucker", "drucker_engagement_get", {"id": "engagement", "scope": [{"title": "Report"}]}),
    ("coase", "coase_backend_packet", {"case_id": "case", "sources": [{"id": "source"}]}),
])
def test_native_contract_passes_through_intact(studio, app, tool, payload):
    args = {"agent": "planner", "title": "Presentation", "words": "Prepare six slides",
            "refs": [{"app": app, "object": "source", "source_rev": "r3", "sha256": "a" * 64}],
            "dossier": {"native": {"app": app, "tool": tool, "object": "source", "source_rev": "r3",
                                   "payload": payload}}}
    order = studio.order_create(args)["order"]
    full = studio.order({"order_id": order["id"]})["order"]["dossier"]
    assert full["native"]["payload"] == payload and full["native"]["source_rev"] == "r3"
    assert full["native"]["app"] == app and full["native"]["tool"] == tool
    assert len(full["native"]["sha256"]) == 64 and full["materials"][0]["sha256"] == "a" * 64
    undone = studio.order_update({"agent": "planner", "order_id": order["id"], "dossier": {"native": None}})
    assert "native" not in studio.order({"order_id": order["id"]})["order"]["dossier"]
    studio.order_undo({"agent": "planner", "token": undone["undo"]})
    assert studio.order({"order_id": order["id"]})["order"]["dossier"] == full


def test_legacy_order_and_unavailable_material_can_be_read(studio):
    args = {"agent": "planner", "words": "Presentación", "refs": [{"path": "missing.txt"}, {"note": "idea"}]}
    oid = studio.order_create(args)["order"]["id"]
    read = lambda: studio.order({"order_id": oid})["order"]["dossier"]
    assert [r["state"] for r in read()["materials"]] == ["unavailable", "unrecorded"]
    studio.db.execute("UPDATE agora_orders SET dossier='{}' WHERE id=?", (oid,))
    assert read()["request"] == "Presentación" and read()["audience"] == ""
    assert [r["state"] for r in read()["materials"]] == ["unrecorded", "unrecorded"]
    with pytest.raises(AgoraError):
        studio.order_update({"agent": "planner", "order_id": oid, "refs": [{"url": "https://example.test", "sha256": "bad"}]})


def test_order_tools_accept_dossier_fields():
    schemas = {t["name"]: t["inputSchema"]["properties"] for t in TOOLS}
    for name in ("hub_agora_order_create", "hub_agora_order_update"):
        assert set(schemas[name]["dossier"]["properties"]) == {"native", "audience", "constraints"}
        assert "sha256" in schemas[name]["refs"]["items"]["properties"]


def test_an_order_named_after_its_document_has_a_request(studio, tmp_path):
    material = tmp_path / "requirements.txt"
    material.write_text("Review the requirements.", encoding="utf-8")
    order = studio.order_create({"agent": "planner", "title": "Review the attached requirements",
                                 "refs": [{"path": str(material)}]})["order"]
    assert order["dossier"]["request"] == order["title"]
    assert order["dossier"]["materials"][0]["state"] == "fingerprinted"


def test_dossier_migrates_an_existing_order(tmp_path):
    path = tmp_path / "agora.db"
    with Database(path, migrations=MIGRATIONS[:MIGRATIONS.index(MIGRATION)]) as db:
        oid = db.insert("agora_orders", {"space_id": "ecosystem", "title": "Older order", "words": "Existing request",
                                        "created_by": "planner", "created": 1, "updated": 1})
    studio = Agora(path)
    try:
        assert studio.order({"order_id": oid})["order"]["dossier"]["request"] == "Existing request"
        studio.order_update({"agent": "planner", "order_id": oid, "dossier": {"audience": "Workshop"}})
        assert studio.order({"order_id": oid})["order"]["dossier"]["audience"] == "Workshop"
    finally:
        studio.close()
