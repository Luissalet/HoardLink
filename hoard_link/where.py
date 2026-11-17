"""Where the family's movable apps answer right now.

The Hub's "Run on" control moves Faustus, Hermes and Prometheus to a Spark so the PC can be switched off. A Hoard on the
PC that keeps calling ``http://127.0.0.1:7000`` then reaches nothing. This module asks the Hub (``GET /api/placement``),
remembers the answer for a few seconds and adds the placed address after the configured ones, so an app that is off on
this PC (it refuses at once) is found on the Spark, and nothing changes when the Hub is silent or the app is on the PC.

Standard library only, so the stdlib copilot transport and the sync config helpers can use it. The JS twin is
``js/hoard-commons/placement.js``.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

DEFAULT_HUB_URL = "http://127.0.0.1:8810"
FRESH_S = 15.0       # a good answer is reused this long
SILENT_S = 10.0      # a silent Hub is not asked again for this long (the reserve is used meanwhile)
TIMEOUT_S = 1.5

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def hub_url(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("HOARD_HUB_URL") or DEFAULT_HUB_URL).strip().rstrip("/")


def fetch(hub: Optional[str] = None, *, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """The Hub's placement document, or ``{}`` when it does not answer."""
    request = urllib.request.Request(hub_url(hub) + "/api/placement", headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def current(hub: Optional[str] = None, *, fetcher: Optional[Callable[[], dict[str, Any]]] = None,
            now: Optional[float] = None) -> dict[str, Any]:
    """The placement document through a short cache. ``fetcher`` replaces the HTTP call (the Hub passes its own state)."""
    key = hub_url(hub)
    moment = time.monotonic() if now is None else now
    with _lock:
        saved = _cache.get(key)
        if saved and saved[0] > moment:
            return saved[1]
    data = (fetcher or (lambda: fetch(key)))() or {}
    with _lock:
        _cache[key] = (moment + (FRESH_S if data else SILENT_S), data)
    return data


def forget() -> None:
    """Drop the remembered answers (after a failed call, or in tests)."""
    with _lock:
        _cache.clear()
        _healthy.clear()


def placed(app_id: str, document: Optional[dict[str, Any]] = None, *, hub: Optional[str] = None) -> str:
    """The address ``app_id`` answers on when the Hub put it on a Spark, else ``""``. Nothing while it is moving."""
    data = current(hub) if document is None else document
    if data.get("location") != "sparks" or (data.get("job") or {}).get("running"):
        return ""
    url = (data.get("urls") or {}).get(app_id)
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url.rstrip("/")
    return ""


def with_placement(app_id: str, configured: Iterable[str], document: Optional[dict[str, Any]] = None, *,
                   hub: Optional[str] = None) -> list[str]:
    """``configured`` followed by the address the Hub placed ``app_id`` on. The configured addresses are tried first:
    an app that is off on this PC refuses at once, so the placed address costs nothing extra, and an address somebody
    pinned on purpose (a test Faustus, another machine) keeps winning."""
    urls = [str(url).strip().rstrip("/") for url in configured if str(url).strip()]
    where = placed(app_id, document, hub=hub)
    return urls if not where or where in urls else urls + [where]


def faustus_urls(configured: Iterable[str], document: Optional[dict[str, Any]] = None, *,
                 hub: Optional[str] = None) -> list[str]:
    return with_placement("faustus", configured, document, hub=hub)


def app_url(app_id: str, configured: str = "", *, hub: Optional[str] = None) -> str:
    """Where one movable app answers: the placed address, else ``configured``."""
    return placed(app_id, hub=hub) or str(configured or "").rstrip("/")


_healthy: dict[tuple[str, ...], tuple[float, str]] = {}


def reachable(candidates: Iterable[str], *, token: str = "", timeout: float = 1.5, remote_timeout: float = 8.0,
              now: Optional[float] = None) -> str:
    """The first address whose ``/api/health`` reports a healthy Faustus, else ``""``. A synchronous answer for callers
    that need one origin (the global-model descriptor); remembered for ``FRESH_S`` seconds per candidate list."""
    urls = tuple(str(url).strip().rstrip("/") for url in candidates if str(url).strip())
    moment = time.monotonic() if now is None else now
    with _lock:
        saved = _healthy.get(urls)
        if saved and saved[0] > moment:
            return saved[1]
    headers = {"Authorization": "Bearer " + token} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    found = ""
    for url in urls:
        local = (urlsplit(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
        try:
            with opener.open(urllib.request.Request(url + "/api/health", headers=headers),
                             timeout=timeout if local else remote_timeout) as response:
                if response.status == 200 and (json.load(response) or {}).get("status") == "healthy":
                    found = url
                    break
        except (OSError, ValueError, AttributeError):
            continue
    with _lock:
        _healthy[urls] = (moment + (FRESH_S if found else SILENT_S), found)
    return found
