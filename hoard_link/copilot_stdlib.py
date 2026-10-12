"""Faustus copilot transport for BaseHTTPRequestHandler hosts (no framework)."""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import LinkConfig
from .copilot import inject_launcher, multipart_context


class CopilotRelay:
    """Call ``handle(self)`` before an HTTP handler's ordinary routes.

    Returns False for other paths. Chats, drafts, model calls and tools remain
    in Faustus; only the data-space identifier is stored by this transport.
    """

    def __init__(self, app_id, data_dir, *, app_name=None, faustus_urls=None,
                 token=None, config_path=None, env=None, timeout=900):
        env = os.environ if env is None else env
        config = LinkConfig.load(path=config_path, env=env, app=app_id)
        app_url = env.get(app_id.upper().replace("-", "_") + "_FAUSTUS_URL", "").strip()
        self.urls = tuple(str(url).strip().rstrip("/") for url in (
            faustus_urls if faustus_urls is not None else
            [app_url] if app_url else config.faustus_urls))
        self.app_id = app_id
        self.app_name = app_name or app_id
        self.token = config.faustus_token if token is None else token
        self.timeout = timeout
        self._selected = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        space_file = Path(data_dir) / "copilot-space-id"
        space_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with space_file.open("x", encoding="utf-8") as file:
                file.write(str(uuid.uuid4()))
        except FileExistsError:
            pass
        self.space_id = space_file.read_text(encoding="utf-8").strip()

    @property
    def origin(self):
        return {"app_id": self.app_id, "data_space_id": self.space_id}

    def _auth(self):
        return {"Authorization": "Bearer " + self.token} if self.token else {}

    def _locate(self):
        if self._selected:
            return self._selected
        for base in self.urls:
            try:
                request = urllib.request.Request(base + "/api/hoard-copilot/capabilities", headers=self._auth())
                with self._opener.open(request, timeout=3) as response:
                    if response.status == 200 and json.load(response).get("contract_version"):
                        self._selected = base
                        return base
            except urllib.error.HTTPError as error:
                error.close()
            except (OSError, ValueError, AttributeError):
                continue
        raise ConnectionError("Faustus copilot is unavailable on the configured endpoints")

    @staticmethod
    def inject_html(html):
        """Inject the shared launcher into uncompressed HTML bytes, idempotently."""
        return inject_launcher(html)

    @staticmethod
    def _reply(handler, status, body, content_type="application/json", headers=None):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    def _relay(self, handler, target, *, method="GET", body=None,
               content_type="application/json", headers=None):
        base = self._locate()
        request = urllib.request.Request(base + target, data=body, method=method,
            headers={**self._auth(), **(headers or {}), "Content-Type": content_type})
        try:
            upstream = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            upstream = error  # Preserve Faustus's status and body, including 4xx/5xx.
        except OSError as error:
            self._selected = None
            raise ConnectionError("Faustus copilot connection failed") from error
        with upstream:
            handler.send_response(upstream.status)
            for name in ("Content-Type", "X-Odysseus-Run-Id", "X-Faustus-Idempotent-Replay", "X-Faustus-Api-Version"):
                value = upstream.headers.get(name)
                if value:
                    handler.send_header(name, value)
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "close")
            if "text/event-stream" in upstream.headers.get("Content-Type", ""):
                handler.send_header("X-Accel-Buffering", "no")
            handler.end_headers()
            handler.close_connection = True
            if handler.command == "HEAD":
                return
            try:
                while chunk := upstream.read1(65536):
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
            except (OSError, ValueError):
                # The headers already travelled. Close the stream; never retry a turn.
                handler.close_connection = True

    def _proxy(self, handler):
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 0:
            raise ValueError("Invalid request length")
        envelope = json.loads(handler.rfile.read(length))
        target = envelope.get("path", "")
        route = urlsplit(target)
        allowed = route.path.startswith(("/api/hoard-copilot/", "/api/chat/", "/api/session/", "/api/sessions/"))
        allowed |= route.path in ("/api/chat_stream", "/api/session", "/api/sessions")
        if not isinstance(target, str) or not target.startswith("/") or route.scheme or route.netloc or route.fragment or not allowed:
            raise ValueError("Unknown copilot operation")
        body = None
        content_type = envelope.get("content_type", "application/json")
        if "raw_body" in envelope:
            body = multipart_context(base64.b64decode(envelope["raw_body"], validate=True), content_type, self.origin)
        elif "body" in envelope:
            payload = envelope["body"]
            if isinstance(payload, dict):
                for field in ("context", "hoard_context"):
                    if isinstance(payload.get(field), dict):
                        existing = payload[field].get("origin")
                        payload[field]["origin"] = {**(existing if isinstance(existing, dict) else {}), **self.origin}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        headers = {name: value for name, value in (envelope.get("headers") or {}).items()
                   if name.lower() in ("x-faustus-client-version", "x-odysseus-run-id") and isinstance(value, str)}
        self._relay(handler, target, method=str(envelope.get("method", "GET")).upper(),
                    body=body, content_type=content_type, headers=headers)

    def handle(self, handler):
        """Serve GET/HEAD info, panel and launcher, and POST proxy; otherwise False."""
        route = urlsplit(handler.path).path
        if route not in ("/api/copilot/info", "/api/copilot/panel.js", "/api/copilot/launcher.js", "/api/copilot/proxy"):
            return False
        expected = ("POST",) if route.endswith("/proxy") else ("GET", "HEAD")
        if handler.command not in expected:
            self._reply(handler, 405, b'{"error":"Method not allowed"}', headers={"Allow": ", ".join(expected)})
            return True
        try:
            if route.endswith("/info"):
                base = self._locate()
                info = {**self.origin, "app_name": self.app_name, "panel_url": "/api/copilot/panel.js",
                        "server": "/", "tool_server_id": self.app_id, "settings_url": base + "/studio/settings"}
                self._reply(handler, 200, json.dumps(info, ensure_ascii=False).encode("utf-8"))
            elif route.endswith("/launcher.js"):
                from .copilot_launcher import LAUNCHER_JS
                self._reply(handler, 200, LAUNCHER_JS.encode("utf-8"), "application/javascript", {"Cache-Control": "no-cache"})
            elif route.endswith("/panel.js"):
                self._relay(handler, "/api/hoard-copilot/panel.js", method=handler.command)
            else:
                self._proxy(handler)
        except ConnectionError as error:
            self._reply(handler, 503, json.dumps({"error": str(error), "code": "copilot_unavailable"}).encode())
        except (ValueError, TypeError, AttributeError):
            self._reply(handler, 400, b'{"error":"Invalid copilot transport request","code":"bad_request"}')
        return True
