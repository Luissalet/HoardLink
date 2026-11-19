"""Manage explicitly isolated, headless app processes owned by this Hub."""
from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from .. import proc
from . import contract
from .registry import MANIFEST_NAME, App, read_manifest


@dataclass
class RunningInstance:
    app: App
    folder: Path
    launcher: App
    renderer_port: int
    process: object = None


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _failure(message, status=400):
    return {"ok": False, "status": status, "error": message}


class IsolatedApps:
    def __init__(self, instances):
        self.instances = instances
        self.hub = instances.hub
        self._lock = RLock()
        self._rows = {}

    def _launch(self, ident, row):
        spec = row.launcher.launch
        folder = row.folder
        app = row.app
        if app.id == "writer":
            config = folder / "aibridge" / "config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            if not config.exists():
                config.write_text('{"enabled":true,"writesEnabled":true}', encoding="utf-8")
            changes = {key: None for key in os.environ if key.startswith("WH_")}
            changes.update(WH_HEADLESS=1, WH_USER_DATA_DIR=folder, WH_AIBRIDGE_PORT=app.port,
                           WH_RENDERER_PORT=row.renderer_port)
        else:
            descriptor = row.launcher.family["isolated_instance"]
            changes = {descriptor["data_env"]: folder, descriptor["port_env"]: app.port}
            if descriptor.get("token_env"):
                changes[descriptor["token_env"]] = app.token_file
        env = proc.build_env(**{**spec.env, **changes, "HOARD_HUB_URL": self.hub.config.url,
                                "HOARD_HUB_DATA_DIR": self.hub.config.data_dir,
                                "HOARD_HUB_TOKEN_FILE": None})
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
        if app_id == "writer":
            script = root / "scripts" / "dev-desktop.mjs"
            try:
                compatible = ("WH_RENDERER_PORT" in script.read_text(encoding="utf-8") and
                              "WH_HEADLESS" in (root / "electron" / "main.ts").read_text(encoding="utf-8"))
            except OSError:
                compatible = False
            if not compatible:
                return _failure("This worktree needs the native headless profile and separate renderer port", 409)
            token = folder / "aibridge" / "token"
        else:
            descriptor = launcher.family.get("isolated_instance")
            if (not isinstance(descriptor, dict) or not descriptor.get("data_env") or not descriptor.get("port_env")
                    or launcher.kind == "window-app"):
                return _failure("The app does not declare a headless isolated launcher", 409)
            token = folder / "mcp-token"
        folder.mkdir(parents=True)
        port = _free_port()
        renderer_port = _free_port() if app_id == "writer" else 0
        while renderer_port and renderer_port == port:
            renderer_port = _free_port()
        app = replace(launcher, url=f"http://127.0.0.1:{port}", data_dir=str(folder), token_file=str(token))
        return self._launch(ident, RunningInstance(app, folder, launcher, renderer_port))

    @staticmethod
    def _describe(ident, row):
        return {"id": ident, "app": row.app.id, "data_dir": str(row.folder), "url": row.app.url,
                "log": str(row.folder / "process.log"), "renderer_port": row.renderer_port or None,
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
