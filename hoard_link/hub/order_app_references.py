"""Resolve order sources through the running apps' read-only tools."""

from functools import cache
import re
import time
import unicodedata


_MESSAGES = {
    "resolved": ("Referencia encontrada", "Reference found"),
    "missing": ("Referencia no encontrada", "Reference not found"),
    "ambiguous": ("Hay varias coincidencias para la referencia", "Multiple matches for the reference"),
    "unavailable": ("No se ha podido consultar la aplicación", "The app could not be reached"),
    "unverified": ("No se ha podido verificar la referencia", "The reference could not be verified"),
}
_WRITER_URI = re.compile(r"hoard://writer/(project|codex|writing)/([^/?#]+)")
_WRITER_ID = re.compile(r"(proj|codex|writing)_[^\s]+")
_CHAPTER = re.compile(r"\b(?:cap[ií]tulo|chapter)\s+(\d+)\b", re.IGNORECASE)
_CHARACTER = re.compile(r"\b(?:personaje|character)\b", re.IGNORECASE)
_WRITER_LISTS = {"codex": ("wh_list_codex", "entries"), "writing": ("wh_list_writings", "writings")}


class _Unavailable(Exception):
    """A lookup could not enumerate the source, so absence is unconfirmed."""


def _fold(value):
    return unicodedata.normalize("NFC", str(value or "")).strip().casefold()


def _matches(row, value):
    return value == row.get("id") or _fold(value) == _fold(row.get("title") or row.get("name"))


def _outcome(matches):
    if len(matches) > 1:
        return "ambiguous"
    return "resolved" if matches else "missing"


def _writer_reference(read, obj, place):
    uri = _WRITER_URI.fullmatch(obj)
    kind, name = uri.groups() if uri else ("", obj)
    if ident := _WRITER_ID.fullmatch(name):
        kind = kind or ("project" if ident[1] == "proj" else ident[1])
    by_id = bool(uri or ident)
    chapter_name = _CHAPTER.fullmatch(name) if not kind else None
    if chapter_name:
        kind = "writing"
    projects = read("wh_list_projects", "projects")
    scoped = [p for p in projects if place and _matches(p, place)]
    number = None if scoped else _CHAPTER.search(place)
    chapter = int(number[1]) if number else None
    character = not scoped and bool(_CHARACTER.search(place))
    if not scoped and place.startswith("proj_"):
        return "missing"
    unknown_place = bool(place and not (scoped or number or character))
    kinds = (kind,) if kind else ("project", "codex", "writing")
    matches = 0
    for project in scoped or projects:
        project_id = project["id"]
        for candidate in kinds:
            if character and candidate != "codex":
                continue
            if number and candidate == "project":
                continue
            rows = [project] if candidate == "project" else read(*_WRITER_LISTS[candidate], project_id)
            if chapter_name:
                rows = [row for row in rows if row.get("chapter") == int(chapter_name[1])]
            else:
                rows = [row for row in rows if (name == row["id"] if by_id else _matches(row, name))]
            if character:
                rows = [row for row in rows if row.get("type") == "character"]
            if number and rows:
                chapters = read(*_WRITER_LISTS["writing"], project_id) if candidate == "codex" else rows
                matches += sum(w.get("chapter") == chapter for w in chapters) * (len(rows) if candidate == "codex" else 1)
            else:
                matches += len(rows)
    if matches > 1:
        return "ambiguous"
    if matches:
        return "unverified" if unknown_place else "resolved"
    return "missing"


def _search_reference(hub, app, obj, place):
    search = hub.facet("search")
    if search is None or hub.get(app) is None:
        return "unverified"
    result = search.search(obj, [app], limit=50)
    groups = [g for g in result["groups"] if g["app"] == app]
    matches = {r["id"] or r["url"] for g in groups for r in g["results"]
               if (r["id"] or r["url"]) and _matches(r, obj)}
    if matches:
        return "unverified" if place else _outcome(matches)
    if groups and all(g.get("error") for g in groups):
        return "unavailable"
    if any(s["app"] == app and s["reason"] != "no search tool" for s in result["skipped"]):
        return "unavailable"
    return "unverified"


def resolve(hub, refs):
    """Return a live check for each app reference, without changing the saved source."""
    deadline = time.monotonic() + 6.0

    @cache
    def writer_read(tool, key, project_id=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        args = {"projectId": project_id} if project_id else {}
        result = hub.call_app("writer", tool, args, timeout=min(2.0, remaining))
        body = result.get("result")
        rows = body.get(key) if result.get("ok") and isinstance(body, dict) else None
        if isinstance(rows, list) and all(isinstance(row, dict) and row.get("id") for row in rows):
            return rows
        return None

    def read(*args):
        rows = writer_read(*args)
        if rows is None:
            raise _Unavailable
        return rows

    @cache
    def status(app, obj, place):
        if not obj:
            return "unverified"
        if time.monotonic() >= deadline:
            return "unavailable"
        if app != "writer":
            return _search_reference(hub, app, obj, place)
        try:
            return _writer_reference(read, obj, place)
        except _Unavailable:
            return "unavailable"

    output = []
    for index, ref in enumerate(refs):
        if not (app := ref.get("app")):
            continue
        obj, place = ref.get("object", ""), ref.get("place", "")
        state = status(_fold(app), obj, place)
        es, en = _MESSAGES[state]
        output.append({"index": index, "app": app, "object": obj, "place": place, "status": state, "es": es, "en": en})
    return output
