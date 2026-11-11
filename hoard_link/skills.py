"""The skills an app carries for the agents that connect to it.

Faustus keeps a library of procedures (``skills/library/<slug>/SKILL.md``: how to do a kind of work well, kept up to date as
the family learns). An app says which of them matter to what it does - by slug, by tag, by category - and its shelf copies
them into the app's own data folder. Every agent that connects then has them: they are named in the instructions it reads on
connecting, listed and read with the ``skills_list`` / ``skill_read`` tools, and offered as prompts by the MCP bridge. The
copy is what is served, so the skills are there when Faustus is closed; the shelf looks at the library again when its copy is
older than ``max_age_s`` and takes whatever changed.

    shelf = Shelf("phidias", data_dir, {"slugs": ["blender-asset-production"], "tags": ["3d", "blender"]})
    router = make_agent_router(..., skills=shelf)

Nothing here raises towards the app: a missing library, an unreadable file or a full disk leave the shelf with what it had.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

log = logging.getLogger("hoard_link.skills")

TOOL_NAMES = ("skills_list", "skill_read", "skills_refresh")
MAX_FILE_BYTES = 400_000
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


class Shelf:
    """The skills of one app. ``wants`` = ``{"slugs": [...], "tags": [...], "categories": [...], "limit": 12}``: the slugs
    are taken as they are; the rest of the room goes to the library's skills that share most tags (or a category) with it.
    A skill whose front matter says ``apps: [a, b]`` drives those apps' own tools: the others only get it by naming it."""

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

    # ----- choosing
    def _choose(self, library: Path) -> list[tuple[str, dict[str, Any], str]]:
        slugs, tags, cats = _words(self.wants.get("slugs")), set(_words(self.wants.get("tags"))), set(_words(self.wants.get("categories")))
        limit = int(self.wants.get("limit") or 12)
        named, scored = [], []
        for path in sorted(library.glob("*/SKILL.md")):
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
            if slug in slugs:
                named.append((slugs.index(slug), slug, meta, text))
                continue
            only = _words(meta.get("apps"))
            if only and self.app.lower() not in only:
                continue                                   # a procedure written for other apps' tools: only by name
            shared = len(tags & set(_words(meta.get("tags"))))
            if shared or str(meta.get("category") or "").lower() in cats:
                scored.append((-shared, slug, meta, text))
        chosen = [(s, m, t) for _, s, m, t in sorted(named, key=lambda r: r[0])]
        chosen += [(s, m, t) for _, s, m, t in sorted(scored, key=lambda r: (r[0], r[1]))[:max(0, limit - len(chosen))]]
        return chosen

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
                for slug, meta, text in self._choose(library):
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                    files = self._copy(library / slug, self.folder / slug, text)
                    old = before.get(slug)
                    if old is None:
                        report["added"].append(slug)
                    elif old.get("digest") != digest:
                        report["updated"].append(slug)
                    rows.append({"slug": slug, "name": str(meta.get("name") or slug), "description": str(meta.get("description") or ""),
                                 "version": str(meta.get("version") or ""), "category": str(meta.get("category") or ""),
                                 "tags": _words(meta.get("tags")), "digest": digest, "files": files,
                                 "named": slug in _words(self.wants.get("slugs"))})
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
    def list(self) -> list[dict[str, Any]]:
        """The skills on the shelf: ``{slug, name, description, version, category, tags, files}``."""
        self.refresh()
        keys = ("slug", "name", "description", "version", "category", "tags", "files")
        return [{k: s.get(k) for k in keys} for s in self._index()["skills"] if isinstance(s, dict)]

    def read(self, slug: str, file: str = "") -> dict[str, Any]:
        """One skill's procedure (or one of its reference files), as text."""
        slug = str(slug or "").strip().lower().removeprefix("skill-")
        row = next((s for s in self.list() if s["slug"] == slug or str(s["name"]).lower() == slug), None)
        if row is None:
            raise KeyError(f"No skill called {slug!r} on this app's shelf. skills_list names the ones there are.")
        rel = str(file or "SKILL.md").replace("\\", "/")
        if rel != "SKILL.md" and rel not in (row.get("files") or []):
            raise KeyError(f"The skill {row['slug']} has no file {rel!r}. Its files: {', '.join(row.get('files') or []) or 'none'}.")
        try:
            text = (self.folder / row["slug"] / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise KeyError(f"The copy of {row['slug']}/{rel} cannot be read ({error}). skills_refresh brings it back.") from error
        return {"slug": row["slug"], "name": row["name"], "version": row["version"], "file": rel, "files": row.get("files") or [], "text": text}

    def status(self) -> dict[str, Any]:
        index = self._index()
        return {"app": self.app, "skills": len(index["skills"]), "refreshed_at": index.get("refreshed_at"), "library": index.get("library"),
                "wants": {k: self.wants.get(k) for k in ("slugs", "tags", "categories", "limit") if self.wants.get(k)}}

    # ----- what an agent sees
    def instructions(self) -> str:
        rows = self.list()
        if not rows:
            return ""
        lines = ["Skills. This app carries procedures from the family's skill library for the work it does. Before a task one of "
                 "them covers, read it with skill_read and follow it; skills_list gives them again."]
        lines += [f"- {r['slug']}: {r['description']}" for r in rows]
        return "\n".join(lines)

    def catalog(self) -> list[dict[str, Any]]:
        """The three tools, as ``GET /api/agent/tools`` entries."""
        read_only = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
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
            {"name": "skills_refresh", "annotations": {"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False, "openWorldHint": False},
             "inputSchema": {"type": "object", "properties": {}},
             "description": "Look at the family's skill library again and bring this app's copies up to date (added, updated, removed). "
                            "The app does it by itself every few minutes; use it right after a skill was improved."},
        ]

    def call(self, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Any:
        args = dict(arguments or {})
        if name == "skills_list":
            return {**self.status(), "skills": self.list()}
        if name == "skill_read":
            return self.read(str(args.get("slug") or ""), str(args.get("file") or ""))
        if name == "skills_refresh":
            return {**self.refresh(force=True), "shelf": self.list()}
        raise KeyError(name)


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
    """The shelf of an app whose manifest has a ``skills`` block, with nothing else to set up; None without the block.
    The copies go to ``data_dir`` when it is a plain path, else next to the app's token (``family.configure``), else to the
    ``data`` folder beside the manifest."""
    manifest = own_manifest()
    wants = wants_of(manifest)
    if manifest is None or not wants:
        return None

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


def make_router(shelf: Shelf) -> Any:
    """The shelf for the app's own page: ``GET /api/skills`` (status and list), ``GET /api/skills/{slug}`` (the text) and
    ``POST /api/skills/refresh``."""
    from fastapi import APIRouter
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/api/skills")

    @router.get("")
    async def listing() -> dict[str, Any]:
        return await run_in_threadpool(shelf.call, "skills_list")

    @router.post("/refresh")
    async def refresh() -> dict[str, Any]:
        return await run_in_threadpool(shelf.call, "skills_refresh")

    @router.get("/{slug}")
    async def one(slug: str, file: str = "") -> Any:
        try:
            return await run_in_threadpool(shelf.read, slug, file)
        except KeyError as error:
            return JSONResponse({"error": str(error.args[0]), "code": "not_found"}, status_code=404)

    return router
