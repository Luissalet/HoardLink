"""Read-only discovery of the family service owners, without starting apps or models."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from collections import defaultdict
from typing import Any

from . import contract
from .facets import Facet, Request
from ..service_contracts import SERVICES

# Names are contract names, not ports: the registry resolves each owner.


class ServicesFacet(Facet):
    id = "services"
    ui_scripts = ("services.js",)

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._at = 0.0
        from .mediaflow import MediaFlow
        self.mediaflow = MediaFlow(hub)

    def close(self):
        self.mediaflow.close()

    def start(self):
        # Worktrack subscribes earlier in the facet lifecycle. Reconcile saved
        # paused imports with the shared jobs view after both have loaded.
        for job in self.mediaflow._jobs.values():
            if job["status"] == "paused":
                self.mediaflow._event(job, "paused")

    def _probe(self, service: tuple[str, str, tuple[str, ...]]) -> dict[str, Any]:
        key, owner, required = service
        app = self.hub.get(owner)
        row = {"service": key, "owner": owner, "name": app.name if app else owner,
               "url": app.url if app else "", "tools": list(required), "available": False}
        if app is None:
            return {**row, "state": "missing", "error": "owner is not registered"}
        try:
            catalog = contract.app_tools(app, timeout=2.0)
        except Exception as exc:
            return {**row, "state": "down", "error": str(exc)}
        if not catalog.get("ok"):
            return {**row, "state": "down", "error": catalog.get("error", "not reachable")}
        names = {t["name"] for t in catalog.get("tools", []) if isinstance(t, dict) and isinstance(t.get("name"), str)}
        missing = [name for name in required if name not in names]
        return {**row, "state": "incompatible" if missing else "ready", "available": not missing, "catalogue_tools": sorted(names),
                "missing_tools": missing, "error": "missing service tools" if missing else ""}

    def status(self, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not refresh and self._cached is not None and time.monotonic() - self._at < 15:
                return self._cached
            with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
                rows = list(pool.map(self._probe, SERVICES))
            catalogues = {r["owner"]: set(r.pop("catalogue_tools", [])) for r in rows}
            requirements = {"links": ("media_download", "media_status"), "funes": ("transcribe_file", "transcribe_status"),
                            "borges": ("library_add_collection", "library_reindex")}
            missing = [{"owner": owner, "tool": tool} for owner, tools in requirements.items()
                       for tool in tools if tool not in catalogues.get(owner, set())]
            web = self.hub.facet("web")
            web_up = bool(web is not None and web.settings().get("enabled"))
            rows.insert(0, {"service": "web", "owner": "hub", "name": "Hoard Hub", "url": "",
                           "available": web_up, "state": "ready" if web_up else "disabled", "error": ""})
            self._cached = {"ok": True, "services": rows, "checked_at": time.time(),
                            "probe": "tool_catalogue", "workflows": [{"id": "media_import", "owners": list(requirements),
                            "available": not missing, "missing": missing, "opt_in": True, "model_readiness": "checked_on_execution"}]}
            self._at = time.monotonic()
            return self._cached

    def get(self, req: Request) -> Any:
        if req.path == "/api/services/status":
            return self.status(req.q_bool("refresh"))
        if req.path == "/api/services/imports":
            if req.caller() not in ("hub", "ui") and not req.agent():
                return {"ok": False, "status": 403, "error": "only the Hub operator may inspect imports"}
            return self.mediaflow.list()
        if req.path == "/api/services/cohesion":
            return self.cohesion(req.q_bool("refresh"))
        return None

    def cohesion(self, refresh=False):
        """Declared capabilities are overlap candidates, never automatic mergers."""
        from . import drift
        from pathlib import Path
        canonical = Path(__file__).parents[1]
        capabilities = defaultdict(list)
        apps = []
        for app in getattr(self.hub, "apps", []):
            for capability in set(app.capabilities):
                capabilities[capability].append(app.id)
            copies = []
            for copy in drift.find_vendored(app.folder, canonical):
                changed, removed = drift.plan_tree(canonical, copy)
                copies.append({"path": str(copy.relative_to(app.folder)), "changed": changed, "removed": removed})
            node = Path(app.folder) / "server" / "hoard-link.js"
            if node.is_file():
                js = canonical.parent / "js"
                changed, removed = drift.plan_tree(js / "hoard-commons", node.parent / "hoard-commons")
                if drift.vendored_js_stale(app.folder, js / "hoard-link.js"):
                    changed.append("../hoard-link.js")
                copies.append({"path": "server/hoard-commons", "changed": changed, "removed": removed})
            apps.append({"id": app.id, "name": app.name, "purpose": app.purpose, "capabilities": app.capabilities,
                         "shared_code_drift": copies})
        state = self.status(refresh)
        return {"ok": True, "apps": apps, "services": state["services"], "workflows": state["workflows"],
                "capability_overlaps": [{"capability": k, "apps": sorted(v), "interpretation": "review_responsibilities"}
                                        for k, v in sorted(capabilities.items()) if len(v) > 1],
                "imports": [{"job_id": j["job_id"], "status": j["status"], "stage": j["stage"]}
                            for j in self.mediaflow.list()["jobs"]], "probe": state["probe"]}

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [{"name": "hub_cohesion", "description": "Audit family ownership, capability overlaps, shared-code drift and workflow prerequisites without starting apps or models.",
                 "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean", "default": False}}, "additionalProperties": False},
                 "annotations": {"readOnlyHint": True}},
                {"name": "hub_services_status", "description": "Discover the owners of web, downloads, transcription, voice, documents and embeddings. Checks their tool catalogues without loading models.",
                 "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean", "default": False}}, "additionalProperties": False},
                 "annotations": {"readOnlyHint": True}},
                {"name": "hub_media_import", "description": "Download audio, transcribe it with Funes and request indexing of a transcript folder in Borges. Opt-in: changes the selected folder and library. Returns job_id; follow hub_media_import_status. Existing downloads use download_id instead of url.",
                 "inputSchema": {"type": "object", "properties": {"folder": {"type": "string", "minLength": 1}, "url": {"type": "string"}, "download_id": {"type": "string"}, "language": {"type": "string", "default": "auto"}, "model": {"type": "string"}}, "required": ["folder"], "additionalProperties": False}},
                {"name": "hub_media_import_status", "description": "Follow a media import job. done means transcription is saved and Borges accepted an indexing request; library_status reports when indexing completes.",
                 "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "wait_s": {"type": "number", "minimum": 0, "maximum": 150, "default": 0}}, "required": ["job_id"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                {"name": "hub_media_imports", "description": "List saved media imports, including work paused by a Hub restart. No providers are called.",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
                {"name": "hub_media_import_resume", "description": "Explicitly resume a saved media import by following its owner job IDs. Lost mutation replies require reconciliation with the owner; never blindly starts duplicate work.",
                 "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "download_id": {"type": "string"},
                    "transcribe_job_id": {"type": "string"}, "collection_id": {"type": "integer"}, "index_requested": {"type": "boolean"}},
                    "required": ["job_id"], "additionalProperties": False}}]

    def post(self, req: Request) -> Any:
        if req.path == "/api/services/imports/resume":
            if req.caller() not in ("hub", "ui") and not req.agent():
                return {"ok": False, "status": 403, "error": "only the Hub operator may resume imports"}
            return self.mediaflow.resume(req.body)
        return None

    def handlers(self) -> dict[str, Any]:
        return {"hub_services_status": lambda a: self.status(bool(a.get("refresh"))),
                "hub_cohesion": lambda a: self.cohesion(bool(a.get("refresh"))),
                "hub_media_import": self.mediaflow.start, "hub_media_import_status": self.mediaflow.status,
                "hub_media_imports": lambda a: self.mediaflow.list(), "hub_media_import_resume": self.mediaflow.resume}
