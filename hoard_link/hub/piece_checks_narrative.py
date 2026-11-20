"""Inspect a narrative PDF and the manuscript revision it was laid out from."""

import re
import unicodedata
from pathlib import Path
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from .piece_checks_sources import W


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", value).replace("\u00ad", "").split())


def _manuscript(path):
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".docx":
        try:
            with ZipFile(path) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
        except (BadZipFile, ET.ParseError) as exc:
            raise ValueError("The manuscript is not a readable DOCX.") from exc
        breaks = {W + "tab", W + "br", W + "cr"}
        return "\n".join("".join((node.text or "") if node.tag == W + "t" else " "
                                 for node in paragraph.iter() if node.tag == W + "t" or node.tag in breaks)
                         for paragraph in root.iter(W + "p"))
    raise ValueError("Use a DOCX or UTF-8 plain-text manuscript.")


def _limit(requirements, key, minimum):
    value = requirements.get(key)
    if value is not None and (type(value) is not int or value < minimum):
        raise ValueError(f"{key} must be an integer of at least {minimum}.")
    return value


def check_narrative(path, requirements, report, *, source=None, continuity=None):
    import pymupdf

    min_words = _limit(requirements, "min_words", 0)
    max_words = _limit(requirements, "max_words", 0)
    expected_pages = _limit(requirements, "page_count", 1)
    if min_words is not None and max_words is not None and min_words > max_words:
        raise ValueError("min_words must not exceed max_words.")
    with pymupdf.open(path) as pdf:
        if not pdf.is_pdf or pdf.needs_pass:
            raise ValueError("Use a readable, unencrypted PDF.")
        fonts = {}
        pages = []
        for page in pdf:
            page_text = page.get_text(flags=pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_DEHYPHENATE)
            pages.append(page_text)
            for xref, _, _, name, *_ in page.get_fonts():
                if xref not in fonts:
                    content = pdf.extract_font(xref)[-1] if xref else b""
                    fonts[xref] = {"name": name, "embedded": bool(content)}

    text = _text("\n".join(pages))
    words = len(re.findall(r"[^\W_]+(?:[’'\-][^\W_]+)*", text))
    empty_pages = [number for number, page in enumerate(pages, 1) if not page.strip()]
    unembedded = [font["name"] for font in fonts.values() if not font["embedded"]]
    report.measures.update(words=words, pages=len(pages), empty_pages=empty_pages, fonts=list(fonts.values()),
                           engine=f"PyMuPDF {pymupdf.VersionBind}")
    report.add("word-count", words > 0 and (min_words is None or words >= min_words)
               and (max_words is None or words <= max_words),
               "Count narrative words and check the requested length.", "Cuenta las palabras y comprueba la extensión pedida.",
               measured=words, expected={"min_words": min_words, "max_words": max_words})
    report.add("pagination", bool(pages) and not empty_pages and (expected_pages is None or len(pages) == expected_pages),
               "Count pages and locate pages without extractable text.", "Cuenta las páginas y localiza las que no tienen texto extraíble.",
               measured=len(pages), expected=expected_pages, details=empty_pages)
    report.add("embedded-fonts", bool(fonts) and not unembedded, "Check that PDF fonts are embedded.",
               "Comprueba que las fuentes del PDF estén incrustadas.", details=unembedded)

    if source is None and requirements.get("source_path"):
        source = Path(requirements["source_path"]).expanduser().resolve(strict=True)
    expected = _text(_manuscript(source)) if source else None
    mismatch = []
    if expected is not None and text != expected:
        offset = next((i for i, (actual, wanted) in enumerate(zip(text, expected)) if actual != wanted),
                      min(len(text), len(expected)))
        mismatch = [{"offset": offset, "pdf": text[max(0, offset - 20):offset + 40],
                     "source": expected[max(0, offset - 20):offset + 40]}]
    report.add("source-text", None if expected is None else bool(expected) and not mismatch,
               "Compare the full text in order with the reviewed manuscript.",
               "Coteja el texto completo en orden con el manuscrito revisado.", details=mismatch)
    _continuity(requirements, continuity, report)


def _continuity(requirements, continuity, report):
    declarations = requirements.get("continuity_declarations", {})
    if not isinstance(declarations, dict) or any(not isinstance(note, str) or not note.strip()
                                                 for note in declarations.values()):
        raise ValueError("continuity_declarations must map defect IDs to nonempty notes.")
    if continuity is None:
        report.add("continuity", None, "Link a reviewed manuscript to check its continuity defects.",
                   "Vincula un manuscrito revisado para comprobar sus defectos de continuidad.")
        return
    findings = []
    for finding in continuity:
        note = declarations.get(str(finding["id"]))
        findings.append({**finding, "disposition": "resolved" if finding["resolved"] else "declared" if note else "open",
                         **({"declaration": note} if note else {})})
    unknown = sorted(declarations.keys() - {str(finding["id"]) for finding in findings})
    pending = [finding for finding in findings if finding["disposition"] == "open"]
    report.measures["continuity"] = findings
    report.add("continuity", not pending and not unknown,
               "Check that the manuscript's continuity defects are resolved in this revision or declared.",
               "Comprueba que los defectos de continuidad estén resueltos en esta revisión o declarados.",
               details=[*pending, *({"unknown_defect_id": key} for key in unknown)])


def registered_source(agora, pdf):
    """Resolve the material review and its writing revisions within this order."""
    from .agora_orders import _parents, _piece_row, piece_file

    parents = [_piece_row(agora, parent["piece"]) for parent in _parents(pdf["derives_from"])
               if parent["role"] == "material"]
    reviewed = [row for row in parents if row["kind"] == "reviewed-manuscript"]
    if len(reviewed) != 1:
        return None, None, None
    review = reviewed[0]
    pending = [review]
    active = {}
    while pending:
        row = pending.pop()
        if row["id"] in active:
            continue
        active[row["id"]] = row
        for parent in _parents(row["derives_from"]):
            if parent["role"] == "material":
                item = _piece_row(agora, parent["piece"])
                if item["kind"] in {"manuscript", "reviewed-manuscript"}:
                    pending.append(item)
    source = Path(piece_file(agora, review["id"])) if review["path"] else None
    identity = {key: review[key] for key in ("id", "revision", "sha256")}
    if not any(row["kind"] == "manuscript" for row in active.values()):
        return source, None, identity
    pending = list(active.values())
    inspected = set()
    while pending:
        row = pending.pop()
        if row["id"] in inspected:
            continue
        inspected.add(row["id"])
        if row["supersedes"]:
            older = _piece_row(agora, row["supersedes"])
            if older["kind"] == row["kind"]:
                pending.append(older)
    defects = agora.db.query("SELECT id, piece_id, note, status, closed_in FROM agora_piece_defects "
                             "WHERE order_id=? ORDER BY id", (pdf["order_id"],))
    findings = [{"id": defect["id"], "piece_id": defect["piece_id"], "note": defect["note"], "closed_in": defect["closed_in"],
                 "resolved": defect["status"] == "closed" and defect["closed_in"] in active}
                for defect in defects if defect["piece_id"] in inspected]
    return source, findings, identity
