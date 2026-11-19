"""Check whether citations and linked source files resolve."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zipfile import ZipFile

MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(<?([^()<>\s]+)>?(?:\s+\"[^\"]*\")?\)")
SOURCE_ROW = re.compile(r"^\s*[-*]\s+\*\*([A-Z]+\d+)\.\*\*")
REFERENCE_GROUP = re.compile(r"\[((?:[A-Z]+\d+)(?:\s*[,;–—-]\s*[A-Z]+\d+)*)\](?!\()")
URL = re.compile(r"https?://[^\s<>\])]+")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _http(url):
    for method in ("HEAD", "GET"):
        try:
            with urlopen(Request(url, method=method, headers={"User-Agent": "HoardHub/1.0"}), timeout=4) as response:
                return {"target": url, "resolved": response.status < 400, "status": response.status,
                        "final_url": response.url}
        except HTTPError as exc:
            if method == "HEAD" and exc.code in (400, 403, 405, 501):
                continue
            return {"target": url, "resolved": False if exc.code in (404, 410) else None, "status": exc.code}
        except OSError as exc:
            return {"target": url, "resolved": None, "error": str(exc)}


def resolve(target, base, remote=True):
    if re.match(r"^[A-Za-z]:[/\\]", target):
        return {"target": target, "resolved": Path(target).exists()}
    parsed = urlsplit(target)
    if parsed.scheme in ("https", "http"):
        return _http(target) if remote else {"target": target, "resolved": None, "reason": "remote disabled"}
    if parsed.scheme not in ("", "file"):
        return {"target": target, "resolved": None, "reason": "unsupported source scheme"}
    path = Path(unquote(parsed.path))
    if parsed.scheme == "file" and path.drive == "" and re.match(r"^/[A-Za-z]:", str(parsed.path)):
        path = Path(unquote(parsed.path[1:]))
    local = (base / path).resolve() if parsed.path else None
    return {"target": target, "resolved": local.exists() if local else None}


def resolve_many(targets, base, remote=True):
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda target: resolve(target, base, remote), dict.fromkeys(targets)))
    return {row["target"]: row for row in rows}


def _markdown(path):
    definitions = {}
    references = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        links = [target for _, target in MARKDOWN_LINK.findall(line)]
        source = SOURCE_ROW.match(line)
        if source:
            definitions[source[1]] = links[0] if links else None
        for group in REFERENCE_GROUP.findall(line):
            for span in re.split(r"\s*[,;]\s*", group):
                ends = re.split(r"\s*[-–—]\s*", span)
                if len(ends) == 2:
                    first, last = [re.fullmatch(r"([A-Z]+)(\d+)", end) for end in ends]
                    if first and last and first[1] == last[1] and 0 <= int(last[2]) - int(first[2]) <= 100:
                        ends = [f"{first[1]}{n}" for n in range(int(first[2]), int(last[2]) + 1)]
                references.extend({"id": key, "line": line_no} for key in ends)
        references.extend({"target": target, "line": line_no} for target in links)
        if not links:
            references.extend({"target": target.rstrip(".,;"), "line": line_no} for target in URL.findall(line))
    return [{**row, "target": row.get("target") or definitions.get(row.get("id"))} for row in references]


def _docx(path):
    rows = []
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        for part in ("document", "footnotes", "endnotes"):
            content, rels = f"word/{part}.xml", f"word/_rels/{part}.xml.rels"
            if content not in names:
                continue
            if rels in names:
                for row in ET.fromstring(archive.read(rels)):
                    if row.get("Type", "").endswith("/hyperlink") and row.get("TargetMode") == "External":
                        rows.append({"part": part, "target": row.get("Target")})
            root = ET.fromstring(archive.read(content))
            for paragraph in root.iter(W + "p"):
                text = "".join(node.text or "" for node in paragraph.iter(W + "t"))
                rows.extend({"part": part, "target": target.rstrip(".,;")} for target in URL.findall(text))
    return rows


def _pdf(path):
    import pymupdf

    with pymupdf.open(path) as doc:
        return [{"page": page.number + 1, "target": item["uri"]}
                for page in doc for item in page.get_links() if item.get("uri")]


def check_document(path, requirements, report):
    reader = {".docx": _docx, ".pdf": _pdf, ".md": _markdown, ".txt": _markdown}.get(path.suffix.lower())
    rows = [{"id": key, "target": target} for key, target in requirements["citations"].items()
            ] if "citations" in requirements else reader(path) if reader else []
    targets = resolve_many([row["target"] for row in rows if row.get("target")],
                           Path(requirements.get("_base", path.parent)),
                           requirements.get("resolve_remote", True))
    citations = [{**row, **targets.get(row.get("target"), {"resolved": False, "reason": "source missing"})}
                 for row in rows]
    report.measures["citations"] = citations
    failed = [row for row in citations if row["resolved"] is False]
    pending = any(row["resolved"] is None for row in citations)
    report.add("citations", False if failed else None if pending or not citations else True,
               "Resolve citation identifiers and their source links.",
               "Resuelve los identificadores de cita y sus enlaces a las fuentes.", details=failed)
