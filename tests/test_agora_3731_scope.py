from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from hoard_link.hub.agora import Agora
from hoard_link.hub.piece_checks import PieceChecksFacet


def test_worker_validates_only_declared_registered_snapshots(tmp_path):
    with closing(Agora(tmp_path / "agora.db")) as agora:
        actor = "worker-fixture"
        order = agora.order_create({"agent": actor, "title": "Fixture", "words": "Inspect an image."})["order"]
        path = tmp_path / "sheet.png"
        Image.new("RGB", (20, 16), "white").save(path)
        piece = agora.piece_add({"agent": actor, "order_id": order["id"], "title": "Image",
                                 "kind": "image", "path": str(path)})["piece"]
        facet = PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))
        args = {"piece_id": piece["id"], "profile": "image"}
        assert facet.validate(args)["ok"]
        assert not facet.validate({**args, "_worker_read_files": []})["ok"]
        scoped = {**args, "_worker_read_files": [piece["stored"]]}
        assert facet.validate(scoped)["measures"]["images"][0]["size_px"] == [20, 16]
        Path(piece["stored"]).unlink()
        assert path.is_file()
        assert not facet.validate(scoped)["ok"]



def test_bound_worker_checks_its_new_piece_and_refuses_pieces_from_other_tasks(tmp_path):
    from hoard_link.hub.agora_faustus import file_scope, production_scope

    with closing(Agora(tmp_path / "agora.db")) as agora:
        actor = "worker-fixture"
        agora.space_create({"id": "estudio", "title": "Estudio", "agent": actor})
        agora = agora.for_space("estudio")
        order = agora.order_create({"agent": actor, "title": "Fixture", "words": "Inspect an image."})["order"]
        jobs = agora.plan_set({"agent": actor, "order_id": order["id"], "jobs": [
            {"key": "image", "title": "Image"}, {"key": "other", "title": "Other"},
        ]})["keys"]
        scope = file_scope(str(tmp_path), {"apps": {"nightingale": ["data_ingest"]}})
        scoped = production_scope(SimpleNamespace(_agora=lambda: agora, space_id="estudio"), actor, scope, jobs["image"])
        assert scoped["production"] == {
            "space_id": "estudio", "task_id": jobs["image"], "order_id": order["id"], "agent": actor,
        }
        assert scoped["apps"] == scope["apps"]
        agora.task_claim({"agent": actor, "task_id": jobs["image"]})

        path = tmp_path / "sheet.png"
        Image.new("RGB", (20, 16), "white").save(path)
        base = {"agent": actor, "order_id": order["id"], "title": "Image", "kind": "image", "path": str(path)}
        piece = agora.piece_add({**base, "task_id": jobs["image"], "_worker_task_id": jobs["image"]})["piece"]
        other = agora.piece_add({**base, "task_id": jobs["other"]})["piece"]
        facet = PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))
        args = {"piece_id": piece["id"], "space_id": "estudio", "agent": actor, "task_id": jobs["image"],
                "profile": "image", "requirements": {"size_px": [20, 16]},
                "_worker_read_files": [], "_worker_output_path": str(path)}
        result = facet.record(args)
        assert result["ok"] and result["verdict"] == "pass" and result["undo"]
        assert not facet.record({**args, "piece_id": other["id"]})["ok"]
        assert not facet.record({**args, "agent": "another-worker"})["ok"]
        assert not facet.record({**args, "_worker_output_path": str(tmp_path / "other.png")})["ok"]
        Path(piece["stored"]).unlink()
        assert not facet.record(args)["ok"]


def test_confined_worker_document_check_never_fetches_links(tmp_path, monkeypatch):
    from zipfile import ZipFile

    from hoard_link.hub import piece_checks_sources

    calls = []
    monkeypatch.setattr(piece_checks_sources, "_http",
                        lambda url: calls.append(url) or {"target": url, "resolved": True, "status": 200})
    path = tmp_path / "report.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml",
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         "<w:body><w:p><w:r><w:t>See https://example.test/source.</w:t></w:r></w:p></w:body></w:document>")
    with closing(Agora(tmp_path / "agora.db")) as agora:
        actor = "worker-fixture"
        order = agora.order_create({"agent": actor, "title": "Fixture", "words": "Inspect a document."})["order"]
        piece = agora.piece_add({"agent": actor, "order_id": order["id"], "title": "Report",
                                 "kind": "document", "path": str(path)})["piece"]
        facet = PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))
        args = {"piece_id": piece["id"], "profile": "document"}
        assert facet.validate(args)["checks"][0]["verdict"] == "pass" and calls == ["https://example.test/source"]
        calls.clear()
        scoped = facet.validate({**args, "_worker_read_files": [piece["stored"]], "requirements": {"resolve_remote": True}})
        assert scoped["ok"] and calls == []
        assert scoped["measures"]["citations"][0]["reason"] == "remote disabled"
        assert scoped["checks"][0]["verdict"] == "unmeasured"
