"""``python -m hoard_link.hub`` — start the hub and open its window.

    python -m hoard_link.hub                  # server + desktop window
    python -m hoard_link.hub --no-window      # server only (for a service, or an agent)
    python -m hoard_link.hub --browser        # server + a browser tab instead of a window
    python -m hoard_link.hub --port 8810 --roots "D:/apps;E:/more apps"
    python -m hoard_link.hub --profile video  # also start a profile once the hub is up

    python -m hoard_link.hub --install-autostart [--profile NAME] [--no-window | --window]
    python -m hoard_link.hub --uninstall-autostart
    python -m hoard_link.hub --autostart-status

The window is native (pywebview) when that package is installed, else a
Chromium ``--app`` window, else a browser tab. With ``exit_with_window``
(the default) the hub stops when its own window is closed; the apps it
started keep running, on purpose — the hub is a remote control, not a
parent. A second launch of the desktop Hub opens another window on the
running one. A Hub with its own port and data is headless.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import DEFAULT_PORT, HUB_VERSION, SERVICE
from .config import HubConfig
from .core import Hub
from .isolation import LinuxJobs, enter_linux_service, service_cgroup
from . import autostart, desktop, procs, ui_session
from .server import make_server


def _already_running(url: str) -> bool:
    status, body = procs.fetch_json(url.rstrip("/") + "/api/health", timeout=1.0)
    return status is not None and isinstance(body, dict) and body.get("service") == SERVICE


def _open_own_window(cfg: HubConfig, mode: str) -> Optional[int]:
    """Returns the pid of a Chromium --app window when that route was used."""
    if mode == "browser":
        desktop.open_in_browser(ui_session.launch_url(cfg.url, cfg.data_dir))
        return None
    icon = os.path.join(os.path.dirname(desktop.SHELL_DIR), "app-icon.png")
    res = desktop.open_window(cfg.url, desktop.HUB_WINDOW_ID, cfg.profiles_dir, browser=cfg.browser,
                              size=(int(cfg.window_size[0]), int(cfg.window_size[1])),
                              engine=cfg.window_engine, name="Hoard Hub", icon=icon,
                              browser_url=lambda: ui_session.launch_url(cfg.url, cfg.data_dir))
    return res.get("pid") if res.get("ok") and res.get("mode") == "app-window" else None


def _start_profile(hub: Hub, name: str) -> dict:
    log = logging.getLogger("hoard_hub")
    if name not in hub.profiles():
        log.warning("--profile %s: no such profile (have: %s)", name, ", ".join(hub.profiles()) or "none")
        return {"ok": False, "error": f"unknown profile: {name}"}
    res = hub.profile_start(name, startup=True)
    if res.get("ok"):
        log.info("profile %s started", name)
    else:
        log.warning("profile %s: %s", name, res.get("error") or "some members did not start")
    return res


def _start_profile_remote(url: str, name: str) -> dict:
    from urllib.parse import quote
    status, body = procs.fetch_json_post(url.rstrip("/") + f"/api/profiles/{quote(name, safe='')}/start", {"startup": True})
    return body if isinstance(body, dict) else {"ok": False, "error": f"HTTP {status}"}


def _autostart_command(args: argparse.Namespace) -> Optional[int]:
    """Handle --install/--uninstall-autostart and --autostart-status."""
    if not (args.install_autostart or args.uninstall_autostart or args.autostart_status):
        return None
    if args.install_autostart:
        env = dict(os.environ)
        if args.data_dir:
            env["HOARD_HUB_DATA_DIR"] = args.data_dir
        known = HubConfig.load(env=env).profiles or {}
        if args.profile and args.profile not in known:
            print(f"autostart: unknown profile '{args.profile}'; nothing installed")
            return 1
        extra: list[str] = []
        if args.port:
            extra += ["--port", str(args.port)]
        if args.data_dir:
            extra += ["--data-dir", os.path.abspath(args.data_dir)]
        if args.roots:
            extra += ["--roots", args.roots]
        res = autostart.install(args.profile, window=bool(args.window), extra=extra)
        if res.get("ok") and args.profile:
            env = dict(os.environ)
            if args.data_dir:
                env["HOARD_HUB_DATA_DIR"] = args.data_dir
            known = HubConfig.load(env=env).profiles or {}
            if args.profile not in known:
                print(f"warning: profile '{args.profile}' is not defined in hub.json yet "
                      f"(have: {', '.join(known) or 'none'})")
    elif args.uninstall_autostart:
        res = autostart.uninstall()
    else:
        res = autostart.status()
    print(autostart.describe(res))
    return 0 if res.get("ok") else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hoard-hub", description="Desktop hub for a family of local, agent-controlled apps.")
    parser.add_argument("--port", type=int, default=None, help=f"listen port (default {DEFAULT_PORT})")
    parser.add_argument("--roots", default=None, help="folders that contain app folders, ';'-separated")
    parser.add_argument("--data-dir", default=None, help="where hub.json, logs, profiles and the token live")
    parser.add_argument("--no-window", action="store_true", help="do not open the hub's own window")
    parser.add_argument("--browser", action="store_true", help="open a browser tab instead of a window")
    parser.add_argument("--native", action="store_true", help="force a pywebview window (needs pywebview)")
    parser.add_argument("--stay", action="store_true", help="keep serving after the window closes")
    parser.add_argument("--profile", default=None, help="start this profile (hub.json) once the hub is up")
    parser.add_argument("--install-autostart", action="store_true",
                        help="Windows: start the hub at login (Startup folder); with --profile, --no-window/--window")
    parser.add_argument("--uninstall-autostart", action="store_true", help="remove the login autostart")
    parser.add_argument("--autostart-status", action="store_true", help="show whether the login autostart is set")
    parser.add_argument("--window", action="store_true",
                        help="with --install-autostart: open the hub window at login (default: headless)")
    parser.add_argument("--isolated-unit", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=f"hoard-hub {HUB_VERSION}")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.data_dir:
        env["HOARD_HUB_DATA_DIR"] = args.data_dir
    if args.roots:
        env["HOARD_HUB_ROOTS"] = args.roots
    if args.port:
        env["HOARD_HUB_PORT"] = str(args.port)
    fixture_jobs = None
    try:
        cfg = HubConfig.load(env=env)
        if cfg.isolated:
            cfg.validate_isolation()
        elif args.isolated_unit:
            raise ValueError("An isolated unit requires an isolated Hub. La unidad necesita un Hub aislado.")
    except (OSError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if cfg.isolated:
        if args.profile or args.install_autostart or args.uninstall_autostart or args.autostart_status:
            print("Session and profile startup are unavailable in an isolated Hub. "
                  "El Hub aislado no admite el inicio de sesión ni de perfiles.", file=sys.stderr)
            return 2
        try:
            recorded_url = Path(cfg.url_file).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            recorded_url = ""
        except (OSError, UnicodeError) as exc:
            print(f"Could not read the Hub URL. No se pudo leer la URL del Hub: {exc}", file=sys.stderr)
            return 2
        if recorded_url == f"http://127.0.0.1:{DEFAULT_PORT}":
            print("An isolated Hub needs its own port and data folder. "
                  "El Hub aislado necesita su propio puerto y carpeta de datos.", file=sys.stderr)
            return 2
        args.no_window = True
        try:
            if sys.platform.startswith("linux"):
                if not args.isolated_unit:
                    return enter_linux_service(sys.argv[1:] if argv is None else argv, env)
                fixture_jobs = LinuxJobs(service_cgroup(args.isolated_unit))
            elif os.name != "nt" or args.isolated_unit:
                raise OSError("Isolated Hub startup requires Windows or a Linux systemd user service. "
                              "El Hub aislado necesita Windows o una unidad de usuario de systemd en Linux.")
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        code = _autostart_command(args)
        if code is not None:
            return code
    log_kwargs: dict = {}
    if sys.stderr is None:  # pythonw (the login autostart): no console, log to a file
        os.makedirs(cfg.logs_dir, exist_ok=True)
        log_kwargs["filename"] = os.path.join(cfg.logs_dir, "hub.log")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", **log_kwargs)

    if not cfg.isolated and _already_running(cfg.url):
        print(f"hoard-hub already running at {cfg.url}")
        if args.profile:
            res = _start_profile_remote(cfg.url, args.profile)
            print(f"profile {args.profile}: {'started' if res.get('ok') else res.get('error')}")
        if not args.no_window:
            _open_own_window(cfg, "browser" if args.browser else "window")
        return 0

    hub = Hub(cfg, fixture_jobs=fixture_jobs) if fixture_jobs is not None else Hub(cfg)
    try:
        server = make_server(hub)
    except OSError as exc:
        print(f"cannot listen on {cfg.url}: {exc}", file=sys.stderr)
        hub.close()
        return 2
    hub.write_url_file()
    thread = threading.Thread(target=server.serve_forever, name="hoard-hub-http", daemon=True)
    thread.start()
    if not cfg.isolated:
        hub.placement.link.start()      # Faustus on a Spark reaches this Hub through a tunnel while it is there
        keepalive = hub.facet("keepalive")
        if keepalive is not None and keepalive.serve():
            print("keep-alive: " + ", ".join(keepalive.apps()))
    print(f"hoard-hub {HUB_VERSION} at {cfg.url} — {len(hub.apps)} apps from {', '.join(cfg.roots)}")
    if args.profile:
        threading.Thread(target=_start_profile, args=(hub, args.profile), name="hoard-hub-profile", daemon=True).start()

    if args.no_window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
            hub.close()
        return 0

    use_native = (args.native or desktop.hub_window_native_available()) and not args.browser
    if use_native:
        try:
            import webview  # type: ignore
            url = ui_session.launch_url(cfg.url, cfg.data_dir)
            webview.create_window("Hoard Hub", url, width=int(cfg.window_size[0]), height=int(cfg.window_size[1]),
                                  min_size=(720, 480))
            webview.start()
            if not args.stay and cfg.exit_with_window:
                server.shutdown()
                return 0
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("hoard_hub").warning("native window failed (%s); using a browser window", exc)
            use_native = False
    if not use_native:
        pid = _open_own_window(cfg, "browser" if args.browser else "window")
        try:
            if pid and not args.stay and cfg.exit_with_window:
                ps = procs._psutil()
                if ps is not None:
                    try:
                        ps.Process(pid).wait()
                    except Exception:  # noqa: BLE001
                        pass
                    # The window may have been re-opened from the tray/other launch; give it a moment.
                    time.sleep(1.0)
                    if not desktop.list_windows(cfg.profiles_dir).get("_hub"):
                        server.shutdown()
                        return 0
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
