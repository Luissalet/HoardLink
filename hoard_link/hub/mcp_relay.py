"""Serve an app's declared stdio MCP over the existing authenticated Hub link.

Each HTTP session owns its child. Requests reuse that child until DELETE,
process exit or Hub shutdown. JSON-RPC is forwarded intact, including skills,
prompts, resources, notifications and requests from the server to its client.
Connecting starts only the MCP bridge. App startup remains an explicit Hub action.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit

from .. import proc
from . import contract, registry
from .config import REPO_DIR
from .facets import Facet, Reply

PREFIX = "/api/mcp/apps/"

REMOTE_HEALTH = '''import json, pathlib, sys, urllib.request, urllib.error
token = pathlib.Path(sys.argv[2])
headers = {"Accept": "application/json"}
if token.is_file():
    headers["Authorization"] = "Bearer " + token.read_text(encoding="utf-8-sig").strip()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    try:
        response = opener.open(urllib.request.Request(sys.argv[1], headers=headers), timeout=2)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        code, raw = response.code, response.read()
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    print(json.dumps([code, body]))
except OSError:
    print(json.dumps([None, None]))
'''


def placed_app(hub, app):
    from .placement import APPS
    placement = getattr(hub, "placement", None)
    if placement is None or app.id not in APPS:
        return None
    state = placement.state()
    if (state.get("job") or {}).get("running"):
        raise ValueError("app placement is moving; reconnect after the move finishes")
    return state if state.get("location") == "sparks" else None


def remote_values(app, manifest):
    from .placement import SPARK_STACK, PORTS
    folder = SPARK_STACK + "/" + app.id
    values = {"PYTHON": folder + "/venv/bin/python", "FAUSTUS_DIR": SPARK_STACK + "/faustus",
              "FAUSTUS_PYTHON": SPARK_STACK + "/faustus/venv/bin/python",
              "APP_URL": f"http://127.0.0.1:{PORTS[app.id]}"}
    missing = set()
    values["TOKEN_FILE"] = registry.resolve_placeholders(
        str((manifest.get("defaults") or {}).get("TOKEN_FILE") or folder + "/data/mcp-token"),
        folder=folder, defaults=manifest.get("defaults") or {}, extra=values, missing=missing)
    if missing:
        raise ValueError("unresolved MCP placeholders: " + ", ".join(sorted(missing)))
    return folder, values


def launch_spec(hub, app, *, local=False):
    """Resolve the installed manifest on the machine where its files live."""
    with open(app.manifest_path, encoding="utf-8-sig") as source:
        manifest = json.load(source)
    bridge = manifest.get("mcp") or {}
    if bridge.get("transport", "stdio") != "stdio" or not bridge.get("command"):
        raise ValueError("app has no stdio MCP server")
    values = registry._hub_values(app.folder, hub.config.faustus_dir, hub.config.faustus_python)
    values.update(APP_URL=app.url, TOKEN_FILE=app.token_file)
    placed = None if local else placed_app(hub, app)
    folder = app.folder
    if placed:
        # Placement installs a native venv and carries the app's data with it.
        # Expand the same declared bridge using paths on its current machine.
        folder, values = remote_values(app, manifest)
    missing = set()

    def fill(value):
        return registry.expand_env(registry.resolve_placeholders(
            str(value), folder=folder, defaults=manifest.get("defaults") or {}, extra=values, missing=missing))

    command = fill(bridge["command"])
    args = [fill(arg) for arg in bridge.get("args") or []]
    declared_env = {key: fill(value) for key, value in (bridge.get("env") or {}).items()}
    if missing:
        raise ValueError("unresolved MCP placeholders: " + ", ".join(sorted(missing)))
    if placed:
        from .placement import SSH_OPTS
        declared_env[app.id.upper() + "_BRIDGE_AUTOSTART"] = "0"
        remote = "cd " + shlex.quote(folder) + " && exec " + shlex.join(
            ["env", *(key + "=" + value for key, value in declared_env.items()), command, *args])
        return ["ssh", *SSH_OPTS, "-T", placed["node"]["ssh"], remote], dict(os.environ)
    return [command, *args], {**os.environ, **declared_env}


class Session:
    def __init__(self, app_id, argv, env, cwd):
        self.id = uuid.uuid4().hex
        self.app_id = app_id
        self.events = queue.Queue()
        self._pending = {}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = False
        self.process = proc.popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True, name="mcp-relay-" + app_id).start()
        threading.Thread(target=self._stderr, daemon=True, name="mcp-stderr-" + app_id).start()

    def _stderr(self):
        for _ in self.process.stderr:
            pass

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # tolerate a bridge's non-protocol startup line
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    continue
                with self._lock:
                    pending = self._pending.get(json.dumps(message.get("id"))) if "method" not in message else None
                if pending is not None:
                    pending.put(message)
                else:
                    self.events.put(message)
        finally:
            self.close()

    def send(self, message):
        with self._write_lock:
            if self._closed:
                raise OSError("MCP bridge closed")
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def request(self, message):
        key = json.dumps(message["id"])
        pending = queue.Queue()
        with self._lock:
            if self._closed:
                raise OSError("MCP bridge closed")
            if key in self._pending:
                raise ValueError("duplicate in-flight JSON-RPC id")
            self._pending[key] = pending
        try:
            self.send(message)
            result = pending.get()
            if result is None:
                raise OSError("MCP bridge exited before responding")
            return result
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for pending in self._pending.values():
                pending.put(None)
            self.events.put(None)
        # Kill only this session's process tree, including interpreter launchers.
        proc.kill_tree(self.process, grace_s=1)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except OSError:
                pass


class McpRelayFacet(Facet):
    id = "mcp_relay"

    def __init__(self, hub):
        super().__init__(hub)
        self._lock = threading.Lock()
        self._sessions = {}
        # The Hub intentionally omits itself from its app cards.
        self._own_app = registry.read_manifest(REPO_DIR / "faustus-plugin.json",
            faustus_dir=hub.config.faustus_dir, faustus_python=hub.config.faustus_python)
        self._own_app.url = hub.config.url
        self._own_app.token_file = hub.config.token_file

    def _app(self, app_id):
        return self._own_app if app_id == self._own_app.id else self.hub.get(app_id)

    def close(self):
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            session.close()

    def describe(self, app_id, *, check=False):
        app = self._app(app_id)
        if app is None:
            raise KeyError(app_id)
        launch_spec(self.hub, app)
        placed = placed_app(self.hub, app)
        result = {"id": app.id, "name": app.name, "app_url": app.url,
                  "transport": "http", "mcp_path": PREFIX + app.id + "/mcp"}
        if placed:
            result.update(location="sparks", app_url=str((placed.get("urls") or {}).get(app.id) or "").rstrip("/"),
                          where=placed["node"]["name"])
        if check:
            started = time.monotonic()
            if placed:
                with open(app.manifest_path, encoding="utf-8-sig") as source:
                    _, values = remote_values(app, json.load(source))
                command = shlex.join([values["PYTHON"], "-c", REMOTE_HEALTH,
                                      values["APP_URL"] + app.health_path, values["TOKEN_FILE"]])
                try:
                    code, body = json.loads(self.hub.placement._ssh(command, timeout=12))
                except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
                    code, body = None, None
            else:
                code, body = contract._get(app.health_url(), contract.read_token(app.token_file), 2)
            result["health"] = {"reachable": code is not None, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "latency_ms": int((time.monotonic() - started) * 1000), "status_code": code,
                                "body": body if isinstance(body, dict) else None,
                                "detail": "" if code == 200 else "app health answered " + str(code)}
        return result

    def list_apps(self):
        apps = []
        for app in [*self.hub.apps, self._own_app]:
            try:
                apps.append(self.describe(app.id))
            except (OSError, ValueError):
                pass
        return {"apps": apps}

    def get(self, req):
        if req.path != PREFIX.rstrip("/") and not req.path.startswith(PREFIX):
            return None
        if not req.agent():
            return Reply({"ok": False, "error": "Hub bearer token required"}, 401)
        if req.path == PREFIX.rstrip("/"):
            return self.list_apps()
        try:
            return self.describe(unquote(req.path[len(PREFIX):]), check=req.q_bool("check"))
        except KeyError:
            return Reply({"ok": False, "error": "unknown family app"}, 404)
        except (OSError, ValueError) as exc:
            return Reply({"ok": False, "error": str(exc)}, 422)

    def post(self, req):
        if req.path != "/api/mcp/resolve":
            return None
        if not req.agent():
            return Reply({"ok": False, "error": "Hub bearer token required"}, 401)
        # Old MCP rows may predate the connector sidecar. Match their actual
        # declared program on the PC, without accepting executable overrides.
        argv = [req.body.get("command") or "", *(req.body.get("args") or [])]
        wanted = [os.path.normcase(os.path.normpath(str(arg))) for arg in argv]
        for app in [*self.hub.apps, self._own_app]:
            try:
                declared, _ = launch_spec(self.hub, app, local=True)
            except (OSError, ValueError):
                continue
            if wanted == [os.path.normcase(os.path.normpath(arg)) for arg in declared]:
                try:
                    return {"app": self.describe(app.id)}
                except (OSError, ValueError) as exc:
                    return Reply({"ok": False, "error": str(exc)}, 422)
        return {"app": None}

    @classmethod
    def tools(cls):
        return [{"name": "hub_mcp_apps", "description": "Family apps reachable through the Hub's MCP relay, with optional app health. Does not start apps or bridges.",
                 "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object", "properties": {
                     "app": {"type": "string"}, "check": {"type": "boolean", "default": False}}}}]

    def handlers(self):
        def describe(arguments):
            if arguments.get("app"):
                return self.describe(str(arguments["app"]), check=bool(arguments.get("check")))
            return self.list_apps()
        return {"hub_mcp_apps": describe}

    def handle(self, handler):
        """Streamable HTTP, handled before the ordinary JSON API body reader."""
        message = {}
        if handler.command == "POST":
            # Drain each message before returning an HTTP error. Closing a
            # socket with unread POST data can reset even the error response.
            message = handler._read_body(limit=int(handler.headers.get("Content-Length") or 0))
        if not handler._agent_ok():
            return
        if handler.command == "POST" and message.get("jsonrpc") != "2.0":
            return handler._json({"error": "expected a JSON-RPC message"}, 400)
        app_id = unquote(urlsplit(handler.path).path[len(PREFIX):-len("/mcp")])
        session_id = handler.headers.get("Mcp-Session-Id")
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None and session.app_id != app_id:
            return handler._json({"error": "session belongs to another app"}, 404)
        if handler.command == "DELETE":
            if session is None:
                return handler._json({"error": "unknown MCP session"}, 404)
            with self._lock:
                self._sessions.pop(session_id, None)
            session.close()
            return handler._json({}, 200)
        if handler.command == "GET":
            if session is None or session._closed:
                return handler._json({"error": "unknown MCP session"}, 404)
            return self._stream(handler, session)
        if handler.command != "POST":
            return handler._json({"error": "method not allowed"}, 405)
        created = False
        try:
            if session is None:
                if session_id or message.get("method") != "initialize" or "id" not in message:
                    return handler._json({"error": "unknown MCP session"}, 404)
                app = self._app(app_id)
                if app is None:
                    return handler._json({"error": "unknown family app"}, 404)
                argv, env = launch_spec(self.hub, app)
                session = Session(app_id, argv, env, app.folder)
                with self._lock:
                    self._sessions[session.id] = session
                created = True
            if "method" in message and "id" in message:
                result = session.request(message)
                if created and "error" in result:
                    session.close()
                    with self._lock:
                        self._sessions.pop(session.id, None)
                return self._reply(handler, result, 200, session.id)
            session.send(message)
            return self._reply(handler, None, 202, session.id)
        except (OSError, ValueError) as exc:
            if session is not None:
                session.close()
                with self._lock:
                    self._sessions.pop(session.id, None)
            return handler._json({"jsonrpc": "2.0", "id": message.get("id"),
                                  "error": {"code": -32000, "message": str(exc)}}, 200 if session else 422)

    @staticmethod
    def _reply(handler, payload, status, session_id):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Mcp-Session-Id", session_id)
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _stream(handler, session):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        try:
            while not session._closed:
                try:
                    message = session.events.get(timeout=10)
                except queue.Empty:
                    handler.wfile.write(b": keepalive\n\n")
                else:
                    if message is None:
                        return
                    handler.wfile.write(("event: message\ndata: " + json.dumps(message, ensure_ascii=False) + "\n\n").encode("utf-8"))
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
