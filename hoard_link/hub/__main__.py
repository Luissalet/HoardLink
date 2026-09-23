"""``python -m hoard_link.hub`` — start the hub and open its window.

    python -m hoard_link.hub                  # server + desktop window
    python -m hoard_link.hub --no-window      # server only (for a service, or an agent)
    python -m hoard_link.hub --browser        # server + a browser tab instead of a window
    python -m hoard_link.hub --port 8810 --roots "D:/apps;E:/more apps"

The window is native (pywebview) when that package is installed, else a
Chromium ``--app`` window, else a browser tab. With ``exit_with_window``
(the default) the hub stops when its own window is closed; the apps it
started keep running, on purpose — the hub is a remote control, not a
parent. A second launch while one hub is already listening just opens
another window on the running one.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from typing import Optional

from . import DEFAULT_PORT, HUB_VERSION, SERVICE
from .config import HubConfig
from .core import Hub
from . import desktop, procs
from .server import make_server


def _already_running(url: str) -> bool:
    status, body = procs.fetch_json(url.rstrip("/") + "/api/health", timeout=1.0)
    return status is not None and isinstance(body, dict) and body.get("service") == SERVICE


def _open_own_window(cfg: HubConfig, mode: str) -> Optional[int]:
    """Returns the pid of a Chromium --app window when that route was used."""
    if mode == "browser":
        desktop.open_in_browser(cfg.url)
        return None
    res = desktop.open_window(cfg.url, "_hub", cfg.profiles_dir, browser=cfg.browser,
                              size=(int(cfg.window_size[0]), int(cfg.window_size[1])))
    return res.get("pid") if res.get("ok") and res.get("mode") == "app-window" else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hoard-hub", description="Desktop hub for a family of local, agent-controlled apps.")
    parser.add_argument("--port", type=int, default=None, help=f"listen port (default {DEFAULT_PORT})")
    parser.add_argument("--roots", default=None, help="folders that contain app folders, ';'-separated")
    parser.add_argument("--data-dir", default=None, help="where hub.json, logs, profiles and the token live")
    parser.add_argument("--no-window", action="store_true", help="do not open the hub's own window")
    parser.add_argument("--browser", action="store_true", help="open a browser tab instead of a window")
    parser.add_argument("--native", action="store_true", help="force a pywebview window (needs pywebview)")
    parser.add_argument("--stay", action="store_true", help="keep serving after the window closes")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=f"hoard-hub {HUB_VERSION}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = dict(os.environ)
    if args.data_dir:
        env["HOARD_HUB_DATA_DIR"] = args.data_dir
    if args.roots:
        env["HOARD_HUB_ROOTS"] = args.roots
    if args.port:
        env["HOARD_HUB_PORT"] = str(args.port)
    cfg = HubConfig.load(env=env)

    if _already_running(cfg.url):
        print(f"hoard-hub already running at {cfg.url}; opening a window on it")
        if not args.no_window:
            _open_own_window(cfg, "browser" if args.browser else "window")
        return 0

    hub = Hub(cfg)
    try:
        server = make_server(hub)
    except OSError as exc:
        print(f"cannot listen on {cfg.url}: {exc}", file=sys.stderr)
        return 2
    hub.write_url_file()
    thread = threading.Thread(target=server.serve_forever, name="hoard-hub-http", daemon=True)
    thread.start()
    print(f"hoard-hub {HUB_VERSION} at {cfg.url} — {len(hub.apps)} apps from {', '.join(cfg.roots)}")

    if args.no_window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0

    use_native = (args.native or desktop.hub_window_native_available()) and not args.browser
    if use_native:
        try:
            import webview  # type: ignore
            webview.create_window("Hoard Hub", cfg.url, width=int(cfg.window_size[0]), height=int(cfg.window_size[1]),
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
