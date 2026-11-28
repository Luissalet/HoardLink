"""The hub's HTTP server: a JSON API, the static UI, and the agent contract.

Standard library only (``http.server``), bound to loopback. Two guards:

* a browser page from another origin cannot call the API — a request whose
  ``Sec-Fetch-Site`` says ``cross-site`` is refused, and every mutating
  route is POST, so a plain link cannot stop an app;
* the agent routes (``/api/agent/*``) need the bearer token from
  ``<data>/mcp-token``, which is what the MCP bridge reads. The Hub page
  uses a separate browser session to write.

Routes
------
GET  /                         the UI
GET  /api/health               {service, version, ...}
GET  /api/apps                 full snapshot (apps + states + faustus)
GET  /api/apps/<id>            one app
GET  /api/apps/<id>/icon       its icon
GET  /api/apps/<id>/log        last lines of its log
POST /api/apps/<id>/start|stop|restart|open|close-windows|folder
POST /api/apps/start-all | stop-all | rescan
GET  /api/placement            where Faustus, Hermes and Prometheus run (pc | sparks), the switch in progress and its log
POST /api/placement/switch     {"to": "pc" | "sparks", "node": "Spark2" (optional)} -> moves the three
GET  /api/backends             what Hoard Link resolves right now
GET  /api/memory               what each GPU holds and which server holds it
GET  /api/services             local backend servers (ComfyUI, Ollama, configured) and who started them
POST /api/services/start|stop  {"id": "comfyui@8188", "gpu": "auto", "wait_s": 0}
GET  /api/lease                GPUs (used/free/reserved), granted leases, queue
GET  /api/lease/<id>           one lease (also keeps a queued one in the queue)
POST /api/lease/request        {owner, purpose, vram_mb, gpu (index | [indices] | "2,3" | "any"), priority, ttl_s, wait, pid[, lease_id]}
POST /api/lease/renew          {lease_id, ttl_s}
POST /api/lease/release        {lease_id}
GET  /api/lane                 active browser lanes (per site / profile), who waits
POST /api/lane/acquire         {domain|url, holder, purpose, ttl_s, wait_s, profile, pid[, queue_seq]} -> granted | busy (held_by, since)
POST /api/lane/renew           {lease_id, holder, ttl_s}
POST /api/lane/release         {lease_id, holder[, force]}
GET  /api/profiles             every profile with the state of its apps and commands
GET  /api/profiles/<name>      one profile
POST /api/profiles/<name>/start|stop
POST /api/profiles/export | /api/profiles/import/preview | /api/profiles/import
GET  /api/config               the effective configuration
GET  /api/agent/tools          (bearer) tool catalogue
POST /api/agent/call           (bearer) {"tool": name, "arguments": {...}}

The family layer (0.4): events, calls between apps, rules, jobs, backups
GET  /api/events               ?since_id&type&source&since&until&text&limit
GET  /api/events/stream        Server-Sent Events, ?since_id (long-lived)
GET  /api/events/stats         counts by type/source
POST /api/events               (family token or the UI) {type, data, source?}
GET  /api/apps/<id>/tools      the app's own tool catalogue
POST /api/apps/<id>/call       (family token or the UI) {tool, arguments} → run it with that app's token
POST /api/apps/<id>/stream/<route> (family bearer) relay an allowlisted app HTTP stream
GET  /api/rules | POST /api/rules (add) | POST /api/rules/<id>/update|remove|run|test
GET  /api/jobs  | POST /api/jobs  (add) | POST /api/jobs/<id>/update|remove|run
GET  /api/backups | /api/backups/<id> | POST /api/backups/run|prune|verify|restore
GET  /api/audit                the family audit (?probe=0 for disk-only)

Models for every app (0.6): the hub serves its local models to any family app
POST /api/link/chat            (family token or the UI) {messages, capability, images, json, effort, ...} -> {ok, text, json, model, ...}
GET  /api/link/status          which model serves llm / vision / embed / tts right now (?force=1 re-probes)

The Repos facet (0.5): the state of every git repository of the family (read-only; the hub never pushes)
GET  /api/repos                the cached snapshot (starts a refresh when it is older than 5 min)
GET  /api/repos/<name>         one repository in full: issues, dirty paths, branches, commits, remotes
GET  /api/repos/<name>/push-command   the command to run to push it (text only, never executed)
POST /api/repos/refresh        rescan everything {"wait": true}
POST /api/repos/<name>/fetch   git fetch --prune in that repository, then rescan it (the only network call)
POST /api/repos/<name>/folder  open its folder in the file manager
"""

from __future__ import annotations

import email.utils
import json
import logging
import mimetypes
import errno
import os
import select
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .. import _chat_timing
from ..agent_journal import clean_identity
from ..service_contracts import VOICE_ALIASES
from . import HUB_VERSION, SERVICE
from .core import Hub
from .agora_http import AgoraBusy, AgoraHTTP
from .directory import AppDirectory
from .hub_app_calls import HUB_IDS
from . import desktop, tools, autostart, profile_editor, portable_profiles
from .isolation import passive_error
from . import agent_routes, agora_orders, app_catalogue, stream_gateway, ui_session, voice_routing
from .browser_lanes import LaneError
from .lease import LeaseError
from .rules import example_rules
from .jobs import example_jobs
from .events import event_types_help
from .agora_social import BIG_BODIES as AGORA_BIG_BODIES
from .linkchat import LINK_MAX_BODY, BadRequest, parse_request

logger = logging.getLogger("hoard_hub")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
#: The family's shared theme (hoard_link/ui/hoard-theme.css), served as /ui/hoard-theme.css.
THEME_CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "hoard-theme.css")
MAX_BODY = 256 * 1024
HUB_APP_CALLS = frozenset(f"/api/apps/{app}/call" for app in HUB_IDS)
PROXY_MAX_BODY = 4 * 1024 * 1024  # authenticated chapter/document handoffs


class _BodyTooLarge(ValueError):
    pass


def _client_disconnected(exc: BaseException) -> bool:
    return (isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))
            or getattr(exc, "winerror", None) in (10053, 10054)
            or getattr(exc, "errno", None) in (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED))


class _HubServer(ThreadingHTTPServer):
    # A short burst of simultaneous worker heartbeats must not fill the TCP listen queue.
    request_queue_size = 128
    daemon_threads = True

    def __init__(self, address, handler, hub):
        super().__init__(address, handler)
        self.agora_work = AgoraHTTP()
        self.directory = AppDirectory(hub)
        self.directory.start()

    def shutdown(self):
        super().shutdown()
        self._close_workers()

    def server_close(self):
        self._close_workers()
        super().server_close()

    def _close_workers(self):
        if hasattr(self, "directory"):
            self.directory.close()
            self.agora_work.close()


def make_server(hub: Hub, host: str = "127.0.0.1", port: Optional[int] = None) -> ThreadingHTTPServer:
    port = hub.config.port if port is None else port

    class Handler(_HubHandler):
        pass

    Handler.hub = hub
    Handler.ui_sessions = ui_session.BrowserSessions(hub.config.data_dir, hub.config.url)
    return _HubServer((host, port), Handler, hub)


class _HubHandler(BaseHTTPRequestHandler):
    hub: Hub
    ui_sessions: ui_session.BrowserSessions
    server_version = f"hoard-hub/{HUB_VERSION}"
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        logger.debug("%s " + fmt, self.address_string(), *args)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        path = getattr(self, "path", "").partition("?")[0]
        if "/api/apps/" in path and "/stream/" in path:
            self.log_message("app stream %s %s", code, size)
            return
        super().log_request(code, size)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except OSError as exc:
            if not _client_disconnected(exc):
                raise
            self.close_connection = True

    def _json(self, payload: Any, status: int = 200, *, headers: Optional[dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            if not _client_disconnected(exc):
                raise
            self.close_connection = True

    def _file(self, path: str, cache: bool = False) -> None:
        if not os.path.isfile(path):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        # Page files are revalidated on every load (an edit shows at once) but
        # an unchanged file costs a 304 instead of its bytes.
        mtime = int(os.stat(path).st_mtime)
        since = self.headers.get("If-Modified-Since")
        if since:
            try:
                if int(email.utils.parsedate_to_datetime(since).timestamp()) >= mtime:
                    self.send_response(304)
                    self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
                    self.end_headers()
                    return
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", email.utils.formatdate(mtime, usegmt=True))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self, limit: int = MAX_BODY) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > limit:
            self.close_connection = True
            raise _BodyTooLarge("body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _guard(self) -> bool:
        """Refuse cross-site calls from a browser; everything else is local."""
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        mode = (self.headers.get("Sec-Fetch-Mode") or "").lower()
        if site == "cross-site" and mode != "navigate":
            self._json({"ok": False, "error": "cross-site requests are refused"}, 403)
            return False
        if self.command == "POST" and site == "cross-site":
            self._json({"ok": False, "error": "cross-site requests are refused"}, 403)
            return False
        return True

    def _bearer(self) -> str:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    def _family_caller(self) -> Optional[str]:
        """Who is calling a family route: an app id (its own token), "hub"
        (the hub's token), "ui" (the hub's own page, no token), else None."""
        token = self._bearer()
        if token:
            return self.hub.token_owner(token)
        if self.command == "POST":
            return "ui" if self._ui_proof() else None
        if self._browser_metadata():
            return "ui"
        return None

    def _mail_apps(self) -> list[dict[str, str]]:
        return [{"id": app.id, "name": app.name} for app in self.hub.apps if app.id != "hub"]

    def _mail_helper_caller(self) -> None:
        token = self._bearer()
        who = self.hub.token_owner(token) if token else None
        if not who or (who == "hub" and token != self.hub.token):
            return self._json({"ok": False, "error": "family bearer token required"}, 401)
        gate = self.hub.facet("mailgate")
        raw_account = self.headers.get("X-Hoard-Mail-Account")
        if raw_account is None:
            if gate is not None:
                gate.last_caller_at = gate.clock()
            return self._json({"ok": True, "app": who, "apps": self._mail_apps()})
        try:
            account = json.loads(raw_account)
            if (not isinstance(account, list) or len(account) != 3
                    or any(not isinstance(value, str) for value in account) or not account[1]):
                raise ValueError("invalid account descriptor")
        except ValueError:
            return self._json({"ok": False, "error": "invalid account descriptor"}, 400)
        if gate is not None:
            gate.last_caller_at = gate.clock()
        allowed = who == "hub" or (gate is not None and gate.helper_account_allowed(who, *account))
        return self._json({"ok": True, "app": who, "mail_allowed": allowed})

    def _browser_origin(self) -> bool:
        return (self.headers.get("Origin") or "").rstrip("/") in ("", self.hub.config.url)

    def _browser_metadata(self) -> bool:
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        return site in ("same-origin", "none") and self._browser_origin()

    def _ui_proof(self) -> bool:
        return self._browser_origin() and self.ui_sessions.accepts(self.headers.get(ui_session.SESSION_HEADER, ""))

    def _ui_denied(self, path: str = "") -> None:
        legacy_console = (path.startswith("/api/agora/")
                          and self.headers.get("Origin") == self.hub.config.url
                          and self.headers.get("Sec-Fetch-Site") == "same-origin"
                          and self.headers.get("Accept") == "*/*"
                          and all(header not in self.headers for header in (
                              "Sec-Fetch-Mode", "User-Agent", "Authorization", ui_session.SESSION_HEADER,
                              "X-Hoard-Person")))
        spanish = self.headers.get("Accept-Language", "es" if legacy_console else "").lower().startswith("es")
        if legacy_console:
            error = ("Esta ventana es de una versión anterior. Ciérrala y vuelve a abrir la consola del Ágora."
                     if spanish else "This window is from an earlier version. Close it and reopen the Agora console.")
        else:
            error = ("Esta ventana es de una versión anterior. Ciérrala y vuelve a abrir el Hub."
                     if spanish else "This window is from an earlier version. Close it and reopen the Hub.")
        # The old native bridge rejected with a bare string on HTTP errors. Its page
        # only displayed the message when the JSON reply reached its own error handler.
        return self._json({"ok": False, "code": "hub_ui_session_required", "error": error}, 200 if legacy_console else 403)

    def _ui_session_post(self, path: str) -> None:
        body = self._read_body()
        if path == ui_session.LAUNCH_PATH:
            ticket = self.ui_sessions.issue(self.headers.get("Authorization", ""))
            return self._json({"ok": True, "ticket": ticket}) if ticket else self._ui_denied()
        proof = self.ui_sessions.exchange(body.get("ticket")) if self._browser_origin() else ""
        return self._json({"ok": True, "session": proof}) if proof else self._ui_denied()

    def _family_ok(self) -> Optional[str]:
        who = self._family_caller()
        if who is None:
            self._json({"ok": False, "error": "a family bearer token is required: the hub's data/mcp-token or any app's own"}, 401)
        return who

    def _sse(self, since_id: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = since_id if since_id else self.hub.events.last_id
        try:
            self.wfile.write(f": hoard-hub events from id {last}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                batch = self.hub.events.follow(last, timeout=20.0)
                if not batch:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for ev in batch:
                    payload = json.dumps(ev, ensure_ascii=False, default=str)
                    self.wfile.write(f"id: {ev['id']}\nevent: {ev['type']}\ndata: {payload}\n\n".encode("utf-8"))
                    last = max(last, int(ev["id"]))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _agent_ok(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or token != self.hub.token:
            self._json({"ok": False, "error": "bearer token required (data/mcp-token)"}, 401)
            return False
        return True

    def _agent_identity(self, envelope: Optional[dict[str, Any]] = None) -> dict[str, str]:
        body = envelope or {}

        def field(key: str, limit: int) -> str:
            value = body.get(key)
            return clean_identity(value, limit) if isinstance(value, str) else ""

        agent = (clean_identity(self.headers.get("X-Agent-Id")) or field("agent", 80)
                 or field("caller", 80))
        session = (clean_identity(self.headers.get("X-Agent-Session"), 120) or field("_session", 120)
                   or field("session", 120))
        return {key: value for key, value in (("agent", agent), ("session", session)) if value}

    # -- models for every app -----------------------------------------------------
    def _link_chat(self) -> None:
        with _chat_timing.request():
            self._serve_link_chat()

    def _serve_link_chat(self) -> None:
        """POST /api/link/chat. Authenticated before the body is read (it may hold 12 MB of images), so a
        refusal closes the connection instead of leaving unread bytes on a keep-alive socket."""
        with _chat_timing.stage("identity"):
            who = self._family_caller()
        if who is None:
            self.close_connection = True
            return self._json({"ok": False, "error": "a family bearer token is required: the hub's data/mcp-token or any app's own"}, 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > LINK_MAX_BODY:
            self.close_connection = True
            return self._json({"ok": False, "error": "too_large", "detail": f"the request is over {LINK_MAX_BODY // (1024 * 1024)} MB"}, 413)
        with _chat_timing.stage("request_body"):
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = None
        if not isinstance(body, dict):
            return self._json({"ok": False, "error": "bad_request", "detail": "the body must be a JSON object"}, 400)
        if body.get("stream"):
            try:
                parse_request(body)
            except BadRequest as exc:
                return self._json({"ok": False, "error": "bad_request", "detail": str(exc)}, 400)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True

            def disconnected():
                try:
                    ready, _, _ = select.select([self.connection], [], [], 0)
                    return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)
                except OSError:
                    return True

            stream = self.hub.link.stream_chat(who, body, disconnected=disconnected)
            try:
                for item in stream:
                    if item["type"] in ("finish", "error"):
                        item = _chat_timing.receipt(item)
                    with _chat_timing.stage("reply_write"):
                        payload = json.dumps(item, ensure_ascii=False)
                        self.wfile.write(f"id: {item['request_id']}:{item['seq']}\nevent: {item['type']}\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                stream.close()
            return
        status, payload = self.hub.link.chat(who, body)
        with _chat_timing.stage("reply_write"):
            return self._json(_chat_timing.receipt(payload), status)

    def _copilot(self) -> None:
        """Relay the shared agent response, including SSE, without buffering it."""
        who = self._family_caller()
        if who is None:
            return self._json({"ok": False, "error": "family bearer token required"}, 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            from .copilot import proxy_for
            response = proxy_for(self.hub).open(who, body)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except OSError as exc:
            return self._json({"ok": False, "code": "faustus_unavailable", "error": str(exc)}, 503)
        with response:
            self.send_response(response.status)
            for name in ("Content-Type", "X-Odysseus-Run-Id", "X-Faustus-Idempotent-Replay", "X-Faustus-API-Version"):
                value = response.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Only the observer disconnected; Faustus owns the detached run.
                pass

    # -- leases ---------------------------------------------------------------
    def _lease_reply(self, res: dict[str, Any]) -> None:
        status = int(res.pop("status", 0) or 0) if isinstance(res, dict) else 0
        if not status:
            status = 200 if res.get("ok", True) else 400
        return self._json(res, status)

    def _lease_post(self, action: str, body: dict[str, Any]) -> None:
        arb = self.hub.leases
        try:
            if action == "request":
                res = arb.request(owner=str(body.get("owner") or ""), purpose=str(body.get("purpose") or ""),
                                  vram_mb=body.get("vram_mb", 0), gpu=body.get("gpu"), priority=body.get("priority", 0),
                                  ttl_s=body.get("ttl_s"), wait=bool(body.get("wait", False)), pid=body.get("pid"),
                                  lease_id=body.get("lease_id"), wait_s=body.get("wait_s"), service=body.get("service"),
                                  avoid_gpus=tuple(int(gpu) for gpu in body.get("avoid_gpus", ())))
            elif action == "renew":
                res = arb.renew(str(body.get("lease_id") or ""), body.get("ttl_s"))
            elif action == "bind-process":
                res = arb.bind_process(str(body.get("lease_id") or ""), int(body["pid"]), service=body.get("service"))
            elif action == "reserve-service":
                res = arb.reserve_service_for_work(str(body.get("lease_id") or ""), str(body.get("service") or ""),
                                                   int(body["vram_mb"]), body.get("gpu", "any"))
            else:
                res = arb.release(str(body.get("lease_id") or ""))
        except (LeaseError, ValueError, KeyError, TypeError) as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        return self._lease_reply(res)

    # -- browser lanes ---------------------------------------------------------
    def _lane_post(self, action: str, body: dict[str, Any], who: str) -> None:
        """``who`` is the authenticated caller: an app id (its own token), ``hub`` (the hub's token: Faustus, agents) or
        ``ui`` (the hub page). A lane belongs to the token owner that took it, an app speaks only for itself (its holder name
        is its id, whatever it sends), and ``force`` exists only on the hub page."""
        arb = self.hub.lanes
        granted: Optional[str] = None
        if who == "ui":
            if action != "release":
                return self._json({"ok": False, "error": "the hub page can only release a lane"}, 403)
            holder, owner, force = None, None, True
        else:
            if body.get("force"):
                return self._json({"ok": False, "error": "force is only available from the hub page"}, 403)
            holder = who if who != "hub" else str(body.get("holder") or "")
            owner, force = who, False
        try:
            if action == "acquire":
                res = arb.acquire(domain=str(body.get("domain") or body.get("url") or ""), holder=holder or "",
                                  purpose=str(body.get("purpose") or ""), ttl_s=body.get("ttl_s"), wait_s=body.get("wait_s", 0),
                                  profile=str(body.get("profile") or ""), pid=body.get("pid"), owner=owner or "",
                                  ticket=body.get("ticket"), final=bool(body.get("final", True)))
                if res.get("granted"):
                    granted = res.get("lease_id")
            elif action == "renew":
                res = arb.renew(str(body.get("lease_id") or ""), holder, body.get("ttl_s"), owner=owner)
            else:
                res = arb.release(str(body.get("lease_id") or ""), holder, force=force, owner=owner)
        except LaneError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        try:
            return self._lease_reply(res)
        except Exception:  # noqa: BLE001  (the caller went away after the lane was granted: do not keep it for its ttl)
            if granted:
                arb.release(granted, force=True)
            raise

    # -- facets (0.7) ----------------------------------------------------------
    def _agora_busy(self):
        self._json({"ok": False, "code": "agora_busy", "error": "The Agora is busy. Try again shortly."}, 503)

    def _invoke_agent(self, name, args, identity):
        if name == app_catalogue.NAME:
            args = app_catalogue.request_arguments(args, self.headers.get(app_catalogue.SCOPE_HEADER))
        if name == agora_orders.ORDER_TOOL:
            args = agora_orders.order_read_arguments(args, self.headers.get(agora_orders.WORKER_ORDER_TASK_HEADER))
        invoke = lambda: agent_routes.invoke(self.hub, name, args, **identity)
        if not name.startswith("hub_agora_"):
            return invoke()
        operation = name
        if name == "hub_agora_console_overview" and (args.get("view") or args.get("since")):
            operation = "overview_delta"
        return self.server.agora_work.call(operation, invoke, wait_s=args.get("wait_s"))

    def _facet(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> bool:
        """Let the facets answer; True when one did."""
        from . import facets as _facets

        def agent() -> bool:
            return bool(self._bearer()) and self._bearer() == self.hub.token

        req = _facets.Request(method=method, path=path, query=query, body=body, caller=self._family_caller,
                              agent=agent, headers=self.headers)
        dispatch = lambda: _facets.dispatch(getattr(self.hub, "facets", []), req)
        operation = ""
        wait_s = req.q("wait_s")
        if path == "/api/agora" or path.startswith("/api/agora/"):
            operation = path.rsplit("/", 1)[-1]
            if operation == "overview" and (req.q("view") or req.q("since")):
                operation = "overview_delta"
        elif method == "POST" and path in HUB_APP_CALLS:
            name = str(body.get("tool") or body.get("name") or "")
            if name.startswith("hub_agora_"):
                args = body.get("arguments") or body.get("args") or {}
                args = args if isinstance(args, dict) else {}
                operation = "overview_delta" if name == "hub_agora_console_overview" and (
                    args.get("view") or args.get("since")) else name
                wait_s = args.get("wait_s")
        res = self.server.agora_work.call(operation, dispatch, wait_s=wait_s) if operation else dispatch()
        if res is None:
            return False
        if isinstance(res, _facets.Reply):
            if res.body is not None:
                self.send_response(res.status)
                self.send_header("Content-Type", res.content_type)
                self.send_header("Content-Length", str(len(res.body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in res.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(res.body)
            else:
                self._json(res.payload, res.status)
            return True
        status = 200
        if isinstance(res, dict) and res.get("ok") is False:
            status = int(res.pop("status", 0) or 400) if isinstance(res.get("status"), int) else 400
        self._json(res, status)
        return True

    # -- routing --------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        hub = self.hub
        try:
            if path == "/api/health":
                return self._json({"ok": True, "service": SERVICE, "version": HUB_VERSION, "apps": hub.app_count,
                                   "url": hub.config.url})
            if path == "/api/apps":
                listing = self.server.directory.list()
                by_id = {app["id"]: app for app in listing["apps"]}
                aliases = [{**by_id[target], "id": alias, "alias_of": target}
                           for alias, target in VOICE_ALIASES.items() if target in by_id and alias not in by_id]
                return self._json({**listing, "apps": [*listing["apps"], *aliases]} if aliases else listing)
            parts = path.split("/")
            if parts[1:3] == ["api", "apps"] and (len(parts) == 4 or (len(parts) == 5 and parts[4] == "status")):
                app = hub.get(parts[3])
                if app is None:
                    return self._json({"ok": False, "error": "unknown app"}, 404)
                return self._json(self.server.directory.status(app))
            if self._mcp_relay():
                return None
            if path == "/":
                return self._file(os.path.join(UI_DIR, "index.html"))
            if path == "/programs":
                return self._file(os.path.join(UI_DIR, "programs.html"))
            if path == "/api/copilot/info":
                if hub.config.isolated or hub.config.isolated_catalog:
                    return self._json({"ok": False, "code": "isolated_catalog"}, 503)
                from .copilot import host_info
                try:
                    return self._json(host_info(hub))
                except OSError as exc:
                    return self._json({"ok": False, "code": "faustus_unavailable", "error": str(exc)}, 503)
            if path in ("/api/copilot/panel.js", "/api/copilot/launcher.js"):
                if hub.config.isolated or hub.config.isolated_catalog:
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                from .copilot import host_relay
                host_relay(hub).handle(self)
                return
            if path == "/ui/hoard-theme.css":
                return self._file(THEME_CSS, cache=False)
            if path.startswith("/ui/"):
                rel = os.path.normpath(path[4:]).replace("\\", "/")
                if rel.startswith("..") or rel.startswith("/"):
                    return self._json({"ok": False, "error": "not found"}, 404)
                return self._file(os.path.join(UI_DIR, rel), cache=False)
            if self._facet("GET", path, query, {}):
                return None
            if path == "/api/family/caller":
                return self._mail_helper_caller()
            if path == "/api/family/mail-apps":
                return self._json({"ok": True, "apps": self._mail_apps()})
            if path == "/api/faustus":
                return self._json(hub.faustus_status())
            if path == "/api/placement":
                if hub.config.isolated:
                    return self._json({"ok": True, "location": "pc", "isolated": True})
                return self._json(hub.placement.status())
            if path == "/api/backends":
                if hub.config.isolated_catalog:
                    return self._json({"ok": True, "isolated": True, "capabilities": {}, "gpus": []})
                return self._json(hub.backends(force=query.get("force", ["0"])[0] in ("1", "true")))
            if path == "/api/services/comfy-profiles":
                items = [] if hub.config.isolated or hub.config.isolated_catalog else hub.launcher.statuses(named_only=True)
                return self._json({"ok": True, "items": items})
            if path == "/api/services":
                if hub.config.isolated_catalog:
                    return self._json({"ok": True, "isolated": True, "items": [], "gpus": [], "config_path": ""})
                return self._json(hub.services())
            if path == "/api/memory":
                return self._json(hub.gpu_memory())
            if path == "/api/lane":
                # lease ids are capabilities for the hub page and for authenticated callers, not for an anonymous reader
                return self._json(hub.lanes.status(ids=self._family_caller() is not None))
            if path == "/api/lease":
                return self._json(hub.leases.status(force=query.get("force", ["0"])[0] in ("1", "true")))
            if path.startswith("/api/lease/"):
                return self._lease_reply(hub.leases.get(path[len("/api/lease/"):]))
            if path == "/api/profiles":
                return self._json(hub.profiles_status())
            if path == "/api/autostart":
                return self._json(autostart.status())
            if path.startswith("/api/profiles/"):
                res = hub.profile_status(unquote(path[len("/api/profiles/"):]))
                return self._json(res, 200 if res.get("ok") else 404)
            if path == "/api/events":
                q = lambda k, d=None: query.get(k, [d])[0]  # noqa: E731
                return self._json({"ok": True, "last_id": hub.events.last_id, "events": hub.events.query(
                    since_id=int(q("since_id", 0) or 0), type=q("type"), source=q("source"),
                    since_ts=float(q("since")) if q("since") else None, until_ts=float(q("until")) if q("until") else None,
                    text=q("text"), limit=int(q("limit", 100) or 100),
                    newest_first=q("order", "desc") != "asc")})
            if path == "/api/events/stream":
                return self._sse(int(query.get("since_id", ["0"])[0] or 0))
            if path == "/api/events/stats":
                since = query.get("since", [None])[0]
                st = hub.events.stats(float(since) if since else None)
                st["conventions"] = event_types_help()
                return self._json({"ok": True, **st})
            if path == "/api/rules":
                return self._json({"ok": True, "rules": hub.rules.list(), "history": hub.rules.history[-30:],
                                   "examples": example_rules()})
            if path == "/api/jobs":
                return self._json({"ok": True, "jobs": hub.jobs.list(), "history": hub.jobs.history[-30:],
                                   "examples": example_jobs(), "enabled": hub.config.jobs_enabled})
            if path == "/api/backups":
                st = hub.backups.status()
                st["snapshots"] = hub.backups.list_snapshots()[-50:]
                st.update(hub.backup_source_inventory())
                return self._json({"ok": True, **st})
            if path.startswith("/api/backups/"):
                sid = unquote(path[len("/api/backups/"):])
                m = hub.backups.load_snapshot(sid)
                if m is None:
                    return self._json({"ok": False, "error": "unknown snapshot"}, 404)
                full = query.get("full", ["0"])[0] in ("1", "true")
                if not full:
                    m = {**m, "apps": {k: {"folder": v.get("folder"), "totals": v.get("totals"), "missing": v.get("missing"),
                                           "skipped": (v.get("skipped") or [])[:40], "errors": v.get("errors")}
                                       for k, v in (m.get("apps") or {}).items()}}
                return self._json({"ok": True, "snapshot": m})
            if path == "/api/audit":
                return self._json(hub.family_audit(probe=query.get("probe", ["1"])[0] not in ("0", "false")))
            if path == "/api/link/status":
                if hub.config.isolated:
                    return self._json(passive_error(), 409)
                return self._json(hub.link.status(force=query.get("force", ["0"])[0] in ("1", "true")))
            if path == "/api/repos":
                if hub.config.isolated:
                    return self._json(passive_error(), 409)
                return self._json(hub.repos.snapshot())
            if path.startswith("/api/repos/"):
                if hub.config.isolated:
                    return self._json(passive_error(), 409)
                rest = path[len("/api/repos/"):].split("/")
                name = unquote(rest[0])
                if len(rest) == 1:
                    res = hub.repos.detail(name)
                    return self._json(res, 200 if res.get("ok") else 404)
                if len(rest) == 2 and rest[1] == "push-command":
                    res = hub.repos.push_command(name)
                    return self._json(res, 200 if res.get("ok") else (404 if "unknown repo" in str(res.get("error")) else 409))
                return self._json({"ok": False, "error": "not found"}, 404)
            if path == "/api/config":
                cfg = hub.config.to_dict()
                cfg["browser_found"] = None if hub.config.isolated else desktop.find_browser(hub.config.browser)
                cfg["native_window"] = not hub.config.isolated and desktop.hub_window_native_available()
                cfg["window_engine_used"] = "headless" if hub.config.isolated else desktop.window_engine(hub.config.window_engine)
                return self._json(cfg)
            if path == "/api/agent/tools":
                if not self._agent_ok():
                    return None
                return self._json({"tools": tools.all_tools()})
            if path.startswith(agent_routes.PREFIX):
                if not self._agent_ok():
                    return None
                tool = agent_routes.describe(path)
                return self._json(tool if tool else {"ok": False, "error": "unknown tool"}, 200 if tool else 404)
            if len(parts) >= 4 and parts[1] == "api" and parts[2] == "apps":
                app = hub.get(parts[3])
                if app is None:
                    return self._json({"ok": False, "error": "unknown app"}, 404)
                sub = parts[4] if len(parts) > 4 else ""
                if sub == "icon":
                    icon_path = app.icon_path      # read once: a snapshot on another thread may reset it meanwhile
                    if icon_path and os.path.isfile(icon_path):
                        return self._file(icon_path, cache=True)
                    return self._file(os.path.join(UI_DIR, "fallback-icon.svg"), cache=True)
                if sub == "log":
                    n = int(query.get("lines", ["80"])[0])
                    return self._json(hub.log_tail(app.id, max(1, min(n, 2000))))
                if sub == "tools":
                    res = hub.app_tools(app.id)
                    return self._json(res, 200 if res.get("ok") else 502)
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            if _client_disconnected(exc):
                self.close_connection = True
                return None
            if isinstance(exc, AgoraBusy):
                return self._agora_busy()
            logger.exception("GET %s failed", path)
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        url = urlsplit(self.path)
        if (route := stream_gateway.match(url.path)) is not None:
            return stream_gateway.handle(self, route, url)
        if self._mcp_relay():
            return
        path = url.path.rstrip("/")
        hub = self.hub
        if path in (ui_session.LAUNCH_PATH, ui_session.SESSION_PATH):
            try:
                return self._ui_session_post(path)
            except ValueError:
                return self._json({"ok": False, "error": "Invalid session request"}, 400)
        if not self._bearer() and (
                self._browser_metadata() or ui_session.SESSION_HEADER in self.headers) and not self._ui_proof():
            # Closing a socket with unread request bytes can reset the connection before the client sees the 403.
            try:
                self._read_body(max(MAX_BODY, AGORA_BIG_BODIES.get(path, 0)))
            except ValueError:
                self.close_connection = True
            return self._ui_denied(path)
        if hub.config.isolated and path in ("/api/link/copilot", "/api/copilot/proxy", "/api/link/chat"):
            self.close_connection = True
            return self._json(passive_error(), 409)
        if path == "/api/link/copilot":
            return self._copilot()
        if path == "/api/copilot/proxy":
            if self._family_caller() is None:
                return self._json({"ok": False, "error": "family bearer token required"}, 401)
            from .copilot import host_relay
            host_relay(hub).handle(self)
            return
        if path == "/api/link/chat":
            try:
                return self._link_chat()
            except Exception as exc:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        try:
            limit = MAX_BODY
            if path == "/api/agent/call" or path.startswith(agent_routes.PREFIX):
                if not self._agent_ok():
                    self.close_connection = True
                    return None
                limit = PROXY_MAX_BODY
            elif path.startswith("/api/apps/") and path.endswith("/call"):
                if self._family_ok() is None:
                    self.close_connection = True
                    return None
                limit = PROXY_MAX_BODY
            elif path in ("/api/agora/order_create", "/api/agora/order_update"):
                if self._family_ok() is None:
                    self.close_connection = True
                    return None
                limit = PROXY_MAX_BODY
            elif path in AGORA_BIG_BODIES:      # a file or a picture for the Agora, from the family only
                if self._family_caller() is None:
                    self.close_connection = True
                    return self._json({"ok": False, "error": "send the hub's bearer token or use the hub's page"}, 401)
                limit = AGORA_BIG_BODIES[path]
            body = self._read_body(limit)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 413)
        try:
            if path.startswith(agent_routes.PREFIX):
                tool = agent_routes.describe(path)
                if tool is None:
                    return self._json({"ok": False, "error": "unknown tool"}, 404)
                result, status = self._invoke_agent(tool["name"], body, self._agent_identity())
                return self._json(result, status)
            if self._facet("POST", path, parse_qs(urlsplit(self.path).query), body):
                return None
            if path == "/api/agent/call":
                if not self._agent_ok():
                    return None
                name = str(body.get("tool") or body.get("name") or "")
                args = body.get("arguments") or body.get("args") or {}
                result, status = self._invoke_agent(name, args if isinstance(args, dict) else {},
                                                    self._agent_identity(body))
                return self._json(result, status)
            if path in ("/api/lease/request", "/api/lease/renew", "/api/lease/release",
                        "/api/lease/bind-process", "/api/lease/reserve-service"):
                return self._lease_post(path.rsplit("/", 1)[1], body)
            if path in ("/api/lane/acquire", "/api/lane/renew", "/api/lane/release"):
                who = self._family_ok()
                if who is None:
                    return None
                return self._lane_post(path.rsplit("/", 1)[1], body, who)
            if path == "/api/events":
                who = self._family_ok()
                if who is None:
                    return None
                source = str(body.get("source") or who)
                if who not in ("hub", "ui") and source != who:
                    source = who  # an app may only speak for itself
                try:
                    ev = hub.events.emit(str(body.get("type") or ""), body.get("data") or {}, source=source)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                return self._json({"ok": True, "event": ev})
            if path in ("/api/services/start", "/api/services/stop"):
                sid = str(body.get("id") or "")
                if path.endswith("start"):
                    res = hub.service_start(sid, gpu=body.get("gpu"), wait_s=float(body.get("wait_s") or 0),
                                            lease_id=body.get("lease_id"), work_lease_id=body.get("work_lease_id"))
                else:
                    res = hub.service_stop(sid, expected_pid=body.get("expected_pid"))
                return self._json(res, 200 if res.get("ok") else 409)
            if path == "/api/rules":
                res = hub.rules.add(body)
                return self._json(res, 200 if res.get("ok") else 400)
            if path == "/api/rules/install-defaults":
                return self._json(hub.rules.install_examples(refresh=bool(body.get("refresh"))))
            if path == "/api/jobs":
                res = hub.jobs.add(body)
                return self._json(res, 200 if res.get("ok") else 400)
            if path == "/api/backups/run":
                apps_arg = body.get("apps")
                res = hub.backup_run([str(a) for a in apps_arg] if isinstance(apps_arg, list) and apps_arg else None,
                                     label=str(body.get("label") or ""))
                return self._json(res, 200 if res.get("ok") or res.get("snapshot") else 409)
            if path == "/api/backups/prune":
                res = hub.backups.prune(int(body.get("keep") or (hub.config.backup or {}).get("keep") or 14))
                return self._json(res, 200 if res.get("ok") else 409)
            if path == "/api/backups/verify":
                res = hub.backups.verify(body.get("snapshot"))
                return self._json(res, 200 if res.get("ok") else 409)
            if path == "/api/backups/restore":
                res = hub.backup_restore(str(body.get("snapshot") or ""), str(body.get("app") or ""),
                                         dest=body.get("dest"), in_place=bool(body.get("in_place", False)))
                return self._json(res, 200 if res.get("ok") else 409)
            if path == "/api/repos/refresh":
                if hub.config.isolated:
                    return self._json(passive_error(), 409)
                res = hub.repos.refresh(wait=bool(body.get("wait", False)))
                return self._json({**res, "summary": hub.repos.snapshot()["summary"]})
            if path.startswith("/api/repos/"):
                if hub.config.isolated:
                    return self._json(passive_error(), 409)
                rest = path[len("/api/repos/"):].split("/")
                if len(rest) == 2 and rest[1] in ("fetch", "folder"):
                    name = unquote(rest[0])
                    res = hub.repos.fetch(name) if rest[1] == "fetch" else hub.open_repo_folder(name)
                    if res.get("ok"):
                        return self._json(res)
                    return self._json(res, 404 if "unknown repo" in str(res.get("error")) else 409)
                return self._json({"ok": False, "error": "not found"}, 404)
            if path == "/api/apps/rescan":
                return self._json({"ok": True, "apps": [a.to_dict() for a in hub.rescan()]})
            if path in ("/api/faustus/start", "/api/faustus/stop"):
                res = hub.faustus_start() if path.endswith("/start") else hub.faustus_stop()
                return self._json(res, 200 if res.get("ok") else 409)
            if path == "/api/placement/switch":
                if hub.config.isolated:
                    return self._json(passive_error(), 409)
                res = hub.placement.switch(str(body.get("to") or ""), str(body.get("node") or ""))
                return self._json(res, 200 if res.get("ok") else 409)
            if path == "/api/apps/start-all":
                return self._json(hub.start_all())
            if path == "/api/apps/stop-all":
                return self._json(hub.stop_all())
            parts = path.split("/")
            if path == "/api/autostart":
                res = profile_editor.startup(hub, body)
                return self._json(res, 200 if res.get("ok") else 400)
            if path == "/api/profiles/export":
                try:
                    return self._json(portable_profiles.export_profile(hub, str(body.get("name") or "")))
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 404 if "unknown profile" in str(exc) else 400)
            if path in ("/api/profiles/import/preview", "/api/profiles/import"):
                options = {key: body.get(key) for key in
                           ("name", "app_map", "command_overrides", "replace")
                           if key in body}
                fn = portable_profiles.preview_import if path.endswith("/preview") else portable_profiles.import_profile
                result = fn(hub, body.get("document"), **options)
                return self._json(result, 200 if result.get("ok") or path.endswith("/preview") else 400)
            if len(parts) == 5 and parts[1:3] == ["api", "profiles"] and parts[4] in ("save", "remove", "patch"):
                name = unquote(parts[3])
                if parts[4] == "patch":
                    res = hub.profile_patch(name, body.get('apps', {}), body.get('desktop', {}))
                else:
                    res = profile_editor.save(hub, name, body) if parts[4] == "save" else profile_editor.remove(hub, name)
                return self._json(res, 200 if res.get("ok") else 400)
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "profiles" and parts[4] in ("start", "stop"):
                name = unquote(parts[3])
                if name not in hub.profiles():
                    return self._json({"ok": False, "error": f"unknown profile: {name}"}, 404)
                res = hub.profile_start(name, startup=body.get("startup") is True) if parts[4] == "start" else hub.profile_stop(name)
                return self._json(res, 200 if res.get("ok") else 409)
            if len(parts) == 5 and parts[1] == "api" and parts[2] in ("rules", "jobs"):
                coll = hub.rules if parts[2] == "rules" else hub.jobs
                rid, action = unquote(parts[3]), parts[4]
                if coll.get(rid) is None:
                    return self._json({"ok": False, "error": f"unknown {parts[2][:-1]}: {rid}"}, 404)
                if action == "update":
                    res = coll.update(rid, body)
                elif action == "remove":
                    res = coll.remove(rid)
                elif action == "run":
                    if parts[2] == "jobs":
                        res = hub.jobs.run_now(rid)
                    else:
                        ev = body.get("event") if isinstance(body.get("event"), dict) else None
                        if ev is None and body.get("event_id"):
                            ev = hub.events.get(int(body["event_id"]))
                        if ev is None:
                            ev = {"id": None, "ts": time.time(), "type": str(body.get("type") or "manual.test"),
                                  "source": "ui", "data": body.get("data") or {}}
                        res = hub.rules.run(hub.rules.get(rid), ev, manual=True)
                elif action == "test" and parts[2] == "rules":
                    ev = body.get("event") if isinstance(body.get("event"), dict) else {"type": body.get("type", ""), "source": body.get("source"), "data": body.get("data") or {}}
                    res = {"ok": True, "matches": [m for m in hub.rules.test(ev) if m["id"] == rid]}
                else:
                    return self._json({"ok": False, "error": "unknown action"}, 404)
                return self._json(res, 200 if res.get("ok", True) else 400)
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "apps":
                app_id, action = parts[3], parts[4]
                name = str(body.get("tool") or body.get("name") or "") if action == "call" else ""
                if hub.get(app_id) is None and voice_routing.requested_service(app_id, name) is None:
                    return self._json({"ok": False, "error": "unknown app"}, 404)
                if action == "call":
                    who = self._family_ok()
                    if who is None:
                        return None
                    args = body.get("arguments") or body.get("args") or {}
                    try:
                        timeout = float(body.get("timeout_s") or 120.0)
                    except (TypeError, ValueError):
                        timeout = 120.0
                    reason = body.get("reason")
                    args = args if isinstance(args, dict) else {}
                    res, meta = agent_routes.capture(
                        hub, body.get(agent_routes.CONTEXT), app_id, name, args,
                        lambda: hub.call_app(
                            app_id, name, args, caller=who, timeout=max(1.0, min(timeout, 900.0)),
                            reason=reason if isinstance(reason, str) else None,
                            agent=self.headers.get("X-Agent-Id") or (body.get("agent") if isinstance(body.get("agent"), str) else None),
                            session=self.headers.get("X-Agent-Session") or (body.get("session") if isinstance(body.get("session"), str) else None),
                        ))
                    res = {**res, **meta}
                    return self._json(res, 200 if res.get("ok") else (int(res.get("status") or 502) if res.get("status") else 502))
                if action == "start":
                    res = hub.start(app_id, wait=bool(body.get("wait", True)))
                elif action == "stop":
                    res = hub.stop(app_id)
                elif action == "restart":
                    res = hub.restart(app_id)
                elif action == "open":
                    res = hub.open(app_id, mode=str(body.get("mode") or "window"), autostart=bool(body.get("autostart", True)))
                elif action == "close-windows":
                    res = hub.close_windows(app_id)
                elif action == "folder":
                    res = hub.open_folder(app_id)
                else:
                    return self._json({"ok": False, "error": "unknown action"}, 404)
                return self._json(res, 200 if res.get("ok", True) else 409)
            return self._json({"ok": False, "error": "not found"}, 404)
        except _BodyTooLarge:
            return self._json({"ok": False, "error": "body too large"}, 413)
        except Exception as exc:  # noqa: BLE001
            if _client_disconnected(exc):
                self.close_connection = True
                return None
            if isinstance(exc, AgoraBusy):
                return self._agora_busy()
            logger.exception("POST %s failed", path)
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


    def _mcp_relay(self) -> bool:
        from .mcp_relay import PREFIX
        path = urlsplit(self.path).path
        if not path.startswith(PREFIX) or not path.endswith("/mcp"):
            return False
        relay = self.hub.facet("mcp_relay")
        if relay is None:
            self._json({"error": "MCP relay unavailable"}, 503)
            return True
        try:
            relay.handle(self)
        except ValueError as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, 400)
        return True

    def do_DELETE(self) -> None:  # noqa: N802
        if self._guard() and not self._mcp_relay():
            self._json({"error": "not found"}, 404)


def serve_in_thread(hub: Hub, host: str = "127.0.0.1", port: Optional[int] = None) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = make_server(hub, host, port)
    thread = threading.Thread(target=server.serve_forever, name="hoard-hub-http", daemon=True)
    thread.start()
    return server, thread
