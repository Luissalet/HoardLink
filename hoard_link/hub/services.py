"""Read-only discovery of the family service owners, without starting apps or models."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any

from . import contract
from .facets import Facet, Request

# Names are contract names, not ports: the registry resolves each owner.
SERVICES = (
    ("media", "links", ("media_download", "media_audio_for_asr", "media_subtitles")),
    ("stt", "funes", ("transcribe_file", "transcribe_status")),
    ("tts", "prospero", ("voice_tts",)),
    ("docs", "kafka", ("doc_extract", "ocr_image")),
    ("embed", "borges", ("embed_texts",)),
)


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
        names = {t.get("name") for t in catalog.get("tools", []) if isinstance(t, dict)}
        missing = [name for name in required if name not in names]
        return {**row, "state": "incompatible" if missing else "ready", "available": not missing,
                "missing_tools": missing, "error": "missing service tools" if missing else ""}

    def status(self, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not refresh and self._cached is not None and time.monotonic() - self._at < 15:
                return self._cached
            with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
                rows = list(pool.map(self._probe, SERVICES))
            web = self.hub.facet("web")
            web_up = bool(web is not None and web.settings().get("enabled"))
            rows.insert(0, {"service": "web", "owner": "hub", "name": "Hoard Hub", "url": "",
                           "available": web_up, "state": "ready" if web_up else "disabled", "error": ""})
            self._cached = {"ok": True, "services": rows, "checked_at": time.time(),
                            "probe": "tool_catalogue"}
            self._at = time.monotonic()
            return self._cached

    def get(self, req: Request) -> Any:
        if req.path == "/api/services/status":
            return self.status(req.q_bool("refresh"))
        return None

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [{"name": "hub_services_status", "description": "Discover the owners of web, downloads, transcription, voice, documents and embeddings. Checks their tool catalogues without loading models.",
                 "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean", "default": False}}, "additionalProperties": False},
                 "annotations": {"readOnlyHint": True}},
                {"name": "hub_media_import", "description": "Download audio, transcribe it with Funes and request indexing of a transcript folder in Borges. Opt-in: changes the selected folder and library. Returns job_id; follow hub_media_import_status. Existing downloads use download_id instead of url.",
                 "inputSchema": {"type": "object", "properties": {"folder": {"type": "string", "minLength": 1}, "url": {"type": "string"}, "download_id": {"type": "string"}, "language": {"type": "string", "default": "auto"}, "model": {"type": "string"}}, "required": ["folder"], "additionalProperties": False}},
                {"name": "hub_media_import_status", "description": "Follow a media import job. done means transcription is saved and Borges accepted an indexing request; library_status reports when indexing completes.",
                 "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "wait_s": {"type": "number", "minimum": 0, "maximum": 150, "default": 0}}, "required": ["job_id"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}}]

    def handlers(self) -> dict[str, Any]:
        return {"hub_services_status": lambda a: self.status(bool(a.get("refresh"))),
                "hub_media_import": self.mediaflow.start, "hub_media_import_status": self.mediaflow.status}
