import hashlib
from pathlib import Path

import pytest

from hoard_link.hub.agora import Agora, AgoraError, TOOLS
from hoard_link.hub.agora_orders import piece_file
from hoard_link.hub.order_destinations import declared_requirements


@pytest.fixture
def studio(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    yield ag
    ag.close()


def cartographer(studio, tmp_path):
    order = studio.order_create({"agent": "artist", "title": "Cartographer", "keep": ["Tubo a la izquierda"],
                                 "folder": str(tmp_path)})["order"]
    jobs = [
        {"key": "sheet", "title": "Hoja", "station": "prospero", "produces": ["character_sheet"]},
        {"key": "mesh", "title": "Volumen", "station": "vulcan", "produces": ["mesh"], "after": ["sheet"]},
        {"key": "game", "title": "Juego", "station": "godot", "produces": ["playable_scene"], "after": ["mesh"]},
        {"key": "print", "title": "Impresión", "station": "phidias", "produces": ["figure"], "after": ["mesh", "sheet"]},
        {"key": "other", "title": "Otro", "station": "vulcan", "produces": ["mesh"]},
    ]
    keys = studio.plan_set({"agent": "planner", "order_id": order["id"], "jobs": jobs})["keys"]
    return order["id"], keys


def test_requirements_reach_the_sheet_and_recompute_after_removal_and_undo(studio, tmp_path):
    oid, keys = cartographer(studio, tmp_path)
    sheet = lambda: studio.job({"task_id": keys["sheet"]})["job"]["requirements"]
    rules = sheet()
    assert {rule["id"] for rule in rules} == {
        "orthographic", "neutral-background", "separable-parts", "print-dimensions", "print-wall", "game-interaction",
    }
    assert len(rules) == 6  # the shared route to the print job contributes only once
    assert {rule["from"]["task_id"] for rule in rules} == {keys["mesh"], keys["game"], keys["print"]}
    assert {r["id"] for r in studio.job({"task_id": keys["other"]})["job"]["requirements"]} == {
        "orthographic", "neutral-background", "separable-parts",
    }
    applied = studio.plan_set({"agent": "planner", "order_id": oid, "remove": [keys["print"]]})
    assert {rule["from"]["task_id"] for rule in sheet()} == {keys["mesh"], keys["game"]}
    studio.order_undo({"agent": "planner", "token": applied["undo"]})
    assert sheet() == rules


def test_custom_requirement_from_catalogue_follows_the_order_graph(studio, tmp_path):
    oid, keys = cartographer(studio, tmp_path)
    custom = {"id": "contrast", "es": "Especifica el contraste.", "en": "Specify contrast."}
    handoff = {"accepts": "editable-report", "produces": ["slides"], "skill": "slides", "requires": [custom]}
    studio.order_station_catalog = lambda: [{"app": "presenter", "handoffs": [handoff]}]
    plan = studio.plan_set({"agent": "planner", "order_id": oid, "jobs": [
        {"key": "slides", "title": "Diapositivas", "station": "presenter", "skill": "slides",
         "produces": ["slides"], "after": [keys["sheet"]]},
    ]})
    assert declared_requirements("presenter", handoff) == [custom]
    sheet = next(j for j in plan["plan"] if j["task_id"] == keys["sheet"])
    assert {r["id"]: r["from"]["task_id"] for r in sheet["requirements"]}["contrast"] == plan["keys"]["slides"]


def test_anchor_uses_exact_preview_and_reaches_the_review(studio, tmp_path):
    oid, keys = cartographer(studio, tmp_path)
    path = tmp_path / "sheet.png"
    preview = tmp_path / "side.png"
    path.write_bytes(b"sheet A")
    preview.write_bytes(b"side A")
    sheet = studio.piece_add({"agent": "artist", "order_id": oid, "task_id": keys["sheet"],
                              "kind": "character_sheet", "title": "Sheet", "path": path.name,
                              "previews": [preview.name]})["piece"]
    selector = {"piece": sheet["id"], "preview": 0, "view": "perfil", "region": [.6, .3, .2, .4]}
    checked = studio.piece_check({"agent": "artist", "piece_id": sheet["id"], "note": "Tubo visible",
                                  "keep": [{"id": "k1", "anchor": selector}]})
    anchor = checked["piece"]["keep"][0]["anchor"]
    assert anchor == checked["piece"]["checks"][0]["keep"][0]["anchor"]
    assert anchor["sha256"] == hashlib.sha256(b"side A").hexdigest()
    assert anchor["region"] == selector["region"] and anchor["revision"] == 1
    preview.write_bytes(b"side B")
    kept = Path(piece_file(studio, sheet["id"], preview=0))
    assert kept.read_bytes() == b"side A"
    kept.unlink()
    with pytest.raises(AgoraError) as changed:
        piece_file(studio, sheet["id"], preview=0)
    assert changed.value.status == 409
    assert studio.job({"task_id": keys["mesh"]})["job"]["anchored_details"][0]["anchor"] == anchor
    mesh = studio.piece_add({"agent": "artist", "order_id": oid, "task_id": keys["mesh"],
                             "kind": "mesh", "title": "Mesh", "path": "mesh.glb", "derives_from": [sheet["id"]]})["piece"]
    assert mesh["keep"][0]["anchor"] == anchor and mesh["keep"][0]["state"] == "pending"
    studio.order_undo({"agent": "artist", "token": checked["undo"]})
    assert not studio.job({"task_id": keys["mesh"]})["job"]["anchored_details"]


@pytest.mark.parametrize("invalid", [{"region": [.9, 0, .2, 1]}, {"palette": ["red"]},
                                      {"measure": {"value": 12, "unit": ""}}, {"sha256": "0" * 64}])
def test_invalid_anchor_does_not_mark_the_piece(studio, tmp_path, invalid):
    oid, _ = cartographer(studio, tmp_path)
    (tmp_path / "sheet.png").write_bytes(b"sheet")
    piece = studio.piece_add({"agent": "artist", "order_id": oid, "kind": "image", "title": "Sheet",
                              "path": "sheet.png"})["piece"]
    with pytest.raises(AgoraError):
        studio.piece_check({"agent": "artist", "piece_id": piece["id"], "note": "Tubo visible",
                            "keep": [{"id": "k1", "anchor": {"piece": piece["id"], **invalid}}]})
    assert studio.order({"order_id": oid})["order"]["piece_list"][0]["checks"] == []


def test_anchor_is_exposed_by_normal_tools_without_a_gate_decision_tool():
    tools = {tool["name"]: tool for tool in TOOLS}
    marks = tools["hub_agora_piece_check"]["inputSchema"]["properties"]["keep"]["items"]["properties"]
    assert marks["anchor"]["required"] == ["piece"]
    assert "hub_agora_gate_decide" not in tools
