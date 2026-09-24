"""Start the hub when you log in (Windows), from ``python -m hoard_link.hub``:

    --install-autostart [--profile NAME] [--no-window | --window]
    --uninstall-autostart
    --autostart-status

On Windows this writes ``Hoard Hub.cmd`` in the user's Startup folder
(``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup``). The
script changes to this repository and launches
``pythonw -m hoard_link.hub --no-window [--profile NAME]`` detached, so the
hub runs headless in the background (no console left open) and, with a
profile, starts that profile's apps once it is up. ``--window`` opens the
hub's own window at login instead (it keeps serving when that window is
closed). Nothing is written to the registry and no admin rights are needed;
``--uninstall-autostart`` deletes the file.

Other platforms: a no-op with a clear message (use a systemd user unit or
a launchd agent that runs the same command).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from .config import REPO_DIR

SCRIPT_NAME = "Hoard Hub.cmd"
MARKER = "rem hoard-hub autostart"
UNSUPPORTED = ("autostart at login is only automated on Windows; on this system add a systemd user unit or "
               "launchd agent that runs: {cmd}")


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def startup_dir(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    env = env if env is not None else os.environ
    appdata = env.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def script_path(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    d = startup_dir(env)
    return d / SCRIPT_NAME if d else None


def pythonw(executable: Optional[str] = None) -> str:
    """``pythonw.exe`` next to the running interpreter (no console window),
    else the interpreter itself."""
    exe = Path(executable or sys.executable)
    for name in ("pythonw.exe", "pythonw"):
        cand = exe.with_name(name)
        if cand.is_file():
            return str(cand)
    return str(exe)


def hub_args(profile: Optional[str] = None, window: bool = False, extra: Optional[list[str]] = None) -> list[str]:
    args = ["-m", "hoard_link.hub"]
    args += ["--stay"] if window else ["--no-window"]
    if profile:
        args += ["--profile", profile]
    return args + list(extra or [])


def _q(arg: str) -> str:
    return '"' + arg.replace('"', '') + '"' if (not arg or re.search(r'[\s&|<>^()"]', arg)) else arg


def render(repo_dir: str, python: str, args: list[str]) -> str:
    """The Startup-folder script. ``start ""`` detaches, so the console that
    runs the script closes at once."""
    line = " ".join(_q(a) for a in [python, *args])
    return (
        "@echo off\r\n"
        f"{MARKER} (python -m hoard_link.hub --uninstall-autostart removes it)\r\n"
        f"cd /d {_q(repo_dir)}\r\n"
        f'start "" {line}\r\n'
    )


def install(profile: Optional[str] = None, window: bool = False, *, extra: Optional[list[str]] = None,
            env: Optional[Mapping[str, str]] = None, repo_dir: Optional[str] = None,
            executable: Optional[str] = None) -> dict[str, Any]:
    python = pythonw(executable)
    args = hub_args(profile, window, extra)
    if not _is_windows():
        return {"ok": False, "supported": False,
                "error": UNSUPPORTED.format(cmd=" ".join([python, *args]) + f"  (in {repo_dir or REPO_DIR})")}
    path = script_path(env)
    if path is None:
        return {"ok": False, "supported": True, "error": "APPDATA is not set: cannot find the Startup folder"}
    text = render(str(repo_dir or REPO_DIR), python, args)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        return {"ok": False, "supported": True, "error": f"could not write {path}: {exc}"}
    return {"ok": True, "supported": True, "path": str(path), "command": [python, *args], "profile": profile,
            "window": window}


def uninstall(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    if not _is_windows():
        return {"ok": True, "supported": False, "removed": False,
                "detail": "autostart at login is only automated on Windows; nothing to remove here"}
    path = script_path(env)
    if path is None or not path.exists():
        return {"ok": True, "supported": True, "removed": False, "path": str(path) if path else None,
                "detail": "not installed"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "supported": True, "error": str(exc)}
    if MARKER not in text:
        return {"ok": False, "supported": True, "path": str(path),
                "error": f"{path} was not written by the hub; leaving it alone"}
    try:
        path.unlink()
    except OSError as exc:
        return {"ok": False, "supported": True, "error": f"could not remove {path}: {exc}"}
    return {"ok": True, "supported": True, "removed": True, "path": str(path)}


def status(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    if not _is_windows():
        return {"ok": True, "supported": False, "installed": False,
                "detail": "autostart at login is only automated on Windows"}
    path = script_path(env)
    if path is None or not path.is_file():
        return {"ok": True, "supported": True, "installed": False, "path": str(path) if path else None}
    text = path.read_text(encoding="utf-8", errors="replace")
    start = next((l for l in text.splitlines() if l.lower().startswith("start ")), "")
    m = re.search(r'--profile\s+("([^"]*)"|(\S+))', start)
    return {"ok": True, "supported": True, "installed": True, "ours": MARKER in text, "path": str(path),
            "command": start[len('start ""'):].strip() if start else None,
            "profile": (m.group(2) or m.group(3)) if m else None, "window": "--no-window" not in start}


def describe(res: dict[str, Any]) -> str:
    """One or two human lines for the command line."""
    if not res.get("ok"):
        return "autostart: " + str(res.get("error"))
    if res.get("supported") is False:
        return "autostart: " + str(res.get("detail") or "not supported on this platform")
    if "installed" in res:
        if not res["installed"]:
            return f"autostart: not installed ({res.get('path')})"
        prof = f", profile {res['profile']}" if res.get("profile") else ""
        mode = "with its window" if res.get("window") else "headless"
        return f"autostart: installed, {mode}{prof}\n  {res['path']}\n  {res.get('command')}"
    if "removed" in res:
        return f"autostart: removed {res['path']}" if res["removed"] else f"autostart: {res.get('detail')}"
    prof = f" and start profile '{res['profile']}'" if res.get("profile") else ""
    return (f"autostart: installed — at login the hub starts {'with its window' if res.get('window') else 'headless'}"
            f"{prof}\n  {res['path']}")
