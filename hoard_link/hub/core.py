"""The hub itself: one object the HTTP API, the MCP bridge and the tests
all talk to. Holds the configuration, the scanned apps, and does the
actions; knows nothing about HTTP.
"""

from __future__ import annotations

import asyncio
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
from . import desktop, procs

BACKENDS_CACHE_S = 8.0
FAUSTUS_CACHE_S = 6.0


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
        self._backends_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._faustus_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._faustus_refreshing = threading.Event()
        self.started_at = time.time()
        os.makedirs(self.config.data_dir, exist_ok=True)
        os.makedirs(self.config.logs_dir, exist_ok=True)
        os.makedirs(self.config.profiles_dir, exist_ok=True)
        self.token = self._load_token()
        # The GPU/VRAM lease arbiter: one queue for every app on this machine.
        self.leases = LeaseArbiter(self.config.leases_file, gpu_fn=gpu_fn,
                                   headroom_mb=int(self.config.lease_headroom_mb or 0))
        # External commands of the profiles (ComfyUI instances, scripts...).
        self.commands = CommandRunner(os.path.join(self.config.data_dir, "commands.json"), self.config.logs_dir)
        procs._protected_pids()  # warm the ancestor list once, off the request path
        self.rescan()

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
    def rescan(self) -> list[App]:
        apps = scan(
            self.config.roots,
            faustus_dir=self.config.faustus_dir,
            faustus_python=self.config.faustus_python,
            icon_dirs=self.config.icon_dirs,
            exclude_ids=self.config.exclude_ids + [SERVICE, "hoardhub"],
        )
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
                    "browser": desktop.find_browser(self.config.browser), "psutil": procs._psutil() is not None},
        }

    # -- actions --------------------------------------------------------------
    def start(self, app_id: str, wait: bool = True) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        with self._lock:
            lock = self._start_locks.setdefault(app_id, threading.Lock())
        with lock:  # one start per app at a time
            pending = self._pending_start(app)
            if pending is not None:
                res = self._await_ready(app, pending) if wait else {"ok": True, "already": True, "pid": pending,
                                                                       "detail": "already starting"}
            else:
                res = procs.start_app(app, self.config.logs_dir, wait=wait)
                if res.get("ok") and res.get("pid"):
                    with self._lock:
                        self._spawned[app_id] = int(res["pid"])
                        if not res.get("ready"):
                            self._inflight[app_id] = (int(res["pid"]), time.time())
            if res.get("ok") and not res.get("pid"):
                # "already running/starting" (e.g. it became healthy while a
                # slow probe ran): name the process that serves it.
                res["pid"] = self._running_pid(app)
        res["app"] = app_id
        return res

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
        h = procs.health(app)
        proc = procs.find_app_process(app)
        if proc is None:
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
        return res

    def restart(self, app_id: str) -> dict[str, Any]:
        stopped = self.stop(app_id)
        if not stopped.get("ok"):
            return stopped
        time.sleep(0.5)
        started = self.start(app_id)
        started["stopped"] = stopped
        return started

    def open(self, app_id: str, mode: str = "window", autostart: bool = True) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        h = procs.health(app)
        started: Optional[dict[str, Any]] = None
        if h.state == "down" and autostart and app.launchable:
            started = self.start(app_id, wait=True)
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
                                      size=(int(self.config.window_size[0]), int(self.config.window_size[1])))
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

    def log_tail(self, app_id: str, lines: int = 80) -> dict[str, Any]:
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"unknown app: {app_id}"}
        path = os.path.join(self.config.logs_dir, f"{app.id}.log")
        return {"ok": True, "app": app_id, "path": path, "lines": procs.tail(path, lines)}

    def start_all(self) -> dict[str, Any]:
        return {"ok": True, "results": [self.start(a.id, wait=False) for a in self.apps if a.launchable]}

    def stop_all(self) -> dict[str, Any]:
        return {"ok": True, "results": [self.stop(a.id) for a in self.apps]}

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

    def profile_start(self, name: str) -> dict[str, Any]:
        """Start the profile's apps and commands together (not waiting for
        each), then open its desktop apps as windows (that waits for them)."""
        p = self.profiles().get(name)
        if p is None:
            return {"ok": False, "error": f"unknown profile: {name}", "profiles": list(self.profiles())}
        unknown = [a for a in p.members if self.get(a) is None]
        known = [a for a in p.members if self.get(a) is not None]
        # A desktop app is started by open() itself (which waits for it to be
        # ready before opening the window); starting it here too would race.
        plain = [a for a in known if a not in p.desktop]
        desktop = [a for a in known if a in p.desktop]
        n = max(2, len(known) + len(p.commands))
        with ThreadPoolExecutor(max_workers=n) as pool:
            fut_apps = [pool.submit(self.start, a, False) for a in plain]
            fut_cmds = [pool.submit(self.commands.start, c) for c in p.commands]
            fut_desk = [pool.submit(self.open, a, "window", True) for a in desktop]
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
        cmd_res = [self.commands.stop(c) for c in p.commands]
        app_res = [self.stop(a) for a in p.members if self.get(a) is not None]
        results = cmd_res + app_res
        errors = [r.get("error") for r in results if not r.get("ok")]
        return {"ok": not errors, "profile": name, "apps": app_res, "commands": cmd_res,
                **({"error": "; ".join(str(e) for e in errors)} if errors else {})}

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
        result = {"reachable": False, "url": None, "status": None, "body": None}
        try:
            for url in self.config.faustus_urls:
                status, body = procs.fetch_json(url.rstrip("/") + "/api/health", timeout=3.0)
                if status is not None:
                    result = {"reachable": status < 500, "url": url, "status": status,
                              "body": body if isinstance(body, dict) else None}
                    break
        finally:
            self._faustus_cache = (time.time(), result)
            self._faustus_refreshing.clear()
        return result

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
                         faustus_token=cfg.faustus_token, comfy_url=cfg.comfy_url, capabilities=cfg.capabilities)
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
