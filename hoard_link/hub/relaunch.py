"""What a pinned taskbar button runs.

Windows pins a window by the command that reopens it. A Hoard window is
drawn by the shared window shell, so on its own a pin would start a bare
shell with nothing behind it. The shell gives Windows this script
instead: with no arguments it brings the Hub back (a window on the
running Hub, or the Hub itself when it is not up); with ``--app <id>`` it
makes sure the Hub is up and asks it to open that app, which also starts
the app when it is stopped.

Run it as a file (``pythonw relaunch.py``): it works from any folder.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import quote

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _wait_for_hub(url: str, seconds: float = 40.0) -> bool:
    from hoard_link.hub.__main__ import _already_running

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _already_running(url):
            return True
        time.sleep(0.5)
    return False


def _start_hub_headless() -> None:
    from hoard_link.hub import autostart

    kwargs: dict = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([autostart.pythonw(), *autostart.hub_args()], cwd=REPO_DIR, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hoard-relaunch")
    parser.add_argument("--app", default="", help="open this app's window instead of the Hub's")
    args = parser.parse_args(argv)

    os.chdir(REPO_DIR)
    if REPO_DIR not in sys.path:
        sys.path.insert(0, REPO_DIR)
    from hoard_link.hub import procs
    from hoard_link.hub.__main__ import _already_running, main as hub_main
    from hoard_link.hub.config import HubConfig

    if not args.app:
        return hub_main([])

    url = HubConfig.load(env=dict(os.environ)).url
    if not _already_running(url):
        _start_hub_headless()
        if not _wait_for_hub(url):
            return 2
    status, body = procs.fetch_json_post(url.rstrip("/") + f"/api/apps/{quote(args.app, safe='')}/open", {})
    return 0 if status == 200 and isinstance(body, dict) and body.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
