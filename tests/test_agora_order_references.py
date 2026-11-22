from pathlib import Path

import pytest
from PIL import Image

from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.config import PERSON_ALIAS


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


def gate(studio, order, *pieces):
    return studio.gate_open({"agent": "producer", "order_id": order["id"], "piece_ids": [p["id"] for p in pieces]})


def keep(studio, order, piece):
    studio.piece_check({"agent": "station", "piece_id": piece["id"], "note": "Viewed the reference"})
    opened = gate(studio, order, piece)
    studio.gate_decide({"agent": PERSON_ALIAS, "_person": True, "gate_id": opened["gate"]["id"],
                        "decision": "keep", "note": "Use this restaging"})


def compare(studio, piece, reference, *, state="ok", preview=None):
    comparisons = {"reference_piece": reference["id"], "observed": "Opened each view and inspected the detail."}
    if preview is not None:
        comparisons["piece_preview"] = preview
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


def test_gate_requires_real_lineage_and_recorded_comparisons_per_revision(studio, tmp_path):
    source = make_order(studio, tmp_path)
    sheet = make_piece(studio, source, "sheet")
    keep(studio, source, sheet)
    order = make_order(studio, tmp_path, refs=[{"order_id": source["id"]}], keep=DETAILS)
    omitted = make_piece(studio, order, "omitted")
    assert omitted["missing_required_references"] == [sheet["id"]]
    note_only(studio, omitted)
    with pytest.raises(AgoraError) as absent:
        gate(studio, order, omitted)
    assert absent.value.extra["comparisons"][0]["reason"] == "missing_reference"

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
