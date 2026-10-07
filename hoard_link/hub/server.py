"""The hub's HTTP server: a JSON API, the static UI, and the agent contract.

Standard library only (``http.server``), bound to loopback. Two guards:

* a browser page from another origin cannot call the API — a request whose
  ``Sec-Fetch-Site`` says ``cross-site`` is refused, and every mutating
  route is POST, so a plain link cannot stop an app;
* the agent routes (``/api/agent/*``) need the bearer token from
  ``<data>/mcp-token``, which is what the MCP bridge reads. The UI routes
  do not, because the person at the keyboard is the authority here.

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
GET  /api/backends             what Hoard Link resolves right now
GET  /api/memory               what each GPU holds and which server holds it
GET  /api/services             local backend servers (ComfyUI, Ollama, configured) and who started them
POST /api/services/start|stop  {"id": "comfyui@8188", "gpu": "auto", "wait_s": 0}
GET  /api/lease                GPUs (used/free/reserved), granted leases, queue
GET  /api/lease/<id>           one lease (also keeps a queued one in the queue)
POST /api/lease/request        {owner, purpose, vram_mb, gpu (index | [indices] | "2,3" | "any"), priority, ttl_s, wait, pid[, lease_id]}
POST /api/lease/renew          {lease_id, ttl_s}
POST /api/lease/release        {lease_id}
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

import json
import logging
import mimetypes
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import HUB_VERSION, SERVICE
from .core import Hub
from . import desktop, tools, autostart, profile_editor, portable_profiles
from .lease import LeaseError
from .rules import example_rules
from .jobs import example_jobs
from .events import event_types_help
from .linkchat import LINK_MAX_BODY

logger = logging.getLogger("hoard_hub")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
#: The family's shared theme (hoard_link/ui/hoard-theme.css), served as /ui/hoard-theme.css.
THEME_CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "hoard-theme.css")
MAX_BODY = 256 * 1024


def make_server(hub: Hub, host: str = "127.0.0.1", port: Optional[int] = None) -> ThreadingHTTPServer:
    port = hub.config.port if port is None else port

    class Handler(_HubHandler):
        pass

    Handler.hub = hub
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


class _HubHandler(BaseHTTPRequestHandler):
    hub: Hub
    server_version = f"hoard-hub/{HUB_VERSION}"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        logger.debug("%s " + fmt, self.address_string(), *args)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path: str, cache: bool = False) -> None:
        if not os.path.isfile(path):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
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
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site in ("same-origin", "none") and (self.headers.get("Origin") or "").rstrip("/") in ("", self.hub.config.url):
            return "ui"
        if not site and not self.headers.get("Origin") and self.headers.get("User-Agent", "").startswith("hoard-"):
            return None
        return None

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

    # -- models for every app -----------------------------------------------------
    def _link_chat(self) -> None:
        """POST /api/link/chat. Authenticated before the body is read (it may hold 12 MB of images), so a
        refusal closes the connection instead of leaving unread bytes on a keep-alive socket."""
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
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._json({"ok": False, "error": "bad_request", "detail": "the body must be a JSON object"}, 400)
        status, payload = self.hub.link.chat(who, body)
        return self._json(payload, status)

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
                                  lease_id=body.get("lease_id"), wait_s=body.get("wait_s"))
            elif action == "renew":
                res = arb.renew(str(body.get("lease_id") or ""), body.get("ttl_s"))
            else:
                res = arb.release(str(body.get("lease_id") or ""))
        except LeaseError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        return self._lease_reply(res)

    # -- facets (0.7) ----------------------------------------------------------
    def _facet(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> bool:
        """Let the facets answer; True when one did."""
        from . import facets as _facets

        def agent() -> bool:
            return bool(self._bearer()) and self._bearer() == self.hub.token

        req = _facets.Request(method=method, path=path, query=query, body=body, caller=self._family_caller,
                              agent=agent, headers=self.headers)
        res = _facets.dispatch(getattr(self.hub, "facets", []), req)
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
            if path == "/":
                return self._file(os.path.join(UI_DIR, "index.html"))
            if path == "/ui/hoard-theme.css":
                return self._file(THEME_CSS, cache=False)
            if path.startswith("/ui/"):
                rel = os.path.normpath(path[4:]).replace("\\", "/")
                if rel.startswith("..") or rel.startswith("/"):
                    return self._json({"ok": False, "error": "not found"}, 404)
                return self._file(os.path.join(UI_DIR, rel), cache=False)
            if self._facet("GET", path, query, {}):
                return None
            if path == "/api/health":
                return self._json({"ok": True, "service": SERVICE, "version": HUB_VERSION, "apps": len(hub.apps),
                                   "url": hub.config.url})
            if path == "/api/apps":
                return self._json(hub.snapshot())
            if path == "/api/faustus":
                return self._json(hub.faustus_status())
            if path == "/api/backends":
                return self._json(hub.backends(force=query.get("force", ["0"])[0] in ("1", "true")))
            if path == "/api/services":
                return self._json(hub.services())
            if path == "/api/memory":
                return self._json(hub.gpu_memory())
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
                st["sources"] = hub.backup_sources()
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
                return self._json(hub.link.status(force=query.get("force", ["0"])[0] in ("1", "true")))
            if path == "/api/repos":
                return self._json(hub.repos.snapshot())
            if path.startswith("/api/repos/"):
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
                cfg["browser_found"] = desktop.find_browser(hub.config.browser)
                cfg["native_window"] = desktop.hub_window_native_available()
                cfg["window_engine_used"] = desktop.window_engine(hub.config.window_engine)
                return self._json(cfg)
            if path == "/api/agent/tools":
                if not self._agent_ok():
                    return None
                return self._json({"tools": tools.all_tools()})
            parts = path.split("/")
            if len(parts) >= 4 and parts[1] == "api" and parts[2] == "apps":
                app = hub.get(parts[3])
                if app is None:
                    return self._json({"ok": False, "error": "unknown app"}, 404)
                sub = parts[4] if len(parts) > 4 else ""
                if sub == "":
                    return self._json(hub.app_status(app))
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
            logger.exception("GET %s failed", path)
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = urlsplit(self.path).path.rstrip("/")
        hub = self.hub
        if path == "/api/link/chat":
            try:
                return self._link_chat()
            except Exception as exc:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        try:
            body = self._read_body()
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 413)
        try:
            if self._facet("POST", path, parse_qs(urlsplit(self.path).query), body):
                return None
            if path == "/api/agent/call":
                if not self._agent_ok():
                    return None
                name = str(body.get("tool") or body.get("name") or "")
                args = body.get("arguments") or body.get("args") or {}
                result = tools.call(hub, name, args if isinstance(args, dict) else {})
                ok = not (isinstance(result, dict) and result.get("ok") is False)
                return self._json({"ok": ok, "tool": name, "result": result}, 200 if ok else 400)
            if path in ("/api/lease/request", "/api/lease/renew", "/api/lease/release"):
                return self._lease_post(path.rsplit("/", 1)[1], body)
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
                    res = hub.service_start(sid, gpu=body.get("gpu"), wait_s=float(body.get("wait_s") or 0))
                else:
                    res = hub.service_stop(sid)
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
                res = hub.repos.refresh(wait=bool(body.get("wait", False)))
                return self._json({**res, "summary": hub.repos.snapshot()["summary"]})
            if path.startswith("/api/repos/"):
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
            if len(parts) == 5 and parts[1:3] == ["api", "profiles"] and parts[4] in ("save", "remove"):
                name = unquote(parts[3])
                res = profile_editor.save(hub, name, body) if parts[4] == "save" else profile_editor.remove(hub, name)
                return self._json(res, 200 if res.get("ok") else 400)
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "profiles" and parts[4] in ("start", "stop"):
                name = unquote(parts[3])
                if name not in hub.profiles():
                    return self._json({"ok": False, "error": f"unknown profile: {name}"}, 404)
                res = hub.profile_start(name) if parts[4] == "start" else hub.profile_stop(name)
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
                if hub.get(app_id) is None:
                    return self._json({"ok": False, "error": "unknown app"}, 404)
                if action == "call":
                    who = self._family_ok()
                    if who is None:
                        return None
                    name = str(body.get("tool") or body.get("name") or "")
                    args = body.get("arguments") or body.get("args") or {}
                    try:
                        timeout = float(body.get("timeout_s") or 120.0)
                    except (TypeError, ValueError):
                        timeout = 120.0
                    res = hub.call_app(app_id, name, args if isinstance(args, dict) else {}, caller=who,
                                       timeout=max(1.0, min(timeout, 900.0)))
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("POST %s failed", path)
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def serve_in_thread(hub: Hub, host: str = "127.0.0.1", port: Optional[int] = None) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = make_server(hub, host, port)
    thread = threading.Thread(target=server.serve_forever, name="hoard-hub-http", daemon=True)
    thread.start()
    return server, thread
