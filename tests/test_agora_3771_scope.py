"""A confined worker's piece check must not reveal which host paths exist outside its declared scope."""

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from hoard_link.hub import piece_checks_sources as sources
from hoard_link.hub.agora import Agora
from hoard_link.hub.piece_checks import PieceChecksFacet

RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships">{}</Relationships>')
LINK = ('<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'hyperlink" Target="{target}" TargetMode="External"/>')
WORD = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        '<w:p><w:r><w:t>Body</w:t></w:r></w:p></w:body></w:document>')


def _docx(path, targets):
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", WORD)
        archive.writestr("word/_rels/document.xml.rels",
                         RELS.format("".join(LINK.format(n=n, target=t) for n, t in enumerate(targets, 1))))


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
    return touched


def _world(tmp_path, targets):
    """A registered DOCX linking ``targets``; an agora, the piece and a facet for it."""
    path = tmp_path / "report.docx"
    _docx(path, targets)
    agora = Agora(tmp_path / "agora.db")
    order = agora.order_create({"agent": "worker-fixture", "title": "Fixture", "words": "Check a document."})["order"]
    piece = agora.piece_add({"agent": "worker-fixture", "order_id": order["id"], "title": "Report",
                             "kind": "document", "path": str(path)})["piece"]
    facet = PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))
    return agora, piece, facet


def _rows(result):
    return {row["target"]: row for row in result["measures"]["citations"]}


def test_confined_worker_cannot_tell_which_outside_paths_exist(tmp_path, spied_disk):
    inside, outside = tmp_path / "declared", tmp_path / "secret"
    inside.mkdir(), outside.mkdir()
    (inside / "note.txt").write_text("ok")
    (outside / "present.txt").write_text("host")
    targets = {"inside": inside / "note.txt", "inside_missing": inside / "gone.txt",
               "present": outside / "present.txt", "absent": outside / "absent.txt",
               "uri": (outside / "present.txt").as_uri(), "unc": r"\\nohost\share\a.txt"}
    targets = {key: str(value) for key, value in targets.items()}
    agora, piece, facet = _world(tmp_path, targets.values())
    with closing(agora):
        spied_disk.clear()
        result = facet.validate({"piece_id": piece["id"], "_worker_read_files": [piece["stored"], targets["inside"]],
                                 "_worker_read_paths": [str(inside)]})
        rows = _rows(result)
        assert rows[targets["inside"]]["resolved"] is True
        assert rows[targets["inside_missing"]]["resolved"] is False
        for key in ("present", "absent", "uri", "unc"):
            row = rows[targets[key]]
            assert row["resolved"] is None and "scope" in row["reason"], key
        assert result["verdict"] == "fail"  # the missing in-scope link is still a real defect
        assert [item for item in spied_disk if "secret" in item or "nohost" in item] == []
        # present and absent are indistinguishable but for the target itself
        assert {k: v for k, v in rows[targets["present"]].items() if k != "target"} == {
            k: v for k, v in rows[targets["absent"]].items() if k != "target"}


def test_relative_and_prefix_tricks_stay_outside_the_scope(tmp_path):
    declared, sibling = tmp_path / "declared", tmp_path / "declared-extra"
    declared.mkdir(), sibling.mkdir()
    (sibling / "x.txt").write_text("x")
    traversal = str(declared / ".." / "declared-extra" / "x.txt")
    targets = [str(sibling / "x.txt"), traversal, "../declared-extra/x.txt", "../../" + tmp_path.name]
    agora, piece, facet = _world(tmp_path, targets)
    with closing(agora):
        rows = _rows(facet.validate({"piece_id": piece["id"], "_worker_read_files": [piece["stored"]],
                                     "_worker_write_paths": [str(declared)]}))
        assert all(rows[target]["resolved"] is None and "scope" in rows[target]["reason"] for target in targets)


def test_requirement_citations_and_the_workers_own_scope_claim_are_checked(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("host")
    agora, piece, facet = _world(tmp_path, [])
    with closing(agora):
        args = {"piece_id": piece["id"], "_worker_read_files": [piece["stored"]],
                "requirements": {"citations": {"A1": str(outside)}, "_scope": {"folders": [str(tmp_path)]}}}
        row = facet.validate(args)["measures"]["citations"][0]
        assert row["resolved"] is None and "scope" in row["reason"]


def test_an_unconfined_check_still_follows_local_links(tmp_path):
    outside = tmp_path / "present.txt"
    outside.write_text("host")
    agora, piece, facet = _world(tmp_path, [str(outside), str(tmp_path / "absent.txt")])
    with closing(agora):
        rows = _rows(facet.validate({"piece_id": piece["id"]}))
        assert rows[str(outside)]["resolved"] is True
        assert rows[str(tmp_path / "absent.txt")]["resolved"] is False


def test_the_output_a_worker_may_write_is_inside_its_scope(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text("mine")
    args = {"_worker_read_files": [str(tmp_path / "piece.docx")]}
    assert sources.resolve(str(out), tmp_path, scope=sources.worker_scope(args))["resolved"] is None
    scope = sources.worker_scope({**args, "_worker_output_path": str(out)})
    assert sources.resolve(str(out), tmp_path, scope=scope)["resolved"] is True
    assert sources.worker_scope({}) is None


def test_a_deck_hyperlink_outside_the_scope_is_not_measured(tmp_path, spied_disk):
    pptx = pytest.importorskip("pptx")
    outside = tmp_path / "secret" / "present.txt"
    outside.parent.mkdir()
    outside.write_text("host")
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Title"
    run = slide.shapes.title.text_frame.paragraphs[0].runs[0]
    run.hyperlink.address = str(outside)
    path = tmp_path / "deck.pptx"
    deck.save(path)
    agora = Agora(tmp_path / "agora.db")
    with closing(agora):
        order = agora.order_create({"agent": "worker-fixture", "title": "Deck", "words": "Check a deck."})["order"]
        piece = agora.piece_add({"agent": "worker-fixture", "order_id": order["id"], "title": "Deck",
                                 "kind": "presentation-pptx", "path": str(path)})["piece"]
        facet = PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))
        spied_disk.clear()
        links = facet.validate({"piece_id": piece["id"], "_worker_read_files": [piece["stored"]]})["measures"]["links"]
        row = next(item for item in links if item["external"])
        assert row["resolved"] is None and "scope" in row["reason"]
        assert [item for item in spied_disk if "secret" in item] == []


def test_the_gate_forwards_the_declared_folders(tmp_path):
    seen = []
    facet = PieceChecksFacet(SimpleNamespace())
    facet.validate = lambda args: seen.append(args) or {"ok": False, "error": "stop"}
    facet.gate({"pieces": [{"piece_id": 1}], "_worker_read_files": ["a"], "_worker_read_paths": ["b"],
                "_worker_write_paths": ["c"]})
    assert seen[0]["_worker_read_paths"] == ["b"] and seen[0]["_worker_write_paths"] == ["c"]
