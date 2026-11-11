"""The request guard every Hoard app runs in front of its API: which ``Host`` values are accepted, which ``Origin``
values may call it and which Fetch Metadata combinations are let through.

Standard library only: the checks are pure functions over plain header mappings (unit-testable, and checked against
the same vectors as the Node twin, ``createGuard`` / ``checkRequest`` in ``js/hoard-commons/express.js``), and
:func:`install_guard` wraps them as a **pure ASGI middleware** (no starlette import at module import; it also guards
WebSocket upgrades, which the 17 copies of ``guard.py`` and the ``BaseHTTPMiddleware`` of the B family never saw).

Rules (the ones the 17 identical ``guard.py`` copies had):

1. ``Host``: ``localhost`` / ``127.0.0.1`` / ``[::1]`` plus the comma-separated list of the app's ``*_ALLOWED_HOSTS``
   variable (exact hostnames or ``*.suffix``, for a LAN name or a tailnet), compared without the port, case-insensitive.
2. ``Origin`` (when present): its host must pass the host rule (any scheme or port), or be one of the dev-server
   origins (Vite on 5173 / 5174 / 4173).
3. Fetch Metadata: cross-site top-level navigations pass. A framed GET navigation (``iframe`` / ``frame``) also
   passes when its Referer origin is a configured Faustus URL or the Hub placement's ``urls.faustus``.
   Placement is fetched lazily in the background and cached for 60 seconds; the current request never waits.
   Unknown parents, ``embed`` / ``object`` and cross-site API calls remain rejected. Requests without
   ``Sec-Fetch-*`` headers (curl, the MCP bridge) pass.
4. State-changing methods are never accepted as navigations (an HTML form post from another page): the API takes JSON
   from ``fetch()`` only.

What the commons add on top, both opt-in so the family keeps one behaviour:

* ``strict_ports=True`` (the B family's rule): a ``Host`` that names a port must name *this app's* port, and a local
  ``Origin`` must carry it too (dev-server origins excepted). ``port`` is ignored otherwise.
* An allowed-hosts entry may pin a port (``"nas.local:8443"``): it then only matches that port.

A rejected request gets ``403 {"error": "<message>"}`` (the envelope the family A frontends read; the B family answered
400 / 403 with ``{"error": code, "message"}``).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any, Callable, Optional, Union
from urllib.parse import urlsplit
from urllib.request import urlopen

__all__ = ["LOCAL_HOSTS", "DEV_ORIGINS", "FRAME_DESTS", "SAFE_METHODS", "host_of", "port_of", "parse_allowed_hosts",
           "is_allowed_host", "check_request", "install_guard", "GuardMiddleware", "check_handler", "wsgi_middleware",
           "FaustusOrigins"]

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
# The Vite dev server (5173 / 5174) and ``vite preview`` (4173), under both spellings of loopback.
DEV_ORIGINS = tuple(f"http://{host}:{port}" for port in (5173, 5174, 4173) for host in ("localhost", "127.0.0.1"))
FRAME_DESTS = frozenset({"iframe", "frame", "embed", "object"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_MSG_HOST = "Only local access is allowed."
_MSG_ORIGIN = "Origin not allowed."
_MSG_SITE = "Cross-site requests are not allowed."
_MSG_FORM = "Form submissions are not allowed."


def _url_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        if url.scheme.lower() not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None:
            return ""
        port = url.port or (443 if url.scheme.lower() == "https" else 80)
        host = url.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        suffix = "" if port == (443 if url.scheme.lower() == "https" else 80) else f":{port}"
        return f"{url.scheme.lower()}://{host}{suffix}"
    except (ValueError, TypeError, AttributeError):
        return ""


class FaustusOrigins:
    """Configured parent origins plus a nonblocking, single-flight Hub placement cache."""

    def __init__(self, *, faustus_urls: Optional[Iterable[str]] = None, hub_url: Optional[str] = None,
                 config_path: Any = None) -> None:
        from .config import LinkConfig
        config = LinkConfig.load(path=config_path)
        urls = config.faustus_urls if faustus_urls is None else faustus_urls
        self.configured = frozenset(filter(None, map(_url_origin, urls)))
        self.hub_url = (hub_url or os.environ.get("HOARD_HUB_URL") or config.hub_url or "http://127.0.0.1:8810").rstrip("/")
        self.discovered: frozenset[str] = frozenset()
        self.expires = 0.0
        self.lock = threading.Lock()

    def _refresh(self) -> None:
        try:
            with urlopen(self.hub_url + "/api/placement", timeout=2) as response:
                payload = json.load(response)
            origin = _url_origin(payload.get("urls", {}).get("faustus", ""))
            self.discovered = frozenset({origin}) if origin else frozenset()
        except Exception:  # A Hub outage leaves the ordinary guard rules in place.
            self.discovered = frozenset()
        finally:
            self.expires = time.monotonic() + 60
            self.lock.release()

    def current(self) -> frozenset[str]:
        now = time.monotonic()
        known = self.discovered if now < self.expires else frozenset()
        if now >= self.expires and self.lock.acquire(blocking=False):
            threading.Thread(target=self._refresh, daemon=True, name="hoard-dock-placement").start()
        return self.configured | known


_default_faustus_origins: Optional[FaustusOrigins] = None


def _dock_origins(method: str, headers: Mapping[str, str], resolver: Optional[FaustusOrigins] = None) -> Iterable[str]:
    global _default_faustus_origins
    if str(method).upper() != "GET" or headers.get("sec-fetch-site") != "cross-site" or headers.get("sec-fetch-mode") != "navigate" or headers.get("sec-fetch-dest") not in ("iframe", "frame"):
        return ()
    if resolver is None:
        if _default_faustus_origins is None:
            _default_faustus_origins = FaustusOrigins()
        resolver = _default_faustus_origins
    return resolver.current()


def _authority(value: Optional[str]) -> str:
    """``host[:port]`` of a Host header, Origin or URL: no scheme, no path, lowercase."""
    text = (value or "").strip().lower()
    scheme = text.find("://")
    if scheme != -1:
        text = text[scheme + 3:]
    text = text.split("/", 1)[0]
    return text


def host_of(value: Optional[str]) -> str:
    """Host part of a Host header, Origin or URL: no scheme, no path, no port, lowercase. An IPv6 literal keeps its
    brackets (``"[::1]:5190"`` gives ``"[::1]"``)."""
    host = _authority(value)
    if host.startswith("["):
        end = host.find("]")
        return host if end == -1 else host[: end + 1]
    return host.split(":", 1)[0]


def port_of(value: Optional[str]) -> Optional[int]:
    """The explicit port of a Host header, Origin or URL, or ``None`` when it names none (or an invalid one)."""
    text = _authority(value)
    if text.startswith("["):
        end = text.find("]")
        rest = "" if end == -1 else text[end + 1:]
    else:
        _, sep, rest = text.partition(":")
        rest = sep + rest if sep else ""
    if not rest.startswith(":"):
        return None
    digits = rest[1:]
    if not digits.isdigit():
        return None
    number = int(digits)
    return number if 1 <= number <= 65535 else None


def parse_allowed_hosts(raw: Union[str, Iterable[str], None]) -> tuple[str, ...]:
    """Parse ``"a.example, *.ts.net, nas.local:8443"`` (or a list of such entries) into a clean tuple of patterns.
    Entries keep an explicit ``:port``; blanks and a bare ``*.`` are dropped."""
    if raw is None:
        return ()
    entries = raw.split(",") if isinstance(raw, str) else [str(e) for e in raw]
    patterns: list[str] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        wildcard = entry.startswith("*.")
        body = entry[2:] if wildcard else entry
        name = host_of(body)
        port = port_of(body)
        if not name:
            continue
        pattern = ("*." if wildcard else "") + name + (f":{port}" if port else "")
        if pattern not in patterns:
            patterns.append(pattern)
    return tuple(patterns)


def _name_matches(name: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return name.endswith(suffix) and len(name) > len(suffix)
    return name == pattern


def _pattern_matches(name: str, explicit_port: Optional[int], pattern: str) -> bool:
    pin: Optional[int] = None
    if pattern.startswith("["):
        pin = port_of(pattern)
        pattern = host_of(pattern)
    elif ":" in pattern:
        pattern, _, digits = pattern.rpartition(":")
        pin = int(digits) if digits.isdigit() else None
    if not _name_matches(name, pattern):
        return False
    if pin is None:
        return True
    return explicit_port == pin or (explicit_port is None and pin in (80, 443))


def is_allowed_host(host: Optional[str], port: Optional[int] = None, allowed: Iterable[str] = (), *,
                    strict_ports: bool = False) -> bool:
    """Is ``host`` (a ``Host`` header such as ``"localhost:5190"``, or a bare hostname) acceptable?

    The name must be loopback or match an ``allowed`` pattern (``"name"``, ``"*.suffix"``, optionally ``":port"``).
    With ``strict_ports`` a port named by ``host`` must equal ``port`` (the app's own); without it ``port`` is not
    consulted, so a dev proxy forwarding ``Host: localhost:5173`` still works."""
    if not host:
        return False
    name = host_of(host)
    if not name:
        return False
    explicit = port_of(host)
    if strict_ports and explicit is not None and port and explicit != int(port):
        return False
    if name in LOCAL_HOSTS:
        return True
    return any(_pattern_matches(name, explicit, pattern) for pattern in allowed)


def _origin_ok(origin: str, port: Optional[int], allowed: tuple[str, ...], dev_origins: Iterable[str], strict_ports: bool) -> bool:
    if origin in dev_origins:
        return True
    name = host_of(origin)
    if strict_ports and name in LOCAL_HOSTS:
        explicit = port_of(origin)
        scheme_default = 443 if origin.strip().lower().startswith("https://") else 80
        return bool(port) and (explicit if explicit is not None else scheme_default) == int(port)
    return is_allowed_host(origin, port, allowed)


def check_request(method: str, headers: Mapping[str, str], port: Optional[int] = None, allowed: Iterable[str] = (), *,
                  dev_origins: Iterable[str] = DEV_ORIGINS, strict_ports: bool = False,
                  guard_safe_methods: bool = True, faustus_origins: Iterable[str] = ()) -> Optional[tuple[int, str]]:
    """``None`` when the request may proceed, otherwise ``(status, message)`` (always 403). ``headers`` is any
    mapping with lowercase names (``host``, ``origin``, ``sec-fetch-site`` / ``-mode`` / ``-dest``)."""
    allowed_t = tuple(allowed)
    host = headers.get("host")
    if not is_allowed_host(host, port, allowed_t, strict_ports=strict_ports):
        return 403, _MSG_HOST
    if not guard_safe_methods and str(method).upper() in SAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin and not _origin_ok(origin, port, allowed_t, tuple(dev_origins), strict_ports):
        return 403, _MSG_ORIGIN
    site = headers.get("sec-fetch-site")
    mode = headers.get("sec-fetch-mode")
    dest = headers.get("sec-fetch-dest")
    parent = _url_origin(headers.get("referer", ""))
    dock = (str(method).upper() == "GET" and mode == "navigate" and dest in ("iframe", "frame")
            and bool(parent) and parent in faustus_origins)
    if site == "cross-site" and (mode != "navigate" or dest in FRAME_DESTS) and not dock:
        return 403, _MSG_SITE
    if mode == "navigate" and str(method).upper() not in SAFE_METHODS:
        return 403, _MSG_FORM
    return None


# ------------------------------------------------------------------------------------------------ ASGI

def check_handler(handler: Any, *, allowed: Iterable[str] = (), strict_ports: bool = False,
                  faustus_origins: Optional[FaustusOrigins] = None) -> bool:
    """Guard an http.server handler. Return True to proceed; send JSON on refusal."""
    headers = {key.lower(): value for key, value in handler.headers.items()}
    port = handler.server.server_address[1]
    verdict = check_request(handler.command, headers, port, allowed, strict_ports=strict_ports,
                            faustus_origins=_dock_origins(handler.command, headers, faustus_origins))
    if verdict is None:
        return True
    status, message = verdict
    body = json.dumps({"error": message}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
    return False


def wsgi_middleware(app: Any, *, port_getter: Callable[[], int], allowed: Iterable[str] = (), strict_ports: bool = False,
                    faustus_origins: Optional[FaustusOrigins] = None) -> Any:
    """Apply the same request rules to a Flask or other WSGI application."""
    patterns = tuple(allowed)

    def guarded(environ: dict[str, Any], start_response: Any) -> Any:
        headers = {key[5:].lower().replace("_", "-"): value for key, value in environ.items() if key.startswith("HTTP_")}
        method = environ.get("REQUEST_METHOD", "GET")
        verdict = check_request(method, headers, port_getter(), patterns, strict_ports=strict_ports,
                                faustus_origins=_dock_origins(method, headers, faustus_origins))
        if verdict is None:
            return app(environ, start_response)
        status, message = verdict
        body = json.dumps({"error": message}).encode("utf-8")
        start_response(f"{status} Forbidden", [("Content-Type", "application/json"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
        return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else body]

    return guarded

def _scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    """The ASGI header list as ``{lowercase name: value}`` (the first of a repeated header wins)."""
    out: dict[str, str] = {}
    for key, value in scope.get("headers") or ():
        name = key.decode("latin-1").lower()
        if name not in out:
            out[name] = value.decode("latin-1")
    return out


class GuardMiddleware:
    """Pure ASGI middleware: ``check_request`` on every ``http`` and ``websocket`` scope (``lifespan`` passes)."""

    def __init__(self, app: Any, *, port_getter: Callable[[], int], allowed: Iterable[str] = (),
                 dev_origins: Iterable[str] = DEV_ORIGINS, strict_ports: bool = False, guard_safe_methods: bool = True,
                 faustus_origins: Optional[FaustusOrigins] = None) -> None:
        self.app = app
        self.port_getter = port_getter
        self.allowed = tuple(allowed)
        self.dev_origins = tuple(dev_origins)
        self.strict_ports = strict_ports
        self.guard_safe_methods = guard_safe_methods
        self.faustus_origins = faustus_origins

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        try:
            port = int(self.port_getter() or 0)
        except Exception:  # noqa: BLE001 - a broken getter must not take the app down; the port is then not enforced
            port = 0
        method = "GET" if kind == "websocket" else scope.get("method", "GET")
        headers = _scope_headers(scope)
        verdict = check_request(method, headers,
                                port, self.allowed, dev_origins=self.dev_origins, strict_ports=self.strict_ports,
                                guard_safe_methods=kind == "websocket" or self.guard_safe_methods,
                                faustus_origins=_dock_origins(method, headers, self.faustus_origins) if kind == "http" else ())
        if verdict is None:
            return await self.app(scope, receive, send)
        status, message = verdict
        if kind == "websocket":
            # Closing before accept makes the server answer the upgrade with 403.
            await receive()
            return await send({"type": "websocket.close", "code": 1008, "reason": message})
        body = json.dumps({"error": message}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii")),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})


def install_guard(app: Any, *, port_getter: Callable[[], int], allowed_env: str = "ALLOWED_HOSTS",
                  allowed_hosts: Union[str, Iterable[str], None] = None, dev_origins: Iterable[str] = DEV_ORIGINS,
                  strict_ports: bool = False, guard_safe_methods: bool = True,
                  faustus_origins: Optional[FaustusOrigins] = None, faustus_urls: Optional[Iterable[str]] = None,
                  hub_url: Optional[str] = None, config_path: Any = None) -> None:
    """Put the guard in front of ``app`` (a FastAPI / Starlette application: ``app.add_middleware``).

    ``port_getter`` returns the port the app really listens on (known after the port search, so it is read per
    request). The allowed hosts are the union of ``allowed_hosts`` (a list or a comma-separated string, e.g.
    ``config.allowed_hosts``) and the comma-separated environment variable named by ``allowed_env`` (the app's
    ``KAFKA_ALLOWED_HOSTS``...), both read once, now."""
    patterns = parse_allowed_hosts(allowed_hosts) + parse_allowed_hosts(os.environ.get(allowed_env) if allowed_env else None)
    if faustus_origins is None and any(value is not None for value in (faustus_urls, hub_url, config_path)):
        faustus_origins = FaustusOrigins(faustus_urls=faustus_urls, hub_url=hub_url, config_path=config_path)
    app.add_middleware(GuardMiddleware, port_getter=port_getter, allowed=tuple(dict.fromkeys(patterns)),
                       dev_origins=tuple(dev_origins), strict_ports=strict_ports, guard_safe_methods=guard_safe_methods,
                       faustus_origins=faustus_origins)
