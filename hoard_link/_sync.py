"""A synchronous facade for apps whose own code is not async.

Runs a private event loop in a background thread and forwards every call
through :func:`asyncio.run_coroutine_threadsafe`. One facade per
:class:`~hoard_link.link.Link`; the loop is started lazily on first use.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .link import Link


class SyncFacade:
    def __init__(self, link: "Link"):
        self._link = link
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()
            box: dict[str, asyncio.AbstractEventLoop] = {}

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                box["loop"] = loop
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=runner, daemon=True, name="hoard-link-sync")
            thread.start()
            ready.wait()
            self._loop = box["loop"]
            self._thread = thread
            return self._loop

    def _run(self, coro: Any) -> Any:
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def resolve(self, capability: str):
        return self._run(self._link.resolve(capability))

    def chat(self, *args: Any, **kwargs: Any):
        return self._run(self._link.chat(*args, **kwargs))

    def embed(self, *args: Any, **kwargs: Any):
        return self._run(self._link.embed(*args, **kwargs))

    def tts(self, *args: Any, **kwargs: Any):
        return self._run(self._link.tts(*args, **kwargs))

    def status(self):
        return self._run(self._link.status())

    def wait_idle(self, *args: Any, **kwargs: Any):
        return self._run(self._link.wait_idle(*args, **kwargs))

    def close(self, timeout: float = 2.0) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
