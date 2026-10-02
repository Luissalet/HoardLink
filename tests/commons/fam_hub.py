"""A fake hub for the family service clients: ``POST /api/apps/<id>/call`` and ``GET /api/apps/<id>`` on 127.0.0.1.

It answers like the real hub's proxy (``hoard_link/hub/contract.py`` + ``server.py``): HTTP 200 and ``{ok, app, tool, status, contract, ms,
result}`` for a tool that ran; the tool's own failure as ``{ok: False, status, error}`` with that status; an app that is not running as
HTTP 502 with ``status: None`` and ``error: "not reachable"``; an unknown app as 404 ``unknown app``; a tool the app does not have as the
app's own 404. Register handlers with :meth:`FakeHub.on`: ``handler(args, call)`` returns the tool's result (a dict) or raises
:class:`ToolError` (status, message). Every request is recorded in :attr:`FakeHub.calls`.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from tests.hub.conftest import free_port

TOKEN = "app-token-xyz"


class ToolError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class FakeHub:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.calls: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.apps: dict[str, dict[str, Any]] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.gets.append(self.path)
                parts = self.path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "apps"]:
                    app = outer.apps.get(parts[2])
                    if app is None:
                        return self._send(404, {"ok": False, "error": "unknown app"})
                    return self._send(200, {"id": parts[2], "state": app["state"]})
                self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                parts = self.path.strip("/").split("/")
                if not (len(parts) == 4 and parts[:2] == ["api", "apps"] and parts[3] == "call"):
                    return self._send(404, {"ok": False, "error": "not found"})
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    return self._send(401, {"ok": False, "error": "a family bearer token is required"})
                app_id, tool, args = parts[2], str(body.get("tool") or ""), body.get("arguments") or {}
                outer.calls.append({"app": app_id, "tool": tool, "args": args, "timeout_s": body.get("timeout_s")})
                app = outer.apps.get(app_id)
                if app is None:
                    return self._send(404, {"ok": False, "error": "unknown app"})
                base = {"app": app_id, "tool": tool, "contract": "shared", "ms": 3}
                if app["state"] != "running":
                    return self._send(502, {"ok": False, "status": None, "error": "not reachable", **base})
                handler = app["tools"].get(tool)
                if handler is None:
                    return self._send(404, {"ok": False, "status": 404, "error": f"unknown tool: {tool}", **base})
                try:
                    result = handler(args, outer.calls[-1])
                except ToolError as exc:
                    return self._send(exc.status, {"ok": False, "status": exc.status, "error": exc.message, **base})
                self._send(200, {"ok": True, "status": 200, "result": result, **base})

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    # -- setup ------------------------------------------------------------------------------------------------------
    def app(self, app_id: str, state: str = "running") -> dict[str, Any]:
        return self.apps.setdefault(app_id, {"state": state, "tools": {}})

    def on(self, app_id: str, tool: str, handler: Callable[[dict, dict], Any]) -> None:
        """``handler(args, call)`` -> the tool's result. A non-callable is returned as it is."""
        self.app(app_id)["tools"][tool] = handler if callable(handler) else (lambda _a, _c, v=handler: v)

    def set_state(self, app_id: str, state: str) -> None:
        self.app(app_id)["state"] = state

    def close(self) -> None:
        self.server.shutdown()

    # -- inspection -------------------------------------------------------------------------------------------------
    def of(self, tool: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["tool"] == tool]


def sequence(*answers: Any) -> Callable[[dict, dict], Any]:
    """A handler that answers ``answers`` in order, then repeats the last one. An answer that is callable is called with (args, call);
    an Exception is raised."""
    state = {"i": 0}
    lock = threading.Lock()

    def handler(args: dict, call: dict) -> Any:
        with lock:
            i = min(state["i"], len(answers) - 1)
            state["i"] += 1
        a = answers[i]
        if isinstance(a, Exception):
            raise a
        return a(args, call) if callable(a) else a
    return handler


def slow(seconds: float, value: Any) -> Callable[[dict, dict], Any]:
    def handler(args: dict, call: dict) -> Any:
        time.sleep(seconds)
        return value
    return handler
