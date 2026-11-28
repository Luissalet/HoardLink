"""Verified routes and catalogue probes shared by one Hub Link configuration."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from contextvars import Context, ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from .errors import InvalidModel
from .types import Resolution

_ROUTE_REFRESH_S = 15.0
_CATALOGUE_BACKOFF_S = 120.0
_MAX_ENTRIES = 256
_T = TypeVar("_T")
RouteKey = tuple[str, str | None, str | None]


@dataclass
class _Refresh:
    route: Resolution
    removed: bool = False


class ChatRouting:
    """One event loop owns a generation of Hub route and provider state."""

    def __init__(self):
        self.probes: dict[str | tuple, tuple[float, asyncio.Future[Any], Any]] = {}
        self.registry: tuple[dict, float] | None = None
        self.routes: OrderedDict[RouteKey, tuple[float, Resolution]] = OrderedDict()
        self._lookups: dict[RouteKey, asyncio.Task[Resolution]] = {}
        self._unavailable: OrderedDict[str, tuple[float, int | None]] = OrderedDict()
        self._catalogues: dict[str, asyncio.Task[tuple[int | None, Any]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._refreshing: ContextVar[_Refresh | None] = ContextVar("chat_route_refresh", default=None)

    def _task(self, work: Coroutine[Any, Any, _T], *, background: bool = False) -> asyncio.Task[_T]:
        task = asyncio.create_task(work, context=Context() if background else None)
        self._tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        return task

    @staticmethod
    def _remember(cache: OrderedDict, key: Any, value: Any) -> None:
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > _MAX_ENTRIES:
            cache.popitem(last=False)

    async def resolve(
        self, key: RouteKey, lookup: Callable[[], Coroutine[Any, Any, Resolution]], now: Callable[[], float],
    ) -> Resolution:
        entry = self.routes.get(key)
        if entry is not None:
            self.routes.move_to_end(key)
            if now() - entry[0] < _ROUTE_REFRESH_S:
                return entry[1]

        async def refresh() -> Resolution:
            task = asyncio.current_task()
            check = _Refresh(entry[1]) if entry is not None else None
            token = self._refreshing.set(check)
            try:
                result = await lookup()
                if self._lookups.get(key) is task:
                    if result.resolved and result.url and result.details.get("resident") is not False:
                        self._remember(self.routes, key, (now(), result))
                    elif entry is not None and self.routes.get(key) is entry:
                        if check is not None and check.removed:
                            self.routes.pop(key, None)
                        else:
                            self._remember(self.routes, key, (now(), entry[1]))
                return result
            except InvalidModel:
                # A fresh registry can explicitly disallow a once valid model.
                if self._lookups.get(key) is task:
                    self.routes.pop(key, None)
                raise
            finally:
                self._refreshing.reset(token)
                if self._lookups.get(key) is task:
                    self._lookups.pop(key, None)

        task = self._lookups.get(key)
        if task is None:
            task = self._lookups[key] = self._task(refresh(), background=entry is not None)
        if entry is not None:
            return entry[1]
        return await asyncio.shield(task)

    def observe_registry(self, items: list[dict]) -> None:
        check = self._refreshing.get()
        if check is None or check.route.details.get("source") != "faustus_registry":
            return
        for item in items:
            listed = item.get("models")
            if (item["url"].rstrip("/") == check.route.url.rstrip("/")
                    and (not isinstance(listed, list) or not listed or check.route.model in listed)):
                return
        check.removed = True

    def observe_catalogue(self, url: str, models: list[str]) -> None:
        check = self._refreshing.get()
        if check is not None and url.rstrip("/") == check.route.url.rstrip("/") and check.route.model not in models:
            check.removed = True

    def invalidate(self, key: RouteKey, used: Resolution) -> None:
        entry = self.routes.get(key)
        if entry is not None and (entry[1].url, entry[1].model) == (used.url, used.model):
            self.routes.pop(key, None)
            # The refresh may still finish, but cannot publish this route again.
            self._lookups.pop(key, None)

    async def catalogue(
        self, url: str, probe: Callable[[], Coroutine[Any, Any, tuple[int | None, Any]]],
        now: Callable[[], float],
    ) -> tuple[int | None, Any]:
        failure = self._unavailable.get(url)
        if failure is not None:
            self._unavailable.move_to_end(url)
            if now() < failure[0]:
                return failure[1], None

        async def fetch() -> tuple[int | None, Any]:
            try:
                status, data = await probe()
                if status in (401, None):
                    self._remember(self._unavailable, url, (now() + _CATALOGUE_BACKOFF_S, status))
                else:
                    self._unavailable.pop(url, None)
                return status, data
            finally:
                self._catalogues.pop(url, None)

        task = self._catalogues.get(url)
        if task is None:
            task = self._catalogues[url] = self._task(fetch())
        # Route revalidation awaits the retry without delaying use of its cached route.
        return await asyncio.shield(task)
