"""Focal checks without inference, app servers or public websites."""

import sys
from types import SimpleNamespace

import pytest

from hoard_link.hub import piece_checks as checks
from hoard_link.hub import piece_checks_layout as layout
from hoard_link.hub import piece_checks_media as media
from hoard_link.hub import piece_checks_mesh as mesh


def _checks(result):
    return {row["id"]: row for row in result["checks"]}


def test_citations_distinguish_missing_target_and_no_citations(tmp_path):
    report = tmp_path / "report.md"
    (tmp_path / "data.txt").write_text("source")
    report.write_text("Figures [A1–A3].\n- **A1.** [Data](data.txt)\n"
                      "- **A2.** [Missing](missing.txt)\n", encoding="utf-8")
    result = checks.run_checks(report, "document", {})
    assert result["verdict"] == "fail"
    cited = {row["id"]: row for row in result["measures"]["citations"] if "id" in row}
    assert cited["A1"]["resolved"] is True
    assert cited["A2"]["resolved"] is False
    assert cited["A3"]["target"] is None
    report.write_text("No citations")
    assert checks.run_checks(report, "document", {})["verdict"] == "unmeasured"


def test_image_dimensions_alpha_and_declared_views(tmp_path):
    image = pytest.importorskip("PIL.Image")
    path = tmp_path / "sheet.png"
    sheet = image.new("RGBA", (128, 64), (240, 240, 240, 255))
    sheet.paste((10, 10, 10, 255), (25, 15, 45, 45))
    sheet.save(path)
    result = checks.run_checks(path, "image", {"size_px": [128, 64], "alpha": True,
                               "neutral_background": True, "views": 2,
                               "view_boxes": [[0, .1, .5, .9], [.5, .1, 1, .9]]})
    by_id = _checks(result)
    assert by_id["resolution"]["verdict"] == by_id["alpha"]["verdict"] == "pass"
    assert by_id["neutral-background"]["verdict"] == "pass"
    assert by_id["views"]["verdict"] == "fail"
    assert by_id["views"]["measured"] == 1


def test_video_checks_declared_cuts_and_preserves_sample_time(tmp_path, monkeypatch):
    image = pytest.importorskip("PIL.Image")
    pytest.importorskip("numpy")
    blue = image.new("RGB", (64, 64), "blue")
    red = image.new("RGB", (64, 64), "red")
    blue_path, red_path = tmp_path / "blue.png", tmp_path / "red.png"
    blue.save(blue_path)
    red.save(red_path)
    monkeypatch.setattr(media, "_video_info", lambda path: ([64, 64], 2.0, 24.0))
    monkeypatch.setattr(media, "_frame", lambda path, seconds: red if seconds >= 1 else blue)
    requirements = {"duration_s": 2, "size_px": [64, 64], "interval_s": .5, "min_similarity": .9}
    first, second = [{"path": str(blue_path)}], [{"path": str(red_path)}]
    result = checks.run_checks(tmp_path / "clip.mp4", "video", {**requirements, "references": first})
    assert result["verdict"] == "fail"
    assert _checks(result)["visual-continuity"]["details"][0]["time_s"] == 1
    shots = [{"id": "blue", "start_s": 0, "end_s": 1, "references": first},
             {"id": "red", "start_s": 1, "end_s": 2, "references": second}]
    assert checks.run_checks(tmp_path / "clip.mp4", "video", {**requirements, "shots": shots})["verdict"] == "pass"
    shots[1]["start_s"] = 1.25
    assert checks.run_checks(tmp_path / "clip.mp4", "video", {**requirements, "shots": shots})["verdict"] == "unmeasured"


def test_print_reuses_vulcan_measurements_without_guessing_walls(tmp_path, monkeypatch):
    class NativeMesh:
        faces = [1] * 4
        extents = [.1, .2, .3]
        is_winding_consistent = True

    native = SimpleNamespace(UNIT_TO_MM={"m": 1000}, load_geometry=lambda path: ([], NativeMesh(), "m", None),
                             _edge_counts=lambda faces: (0, 2),
                             _wall_records=lambda *args: ({}, {}, {"status": "open"}), sha256_of=lambda path: "sha")
    monkeypatch.setitem(sys.modules, "vulcan.asset_inspection", native)
    result = checks.run_checks(tmp_path / "model.3mf", "print-mesh",
                               {"build_volume_mm": [100, 200, 299], "min_wall_mm": 1})
    by_id = _checks(result)
    assert result["verdict"] == "fail"
    assert by_id["nonmanifold-edges"]["measured"] == 2
    assert by_id["build-volume"]["verdict"] == "fail"
    assert by_id["wall-thickness"]["verdict"] == "unmeasured"


def test_skin_reads_all_weight_sets_and_flags_bad_joint(tmp_path):
    np = pytest.importorskip("numpy")
    arrays = [np.array(row, dtype="uint8") for row in (
        [[0, 0, 0, 0], [1, 0, 0, 0], [0, 0, 0, 0]], [[0] * 4, [99, 0, 0, 0], [0] * 4],
        [[1, 0, 0, 0], [0] * 4, [0] * 4], [[0] * 4, [0] * 4, [0] * 4])]
    doc = {"buffers": [{}], "bufferViews": [{"buffer": 0, "byteOffset": i * 12} for i in range(4)],
           "accessors": [{"type": "VEC4", "count": 3, "componentType": 5121, "bufferView": i}
                         for i in range(4)],
           "nodes": [{"mesh": 0, "skin": 0, "name": "mesh"}], "skins": [{"joints": [0]}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "WEIGHTS_0": 0, "JOINTS_0": 1,
                                                     "WEIGHTS_1": 2, "JOINTS_1": 3}}]}]}
    assert mesh._weights(doc, b"".join(array.tobytes() for array in arrays), tmp_path) == [
        {"node": "mesh", "primitive": 0, "vertices": 3, "unweighted": 1, "invalid_weights": 1}]


def test_slide_diagnostics_name_the_affected_shape(tmp_path, monkeypatch):
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(deck.slide_width, 10, 10000, 10000)
    shape.text = "Lost text"
    path = tmp_path / "example.pptx"
    deck.save(path)
    monkeypatch.setattr(layout, "_pdf_words", lambda path: [layout._words("")])
    result = checks.run_checks(path, "slides", {"slide_count": 1})
    by_id = _checks(result)
    assert by_id["accessibility"]["details"] == [{"slide": 1, "rule": "slide-title-placeholder"}]
    assert by_id["canvas-overflow"]["details"][0]["shape"] == shape.name
    assert by_id["rendered-text"]["details"][0]["missing_words"] == {"lost": 1, "text": 1}


def test_checked_look_calls_eyes_after_checking_and_keeps_failure(tmp_path):
    path = tmp_path / "unresolved.md"
    path.write_text("Source [A1].")
    received = []
    hub = SimpleNamespace(facet=lambda name: SimpleNamespace(look=lambda args: received.append(args) or {
        "ok": True, "undo": {"tool": "hub_eyes_forget"}}))
    facet = checks.PieceChecksFacet(hub)
    result = facet.look({"path": str(path), "visual": {"describe": True}, "language": "en"})
    assert result["technical_check"]["verdict"] == "fail"
    assert "found a defect" in received[0]["label"]
    assert received[0]["path"] == str(path)
    assert result["undo"]["tool"] == "hub_eyes_forget"
    assert checks.descriptor("character-sheet") == {"tool": "hub_piece_validate", "profile": "image"}


def test_gate_advisories_use_the_matching_revision_and_preserve_checked_notes():
    received = []
    db = SimpleNamespace(one=lambda query, args: received.append(args) or {"note": "Recorded finding"})
    agora = SimpleNamespace(db=db)
    piece = {"id": 12, "revision": 3, "sha256": "hash", "kind": "video-clip", "title": "Video"}
    assert checks.gate_advisories(agora, [piece], "") == [
        {"piece_id": 12, "title": "Video", "profile": "video", "note": "Recorded finding"}]
    assert received == [(12, "Technical check · #12 · r3 · hash · %", "Comprobación automática · #12 · r3 · hash · %")]
    assert not checks.gate_advisories(agora, [piece], "Technical check · #12 · r3 · hash · video")
