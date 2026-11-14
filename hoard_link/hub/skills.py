"""Skills facet: which procedures of the skill library each app of the family carries, in one place.

Every app keeps its own shelf (:mod:`hoard_link.skills`): what its manifest asks for plus what the person added or took
away, copied into the app's data folder. This facet reads and changes those shelves from the hub, running or not, and
serves the same skills page an app serves for itself.

* ``GET  /api/skills/overview``                 -> every app with the skills it carries
* ``GET  /api/skills/page?app=ID``              -> the skills page of that app
* ``GET  /api/skills/app/ID[?q=]``              -> its state (shelf, library, choices)
* ``GET  /api/skills/app/ID/read/SLUG``         -> one skill's text
* ``POST /api/skills/app/ID/assign|refresh|create``
* ``POST /api/skills/refresh``                  -> bring every app's copies up to date
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from typing import Any, Optional

from .. import skills
from .. import agent_flows
from .facets import Facet, Reply, Request


class SkillsFacet(Facet):
    id = "skills"
    ui_scripts = ("skills.js",)

    def start(self) -> None:
        """Keep every app's copies current from here: an app only reads its copy when an agent lists its tools."""
        self._stop = threading.Event()

        def loop() -> None:
            wait = 20.0
            while not self._stop.wait(wait):
                wait = 900.0
                try:
                    self.refresh_all()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=loop, daemon=True, name="skills-keeper").start()

    def close(self) -> None:
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.set()

    # ----- one app
    def _app(self, app_id: str) -> Any:
        return next((a for a in self.hub.apps if a.id == app_id), None)

    def _shelf(self, app: Any) -> skills.Shelf:
        data = app.data_dir or os.path.join(app.folder, "data")
        return skills.Shelf(app.id, data, skills.wants_of(app.manifest_path), library=self.hub.config.faustus_dir, max_age_s=0)

    def _poke(self, app: Any) -> None:
        """Tell a running app its shelf changed (it would notice by itself in a few minutes)."""
        def go() -> None:
            try:
                req = urllib.request.Request(app.url.rstrip("/") + "/api/agent/skills/refresh", data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=4).read()
            except Exception:  # noqa: BLE001 - stopped, or a copy of the library that has no skills page yet
                pass
        threading.Thread(target=go, daemon=True, name=f"skills-poke-{app.id}").start()

    # ----- all of them
    def overview(self) -> dict[str, Any]:
        library = skills.find_library(self.hub.config.faustus_dir)
        rows = []
        for app in sorted(self.hub.apps, key=lambda a: a.name.lower()):
            shelf = self._shelf(app)
            index = shelf._index()
            carried = [s.get("slug") for s in index["skills"] if isinstance(s, dict)]
            rows.append({"id": app.id, "name": app.name, "skills": carried, "count": len(carried), "refreshed_at": index.get("refreshed_at"),
                         "asks": bool(shelf.wants), "serves": _serves(app)})
        return {"ok": True, "apps": rows, "library": str(library) if library else None,
                "library_skills": len(skills.library_rows(library)) if library else 0}

    def refresh_all(self) -> dict[str, Any]:
        out = []
        for app in self.hub.apps:
            report = self._shelf(app).refresh(force=True)
            if report.get("added") or report.get("updated") or report.get("removed"):
                out.append({"app": app.id, **{k: report[k] for k in ("added", "updated", "removed")}})
                self._poke(app)
        return {"ok": True, "changed": out, "apps": len(self.hub.apps)}

    # ----- routes
    def flows(self, app_id: str = "", query: str = "") -> dict[str, Any]:
        if app_id and self._app(app_id) is None:
            return {"ok": False, "status": 404, "error": "unknown app"}
        apps = []
        for app in sorted(self.hub.apps, key=lambda a: a.name.lower()):
            if app_id and app.id != app_id:
                continue
            flows = agent_flows.catalogue(self._shelf(app), base=f"/api/skills/app/{app.id}", query=query)
            apps.append({"id": app.id, "name": app.name, "flows": flows, "count": len(flows),
                         "shelf_url": "/api/skills/page?app=" + app.id})
        return {"ok": True, "apps": apps, "kind": "written_procedures", "executed": False}

    def _route(self, req: Request) -> Optional[tuple[Any, str]]:
        parts = req.path.split("/")                        # ['', 'api', 'skills', 'app', ID, ...]
        if len(parts) < 5 or parts[:4] != ["", "api", "skills", "app"]:
            return None
        app = self._app(parts[4])
        if app is None:
            return None
        return app, "/".join(parts[5:])

    def get(self, req: Request) -> Any:
        if req.path == "/api/skills/flows":
            return self.flows(str(req.q("app") or ""), str(req.q("q") or ""))
        if req.path == "/api/skills/overview":
            return self.overview()
        if req.path == "/api/skills/page":
            app = self._app(str(req.q("app") or ""))
            if app is None:
                return {"ok": False, "status": 404, "error": "unknown app"}
            page = skills.PAGE.replace("__APP__", json.dumps(app.name)).replace("__BASE__", json.dumps(f"/api/skills/app/{app.id}"))
            page = page.replace('href="/hoard-theme.css"', 'href="/ui/hoard-theme.css"')
            return Reply(body=page.encode("utf-8"), content_type="text/html; charset=utf-8")
        found = self._route(req)
        if found is None:
            return None
        app, rest = found
        shelf = self._shelf(app)
        if rest == "":
            return shelf.state(str(req.q("q") or ""))
        if rest.startswith("read/"):
            slug = rest[5:]
            try:
                return shelf.read(slug, str(req.q("file") or ""))
            except (KeyError, ValueError) as error:
                library = skills.find_library(self.hub.config.faustus_dir)
                path = os.path.join(str(library), slug, "SKILL.md") if library is not None and skills._SLUG.match(slug) else ""
                if path and os.path.isfile(path):
                    with open(path, encoding="utf-8") as fh:
                        return {"slug": slug, "name": slug, "file": "SKILL.md", "files": [], "text": fh.read()}
                return {"ok": False, "status": 404, "error": str(error.args[0]) if error.args else str(error)}
        return None

    def post(self, req: Request) -> Any:
        if req.path == "/api/skills/refresh":
            return self.refresh_all()
        found = self._route(req)
        if found is None:
            return None
        app, rest = found
        shelf = self._shelf(app)
        try:
            if rest == "assign":
                out = shelf.assign(str(req.body.get("slug") or ""), req.body.get("on") is not False)
            elif rest == "refresh":
                out = shelf.call("skills_refresh")
            elif rest == "create":
                out = shelf.create(req.body)
            else:
                return None
        except KeyError as error:
            return {"ok": False, "status": 404, "error": str(error.args[0]) if error.args else str(error)}
        except ValueError as error:
            return {"ok": False, "status": 400, "error": str(error)}
        self._poke(app)
        return out

    # ----- for agents
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        read_only = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
        return [
            {"name": "hub_agent_flows", "annotations": read_only,
             "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "query": {"type": "string"}}},
             "description": "Written agent flows per app, from its current skill shelf: trigger, full steps, outputs when declared, verification, version and source hash. Does not execute flows or start apps."},
            {"name": "hub_skills_overview", "annotations": read_only, "inputSchema": {"type": "object", "properties": {}},
             "description": "Which procedures of the family's skill library each app carries for the agents that connect to it "
                            "(app, skills, whether its manifest asks for any, whether the app serves them)."},
            {"name": "hub_skills_library", "annotations": read_only,
             "inputSchema": {"type": "object", "required": ["app"], "properties": {
                 "app": {"type": "string", "description": "App id."}, "query": {"type": "string", "description": "Words to look for in slug, description and tags."}}},
             "description": "The skill library as one app sees it: every skill with whether that app carries it (`on`) and why. Use it to "
                            "choose what to assign."},
            {"name": "hub_skill_read", "annotations": read_only,
             "inputSchema": {"type": "object", "required": ["app", "slug"], "properties": {
                 "app": {"type": "string", "description": "App id."}, "slug": {"type": "string", "description": "A skill that app carries (hub_skills_overview names them)."},
                 "file": {"type": "string", "description": "One of the skill's reference files; empty = the procedure itself."}}},
             "description": "One of an app's skills in full: the procedure to follow with that app's tools, its pitfalls and how to verify the "
                            "result. Works for every app, running or not, also the ones whose own bridge does not hand skills out."},
            {"name": "hub_skills_assign", "annotations": {"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False, "openWorldHint": False},
             "inputSchema": {"type": "object", "required": ["app", "slug"], "properties": {
                 "app": {"type": "string", "description": "App id."}, "slug": {"type": "string", "description": "Skill slug in the library."},
                 "on": {"type": "boolean", "default": True, "description": "true gives the skill to the app; false takes it away."}}},
             "description": "Give an app a skill of the library, or take one away. It stays for every agent that connects to that app; the "
                            "app is told when it is running."},
        ]

    def handlers(self) -> dict[str, Any]:
        def library(a: dict[str, Any]) -> Any:
            app = self._app(str(a.get("app") or ""))
            if app is None:
                return {"ok": False, "error": "unknown app", "apps": [x.id for x in self.hub.apps]}
            return {"ok": True, "app": app.id, "skills": self._shelf(app).catalogue(str(a.get("query") or ""), 80)}

        def assign(a: dict[str, Any]) -> Any:
            app = self._app(str(a.get("app") or ""))
            if app is None:
                return {"ok": False, "error": "unknown app", "apps": [x.id for x in self.hub.apps]}
            try:
                out = self._shelf(app).assign(str(a.get("slug") or ""), a.get("on") is not False)
            except (KeyError, ValueError) as error:
                return {"ok": False, "error": str(error.args[0]) if error.args else str(error)}
            self._poke(app)
            return {"ok": True, **out}

        def read(a: dict[str, Any]) -> Any:
            app = self._app(str(a.get("app") or ""))
            if app is None:
                return {"ok": False, "error": "unknown app", "apps": [x.id for x in self.hub.apps]}
            try:
                return {"ok": True, "app": app.id, **self._shelf(app).read(str(a.get("slug") or ""), str(a.get("file") or ""))}
            except (KeyError, ValueError) as error:
                return {"ok": False, "error": str(error.args[0]) if error.args else str(error)}

        return {"hub_agent_flows": lambda a: self.flows(str(a.get("app") or ""), str(a.get("query") or "")),
                "hub_skills_overview": lambda a: self.overview(), "hub_skills_library": library, "hub_skill_read": read, "hub_skills_assign": assign}


_HANDS_OUT = ("family_skills", "installSkills", "skillCall", "skills_list", "skills.attach(", "skills.raw(")


def _shared_bridge(folder: str) -> bool:
    """Whether the app's copy of the shared bridge hands out the shelf."""
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in (".venv", "venv", "node_modules", ".git", "build", "dist", "data", "__pycache__", "runtime")]
        if os.path.basename(root) == "hoard_link" and "bridge.py" in files:
            try:
                with open(os.path.join(root, "bridge.py"), encoding="utf-8") as fh:
                    return "skills.py" in files and "_own_skills" in fh.read()
            except OSError:
                return False
        if root.count(os.sep) - folder.count(os.sep) >= 3:
            dirs[:] = []
    return False


def _serves(app: Any) -> bool:
    """Whether an agent that connects to the app is handed its skills: the program its manifest starts as MCP server is
    read, and it either is the shared bridge (Python or Node) in a copy that does it, or does it itself."""
    try:
        with open(app.manifest_path, encoding="utf-8-sig") as fh:
            manifest = json.load(fh)
        args = [str(a) for a in ((manifest.get("mcp") or {}).get("args") or [])]
    except (OSError, ValueError, AttributeError):
        return False
    folders = [app.folder]
    holder = os.path.dirname(os.path.normpath(app.folder))
    if os.path.basename(holder) == "compatibility":
        folders.append(os.path.dirname(holder))            # a former app that lives on as a module of the app that absorbed it
    entry = ""
    for folder in folders:
        for i, arg in enumerate(args):
            if arg == "-m" and i + 1 < len(args):
                entry = os.path.join(folder, *args[i + 1].split(".")) + ".py"
                break
            found = re.search(r"\}[/\\]([\w./\\ -]+\.(?:py|mjs|js))$", arg)
            if found:
                entry = os.path.join(folder, *re.split(r"[/\\]", found.group(1)))
                break
        if entry and os.path.isfile(entry):
            break
    if not (entry and os.path.isfile(entry)):                # a program named by a placeholder of the manifest's own defaults
        values = {str(k): str(v) for k, v in (manifest.get("defaults") or {}).items()}
        values.update(COMPAT_DIR=app.folder)
        for folder in folders:
            for arg in args:
                text = arg
                for _ in range(6):
                    for key, value in values.items():
                        text = text.replace("{" + key + "}", value)
                    text = text.replace("{" + next((p for p in manifest.get("placeholders") or [] if str(p).endswith("_DIR")), "_") + "}", folder)
                text = os.path.normpath(os.path.expandvars(text))
                if "{" not in text and text.lower().endswith((".cjs", ".mjs", ".js", ".py")) and os.path.isfile(text):
                    entry = text
                    break
            if entry and os.path.isfile(entry):
                break
    sources = []
    for path in (entry, os.path.join(os.path.dirname(entry), "server.mjs")) if entry else ():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                sources.append(fh.read())
        except OSError:
            pass
    text = "\n".join(sources)
    if "CatalogBridge" in text:
        return _shared_bridge(app.folder) or any(mark in text for mark in _HANDS_OUT)
    if "createBridge" in text:
        try:
            with open(os.path.join(app.folder, "server", "hoard-commons", "express.js"), encoding="utf-8") as fh:
                return "installSkills" in fh.read()
        except OSError:
            return False
    return any(mark in text for mark in _HANDS_OUT)
