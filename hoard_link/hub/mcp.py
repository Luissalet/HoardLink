"""``python -m hoard_link.hub.mcp`` — a stdio MCP server for the hub.

Hand-written JSON-RPC over stdio (no SDK, standard library only), the
same shape the rest of the family uses: it never touches the hub's state
itself, every tool call is proxied to ``POST /api/agent/call`` with the
bearer token from ``<data>/mcp-token``. When no hub is listening it
starts one headless (``--no-window``) unless ``HOARD_HUB_AUTOSTART=0``.

Environment: ``HOARD_HUB_URL`` (default ``http://127.0.0.1:8810``),
``HOARD_HUB_TOKEN_FILE``, ``HOARD_HUB_DATA_DIR``, ``HOARD_HUB_AUTOSTART``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from . import DEFAULT_PORT, HUB_VERSION, SERVICE
from .tools import catalogue

PROTOCOL = "2024-11-05"


def _data_dir() -> str:
    from .config import HubConfig
    return HubConfig.load().data_dir


def _url() -> str:
    return (os.environ.get("HOARD_HUB_URL") or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")


def _token() -> str:
    path = os.environ.get("HOARD_HUB_TOKEN_FILE") or os.path.join(_data_dir(), "mcp-token")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _request(method: str, path: str, body: Optional[dict[str, Any]] = None, timeout: float = 90.0) -> tuple[Optional[int], Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + _token()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "null")
        except Exception:  # noqa: BLE001
            return exc.code, None
    except Exception:  # noqa: BLE001
        return None, None


def _hub_up() -> bool:
    status, body = _request("GET", "/api/health", timeout=1.5)
    return status == 200 and isinstance(body, dict) and body.get("service") == SERVICE


def _ensure_hub() -> bool:
    # The same headless auto-start the lease client uses (HOARD_HUB_AUTOSTART=0 turns it off).
    from hoard_link._hubclient import ensure_hub
    return ensure_hub(_url())


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1, default=str)}]}


def handle(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": {"name": "hoard-hub", "version": HUB_VERSION},
            "instructions": "Local desktop hub for a family of agent-controlled apps: list them, see which are running, "
                            "start/stop them, open them as windows, and arbitrate GPU memory (VRAM leases) between them. "
                            "Use hub_list_apps first to learn the ids.",
        }}
    if method == "notifications/initialized" or method == "initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": catalogue()}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not _ensure_hub():
            return {"jsonrpc": "2.0", "id": msg_id, "result": {**_text({"ok": False, "error": f"hub not reachable at {_url()} and could not be started"}), "isError": True}}
        status, body = _request("POST", "/api/agent/call", {"tool": name, "arguments": args})
        if status is None:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {**_text({"ok": False, "error": "hub did not answer"}), "isError": True}}
        result = body.get("result") if isinstance(body, dict) else body
        is_error = status >= 400
        if status == 401:
            result = {"ok": False, "error": "hub refused the token: check HOARD_HUB_TOKEN_FILE / data/mcp-token"}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {**_text(result), "isError": is_error}}
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve_stdio() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        try:
            reply = handle(msg)
        except Exception as exc:  # noqa: BLE001
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": str(exc)}}
        if reply is not None:
            stdout.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


if __name__ == "__main__":
    serve_stdio()
