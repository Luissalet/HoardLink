"""fam_docs: PDF operations, extraction and OCR through Kafka's Hoard — against a fake hub."""
from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from hoard_link import _famsvc, family, fam_docs
from tests.commons.fam_hub import TOKEN, FakeHub, ToolError, sequence
from tests.hub.conftest import free_port

PNG_1x1 = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def configure(tmp_path, url, token=TOKEN):
    tf = tmp_path / "mcp-token"
    tf.write_text(token, encoding="utf-8")
    family.configure("borges", token_file=str(tf), hub=url)


@pytest.fixture(autouse=True)
def _isolated_state():
    saved = dict(family._state)
    _famsvc.forget_availability()
    yield
    family._state.clear()
    family._state.update(saved)
    _famsvc.forget_availability()


@pytest.fixture
def hub(tmp_path):
    h = FakeHub()
    h.app("kafka")
    configure(tmp_path, h.url)
    yield h
    h.close()


def out(*paths, **extra):
    return {"ok": True, "operation": "x", "outputs": [{"path": p} for p in paths], "output": paths[0] if len(paths) == 1 else None, **extra}


# ---- the PDF workshop ------------------------------------------------------------------------------------------------

def test_pdf_merge_maps_to_kafkas_arguments(hub, tmp_path):
    hub.on("kafka", "pdf_merge", out("/w/m.pdf"))
    res = fam_docs.pdf_merge([tmp_path / "a.pdf", "d_123", "/b.pdf"], ranges=["", "1-3", "2,5-"], out_dir=tmp_path / "w", file_result=True)
    assert res["ok"] is True and res["via"] == "kafka" and res["output"] == "/w/m.pdf" and res["paths"] == ["/w/m.pdf"]
    (call,) = hub.of("pdf_merge")
    assert call["args"] == {"files": [str(tmp_path / "a.pdf"), "d_123", str(Path("/b.pdf").resolve())], "ranges": ["", "1-3", "2,5-"],
                            "out_dir": str(tmp_path / "w"), "file_result": True}


def test_pdf_split_pages_pages_and_info_arguments(hub):
    hub.on("kafka", "pdf_split", out("/w/1.pdf", "/w/2.pdf"))
    hub.on("kafka", "pdf_pages", out("/w/r.pdf"))
    hub.on("kafka", "pdf_info", {"ok": True, "pages": 4, "has_text": True})
    res = fam_docs.pdf_split("/a.pdf", mode="every", every=2, out_dir="/w")
    assert res["paths"] == ["/w/1.pdf", "/w/2.pdf"] and res["output"] is None
    assert hub.of("pdf_split")[0]["args"] == {"file": str(Path("/a.pdf").resolve()), "mode": "every", "every": 2, "out_dir": str(Path("/w").resolve())}
    fam_docs.pdf_split("/a.pdf", ranges="1-3,4-")
    assert hub.of("pdf_split")[1]["args"] == {"file": str(Path("/a.pdf").resolve()), "mode": "ranges", "ranges": "1-3,4-"}
    fam_docs.pdf_pages("/a.pdf", "rotate", pages="2", degrees=270, output="/w/r.pdf")
    assert hub.of("pdf_pages")[0]["args"] == {"action": "rotate", "file": str(Path("/a.pdf").resolve()), "pages": "2", "degrees": 270, "output": str(Path("/w/r.pdf").resolve())}
    fam_docs.pdf_pages("/a.pdf", "extract", pages="1-3")
    assert hub.of("pdf_pages")[1]["args"] == {"action": "extract", "file": str(Path("/a.pdf").resolve()), "pages": "1-3"}
    info = fam_docs.pdf_info("/a.pdf", password="s3cret")
    assert info["ok"] and info["pages"] == 4 and hub.of("pdf_info")[0]["args"] == {"file": str(Path("/a.pdf").resolve()), "password": "s3cret"}


def test_the_other_workshop_tools(hub):
    for tool in ("pdf_compress", "pdf_watermark", "pdf_protect", "pdf_metadata_set", "pdf_to_images", "pdf_from_images", "pdf_from_office", "images_compress"):
        hub.on("kafka", tool, out("/w/o"))
    fam_docs.pdf_compress("/a.pdf", preset="screen", target_mb=2, engine="pypdf")
    fam_docs.pdf_watermark("/a.pdf", "CONFIDENCIAL", opacity=0.5, color="#ff0000", pages="1-2")
    fam_docs.pdf_protect("/a.pdf", "protect", "pw", allow_copy=False)
    fam_docs.pdf_metadata_set("/a.pdf", title="T", author="")
    fam_docs.pdf_to_images("/a.pdf", pages="1-2", format="jpg", dpi=100, out_dir="/w")
    fam_docs.images_to_pdf(["/1.png", "/2.png"], page_size="fit", margin_mm=0, out_dir="/w")
    fam_docs.pdf_from_office("/a.docx")
    fam_docs.images_compress(["/imgs"], limit_kb=300, skip_small=True, out_dir="/w", time_limit_s=10)
    args = {t: hub.of(t)[0]["args"] for t in ("pdf_compress", "pdf_watermark", "pdf_protect", "pdf_metadata_set", "pdf_to_images", "pdf_from_images",
                                              "pdf_from_office", "images_compress")}
    assert args["pdf_compress"] == {"file": str(Path("/a.pdf").resolve()), "preset": "screen", "target_mb": 2, "engine": "pypdf"}
    assert args["pdf_watermark"] == {"file": str(Path("/a.pdf").resolve()), "text": "CONFIDENCIAL", "opacity": 0.5, "angle": 45, "font_size": 60, "color": "#ff0000", "pages": "1-2"}
    assert args["pdf_protect"] == {"action": "protect", "file": str(Path("/a.pdf").resolve()), "password": "pw", "allow_print": True, "allow_copy": False, "allow_modify": True}
    assert args["pdf_metadata_set"] == {"file": str(Path("/a.pdf").resolve()), "title": "T", "author": ""}                   # "" clears, an absent key keeps
    assert args["pdf_to_images"] == {"file": str(Path("/a.pdf").resolve()), "pages": "1-2", "format": "jpg", "dpi": 100, "quality": 90, "out_dir": str(Path("/w").resolve())}
    assert args["pdf_from_images"] == {"images": [str(Path("/1.png").resolve()), str(Path("/2.png").resolve())], "page_size": "fit", "margin_mm": 0, "orientation": "auto", "out_dir": str(Path("/w").resolve())}
    assert args["pdf_from_office"] == {"file": str(Path("/a.docx").resolve()), "engine": "auto"}
    assert args["images_compress"] == {"sources": [str(Path("/imgs").resolve())], "limit_kb": 300, "recursive": True, "lossless_only": False, "skip_small": True, "out_dir": str(Path("/w").resolve()),
                                       "time_limit_s": 10.0}
    assert hub.of("images_compress")[0]["timeout_s"] >= 40


def test_a_workshop_failure_is_kafkas_message(hub):
    def refuse(a, c):
        raise ToolError(400, "Contraseña incorrecta")
    hub.on("kafka", "pdf_info", refuse)
    assert fam_docs.pdf_info("/a.pdf") == {"ok": False, "error": "Contraseña incorrecta", "via": "kafka", "kind": "tool_error"}


def test_a_workshop_answer_that_says_ok_false_is_an_error(hub):
    hub.on("kafka", "pdf_merge", {"ok": False, "error": "need at least two PDFs"})
    res = fam_docs.pdf_merge(["/a.pdf", "/b.pdf"])
    assert res["ok"] is False and res["error"] == "need at least two PDFs" and res["via"] == "kafka"


# ---- extract ---------------------------------------------------------------------------------------------------------

KAFKA_EXTRACT = {"kind": "pdf", "title": "Contrato", "text": "Cláusula primera\n\nCláusula segunda",
                 "units": [{"kind": "page", "number": 1, "title": "", "text": "Cláusula primera"}, {"kind": "page", "number": 2, "title": "", "text": "Cláusula segunda"}],
                 "needs_ocr": False, "notes": [], "pages_ocr": 2}


def test_extract_through_kafka(hub, tmp_path):
    hub.on("kafka", "doc_extract", KAFKA_EXTRACT)
    res = fam_docs.extract(tmp_path / "scan.pdf", ocr="force", max_pages=10, lang="en")
    assert res["ok"] is True and res["via"] == "kafka" and res["kind"] == "pdf" and res["pages_ocr"] == 2 and res["needs_ocr"] is False
    assert [u["number"] for u in res["units"]] == [1, 2] and res["text"].startswith("Cláusula primera")
    (call,) = hub.of("doc_extract")
    assert call["args"] == {"path": str(tmp_path / "scan.pdf"), "ocr": "force", "max_pages": 10, "lang": "en", "wait_s": 150}


def test_extract_follows_a_long_job_through_ocr_status(hub):
    hub.on("kafka", "doc_extract", {"job_id": "o1", "status": "running", "pages_done": 3, "pages_total": 40})
    hub.on("kafka", "ocr_status", sequence({"job_id": "o1", "status": "running", "pages_done": 20}, {**KAFKA_EXTRACT, "job_id": "o1", "status": "done"}))
    res = fam_docs.extract("/scan.pdf", timeout_s=60)
    assert res["ok"] is True and res["via"] == "kafka" and res["pages_ocr"] == 2
    assert [c["args"]["job_id"] for c in hub.of("ocr_status")] == ["o1", "o1"]


def test_extract_past_its_deadline_gives_the_job_id(hub):
    hub.on("kafka", "doc_extract", {"job_id": "o2", "status": "running"})
    hub.on("kafka", "ocr_status", {"job_id": "o2", "status": "running"})
    t0 = time.monotonic()
    res = fam_docs.extract("/scan.pdf", timeout_s=1)
    assert time.monotonic() - t0 < 5
    assert res["ok"] is False and res["kind"] == "timeout" and res["job_id"] == "o2" and res["still_running"] is True and res["via"] == "kafka"


def test_extract_validates_its_arguments_without_calling_the_hub(hub):
    assert fam_docs.extract("/a.pdf", ocr="maybe")["kind"] == "client_error"
    assert fam_docs.extract("")["error"] == "path is required"
    assert hub.calls == []


def test_extract_a_document_kafka_cannot_read_is_an_error_with_the_partial_result(hub):
    hub.on("kafka", "doc_extract", {**KAFKA_EXTRACT, "text": "", "units": [], "error": "the file is empty"})
    res = fam_docs.extract("/a.pdf")
    assert res["ok"] is False and res["error"] == "the file is empty" and res["kind"] == "tool_error" and res["result"]["units"] == []


def test_extract_a_tool_error_is_not_redone_locally(hub, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello there, this is a text", encoding="utf-8")

    def refuse(a, c):
        raise ToolError(400, "No such file")
    hub.on("kafka", "doc_extract", refuse)
    res = fam_docs.extract(f)
    assert res == {"ok": False, "error": "No such file", "via": "kafka", "kind": "tool_error"}


@pytest.mark.parametrize("why", ["hub_down", "app_down", "tool_missing"])
def test_extract_falls_back_to_the_light_readers(tmp_path, hub, why):
    docx = tmp_path / "carta.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                                       '<w:p><w:r><w:t>Estimado señor, le escribo por la garantía de la lavadora.</w:t></w:r></w:p></w:body></w:document>')
    if why == "hub_down":
        configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    elif why == "app_down":
        hub.set_state("kafka", "stopped")
    res = fam_docs.extract(docx)
    assert res["ok"] is True and res["via"] == "local" and "garantía de la lavadora" in res["text"] and res["pages_ocr"] == 0
    assert res["units"] and res["needs_ocr"] is False and {"kind", "title", "text", "units", "needs_ocr", "notes"} <= set(res)


def test_the_local_fallback_marks_a_scan_as_needing_ocr(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    img = tmp_path / "ticket.png"
    img.write_bytes(PNG_1x1)
    res = fam_docs.extract(img)
    assert res["ok"] is True and res["via"] == "local" and res["needs_ocr"] is True and any("OCR was not run" in n for n in res["notes"])
    assert fam_docs.extract(img, ocr="off")["notes"] == [] or all("OCR was not run" not in n for n in fam_docs.extract(img, ocr="off")["notes"])


def test_the_local_fallback_reports_a_bad_file_and_never_raises(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    res = fam_docs.extract(empty)
    assert res["ok"] is False and res["via"] == "local" and "empty" in res["error"]
    res = fam_docs.extract(tmp_path / "nope.pdf")
    assert res["ok"] is False and res["via"] == "local" and "hub unreachable" in res["error"] and res["hub_error"] == "hub unreachable"


def test_extract_without_the_fallback_says_the_hub_is_down(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    assert fam_docs.extract("/a.pdf", local_fallback=False) == {"ok": False, "error": "hub unreachable", "via": "kafka", "kind": "hub_down"}


# ---- OCR ---------------------------------------------------------------------------------------------------------------

def test_ocr_image_and_status(hub, tmp_path):
    hub.on("kafka", "ocr_image", {"text": "TOTAL 12,50", "blocks": [{"text": "TOTAL 12,50", "box": [[0, 0], [1, 0], [1, 1], [0, 1]], "score": 0.9}], "backend": "rapidocr"})
    hub.on("kafka", "ocr_status", {"available": True, "backend": "rapidocr", "languages": ["es", "en"]})
    res = fam_docs.ocr_image(tmp_path / "t.png", lang="es", blocks=True)
    assert res["ok"] and res["text"] == "TOTAL 12,50" and res["backend"] == "rapidocr" and res["via"] == "kafka" and len(res["blocks"]) == 1
    assert hub.of("ocr_image")[0]["args"] == {"path": str(tmp_path / "t.png"), "lang": "es", "blocks": True}
    fam_docs.ocr_image("/t.png")
    assert hub.of("ocr_image")[1]["args"] == {"path": str(Path(str(Path("/t.png").resolve())).resolve()), "lang": "es"}
    st = fam_docs.ocr_status()
    assert st["ok"] and st["available"] is True and hub.of("ocr_status")[0]["args"] == {}


def test_ocr_image_has_no_local_fallback(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    assert fam_docs.ocr_image("/t.png") == {"ok": False, "error": "hub unreachable", "via": "kafka", "kind": "hub_down"}
    assert fam_docs.ocr_status()["error"] == "hub unreachable"


def test_ocr_pdf_runs_as_a_job(hub):
    hub.on("kafka", "ocr_pdf", {"job_id": "p1", "status": "running", "pages_done": 1, "pages_total": 3})
    hub.on("kafka", "ocr_status", sequence({"job_id": "p1", "status": "done", "text": "uno\n\ndos", "pages": [{"page": 1, "text": "uno"}, {"page": 2, "text": "dos"}],
                                            "pages_done": 2, "pages_total": 2, "backend": "rapidocr"}))
    res = fam_docs.ocr_pdf("/scan.pdf", pages="1-2", dpi=150, lang="es", max_pages=5, timeout_s=30)
    assert res["ok"] and res["via"] == "kafka" and res["pages_done"] == 2 and res["pages"][1]["text"] == "dos"
    assert hub.of("ocr_pdf")[0]["args"] == {"path": str(Path(str(Path("/scan.pdf").resolve())).resolve()), "pages": "1-2", "dpi": 150, "lang": "es", "max_pages": 5, "wait_s": 30}
    st = fam_docs.ocr_status("p1", wait_s=500)
    assert hub.of("ocr_status")[-1]["args"] == {"job_id": "p1", "wait_s": 150}
    assert st["ok"]


def test_ocr_pdf_past_its_deadline(hub):
    hub.on("kafka", "ocr_pdf", {"job_id": "p2", "status": "running"})
    hub.on("kafka", "ocr_status", {"job_id": "p2", "status": "running"})
    res = fam_docs.ocr_pdf("/scan.pdf", timeout_s=1)
    assert res["ok"] is False and res["kind"] == "timeout" and res["job_id"] == "p2"


def test_app_down_unknown_app_and_missing_tool(hub):
    hub.set_state("kafka", "stopped")
    res = fam_docs.pdf_info("/a.pdf")
    assert res == {"ok": False, "error": "kafka unreachable", "via": "kafka", "kind": "app_down"}
    hub.set_state("kafka", "running")
    res = fam_docs.ocr_image("/t.png")
    assert res["kind"] == "tool_missing" and "ocr_image" in res["error"]
    del hub.apps["kafka"]
    assert fam_docs.pdf_info("/a.pdf")["kind"] == "app_missing"


def test_nothing_raises_and_available_follows_the_app(hub):
    assert fam_docs.extract(None)["ok"] is False
    assert fam_docs.pdf_merge(None)["ok"] is False
    assert fam_docs.available() is True
    hub.set_state("kafka", "stopped")
    fam_docs.forget_availability()
    assert fam_docs.available() is False
