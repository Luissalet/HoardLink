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


def _narrative_pdf(path, content, *, embed=True, blank_page=False):
    fitz = pytest.importorskip("pymupdf")
    with fitz.open() as doc:
        page = doc.new_page()
        font = "helv"
        if embed:
            font = "test-font"
            page.insert_font(fontname=font, fontbuffer=fitz.Font("helv").buffer)
        page.insert_text((60, 60), content, fontname=font)
        if blank_page:
            doc.new_page()
        doc.save(path)


def test_narrative_distinguishes_layout_defects_and_ordered_text(tmp_path):
    path = tmp_path / "story.pdf"
    source = tmp_path / "story.txt"
    source.write_text("Inés volvió. Mauro salió.", encoding="utf-8")
    _narrative_pdf(path, "Mauro salió.\nInés volvió.", embed=False, blank_page=True)
    requirements = {"min_words": 4, "max_words": 4, "page_count": 1, "source_path": str(source)}
    result = _checks(checks.run_checks(path, "narrative", requirements))
    assert result["word-count"]["verdict"] == "pass"
    assert result["pagination"]["verdict"] == "fail" and result["pagination"]["details"] == [2]
    assert result["embedded-fonts"]["verdict"] == "fail"
    assert result["source-text"]["verdict"] == "fail"
    assert result["continuity"]["verdict"] == "unmeasured"


def test_narrative_uses_retained_review_and_checks_writing_defect_history(tmp_path):
    from hoard_link.hub.agora import Agora

    agora = Agora(tmp_path / "agora.db")
    try:
        order = agora.order_create({"agent": "writer", "title": "Story", "words": "Write a story.",
                                    "folder": str(tmp_path)})["order"]["id"]
        plan = agora.plan_set({"agent": "writer", "order_id": order, "jobs": [
            {"key": "write", "title": "Draft", "station": "writer", "produces": ["manuscript"]},
        ]})
        task = plan["keys"]["write"]

        def add(kind, path, **extra):
            return agora.piece_add({"agent": "writer", "order_id": order, "title": kind,
                                    "kind": kind, "path": str(path), **extra})["piece"]["id"]

        source = tmp_path / "story.txt"
        source.write_text("Inés usó la llave.", encoding="utf-8")
        draft = add("manuscript", source, task_id=task)
        defect = agora.piece_defect({"agent": "reviewer", "piece_id": draft,
                                     "note": "The key is used before it is found."})["defect_id"]
        source.write_text("Inés encontró la llave.", encoding="utf-8")
        corrected = add("manuscript", source, task_id=task)
        reviewed = add("reviewed-manuscript", source, derives_from=[corrected])
        source.write_text("Overwritten outside Orders.", encoding="utf-8")
        path = tmp_path / "story.pdf"
        _narrative_pdf(path, "Inés encontró\nla llave.")
        pdf = add("pdf", path, derives_from=[reviewed])
        facet = checks.PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))
        args = {"piece_id": pdf, "agent": "reviewer"}
        result = facet.validate(args)
        assert _checks(result)["source-text"]["verdict"] == "pass"
        assert result["measures"]["source_piece"]["id"] == reviewed
        assert result["verdict"] == "fail"
        assert result["measures"]["continuity"][0]["disposition"] == "open"
        result = facet.record({**args, "requirements": {"continuity_declarations": {str(defect): "Pending author review."}}})
        assert result["verdict"] == "pass" and result["undo"]
        assert "Pending author review." in checks._note(result, "en")
        agora.defect_close({"agent": "reviewer", "defect_id": defect, "fixed_in": corrected,
                            "note": "The text now finds the key first."})
        final = facet.validate(args)
        assert final["measures"]["continuity"][0]["disposition"] == "resolved"
        assert final["verdict"] == "pass"
    finally:
        agora.close()


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


def test_video_identity_uses_each_shots_references_and_timecodes(tmp_path, monkeypatch):
    import random
    import math

    image = pytest.importorskip("PIL.Image")
    draw = pytest.importorskip("PIL.ImageDraw")
    np = pytest.importorskip("numpy")
    subject = image.new("RGB", (384, 384), "white")
    shapes = []
    rng = random.Random(7)
    for y in range(20, 350, 36):
        for x in range(20, 350, 36):
            shapes.append((x, y, rng.randint(3, 17), rng.randint(3, 17), rng.randint(0, 150)))
    other = subject.copy()
    shuffled = rng.sample(shapes, len(shapes))
    for picture, ordered in ((subject, shapes), (other, shuffled)):
        painter = draw.Draw(picture)
        for (x, y, *_), (_, _, w, h, tone) in zip(shapes, ordered):
            painter.ellipse((x, y, x + w, y + h), fill=(tone,) * 3)
    ref = tmp_path / "face.png"
    reference_frame = tmp_path / "reference.mp4"
    subject.save(ref)
    reference_crop = media.identity_model.prepare(subject)
    other_crop = media.identity_model.prepare(other)
    vectors = {reference_crop: np.array([1., 0.]), other_crop: np.array([.4, .916515])}
    monkeypatch.setattr(media.identity_model, "compare", lambda shots: {
        "model": {"type": "fixture"}, "scores": media.identity_model._scores(shots, vectors)})
    monkeypatch.setattr(media, "_video_info", lambda path: ([384, 384], 2.0, 24.0))
    monkeypatch.setattr(media, "_frame", lambda path, seconds: other if path != reference_frame and .5 <= seconds < 1
                        else subject)
    references = [{"path": str(ref)}]
    requirements = {"duration_s": 2, "size_px": [384, 384], "interval_s": .5, "min_similarity": .98,
                    "identity": {"min_similarity": .7}}
    shots = [{"id": "turn", "start_s": 0, "end_s": 1, "references": references},
             {"id": "cut", "start_s": 1, "end_s": 2, "references": references,
              "identity": {"references": [{"path": str(reference_frame), "time_s": .25}]}}]
    report = checks.run_checks(tmp_path / "video.mp4", "video", {**requirements, "shots": shots})
    identity = [row for row in report["checks"] if row["id"] == "identity"]
    assert [(row["shot"], row["verdict"]) for row in identity] == [("turn", "fail"), ("cut", "pass")]
    assert identity[0]["details"][0]["time_s"] == .5
    first, second = report["measures"]["shots"]
    assert math.isclose(first["samples"][1]["similarity"], 1.0, abs_tol=.01)
    assert first["identity"]["samples"][0]["similarity"] == 1.0
    assert first["identity"]["samples"][1]["similarity"] == .4
    assert second["identity"]["samples"][0]["similarity"] == 1.0


def test_video_with_unconfigured_identity_keeps_the_other_checks(tmp_path, monkeypatch):
    image = pytest.importorskip("PIL.Image")
    monkeypatch.delenv("HOARD_HUB_IDENTITY_MODEL", raising=False)
    portrait = image.effect_noise((64, 64), 35)
    path = tmp_path / "reference.png"
    portrait.save(path)
    monkeypatch.setattr(media, "_video_info", lambda _: ([64, 64], 1, 12))
    monkeypatch.setattr(media, "_frame", lambda *_: portrait)
    result = checks.run_checks(tmp_path / "clip.mp4", "video", {"size_px": [64, 64],
        "duration_s": 1, "references": [{"path": str(path)}], "min_similarity": .9, "identity": {"min_similarity": .7}})
    assert _checks(result)["visual-continuity"]["verdict"] == "pass"
    assert _checks(result)["identity"]["verdict"] == "unmeasured"


def test_web_page_overflow_names_the_element_and_excludes_a_scroll_container():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(channel="chrome", headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Chrome unavailable: {exc}")
        with browser:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.set_content("""
                <body style="margin:0">
                  <aside style="width:200px; overflow:auto"><div id="inside" style="width:700px">Scroll</div></aside>
                  <section><div id="culprit" style="width:470px">
                    <div style="width:100%">Shown in the offending box</div></div></section>
                </body>
                """)
            assert page.evaluate(layout._WEB_MEASURES)["page_overflow_elements"] == [
                {"selector": "#culprit", "source": "element", "right_px": 470, "beyond_px": 80}]


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


def _game_doc(skinned):
    nodes = [{"name": "Escalera", "mesh": 0}]
    if skinned:
        nodes.append({"name": "root"})
    return {"nodes": nodes, "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "skins": [{"joints": [1]}] if skinned else []}


@pytest.mark.parametrize("requirements, verdict", [
    ({"max_triangles": 20000}, "pass"),
    ({"max_triangles": 20000, "skinned": True}, "fail"),
    ({"max_triangles": 20000, "bones": ["mapa"]}, "fail"),
])
def test_rigid_set_piece_needs_no_skin_unless_requested(tmp_path, monkeypatch, requirements, verdict):
    native = SimpleNamespace(gltf_facts=lambda path: {"triangles": 120, "image_sizes": []},
                             read_gltf=lambda path: (_game_doc(False), b""))
    monkeypatch.setitem(sys.modules, "vulcan.asset_inspection", native)
    result = checks.run_checks(tmp_path / "escalera.glb", "game-mesh", requirements)
    by_id = _checks(result)
    assert result["verdict"] == verdict
    assert by_id["bones"]["applies"] is (verdict == "fail")
    assert by_id["vertex-weights"]["verdict"] == verdict


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
