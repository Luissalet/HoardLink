"""Probe the app catalog without treating a slow server as a stopped app."""
from __future__ import annotations

import socket
import time
from urllib.parse import urlsplit

from .contract import app_tools


def _accepts_connection(url):
    try:
        address = urlsplit(url)
        with socket.create_connection((address.hostname, address.port or (443 if address.scheme == "https" else 80)),
                                      timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def probe_catalog(app, *, slow_s=2.0, timeout_s=8.0):
    started = time.monotonic()
    result = app_tools(app, timeout=timeout_s)
    elapsed = round(time.monotonic() - started, 3)
    status = result.get("status", 200 if result["ok"] else None)
    if result["ok"]:
        state = "slow" if elapsed >= slow_s else "ready"
    elif result.get("transport") == "timeout" and _accepts_connection(app.url):
        state = "slow"
    else:
        state = "catalog_error" if status is not None else "unreachable"
    return {
        "app": app.id, "reachable": result["ok"], "responding": status is not None,
        "state": state, "status": status, "elapsed_s": elapsed,
        "contract": result["contract"], "tool_count": len(result.get("tools", [])),
        "error": "not reachable" if state == "unreachable" else result.get("error"),
    }
