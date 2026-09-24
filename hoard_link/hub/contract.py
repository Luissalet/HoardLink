"""Talking to the apps on the family contract, from the hub.

Every app ships ``GET /api/agent/tools`` and ``POST /api/agent/call``
guarded by a bearer token in its own ``data/mcp-token``. The hub can read
every one of those files (they are next to the manifests it scans), so it
is the one place that can call *any* app on behalf of *any other* without
the caller knowing ports or tokens: ``POST /api/apps/<id>/call`` in
``server.py`` is that proxy, and ``rules.py`` / ``jobs.py`` use the same
function to run their actions.

Tokens are read at call time (a Node app writes a fresh one on every
start) with a short cache, and never leave this process: the proxy takes
the *caller's* own token to authenticate (any app's, or the hub's) and
uses the *target's* token towards the target.

The first six apps of the family (Babel, Laplace, Funes, Daguerre,
Prospero, Scheherazade) predate the shared ``/api/agent/call`` route and
expose one ``POST /api/agent/<tool>`` per tool with no token. ``call_app``
falls back to that shape when the shared route is missing, so a rule can
target them too; the audit in ``audit.py`` reports which shape each app
speaks so the gap is visible instead of silent.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from . import procs
from .registry import App

TOKEN_CACHE_S = 3.0
_token_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def read_token(path: str) -> str:
    """The token in ``path`` right now ('' when there is none)."""
    if not path:
        return ""
    now = time.monotonic()
    with _lock:
        hit = _token_cache.get(path)
        if hit and now - hit[0] < TOKEN_CACHE_S:
            return hit[1]
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            tok = fh.read().strip()
    except OSError:
        tok = ""
    with _lock:
        _token_cache[path] = (now, tok)
    return tok


def token_owner(token: str, apps: list[App], hub_token: str) -> Optional[str]:
    """Which app (id) the bearer ``token`` belongs to; ``"hub"`` for the
    hub's own; None when nobody's."""
    if not token:
        return None
    if token == hub_token:
        return "hub"
    for app in apps:
        tok = read_token(app.token_file)
        if tok and tok == token:
            return app.id
    return None


def _post(url: str, body: dict[str, Any], token: str, timeout: float) -> tuple[Optional[int], Any]:
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body, default=str).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "hoard-hub", **({"Authorization": "Bearer " + token} if token else {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return None, {"error": f"{type(exc).__name__}: {exc}"}
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, {"error": raw.decode("utf-8", "replace")[:500]}


def _get(url: str, token: str, timeout: float) -> tuple[Optional[int], Any]:
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hoard-hub",
                                               **({"Authorization": "Bearer " + token} if token else {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception:  # noqa: BLE001
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, None


def app_tools(app: App, timeout: float = 5.0) -> dict[str, Any]:
    """The app's tool catalogue (``/api/agent/tools``), or why not."""
    token = read_token(app.token_file)
    status, body = _get(app.url + "/api/agent/tools", token, timeout)
    if status is None:
        return {"ok": False, "app": app.id, "error": "not reachable", "contract": "unknown"}
    if status == 200 and isinstance(body, dict) and isinstance(body.get("tools"), list):
        return {"ok": True, "app": app.id, "contract": "shared", "tools": body["tools"],
                "instructions": body.get("instructions", "")}
    return {"ok": False, "app": app.id, "contract": "per-tool" if status == 404 else "unknown",
            "status": status, "error": f"/api/agent/tools answered {status}"}


def call_app(app: App, tool: str, arguments: Optional[dict[str, Any]] = None, *, timeout: float = 120.0,
             caller: str = "hub") -> dict[str, Any]:
    """Run ``tool`` on ``app`` with its own token. Result:
    ``{ok, app, tool, status, result|error, contract, ms}``."""
    tool = str(tool or "").strip()
    args = arguments if isinstance(arguments, dict) else {}
    if not tool:
        return {"ok": False, "app": app.id, "error": "tool name is required"}
    token = read_token(app.token_file)
    t0 = time.monotonic()
    status, body = _post(app.url + "/api/agent/call", {"name": tool, "tool": tool, "arguments": args, "caller": caller},
                         token, timeout)
    contract = "shared"
    if status == 404 and not (isinstance(body, dict) and _is_unknown_tool(body)):
        # No shared route: the per-tool shape of the first six apps.
        contract = "per-tool"
        status, body = _post(app.url + f"/api/agent/{tool}", args, token, timeout)
    ms = int((time.monotonic() - t0) * 1000)
    if status is None:
        return {"ok": False, "app": app.id, "tool": tool, "status": None, "contract": contract, "ms": ms,
                "error": (body or {}).get("error", "not reachable") if isinstance(body, dict) else "not reachable"}
    ok = 200 <= status < 300
    out: dict[str, Any] = {"ok": ok, "app": app.id, "tool": tool, "status": status, "contract": contract, "ms": ms}
    if ok:
        # Some apps wrap ({ok, tool, result}), most answer the tool's value directly.
        if isinstance(body, dict) and "result" in body and set(body) <= {"ok", "tool", "result", "name"}:
            out["result"] = body["result"]
            if body.get("ok") is False:
                out["ok"] = False
                out["error"] = _error_text(body["result"])
        else:
            out["result"] = body
    else:
        out["error"] = _error_text(body) or f"HTTP {status}"
        if status == 401:
            out["error"] = "the app refused the token in " + app.token_file + " (restart the app or the hub?)"
    return out


def _is_unknown_tool(body: dict[str, Any]) -> bool:
    text = _error_text(body).lower()
    return "tool" in text and ("unknown" in text or "no existe" in text or "not found" in text or "desconoc" in text)


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            v = body.get(key)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                inner = _error_text(v)
                if inner:
                    return inner
        return ""
    if isinstance(body, str):
        return body[:500]
    return ""


def data_dir_size(path: str, *, limit_files: int = 200000) -> dict[str, Any]:
    total, files = 0, 0
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__")]
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                continue
            files += 1
            if files >= limit_files:
                return {"bytes": total, "files": files, "truncated": True}
    return {"bytes": total, "files": files, "truncated": False}


__all__ = ["read_token", "token_owner", "app_tools", "call_app", "data_dir_size", "procs"]
