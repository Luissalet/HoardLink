"""Serve the last app scan without making HTTP handlers wait for probes."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import threading
import time

from . import procs

logger = logging.getLogger("hoard_hub.directory")


@dataclass(frozen=True)
class _Sample:
    epoch: object
    expires: float
    catalog: dict
    listing: dict
    apps: dict


class AppDirectory:
    """Refresh on demand, with one in-flight scan and a catalog available at startup."""

    MAX_AGE_S = 5.0

    def __init__(self, hub):
        self.hub = hub
        self._latest = None
        self._wanted = threading.Event()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._refresh, name="hoard-hub-directory", daemon=True)

    def start(self):
        self._wanted.set()
        self._thread.start()

    def close(self):
        self._closed.set()
        self._wanted.set()

    def _refresh(self):
        while True:
            self._wanted.wait()
            self._wanted.clear()
            if self._closed.is_set():
                return
            epoch = self.hub._snapshot_epoch
            latest = self._latest
            if latest is not None and not self._stale(latest, epoch):
                continue
            try:
                catalog = {app.id: (app, app.url) for app in self.hub.apps}
                data = self.hub.snapshot()
                checked_at = time.time()
                rows = {row["id"]: {**row, "status_checked_at": checked_at, "status_stale": False}
                        for row in data["apps"]}
                listing = {**data, "apps": list(rows.values()), "status_checked_at": checked_at, "status_stale": False}
                if epoch is self.hub._snapshot_epoch:
                    self._latest = _Sample(epoch, time.monotonic() + self.MAX_AGE_S, catalog, listing, rows)
            except Exception:
                logger.exception("Could not refresh the app directory")

    @staticmethod
    def _stale(sample, epoch):
        return sample.epoch is not epoch or time.monotonic() >= sample.expires

    def _sample(self):
        latest = self._latest
        stale = latest is None or self._stale(latest, self.hub._snapshot_epoch)
        if stale:
            self._wanted.set()
        return latest, stale

    def _unobserved(self, app):
        state = "down" if self.hub.config.isolated else "unknown"
        return {**app.to_dict(check_icon=False), "state": state, "health": procs.Health(state).to_dict(),
                "process": None, "windows": [], "stoppable": False, "manually_stopped": False,
                "log": os.path.join(self.hub.config.logs_dir, f"{app.id}.log"), "status_checked_at": None,
                "keep_alive": False, "status_stale": True}

    def list(self):
        recent, stale = self._sample()
        if recent is not None:
            data = recent.listing
            if stale:
                apps = [self._status(app, recent, stale) for app in self.hub.apps]
                data = {**data, "apps": apps, "status_stale": True,
                        "counts": {**data["counts"], "total": len(apps),
                                   "running": sum(app["state"] == "running" for app in apps)}}
            return {**data, "hub": {**data["hub"], "uptime_s": int(time.time() - self.hub.started_at),
                                   "events": self.hub.events.last_id}}
        apps = [self._unobserved(app) for app in self.hub.apps]
        faustus = {"reachable": False, "url": None, "status": None, "body": None, "available": False,
                   "startable": False, "stoppable": False, "manually_stopped": False}
        return {**self.hub._snapshot_data(apps, {}, faustus, sampled=False),
                "status_checked_at": None, "status_stale": True}

    def status(self, app):
        recent, stale = self._sample()
        return self._status(app, recent, stale)

    def _status(self, app, recent, stale):
        if recent is not None:
            observed = recent.catalog.get(app.id)
            row = recent.apps.get(app.id)
            if observed is not None and row is not None and observed[0] is app and observed[1] == app.url:
                return {**row, "status_stale": True} if stale else row
        return self._unobserved(app)
