"""The hub's HTTP server: a JSON API, the static UI, and the agent contract.

Standard library only (``http.server``), bound to loopback. Two guards:

* a browser page from another origin cannot call the API — a request whose
  ``Sec-Fetch-Site`` says ``cross-site`` is refused, and every mutating
  route is POST, so a plain link cannot stop an app;
* the agent routes (``/api/agent/*``) need the bearer token from
  ``<data>/mcp-token``, which is what the MCP bridge reads. The UI routes
  do not, because the person at the keyboard is the authority here.

Routes
------
GET  /                         the UI
GET  /api/health               {service, version, ...}
GET  /api/apps                 full snapshot (apps + states + faustus)
GET  /api/apps/<id>            one app
GET  /api/apps/<id>/icon       its icon
GET  /api/apps/<id>/log        last lines of its log
POST /api/apps/<id>/start|stop|restart|open|close-windows|folder
POST /api/apps/start-all | stop-all | rescan
GET  /api/backends             what Hoard Link resolves right now
GET  /api/lease                GPUs (used/free/reserved), granted leases, queue
GET  /api/lease/<id>           one lease (also keeps a queued one in the queue)
POST /api/lease/request        {owner, purpose, vram_mb, gpu, priority, ttl_s, wait, pid[, lease_id]}
POST /api/lease/renew          {lease_id, ttl_s}
POST /api/lease/release        {lease_id}
GET  /api/config               the effective configuration
GET  /api/agent/tools          (bearer) tool catalogue
POST /api/agent/call           (bearer) {"tool": name, "arguments": {...}}
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from . import HUB_VERSION, SERVICE
from .core import Hub
from . import desktop, tools
from .lease import LeaseError

logger = logging.getLogger("hoard_hub")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
MAX_BODY = 256 * 1024


def make_server(hub: Hub, host: str = "127.0.0.1", port: Optional[int] = None) -> ThreadingHTTPServer:
    port = hub.config.port if port is None else port

    class Handler(_HubHandler):
        pass

    Handler.hub = hub
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


class _HubHandler(BaseHTTPRequestHandler):
    hub: Hub
    server_version = f"hoard-hub/{HUB_VERSION}"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        logger.debug("%s " + fmt, self.address_string(), *args)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path: str, cache: bool = False) -> None:
        if not os.path.isfile(path):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _guard(self) -> bool:
        """Refuse cross-site calls from a browser; everything else is local."""
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        mode = (self.headers.get("Sec-Fetch-Mode") or "").lower()
        if site == "cross-site" and mode != "navigate":
            self._json({"ok": False, "error": "cross-site requests are refused"}, 403)
            return False
        if self.command == "POST" and site == "cross-site":
            self._json({"ok": False, "error": "cross-site requests are refused"}, 403)
            return False
        return True

    def _agent_ok(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or token != self.hub.token:
            self._json({"ok": False, "error": "bearer token required (data/mcp-token)"}, 401)
            return False
        return True

    # -- leases ---------------------------------------------------------------
    def _lease_reply(self, res: dict[str, Any]) -> None:
        status = int(res.pop("status", 0) or 0) if isinstance(res, dict) else 0
        if not status:
            status = 200 if res.get("ok", True) else 400
        return self._json(res, status)

    def _lease_post(self, action: str, body: dict[str, Any]) -> None:
        arb = self.hub.leases
        try:
            if action == "request":
                res = arb.request(owner=str(body.get("owner") or ""), purpose=str(body.get("purpose") or ""),
                                  vram_mb=body.get("vram_mb", 0), gpu=body.get("gpu"), priority=body.get("priority", 0),
                                  ttl_s=body.get("ttl_s"), wait=bool(body.get("wait", False)), pid=body.get("pid"),
                                  lease_id=body.get("lease_id"), wait_s=body.get("wait_s"))
            elif action == "renew":
                res = arb.renew(str(body.get("lease_id") or ""), body.get("ttl_s"))
            else:
                res = arb.release(str(body.get("lease_id") or ""))
        except LeaseError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        return self._lease_reply(res)

    # -- routing --------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        hub = self.hub
        try:
            if path == "/":
                return self._file(os.path.join(UI_DIR, "index.html"))
            if path.startswith("/ui/"):
                rel = os.path.normpath(path[4:]).replace("\\", "/")
                if rel.startswith("..") or rel.startswith("/"):
                    return self._json({"ok": False, "error": "not found"}, 404)
                return self._file(os.path.join(UI_DIR, rel), cache=False)
            if path == "/api/health":
                return self._json({"ok": True, "service": SERVICE, "version": HUB_VERSION, "apps": len(hub.apps),
                                   "url": hub.config.url})
            if path == "/api/apps":
                return self._json(hub.snapshot())
            if path == "/api/backends":
                return self._json(hub.backends(force=query.get("force", ["0"])[0] in ("1", "true")))
            if path == "/api/lease":
                return self._json(hub.leases.status(force=query.get("force", ["0"])[0] in ("1", "true")))
            if path.startswith("/api/lease/"):
                return self._lease_reply(hub.leases.get(path[len("/api/lease/"):]))
            if path == "/api/config":
                cfg = hub.config.to_dict()
                cfg["browser_found"] = desktop.find_browser(hub.config.browser)
                cfg["native_window"] = desktop.hub_window_native_available()
                return self._json(cfg)
            if path == "/api/agent/tools":
                if not self._agent_ok():
                    return None
                return self._json({"tools": tools.catalogue()})
            parts = path.split("/")
            if len(parts) >= 4 and parts[1] == "api" and parts[2] == "apps":
                app = hub.get(parts[3])
                if app is None:
                    return self._json({"ok": False, "error": "unknown app"}, 404)
                sub = parts[4] if len(parts) > 4 else ""
                if sub == "":
                    return self._json(hub.app_status(app))
                if sub == "icon":
                    if app.icon_path:
                        return self._file(app.icon_path, cache=True)
                    return self._file(os.path.join(UI_DIR, "fallback-icon.svg"), cache=True)
                if sub == "log":
                    n = int(query.get("lines", ["80"])[0])
                    return self._json(hub.log_tail(app.id, max(1, min(n, 2000))))
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            logger.exception("GET %s failed", path)
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = urlsplit(self.path).path.rstrip("/")
        hub = self.hub
        try:
            body = self._read_body()
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 413)
        try:
            if path == "/api/agent/call":
                if not self._agent_ok():
                    return None
                name = str(body.get("tool") or body.get("name") or "")
                args = body.get("arguments") or body.get("args") or {}
                result = tools.call(hub, name, args if isinstance(args, dict) else {})
                ok = not (isinstance(result, dict) and result.get("ok") is False)
                return self._json({"ok": ok, "tool": name, "result": result}, 200 if ok else 400)
            if path in ("/api/lease/request", "/api/lease/renew", "/api/lease/release"):
                return self._lease_post(path.rsplit("/", 1)[1], body)
            if path == "/api/apps/rescan":
                return self._json({"ok": True, "apps": [a.to_dict() for a in hub.rescan()]})
            if path == "/api/apps/start-all":
                return self._json(hub.start_all())
            if path == "/api/apps/stop-all":
                return self._json(hub.stop_all())
            parts = path.split("/")
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "apps":
                app_id, action = parts[3], parts[4]
                if hub.get(app_id) is None:
                    return self._json({"ok": False, "error": "unknown app"}, 404)
                if action == "start":
                    res = hub.start(app_id, wait=bool(body.get("wait", True)))
                elif action == "stop":
                    res = hub.stop(app_id)
                elif action == "restart":
                    res = hub.restart(app_id)
                elif action == "open":
                    res = hub.open(app_id, mode=str(body.get("mode") or "window"), autostart=bool(body.get("autostart", True)))
                elif action == "close-windows":
                    res = hub.close_windows(app_id)
                elif action == "folder":
                    res = hub.open_folder(app_id)
                else:
                    return self._json({"ok": False, "error": "unknown action"}, 404)
                return self._json(res, 200 if res.get("ok", True) else 409)
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            logger.exception("POST %s failed", path)
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def serve_in_thread(hub: Hub, host: str = "127.0.0.1", port: Optional[int] = None) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = make_server(hub, host, port)
    thread = threading.Thread(target=server.serve_forever, name="hoard-hub-http", daemon=True)
    thread.start()
    return server, thread
