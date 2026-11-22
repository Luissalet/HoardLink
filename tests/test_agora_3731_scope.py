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


def _counting_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.end_headers()
            self.wfile.write(b"GIF89a")

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


def test_confined_worker_web_check_sends_no_request_outside_its_page_server(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from zipfile import ZipFile

    import pytest

    engine = Path(os.environ.get("VITRUVIUS_REPO", r"E:\Development\IA\Hoards\Faustusables\Vitruvius's Hoard"))
    python = engine / "venv/Scripts/python.exe"
    if not python.is_file():
        pytest.skip("The installed page engine is unavailable")
    outside, hits = _counting_server()
    try:
        remote = f"http://127.0.0.1:{outside.server_port}"
        page = ('<!doctype html><html lang="en"><title>Fixture</title><body><p>Fixture</p>'
                f'<img alt="" src="{remote}/image?secret=1"><link rel="stylesheet" href="{remote}/style.css">'
                f'<script>fetch("{remote}/fetch?secret=2").catch(() => {{}});'
                f'try {{ new WebSocket("ws://127.0.0.1:{outside.server_port}/socket"); }} catch (e) {{}}'
                f'navigator.sendBeacon("{remote}/beacon", "secret");</script></body></html>')
        site = tmp_path / "site.zip"
        with ZipFile(site, "w") as archive:
            archive.writestr("index.html", page)
        hub_root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(hub_root), str(engine))), "PYTHONUTF8": "1"}

        def run(requirements):
            done = subprocess.run([str(python), "-m", "hoard_link.hub.piece_checks", "web", str(site),
                                   json.dumps(requirements)], cwd=hub_root, env=env, capture_output=True,
                                  encoding="utf-8", timeout=170)
            assert done.returncode == 0, done.stderr[-800:]
            return json.loads(done.stdout)

        result = run({"viewports": [[800, 600]], "_offline": True})
        assert result["measures"].get("viewports"), result
        assert hits == []
        # The control: the same page reaches the outside server when the Hub renders it for a person.
        run({"viewports": [[800, 600]]})
        assert any(path.startswith("/image") for path in hits), hits
    finally:
        outside.shutdown()
        outside.server_close()


def test_confined_worker_deck_with_external_resources_is_not_rendered(tmp_path, monkeypatch):
    import pytest
    from zipfile import ZipFile

    from hoard_link.hub import piece_checks as checks
    from hoard_link.hub import piece_checks_layout as layout

    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    plain = tmp_path / "plain.pptx"
    deck.save(plain)
    linked = tmp_path / "linked.pptx"
    with ZipFile(plain) as source, ZipFile(linked, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "ppt/slides/_rels/slide1.xml.rels":
                data = data.replace(b"</Relationships>", (
                    b'<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    b'relationships/image" Target="https://example.invalid/picture.png" TargetMode="External"/>'
                    b"</Relationships>"))
            target.writestr(item, data)
    rendered = []
    monkeypatch.setattr(layout, "_pdf_words", lambda path: rendered.append(path) or [layout._words("")])
    for path, offline, expected in ((plain, True, 1), (linked, False, 1), (linked, True, 0)):
        rendered.clear()
        result = checks.run_checks(path, "slides", {"slide_count": 1, **({"_offline": True} if offline else {})})
        assert len(rendered) == expected, (path.name, offline)
    row = {entry["id"]: entry for entry in result["checks"]}["rendered-text"]
    assert row["verdict"] == "unmeasured" and "external resources" in row["reason"]["en"]
