"""Apps that live as long as the Hub does.

Some apps do their work on a clock of their own (a discovery feed that syncs
every few hours, a link library that downloads in the background, the
paperwork deadlines): when one of them is down nobody notices until the
work it should have done is missing. The keep-alive list names those apps.
The serving Hub starts them when it comes up and starts them again when
they fall, looking every minute.

* The list lives in ``<data>/keep-alive.json`` (``{"apps": [...]}``); with no
  file it is :data:`DEFAULT_APPS`. The page, the HTTP API and the MCP tools
  add or remove apps.
* An app whose manifest says ``auto_start: false`` and the apps in
  :data:`NEVER` are never kept alive: they start only when the person asks.
* A stop made by the person is respected while the Hub runs (the app is not
  started again until someone starts it); a new Hub start brings the list
  back up, which is what "starts with the Hub" means.
* An app on a Spark (placement) or whose port answers as another program is
  left alone. A start that fails waits longer before the next try, up to half
  an hour, so a broken app is not hammered.
* Only the Hub that serves (``python -m hoard_link.hub``) runs the watch: a
  Hub object built by a test or a tool never starts anything. The variable
  ``HOARD_HUB_KEEP_ALIVE=0`` turns the watch off for a whole process (a test
  Hub on another port).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

from ..atomic import write_json_atomic
from . import procs
from .facets import Facet, Request

logger = logging.getLogger("hoard_hub.keepalive")

DEFAULT_APPS = ("darwin", "links", "talos", "kafka")
NEVER = frozenset({"baird"})
CHECK_S = 60.0
BOOT_DELAY_S = 3.0
BACKOFF_MAX_S = 1800.0
FILE_NAME = "keep-alive.json"


def disabled_by_env(env: Optional[dict[str, str]] = None) -> bool:
    value = (env if env is not None else os.environ).get("HOARD_HUB_KEEP_ALIVE", "")
    return value.strip().lower() in ("0", "off", "false", "no")


class KeepAliveFacet(Facet):
    id = "keepalive"

    def __init__(self, hub: Any):
        super().__init__(hub)
        self.path = os.path.join(hub.config.data_dir, FILE_NAME)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: dict[str, dict[str, Any]] = {}
        self._last_check: Optional[float] = None
        self._apps = self._load()

    # -- the list -----------------------------------------------------------------------------------------------
    def _load(self) -> list[str]:
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            apps = raw.get("apps") if isinstance(raw, dict) else None
            if isinstance(apps, list):
                return self._clean(apps)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("keep-alive list unreadable at %s; using the default list", self.path)
        return list(DEFAULT_APPS)

    @staticmethod
    def _clean(apps: list[Any]) -> list[str]:
        out: list[str] = []
        for a in apps:
            if isinstance(a, str) and a.strip() and a.strip() not in out and a.strip() not in NEVER:
                out.append(a.strip())
        return out

    def _save(self) -> None:
        write_json_atomic(self.path, {"apps": self._apps})

    def apps(self) -> list[str]:
        with self._lock:
            return list(self._apps)

    def refusal(self, app_id: str) -> Optional[str]:
        """Why ``app_id`` cannot be kept alive (None = it can)."""
        if app_id in NEVER:
            return "Esta app solo se arranca a mano: no se mantiene viva."
        app = self.hub.get(app_id)
        if app is None:
            return f"No hay ninguna app «{app_id}» en el catálogo."
        if app.family.get("auto_start") is False:
            return "Su manifiesto pide que solo se arranque a mano (auto_start: false)."
        if not app.launchable:
            return "El Hub no sabe arrancarla: no tiene orden de arranque."
        return None

    def set(self, app_id: str, keep: bool) -> dict[str, Any]:
        app_id = str(app_id or "").strip()
        if not app_id:
            return {"ok": False, "error": "falta la app"}
        if keep:
            why = self.refusal(app_id)
            if why:
                return {"ok": False, "app": app_id, "error": why}
        with self._lock:
            if keep and app_id not in self._apps:
                self._apps.append(app_id)
            elif not keep and app_id in self._apps:
                self._apps.remove(app_id)
                self._state.pop(app_id, None)
            self._save()
        invalidate = getattr(self.hub, "invalidate_snapshot", None)
        if invalidate:
            invalidate()                      # the page shows the new mark on its next read
        if keep:
            self._wake.set()                  # a serving Hub brings it up now, not in a minute
        return {"ok": True, "app": app_id, "keep": keep, "apps": self.apps()}

    # -- the watch -----------------------------------------------------------------------------------------------
    @property
    def serving(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def serve(self) -> bool:
        """Start the watch (the serving Hub calls this once it listens). False when it is turned off."""
        if disabled_by_env() or self.serving:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hoard-hub-keepalive", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        if self._stop.wait(BOOT_DELAY_S):
            return
        try:
            self.check(boot=True)
        except Exception:  # noqa: BLE001 - the watch keeps going
            logger.exception("keep-alive boot pass failed")
        while not self._stop.is_set():
            self._wake.wait(CHECK_S)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.check()
            except Exception:  # noqa: BLE001
                logger.exception("keep-alive pass failed")

    def check(self, boot: bool = False) -> list[dict[str, Any]]:
        """Look at every app on the list once and start the ones that are down. Returns what was done."""
        ids = self.apps()
        if boot:
            # Starting with the Hub: a stop from a previous Hub session does not keep these apps down.
            self.hub._manual_stop(ids, False)
        done: list[dict[str, Any]] = []
        now = time.time()
        for app_id in ids:
            entry = self._entry(app_id)
            if self.refusal(app_id) or self.hub.placement.local_block(app_id):
                continue
            app = self.hub.get(app_id)
            health = procs.health(app)
            entry["state"] = health.state
            if health.state != "down":
                if health.state == "healthy":
                    entry["attempts"] = 0
                    entry["next_try_ts"] = None
                continue
            if self.hub.automation_blocked(app_id):
                continue
            if entry.get("next_try_ts") and now < entry["next_try_ts"]:
                continue
            res = self.hub.start(app_id, wait=False, automatic=True)
            entry["last_start_ts"] = now
            entry["last_result"] = {k: res.get(k) for k in ("ok", "error", "pid", "already") if k in res}
            entry["starts"] = entry.get("starts", 0) + 1
            # Attempts since it was last seen healthy: 1 min, 2, 4… up to half an hour between tries.
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["next_try_ts"] = now + min(BACKOFF_MAX_S, CHECK_S * (2 ** (entry["attempts"] - 1)))
            self.hub._safe_emit("hub.keepalive.started", {"app": app_id, "boot": boot, "ok": bool(res.get("ok")),
                                                          "error": res.get("error"), "attempt": entry["attempts"]})
            done.append({"app": app_id, **entry["last_result"]})
        self._last_check = now
        return done

    def _entry(self, app_id: str) -> dict[str, Any]:
        with self._lock:
            return self._state.setdefault(app_id, {"attempts": 0, "starts": 0})

    # -- views ------------------------------------------------------------------------------------------------------
    def status(self, probe: bool = True) -> dict[str, Any]:
        rows = []
        for app_id in self.apps():
            app = self.hub.get(app_id)
            entry = dict(self._entry(app_id))
            if probe and app is not None:
                entry["state"] = procs.health(app).state
            rows.append({"id": app_id, "name": app.name if app else app_id, "known": app is not None,
                         "refusal": self.refusal(app_id), "placement": self.hub.placement.local_block(app_id),
                         "manually_stopped": self.hub.automation_blocked(app_id), **entry})
        return {"ok": True, "serving": self.serving, "disabled_by_env": disabled_by_env(), "interval_s": CHECK_S,
                "last_check_ts": self._last_check, "apps": rows, "never": sorted(NEVER), "file": self.path}

    def get(self, req: Request) -> Optional[Any]:
        if req.path == "/api/keep-alive":
            return self.status(probe=not req.q_bool("quick"))
        return None

    def post(self, req: Request) -> Optional[Any]:
        if req.path == "/api/keep-alive":
            return self.set(req.body.get("app", ""), bool(req.body.get("keep", True)))
        return None

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {"name": "hub_keep_alive_status",
             "description": "List the apps the Hub keeps alive: started with the Hub and started again when they fall "
                            "(checked every minute). Shows each one's state, failed starts and whether the person stopped it. "
                            "Keywords: mantener viva, siempre encendida, relanzar, keep alive.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_keep_alive_set",
             "description": "Add an app to the keep-alive list (keep=true: the Hub starts it now and whenever it falls) or remove "
                            "it (keep=false). Apps that must be started by hand (auto_start false) are refused. "
                            "Keywords: mantener viva, dejar de relanzar.",
             "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "keep": {"type": "boolean"}},
                             "required": ["app"], "additionalProperties": False}},
        ]

    def handlers(self) -> dict[str, Any]:
        return {"hub_keep_alive_status": lambda args: self.status(),
                "hub_keep_alive_set": lambda args: self.set(args.get("app", ""), bool(args.get("keep", True)))}
