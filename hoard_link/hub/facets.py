"""Facets: self-contained parts of the hub that bring their own routes, tools and UI.

The hub's core (apps, events, rules, jobs, backups, leases, repos) is wired by
hand in ``core.py`` / ``server.py`` / ``tools.py``. Everything added in 0.7 —
notifications, spheres, the mail gateway, chat sources, Today and the family
calendar, references between apps, the global search and the jobs view — is a
*facet*: one module with one class that

* is built once with the :class:`~hoard_link.hub.core.Hub` (``Facet(hub)``),
  may start background threads in :meth:`Facet.start` and stops them in
  :meth:`Facet.close`;
* answers its own HTTP routes through :meth:`Facet.get` / :meth:`Facet.post`
  (return ``None`` when the path is not yours);
* lists its agent tools in :meth:`Facet.tools` (same shape as
  ``tools.catalogue()``) and runs them in :meth:`Facet.handlers`;
* names the UI scripts it ships in ``hoard_link/hub/ui/`` (``ui_scripts``);
  ``/api/facets`` tells the page which ones to load.

A facet that fails to build is logged and skipped: the rest of the hub runs.

A route handler gets a :class:`Request` (path, query, body, ``caller()``)
and returns a :class:`Reply` or a plain ``dict`` (sent as JSON, status 200
unless the dict says ``ok: False`` and carries ``status``).
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("hoard_hub.facets")

#: Facet modules, in load order (later facets may use earlier ones through ``hub.facet(id)``).
FACET_MODULES = (
    "notify",
    "spheres",
    "refs",
    "mailgate",
    "chats",
    "today",
    "search",
    "worktrack",
    "purchases",
)


@dataclass
class Reply:
    """A non-default answer: another status, or raw bytes (an .ics file)."""

    payload: Any = None
    status: int = 200
    body: Optional[bytes] = None
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    body: dict[str, Any]
    caller: Callable[[], Optional[str]]       # family caller: app id, "hub", "ui" or None
    agent: Callable[[], bool]                 # True when the hub's own bearer token was sent
    headers: Any = None

    def q(self, key: str, default: Any = None) -> Any:
        v = self.query.get(key)
        return v[0] if v else default

    def q_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.q(key, default))
        except (TypeError, ValueError):
            return default

    def q_bool(self, key: str, default: bool = False) -> bool:
        v = self.q(key)
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")


class Facet:
    """Base class. Override what you need."""

    id = "facet"
    ui_scripts: tuple[str, ...] = ()

    def __init__(self, hub: Any):
        self.hub = hub

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get(self, req: Request) -> Optional[Any]:
        return None

    def post(self, req: Request) -> Optional[Any]:
        return None

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return []

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {}

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "ui_scripts": list(self.ui_scripts)}


def _facet_class(module: Any) -> Optional[type]:
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Facet) and value is not Facet and value.__module__ == module.__name__:
            return value
    return None


def load_all(hub: Any, modules: tuple[str, ...] = FACET_MODULES) -> list[Facet]:
    out: list[Facet] = []
    hub._facets_by_id = {}
    for name in modules:
        try:
            mod = importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.endswith(f".{name}"):
                continue              # not shipped in this build
            logger.exception("facet %s failed to import", name)
            continue
        except Exception:  # noqa: BLE001
            logger.exception("facet %s failed to import", name)
            continue
        cls = _facet_class(mod)
        if cls is None:
            continue
        try:
            facet = cls(hub)
        except Exception:  # noqa: BLE001
            logger.exception("facet %s failed to build", name)
            continue
        out.append(facet)
        hub._facets_by_id[facet.id] = facet
    return out


def start_all(facets: list[Facet]) -> None:
    for f in facets:
        try:
            f.start()
        except Exception:  # noqa: BLE001
            logger.exception("facet %s failed to start", f.id)


def close_all(facets: list[Facet]) -> None:
    for f in reversed(facets):
        try:
            f.close()
        except Exception:  # noqa: BLE001
            logger.exception("facet %s failed to close", f.id)


def dispatch(facets: list[Facet], req: Request) -> Optional[Any]:
    if req.method == "GET" and req.path == "/api/facets":
        return {"ok": True, "facets": [f.info() for f in facets]}
    for f in facets:
        fn = f.get if req.method == "GET" else f.post
        res = fn(req)
        if res is not None:
            return res
    return None


def catalogue() -> list[dict[str, Any]]:
    """Every facet's tools, without a hub (the MCP bridge lists tools before it has one)."""
    out: list[dict[str, Any]] = []
    for name in FACET_MODULES:
        try:
            mod = importlib.import_module(f"{__package__}.{name}")
        except Exception:  # noqa: BLE001
            continue
        cls = _facet_class(mod)
        if cls is not None:
            try:
                out.extend(cls.tools())
            except Exception:  # noqa: BLE001
                logger.exception("facet %s tools() failed", name)
    return out


def handlers(facets: list[Facet]) -> dict[str, Callable[[dict[str, Any]], Any]]:
    out: dict[str, Callable[[dict[str, Any]], Any]] = {}
    for f in facets:
        try:
            out.update(f.handlers())
        except Exception:  # noqa: BLE001
            logger.exception("facet %s handlers() failed", f.id)
    return out
