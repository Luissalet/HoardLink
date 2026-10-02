"""Shared fakes for the 0.7 facet tests: a hub over temporary app folders (manifest + token), fake apps
answering the agent contract from local ``http.server`` threads, a recording notify facet, and a few waits."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server

from .conftest import free_port, write_manifest


class _QuietServer(ThreadingHTTPServer):
    """A client that gave up (a timeout test) is not worth a traceback on stderr."""

    def handle_error(self, request, client_address):  # noqa: D102
        pass


class FakeApp:
    """A fake app on the shared agent contract. ``tools`` is the catalogue; ``handlers[name](args) -> result``
    (a callable may raise ``ValueError`` for a 400, or return ``(status, body)``). Every call is recorded."""

    def __init__(self, app_id: str, tools: Optional[list[dict[str, Any]]] = None,
                 handlers: Optional[dict[str, Callable[[dict[str, Any]], Any]]] = None, token: Optional[str] = None,
                 delay: float = 0.0, catalogue_status: int = 200):
        self.app_id = app_id
        self.tools = tools if tools is not None else []
        self.handlers = handlers or {}
        self.token = token or f"token-{app_id}"
        self.delay = delay
        self.catalogue_status = catalogue_status
        self.calls: list[dict[str, Any]] = []
        self.port = free_port()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D102
                pass

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/api/agent/tools"):
                    if outer.catalogue_status != 200:
                        return self._json({"error": "nope"}, outer.catalogue_status)
                    return self._json({"tools": outer.tools})
                return self._json({"ok": True, "service": f"{outer.app_id}-hoard"})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path != "/api/agent/call":
                    return self._json({"error": "not found"}, 404)
                if self.headers.get("Authorization", "") != "Bearer " + outer.token:
                    return self._json({"error": "Invalid MCP token."}, 401)
                outer.calls.append(body)
                if outer.delay:
                    time.sleep(outer.delay)
                fn = outer.handlers.get(body.get("name"))
                if fn is None:
                    return self._json({"error": f"unknown tool: {body.get('name')}"}, 404)
                try:
                    res = fn(body.get("arguments") or {})
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                if isinstance(res, tuple):
                    return self._json(res[1], res[0])
                return self._json(res)

        self.server = _QuietServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RecordingNotify:
    """Stands in for the notify facet: records ``send`` calls."""

    id = "notify"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, title, body="", **kw):
        rec = {"title": title, "body": body, **kw}
        self.sent.append(rec)
        return {"ok": True, "id": len(self.sent)}


def make_hub(tmp_path: Path, fakes: Optional[list[FakeApp]] = None, extra_apps: Optional[list[str]] = None) -> Hub:
    """A hub whose family is the given fake apps (and bare manifests for ``extra_apps``)."""
    root = tmp_path / "apps"
    root.mkdir(parents=True, exist_ok=True)
    for fake in fakes or []:
        folder = write_manifest(root / f"{fake.app_id.title()}'s Hoard", fake.app_id, fake.port, service=f"{fake.app_id}-hoard")
        (folder / "data").mkdir(exist_ok=True)
        (folder / "data" / "mcp-token").write_text(fake.token, encoding="utf-8")
    for app_id in extra_apps or []:
        folder = write_manifest(root / f"{app_id.title()}'s Hoard", app_id, free_port(), service=f"{app_id}-hoard")
        (folder / "data").mkdir(exist_ok=True)
        (folder / "data" / "mcp-token").write_text(f"token-{app_id}", encoding="utf-8")
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "hubdata"), roots=[str(root)], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False)
    return Hub(cfg)


def serve(hub: Hub):
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True).start()
    return server


def http(url: str, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def wait_for(pred: Callable[[], Any], timeout: float = 5.0, step: float = 0.02) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()
