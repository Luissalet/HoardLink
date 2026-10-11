"""Transport to the shared Faustus agent. No model, history or tool loop."""
from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class CopilotProxy:
    def __init__(self, urls, token=None):
        self.urls = [url.rstrip("/") for url in urls]
        self.token = token
        self._url = None
        self._lock = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def resolve(self):
        with self._lock:
            if self._url:
                return self._url
            for url in self.urls:
                try:
                    request = urllib.request.Request(url + "/api/hoard-copilot/capabilities", headers=self.headers())
                    with self.opener.open(request, timeout=2) as response:
                        body = json.load(response)
                        if body.get("contract_version"):
                            self._url = url
                            return url
                except (OSError, ValueError, urllib.error.URLError):
                    continue
        raise ConnectionError("Faustus copilot is unavailable on the configured endpoints")

    def open(self, caller, envelope):
        if not isinstance(envelope, dict):
            raise ValueError("The copilot request must be an object")
        path = envelope.get("path", "")
        if not isinstance(path, str):
            raise ValueError("Copilot operation path must be a string")
        route = urlsplit(path).path
        prefixes = ("/api/hoard-copilot/", "/api/chat/", "/api/session/", "/api/sessions/")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or (not route.startswith(prefixes) and route not in ("/api/chat_stream", "/api/session", "/api/sessions")):
            raise ValueError("Unknown Faustus copilot operation path")
        method = str(envelope.get("method", "GET")).upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("Unsupported copilot request method")
        headers = {**self.headers(), "X-Hoard-Caller": caller}
        content_type = envelope.get("content_type") or "application/json"
        headers["Content-Type"] = content_type
        for name in ("X-Faustus-Client-Version", "X-Odysseus-Run-Id"):
            value = (envelope.get("headers") or {}).get(name)
            if value:
                headers[name] = str(value)
        data = None
        if "raw_body" in envelope:
            data = base64.b64decode(envelope["raw_body"], validate=True)
        elif "body" in envelope:
            body = envelope["body"]
            if isinstance(body, dict) and caller not in ("hub", "ui"):
                body = dict(body)
                for field in ("context", "hoard_context"):
                    if isinstance(body.get(field), dict):
                        body[field] = {**body[field], "origin": {**body[field].get("origin", {}), "app_id": caller}}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.resolve() + path, data=data, headers=headers, method=method)
        try:
            # This is an observer timeout, not an execution cap; native runs are detached.
            return self.opener.open(request, timeout=900)
        except urllib.error.HTTPError as response:
            return response
        except urllib.error.URLError:
            self._url = None
            raise


def proxy_for(hub):
    if getattr(hub, "copilot", None) is None:
        from hoard_link.config import LinkConfig
        hub.copilot = CopilotProxy(hub.config.faustus_urls, LinkConfig.load().faustus_token)
    return hub.copilot
