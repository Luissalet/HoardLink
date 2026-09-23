"""Desktop windows for web apps, without packaging anything.

Every app in the family is a local web server with a UI. Its "desktop
version" is that UI in a window of its own — no tabs, no address bar, its
own taskbar entry and icon — which any installed Chromium (Edge, Chrome,
Chromium, Brave) provides with ``--app=<url>``. The hub gives each app its
own ``--user-data-dir`` under ``<data>/profiles/<id>``: that is what makes
the window a separate process the hub can find again later (by that
directory in the command line) and close, instead of a tab handed to
whatever browser was already open. The first launch of a profile is a
little slower; afterwards the profile remembers the window size.

``pywebview`` is used for the hub's own window when it is installed
(``pip install hoard-link[desktop]``); the app windows always use the
Chromium ``--app`` route, because a pywebview window has to live on the
main thread of the process that created it, which the hub cannot offer to
every app at once.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import webbrowser
from typing import Any, Optional

from .procs import _psutil, proc_info, terminate_tree

_WINDOWS_CANDIDATES = {
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    ],
    "brave": [r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"],
    "chromium": [],
}
_UNIX_CANDIDATES = {
    "edge": ["microsoft-edge", "microsoft-edge-stable"],
    "chrome": ["google-chrome", "google-chrome-stable"],
    "chromium": ["chromium", "chromium-browser"],
    "brave": ["brave-browser", "brave"],
}
_MAC_CANDIDATES = {
    "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
    "brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
}
_ORDER = ("edge", "chrome", "chromium", "brave")
_BROWSER_NAMES = ("msedge", "chrome", "chromium", "brave")


def find_browser(preference: str = "auto") -> Optional[str]:
    """Path to a Chromium-based browser executable, or None. ``preference``
    is ``auto``, one of edge/chrome/chromium/brave, or an explicit path."""
    pref = (preference or "auto").strip()
    if pref not in ("auto", *_ORDER):
        return pref if os.path.isfile(pref) or shutil.which(pref) else None
    names = [pref] if pref != "auto" else list(_ORDER)
    if sys.platform.startswith("win"):
        table = _WINDOWS_CANDIDATES
    elif sys.platform == "darwin":
        table = _MAC_CANDIDATES
    else:
        table = _UNIX_CANDIDATES
    for name in names:
        for candidate in table.get(name, []):
            if candidate and os.path.isfile(candidate):
                return candidate
            found = shutil.which(candidate) if candidate and os.sep not in candidate else None
            if found:
                return found
    return None


def profile_dir(profiles_root: str, app_id: str) -> str:
    return os.path.join(os.path.abspath(profiles_root), app_id)


def open_window(
    url: str, app_id: str, profiles_root: str, *, browser: str = "auto", size: tuple[int, int] = (1280, 860)
) -> dict[str, Any]:
    """Open ``url`` as a Chromium ``--app`` window with its own profile.
    Falls back to the default browser (a tab) when no Chromium is found."""
    exe = find_browser(browser)
    if exe is None:
        opened = webbrowser.open(url)
        return {"ok": bool(opened), "mode": "browser-tab", "detail": "no Chromium-based browser found; opened a tab instead"}
    profile = profile_dir(profiles_root, app_id)
    _prepare_profile(profile)
    cmd = [
        exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-sync", "--disable-features=" + ",".join(_DISABLED_FEATURES), f"--window-size={size[0]},{size[1]}",
    ]
    kwargs: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not open the window: {exc}"}
    return {"ok": True, "mode": "app-window", "pid": proc.pid, "browser": exe, "profile": profile}


#: Chromium/Edge features that only get in the way of an app window: the
#: welcome page, the "sign in to sync" sheet, translate bubbles, sidebars.
_DISABLED_FEATURES = (
    "Translate", "TranslateUI", "msEdgeWelcomePage", "msHubApps", "msImplicitSignin", "msEdgeSyncPromo",
    "msSidebar", "msHubSidebar", "msEdgeShoppingUI", "msEdgeMouseGestureDefaultEnabled",
)

_PROFILE_PREFS = {
    "translate": {"enabled": False},
    "translate_blocked_languages": ["es", "en"],
    "browser": {"has_seen_welcome_page": True, "show_hub_apps_tower": False, "show_home_button": False},
    "sync_promo": {"user_skipped": True, "show_on_first_run_allowed": False},
    "intl": {"accept_languages": "es,en"},
}


def _prepare_profile(profile: str) -> None:
    """Create the profile with a Preferences file that already says no to
    the first-run nuisances (translate bubble, sync sheet, welcome page).
    Only on first use: once Chromium owns the file it is left alone."""
    default = os.path.join(profile, "Default")
    prefs = os.path.join(default, "Preferences")
    if os.path.exists(prefs):
        return
    try:
        os.makedirs(default, exist_ok=True)
        with open(prefs, "w", encoding="utf-8") as fh:
            json.dump(_PROFILE_PREFS, fh)
        with open(os.path.join(profile, "First Run"), "w", encoding="utf-8") as fh:
            fh.write("")
    except OSError:
        pass


def list_windows(profiles_root: str) -> dict[str, list[dict[str, Any]]]:
    """``{app_id: [process, ...]}`` for every Chromium process whose command
    line carries one of the hub's profile folders. Only the root of each
    browser tree is listed (renderers share the flag)."""
    ps = _psutil()
    out: dict[str, list[dict[str, Any]]] = {}
    if ps is None:
        return out
    root = os.path.abspath(profiles_root).lower().rstrip("\\/")
    needle = root + ("\\" if sys.platform.startswith("win") else "/")
    found: list[tuple[str, Any]] = []
    for p in ps.process_iter(["pid", "name"]):
        # Command lines are expensive to read (very much so on Windows):
        # only browsers can carry our profile flag, so look at those alone.
        name = (p.info.get("name") or "").lower()
        if not any(b in name for b in _BROWSER_NAMES):
            continue
        try:
            cmd = p.cmdline() or []
        except Exception:  # noqa: BLE001
            continue
        for arg in cmd:
            a = str(arg).lower()
            if a.startswith("--user-data-dir=") and a[len("--user-data-dir="):].startswith(needle):
                app_id = a[len("--user-data-dir=") + len(needle):].split("\\")[0].split("/")[0]
                found.append((app_id, p))
                break
    pids = {p.pid for _, p in found}
    for app_id, p in found:
        try:
            if p.ppid() in pids:
                continue  # a renderer/gpu child of a window we already list
        except Exception:  # noqa: BLE001
            pass
        out.setdefault(app_id, []).append(proc_info(p.pid).to_dict())
    return out


def close_windows(app_id: str, profiles_root: str) -> dict[str, Any]:
    wins = list_windows(profiles_root).get(app_id, [])
    if not wins:
        return {"ok": True, "closed": [], "detail": "no window open"}
    closed: list[int] = []
    errors: list[str] = []
    for w in wins:
        res = terminate_tree(int(w["pid"]), grace_s=3.0)
        if res.get("ok"):
            closed.append(int(w["pid"]))
        else:
            errors.append(str(res.get("error")))
    return {"ok": not errors, "closed": closed, "errors": errors}


def open_in_browser(url: str) -> dict[str, Any]:
    try:
        return {"ok": bool(webbrowser.open(url)), "mode": "browser-tab"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def open_folder(path: str) -> dict[str, Any]:
    if not os.path.isdir(path):
        return {"ok": False, "error": "folder not found"}
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def hub_window_native_available() -> bool:
    try:
        import webview  # type: ignore # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False
