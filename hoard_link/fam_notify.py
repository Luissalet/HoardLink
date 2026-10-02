"""Tell the person through the hub — from inside an app.

The hub (facet ``notify``) decides how the person is told: Windows toast, ntfy, Telegram or mail,
by the priority and the sphere, with quiet hours, duplicate and rate limits. An app calls
:func:`notify` instead of carrying its own channel code, and keeps that code only as the fallback
for when the hub is unreachable (``notify`` then answers ``{"ok": False, "error": "hub unreachable"}``)::

    from .hoard_link import fam_notify
    res = fam_notify.notify("Payment failed", "Netflix 12.99 EUR", priority="high", url=url,
                            group="payment", dedupe_key=f"pay:{tx_id}")
    if not res.get("ok") and res.get("error") == "hub unreachable":
        own_toast(...)          # the app's own channel

Standard library only; configure the app first (``family.configure`` or ``family.install_fastapi``)
so the request carries the app's own token.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from . import family as _f
from ._hubclient import fetch

CACHE_S = 30.0
_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()


def notify(title: str, body: str = "", *, priority: str = "normal", url: str = "", group: str = "",
           dedupe_key: str = "", sphere: Optional[str] = None, timeout: float = 5.0) -> dict[str, Any]:
    """Ask the hub to notify the person (blocking POST). Returns the hub's answer
    (``{ok, id, held, channels, delivered, ...}``; ``held`` says why nothing was pushed), or
    ``{"ok": False, "error": "hub unreachable"}`` when nothing answered."""
    payload: dict[str, Any] = {"title": str(title or ""), "body": str(body or ""), "priority": str(priority or "normal")}
    for key, value in (("url", url), ("group", group), ("dedupe_key", dedupe_key), ("sphere", sphere)):
        if value:
            payload[key] = str(value)
    status, data = _f._post("/api/notify", payload, timeout)
    if status is None:
        with _lock:
            _cache.pop(_f._hub(), None)
        return {"ok": False, "error": "hub unreachable"}
    if isinstance(data, dict):
        data.setdefault("ok", 200 <= status < 300)
        if status == 401:
            data["error"] = "the hub refused this app's token (" + (_f.status().get("token_file") or "no token file") + ")"
        data.setdefault("status", status)
        return data
    return {"ok": 200 <= status < 300, "status": status, **({} if 200 <= status < 300 else {"error": f"HTTP {status}"})}


def hub_available(timeout: float = 1.0) -> bool:
    """True when a hub with the notification facet answers. Cached for 30 seconds."""
    base = _f._hub()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(base)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    status, data = fetch(base + "/api/notify?limit=1", timeout=timeout)
    ok = status == 200 and isinstance(data, dict) and data.get("ok") is True
    with _lock:
        _cache[base] = (now, ok)
    return ok


def _reset_cache() -> None:
    with _lock:
        _cache.clear()
