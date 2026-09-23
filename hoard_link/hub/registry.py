"""The apps the hub knows about, read from their own ``faustus-plugin.json``.

An app is a folder with a manifest in it. The manifest is the one the app
already ships for Faustus (``faustus-plugin.json``, schema 1), so nothing
new has to be written for the hub: it takes the same ``app`` block
(``url_default``, ``health``, ``launch_hint``) and resolves the same
placeholders — ``{NAME_DIR}`` is the app's own folder, ``{APP_URL}`` its
URL, ``{PYTHON}`` whatever the manifest defaults it to, ``{FAUSTUS_DIR}`` /
``{FAUSTUS_PYTHON}`` the hub's configuration (or this interpreter, for the
latter). A placeholder nobody can fill does not raise: the app is still
listed, with ``launchable = False`` and the reason, so a card can say "I
can show you this app but not start it, and here is why".

Nothing here touches the network or spawns anything; that is
``procs.py``. This module is pure reading, which is what makes it
testable with a temporary folder and two JSON files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

MANIFEST_NAME = "faustus-plugin.json"
ICON_NAMES = ("app-icon.png", "icon.png", "Icon.png", "app-icon.svg", "icon.svg")
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_MAX_DEPTH = 8


@dataclass
class LaunchSpec:
    executable: str
    argv: list[str]
    cwd: str
    readiness_url: Optional[str]
    readiness_timeout_s: float = 30.0
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "readiness_url": self.readiness_url,
            "readiness_timeout_s": self.readiness_timeout_s,
        }


@dataclass
class App:
    id: str
    name: str
    purpose: str
    folder: str
    url: str
    health_path: str
    expect_service: Optional[str]
    capabilities: list[str] = field(default_factory=list)
    icon_path: Optional[str] = None
    launch: Optional[LaunchSpec] = None
    launchable: bool = False
    launch_reason: str = ""
    manifest_path: str = ""
    notes: str = ""
    kind: str = "app"        # app | window-app (an exe that opens its own window)

    @property
    def port(self) -> Optional[int]:
        try:
            return urlsplit(self.url).port
        except ValueError:
            return None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or "127.0.0.1"

    def health_url(self) -> str:
        return self.url.rstrip("/") + self.health_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "purpose": self.purpose,
            "folder": self.folder,
            "url": self.url,
            "port": self.port,
            "health_url": self.health_url(),
            "expect_service": self.expect_service,
            "capabilities": list(self.capabilities),
            "has_icon": bool(self.icon_path),
            "launchable": self.launchable,
            "launch_reason": self.launch_reason,
            "launch": self.launch.to_dict() if self.launch else None,
            "kind": self.kind,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# placeholders
# ---------------------------------------------------------------------------

def _hub_values(folder: str, faustus_dir: Optional[str], faustus_python: Optional[str]) -> dict[str, str]:
    values = {"FAUSTUS_PYTHON": faustus_python or sys.executable}
    if faustus_dir:
        values["FAUSTUS_DIR"] = faustus_dir.rstrip("/\\")
    return values


def resolve_placeholders(
    template: str,
    *,
    folder: str,
    defaults: dict[str, str],
    extra: dict[str, str],
    missing: set[str],
    _depth: int = 0,
) -> str:
    """Fill ``{NAME}`` in ``template``. Any ``{X_DIR}`` (other than
    ``FAUSTUS_DIR``) is the app's folder; the rest come from ``extra`` (the
    hub's values) then the manifest ``defaults`` (themselves templates).
    Unknown names are left in place and recorded in ``missing``."""
    if _depth > _MAX_DEPTH:
        return template

    def one(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in extra:
            return extra[name]
        if name.endswith("_DIR") and name != "FAUSTUS_DIR":
            return folder
        if name in defaults:
            return resolve_placeholders(
                str(defaults[name]), folder=folder, defaults=defaults, extra=extra,
                missing=missing, _depth=_depth + 1,
            )
        missing.add(name)
        return match.group(0)

    return _PLACEHOLDER.sub(one, template)


def _executable_exists(executable: str, cwd: str) -> bool:
    if not executable:
        return False
    if os.sep in executable or "/" in executable:
        return os.path.isfile(executable) or os.path.isfile(os.path.join(cwd, executable))
    return shutil.which(executable) is not None


# ---------------------------------------------------------------------------
# reading one manifest
# ---------------------------------------------------------------------------

def _find_icon(folder: str, app_id: str, name: str, icon_dirs: Iterable[str]) -> Optional[str]:
    for candidate in ICON_NAMES:
        p = os.path.join(folder, candidate)
        if os.path.isfile(p):
            return p
    # A shared icon folder with loosely named files ("Babels hoard.png").
    wanted = {_slug(name), _slug(app_id), _slug(name.replace("'s", "s"))}
    for d in icon_dirs:
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            stem, ext = os.path.splitext(entry)
            if ext.lower() not in (".png", ".svg", ".ico", ".jpg", ".jpeg") :
                continue
            if _slug(stem) in wanted:
                return os.path.join(d, entry)
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def read_manifest(
    path: str | Path,
    *,
    faustus_dir: Optional[str] = None,
    faustus_python: Optional[str] = None,
    icon_dirs: Iterable[str] = (),
) -> Optional[App]:
    """One ``faustus-plugin.json`` → an :class:`App`, or None when the file
    is not a usable manifest (malformed JSON, no ``id``, no ``app`` block).
    Never raises."""
    path = str(path)
    folder = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    app_id = str(raw.get("id") or "").strip()
    app_block = raw.get("app")
    if not app_id or not isinstance(app_block, dict):
        return None
    if int(raw.get("schema") or 1) > 1:
        return None

    defaults = {str(k): str(v) for k, v in (raw.get("defaults") or {}).items()}
    extra = _hub_values(folder, faustus_dir, faustus_python)
    missing: set[str] = set()

    def fill(value: Any) -> str:
        return resolve_placeholders(str(value), folder=folder, defaults=defaults, extra=extra, missing=missing)

    url_default = str(app_block.get("url_default") or defaults.get("APP_URL") or "").strip()
    url = fill(url_default) if url_default else ""
    if not url:
        return None
    extra.setdefault("APP_URL", url.rstrip("/"))
    health = app_block.get("health") or {}
    health_path = str(health.get("path") or "/api/health")
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    expect = health.get("expect") or {}
    expect_service = str(expect.get("service")) if isinstance(expect, dict) and expect.get("service") else None

    name = str(raw.get("name") or app_id)
    app = App(
        id=app_id,
        name=name,
        purpose=str(raw.get("purpose") or ""),
        folder=folder,
        url=url.rstrip("/"),
        health_path=health_path,
        expect_service=expect_service,
        capabilities=[str(c) for c in (raw.get("capabilities") or [])],
        icon_path=_find_icon(folder, app_id, name, icon_dirs),
        manifest_path=path,
        notes=str(raw.get("notes") or ""),
    )

    hint = app_block.get("launch_hint")
    if not isinstance(hint, dict) or hint.get("kind", "process") != "process":
        app.launch_reason = "the manifest has no process launch hint"
        return app
    missing.clear()
    executable = fill(hint.get("executable") or "")
    argv = [fill(a) for a in (hint.get("argv") or [])]
    cwd = fill(hint.get("cwd") or folder) or folder
    readiness = hint.get("readiness") or {}
    readiness_url = fill(readiness.get("url")) if readiness.get("url") else app.health_url()
    try:
        timeout_s = float(readiness.get("timeout_s") or 30)
    except (TypeError, ValueError):
        timeout_s = 30.0
    env = {str(k): fill(v) for k, v in (hint.get("env") or {}).items()}
    app.launch = LaunchSpec(executable, argv, cwd, readiness_url, timeout_s, env)
    if executable.lower().endswith(".exe") and not argv and "python" not in os.path.basename(executable).lower():
        app.kind = "window-app"

    if missing:
        app.launch_reason = "unresolved placeholders: " + ", ".join(sorted(missing))
    elif not executable:
        app.launch_reason = "the launch hint has no executable"
    elif not _executable_exists(executable, cwd):
        app.launch_reason = f"executable not found: {executable}"
    elif not os.path.isdir(cwd):
        app.launch_reason = f"working directory not found: {cwd}"
    else:
        app.launchable = True
    return app


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------

def scan(
    roots: Iterable[str],
    *,
    faustus_dir: Optional[str] = None,
    faustus_python: Optional[str] = None,
    icon_dirs: Iterable[str] = (),
    exclude_ids: Iterable[str] = (),
) -> list[App]:
    """Every ``<root>/*/faustus-plugin.json`` (one level deep) plus any root
    that is itself an app folder, sorted by name. A duplicate ``id`` keeps
    the first folder found and drops the rest, so two clones of the same
    app never make two cards."""
    seen: dict[str, App] = {}
    excluded = set(exclude_ids)
    icon_dirs = list(icon_dirs)
    for root in roots:
        root = os.path.abspath(os.path.expanduser(str(root)))
        candidates: list[str] = []
        direct = os.path.join(root, MANIFEST_NAME)
        if os.path.isfile(direct):
            candidates.append(direct)
        elif os.path.isdir(root):
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                entries = []
            for entry in entries:
                p = os.path.join(root, entry, MANIFEST_NAME)
                if os.path.isfile(p):
                    candidates.append(p)
        for path in candidates:
            app = read_manifest(path, faustus_dir=faustus_dir, faustus_python=faustus_python, icon_dirs=icon_dirs)
            if app is None or app.id in excluded or app.id in seen:
                continue
            seen[app.id] = app
    return sorted(seen.values(), key=lambda a: a.name.lower())
