"""hoard_link.docs.sniff on real files generated in tmp_path."""

from __future__ import annotations

import io

import pytest

from hoard_link.docs import sniff as sn
from tests.commons.docs_helpers import make_pdf, make_zip


def _image(fmt, **kw):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (20, 10), (200, 30, 30)).save(buf, fmt, **kw)
    return buf.getvalue()


@pytest.mark.parametrize("fmt,kind,ext", [("PNG", "image", "png"), ("JPEG", "image", "jpg"), ("GIF", "image", "gif"), ("WEBP", "image", "webp"),
                                          ("BMP", "image", "bmp"), ("TIFF", "image", "tif")])
def test_real_images_win_over_a_lying_name(fmt, kind, ext):
    data = _image(fmt)
    for name in (f"photo.{ext}", "photo.pdf", "photo", "photo.txt"):
        r = sn.sniff(name, data)
        assert (r.kind, r.ext) == (kind, ext), name
    assert r.mime == sn.mime_for(ext)


def test_real_pdf():
    r = sn.sniff("whatever.png", make_pdf(["hola"]))
    assert (r.kind, r.mime, r.ext) == ("pdf", "application/pdf", "pdf")


def test_docx_made_by_python_docx():
    import docx

    d = docx.Document()
    d.add_paragraph("hola")
    buf = io.BytesIO()
    d.save(buf)
    for name in ("a.docx", "a.zip", "a", "a.xlsx"):
        assert sn.sniff(name, buf.getvalue()).kind == "docx"
    assert sn.sniff("a", buf.getvalue()).mime.endswith("wordprocessingml.document")


def test_office_epub_odf_and_plain_zip():
    cases = {
        "docx": [("[Content_Types].xml", "x"), ("word/document.xml", "x")],
        "xlsx": [("[Content_Types].xml", "x"), ("xl/workbook.xml", "x")],
        "pptx": [("[Content_Types].xml", "x"), ("ppt/presentation.xml", "x")],
    }
    for kind, files in cases.items():
        assert sn.sniff("x.bin", make_zip(files)).kind == kind
    epub = make_zip([("mimetype", "application/epub+zip"), ("META-INF/container.xml", "x")], stored_first=True)
    assert sn.sniff("book.zip", epub) == sn.Sniffed("epub", "application/epub+zip", "epub")
    for mt, kind in [("text", "odt"), ("spreadsheet", "ods"), ("presentation", "odp")]:
        odf = make_zip([("mimetype", f"application/vnd.oasis.opendocument.{mt}"), ("content.xml", "x")], stored_first=True)
        assert sn.sniff("x", odf).kind == kind
    assert sn.sniff("x.docx", make_zip([("a.txt", "hi")])).kind == "zip"          # named docx, but a plain zip
    assert sn.sniff("x", make_zip([])).kind == "zip"
    assert sn.zip_member_names(make_zip([("a", "1"), ("d/b", "2")])) == ["a", "d/b"]
    assert sn.zip_member_names(b"not a zip") is None


def test_truncated_zip_is_still_recognised_by_its_first_members():
    docx = make_zip([("[Content_Types].xml", "x" * 500), ("word/document.xml", "y" * 500), ("word/styles.xml", "z" * 500)])
    cut = docx[: len(docx) // 2]
    assert sn.sniff("a", cut).kind == "docx"
    epub = make_zip([("mimetype", "application/epub+zip"), ("a", "b" * 100)], stored_first=True)[:70]
    assert sn.sniff("a", epub).kind == "epub"


def test_text_encodings_and_formats():
    assert sn.sniff("a.txt", "Hola ñandú, café".encode()).kind == "text"
    assert sn.sniff("a", "x".encode("utf-16")).kind == "text"
    assert sn.sniff("a", b"\xef\xbb\xbfhola").kind == "text"
    assert sn.sniff("a.txt", "Hola ñandú".encode("latin-1")).kind == "text"                  # not UTF-8, but a text extension
    assert sn.sniff("a", "Hola ñandú".encode("latin-1")).kind == "unknown"
    assert sn.sniff("a", b'{"a": [1, 2]}').kind == "json" and sn.sniff("a", b"[link](x) is markdown").kind == "text"
    assert sn.sniff("a", b"<!DOCTYPE html><html></html>").kind == "html"
    assert sn.sniff("a", b"a,b,c\n1,2,3\n4,5,6\n").kind == "csv" and sn.sniff("a.csv", b"a,b\n1,2").kind == "csv"
    assert sn.sniff("a.md", b"# hi").mime == "text/markdown"
    assert sn.sniff("a", b'<svg xmlns="http://www.w3.org/2000/svg"/>') == sn.Sniffed("image", "image/svg+xml", "svg")
    assert sn.sniff("a", b"Subject: x\n").kind == "text" and sn.sniff("a", b"Received: from x\nSubject: hi\n\nbody").kind == "eml"
    assert sn.sniff("a", b"{\\rtf1 hola}").kind == "rtf"


def test_binary_and_empty():
    assert sn.sniff("a.dat", bytes(range(256))) == sn.Sniffed("unknown", "application/octet-stream", "dat")
    assert sn.sniff("a", b"\x00\x01\x02") == sn.Sniffed("unknown", "application/octet-stream", "bin")
    assert sn.sniff("a.pdf", b"").kind == "pdf" and sn.sniff("a", b"").kind == "unknown"
    assert sn.sniff(None, None).kind == "unknown"
    assert sn.sniff("C:\\Users\\Luis\\Documentos\\a.PDF", b"").ext == "pdf"


def test_media_and_archives():
    assert sn.sniff("a", b"\x00\x00\x00\x18ftypheic" + b"\0" * 20).ext == "heic"
    assert sn.sniff("a", b"\x00\x00\x00\x18ftypmp42" + b"\0" * 20) == sn.Sniffed("video", "video/mp4", "mp4")
    assert sn.sniff("a", b"\x00\x00\x00\x18ftypM4A " + b"\0" * 20).kind == "audio"
    assert sn.sniff("a", b"ID3\x04" + b"\0" * 20).ext == "mp3" and sn.sniff("a", b"fLaC" + b"\0" * 8).ext == "flac"
    assert sn.sniff("a", b"RIFF\0\0\0\0WAVEfmt ").ext == "wav"
    assert sn.sniff("a", b"\x1a\x45\xdf\xa3....webm").ext == "webm"
    assert sn.sniff("a", b"7z\xbc\xaf\x27\x1c\0\x04").kind == "archive" and sn.sniff("a", b"\x1f\x8b\x08\0").ext == "gz"
    assert sn.sniff("a.bin", b"\xff\xfb\x90\x00" + b"\0" * 8).kind == "unknown"          # MP3 frame sync needs an mp3 name
    assert sn.sniff("a.mp3", b"\xff\xfb\x90\x00" + b"\0" * 8).ext == "mp3"
    assert sn.sniff("a", b"SQLite format 3\x00" + b"\0" * 20).kind == "sqlite"


def test_mime_for_and_ext_of():
    assert sn.mime_for("pdf") == sn.mime_for(".PDF") == sn.mime_for("x.y.pdf") == "application/pdf"
    assert sn.mime_for("nope") == sn.mime_for("") == sn.mime_for(None) == "application/octet-stream"
    assert sn.mime_for("constructor") == "application/octet-stream"
    assert [sn.ext_of(n) for n in ("a.tar.gz", "A.JPG", ".bashrc", "noext", "dir.d/file", "a.")] == ["gz", "jpg", "", "", "", ""]


def test_eml_by_extension_needs_mail_headers():
    assert sn.sniff("a.eml", b"From: Ana <a@b.c>\nTo: x@y.z\nSubject: hola\n\ncuerpo").kind == "eml"
    assert sn.sniff("a.txt", b"From: Ana <a@b.c>\nSubject: hola\n\ncuerpo").kind == "text"
    assert sn.sniff("a.eml", b"just some notes").kind == "text"
