"""Requirement paths of a confined worker's check are judged by their text, never by asking the disk."""

from contextlib import closing
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard_link.hub import piece_checks as checks
from hoard_link.hub import piece_checks_media as media
from hoard_link.hub.agora import Agora
from hoard_link.hub.piece_checks import PieceChecksFacet

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("numpy")


@pytest.fixture
def spied_disk(monkeypatch):
    """Record every path the Hub asks the filesystem about."""
    touched = []
    for name in ("exists", "resolve", "is_file", "is_dir", "stat"):
        original = getattr(Path, name)

        def spy(self, *args, _original=original, **kwargs):
            touched.append(str(self))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, spy)
    real_open = Image.open

    def spy_open(fp, *args, **kwargs):
        touched.append(str(fp))
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", spy_open)
    return touched


@pytest.fixture(autouse=True)
def video(monkeypatch):
    monkeypatch.setattr(media, "_video_info", lambda path, safe=False: ([64, 64], 1.0, 12.0))
    monkeypatch.setattr(media, "_frame", lambda path, seconds, safe=False: Image.new("RGB", (64, 64), "blue"))


def _world(tmp_path, kind="video-clip", name="clip.mp4", content=b"clip"):
    path = tmp_path / name
    path.write_bytes(content)
    agora = Agora(tmp_path / "agora.db")
    order = agora.order_create({"agent": "worker-fixture", "title": "Fixture", "words": "Check a piece."})["order"]
    piece = agora.piece_add({"agent": "worker-fixture", "order_id": order["id"], "title": "Piece",
                             "kind": kind, "path": str(path)})["piece"]
    return agora, piece, PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))


def _folders(tmp_path):
    inside, secret = tmp_path / "declared", tmp_path / "secret"
    inside.mkdir(), secret.mkdir()
    for folder in (inside, secret):
        Image.new("RGB", (64, 64), "blue").save(folder / "present.png")
    return inside, secret


def _same(first, second, first_path, second_path):
    """The two answers, but for the elapsed time and the path each one named, are the same bytes."""
    texts = []
    for result, path in ((first, first_path), (second, second_path)):
        result = {key: value for key, value in result.items() if key != "elapsed_ms"}
        texts.append(json.dumps(result, sort_keys=True).replace(json.dumps(str(path))[1:-1], "<ref>"))
    return texts[0] == texts[1]


def _check(result, key):
    return next(row for row in result["checks"] if row["id"] == key)


def test_a_reference_outside_the_scope_is_refused_the_same_whether_it_exists_or_not(tmp_path, spied_disk):
    inside, secret = _folders(tmp_path)
    agora, piece, facet = _world(tmp_path)
    with closing(agora):
        def validate(reference):
            spied_disk.clear()
            return facet.validate({"piece_id": piece["id"], "_worker_read_files": [piece["stored"]],
                                   "_worker_read_paths": [str(inside)],
                                   "requirements": {"min_similarity": .9, "references": [{"path": str(reference)}]}})

        present, absent = validate(secret / "present.png"), validate(secret / "absent.png")
        assert [item for item in spied_disk if "secret" in item] == []
        assert present["verdict"] == "unmeasured" and "scope" in _check(present, "inspection")["detail"]
        assert _same(present, absent, secret / "present.png", secret / "absent.png")
        measured = validate(inside / "present.png")
        assert _check(measured, "visual-continuity")["verdict"] == "pass"
        assert "scope" not in _check(validate(inside / "absent.png"), "inspection")["detail"]  # a missing in-scope file is still said


def test_shot_references_and_identity_references_are_checked_where_they_are_opened(tmp_path, spied_disk):
    inside, secret = _folders(tmp_path)
    agora, piece, facet = _world(tmp_path)
    with closing(agora):
        def validate(reference):
            spied_disk.clear()
            ref = [{"path": str(reference)}]
            shots = [{"id": "a", "start_s": 0, "end_s": 1, "references": ref,
                      "identity": {"references": ref, "min_similarity": .5}}]
            result = facet.validate({"piece_id": piece["id"], "_worker_read_files": [piece["stored"]],
                                     "_worker_read_paths": [str(inside)],
                                     "requirements": {"min_similarity": .9, "shots": shots,
                                                      "identity": {"references": ref}}})
            return result

        present, absent = validate(secret / "present.png"), validate(secret / "absent.png")
        assert [item for item in spied_disk if "secret" in item] == []
        assert present["verdict"] == "unmeasured"
        assert _same(present, absent, secret / "present.png", secret / "absent.png")
        # The identity route reports it per shot instead of stopping the check.
        facet_result = validate(secret / "present.png")
        assert "scope" in json.dumps(facet_result)


def test_identity_references_alone_keep_the_other_checks(tmp_path):
    _, secret = _folders(tmp_path)
    agora, piece, facet = _world(tmp_path)
    with closing(agora):
        result = facet.validate({"piece_id": piece["id"], "_worker_read_files": [piece["stored"]],
                                 "requirements": {"size_px": [64, 64], "identity": {
                                     "references": [{"path": str(secret / "present.png")}], "min_similarity": .5}}})
        assert _check(result, "resolution")["verdict"] == "pass"
        identity = _check(result, "identity")
        assert identity["verdict"] == "unmeasured" and "scope" in identity["detail"]


def test_a_reference_frame_of_a_video_outside_the_scope_is_not_decoded(tmp_path, monkeypatch):
    _, secret = _folders(tmp_path)
    decoded = []
    monkeypatch.setattr(media, "_frame", lambda path, seconds, safe=False: decoded.append(path) or Image.new("RGB", (64, 64)))
    report = checks.run_checks(tmp_path / "clip.mp4", "video", {
        "min_similarity": .9, "references": [{"path": str(secret / "ref.mp4"), "time_s": 0}],
        "_offline": True, "_scope": {"files": [], "folders": [str(tmp_path / "declared")]}})
    assert decoded == [] and report["verdict"] == "unmeasured"


def test_relative_home_and_traversal_references_are_outside_the_scope(tmp_path, monkeypatch):
    inside, secret = _folders(tmp_path)
    monkeypatch.chdir(secret)
    monkeypatch.setenv("HOME", str(secret))
    monkeypatch.setenv("USERPROFILE", str(secret))
    scope = {"files": [], "folders": [str(inside)]}
    for text in ("present.png", "~/present.png", str(inside / ".." / "secret" / "present.png"),
                 str(inside) + "-extra/present.png"):
        with pytest.raises(ValueError, match="scope"):
            media._reference({"path": text}, media._signature, scope)
    assert media._reference({"path": str(inside / "present.png")}, media._signature, scope)
    assert media._reference({"path": "present.png"}, media._signature)  # not confined: as before

def test_an_unconfined_check_still_opens_any_reference(tmp_path):
    _, secret = _folders(tmp_path)
    agora, piece, facet = _world(tmp_path)
    with closing(agora):
        result = facet.validate({"piece_id": piece["id"], "requirements": {
            "min_similarity": .9, "references": [{"path": str(secret / "present.png")}]}})
        assert _check(result, "visual-continuity")["verdict"] == "pass"
        result = facet.validate({"piece_id": piece["id"], "requirements": {
            "min_similarity": .9, "references": [{"path": str(secret / "absent.png")}]}})
        assert result["verdict"] == "unmeasured" and "scope" not in _check(result, "inspection")["detail"]


def test_the_workers_own_scope_claim_in_the_requirements_changes_nothing(tmp_path):
    _, secret = _folders(tmp_path)
    agora, piece, facet = _world(tmp_path)
    with closing(agora):
        result = facet.validate({"piece_id": piece["id"], "_worker_read_files": [piece["stored"]], "requirements": {
            "min_similarity": .9, "references": [{"path": str(secret / "present.png")}],
            "_scope": {"files": [str(secret / "present.png")], "folders": [str(tmp_path)]}}})
        assert "scope" in _check(result, "inspection")["detail"]


def _narrative(tmp_path, source_text="Inés volvió."):
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "story.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((60, 60), source_text)
        doc.save(pdf)
    return pdf


def test_a_narrative_source_path_outside_the_scope_is_refused_the_same_whether_it_exists_or_not(tmp_path, spied_disk):
    _, secret = _folders(tmp_path)
    present, absent = secret / "story.txt", secret / "gone.txt"
    present.write_text("Inés volvió.", encoding="utf-8")
    pdf = _narrative(tmp_path)
    inside = tmp_path / "declared" / "story.txt"
    inside.write_text("Inés volvió.", encoding="utf-8")
    facet = PieceChecksFacet(SimpleNamespace())

    def validate(source):
        spied_disk.clear()
        return facet.validate({"path": str(pdf), "profile": "narrative", "_worker_read_files": [str(pdf)],
                               "_worker_read_paths": [str(tmp_path / "declared")],
                               "requirements": {"source_path": str(source)}})

    first, second = validate(present), validate(absent)
    assert [item for item in spied_disk if "secret" in item] == []
    assert "scope" in _check(first, "inspection")["detail"]
    assert _same(first, second, present, absent)
    assert _check(validate(inside), "source-text")["verdict"] == "pass"
    # Not confined, the manuscript is compared as before.
    plain = facet.validate({"path": str(pdf), "profile": "narrative", "requirements": {"source_path": str(present)}})
    assert _check(plain, "source-text")["verdict"] == "pass"

