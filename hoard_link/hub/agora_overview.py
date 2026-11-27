"""Build and share incremental console snapshots outside the request threads."""

from collections import OrderedDict
from dataclasses import dataclass, replace
import logging
import threading
import time
from uuid import uuid4

from .agora_http import AgoraBusy, AgoraWorkPool

logger = logging.getLogger("hoard_hub.agora_overview")

REFRESH_S = 5.0
MAX_IN_FLIGHT = 8
MAX_VIEWS = 32


@dataclass(frozen=True)
class _Snapshot:
    expires: float
    checked_at: float
    revision: str
    data: dict
    previous: str | None
    delta: dict


class OverviewSnapshots:
    def __init__(self):
        self._pool = AgoraWorkPool("snapshots", workers=2, capacity=MAX_IN_FLIGHT)
        self._lock = threading.Lock()
        self._running = set()
        self._views = OrderedDict()

    def read(self, key, build, since, refresh):
        now = time.monotonic()
        with self._lock:
            sample = self._views.get(key)
            if sample is not None:
                self._views.move_to_end(key)
            if key not in self._running and (sample is None or refresh or sample.expires <= now):
                self._running.add(key)
                try:
                    self._pool.submit(self._update, key, build, sample)
                except AgoraBusy:
                    self._running.discard(key)
            pending = key in self._running or sample is None

        if sample is None:
            return {"ok": True, "view": key[1], "revision": None, "pending": pending, "reset": True,
                    "checked_at": None}
        is_previous = bool(since) and since == sample.previous
        metadata = {"ok": True, "view": key[1], "revision": sample.revision, "pending": pending,
                    "checked_at": sample.checked_at, "reset": since != sample.revision and not is_previous}
        if since == sample.revision:
            return metadata
        return {**(sample.delta if is_previous else sample.data), **metadata}

    def _update(self, key, build, old):
        try:
            data = build()
            checked_at = data.pop("checked_at")
            data.pop("ok", None)
            expires = time.monotonic() + REFRESH_S
            if old is not None and data == old.data:
                sample = replace(old, expires=expires, checked_at=checked_at)
            else:
                before = old.data if old is not None else {}
                delta = {k: v for k, v in data.items() if k not in before or before[k] != v}
                previous = old.revision if old is not None else None
                sample = _Snapshot(expires, checked_at, uuid4().hex, data, previous, delta)
            with self._lock:
                self._views[key] = sample
                self._views.move_to_end(key)
                if len(self._views) > MAX_VIEWS:
                    self._views.popitem(last=False)
        except Exception:
            logger.exception("Could not refresh Agora console snapshot for %s", key)
        finally:
            with self._lock:
                self._running.remove(key)

    def close(self):
        self._pool.close()
