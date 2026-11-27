"""Authenticated HTTP pass-through for explicitly exposed application streams."""

from __future__ import annotations

from contextlib import closing
from http.client import HTTPConnection, HTTPException, HTTPResponse, IncompleteRead
from io import BufferedReader, RawIOBase
from ipaddress import ip_address
import re
import select
import socket
from threading import BoundedSemaphore
from time import monotonic
from urllib.parse import urlsplit

from . import contract, stream_routes
from .stream_trace import HEADER, GatewayTrace

_PREFIX = re.compile(r"/api/apps/([a-z0-9_-]{1,64})/stream(?:/(.*))?")
_JSON = "application/json"
_NDJSON = "application/x-ndjson"
_REQUEST_LIMIT = {"json": 256 * 1024, "audio": 17 * 1024 * 1024}
_REPLY_LIMIT = {_JSON: 4 * 1024 * 1024, _NDJSON: 64 * 1024 * 1024}
_TIME_LIMIT = {"": 300, "json": 65, "audio": 120}
_CONNECT_S = 3
_UPLOAD_S = 60
_CLIENT_IDLE_S = 10
_UPSTREAM_IDLE_S = 60
_BLOCK = 64 * 1024
_SLOTS = BoundedSemaphore(8)
_CANCEL_SLOTS = BoundedSemaphore(4)


class _Problem(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code


class _ClientClosed(Exception):
    pass


def match(path):
    return _PREFIX.fullmatch(path)


def _origin(url, hub_port):
    address = urlsplit(url)
    host = address.hostname
    if (address.scheme != "http" or not host or address.username is not None or address.password is not None
            or address.path not in ("", "/") or address.query or address.fragment
            or not address.port or address.port == hub_port):
        raise ValueError("invalid application origin")
    if host == "localhost":
        host = "127.0.0.1"
    if "%" in host or not ip_address(host).is_loopback:
        raise ValueError("application origin must be loopback")
    return host, address.port


def _read_upload(handler, mode):
    lengths = handler.headers.get_all("Content-Length") or []
    if "Transfer-Encoding" in handler.headers or len(lengths) > 1:
        raise _Problem(400, "invalid_body_framing")
    length = lengths[0] if lengths else "0"
    if not re.fullmatch(r"[0-9]{1,10}", length):
        raise _Problem(400, "invalid_content_length")
    size = int(length)
    if not mode:
        if size:
            raise _Problem(400, "body_not_allowed")
        return None
    if size == 0:
        raise _Problem(411, "content_length_required")
    if size > _REQUEST_LIMIT[mode]:
        raise _Problem(413, "body_too_large")
    if handler.headers.get_content_type() != _JSON:
        raise _Problem(415, "json_required")
    if handler.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise _Problem(415, "content_encoding_not_supported")

    deadline = monotonic() + _UPLOAD_S
    body = bytearray()
    while len(body) < size:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        handler.connection.settimeout(min(remaining, _CLIENT_IDLE_S))
        chunk = handler.rfile.read1(min(size - len(body), _BLOCK))
        if not chunk:
            raise _ClientClosed
        body.extend(chunk)
    return body


def _check_client(downstream, deadline):
    if monotonic() >= deadline:
        raise TimeoutError
    if downstream is None:
        return
    try:
        readable, _, _ = select.select([downstream], [], [], 0)
        disconnected = readable and not downstream.recv(1, socket.MSG_PEEK)
    except OSError as exc:
        raise _ClientClosed from exc
    if disconnected:
        raise _ClientClosed


class _UpstreamRead(RawIOBase):
    """Poll for readiness before each read, including reads of HTTP framing."""

    def __init__(self, upstream, downstream, deadline):
        self.upstream = upstream
        self.downstream = downstream
        self.deadline = deadline
        # Keep the underlying fd alive when HTTPConnection sees Connection: close.
        self.raw = upstream.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        idle = monotonic() + _UPSTREAM_IDLE_S
        while True:
            _check_client(self.downstream, self.deadline)
            wait = min(idle, self.deadline) - monotonic()
            if wait <= 0:
                raise TimeoutError
            readable, _, _ = select.select([self.upstream], [], [], min(wait, 0.05))
            if readable:
                return self.raw.readinto(buffer)

    def close(self):
        self.raw.close()
        super().close()


class _ResponseSocket:
    def __init__(self, sock, client, deadline):
        self.sock = sock
        self.client = client
        self.deadline = deadline

    def makefile(self, mode):
        return BufferedReader(_UpstreamRead(self.sock, self.client, self.deadline))


def _headers(token, trace, *, accept, body=None):
    headers = {"Authorization": "Bearer " + token, **trace.headers,
               "Accept": accept, "Accept-Encoding": "identity"}
    if body is not None:
        headers.update({"Content-Type": _JSON, "Content-Length": str(len(body))})
    return headers


def _response_media(response, expected):
    status = response.status
    if not (200 <= status < 300 or 400 <= status <= 599):
        raise _Problem(502, "unexpected_upstream_status")
    actual = (response.getheader("Content-Type") or "").partition(";")[0].strip().lower()
    media = _JSON if status >= 400 or expected == "json" else _NDJSON
    encoding = response.getheader("Content-Encoding", "identity").lower()
    if actual != media or encoding != "identity":
        raise _Problem(502, "unexpected_upstream_media")
    transfer = response.getheader("Transfer-Encoding")
    if transfer is not None and (transfer.lower() != "chunked" or not response.chunked):
        raise _Problem(502, "invalid_upstream_framing")
    lengths = [value for name, value in response.getheaders() if name.lower() == "content-length"]
    if lengths and (len(lengths) != 1 or response.chunked or not re.fullmatch(r"[0-9]{1,12}", lengths[0])):
        raise _Problem(502, "invalid_upstream_framing")
    if lengths and int(lengths[0]) > _REPLY_LIMIT[media]:
        raise _Problem(502, "upstream_body_too_large")
    return media


def _write_timeout(handler, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    handler.connection.settimeout(min(remaining, _CLIENT_IDLE_S))


def _start_reply(handler, status, media, trace):
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", media + "; charset=utf-8")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Connection", "close")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("X-Content-Type-Options", "nosniff")
        for name, value in trace.headers.items():
            handler.send_header(name, value)
        handler.end_headers()
    except TimeoutError:
        raise
    except OSError as exc:
        raise _ClientClosed from exc


def _send_chunk(handler, frame, deadline):
    _write_timeout(handler, deadline)
    try:
        handler.wfile.write(frame)
        handler.wfile.flush()
    except TimeoutError:
        raise
    except OSError as exc:
        raise _ClientClosed from exc


def _copy_reply(handler, upstream, media, deadline, trace):
    forwarded = 0
    while True:
        _check_client(handler.connection, deadline)
        chunk = upstream.read1(_BLOCK)
        arrived = monotonic()
        _check_client(handler.connection, deadline)
        if not chunk:
            if upstream.length:
                raise IncompleteRead(b"", upstream.length)
            _send_chunk(handler, b"0\r\n\r\n", deadline)
            return
        forwarded += len(chunk)
        if forwarded > _REPLY_LIMIT[media]:
            raise _Problem(502, "upstream_body_too_large")
        _send_chunk(handler, f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n", deadline)
        trace.forwarded(arrived)


def _cancel(origin, path, token, trace):
    try:
        with closing(HTTPConnection(*origin, timeout=_CONNECT_S)) as connection:
            deadline = monotonic() + _CONNECT_S
            connection.response_class = lambda sock, **kwargs: HTTPResponse(
                _ResponseSocket(sock, None, deadline), **kwargs,
            )
            connection.request("DELETE", path, headers=_headers(token, trace, accept=_JSON))
            with connection.getresponse():
                pass
    except (OSError, HTTPException):
        pass  # The job may have finished as the client closed.


def _error(handler, status, code, trace=None):
    headers = {"Connection": "close"}
    if trace is not None:
        headers.update(trace.headers)
    try:
        handler._json({"ok": False, "code": code, "error": code.replace("_", " ")}, status, headers=headers)
        # Windows may reset a socket closed with unread upload bytes. Close
        # first in the write direction and discard only a bounded tail.
        client = handler.connection
        client.shutdown(socket.SHUT_WR)
        remaining = _BLOCK
        until = monotonic() + 0.05
        while remaining and (wait := until - monotonic()) > 0:
            readable, _, _ = select.select([client], [], [], wait)
            if not readable or not (chunk := client.recv(remaining)):
                break
            remaining -= len(chunk)
    except OSError:
        pass


def _relay(handler, origin, selected, token, trace):
    route, target, cancel = selected
    started_reply = requested = completed = False
    try:
        body = _read_upload(handler, route.body)
        handler.connection.settimeout(_CLIENT_IDLE_S)
        limit = _TIME_LIMIT[route.body] if route.response == "ndjson" or route.body else _CONNECT_S + _CLIENT_IDLE_S
        deadline = monotonic() + limit
        with closing(HTTPConnection(*origin, timeout=_CONNECT_S)) as connection:
            _check_client(handler.connection, deadline)
            connection.response_class = lambda sock, **kwargs: HTTPResponse(
                _ResponseSocket(sock, handler.connection, deadline), **kwargs,
            )
            accept = _NDJSON + ", " + _JSON if route.response == "ndjson" else _JSON
            connection.request(route.method, target, body, headers=_headers(token, trace, accept=accept, body=body))
            requested = True
            with connection.getresponse() as response:
                media = _response_media(response, route.response)
                _write_timeout(handler, deadline)
                started_reply = True
                _start_reply(handler, response.status, media, trace)
                _copy_reply(handler, response, media, deadline, trace)
                completed = True
                trace.outcome = "ok" if response.status < 400 else "error"
    except _ClientClosed:
        trace.outcome = "cancelled"
    except _Problem as exc:
        if not started_reply:
            _error(handler, exc.status, exc.code, trace)
    except (OSError, HTTPException) as exc:
        if isinstance(exc, TimeoutError):
            trace.outcome = "timeout"
        if not started_reply:
            status = 504 if trace.outcome == "timeout" else 502
            code = "stream_timeout" if status == 504 else "upstream_unavailable"
            _error(handler, status, code, trace)
    finally:
        if cancel and requested and not completed:
            _cancel(origin, cancel, token, trace)
        trace.emit(handler.hub.events)


def handle(handler, matched, url):
    """Authorize the family caller and dispatch a single allowlisted operation."""
    handler.close_connection = True
    hub = handler.hub
    bearer = handler._bearer()
    caller = hub.token_owner(bearer) if bearer else None
    if caller is None:
        return _error(handler, 401, "family_bearer_required")
    if hub.config.isolated:
        return _error(handler, 409, "isolated_hub")
    if url.query or url.fragment or url.netloc:
        return _error(handler, 400, "invalid_stream_url")
    app_id, path = matched.groups()
    app = hub.get("boltagon" if app_id == "cyrano" else app_id)
    if app is None:
        return _error(handler, 404, "unknown_app")
    selected = stream_routes.resolve(app, path)
    if selected is None:
        return _error(handler, 404, "route_not_allowed")
    if selected[0].method != "DELETE" and hub.automation_blocked(app.id):
        return _error(handler, 409, "app_manually_stopped")
    try:
        origin = _origin(app.url, handler.server.server_port)
    except ValueError:
        return _error(handler, 502, "invalid_app_origin")
    token = contract.read_token(app.token_file)
    if not token:
        return _error(handler, 503, "app_token_unavailable")
    slots = _CANCEL_SLOTS if selected[0].method == "DELETE" else _SLOTS
    if not slots.acquire(blocking=False):
        return _error(handler, 429, "stream_busy")
    try:
        tracing = getattr(hub.events, "trace_enabled", None)
        enabled = tracing is None or tracing(caller, app.id)
        trace = GatewayTrace(handler.headers.get(HEADER, ""), caller, app.id, enabled=enabled)
        _relay(handler, origin, selected, token, trace)
    finally:
        slots.release()
