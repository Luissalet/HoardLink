"""Manage explicitly isolated, headless app processes owned by this Hub."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from .. import proc
from ..atomic import write_json_atomic
from . import contract, procs
from .config import _configured_paths
from .isolation import inside, passive_error
from .loopback_ports import free_loopback_ports
from .registry import MANIFEST_NAME, App, _writer_source, read_manifest

# These shipped entry points accept the shared service flags. Older manifests
# for them predate the isolated_instance declaration.
_SERVICE_MODULES = {
    "cicero": ("cicero_hoard", "CICERO"),
    "nikias": ("nikias_hoard", "NIKIAS"),
    "phidias": ("phidias_hoard", "PHIDIAS"),
    "prospero": ("prosperos_hoard", "PROSPERO"),
    "vulcan": ("vulcan", "VULCAN"),
}
_ENV_MODULES = {
    "coase": ("coase_hoard", "COASE_PROFILE_DIR", "COASE_PORT", "COASE_TOKEN_FILE"),
    "hypatia": ("hypatia", "HYPATIA_DATA_DIR", "HYPATIA_PORT", None),
}


@dataclass
class RunningInstance:
    app: App
    folder: Path
    ports: tuple[int, ...]
    process: object = None


def _failure(message, status=400):
    return {"ok": False, "status": status, "error": message}


def _manifest_isolation(app):
    descriptor = app.family.get("isolated_instance")
    return descriptor if isinstance(descriptor, dict) else {}


def _unsupported_reason(app):
    unavailable = _manifest_isolation(app).get("unsupported")
    if isinstance(unavailable, dict) and all(
        isinstance(unavailable.get(lang), str) and unavailable[lang].strip() for lang in ("en", "es")
    ):
        return f"{unavailable['en']} {unavailable['es']}"
    return ""


def _isolation_reason(app):
    return _unsupported_reason(app) or app.launch_reason or "No headless launcher. No hay un lanzador sin ventana."


def _manifest_launcher(descriptor):
    argv = descriptor.get("argv", [])
    env = descriptor.get("env", {})
    data_subdir = descriptor.get("data_subdir", "data")
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        return False
    if not isinstance(env, dict) or not all(
        re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and isinstance(value, str) for key, value in env.items()
    ):
        return False
    if (not isinstance(data_subdir, str) or not re.fullmatch(r"[\w-]+", data_subdir)):
        return False
    for field in ("data_env", "port_env", "token_env"):
        name = descriptor.get(field)
        if field in descriptor and (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)):
            return False
    return (bool(descriptor.get("data_env")) or any("{DATA_DIR}" in arg for arg in argv)) and (
        bool(descriptor.get("port_env")) or any("{PORT}" in arg for arg in argv)
    )


def _headless_launcher(app):
    """Identify a native launcher whose profile and display can be controlled."""
    descriptor = _manifest_isolation(app)
    if "unsupported" in descriptor or not app.launch or not app.launchable:
        return None
    if app.id == "writer":
        if not _writer_source(app):
            return None
        try:
            script = Path(app.folder) / "scripts" / "dev-desktop.mjs"
            main = Path(app.folder) / "electron" / "main.ts"
            if ("WH_RENDERER_PORT" in script.read_text(encoding="utf-8")
                    and "WH_HEADLESS" in main.read_text(encoding="utf-8")):
                return "writer"
        except OSError:
            pass
        return None
    if app.kind == "window-app":
        return None
    if _manifest_launcher(descriptor):
        return "manifest"
    module = _SERVICE_MODULES.get(app.id) or _ENV_MODULES.get(app.id)
    if module and "-m" in app.launch.argv:
        index = app.launch.argv.index("-m")
        if app.launch.argv[index + 1:index + 2] == [module[0]]:
            return "service" if app.id in _SERVICE_MODULES else "env"
    return None


def _headless_app(app, folder, ports, config, launcher):
    port = ports[0]
    url = f"http://127.0.0.1:{port}"
    data_path = folder
    token = folder / "mcp-token"
    spec = app.launch
    argv = spec.argv
    changes = {}
    if launcher == "writer":
        token = folder / "aibridge" / "token"
        changes = {key: None for key in os.environ if key.startswith("WH_")}
        changes.update(WH_HEADLESS="1", WH_USER_DATA_DIR=str(folder), WH_AIBRIDGE_PORT=str(port),
                       WH_RENDERER_PORT=str(ports[1]), MEDIA_DOWNLOADER_PORT=str(ports[2]))
        argv = [argv[0], "--hub"]
    elif launcher == "service":
        module, prefix = _SERVICE_MODULES[app.id]
        # Keep interpreter flags such as -I. Replace service flags that may
        # contain the installed data path, a fixed port or browser options.
        argv = [*argv[:argv.index("-m")], "-m", module, "--port", str(port),
                "--data-dir", str(folder), "--no-browser"]
        changes = {prefix + "_DATA_DIR": str(folder), prefix + "_PORT": str(port),
                   prefix + "_TOKEN_FILE": str(token)}
    elif launcher == "env":
        module, data_env, port_env, token_env = _ENV_MODULES[app.id]
        argv = [*argv[:argv.index("-m")], "-m", module]
        changes = {data_env: str(folder), port_env: str(port)}
        if token_env:
            changes[token_env] = str(token)
        if app.id == "coase":
            changes["COASE_HOST"] = "127.0.0.1"
    else:
        descriptor = _manifest_isolation(app)
        if "data_subdir" in descriptor:
            data_path = folder / descriptor["data_subdir"]
        token = data_path / "mcp-token"
        values = {"INSTANCE_DIR": str(folder), "DATA_DIR": str(data_path), "PORT": str(port),
                  "TOKEN_FILE": str(token)}

        def expand(template):
            return re.sub(r"\{(INSTANCE_DIR|DATA_DIR|PORT|TOKEN_FILE)\}", lambda match: values[match[1]], template)

        argv = [expand(arg) for arg in descriptor["argv"]] if "argv" in descriptor else argv
        changes = {key: expand(value) for key, value in descriptor.get("env", {}).items()}
        for field, value in (("data_env", data_path), ("port_env", port), ("token_env", token)):
            if field in descriptor:
                changes[descriptor[field]] = str(value)
    env = procs.app_launch_env(
        {**spec.env, "HOARD_TOKEN_FILE": None, **changes, "PORT_STRICT": "1", "HOARD_NO_BROWSER": "1",
         "HOARD_HUB_DATA_DIR": config.data_dir},
        config.url, app_id=app.id,
    )
    return replace(app, url=url, data_dir=str(data_path), token_file=str(token), kind="app",
                   runtime_url_file="", launch=replace(spec, argv=argv, env=env,
                                                        readiness_url=url + app.health_path, port_argument=""))


def prepare_writer_profile(app):
    config = Path(app.data_dir) / "aibridge" / "config.json"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(config, {"enabled": True, "writesEnabled": True})


def isolated_catalog(apps, config):
    """Keep fixture metadata while omitting installed URLs, tokens and launch commands."""
    root = config.validate_isolation()
    reason = "Starts only as an explicit fixture. Se inicia como prueba explícita."
    return [replace(app, url="", data_dir="", token_file="", runtime_url_file="", launch=None,
                    icon_path=app.icon_path if app.icon_path and inside(app.icon_path, root) else "",
                    launchable=False, launch_reason=_unsupported_reason(app) or reason) for app in apps]


class IsolatedApps:
    def __init__(self, instances):
        self.instances = instances
        self.hub = instances.hub
        self._lock = RLock()
        self._rows = {}
        self._jobs = getattr(self.hub, "fixture_jobs", None)
        self._closed = False

    def _fixture_launch(self, spec, folder, log):
        root = self.hub.config.validate_isolation()
        cwd = Path(spec.cwd).resolve()
        executable = cwd / spec.executable
        if not executable.is_file() and (resolved := shutil.which(spec.executable)):
            executable = Path(resolved)
        executable = executable.resolve()
        interpreter = Path(sys.executable).resolve()
        python_executables = {interpreter}
        if os.name == "nt" and interpreter.name.lower() in {"python.exe", "pythonw.exe"}:
            python_executables.update(interpreter.with_name(name).resolve() for name in ("python.exe", "pythonw.exe"))
        if (not inside(cwd, root) or not executable.is_file()
                or (executable not in python_executables and not inside(executable, root))
                or (os.name == "nt" and executable.suffix.lower() != ".exe")):
            raise OSError("Use a direct fixture executable and working directory inside the test folder. "
                          "Usa un ejecutable directo y un directorio de trabajo dentro de la prueba.")
        for arg in spec.argv:
            value = Path(arg.split("=", 1)[-1])
            if (value.is_absolute() or ".." in value.parts) and not inside(cwd / value, root):
                raise OSError("A fixture argument refers outside the test folder. "
                              "Un argumento de la prueba apunta fuera de la carpeta temporal.")
        for name, value in spec.env.items():
            for _, path in _configured_paths(value, name.lower()):
                if not inside(cwd / path, root):
                    raise OSError(f"Fixture setting {name} points outside the test folder. "
                                  f"La variable {name} apunta fuera de la carpeta temporal.")
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG") if key in os.environ}
        env.update({key: value for key, value in spec.env.items() if value is not None})
        env.update({key: str(folder) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP")})
        env["FAUSTUS_DIR"] = self.hub.config.faustus_dir or str(folder / "faustus")
        if self._jobs is None:
            if os.name != "nt":
                raise OSError("The fixture requires an isolated Linux service. La prueba necesita una unidad aislada.")
            from .windows_job import WindowsJobs

            self._jobs = WindowsJobs()
        return self._jobs.launch([str(executable), *spec.argv], cwd=str(cwd), env=env, log=log)

    def _launch(self, ident, row):
        spec = row.app.launch
        folder = row.folder
        app = row.app
        if app.id == "writer":
            prepare_writer_profile(app)
        accepted = self.instances.add(app.id, token_file=app.token_file, label=f"Isolated {ident}")
        if not accepted["ok"]:
            return accepted
        try:
            with (folder / "process.log").open("ab") as log:
                if self.hub.config.isolated:
                    row.process = self._fixture_launch(spec, folder, log)
                else:
                    row.process = proc.popen([spec.executable, *spec.argv], cwd=spec.cwd, env=proc.build_env(**spec.env),
                                             stdout=log, stderr=log)
        except (OSError, ValueError) as exc:
            self.instances.remove(app=app.id, token_file=app.token_file)
            return _failure(f"Could not start the isolated process. No se pudo arrancar el proceso aislado: {exc}", 409)
        self._rows[ident] = row
        return {"ok": True, "instance": self._describe(ident, row),
                "undo": {"action": "stop", "instance_id": ident}}

    def _new(self, args):
        app_id = args.get("app")
        if not isinstance(app_id, str):
            return _failure("App id is required")
        registered = self.hub.get(app_id)
        if not registered:
            return _failure("Unknown app", 404)
        worktree = args.get("worktree")
        if not isinstance(worktree, str) or not worktree:
            return _failure("An isolated app worktree is required")
        root = Path(worktree).resolve()
        registered_root = Path(registered.folder).resolve()
        if self.hub.config.isolated:
            if (root != registered_root or not inside(root, self.hub.config.validate_isolation())
                    or not inside(root / MANIFEST_NAME, root)):
                return _failure("Use a registered fixture inside the test folder. "
                                "Usa una app de prueba registrada en la carpeta temporal.")
        elif root == registered_root or not (root / ".git").is_file():
            return _failure("Use a linked worktree separate from the registered installation")
        launcher = read_manifest(root / MANIFEST_NAME, faustus_dir=self.hub.config.faustus_dir)
        if not launcher or launcher.id != app_id or not launcher.launch:
            return _failure("The worktree has no launch manifest for this app")
        ident = secrets.token_hex(12)
        folder = Path(self.hub.config.data_dir).resolve() / "isolated-apps" / ident
        if self.hub.config.isolated and not inside(folder, self.hub.config.validate_isolation()):
            return _failure("Fixture data must stay inside the test folder. Los datos deben quedar en la carpeta de prueba.")
        kind = _headless_launcher(launcher)
        if not kind:
            return _failure(_isolation_reason(launcher), 409)
        folder.mkdir(parents=True)
        owned_ports = (port for row in self._rows.values() for port in row.ports)
        ports = free_loopback_ports(3 if kind == "writer" else 1,
                                    exclude=(self.hub.config.port, *owned_ports))
        app = _headless_app(launcher, folder, ports, self.hub.config, kind)
        return self._launch(ident, RunningInstance(app, folder, ports))

    def _describe(self, ident, row):
        running = row.process is not None and (row.process.running() if self.hub.config.isolated else row.process.poll() is None)
        return {"id": ident, "app": row.app.id, "data_dir": str(row.folder), "url": row.app.url,
                "log": str(row.folder / "process.log"), "renderer_port": row.ports[1] if len(row.ports) > 1 else None,
                "media_port": row.ports[2] if len(row.ports) > 1 else None,
                "state": "running" if running else "stopped"}

    def manage(self, args):
        action = args.get("action")
        if action not in ("start", "status", "list", "call", "stop", "resume"):
            return _failure("Choose start, status, list, call, stop or resume")
        with self._lock:
            if self._closed or (self.hub.config.isolated and args.get("_automatic")):
                return passive_error()
            if action == "start":
                return self._new(args)
            if action == "list":
                return {"ok": True, "instances": [self._describe(ident, row) for ident, row in self._rows.items()]}
            ident = args.get("instance_id")
            row = self._rows.get(ident) if isinstance(ident, str) else None
            if not row:
                return _failure("Unknown isolated instance", 404)
            description = self._describe(ident, row)
            if action == "status":
                return {"ok": True, "instance": description}
            if action == "stop":
                active = description["state"] == "running"
                if self.hub.config.isolated and row.process is not None:
                    try:
                        self._jobs.stop(row.process)
                    except OSError as exc:
                        return _failure(f"Could not stop the fixture. No se pudo parar la prueba: {exc}", 409)
                    row.process = None
                elif active and not proc.kill_tree(row.process):
                    return _failure("The isolated process did not stop", 409)
                self.instances.remove(app=row.app.id, token_file=row.app.token_file)
                return {"ok": True, "instance": self._describe(ident, row),
                        "undo": {"action": "resume", "instance_id": ident} if active else None}
            if action == "resume":
                if description["state"] == "running":
                    return {"ok": True, "instance": description}
                if self.hub.config.isolated and row.process is not None:
                    self._jobs.stop(row.process)
                    row.process = None
                return self._launch(ident, row)
            if description["state"] != "running":
                return _failure("The isolated process is not running", 409)
            if not contract.read_token(row.app.token_file):
                return _failure("The isolated instance has not created its token yet", 503)
            app = row.app
        return contract.call_app(app, args.get("tool"), args.get("arguments"), reason=args.get("reason"),
                                 agent=args.get("agent") or "hub", timeout=45)

    def close(self):
        with self._lock:
            if self._jobs is not None:
                self._jobs.close()
                for row in self._rows.values():
                    self.instances.remove(app=row.app.id, token_file=row.app.token_file)
                    row.process = None
            else:
                for ident in list(self._rows):
                    self.manage({"action": "stop", "instance_id": ident})
            self._closed = True
