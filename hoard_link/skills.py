"""The skills an app carries for the agents that connect to it.

Faustus keeps a library of procedures (``skills/library/<slug>/SKILL.md``: how to do a kind of work well, kept up to date as
the family learns). An app says which of them matter to what it does - by slug, by tag, by category - in the ``skills`` block
of its ``faustus-plugin.json``, the person adds or takes away from that on the app's skills page, and the shelf copies the
result into the app's own data folder. Every agent that connects then has them: they are named in the instructions it reads
on connecting, listed and read with the ``skills_list`` / ``skill_read`` tools, and offered as prompts by the MCP bridge. The
copy is what is served, so the skills are there when Faustus is closed; the shelf looks at the library again when its copy is
older than ``max_age_s`` and takes whatever changed.

    shelf = Shelf("phidias", data_dir, {"slugs": ["blender-asset-production"], "tags": ["3d", "blender"]})
    router = make_agent_router(..., skills=shelf)          # or nothing at all: a manifest with a skills block is enough

Nothing here raises towards the app: a missing library, an unreadable file or a full disk leave the shelf with what it had.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

log = logging.getLogger("hoard_link.skills")

TOOL_NAMES = ("skills_list", "skill_read", "skills_refresh", "skills_library", "skills_assign")
MAX_FILE_BYTES = 400_000
CATEGORIES = ("engineering", "testing", "security", "research", "planning", "writing")
SECTIONS = ("When to Use", "Procedure", "Pitfalls", "Verification")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".sh", ".ps1", ".toml", ".csv"}


def parse(text: str) -> tuple[dict[str, Any], str]:
    """A SKILL.md as (front matter, body). The front matter is the block between the two ``---`` lines: ``key: value``
    lines, with ``[a, b]`` read as a list. A file without one is all body."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or key.startswith((" ", "\t", "#")):
            continue
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = value.strip("'\"")
    return meta, text[end + 4:].lstrip("\n")


def _words(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip().lower() for v in (value or []) if str(v).strip()]


def find_library(setting: Any = None) -> Optional[Path]:
    """The folder of Faustus's skill library, or None. ``setting`` is where the app was told the library (or Faustus) is."""
    try:
        told = setting() if callable(setting) else setting
    except Exception:  # noqa: BLE001
        told = None
    if told:
        path = Path(str(told)).expanduser()
        for candidate in (path, path / "skills" / "library"):
            if candidate.is_dir() and any(candidate.glob("*/SKILL.md")):
                return candidate
    try:
        from . import fam_mail
        root = fam_mail.faustus_dir(told)
    except Exception:  # noqa: BLE001
        root = None
    if root is not None and (Path(root) / "skills" / "library").is_dir():
        return Path(root) / "skills" / "library"
    return None


def library_rows(library: Path) -> list[dict[str, Any]]:
    """Every published skill of the library: ``{slug, name, description, version, category, tags, apps, text}``."""
    rows = []
    for path in sorted(Path(library).glob("*/SKILL.md")):
        slug = path.parent.name
        if not _SLUG.match(slug):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse(text)
        if str(meta.get("status") or "published").lower() not in ("published", ""):
            continue
        rows.append({"slug": slug, "name": str(meta.get("name") or slug), "description": str(meta.get("description") or ""),
                     "version": str(meta.get("version") or ""), "category": str(meta.get("category") or ""),
                     "tags": _words(meta.get("tags")), "apps": _words(meta.get("apps")), "text": text})
    return rows


def select(app: str, wants: Mapping[str, Any], choices: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows an app carries, in order, each with ``why``: ``manifest`` (named in its manifest), ``added`` (the person
    added it), ``tags`` (it shares tags or a category with what the manifest asks for). What the person took away is out."""
    slugs = _words(wants.get("slugs"))
    tags, cats = set(_words(wants.get("tags"))), set(_words(wants.get("categories")))
    added, removed = _words(choices.get("add")), set(_words(choices.get("remove")))
    limit = int(wants.get("limit") or 12)
    order = [s for s in slugs if s not in removed] + [s for s in added if s not in slugs and s not in removed]
    by_slug = {r["slug"]: r for r in rows}
    named = [{**by_slug[s], "why": "manifest" if s in slugs else "added"} for s in order if s in by_slug]
    scored = []
    for row in rows:
        if row["slug"] in order or row["slug"] in removed:
            continue
        if row["apps"] and str(app).lower() not in row["apps"]:
            continue                                       # a procedure written for other apps' tools: only by name
        shared = len(tags & set(row["tags"]))
        if shared or (row["category"].lower() in cats):
            scored.append((-shared, row["slug"], row))
    room = max(0, limit - len([r for r in named if r["why"] == "manifest"]))
    return named + [{**row, "why": "tags"} for _, _, row in sorted(scored, key=lambda r: (r[0], r[1]))[:room]]


def write_skill(library: Path, *, slug: str, description: str, when: str, procedure: str, pitfalls: str, verification: str,
                tags: Any = (), category: str = "engineering", apps: Any = (), replace: bool = False) -> dict[str, Any]:
    """A new skill in the library, in the shape the library keeps (front matter and the four sections). ValueError says
    what is missing."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(slug or "").strip().lower()).strip("-")
    if not _SLUG.match(slug or "-"):
        raise ValueError("The skill needs a name: lower-case words joined by hyphens.")
    description = " ".join(str(description or "").split())
    if len(description) < 30 or "use when" not in description.lower():
        raise ValueError('The description says what the skill does and then when to reach for it: "... Use when ...".')
    parts = {"When to Use": when, "Procedure": procedure, "Pitfalls": pitfalls, "Verification": verification}
    empty = [name for name, text in parts.items() if not str(text or "").strip()]
    if empty:
        raise ValueError("Missing: " + ", ".join(empty) + ".")
    if category not in CATEGORIES:
        raise ValueError("The category is one of: " + ", ".join(CATEGORIES) + ".")
    target = Path(library) / slug
    if (target / "SKILL.md").exists() and not replace:
        raise ValueError(f"The library already has a skill called {slug}.")
    head = [f"name: {slug}", f"description: {description}", "version: 1.0.0", f"category: {category}",
            "tags: [" + ", ".join(_words(tags)) + "]"]
    if _words(apps):
        head.append("apps: [" + ", ".join(_words(apps)) + "]")
    head += ["status: published", "source: imported"]
    body = "\n\n".join(f"## {name}\n\n{str(text).strip()}" for name, text in parts.items())
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text("---\n" + "\n".join(head) + "\n---\n\n" + body + "\n", encoding="utf-8", newline="\n")
    return {"slug": slug, "path": str(target / "SKILL.md"), "words": len(body.split())}


class Shelf:
    """The skills of one app. ``wants`` = ``{"slugs": [...], "tags": [...], "categories": [...], "limit": 12}``: the slugs
    are taken as they are; the rest of the room goes to the library's skills that share most tags (or a category) with it.
    A skill whose front matter says ``apps: [a, b]`` drives those apps' own tools: the others only get it by naming it.
    ``<data>/skills/choices.json`` holds what the person added and took away."""

    def __init__(self, app: str, data_dir: Union[str, Path, Callable[[], Any]], wants: Optional[Mapping[str, Any]] = None, *,
                 library: Any = None, max_age_s: float = 900.0) -> None:
        self.app = app
        self._data_dir = data_dir
        self.wants = dict(wants or {})
        self.library = library
        self.max_age_s = max_age_s
        self._lock = threading.Lock()
        self._checked = 0.0

    # ----- where things are
    @property
    def folder(self) -> Path:
        base = self._data_dir() if callable(self._data_dir) else self._data_dir
        return Path(str(base)) / "skills"

    def _index(self) -> dict[str, Any]:
        try:
            data = json.loads((self.folder / "index.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("skills"), list) else {"skills": []}
        except (OSError, ValueError):
            return {"skills": []}

    def choices(self) -> dict[str, list[str]]:
        try:
            data = json.loads((self.folder / "choices.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"add": _words(data.get("add")) if isinstance(data, dict) else [], "remove": _words(data.get("remove")) if isinstance(data, dict) else []}

    def assign(self, slug: str, on: bool = True) -> dict[str, Any]:
        """Put a library skill on this app's shelf, or take one off it, and bring the copies up to date."""
        slug = str(slug or "").strip().lower()
        if not _SLUG.match(slug or "-"):
            raise ValueError("Which skill? Give its slug as the library lists it.")
        library = find_library(self.library)
        if on and library is not None and not (library / slug / "SKILL.md").is_file():
            raise KeyError(f"The library has no skill called {slug!r}. skills_library lists the ones there are.")
        now = self.choices()
        add, remove = [s for s in now["add"] if s != slug], [s for s in now["remove"] if s != slug]
        named = slug in _words(self.wants.get("slugs"))
        if on and not named:
            add.append(slug)
        if not on:
            remove.append(slug)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.folder / "choices.json.tmp"
            tmp.write_text(json.dumps({"add": add, "remove": remove}, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.folder / "choices.json")
        except OSError as error:
            raise ValueError(f"The choice could not be saved ({error}).") from error
        return {"slug": slug, "on": bool(on), **self.refresh(force=True), "shelf": self.list()}

    # ----- copying
    def refresh(self, force: bool = False) -> dict[str, Any]:
        """Look at the library and bring the shelf up to date. Returns what happened:
        ``{"source": "library"|"copy", "library": path|None, "skills": n, "added": [...], "updated": [...], "removed": [...]}``."""
        with self._lock:
            if not force and time.monotonic() - self._checked < self.max_age_s and (self.folder / "index.json").is_file():
                return {"source": "copy", "library": None, "skills": len(self._index()["skills"]), "added": [], "updated": [], "removed": []}
            self._checked = time.monotonic()
            before = {s["slug"]: s for s in self._index()["skills"] if isinstance(s, dict) and s.get("slug")}
            library = find_library(self.library)
            if library is None:
                return {"source": "copy", "library": None, "skills": len(before), "added": [], "updated": [], "removed": []}
            report: dict[str, Any] = {"source": "library", "library": str(library), "added": [], "updated": [], "removed": []}
            rows = []
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                for row in select(self.app, self.wants, self.choices(), library_rows(library)):
                    slug, text = row["slug"], row["text"]
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                    old = before.get(slug)
                    if old is None or old.get("digest") != digest or not (self.folder / slug / "SKILL.md").is_file():
                        files = self._copy(library / slug, self.folder / slug, text)
                        report["added" if old is None else "updated"].append(slug)
                    else:
                        files = old.get("files") or []
                    rows.append({**{k: row[k] for k in ("slug", "name", "description", "version", "category", "tags", "why")},
                                 "digest": digest, "files": files})
                kept = {r["slug"] for r in rows}
                for slug in before:
                    if slug not in kept and _SLUG.match(slug):
                        shutil.rmtree(self.folder / slug, ignore_errors=True)
                        report["removed"].append(slug)
                tmp = self.folder / "index.json.tmp"
                tmp.write_text(json.dumps({"app": self.app, "refreshed_at": time.time(), "library": str(library), "skills": rows},
                                          ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self.folder / "index.json")
            except OSError as error:
                log.warning("could not refresh the skills of %s: %s", self.app, error)
                return {"source": "copy", "library": str(library), "skills": len(before), "added": [], "updated": [], "removed": []}
            report["skills"] = len(rows)
            return report

    @staticmethod
    def _copy(source: Path, target: Path, text: str) -> list[str]:
        """The skill's SKILL.md and the text files beside it (its references), copied. Returns their names."""
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(text, encoding="utf-8")
        names = []
        for path in sorted(source.rglob("*")):
            rel = path.relative_to(source).as_posix()
            if not path.is_file() or rel == "SKILL.md" or path.suffix.lower() not in _TEXT_SUFFIXES or len(names) >= 40:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                out = target / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, out)
                names.append(rel)
            except OSError:
                continue
        return names

    # ----- reading
    def refresh_soon(self) -> None:
        """Bring the shelf up to date in the background when its copy is old (never blocks, never raises)."""
        if time.monotonic() - self._checked < self.max_age_s or getattr(self, "_busy", False):
            return
        self._busy = True

        def go() -> None:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                log.exception("skills refresh failed")
            finally:
                self._busy = False

        threading.Thread(target=go, daemon=True, name=f"skills-{self.app}").start()

    def list(self, refresh: bool = True) -> list[dict[str, Any]]:
        """The skills on the shelf: ``{slug, name, description, version, category, tags, why, files}``. ``refresh=False``
        only reads the copy: nothing is looked up or written (what listing an app's tools does)."""
        if refresh:
            self.refresh()
        keys = ("slug", "name", "description", "version", "category", "tags", "why", "files")
        return [{k: s.get(k) for k in keys} for s in self._index()["skills"] if isinstance(s, dict)]

    def read(self, slug: str, file: str = "") -> dict[str, Any]:
        """One skill's procedure (or one of its reference files), as text."""
        slug = str(slug or "").strip().lower().removeprefix("skill-")
        row = next((s for s in self.list() if s["slug"] == slug or str(s["name"]).lower() == slug), None)
        if row is None:
            raise KeyError(f"No skill called {slug!r} on this app's shelf. skills_list names the ones there are; skills_assign brings one from the library.")
        rel = str(file or "SKILL.md").replace("\\", "/")
        if rel != "SKILL.md" and rel not in (row.get("files") or []):
            raise KeyError(f"The skill {row['slug']} has no file {rel!r}. Its files: {', '.join(row.get('files') or []) or 'none'}.")
        try:
            text = (self.folder / row["slug"] / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise KeyError(f"The copy of {row['slug']}/{rel} cannot be read ({error}). skills_refresh brings it back.") from error
        return {"slug": row["slug"], "name": row["name"], "version": row["version"], "file": rel, "files": row.get("files") or [], "text": text}

    def catalogue(self, query: str = "", limit: int = 400) -> list[dict[str, Any]]:
        """The library as this app sees it: every skill with ``on`` (it is on the shelf), ``why`` and ``other_apps`` (it was
        written for other apps' tools). ``query`` keeps the ones whose slug, description or tags have every word of it."""
        library = find_library(self.library)
        if library is None:
            return []
        on = {s["slug"]: s.get("why") or "" for s in self.list()}
        removed = set(self.choices()["remove"])
        words = [w for w in str(query or "").lower().split() if w]
        out = []
        for row in library_rows(library):
            hay = " ".join([row["slug"], row["description"], " ".join(row["tags"]), row["category"]]).lower()
            if any(w not in hay for w in words):
                continue
            out.append({**{k: row[k] for k in ("slug", "name", "description", "version", "category", "tags")}, "on": row["slug"] in on,
                        "why": on.get(row["slug"], ""), "removed": row["slug"] in removed,
                        "other_apps": bool(row["apps"]) and self.app.lower() not in row["apps"]})
        return out[:max(1, int(limit))]

    def status(self) -> dict[str, Any]:
        index = self._index()
        library = find_library(self.library)
        return {"app": self.app, "skills": len(index["skills"]), "refreshed_at": index.get("refreshed_at"),
                "library": str(library) if library else index.get("library"), "library_found": library is not None,
                "wants": {k: self.wants.get(k) for k in ("slugs", "tags", "categories", "limit") if self.wants.get(k)}, "choices": self.choices()}

    # ----- what an agent sees
    def instructions(self, rows: Optional[list[dict[str, Any]]] = None) -> str:
        rows = self.list() if rows is None else rows
        if not rows:
            return ""
        lines = ["Skills. This app carries procedures from the family's skill library for the work it does. Before a task one of "
                 "them covers, read it with skill_read and follow it; skills_list gives them again, and skills_library looks in the "
                 "whole library for one that is not here yet."]
        lines += [f"- {r['slug']}: {r['description']}" for r in rows]
        return "\n".join(lines)

    def catalog(self) -> list[dict[str, Any]]:
        """The tools, as ``GET /api/agent/tools`` entries."""
        read_only = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
        change = {"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False, "openWorldHint": False}
        return [
            {"name": "skills_list", "annotations": dict(read_only), "inputSchema": {"type": "object", "properties": {}},
             "description": "The skills this app carries: procedures from the family's skill library for the kind of work done here "
                            "(slug, what each is for and when to use it, version, reference files). Read the one that fits with skill_read "
                            "before starting."},
            {"name": "skill_read", "annotations": dict(read_only),
             "inputSchema": {"type": "object", "required": ["slug"], "properties": {
                 "slug": {"type": "string", "description": "The skill's slug, from skills_list."},
                 "file": {"type": "string", "description": "One of the skill's reference files (from its `files`); empty = the procedure itself."}}},
             "description": "The full text of one of this app's skills: when to use it, the procedure step by step, its pitfalls and how to "
                            "verify the result. Follow it while doing that work with this app's tools."},
            {"name": "skills_refresh", "annotations": dict(change), "inputSchema": {"type": "object", "properties": {}},
             "description": "Look at the family's skill library again and bring this app's copies up to date (added, updated, removed). "
                            "The app does it by itself every few minutes; use it right after a skill was improved."},
            {"name": "skills_library", "annotations": dict(read_only),
             "inputSchema": {"type": "object", "properties": {
                 "query": {"type": "string", "description": "Words to look for in the slug, the description and the tags (all must be there). Empty = everything."},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 400, "default": 40}}},
             "description": "Search the whole skill library of the family, not only what this app carries: each skill with whether it is on "
                            "this app's shelf (`on`) and whether it was written for other apps' tools (`other_apps`). Use it when no skill "
                            "here covers the task; skills_assign then brings the one that does."},
            {"name": "skills_assign", "annotations": dict(change),
             "inputSchema": {"type": "object", "required": ["slug"], "properties": {
                 "slug": {"type": "string", "description": "The library skill's slug, from skills_library."},
                 "on": {"type": "boolean", "default": True, "description": "true puts it on this app's shelf; false takes it off."}}},
             "description": "Put a skill of the library on this app's shelf (it stays for every agent that connects from now on) or take one "
                            "off. The person sees and changes the same list on the app's skills page."},
        ]

    def call(self, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Any:
        args = dict(arguments or {})
        if name == "skills_list":
            return {**self.status(), "skills": self.list()}
        if name == "skill_read":
            return self.read(str(args.get("slug") or ""), str(args.get("file") or ""))
        if name == "skills_refresh":
            return {**self.refresh(force=True), "shelf": self.list()}
        if name == "skills_library":
            rows = self.catalogue(str(args.get("query") or ""), int(args.get("limit") or 40))
            return {"query": str(args.get("query") or ""), "count": len(rows), "skills": rows}
        if name == "skills_assign":
            return self.assign(str(args.get("slug") or ""), args.get("on") is not False)
        raise KeyError(name)

    def state(self, query: str = "") -> dict[str, Any]:
        """Everything the skills page shows."""
        return {**self.status(), "shelf": self.list(), "library_skills": self.catalogue(query), "categories": list(CATEGORIES)}

    def create(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Write a new skill into the library (see :func:`write_skill`) and, unless ``assign`` is false, carry it here."""
        library = find_library(self.library)
        if library is None:
            raise ValueError("The skill library was not found on this computer, so there is nowhere to write the skill.")
        made = write_skill(library, slug=str(fields.get("slug") or fields.get("name") or ""), description=str(fields.get("description") or ""),
                           when=str(fields.get("when") or ""), procedure=str(fields.get("procedure") or ""), pitfalls=str(fields.get("pitfalls") or ""),
                           verification=str(fields.get("verification") or ""), tags=fields.get("tags") or [],
                           category=str(fields.get("category") or "engineering"), apps=[self.app] if fields.get("only_this_app") else [])
        if fields.get("assign") is not False:
            self.assign(made["slug"], True)
        return {**made, "shelf": self.list()}


def wants_of(manifest: Union[str, Path, Mapping[str, Any], None]) -> dict[str, Any]:
    """The ``skills`` block of an app's ``faustus-plugin.json`` (a path or the parsed manifest), or ``{}``."""
    if manifest is None:
        return {}
    if not isinstance(manifest, Mapping):
        try:
            manifest = json.loads(Path(str(manifest)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    block = manifest.get("skills") if isinstance(manifest, Mapping) else None
    return dict(block) if isinstance(block, Mapping) else {}


def own_manifest() -> Optional[Path]:
    """The ``faustus-plugin.json`` of the app this copy of the package belongs to."""
    for parent in list(Path(__file__).resolve().parents)[1:4]:
        if (parent / "faustus-plugin.json").is_file():
            return parent / "faustus-plugin.json"
    return None


def auto(app: str, data_dir: Any = None) -> Optional[Shelf]:
    """The shelf of the app this copy of the package belongs to, with nothing to set up: what the ``skills`` block of its
    manifest asks for plus what the person chose. None when there is no manifest, under pytest (pass ``skills=`` to test it)
    and with ``HOARD_SKILLS=0``. The copies go to ``data_dir`` when it is a plain path, else next to the app's token (``family.configure``), else to the
    ``data`` folder beside the manifest."""
    if "pytest" in sys.modules or os.environ.get("HOARD_SKILLS", "1").strip().lower() in ("0", "false", "no", "off"):
        return None                                        # an app's own tests see the app, not this computer's library
    manifest = own_manifest()
    if manifest is None:
        return None
    wants = wants_of(manifest)

    def where() -> Any:
        if data_dir is not None and not callable(data_dir):
            return data_dir
        try:
            from . import family
            token_file = family.status().get("token_file")
        except Exception:  # noqa: BLE001
            token_file = ""
        return Path(token_file).parent if token_file else manifest.parent / "data"

    return Shelf(app, where, wants)


_BRIDGE_TOOLS = ("skills_list", "skill_read", "skills_library", "skills_assign")


def attach(server: Any, app: str, root: Any = None, data_dir: Any = None) -> Optional[Shelf]:
    """Give an MCP server an app wrote by hand (``mcp`` 1.x ``Server`` / ``FastMCP``, 2.x ``Server`` / ``MCPServer``) the app's
    shelf: the skills are named at the end of its instructions, listed and read with ``skills_list`` / ``skill_read`` /
    ``skills_library`` / ``skills_assign`` (answered here) and offered as prompts called ``skill-<slug>``. One line anywhere
    after the server object exists: ``skills.attach(server, "myapp", root=__file__)``. ``root`` is the file or folder the
    manifest is beside (or up to two folders above); the copies are read from ``data_dir``, else ``$<APP>_DATA_DIR``, else the
    ``data`` folder beside the manifest. It only reads the copy (the hub or the app keeps it). Returns the shelf, or None when
    there is nothing to hand out (no manifest, an empty shelf, under pytest, ``HOARD_SKILLS=0``). Never raises."""
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("HOARD_SKILLS", "1").strip().lower() in ("0", "false", "no", "off"):
        return None                                        # an app's own tests see the app, not this computer's library
    try:
        return _attach(server, app, root, data_dir)
    except Exception as error:  # noqa: BLE001 - a bridge without skills is still a bridge
        log.debug("skills not attached to %s's MCP server: %s", app, error)
        return None


def _attach(server: Any, app: str, root: Any, data_dir: Any) -> Optional[Shelf]:
    here = Path(root).resolve() if root else Path.cwd()
    if here.is_file() or here.suffix:
        here = here.parent
    manifest = next((p / "faustus-plugin.json" for p in (here, *list(here.parents)[:2]) if (p / "faustus-plugin.json").is_file()), None)
    if manifest is None:
        return None
    where = data_dir or os.environ.get(f"{re.sub(r'[^A-Za-z0-9]+', '_', app).upper()}_DATA_DIR") or manifest.parent / "data"
    shelf = Shelf(app, where, wants_of(manifest), max_age_s=float("inf"))
    shelf._checked = time.monotonic()                      # never looks the library up from a bridge
    if not shelf.list(refresh=False):
        return None

    import asyncio

    from mcp import types

    low = getattr(server, "_mcp_server", None) or getattr(server, "_lowlevel_server", None) or server
    state = {"local": True}

    def extra_tools() -> list[Any]:
        return [types.Tool.model_validate({"name": e["name"], "description": e["description"], "inputSchema": e["inputSchema"], "annotations": e.get("annotations") or {}})
                for e in shelf.catalog() if e["name"] in _BRIDGE_TOOLS]

    def with_skills(tools: list[Any]) -> list[Any]:
        state["local"] = not any(getattr(t, "name", "") == "skills_list" for t in tools)       # an app that hands them out itself is left alone
        return [*tools, *extra_tools()] if state["local"] and shelf.list(refresh=False) else tools

    def mine(name: Any) -> bool:
        return state["local"] and name in _BRIDGE_TOOLS

    async def answer(name: str, arguments: Any) -> Any:
        try:
            body, failed = await asyncio.to_thread(shelf.call, name, dict(arguments or {})), False
        except (KeyError, ValueError) as error:
            body, failed = {"error": str(error.args[0]) if error.args else str(error), "code": "not_found" if isinstance(error, KeyError) else "invalid"}, True
        return types.CallToolResult.model_validate({"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": failed})

    def prompts() -> list[Any]:
        return [types.Prompt.model_validate({"name": f"skill-{s['slug']}", "description": str(s.get("description") or "")[:900]}) for s in shelf.list(refresh=False)]

    def prompt(name: Any) -> Any:
        row = shelf.read(str(name))
        text = str(row["text"])
        if text.startswith("---") and "\n---" in text[3:]:
            text = text[text.index("\n---", 3) + 4:].lstrip("\n")                                # the front matter is for the library
        return types.GetPromptResult.model_validate({"description": str(row.get("name") or name), "messages": [
            {"role": "user", "content": {"type": "text", "text": f"Follow this procedure with the tools of {app}.\n\n{text}"}}]})

    def is_skill(name: Any) -> bool:
        return str(name or "").startswith("skill-")

    class Lazy(dict):  # type: ignore[type-arg]
        """The server's handler table: what the app registers (now or later) is looked up through ``wrap``."""

        def __init__(self, base: Any, wrap: Callable[[Any, Any], Any], added: tuple[Any, ...]) -> None:
            super().__init__(base)
            self._wrap, self._added = wrap, added

        def __contains__(self, key: Any) -> bool:
            return key in self._added or dict.__contains__(self, key)

        def __getitem__(self, key: Any) -> Any:
            found = self._wrap(key, dict.get(self, key))
            if found is None:
                raise KeyError(key)
            return found

        def get(self, key: Any, default: Any = None) -> Any:
            found = self._wrap(key, dict.get(self, key))
            return default if found is None else found

    if isinstance(getattr(low, "request_handlers", None), dict):                                  # mcp 1.x: handlers by request class
        def wrap1(key: Any, handler: Any) -> Any:
            if key is types.ListToolsRequest and handler is not None:
                async def list_tools(request: Any) -> Any:
                    result = await handler(request)
                    inner = getattr(result, "root", result)
                    if getattr(inner, "nextCursor", None):
                        return result
                    return types.ServerResult(types.ListToolsResult(tools=with_skills(list(inner.tools))))
                return list_tools
            if key is types.CallToolRequest and handler is not None:
                async def call_tool(request: Any) -> Any:
                    if mine(request.params.name):
                        return types.ServerResult(await answer(request.params.name, request.params.arguments))
                    return await handler(request)
                return call_tool
            if key is types.ListPromptsRequest:
                async def list_prompts(request: Any) -> Any:
                    inner = getattr(await handler(request), "root", None) if handler is not None else None
                    return types.ServerResult(types.ListPromptsResult(prompts=[*(inner.prompts if inner is not None else []), *prompts()]))
                return list_prompts
            if key is types.GetPromptRequest:
                async def get_prompt(request: Any) -> Any:
                    if handler is not None and not is_skill(request.params.name):
                        return await handler(request)
                    return types.ServerResult(prompt(request.params.name))
                return get_prompt
            return handler

        low.request_handlers = Lazy(low.request_handlers, wrap1, (types.ListPromptsRequest, types.GetPromptRequest))
    elif isinstance(getattr(low, "_request_handlers", None), dict):                               # mcp 2.x: entries by method name
        table = low._request_handlers
        entry_type = type(next(iter(table.values())))

        def entry(params_type: Any, handler: Any) -> Any:
            return entry_type(params_type, handler)

        def wrap2(key: Any, found: Any) -> Any:
            handler = getattr(found, "handler", None)
            if key == "tools/list" and found is not None:
                async def list_tools(ctx: Any, params: Any) -> Any:
                    result = await handler(ctx, params)
                    if getattr(result, "next_cursor", None):
                        return result
                    return result.model_copy(update={"tools": with_skills(list(result.tools))})
                return entry(found.params_type, list_tools)
            if key == "tools/call" and found is not None:
                async def call_tool(ctx: Any, params: Any) -> Any:
                    if mine(params.name):
                        return await answer(params.name, params.arguments)
                    return await handler(ctx, params)
                return entry(found.params_type, call_tool)
            if key == "prompts/list":
                async def list_prompts(ctx: Any, params: Any) -> Any:
                    inner = await handler(ctx, params) if handler is not None else None
                    return types.ListPromptsResult(prompts=[*(inner.prompts if inner is not None else []), *prompts()])
                return entry(found.params_type if found is not None else types.PaginatedRequestParams, list_prompts)
            if key == "prompts/get":
                async def get_prompt(ctx: Any, params: Any) -> Any:
                    if handler is not None and not is_skill(params.name):
                        return await handler(ctx, params)
                    return prompt(params.name)
                return entry(found.params_type if found is not None else types.GetPromptRequestParams, get_prompt)
            return found

        low._request_handlers = Lazy(table, wrap2, ("prompts/list", "prompts/get"))
    else:
        return None
    told = shelf.instructions(shelf.list(refresh=False))
    if told and "Skills." not in str(getattr(low, "instructions", "") or ""):
        low.instructions = "\n\n".join(x for x in (getattr(low, "instructions", None), told) if x)
    return shelf


def mount(router: Any, shelf: Shelf, prefix: str = "/skills", title: str = "") -> Any:
    """The skills page of an app and what it calls, added to ``router``: ``GET {prefix}`` (state; ``?q=`` filters the
    library), ``GET {prefix}/page`` (the page), ``GET {prefix}/read/{slug}``, ``POST {prefix}/assign {slug, on}``,
    ``POST {prefix}/refresh`` and ``POST {prefix}/create``. ``make_agent_router`` mounts it under ``/api/agent``."""
    from fastapi import Body
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import HTMLResponse, JSONResponse

    def fail(error: Exception) -> Any:
        status = 404 if isinstance(error, KeyError) else 400
        return JSONResponse({"error": str(error.args[0]) if error.args else str(error), "code": "not_found" if status == 404 else "invalid"}, status_code=status)

    @router.get(prefix)
    async def skills_state(q: str = "") -> Any:
        return await run_in_threadpool(shelf.state, q)

    @router.get(prefix + "/page", include_in_schema=False)
    async def skills_page() -> Any:
        return HTMLResponse(PAGE.replace("__APP__", json.dumps(title or shelf.app)).replace("__BASE__", json.dumps(str(router.prefix) + prefix)))

    @router.get(prefix + "/read/{slug}")
    async def skills_read(slug: str, file: str = "") -> Any:
        try:
            return await run_in_threadpool(shelf.read, slug, file)
        except (KeyError, ValueError) as error:
            library = find_library(shelf.library)                # not on the shelf: show it from the library, to decide
            path = library / slug / "SKILL.md" if library is not None and _SLUG.match(slug) else None
            if path is not None and path.is_file():
                return {"slug": slug, "name": slug, "file": "SKILL.md", "files": [], "text": path.read_text(encoding="utf-8")}
            return fail(error)

    @router.post(prefix + "/assign")
    async def skills_assign(body: dict = Body(default={})) -> Any:
        try:
            return await run_in_threadpool(shelf.assign, str(body.get("slug") or ""), body.get("on") is not False)
        except (KeyError, ValueError) as error:
            return fail(error)

    @router.post(prefix + "/refresh")
    async def skills_refresh() -> Any:
        return await run_in_threadpool(shelf.call, "skills_refresh")

    @router.post(prefix + "/create")
    async def skills_create(body: dict = Body(default={})) -> Any:
        try:
            return await run_in_threadpool(shelf.create, body)
        except (KeyError, ValueError) as error:
            return fail(error)

    return router


def make_router(shelf: Shelf) -> Any:
    """The same page and calls under ``/api/skills`` (for an app that does not use ``make_agent_router``)."""
    from fastapi import APIRouter

    return mount(APIRouter(prefix="/api"), shelf, "/skills")


PAGE = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skills</title>
<link rel="stylesheet" href="/hoard-theme.css">
<style>
:root{--bg:var(--hoard-bg,#17161a);--panel:var(--hoard-surface,#201f24);--line:var(--hoard-line,rgba(255,255,255,.1));--text:var(--hoard-text,#ece9e2);--dim:var(--hoard-text-dim,#a39f96);--accent:var(--hoard-accent,#e8dcc0)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 16px 80px}h1{font-size:24px;margin:0 0 4px}h2{font-size:16px;margin:30px 0 8px}
p.dim,.dim{color:var(--dim)}a{color:inherit}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0}
input,textarea,select{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:100%}
textarea{min-height:92px;resize:vertical}
button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 13px;cursor:pointer}
button.primary{background:var(--accent);color:#17161a;border-color:transparent;font-weight:600}button:disabled{opacity:.5;cursor:default}
.row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:start;padding:12px 0;border-top:1px solid var(--line)}
.row strong{font-weight:600}.tag{display:inline-block;font-size:12px;color:var(--dim);border:1px solid var(--line);border-radius:99px;padding:0 8px;margin:4px 4px 0 0}
.why{font-size:12px;color:var(--dim);margin-left:8px}.acts{display:flex;gap:8px}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;max-width:820px;width:92vw;padding:20px}
dialog::backdrop{background:rgba(0,0,0,.6)}pre{white-space:pre-wrap;font:13px/1.55 ui-monospace,Consolas,monospace;max-height:62vh;overflow:auto;margin:0}
form.new{display:grid;gap:10px}form.new label{display:grid;gap:4px;font-size:13px;color:var(--dim)}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
#msg{min-height:22px;color:var(--dim)}@media(max-width:640px){.row{grid-template-columns:1fr}.two{grid-template-columns:1fr}}
</style></head><body><main>
<h1>Skills</h1>
<p class="dim" id="lead"></p>
<div class="bar"><button id="refresh">Actualizar desde la biblioteca</button><button id="new">Nueva skill</button><span id="msg" role="status" aria-live="polite"></span></div>
<h2>Las que lleva esta app</h2><div id="shelf"></div>
<h2>Biblioteca</h2>
<div class="bar"><input id="q" type="search" placeholder="Buscar en la biblioteca por nombre, descripción o etiqueta" aria-label="Buscar en la biblioteca"></div>
<div id="lib"></div>
<dialog id="reader"><h2 id="reader-title" style="margin-top:0"></h2><pre id="reader-text"></pre><div class="bar" style="justify-content:flex-end"><button id="reader-close">Cerrar</button></div></dialog>
<dialog id="maker"><h2 style="margin-top:0">Nueva skill en la biblioteca</h2>
<form class="new" id="maker-form">
<div class="two"><label>Nombre (palabras en minúscula con guiones)<input name="slug" required placeholder="revisar-malla-antes-de-imprimir"></label>
<label>Categoría<select name="category"></select></label></div>
<label>Qué hace y cuándo usarla, en inglés y con «Use when…»<input name="description" required placeholder="Checks a mesh before printing. Use when a model is about to be sliced."></label>
<label>Etiquetas, separadas por comas<input name="tags" placeholder="3d, mesh, malla, impresion-3d"></label>
<label>Cuándo se usa<textarea name="when" required></textarea></label>
<label>Procedimiento, paso a paso<textarea name="procedure" required style="min-height:150px"></textarea></label>
<div class="two"><label>Errores típicos<textarea name="pitfalls" required></textarea></label><label>Cómo se comprueba<textarea name="verification" required></textarea></label></div>
<label style="display:flex;gap:8px;align-items:center;color:var(--text)"><input type="checkbox" name="only_this_app" style="width:auto"> Solo para las herramientas de esta app</label>
<div class="bar" style="justify-content:flex-end"><button type="button" id="maker-cancel">Cancelar</button><button class="primary" type="submit">Guardar en la biblioteca y añadirla aquí</button></div>
</form></dialog>
</main><script>
const APP = __APP__, BASE = __BASE__;
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => { const e = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) { if (k === 'text') e.textContent = v; else if (k === 'class') e.className = v; else e.setAttribute(k, v); } for (const k of kids) if (k) e.append(k); return e; };
const WHY = { manifest: 'la pide la app', added: 'añadida por ti', tags: 'por afinidad' };
let state = null, timer = null;
async function api(path, body) {
  const r = await fetch(BASE + path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({ error: 'HTTP ' + r.status }));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
const say = (text) => { $('msg').textContent = text || ''; };
async function read(slug, name) {
  try { const j = await api('/read/' + encodeURIComponent(slug)); $('reader-title').textContent = name || slug; $('reader-text').textContent = j.text; $('reader').showModal(); }
  catch (e) { say(e.message); }
}
async function assign(slug, on, button) {
  button.disabled = true;
  try { await api('/assign', { slug, on }); say(on ? `Añadida: ${slug}.` : `Quitada: ${slug}.`); await load(); }
  catch (e) { say(e.message); button.disabled = false; }
}
function row(s, onShelf) {
  const info = el('div', {}, el('strong', { text: s.slug + (s.version ? '  ' + s.version : '') }),
    s.why ? el('span', { class: 'why', text: WHY[s.why] || s.why }) : null, s.other_apps ? el('span', { class: 'why', text: 'escrita para otra app' }) : null,
    el('div', { class: 'dim', text: s.description }), el('div', {}, ...(s.tags || []).slice(0, 8).map((t) => el('span', { class: 'tag', text: t }))));
  const view = el('button', { type: 'button', text: 'Leer', 'aria-label': 'Leer ' + s.slug });
  view.addEventListener('click', () => read(s.slug, s.name));
  const toggle = el('button', { type: 'button', class: onShelf ? '' : 'primary', text: onShelf ? 'Quitar' : 'Añadir', 'aria-label': (onShelf ? 'Quitar ' : 'Añadir ') + s.slug });
  toggle.addEventListener('click', () => assign(s.slug, !onShelf, toggle));
  return el('div', { class: 'row' }, info, el('div', { class: 'acts' }, view, toggle));
}
function draw() {
  $('lead').textContent = state.library_found
    ? `Los agentes que se conectan a ${APP} reciben estos procedimientos de la biblioteca de skills. Aquí eliges cuáles lleva.`
    : `No encuentro la biblioteca de skills en este equipo: ${APP} sigue sirviendo las copias que ya tiene.`;
  const shelf = $('shelf'); shelf.replaceChildren(...state.shelf.map((s) => row(s, true)));
  if (!state.shelf.length) shelf.append(el('p', { class: 'dim', text: 'Ninguna todavía. Añade alguna de la biblioteca.' }));
  const rest = state.library_skills.filter((s) => !s.on);
  const lib = $('lib'); lib.replaceChildren(...rest.map((s) => row(s, false)));
  if (!rest.length) lib.append(el('p', { class: 'dim', text: $('q').value ? 'Nada en la biblioteca con esas palabras.' : 'Esta app ya lleva todo lo que hay en la biblioteca.' }));
  const cat = $('maker-form').elements.category;
  if (!cat.options.length) for (const c of state.categories) cat.append(el('option', { value: c, text: c }));
}
async function load() { try { state = await api('?q=' + encodeURIComponent($('q').value.trim())); draw(); } catch (e) { say(e.message); } }
$('q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 220); });
$('refresh').addEventListener('click', async () => {
  $('refresh').disabled = true;
  try { const r = await api('/refresh', {}); const parts = [['añadidas', r.added], ['actualizadas', r.updated], ['retiradas', r.removed]].filter(([, v]) => v && v.length).map(([k, v]) => `${k}: ${v.join(', ')}`);
    say(r.source === 'library' ? (parts.join(' · ') || 'Todo estaba al día.') : 'No encuentro la biblioteca; sigo con las copias.'); await load(); }
  catch (e) { say(e.message); } finally { $('refresh').disabled = false; }
});
$('reader-close').addEventListener('click', () => $('reader').close());
$('new').addEventListener('click', () => $('maker').showModal());
$('maker-cancel').addEventListener('click', () => $('maker').close());
$('maker-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const f = ev.target.elements, body = { only_this_app: f.only_this_app.checked };
  for (const k of ['slug', 'category', 'description', 'tags', 'when', 'procedure', 'pitfalls', 'verification']) body[k] = f[k].value;
  try { const r = await api('/create', body); $('maker').close(); ev.target.reset(); say(`Guardada en la biblioteca: ${r.slug}.`); await load(); }
  catch (e) { say(e.message); alertBox(e.message); }
});
function alertBox(text) { $('reader-title').textContent = 'No se ha guardado'; $('reader-text').textContent = text; $('reader').showModal(); }
load();
</script></body></html>
"""
