"""Discover local tools without starting applications or model services."""

from __future__ import annotations

import json
from hashlib import sha256
from importlib import metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Lock
import time
import tomllib
from uuid import uuid4

from ..atomic import write_json_atomic
from ..media import bins
from .config import REPO_DIR, _locked_config
from .facets import Facet, Reply

API = "/api/programs"
USAGE = {
    "blender": "blender --background scene.blend --python script.py",
    "godot": "godot --headless --path <project> --script res://check.gd",
    "orcaslicer": "orca-slicer model.3mf --datadir <profile-dir> --slice 0 --export-3mf sliced.3mf",
    "libreoffice": "soffice --headless -env:UserInstallation=file:///<profile> --convert-to pdf --outdir <dir> <file>",
    "ffmpeg": "ffmpeg -nostdin -i <input> <output>",
    "playwright": "python <script.py>  # chromium.launch(headless=True)",
    "python": "python <script.py>",
}


def _first_file(paths):
    return next((path for path in paths if path and Path(path).is_file()), None)


def _newest(paths):
    return sorted(paths, key=lambda path: tuple(map(int, re.findall(r"\d+", path.parent.name))), reverse=True)


def _version(path, flag="--version"):
    if not path:
        return None
    try:
        result = subprocess.run(
            [str(path), flag], capture_output=True, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
        return lines[0].strip() if lines else None
    return None


def _orca_installations():
    """Read installed versions without starting the GUI binary."""
    if os.name != "nt":
        return {}
    import winreg

    installs = {}
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OrcaSlicer"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as entry:
                    version = winreg.QueryValueEx(entry, "DisplayVersion")[0]
                    try:
                        folder = winreg.QueryValueEx(entry, "InstallLocation")[0]
                    except FileNotFoundError:
                        folder = ""
                    if not folder:
                        uninstall = winreg.QueryValueEx(entry, "UninstallString")[0].strip()
                        folder = Path(uninstall.split('"')[1] if uninstall.startswith('"') else uninstall).parent
                    executable = Path(folder) / "orca-slicer.exe"
                    installs[os.path.normcase(str(executable))] = version
            except (OSError, IndexError):
                continue
    return installs


def _comfy_version(folder):
    try:
        text = (folder / "comfyui_version.py").read_text(encoding="utf-8")
        match = re.search(r"""^__version__\s*=\s*["']([^"']+)""", text, re.MULTILINE)
        if match:
            return match[1]
    except OSError:
        pass
    try:
        return tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, ValueError, KeyError):
        return None


def _row(id_, name, path, version, headless=None, **extra):
    # Preserve virtualenv paths: resolving a POSIX python symlink gives the base interpreter.
    return {
        "id": id_, "name": name, "path": os.path.abspath(path) if path else None,
        "version": version, "headless": headless or USAGE[id_],
        "source": "detected", **extra,
    }


def _venv_python(folder):
    return _first_file((folder / "Scripts" / "python.exe", folder / "bin" / "python"))


def _python_version(path):
    settings = Path(path).parent.parent / "pyvenv.cfg"
    try:
        content = settings.read_text(encoding="utf-8")
        match = re.search(r"^version\s*=\s*(\S+)", content, re.MULTILINE)
        if match:
            return match[1]
    except OSError:
        pass
    return _version(path)


def _python_paths(hub, comfy, comfy_python, tool_dirs):
    config = hub.config
    app_dirs = [(app.name, Path(app.folder)) for app in hub.apps]
    if config.faustus_dir:
        app_dirs.append(("Faustus", Path(config.faustus_dir)))
    if comfy:
        app_dirs.append(("ComfyUI", comfy))
    app_dirs.append(("Hoard Hub", REPO_DIR))
    candidates = [
        ("hub", "Hoard Hub", sys.executable), ("base", "Base", getattr(sys, "_base_executable", None)),
        ("path", "PATH", shutil.which("python") or shutil.which("python3")),
        ("faustus", "Faustus", config.faustus_python), ("comfyui", "ComfyUI", comfy_python),
    ]
    for app in hub.apps:
        executable = getattr(app.launch, "executable", "")
        if executable and Path(executable).is_absolute() and re.fullmatch(r"python(?:\d+(?:\.\d+)?)?(?:\.exe)?",
                                                           Path(executable).name, re.IGNORECASE):
            candidates.append((app.id, app.name, executable))
    for directory in tool_dirs:
        for pattern in ("*/pyvenv.cfg", "engines/*/pyvenv.cfg"):
            for config_file in sorted(directory.glob(pattern)):
                env = config_file.parent
                candidates.append((env.name, env.name, _venv_python(env)))
    environments = set()
    for label, directory in app_dirs:
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for env in children:
            if ((env / "pyvenv.cfg").is_file() or env.name in (".venv", "venv", "env")):
                candidates.append((env.name, f"{label} · {env.name}", _venv_python(env)))
    for id_, label, value in candidates:
        if value and Path(value).is_file():
            key = os.path.normcase(os.path.abspath(value))
            if key not in environments:
                environments.add(key)
                yield id_, label, Path(value).absolute()


def _playwright(pythons):
    for id_, _, path in pythons:
        if id_ == "hub":
            try:
                return path, metadata.version("playwright")
            except metadata.PackageNotFoundError:
                pass
        env = path.parent.parent
        for pattern in ("Lib/site-packages/playwright-*.dist-info", "lib/python*/site-packages/playwright-*.dist-info"):
            for info in env.glob(pattern):
                return path, info.name.removeprefix("playwright-").removesuffix(".dist-info")
    return None, None


def portable_tool_dirs(roots):
    roots = {REPO_DIR, *(Path(root) for root in roots)}
    # A family checkout may be at <workspace>/IA/Hoards/Faustusables.
    # Look beside the workspace for portable installs without crawling disks.
    return tuple(sorted({parent / "Tools" for root in roots for parent in (root, *root.parents[:4])}))


def discover(hub, *, refresh=False):
    tool_dirs = hub.launcher.tool_dirs
    program_files = sorted({Path(os.environ[key]) for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
                            if os.environ.get(key)})
    blender = _first_file([
        shutil.which("blender"),
        *_newest(path for parent in [*program_files, *tool_dirs] for path in
                 parent.glob("Blender Foundation/Blender */blender.exe")),
    ])
    godot = _first_file([
        shutil.which("godot"), shutil.which("godot4"),
        *_newest(path for parent in tool_dirs for path in parent.glob("Godot*/Godot*_console.exe")),
        *_newest(path for parent in tool_dirs for path in parent.glob("Godot*/Godot*.exe")),
    ])
    orca_versions = _orca_installations()
    orca = _first_file([
        shutil.which("orca-slicer"), *orca_versions,
        *(parent / "OrcaSlicer" / "orca-slicer.exe" for parent in program_files),
    ])
    libreoffice = _first_file([
        shutil.which("soffice.com"), shutil.which("soffice"), shutil.which("libreoffice"),
        *(parent / "LibreOffice" / "program" / "soffice.com" for parent in program_files),
    ])
    ffmpeg = bins.find("ffmpeg", refresh=refresh)
    comfy, comfy_python, _ = hub.launcher.comfy_install()
    if comfy is None:
        main = _first_file(parent / "ComfyUI" / "main.py" for parent in tool_dirs)
        if main:
            comfy = main.parent
            comfy_python = _first_file(_venv_python(comfy / name) for name in ("venv", ".venv", "env"))
    pythons = list(_python_paths(hub, comfy, comfy_python, tool_dirs))
    playwright, playwright_version = _playwright(pythons)
    rows = [
        _row("blender", "Blender", blender, _version(blender)),
        _row("godot", "Godot", godot, _version(godot)),
        _row("orcaslicer", "OrcaSlicer", orca, orca_versions.get(os.path.normcase(str(orca)))),
        _row("libreoffice", "LibreOffice", libreoffice, _version(libreoffice)),
        _row("ffmpeg", "ffmpeg", ffmpeg.path, ffmpeg.version or None),
        _row("playwright", "Playwright", playwright, playwright_version),
        _row("comfyui", "ComfyUI", comfy / "main.py" if comfy else None, _comfy_version(comfy) if comfy else None,
             headless="Select a GPU under Hub → Services; then submit workflows through that instance’s HTTP API.",
             headless_es="Elige una GPU en Hub → Servicios y envía los flujos mediante la API HTTP de esa instancia.",
             interpreter=str(comfy_python) if comfy_python else None),
    ]
    for id_, label, path in pythons:
        rows.append(_row("python", f"Python · {label}", path, _python_version(path),
                         environment=id_) | {"id": "python:" + sha256(os.fsencode(os.path.normcase(path))).hexdigest()[:16]})
    return rows


class LocalPrograms(Facet):
    id = "local_programs"

    def __init__(self, hub):
        super().__init__(hub)
        self.path = Path(hub.config.data_dir) / "local-programs.json"
        self._scan_lock = Lock()
        self._detected = []
        self._expires_at = 0

    def _manual(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def list_programs(self, args):
        with self._scan_lock:
            if args.get("refresh") is True or time.monotonic() >= self._expires_at:
                self._detected = discover(self.hub, refresh=args.get("refresh") is True)
                self._expires_at = time.monotonic() + 300
            rows = self._detected
        manual = [{"id": id_, **row} for id_, row in self._manual().items()]
        return {"ok": True, "programs": [*rows, *manual]}

    def register(self, args):
        row = {key: str(args[key]).strip() if isinstance(args.get(key), str) else ""
               for key in ("name", "path", "version", "headless")}
        if not all(row[key] for key in ("name", "path", "headless")) or not Path(row["path"]).is_absolute():
            return {"ok": False, "error": "Supply a name, an absolute path and headless instructions"}
        row["path"] = os.path.normpath(row["path"])
        row["version"] = row["version"] or None
        row["source"] = "manual"
        with _locked_config(self.path):
            manual = self._manual()
            id_ = next((key for key, old in manual.items() if old == row), None)
            changed = id_ is None
            if changed:
                id_ = f"manual:{uuid4().hex}"
                manual[id_] = row
                write_json_atomic(self.path, manual)
        return {"ok": True, "changed": changed, "program": {"id": id_, **row},
                "undo": {"tool": "hub_program_undo", "arguments": {"id": id_}}}

    def undo(self, args):
        id_ = args.get("id")
        if not isinstance(id_, str) or not id_.startswith("manual:"):
            return {"ok": False, "error": "Supply the manual registration id"}
        with _locked_config(self.path):
            manual = self._manual()
            row = manual.pop(id_, None)
            if row is not None:
                write_json_atomic(self.path, manual)
        return {"ok": True, "changed": row is not None,
                **({"undo": {"tool": "hub_program_register", "arguments":
                    {key: row[key] for key in ("name", "path", "version", "headless")}}} if row else {})}

    def get(self, req):
        if req.path == API:
            return self.list_programs({"refresh": req.q_bool("refresh")})
        return None

    def post(self, req):
        action = {f"{API}/register": self.register, f"{API}/undo": self.undo}.get(req.path)
        if action is None:
            return None
        if not req.agent() and req.caller() != "ui":
            return Reply({"ok": False, "error": "Use the Hub page or agent credentials"}, 401)
        return action(req.body)

    @classmethod
    def tools(cls):
        text = {"type": "string", "minLength": 1}
        specs = [
            ("hub_programs", "List local programs and headless commands. Ver programas locales y uso sin ventana.",
             {"refresh": {"type": "boolean"}}, [], True),
            ("hub_program_register", "Register a local program and return undo. Registrar programa y devolver cómo deshacer.",
             {"name": text, "path": text, "version": {"type": ["string", "null"]}, "headless": text},
             ["name", "path", "headless"], False),
            ("hub_program_undo", "Undo a manual program registration. Deshacer el alta de un programa.",
             {"id": text}, ["id"], False),
        ]
        return [{"name": name, "description": description, "annotations": {"readOnlyHint": read},
                 "inputSchema": {"type": "object", "properties": props, "required": required,
                                 "additionalProperties": False}}
                for name, description, props, required, read in specs]

    def handlers(self):
        return {"hub_programs": self.list_programs, "hub_program_register": self.register,
                "hub_program_undo": self.undo}
