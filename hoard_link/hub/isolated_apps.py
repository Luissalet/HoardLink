"""Manage explicitly isolated, headless app processes owned by this Hub."""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from .. import proc
from ..atomic import write_json_atomic
from . import contract
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


def _free_ports(count, *, exclude=()):
    with ExitStack() as stack:
        ports = []
        while len(ports) < count:
            sock = stack.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            if port not in exclude:
                ports.append(port)
        return tuple(ports)


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
    env = {**spec.env, "HOARD_TOKEN_FILE": None, **changes, "PORT_STRICT": "1", "HOARD_NO_BROWSER": "1",
           "HOARD_HUB_URL": config.url, "HOARD_HUB_DATA_DIR": config.data_dir, "HOARD_HUB_TOKEN_FILE": None}
    return replace(app, url=url, data_dir=str(data_path), token_file=str(token), kind="app",
                   runtime_url_file="", launch=replace(spec, argv=argv, env=env,
                                                        readiness_url=url + app.health_path, port_argument=""))


def prepare_writer_profile(app):
    config = Path(app.data_dir) / "aibridge" / "config.json"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(config, {"enabled": True, "writesEnabled": True})


def isolated_catalog(apps, config):
    """Give every launchable app its own data, token, and stable loopback ports."""
    rows = []
    assigned = {config.port}
    for app in apps:
        launcher = _headless_launcher(app) if re.fullmatch(r"[a-zA-Z0-9_-]+", app.id) else None
        if not launcher:
            reason = _isolation_reason(app)
            rows.append(replace(app, url="", data_dir="", token_file="", runtime_url_file="",
                                launch=None, launchable=False, launch_reason=reason))
            continue
        folder = Path(config.data_dir) / "apps" / app.id
        marker = folder / "ports.json"
        count = 3 if launcher == "writer" else 1
        try:
            ports = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ports = []
        if (not isinstance(ports, list) or len(ports) != count
                or any(type(port) is not int or not 1024 <= port <= 65535 for port in ports)
                or len(set(ports)) != count or assigned.intersection(ports)):
            ports = _free_ports(count, exclude=assigned)
            folder.mkdir(parents=True, exist_ok=True)
            write_json_atomic(marker, ports)
        assigned.update(ports)
        rows.append(_headless_app(app, folder, ports, config, launcher))
    return rows


class IsolatedApps:
    def __init__(self, instances):
        self.instances = instances
        self.hub = instances.hub
        self._lock = RLock()
        self._rows = {}

    def _launch(self, ident, row):
        spec = row.app.launch
        folder = row.folder
        app = row.app
        if app.id == "writer":
            prepare_writer_profile(app)
        env = proc.build_env(**spec.env)
        accepted = self.instances.add(app.id, token_file=app.token_file, label=f"Isolated {ident}")
        if not accepted["ok"]:
            return accepted
        try:
            with (folder / "process.log").open("ab") as log:
                row.process = proc.popen([spec.executable, *spec.argv], cwd=spec.cwd, env=env,
                                         stdout=log, stderr=log)
        except OSError as exc:
            self.instances.remove(app=app.id, token_file=app.token_file)
            return _failure(f"Could not start the isolated process: {exc}", 409)
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
        if root == Path(registered.folder).resolve() or not (root / ".git").is_file():
            return _failure("Use a linked worktree separate from the registered installation")
        launcher = read_manifest(root / MANIFEST_NAME, faustus_dir=self.hub.config.faustus_dir)
        if not launcher or launcher.id != app_id or not launcher.launch:
            return _failure("The worktree has no launch manifest for this app")
        ident = secrets.token_hex(12)
        folder = Path(self.hub.config.data_dir).resolve() / "isolated-apps" / ident
        kind = _headless_launcher(launcher)
        if not kind:
            return _failure(_isolation_reason(launcher), 409)
        folder.mkdir(parents=True)
        ports = _free_ports(3 if kind == "writer" else 1)
        app = _headless_app(launcher, folder, ports, self.hub.config, kind)
        return self._launch(ident, RunningInstance(app, folder, ports))

    @staticmethod
    def _describe(ident, row):
        return {"id": ident, "app": row.app.id, "data_dir": str(row.folder), "url": row.app.url,
                "log": str(row.folder / "process.log"), "renderer_port": row.ports[1] if len(row.ports) > 1 else None,
                "media_port": row.ports[2] if len(row.ports) > 1 else None,
                "state": "running" if row.process and row.process.poll() is None else "stopped"}

    def manage(self, args):
        action = args.get("action")
        if action not in ("start", "status", "list", "call", "stop", "resume"):
            return _failure("Choose start, status, list, call, stop or resume")
        with self._lock:
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
                if active and not proc.kill_tree(row.process):
                    return _failure("The isolated process did not stop", 409)
                self.instances.remove(app=row.app.id, token_file=row.app.token_file)
                return {"ok": True, "instance": self._describe(ident, row),
                        "undo": {"action": "resume", "instance_id": ident} if active else None}
            if action == "resume":
                if description["state"] == "running":
                    return {"ok": True, "instance": description}
                return self._launch(ident, row)
            if description["state"] != "running":
                return _failure("The isolated process is not running", 409)
            if not contract.read_token(row.app.token_file):
                return _failure("The isolated instance has not created its token yet", 503)
            app = row.app
        return contract.call_app(app, args.get("tool"), args.get("arguments"), reason=args.get("reason"),
                                 agent=args.get("agent") or "hub", timeout=45)

    def close(self):
        for ident in list(self._rows):
            self.manage({"action": "stop", "instance_id": ident})
