"""Check whether citations and linked source files resolve."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
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


def worker_scope(args):
    """What a confined worker may have the Hub look at, or ``None`` for a caller that is not confined.

    Exact files (granted revisions and the output it may write) and folders (declared read and write
    paths). Whatever the worker puts in its own requirements never widens it.
    """
    if args.get("_worker_read_files") is None:
        return None
    files = [*args["_worker_read_files"], *([args["_worker_output_path"]] if args.get("_worker_output_path") else [])]
    return {"files": files, "folders": [*(args.get("_worker_read_paths") or []), *(args.get("_worker_write_paths") or [])]}


def _key(path):
    return os.path.normcase(os.path.normpath(str(path)))


def in_scope(path, scope):
    """Whether ``path`` is in ``scope``, judged from the text of the path alone (the disk is not asked)."""
    here = _key(path)
    if here in {_key(item) for item in scope.get("files", ())}:
        return True
    for folder in scope.get("folders", ()):
        try:
            if os.path.commonpath([here, _key(folder)]) == _key(folder):
                return True
        except ValueError:  # another drive
            continue
    return False


def _outside(target):
    return {"target": target, "resolved": None,
            "reason": "outside the declared scope / fuera del ámbito declarado"}


def resolve(target, base, remote=True, scope=None):
    if re.match(r"^[A-Za-z]:[/\\]", target):
        if scope is not None and not in_scope(target, scope):
            return _outside(target)
        return {"target": target, "resolved": Path(target).exists()}
    parsed = urlsplit(target)
    if parsed.scheme in ("https", "http"):
        return _http(target) if remote else {"target": target, "resolved": None, "reason": "remote disabled"}
    if parsed.scheme not in ("", "file"):
        return {"target": target, "resolved": None, "reason": "unsupported source scheme"}
    path = Path(unquote(parsed.path))
    if parsed.scheme == "file" and path.drive == "" and re.match(r"^/[A-Za-z]:", str(parsed.path)):
        path = Path(unquote(parsed.path[1:]))
    if not parsed.path:
        return {"target": target, "resolved": None}
    if scope is not None:
        if not in_scope(base / path, scope):
            return _outside(target)
        local = (base / path).resolve()
        return _outside(target) if not in_scope(local, scope) else {"target": target, "resolved": local.exists()}
    return {"target": target, "resolved": (base / path).resolve().exists()}


def resolve_many(targets, base, remote=True, scope=None):
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda target: resolve(target, base, remote, scope), dict.fromkeys(targets)))
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
                           requirements.get("resolve_remote", True), requirements.get("_scope"))
    citations = [{**row, **targets.get(row.get("target"), {"resolved": False, "reason": "source missing"})}
                 for row in rows]
    report.measures["citations"] = citations
    failed = [row for row in citations if row["resolved"] is False]
    pending = any(row["resolved"] is None for row in citations)
    report.add("citations", False if failed else None if pending or not citations else True,
               "Resolve citation identifiers and their source links.",
               "Resuelve los identificadores de cita y sus enlaces a las fuentes.", details=failed)
