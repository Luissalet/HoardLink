"""The hub itself: one object the HTTP API, the MCP bridge and the tests
all talk to. Holds the configuration, the scanned apps, and does the
actions; knows nothing about HTTP.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
import secrets
import threading
import time
from typing import Any, Callable, Optional

from . import HUB_VERSION, SERVICE
from .config import HubConfig
from .lease import LeaseArbiter
from .profiles import CommandRunner, Profile, parse as parse_profiles
from .registry import App, scan
from . import actions as _actions, audit as _audit, contract, desktop, procs
from .backup import BackupStore
from .events import EventLog
from .jobs import Scheduler
from .linkchat import LinkService
from .repos import RepoMonitor, RepoSettings
from .rules import RuleEngine
from .faustus import FaustusRuntime

BACKENDS_CACHE_S = 8.0
FAUSTUS_CACHE_S = 6.0



def default_call_reason(caller: str) -> str:
    """The reason the hub gives an app when one of its own steps (or another app) writes through it."""
    who = str(caller or "hub").strip() or "hub"
    if who.startswith("rule:"):
        return f"Automatic step of the Hub rule {who[5:]}"[:300]
    if who.startswith("job:"):
        return f"Automatic step of the Hub job {who[4:]}"[:300]
    if who in ("hub", "hub-tool"):
        return "Automatic step run by the Hub"
    return f"Requested by {who} through the Hub"[:300]

class Hub:
    def __init__(self, config: Optional[HubConfig] = None, *, gpu_fn: Optional[Callable[[], list[Any]]] = None):
        self.config = config or HubConfig.load()
        self._apps: dict[str, App] = {}
        self._lock = threading.RLock()
        # Starts in flight: app id -> (pid, spawned at). A start that is not
        # ready yet must not be spawned a second time (open() right after a
        # no-wait start, a profile and "start all" at once...).
        self._inflight: dict[str, tuple[int, float]] = {}
        self._spawned: dict[str, int] = {}   # app id -> pid of the last process the hub spawned for it
        self._start_locks: dict[str, threading.Lock] = {}
        self._spawned_created: dict[str, float] = {}
        self._stopping_all = threading.Event()
        self._stop_all_lock = threading.Lock()
        self._manual_stops: set[str] = set()
        self._backends_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._faustus_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._faustus_refreshing = threading.Event()
        self.started_at = time.time()
        os.makedirs(self.config.data_dir, exist_ok=True)
        os.makedirs(self.config.logs_dir, exist_ok=True)
        os.makedirs(self.config.profiles_dir, exist_ok=True)
        from .ports import PortAssignments
        self.ports = PortAssignments(os.path.join(self.config.data_dir, "app-ports.json"))
        try:
            with open(os.path.join(self.config.data_dir, "manual-stops.json"), encoding="utf-8") as fh:
                self._manual_stops = set(json.load(fh).get("stopped", []))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.faustus_runtime = FaustusRuntime(self.config)
        self.token = self._load_token()
        # The GPU/VRAM lease arbiter: one queue for every app on this machine.
        self.leases = LeaseArbiter(self.config.leases_file, gpu_fn=gpu_fn,
                                   headroom_mb=int(self.config.lease_headroom_mb or 0),
                                   protected_gpus=self.config.protected_gpus)
        # External commands of the profiles (ComfyUI instances, scripts...).
        # the shared backends (ComfyUI, Ollama, configured servers) any app of
        # the family can start without Faustus: same files as the apps use
        from ..launch import Launcher

        self.launcher = Launcher(app="hub")
        self.commands = CommandRunner(os.path.join(self.config.data_dir, "commands.json"), self.config.logs_dir)
        # The family's nervous system: the event log every app writes to,
        # rules that react to it, jobs on a clock, and the backup store.
        self.events = EventLog(self.config.events_file, keep=int(self.config.events_keep or 20000))
        self.leases.on_event = self._lease_event
        runner = lambda acts, ctx, caller: _actions.run_all(self, acts, ctx, caller=caller)  # noqa: E731
        self.rules = RuleEngine(self.config.rules_file, self.events, runner)
        self.jobs = Scheduler(self.config.jobs_file, runner, events=self.events)
        bk = self.config.backup or {}
        self.backups = BackupStore(self.config.backup_dir, exclude=list(bk.get("exclude") or []),
                                   max_file_mb=float(bk.get("max_file_mb") or 512))
        # The Repos facet: reads the family's git repositories, never changes one. Nothing is scanned until
        # someone asks (the page, a tool, a job); the last snapshot is only loaded from data/repos.json.
        self.repos = RepoMonitor(self.config.data_dir, self._repo_settings, lambda: self.apps,
                                 emit=lambda t, d: self.events.emit(t, d, source="hub"),
                                 lang_fn=lambda: self.config.language)
        # Models for every app (Hoard Link 0.6): chat/vision over HTTP through the hub's own Link.
        self.link = LinkService(self.config, emit=lambda t, d: self.events.emit(t, d, source="hub"))
        procs._protected_pids()  # warm the ancestor list once, off the request path
        self.rescan()
        # Facets (0.7): notifications, spheres, mail, chats, Today, references, search, jobs view, purchases.
        from . import facets as _facets
        self.facets = _facets.load_all(self)
        if self.config.jobs_enabled:
            self.jobs.start()
        _facets.start_all(self.facets)

    def facet(self, facet_id: str) -> Any:
        """The facet with that id (``notify``, ``spheres``…) or None."""
        return getattr(self, "_facets_by_id", {}).get(facet_id)

    def _repo_settings(self) -> RepoSettings:
        """The Repos facet's settings: ``hub.json`` ``repos`` plus the hub's own roots and Faustus folder."""
        cfg = RepoSettings.from_config(self.config.repos, self.config.faustus_dir)
        cfg.roots = [*self.config.roots, *cfg.roots]
        return cfg

    def _lease_event(self, kind: str, lease: dict[str, Any]) -> None:
        try:
            self.events.emit("hub.lease." + kind, {k: lease.get(k) for k in ("lease_id", "owner", "purpose", "gpu", "vram_mb")})
        except Exception:  # noqa: BLE001
            pass

    def emit(self, type: str, data: Optional[dict[str, Any]] = None, *, source: str = "hub") -> dict[str, Any]:
        return self.events.emit(type, data, source=source)

    def close(self) -> None:
        from . import facets as _facets
        _facets.close_all(getattr(self, "facets", []))
        for part in (self.jobs, self.rules, self.link, self.events):
            try:
                part.close()
            except Exception:  # noqa: BLE001
                pass

    # -- token / files -----------------------------------------------------
    def _load_token(self) -> str:
        path = self.config.token_file
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tok = fh.read().strip()
                if tok:
                    return tok
        except OSError:
            pass
        tok = secrets.token_urlsafe(32)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(tok)
        except OSError:
            pass
        return tok

    def write_url_file(self) -> None:
        try:
            with open(self.config.url_file, "w", encoding="utf-8") as fh:
                fh.write(self.config.url)
        except OSError:
            pass

    # -- apps ---------------------------------------------------------------
    def _launch_overrides(self) -> Optional[dict[str, Any]]:
        """``launch_overrides`` from ``hub.json``, re-read on every rescan so an edit takes
        effect without restarting the hub; the loaded config is the fallback."""
        path = os.path.join(self.config.data_dir, "hub.json")
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                # Removing the key (or the whole entry) must undo the override
                # too, not leave the last one loaded in memory.
                overrides = raw.get("launch_overrides")
                self.config.launch_overrides = overrides if isinstance(overrides, dict) else {}
        except (OSError, ValueError):
            pass
        value = self.config.launch_overrides
        return value if isinstance(value, dict) and value else None

    def rescan(self) -> list[App]:
        apps = scan(
            self.config.roots,
            faustus_dir=self.config.faustus_dir,
            faustus_python=self.config.faustus_python,
            icon_dirs=self.config.icon_dirs,
            exclude_ids=self.config.exclude_ids + [SERVICE, "hoardhub"],
            launch_overrides=self._launch_overrides(),
        )
        self.ports.restore(apps)
        with self._lock:
            self._apps = {a.id: a for a in apps}
        return apps

    @property
    def apps(self) -> list[App]:
        with self._lock:
            return list(self._apps.values())

    def get(self, app_id: str) -> Optional[App]:
        with self._lock:
            return self._apps.get(app_id)

    def app_status(self, app: App, listeners: Optional[dict[int, int]] = None,
                   windows: Optional[dict[str, list[dict[str, Any]]]] = None,
                   health: Optional[procs.Health] = None) -> dict[str, Any]:
        h = health if health is not None else procs.health(app)
        proc = procs.find_app_process(app, listeners)
        if windows is None:
            windows = desktop.list_windows(self.config.profiles_dir)
        state = h.state
        if state == "down" and proc is not None:
            state = "starting"  # a listener with no healthy answer yet
        if state == "healthy":
            state = "running"
        d = app.to_dict()
        d.update({
            "state": state,
            "health": h.to_dict(),
            "process": proc.to_dict() if proc else None,
            "windows": windows.get(app.id, []),
            "log": os.path.join(self.config.logs_dir, f"{app.id}.log"),
            "stoppable": bool(proc) and h.state == "healthy" and not proc.protected,
            "manually_stopped": self.automation_blocked(app.id),
        })
        return d

    def snapshot(self) -> dict[str, Any]:
        apps_now = self.apps
        # Every probe in parallel: a closed loopback port costs ~1s on
        # Windows, and a dozen of them in a row would make the page crawl.
        with ThreadPoolExecutor(max_workers=max(4, len(apps_now) + 3)) as pool:
            fut_windows = pool.submit(desktop.list_windows, self.config.profiles_dir)
            fut_faustus = pool.submit(self.faustus_status)
            listeners = procs.listening_pids()
            healths = list(pool.map(lambda a: procs.health(a, listeners), apps_now))
            windows = fut_windows.result()
            faustus = fut_faustus.result()
            apps = list(pool.map(lambda ah: self.app_status(ah[0], listeners, windows, ah[1]), zip(apps_now, healths)))
        profiles = self.profiles_status({a["id"]: a["state"] for a in apps})
        running = sum(1 for a in apps if a["state"] == "running")
        return {
            "service": SERVICE,
            "version": HUB_VERSION,
            "apps": apps,
            "counts": {"total": len(apps), "running": running,
                       "windows": sum(len(v) for k, v in windows.items() if not k.startswith("_"))},
            "roots": list(self.config.roots),
            "faustus": faustus,
            "profiles": profiles["profiles"],
            "hub": {"url": self.config.url, "data_dir": self.config.data_dir, "uptime_s": int(time.time() - self.started_at),
                    "browser": desktop.find_browser(self.config.browser),
                    "window_engine": desktop.window_engine(self.config.window_engine), "psutil": procs._psutil() is not None,
                    "events": self.events.last_id, "rules": len(self.rules.rules), "jobs": len(self.jobs.jobs),
                    "backup_dir": self.backups.root},
        }

    # -- actions --------------------------------------------------------------
    def automation_blocked(self, target: str) -> bool:
        with self._lock:
            return self._stopping_all.is_set() or target in self._manual_stops

    def _manual_stop(self, targets: list[str], stopped: bool = True) -> None:
        with self._lock:
            if stopped:
                self._manual_stops.update(targets)
            else:
                self._manual_stops.difference_update(targets)
            path = os.path.join(self.config.data_dir, "manual-stops.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"stopped": sorted(self._manual_stops)}, fh)
            os.replace(tmp, path)

    def start(self, app_id: str, wait: bool = True, *, automatic: bool = False) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        with self._lock:
            lock = self._start_locks.setdefault(app_id, threading.RLock())
        with lock:  # one start per app at a time
            if self._stopping_all.is_set() or (automatic and self.automation_blocked(app_id)):
                return {"ok": False, "app": app_id, "error": "manually stopped; start it explicitly to resume"}
            if not automatic:
                self._manual_stop([app_id], False)
            pending = self._pending_start(app)
            if pending is not None:
                res = self._await_ready(app, pending) if wait else {"ok": True, "already": True, "pid": pending,
                                                                       "detail": "already starting"}
            else:
                allocation = self.ports.prepare(app, self.apps, self.config.port)
                if not allocation.get("ok"):
                    return dict(allocation, app=app_id)
                res = procs.start_app(app, self.config.logs_dir, wait=wait)
                if allocation.get("previous_port"):
                    res.update(previous_port=allocation["previous_port"], port=allocation["port"])
                if res.get("ok") and res.get("pid"):
                    with self._lock:
                        self._spawned[app_id] = int(res["pid"])
                        created = procs.proc_info(int(res["pid"])).created_at
                        if created is not None:
                            self._spawned_created[app_id] = created
                        if not res.get("ready"):
                            self._inflight[app_id] = (int(res["pid"]), time.time())
            if res.get("ok") and not res.get("pid"):
                # "already running/starting" (e.g. it became healthy while a
                # slow probe ran): name the process that serves it.
                res["pid"] = self._running_pid(app)
        res["app"] = app_id
        if res.get("ok") and not res.get("already"):
            self._safe_emit("hub.app.started", {"app": app_id, "pid": res.get("pid"), "ready": res.get("ready")})
        return res

    def _safe_emit(self, type: str, data: dict[str, Any]) -> None:
        try:
            self.events.emit(type, data)
        except Exception:  # noqa: BLE001
            pass

    def _running_pid(self, app: App) -> Optional[int]:
        """The pid the hub spawned for ``app`` when it still runs (on Windows a
        venv python.exe is a launcher whose child listens, so prefer the
        spawned one), else whoever listens on the app's port, else None."""
        with self._lock:
            pid = self._spawned.get(app.id)
        if pid is not None and procs.pid_running(pid):
            return pid
        proc = procs.find_app_process(app)
        return proc.pid if proc else None

    def _pending_start(self, app: App) -> Optional[int]:
        """Pid of a start of ``app`` that is still booting, else None."""
        with self._lock:
            rec = self._inflight.get(app.id)
        if rec is None:
            return None
        pid, t0 = rec
        timeout = app.launch.readiness_timeout_s if app.launch else 30.0
        if time.time() - t0 > timeout or not procs.pid_running(pid) or procs.health(app).state == "healthy":
            with self._lock:
                self._inflight.pop(app.id, None)
            return None
        return pid

    def _await_ready(self, app: App, pid: int) -> dict[str, Any]:
        timeout = app.launch.readiness_timeout_s if app.launch else 30.0
        deadline = time.time() + timeout
        while time.time() < deadline:
            h = procs.health(app)
            if h.state == "healthy":
                with self._lock:
                    self._inflight.pop(app.id, None)
                return {"ok": True, "pid": pid, "ready": True, "health": h.to_dict(), "detail": "was already starting"}
            if not procs.pid_running(pid):
                with self._lock:
                    self._inflight.pop(app.id, None)
                return {"ok": False, "pid": pid, "error": "the process that was starting exited",
                        "log_tail": procs.tail(os.path.join(self.config.logs_dir, f"{app.id}.log"), 30)}
            time.sleep(0.5)
        return {"ok": True, "pid": pid, "ready": False, "detail": f"not ready after {timeout:.0f}s (still starting?)"}

    def stop(self, app_id: str) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        # Record intent before probing/killing: a queued recovery rule must not undo it.
        self._manual_stop([app_id])
        with self._lock:
            lock = self._start_locks.setdefault(app_id, threading.RLock())
        with lock:
            self._manual_stop([app_id])
            return self._stop_app(app)

    def _stop_app(self, app: App) -> dict[str, Any]:
        app_id = app.id
        h = procs.health(app)
        proc = procs.find_app_process(app)
        with self._lock:
            spawned, created = self._spawned.get(app_id), self._spawned_created.get(app_id)
        if spawned and created is not None and procs.pid_running(spawned):
            tracked = procs.proc_info(spawned)
            if tracked.created_at is not None and abs(tracked.created_at - created) < .01:
                proc = tracked  # stop the launcher too, including a server still booting
        if proc is None:
            desktop.close_windows(app_id, self.config.profiles_dir)
            return {"ok": True, "app": app_id, "detail": "not running"}
        if h.state == "foreign":
            return {"ok": False, "app": app_id, "error": "refusing: " + h.detail}
        if h.state == "down":
            # Something listens but does not answer: only stop it when it is
            # clearly this app (its folder in the command line / cwd).
            folder = app.folder.lower()
            if folder not in (proc.cmdline or "").lower() and folder != (proc.cwd or "").lower():
                return {"ok": False, "app": app_id,
                        "error": f"port {app.port} is held by pid {proc.pid} ({proc.name}) which does not look like {app.name}"}
        res = procs.terminate_tree(proc.pid, created_at=proc.created_at)
        res["app"] = app_id
        res["pid"] = proc.pid
        # Its windows are pointless without the server behind them.
        desktop.close_windows(app_id, self.config.profiles_dir)
        if res.get("ok"):
            with self._lock:
                self._inflight.pop(app_id, None)
                self._spawned.pop(app_id, None)
                self._spawned_created.pop(app_id, None)
            self._safe_emit("hub.app.stopped", {"app": app_id, "pid": proc.pid})
        return res

    def restart(self, app_id: str, *, automatic: bool = False) -> dict[str, Any]:
        if automatic:
            app = self.get(app_id)
            if app is None:
                return {"ok": False, "error": f"unknown app: {app_id}"}
            with self._lock:
                lock = self._start_locks.setdefault(app_id, threading.RLock())
            with lock:
                if self.automation_blocked(app_id):
                    return {"ok": False, "error": "manually stopped; start it explicitly to resume"}
                stopped = self._stop_app(app)
                if not stopped.get("ok"):
                    return stopped
                started = self.start(app_id, automatic=True)
                started["stopped"] = stopped
                return started
        stopped = self.stop(app_id)
        if not stopped.get("ok"):
            return stopped
        time.sleep(0.5)
        started = self.start(app_id, automatic=automatic)
        started["stopped"] = stopped
        return started

    def open(self, app_id: str, mode: str = "window", autostart: bool = True, *, automatic: bool = False) -> dict[str, Any]:
        with self._lock:
            lock = self._start_locks.setdefault(app_id, threading.RLock())
        # The stop uses this same lock and closes windows after a pending open finishes.
        with lock:
            return self._open_app(app_id, mode, autostart, automatic=automatic)

    def _open_app(self, app_id: str, mode: str, autostart: bool, *, automatic: bool = False) -> dict[str, Any]:
        if self._stopping_all.is_set() or (automatic and self.automation_blocked(app_id)):
            return {"ok": False, "error": "manually stopped; start it explicitly to resume"}
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        h = procs.health(app)
        started: Optional[dict[str, Any]] = None
        if h.state == "down" and autostart and app.launchable:
            started = self.start(app_id, wait=True, automatic=automatic)
            if not started.get("ok"):
                started["app"] = app_id
                return started
            if app.kind == "window-app":
                return started  # the exe opens its own window
        elif h.state == "down" and not app.launchable:
            return {"ok": False, "app": app_id, "error": "not running and cannot be started here: " + app.launch_reason}
        if app.kind == "window-app" and h.state == "healthy" and mode == "window":
            # Already running; its own window exists (or its tray). Focus by re-opening is not
            # possible generically, so open the web UI as a window like everyone else.
            pass
        if mode == "browser":
            res = desktop.open_in_browser(app.url)
        else:
            res = desktop.open_window(app.url, app.id, self.config.profiles_dir, browser=self.config.browser,
                                      size=(int(self.config.window_size[0]), int(self.config.window_size[1])),
                                      engine=self.config.window_engine, name=app.name, icon=app.icon_path)
        res["app"] = app_id
        if started:
            res["started"] = started
        return res

    def close_windows(self, app_id: str) -> dict[str, Any]:
        res = desktop.close_windows(app_id, self.config.profiles_dir)
        res["app"] = app_id
        return res

    def open_folder(self, app_id: str) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        return desktop.open_folder(app.folder)

    def open_repo_folder(self, name: str) -> dict[str, Any]:
        """Open a scanned repository's folder in the file manager (only ones the Repos facet knows)."""
        rec, candidates = self.repos.find(name)
        if rec is None:
            return {"ok": False, "error": f"unknown repo: {name}", "candidates": candidates}
        res = desktop.open_folder(rec["path"])
        res["repo"] = rec["name"]
        return res

    def log_tail(self, app_id: str, lines: int = 80) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        path = os.path.join(self.config.logs_dir, f"{app.id}.log")
        return {"ok": True, "app": app_id, "path": path, "lines": procs.tail(path, lines)}

    def start_all(self, *, automatic: bool = False) -> dict[str, Any]:
        results = [self.start(a.id, wait=False, automatic=automatic) for a in self.apps if a.launchable]
        faustus = self.faustus_start(automatic=automatic) if self.faustus_runtime.available() else {"ok": True, "skipped": True}
        return {"ok": all(r.get("ok") for r in results) and faustus["ok"], "results": results, "faustus": faustus}

    def stop_all(self) -> dict[str, Any]:
        with self._stop_all_lock:
            self._stopping_all.set()
            try:
                apps = self.apps
                self._manual_stop([a.id for a in apps] + ["faustus"] +
                                  ["profile:" + name for name in self.profiles()] +
                                  ["service:" + svc.id for svc in self.launcher.services()])
                faustus = self.faustus_stop() if self.faustus_runtime.available() else {"ok": True, "skipped": True}
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda a: self.stop(a.id), apps))
                commands = [self.commands.stop(c) for p in self.profiles().values() for c in p.commands]
                services = [self.service_stop(s["id"]) for s in self.launcher.statuses()
                            if s.get("stoppable") or s.get("state") in ("running", "starting")]
                all_results = results + commands + services + [faustus]
                return {"ok": all(r.get("ok") for r in all_results), "results": results,
                        "commands": commands, "services": services, "faustus": faustus}
            finally:
                self._stopping_all.clear()

    # -- profiles -------------------------------------------------------------
    def profiles(self) -> dict[str, Profile]:
        return parse_profiles(self.config.profiles)[0]

    def profiles_status(self, app_states: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Every profile with the state of each member. ``app_states``
        (id -> state, from a snapshot) avoids probing the apps twice."""
        profiles, problems = parse_profiles(self.config.profiles)
        if app_states is None:
            wanted = {a for p in profiles.values() for a in p.members}
            apps = [a for a in self.apps if a.id in wanted]
            with ThreadPoolExecutor(max_workers=max(2, len(apps))) as pool:
                healths = list(pool.map(procs.health, apps))
            app_states = {a.id: ("running" if h.state == "healthy" else h.state) for a, h in zip(apps, healths)}
        cmds = [c for p in profiles.values() for c in p.commands]
        with ThreadPoolExecutor(max_workers=max(2, len(cmds))) as pool:
            cmd_status = dict(zip([c.id for c in cmds], pool.map(self.commands.status, cmds)))
        out = []
        for p in profiles.values():
            members = [{"id": a, "kind": "app", "name": (self.get(a).name if self.get(a) else a),
                        "state": app_states.get(a, "down") if self.get(a) else "unknown",
                        "desktop": a in p.desktop} for a in p.members]
            members += [{"id": c.id, "kind": "command", "name": c.name, "state": cmd_status[c.id]["state"],
                         "pid": cmd_status[c.id]["pid"], "health": c.health} for c in p.commands]
            total = len(members)
            running = sum(1 for m in members if m["state"] == "running")
            state = "empty" if not total else ("running" if running == total else ("partial" if running else "stopped"))
            out.append({"name": p.name, "state": state, "running": running, "total": total, "members": members,
                        "apps": list(p.apps), "desktop": list(p.desktop), "commands": [c.to_dict() for c in p.commands]})
        return {"ok": True, "profiles": out, "problems": problems}

    def profile_status(self, name: str) -> dict[str, Any]:
        for p in self.profiles_status()["profiles"]:
            if p["name"] == name:
                return dict(p, ok=True)
        return {"ok": False, "error": f"unknown profile: {name}", "profiles": list(self.profiles())}

    def profile_start(self, name: str, *, automatic: bool = False) -> dict[str, Any]:
        """Start the profile's apps and commands together (not waiting for
        each), then open its desktop apps as windows (that waits for them)."""
        p = self.profiles().get(name)
        if p is None:
            return {"ok": False, "error": f"unknown profile: {name}", "profiles": list(self.profiles())}
        if self._stopping_all.is_set() or (automatic and
                (self.automation_blocked("profile:" + name) or any(self.automation_blocked(a) for a in p.members))):
            return {"ok": False, "error": "profile was manually stopped; start it explicitly to resume"}
        if not automatic:
            self._manual_stop(["profile:" + name], False)
        unknown = [a for a in p.members if self.get(a) is None]
        known = [a for a in p.members if self.get(a) is not None]
        # A desktop app is started by open() itself (which waits for it to be
        # ready before opening the window); starting it here too would race.
        plain = [a for a in known if a not in p.desktop]
        desktop = [a for a in known if a in p.desktop]
        n = max(2, len(known) + len(p.commands))
        with ThreadPoolExecutor(max_workers=n) as pool:
            fut_apps = [pool.submit(self.start, a, False, automatic=automatic) for a in plain]
            fut_cmds = [pool.submit(self.commands.start, c) for c in p.commands]
            fut_desk = [pool.submit(self.open, a, "window", True, automatic=automatic) for a in desktop]
            app_res = [f.result() for f in fut_apps]
            cmd_res = [f.result() for f in fut_cmds]
            desk_res = [f.result() for f in fut_desk]
        results = app_res + cmd_res + desk_res
        return {"ok": not unknown and all(r.get("ok") for r in results), "profile": name,
                "apps": app_res, "commands": cmd_res, "desktop": desk_res,
                "unknown": unknown, **({"error": "unknown apps: " + ", ".join(unknown)} if unknown else {})}

    def profile_stop(self, name: str) -> dict[str, Any]:
        p = self.profiles().get(name)
        if p is None:
            return {"ok": False, "error": f"unknown profile: {name}", "profiles": list(self.profiles())}
        self._manual_stop(["profile:" + name])
        cmd_res = [self.commands.stop(c) for c in p.commands]
        app_res = [self.stop(a) for a in p.members if self.get(a) is not None]
        results = cmd_res + app_res
        errors = [r.get("error") for r in results if not r.get("ok")]
        return {"ok": not errors, "profile": name, "apps": app_res, "commands": cmd_res,
                **({"error": "; ".join(str(e) for e in errors)} if errors else {})}

    # -- the family: calls between apps, backups, audit -------------------------
    def call_app(self, app_id: str, tool: str, arguments: Optional[dict[str, Any]] = None, *,
                 caller: str = "hub", timeout: float = 120.0, reason: Optional[str] = None,
                 agent: Optional[str] = None, session: Optional[str] = None) -> dict[str, Any]:
        """Run a tool of one app with that app's own token (the proxy).

        Apps with accountable agents refuse writes without a ``reason``. The hub's own steps (rules, jobs, purchases,
        search, media flows) and the calls other apps make through the hub get a default reason that names the
        caller (or the agent named by ``X-Agent-Id``); an agent using ``hub_call_app`` (caller ``hub-tool``) must give its
        own, so it is told why it is asked when it forgets."""
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}", "apps": [a.id for a in self.apps]}
        args = arguments if isinstance(arguments, dict) else {}
        given = reason if isinstance(reason, str) and reason.strip() else args.get("reason")
        if not (isinstance(given, str) and given.strip()) and caller != "hub-tool":
            given = default_call_reason(agent if agent and agent != caller else caller)
        res = contract.call_app(app, tool, args, timeout=timeout, caller=caller,
                                reason=given if isinstance(given, str) else None,
                                agent=agent or caller, session=session)
        self._safe_emit("hub.call", {"app": app_id, "tool": tool, "ok": res.get("ok"), "ms": res.get("ms"),
                                     "caller": caller, "contract": res.get("contract"), "error": res.get("error")})
        return res

    def app_tools(self, app_id: str) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        return contract.app_tools(app)

    def token_owner(self, token: str) -> Optional[str]:
        return contract.token_owner(token, self.apps, self.token)

    def backup_sources(self, only: Optional[list[str]] = None) -> dict[str, str]:
        plan = self.backup_source_inventory(only)
        if plan["source_errors"]:
            raise ValueError("; ".join(e["error"] for e in plan["source_errors"]))
        return plan["sources"]

    def backup_source_inventory(self, only: Optional[list[str]] = None) -> dict[str, Any]:
        from .backup_sources import inventory
        return inventory(self.apps, self.config.data_dir, only)

    def backup_run(self, apps: Optional[list[str]] = None, label: str = "") -> dict[str, Any]:
        plan = self.backup_source_inventory(apps)
        if plan["source_errors"]:
            return {"ok": False, "error": "; ".join(e["error"] for e in plan["source_errors"]),
                    "source_errors": plan["source_errors"]}
        sources = plan["sources"]
        unknown = [a for a in (apps or []) if a not in sources]
        if unknown:
            return {"ok": False, "error": "unknown apps: " + ", ".join(unknown)}
        res = self.backups.snapshot(sources, label=label)
        if res.get("ok") or res.get("snapshot"):
            self._safe_emit("hub.backup.done" if res.get("ok") else "hub.backup.failed",
                            {"snapshot": res.get("snapshot"), "apps": sorted(sources), "files": res.get("totals", {}).get("files"),
                             "new_bytes": res.get("totals", {}).get("new_bytes"), "errors": res.get("totals", {}).get("errors")})
        return res

    def backup_restore(self, snapshot: str, app_id: str, *, dest: Optional[str] = None, in_place: bool = False) -> dict[str, Any]:
        restore_options = {}
        if app_id == "atlas-files":
            error = "restore shared files into a separate folder outside the original storage roots"
            if in_place:
                return {"ok": False, "error": error}
            from pathlib import Path
            from .backup_sources import read_shared_root
            atlas = self.get("atlas")
            try:
                current = read_shared_root(atlas.data_dir) if atlas and atlas.data_dir else None
            except (OSError, ValueError, TypeError):
                current = None

            def destination_guard(destination, original):
                try:
                    target = Path(destination).resolve()
                    for folder in (original, current):
                        if folder and Path(folder).is_absolute():
                            root = Path(folder).resolve()
                            if target.is_relative_to(root) or root.is_relative_to(target):
                                return error
                except (OSError, ValueError, RuntimeError):
                    return "invalid restore destination"
                return None

            restore_options["destination_guard"] = destination_guard
        running: Optional[bool] = None
        if in_place:
            app = self.get(app_id)
            running = app is not None and procs.health(app).state == "healthy"
        res = self.backups.restore(snapshot, app_id, dest=dest, in_place=in_place, app_running=running, **restore_options)
        self._safe_emit("hub.backup.restored" if res.get("ok") else "hub.backup.restore_failed",
                        {"snapshot": snapshot, "app": app_id, "dest": res.get("dest"), "in_place": in_place, "error": res.get("error")})
        return res

    def family_audit(self, probe: bool = True) -> dict[str, Any]:
        return _audit.audit(self.apps, self.config.url, probe=probe)

    # -- surroundings ---------------------------------------------------------
    def faustus_status(self) -> dict[str, Any]:
        """Cached: a Faustus busy with a long model turn can take seconds to
        answer its health check, and that must never slow the hub's own
        page. The first call probes synchronously; later calls return the
        last answer and refresh it in the background when it is older than
        ``FAUSTUS_CACHE_S``."""
        ts, cached = self._faustus_cache
        age = time.time() - ts
        if cached and age < FAUSTUS_CACHE_S:
            return cached
        if cached:
            if not self._faustus_refreshing.is_set():
                self._faustus_refreshing.set()
                threading.Thread(target=self._refresh_faustus, name="hoard-hub-faustus", daemon=True).start()
            return cached
        return self._refresh_faustus()

    def _refresh_faustus(self) -> dict[str, Any]:
        result = {"reachable": False, "url": self.config.faustus_urls[0] if self.config.faustus_urls else None,
                  "status": None, "body": None}
        try:
            for url in self.config.faustus_urls:
                status, body = procs.fetch_json(url.rstrip("/") + "/api/health", timeout=3.0)
                if status is not None:
                    result = {"reachable": status < 500, "url": url, "status": status,
                              "body": body if isinstance(body, dict) else None}
                    break
        finally:
            result.update(available=self.faustus_runtime.available(),
                          manually_stopped=self.automation_blocked("faustus"))
            result["startable"] = result["available"] and not result["reachable"]
            result["stoppable"] = result["available"]
            self._faustus_cache = (time.time(), result)
            self._faustus_refreshing.clear()
        return result

    def faustus_start(self, *, automatic: bool = False) -> dict[str, Any]:
        with self._lock:
            lock = self._start_locks.setdefault("faustus", threading.RLock())
        with lock:
            if self._stopping_all.is_set() or (automatic and self.automation_blocked("faustus")):
                return {"ok": False, "error": "stop all is still in progress"}
            self._manual_stop(["faustus"], False)
            self.faustus_runtime.prepare_start()
            res = self.faustus_runtime.run("start")
            self._faustus_cache = (0.0, {})
            if res.get("ok"):
                self._safe_emit("hub.faustus.started", {"port": res.get("port")})
            return res

    def faustus_stop(self) -> dict[str, Any]:
        self._manual_stop(["faustus"])
        self.faustus_runtime.cancel_start()
        with self._lock:
            lock = self._start_locks.setdefault("faustus", threading.RLock())
        with lock:
            self._manual_stop(["faustus"])
            res = self.faustus_runtime.run("stop-all")
            self._faustus_cache = (0.0, {})
            if res.get("ok"):
                self._safe_emit("hub.faustus.stopped", {})
            return res

    def services(self) -> dict[str, Any]:
        """Local backend servers (ComfyUI, Ollama, backends.json commands):
        running, down or startable, who started them."""
        from ..launch import list_gpus

        items = self.launcher.statuses()
        return {"ok": True, "items": items, "gpus": list_gpus(), "config_path": str(self.launcher.config_path)}

    def gpu_memory(self) -> dict[str, Any]:
        from ..launch import memory

        return {"ok": True, **memory(self.launcher)}

    def service_start(self, service_id: str, gpu: Any = None, wait_s: float = 0.0, *, automatic: bool = False) -> dict[str, Any]:
        if self._stopping_all.is_set() or (automatic and self.automation_blocked("service:" + service_id)):
            return {"ok": False, "error": "stop all is still in progress"}
        self._manual_stop(["service:" + service_id], False)
        res = self.launcher.start(str(service_id or ""), gpu=gpu, wait_s=max(0.0, min(float(wait_s or 0), 300.0)))
        if res.get("ok"):
            self._backends_cache = (0.0, None)
            try:
                self.events.emit("hub.service.started", {"service": res.get("service"), "gpu": res.get("gpu"),
                                                         "already": bool(res.get("already"))}, source="hub")
            except Exception:  # noqa: BLE001 - the bus is best effort
                pass
        return res

    def service_stop(self, service_id: str) -> dict[str, Any]:
        self._manual_stop(["service:" + service_id])
        res = self.launcher.stop(str(service_id or ""))
        if res.get("ok"):
            self._backends_cache = (0.0, None)
            try:
                self.events.emit("hub.service.stopped", {"service": res.get("service")}, source="hub")
            except Exception:  # noqa: BLE001
                pass
        return res

    def backends(self, force: bool = False) -> dict[str, Any]:
        ts, cached = self._backends_cache
        if not force and cached and time.time() - ts < BACKENDS_CACHE_S:
            return cached
        result = _backends_now(self.config)
        self._backends_cache = (time.time(), result)
        return result


def _backends_now(config: HubConfig) -> dict[str, Any]:
    """What Hoard Link resolves for every capability right now, plus GPU
    free memory — the library used for what it is for."""
    try:
        from hoard_link import CAPABILITIES, Link, LinkConfig, gpu_free_mb
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"hoard_link not importable: {exc}", "capabilities": {}}

    async def run() -> dict[str, Any]:
        cfg = LinkConfig.load(None, app="hoard-hub")
        cfg = LinkConfig(app="hoard-hub", only_resident=True, faustus_urls=tuple(config.faustus_urls),
                         faustus_token=cfg.faustus_token, comfy_url=cfg.comfy_url, capabilities=cfg.capabilities,
                         use_routes=cfg.use_routes, routes_file=cfg.routes_file)
        async with Link(cfg) as link:
            status = await link.status()
        return {cap: status[cap] for cap in CAPABILITIES}

    try:
        caps = asyncio.run(run())
        ok = True
        error = None
    except Exception as exc:  # noqa: BLE001
        caps, ok, error = {}, False, str(exc)
    gpus: list[dict[str, Any]] = []
    try:
        for g in gpu_free_mb():
            gpus.append({"index": getattr(g, "index", None), "name": getattr(g, "name", ""),
                         "free_mb": getattr(g, "free_mb", None), "total_mb": getattr(g, "total_mb", None)})
    except Exception:  # noqa: BLE001
        gpus = []
    return {"ok": ok, "error": error, "capabilities": caps, "gpus": gpus, "checked_at": time.time()}
