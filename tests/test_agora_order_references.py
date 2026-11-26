from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from hoard_link.hub import agent_routes
from hoard_link.hub.agora import Agora, AgoraError, AgoraFacet
from hoard_link.hub.config import HubConfig, PERSON_ALIAS


DETAILS = ["Same face and braid", "Short grey cape", "Brass tube"]


@pytest.fixture
def studio(tmp_path):
    instance = Agora(tmp_path / "agora.db")
    instance.space_create({"agent": "producer", "id": "estudio", "title": "Estudio"})
    yield instance.for_space("estudio")
    instance.close()


def make_order(studio, folder, **kwargs):
    return studio.order_create({"agent": "producer", "title": "Cartographer", "words": "Turn the map",
                                "folder": str(folder), **kwargs})["order"]


def make_piece(studio, order, name, **kwargs):
    path = Path(order["folder"]) / f"{name}.png"
    Image.new("RGB", (8, 8), color=kwargs.pop("color", "grey")).save(path)
    return studio.piece_add({"agent": "station", "order_id": order["id"], "title": name, "kind": "image",
                             "path": str(path), **kwargs})["piece"]


def gate(studio, order, *pieces, **kwargs):
    return studio.gate_open({"agent": "producer", "order_id": order["id"], "piece_ids": [p["id"] for p in pieces],
                             **kwargs})


def keep(studio, order, piece):
    studio.piece_check({"agent": "station", "piece_id": piece["id"], "note": "Viewed the reference"})
    opened = gate(studio, order, piece)
    studio.gate_decide({"agent": PERSON_ALIAS, "_person": True, "gate_id": opened["gate"]["id"],
                        "decision": "keep", "note": "Use this restaging"})


def compare(studio, piece, reference, *, state="ok", preview=None, reference_preview=None):
    comparisons = {"reference_piece": reference["id"], "observed": "Opened each view and inspected the detail."}
    if preview is not None:
        comparisons["piece_preview"] = preview
    if reference_preview is not None:
        comparisons["reference_preview"] = reference_preview
    return studio.piece_check({"agent": "reviewer", "piece_id": piece["id"], "note": "Compared the retained views",
                               "ok": state == "ok", "keep": [{"detail": detail, "state": state,
                                                               "comparisons": [comparisons]} for detail in DETAILS]})


def note_only(studio, piece):
    return studio.piece_check({"agent": "station", "piece_id": piece["id"], "note": "General inspection", "ok": True,
                               "keep": [{"detail": detail, "state": "ok"} for detail in DETAILS]})


@pytest.mark.parametrize("citation", ["id", "path", "note", "url"])
def test_job_and_draft_receive_kept_revisions_without_a_draft(studio, tmp_path, citation):
    source = make_order(studio, tmp_path)
    sheet = make_piece(studio, source, "sheet")
    keep(studio, source, sheet)
    make_piece(studio, source, "unused")
    other = make_order(studio, tmp_path)
    keep(studio, other, make_piece(studio, other, "other"))
    refs = {"id": {"order_id": source["id"]}, "path": {"path": sheet["path"]},
            "note": {"note": f"Hoja de la orden #{source['id']}"},
            "url": {"url": f"/api/agora/order?order_id={source['id']}"}}
    order = make_order(studio, tmp_path, refs=[refs[citation]])
    local = make_piece(studio, order, "local")
    keep(studio, order, local)
    plan = studio.plan_set({"agent": "producer", "order_id": order["id"], "jobs": [
        {"key": "key", "station": "prospero", "title": "Image"},
        {"key": "clip", "station": "prospero", "title": "Video"},
        {"key": "volume", "station": "vulcan", "title": "Volume"},
    ]})
    draft = studio.draft_anchors({"order_id": order["id"]})
    for task_id in plan["keys"].values():
        packet = studio.job({"task_id": task_id})["job"]
        assert packet["required_references"] == draft["required_references"]
        assert [ref["id"] for ref in packet["references"]] == [local["id"]]
    assert draft["variant"] is None
    assert draft["derives_from"] == [{"piece": p["id"], "role": "reference"} for p in (sheet, local)]
    anchor = draft["required_references"][0]
    assert anchor["path"] == sheet["stored"] and anchor["sha256"] == sheet["sha256"]
    assert anchor["kept_note"] == "Use this restaging"


def test_gate_requires_recorded_comparisons_per_revision(studio, tmp_path):
    source = make_order(studio, tmp_path)
    sheet = make_piece(studio, source, "sheet")
    keep(studio, source, sheet)
    order = make_order(studio, tmp_path, refs=[{"order_id": source["id"]}], keep=DETAILS)
    links = [{"piece": sheet["id"], "role": "reference"}]
    wrong = make_piece(studio, order, "wrong", color="orange", derives_from=links)
    note_only(studio, wrong)
    with pytest.raises(AgoraError) as pending:
        gate(studio, order, wrong)
    assert pending.value.extra["comparisons"][0]["reason"] == "comparison_pending"
    compare(studio, wrong, sheet, state="missing")
    note_only(studio, wrong)
    with pytest.raises(AgoraError) as mismatch:
        gate(studio, order, wrong)
    assert mismatch.value.extra["comparisons"][0]["reason"] == "comparison_failed"

    right = make_piece(studio, order, "right", derives_from=[wrong["id"]])
    assert right["missing_required_references"] == []
    note_only(studio, right)
    reviewed = compare(studio, right, sheet)
    evidence = reviewed["piece"]["checks"][-1]["keep"][0]["comparisons"][0]
    assert evidence["result"]["sha256"] == right["sha256"]
    assert evidence["reference"]["file_url"] == f"/api/agora/piece_file/{sheet['id']}"
    opened = gate(studio, order, right)
    with pytest.raises(AgoraError) as rejected:
        studio.gate_decide({"agent": "reviewer", "gate_id": opened["gate"]["id"], "decision": "keep"})
    assert rejected.value.status == 403
    studio.order_undo({"agent": "producer", "token": opened["undo"]})
    studio.order_undo({"agent": "reviewer", "token": reviewed["undo"]})
    with pytest.raises(AgoraError, match="passing visual comparison"):
        gate(studio, order, right)


def test_gate_accepts_comparison_with_a_kept_reference_outside_lineage(studio, tmp_path):
    source = make_order(studio, tmp_path)
    sheet = make_piece(studio, source, "sheet")
    keep(studio, source, sheet)
    (tmp_path / "model.obj").write_text("v 0 0 0\n", encoding="utf-8")
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "model-view.png")
    model = studio.piece_add({"agent": "station", "order_id": source["id"], "title": "Model", "kind": "mesh",
                              "path": "model.obj", "previews": ["model-view.png"]})["piece"]
    keep(studio, source, model)

    order = make_order(studio, tmp_path, refs=[{"order_id": source["id"]}], keep=DETAILS)
    previous = make_piece(studio, order, "previous", derives_from=[sheet["id"]])
    candidate = make_piece(studio, order, "candidate", derives_from=[previous["id"]])
    assert candidate["missing_required_references"] == [model["id"]]
    compare(studio, candidate, sheet)
    with pytest.raises(AgoraError) as pending:
        gate(studio, order, candidate)
    assert pending.value.status == 409
    assert pending.value.extra["error_code"] == "kept_reference_comparison"
    assert pending.value.extra["comparisons"] == [
        {"piece": candidate["id"], "reference_piece": model["id"], "reason": "comparison_pending"},
    ]
    for lang in ("es", "en"):
        assert "hub_agora_piece_check" in pending.value.extra[lang]
        assert "keep[].comparisons" in pending.value.extra[lang]

    reviewed = compare(studio, candidate, model, reference_preview=0)
    check = reviewed["piece"]["checks"][-1]
    assert check["agent"] == "reviewer"
    evidence = check["keep"][0]["comparisons"][0]
    assert evidence["observed"] == "Opened each view and inspected the detail."
    assert evidence["result"]["revision"] == candidate["revision"]
    assert evidence["reference"] == {
        "piece": model["id"], "revision": model["revision"], "preview": 0,
        "sha256": Path(model["previews_stored"][0]).stem,
        "file_url": f"/api/agora/piece_file/{model['id']}?preview=0",
    }
    assert gate(studio, order, candidate)["gate"]["status"] == "open"
    pieces = studio.order({"order_id": order["id"]})["order"]["piece_list"]
    assert {piece["id"]: piece["derivation"] for piece in pieces} == {
        piece["id"]: piece["derivation"] for piece in (previous, candidate)
    }


def test_video_check_uses_a_registered_preview(studio, tmp_path):
    source = make_order(studio, tmp_path)
    sheet = make_piece(studio, source, "sheet")
    keep(studio, source, sheet)
    order = make_order(studio, tmp_path, refs=[{"order_id": source["id"]}], keep=DETAILS)
    (tmp_path / "clip.mp4").write_bytes(b"the clip")
    Image.new("RGB", (8, 8)).save(tmp_path / "frame.png")
    clip = studio.piece_add({"agent": "station", "order_id": order["id"], "title": "Clip", "kind": "video",
                             "path": "clip.mp4", "previews": ["frame.png"], "derives_from": [sheet["id"]]})["piece"]
    with pytest.raises(AgoraError, match="retained image"):
        compare(studio, clip, sheet)
    assert not studio.order({"order_id": order["id"]})["order"]["piece_list"][0]["checks"]
    found = compare(studio, clip, sheet, preview=0)["piece"]["checks"][0]["keep"][0]["comparisons"][0]
    assert found["result"]["file_url"].endswith("?preview=0")
    assert gate(studio, order, clip)["gate"]["status"] == "open"


DEFECT_NOTE = "The cape collar covers the mouth and chin when the head tilts down"


@pytest.fixture
def references_with_defect(studio, tmp_path):
    source = make_order(studio, tmp_path)
    sheet = make_piece(studio, source, "sheet")
    (tmp_path / "model.obj").write_text("v 0 0 0\n", encoding="utf-8")
    Image.new("RGB", (8, 8), "navy").save(tmp_path / "downward-pose.png")
    model = studio.piece_add({"agent": "station", "order_id": source["id"], "title": "Model", "kind": "mesh",
                              "path": "model.obj", "previews": ["downward-pose.png"]})["piece"]
    keep(studio, source, sheet)
    keep(studio, source, model)
    defect = studio.piece_defect({"agent": "reviewer", "piece_id": model["id"], "note": DEFECT_NOTE,
                                 "view": "downward-pose", "pose": "head tilted down"})
    order = make_order(studio, tmp_path, refs=[{"order_id": source["id"]}], keep=[DETAILS[0]])
    piece = make_piece(studio, order, "candidate", derives_from=[{"piece": sheet["id"], "role": "reference"}])
    return source, sheet, model, defect["defect_id"], order, piece


def compare_detail(studio, piece, reference, *, state="ok", defect_id=None, **kwargs):
    comparison = {"reference_piece": reference["id"], "observed": "Opened both retained views and compared the collar."}
    if reference["previews"]:
        comparison["reference_preview"] = 0
    if defect_id is not None:
        comparison["reference_defect_id"] = defect_id
    return studio.piece_check({"agent": "reviewer", "piece_id": piece["id"], "note": "Compared the retained views",
                               "keep": [{"id": "k1", "state": state, "comparisons": [comparison]}], **kwargs})


def test_open_reference_defect_requires_title_and_does_not_replace_identity_reference(studio, references_with_defect):
    _, sheet, model, defect_id, order, piece = references_with_defect
    checked = compare_detail(studio, piece, model, state="reference_defect", defect_id=defect_id)
    assert checked["piece"]["status"] == "checked"
    assert checked["piece"]["keep"][0]["state"] == "reference_defect"
    pair = checked["piece"]["checks"][-1]["keep"][0]["comparisons"][0]
    assert (pair["reference"]["piece"], pair["reference_defect_id"]) == (model["id"], defect_id)
    assert pair["reference"]["preview"] == 0
    with pytest.raises(AgoraError) as pending:
        gate(studio, order, piece)
    assert pending.value.extra["comparisons"] == [
        {"piece": piece["id"], "reference_piece": sheet["id"], "reason": "comparison_pending"},
    ]

    compare_detail(studio, piece, sheet)
    with pytest.raises(AgoraError) as hidden:
        gate(studio, order, piece, note=DEFECT_NOTE)
    assert hidden.value.status == 409
    assert hidden.value.extra["code"] == "piece_undisclosed_reference_defect"
    assert hidden.value.extra["defect_id"] == defect_id

    title = f"{piece['title']} – {DEFECT_NOTE}"
    disclosed = compare_detail(studio, piece, model, state="reference_defect", defect_id=defect_id, title=title)
    revised = disclosed["piece"]
    assert revised["title"] == title
    for field in ("id", "revision", "sha256", "derivation", "note"):
        assert revised[field] == piece[field]
    opened = gate(studio, order, piece, note=DEFECT_NOTE)
    assert opened["gate"]["status"] == "open"
    studio.order_undo({"agent": "producer", "token": opened["undo"]})
    studio.order_undo({"agent": "reviewer", "token": disclosed["undo"]})
    with pytest.raises(AgoraError) as undisclosed:
        gate(studio, order, piece)
    assert undisclosed.value.extra["code"] == "piece_undisclosed_reference_defect"


@pytest.mark.parametrize(("case", "code", "message"), [
    ("unlinked", "reference_defect_required", "Link the open defect"),
    ("missing", "reference_defect_missing", "does not exist"),
    ("closed", "reference_defect_closed", "is closed"),
    ("wrong_piece", "reference_defect_wrong_piece", "does not belong"),
])
def test_reference_defect_rejects_invalid_links(studio, references_with_defect, case, code, message):
    _, sheet, model, defect_id, order, piece = references_with_defect
    if case == "unlinked":
        defect_id = None
    elif case == "missing":
        defect_id += 100
    elif case == "closed":
        studio.defect_close({"agent": "reviewer", "defect_id": defect_id, "note": "Checked the repair"})
    elif case == "wrong_piece":
        model = sheet
    with pytest.raises(AgoraError, match=message) as rejected:
        compare_detail(studio, piece, model, state="reference_defect", defect_id=defect_id)
    assert rejected.value.status == 409
    assert rejected.value.extra["error_code"] == code
    found = studio.order({"order_id": order["id"]})["order"]["piece_list"][0]
    assert found["keep"] == piece["keep"]
    assert not found["checks"]


def test_agent_route_returns_conflicts_and_persists_the_defect_link(studio, references_with_defect, tmp_path):
    _, sheet, model, defect_id, order, piece = references_with_defect
    compare_detail(studio, piece, sheet)
    config = HubConfig(data_dir=str(tmp_path), roots=[], agent_watch={"enabled": False})
    hub = SimpleNamespace(config=config, facets=[])
    facet = AgoraFacet(hub)
    hub.facets.append(facet)

    def call(name, **arguments):
        body, status = agent_routes.invoke(hub, f"hub_agora_{name}", {
            "agent": "reviewer", "space_id": "estudio", **arguments,
        })
        return status, body["result"]

    try:
        comparison = {"reference_piece": model["id"], "reference_preview": 0,
                      "observed": "The collar only covers the chin in the reference preview.",
                      "reference_defect_id": defect_id + 100}
        args = {"piece_id": piece["id"], "note": "Compared the views",
                "keep": [{"id": "k1", "state": "reference_defect", "comparisons": [comparison]}]}
        status, result = call("piece_check", **args)
        assert (status, result["error_code"]) == (409, "reference_defect_missing")
        comparison["reference_defect_id"] = defect_id
        status, result = call("piece_check", **args)
        assert status == 200 and result["undo"]
        gate_args = {"order_id": order["id"], "piece_ids": [piece["id"]]}
        status, result = call("gate_open", **gate_args)
        assert (status, result["code"]) == (409, "piece_undisclosed_reference_defect")
        status, result = call("piece_check", **args, title=f"Candidate – reference defect #{defect_id}")
        assert status == 200 and result["piece"]["id"] == piece["id"]
        assert call("gate_open", **gate_args)[0] == 200
    finally:
        facet.close()


@pytest.mark.parametrize("action", ["close", "replace"])
def test_reference_defect_expires_and_requires_a_fresh_comparison(studio, references_with_defect, action):
    source, sheet, model, defect_id, order, piece = references_with_defect
    compare_detail(studio, piece, sheet)
    compare_detail(studio, piece, model, state="reference_defect", defect_id=defect_id,
                   title=f"Candidate – defecto de referencia #{defect_id}")
    opened = gate(studio, order, piece)

    if action == "close":
        studio.defect_close({"agent": "reviewer", "defect_id": defect_id, "fixed_in": model["id"],
                             "note": "Checked the repair"})
        cause = "reference_defect_closed"
    else:
        replacement = make_piece(studio, source, "replacement", supersedes=model["id"])
        keep(studio, source, replacement)
        cause = "reference_replaced"
    with pytest.raises(AgoraError) as stale_gate:
        studio.gate_decide({"agent": PERSON_ALIAS, "_person": True, "gate_id": opened["gate"]["id"], "decision": "keep"})
    assert stale_gate.value.status == 409 and stale_gate.value.extra["error_code"] == "kept_reference_comparison"
    with pytest.raises(AgoraError) as expired:
        gate(studio, order, piece)
    assert expired.value.status == 409
    assert expired.value.extra["error_code"] == "kept_reference_comparison"
    outstanding = {"piece": piece["id"], "reference_piece": model["id"], "defect_id": defect_id,
                   "reason": "comparison_pending", "cause": cause}
    if action == "replace":
        outstanding["replacement_piece"] = replacement["id"]
        with pytest.raises(AgoraError, match="was replaced") as old:
            compare_detail(studio, piece, model, state="reference_defect", defect_id=defect_id)
        assert old.value.status == 409
        checked = compare_detail(studio, piece, replacement, title="Candidate")
    else:
        checked = compare_detail(studio, piece, model, title="Candidate")
    assert outstanding in expired.value.extra["comparisons"]
    assert checked["piece"]["title"] == "Candidate"
    assert gate(studio, order, piece)["gate"]["status"] == "open"


def test_job_comparison_uses_registered_revisions_and_views(studio, tmp_path):
    order = make_order(studio, tmp_path)
    view = tmp_path / "detail.png"
    Image.new("RGB", (8, 8), "blue").save(view)
    old = make_piece(studio, order, "candidate", color="red", previews=[str(view)])
    Image.new("RGB", (8, 8), "green").save(view)
    current = make_piece(studio, order, "candidate", color="white", previews=[str(view)])
    first = make_piece(studio, order, "first", color="black")
    plan = studio.plan_set({"agent": "producer", "order_id": order["id"], "jobs": [
        {"key": "check", "title": "Check changes", "inputs": [current["id"], first["id"]]},
    ]})
    task = {"task_id": plan["keys"]["check"]}
    compared, standalone = studio.job(task)["job"]["revision_comparisons"]
    assert compared["status"] == "available"
    for side, piece, file_color, view_color in (
        ("previous", old, (255, 0, 0), (0, 0, 255)),
        ("current", current, (255, 255, 255), (0, 128, 0)),
    ):
        receipt = compared[side]
        assert (receipt["piece_id"], receipt["revision"]) == (piece["id"], piece["revision"])
        assert receipt["path"] == piece["stored"]
        assert receipt["previews"] == [{"preview": 0, "path": piece["previews_stored"][0]}]
        with Image.open(receipt["path"]) as image:
            assert image.getpixel((0, 0)) == file_color
        with Image.open(receipt["previews"][0]["path"]) as image:
            assert image.getpixel((0, 0)) == view_color
    assert standalone["current"]["piece_id"] == first["id"]
    assert standalone["previous"] is None and standalone["status"] == "missing"
    assert standalone["label"]["es"] == "sin referencia anterior"

    # Neither the current original nor its latest detail can stand in for the lost predecessor.
    Path(old["stored"]).unlink()
    Path(old["previews_stored"][0]).unlink()
    compared = studio.job(task)["job"]["revision_comparisons"][0]
    assert compared["status"] == "unavailable"
    assert compared["previous"] == {"piece_id": old["id"], "revision": 1, "path": None,
                                    "previews": [{"preview": 0, "path": None}]}
    assert compared["label"]["en"] == "no previous reference"
