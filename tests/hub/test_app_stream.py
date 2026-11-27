"""Drive the real Hub HTTP handler and a voice double over in-memory socket pairs."""

from contextlib import closing, contextmanager
import http.client
from http.server import BaseHTTPRequestHandler
import json
import logging
import socket
from threading import BoundedSemaphore, Event, RLock, Thread
from time import monotonic
from types import SimpleNamespace

import pytest

from hoard_link.hub import stream_gateway
from hoard_link.hub.core import Hub
from hoard_link.hub.registry import read_manifest
from hoard_link.hub.server import _HubHandler

JOB = "a" * 32
VERSION = "cyrano.voice/v0"
TRACE = "b" * 32 + "/" + "c" * 32
VOICE_URL = f"/api/voice/jobs/{JOB}"
TOKEN_HUB = "test-hub-token"
TOKEN_APP = "test-app-token"
TOKEN_CALLER = "test-consumer-token"
HUB_ROUTE = "/api/apps/boltagon/stream/"


class SocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock, *, timeout=4):
        super().__init__("127.0.0.1", timeout=timeout)
        self.sock = sock
        sock.settimeout(timeout)

    def connect(self):
        raise AssertionError("A test attempted to open a socket outside the pair")


class SocketPairServers:
    def __init__(self):
        self.workers = []

    def connect(self, handler, *, timeout=4):
        server, client = socket.socketpair()

        def serve():
            with server:
                handler(server, ("127.0.0.1", 0), SimpleNamespace(server_port=61111))

        worker = Thread(target=serve, daemon=True)
        worker.start()
        self.workers.append(worker)
        return SocketHTTPConnection(client, timeout=timeout)

    def close(self):
        for worker in self.workers:
            worker.join(5)
            assert not worker.is_alive()


class VoiceDouble:
    def __init__(self, pairs):
        self.pairs = pairs
        self.calls = []
        self.entered = Event()
        self.release = Event()
        self.cancelled = Event()
        self.finished = Event()
        self.finished_at = None
        self.mode = "normal"

    def connect(self, host, port, *, timeout):
        # The registered origin is validated before the transport is built.
        assert (host, port) in {("127.0.0.1", 61230), ("127.0.0.1", 61231)}
        peer = self

        class Voice(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def record(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                peer.calls.append((self.command, self.path, dict(self.headers), body, port))
                return body

            def reply(self, status, body):
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = json.loads(self.record())
                if self.path == "/api/voice/speak":
                    if peer.mode == "redirect":
                        self.send_response(302)
                        self.send_header("Location", "/api/other")
                        self.send_header("Content-Length", "0")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        return
                    assert body["version"] == VERSION
                    self.reply(200, {"version": VERSION, "job_id": JOB, "state": "queued",
                                     "stream_url": VOICE_URL + "/stream", "undo_token": JOB})
                elif self.path == "/api/dictation/understand":
                    self.reply(200, {"version": body["version"], "raw_transcript": "texto de prueba"})
                else:
                    self.reply(404, {"error": "not found"})

            def do_DELETE(self):
                self.record()
                assert self.path == VOICE_URL
                peer.cancelled.set()
                peer.release.set()
                try:
                    self.reply(200, {"version": VERSION, "job_id": JOB, "state": "cancelled"})
                except OSError:
                    pass

            def event(self, value):
                data = (json.dumps(value) + "\n").encode("utf-8")
                self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
                self.wfile.flush()

            def do_GET(self):
                self.record()
                assert self.path == VOICE_URL + "/stream"
                peer.entered.set()
                if peer.mode == "wait_headers":
                    peer.release.wait(4)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.event({"type": "start", "version": VERSION, "job_id": JOB, "sample_rate": 16000})
                    self.event({"type": "audio", "offset_frames": 0, "frames": 1, "data_base64": "AAA="})
                    if peer.mode == "partial":
                        return
                    if not peer.release.wait(4) or peer.cancelled.is_set():
                        return
                    self.event({"type": "audio", "offset_frames": 1, "frames": 1, "data_base64": "AQA="})
                    self.event({"type": "end", "version": VERSION, "job_id": JOB, "state": "succeeded"})
                    peer.finished_at = monotonic()
                    peer.finished.set()
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

        # The fake uses its own timeout. The gateway can still set its read timeout.
        connection = self.pairs.connect(Voice)
        sock, connection.sock = connection.sock, None

        class Upstream(SocketHTTPConnection):
            def __init__(self):
                http.client.HTTPConnection.__init__(self, host, port, timeout=timeout)

            def connect(self):
                self.sock = sock

        return Upstream()


def _manifest(tmp_path, app_id, port, token, *, expose=None):
    folder = tmp_path / app_id
    folder.mkdir()
    path = folder / "faustus-plugin.json"
    raw = {
        "schema": 1, "id": app_id,
        "app": {"url_default": f"http://127.0.0.1:{port}",
                "health": {"expect": {"service": f"{app_id}-hoard"}}},
    }
    if expose is not None:
        raw["x-family"] = {"stream_routes": expose}
    path.write_text(json.dumps(raw), encoding="utf-8")
    app = read_manifest(path)
    folder = folder / "data"
    folder.mkdir()
    (folder / "mcp-token").write_text(token, encoding="utf-8")
    return app


@pytest.fixture
def rig(tmp_path, monkeypatch):
    pairs = SocketPairServers()
    double = VoiceDouble(pairs)
    traces = []
    # Use the real alias and bearer lookups with fixture manifests, without Hub
    # startup, process ownership, workers, scanners, listeners or installed data.
    hub = Hub.__new__(Hub)
    hub._lock = RLock()
    hub._stopping_all = Event()
    hub._manual_stops = set()
    hub._apps = {
        "boltagon": _manifest(tmp_path, "boltagon", 61230, TOKEN_APP),
        "consumer": _manifest(tmp_path, "consumer", 61231, TOKEN_CALLER, expose=[]),
    }
    hub.config = SimpleNamespace(isolated=False, url="http://127.0.0.1:61111")
    hub.token = TOKEN_HUB
    hub.facets = []
    hub.events = SimpleNamespace(emit_trace_spans=lambda batch, *, source: traces.extend((source, rec) for rec in batch))

    class Handler(_HubHandler):
        ui_sessions = SimpleNamespace(accepts=lambda _: True)  # UI cannot authorize a stream.

    Handler.hub = hub
    monkeypatch.setattr(stream_gateway, "HTTPConnection", double.connect)

    @contextmanager
    def request(route, body=None, *, token=TOKEN_CALLER, headers=None, method="POST"):
        headers = dict(headers or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        connection = pairs.connect(Handler)
        try:
            connection.request(method, route, body=body, headers=headers)
            with connection.getresponse() as response:
                yield response
        finally:
            connection.close()

    yield SimpleNamespace(double=double, hub=hub, connect=lambda: pairs.connect(Handler), request=request, traces=traces)
    double.release.set()
    pairs.close()


def _reply(rig, route, body=None, **kwargs):
    with rig.request(route, body, **kwargs) as response:
        return response.status, json.loads(response.read())


def _speak(rig, route=HUB_ROUTE + "voice/speak", **kwargs):
    return _reply(rig, route, json.dumps({"version": VERSION, "text": "Frase de prueba", "wait": False}), **kwargs)


def test_original_voice_contract_and_audio_upload(rig):
    assert _speak(rig, token=TOKEN_HUB) == (200, {
        "version": VERSION, "job_id": JOB, "state": "queued", "stream_url": VOICE_URL + "/stream", "undo_token": JOB,
    })
    audio = json.dumps({"version": "cyrano.dictation/v0", "audio": {
        "mime_type": "audio/wav", "data_base64": "AAAA" * 100_000,
    }})
    assert _reply(rig, HUB_ROUTE + "dictation/understand", audio) == (
        200, {"version": "cyrano.dictation/v0", "raw_transcript": "texto de prueba"},
    )
    assert rig.double.calls[1][3] == audio.encode()
    assert _reply(rig, HUB_ROUTE + f"voice/jobs/{JOB}/cancel")[1]["state"] == "cancelled"
    assert [(entry[0], entry[1]) for entry in rig.double.calls] == [
        ("POST", "/api/voice/speak"), ("POST", "/api/dictation/understand"), ("DELETE", VOICE_URL),
    ]


def test_first_pcm_reaches_client_before_synthesis_ends(rig, record_property):
    with rig.request(HUB_ROUTE + f"voice/jobs/{JOB}/stream", headers={
        "X-Hoard-Trace": TRACE, "Cookie": "caller-cookie", "X-Untrusted": "discard",
    }) as response:
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("application/x-ndjson")
        assert response.getheader("X-Accel-Buffering") == "no"
        downstream_trace = response.getheader("X-Hoard-Trace")
        assert json.loads(response.readline())["type"] == "start"
        assert json.loads(response.readline())["data_base64"] == "AAA="
        first_at = monotonic()
        assert not rig.double.finished.is_set()
        rig.double.release.set()
        assert json.loads(response.readline())["offset_frames"] == 1
        assert json.loads(response.readline())["state"] == "succeeded"
        assert response.read() == b""
    assert rig.double.finished_at >= first_at
    record_property("first_pcm_before_synthesis_end_ms", (rig.double.finished_at - first_at) * 1000)
    headers = rig.double.calls[0][2]
    assert headers["Authorization"] == "Bearer " + TOKEN_APP
    assert headers["X-Hoard-Trace"] == downstream_trace
    assert "Cookie" not in headers and "X-Untrusted" not in headers
    assert not rig.double.cancelled.is_set()

    # Trace writes come after the terminal HTTP chunk.
    rig.double.pairs.close()
    (_, parent), (_, forward) = rig.traces
    assert parent["trace_id"] == "b" * 32 and parent["parent_id"] == "c" * 32
    assert parent["outcome"] == "ok" and parent["caller"] == "consumer" and parent["callee"] == "boltagon"
    assert parent["version"] == "hoard.trace/v1"
    assert forward["parent_id"] == parent["span_id"] and forward["operation"] == "gateway.forward_first"
    assert 0 <= forward["duration_ms"] <= parent["duration_ms"]
    assert all(source == "hub" for source, _ in rig.traces)
    record_property("gateway_forward_first_ms", forward["duration_ms"])


def test_disconnect_while_paused_cancels_the_job(rig):
    with rig.request(HUB_ROUTE + f"voice/jobs/{JOB}/stream") as response:
        assert json.loads(response.readline())["type"] == "start"
        assert json.loads(response.readline())["type"] == "audio"
    assert rig.double.cancelled.wait(2)
    assert [(row[0], row[1]) for row in rig.double.calls] == [
        ("GET", VOICE_URL + "/stream"), ("DELETE", VOICE_URL),
    ]
    rig.double.pairs.close()
    assert rig.traces[0][1]["outcome"] == "cancelled"


def test_disconnect_before_upstream_headers(rig):
    rig.double.mode = "wait_headers"
    with closing(rig.connect()) as connection:
        connection.request("POST", HUB_ROUTE + f"voice/jobs/{JOB}/stream", headers={
            "Authorization": "Bearer " + TOKEN_CALLER,
        })
        assert rig.double.entered.wait(2)
    assert rig.double.cancelled.wait(2)
    assert not rig.double.finished.is_set()


def test_aborted_upstream_is_truncated_and_cancelled(rig):
    rig.double.mode = "partial"
    with rig.request(HUB_ROUTE + f"voice/jobs/{JOB}/stream") as response:
        assert response.status == 200
        with pytest.raises(http.client.IncompleteRead):
            response.read()
    assert rig.double.cancelled.wait(2)


def test_timeout_aborts_transfer_and_cancels(rig, monkeypatch):
    rig.double.mode = "wait_headers"
    monkeypatch.setitem(stream_gateway._TIME_LIMIT, "", 0.25)
    status, body = _reply(rig, HUB_ROUTE + f"voice/jobs/{JOB}/stream")
    assert (status, body["code"]) == (504, "stream_timeout")
    assert rig.double.cancelled.wait(2)
    rig.double.pairs.close()
    assert rig.traces[0][1]["outcome"] == "timeout"


@pytest.mark.parametrize(("suffix", "headers", "token", "code"), [
    ("voice/speak", {"X-Hoard-Ui-Session": "browser-proof"}, None, 401),
    ("voice/speak", {}, "wrong-token", 401),
    ("voice/speak", {"Sec-Fetch-Site": "cross-site"}, TOKEN_CALLER, 403),
    ("voice/speak?url=http://localhost", {}, TOKEN_CALLER, 400),
    ("voice/%73peak", {}, TOKEN_CALLER, 404),
    ("voice/../voice/speak", {}, TOKEN_CALLER, 404),
    ("voice/jobs/" + "z" * 32 + "/stream", {}, TOKEN_CALLER, 404),
    ("health", {}, TOKEN_CALLER, 404),
    ("dictation/understand", {"Content-Length": str(18 * 1024 * 1024)}, TOKEN_CALLER, 413),
    ("voice/speak", {"Transfer-Encoding": "chunked"}, TOKEN_CALLER, 400),
    ("voice/jobs/" + JOB + "/stream", {"Content-Length": "3"}, TOKEN_CALLER, 400),
])
def test_auth_route_and_upload_limits_precede_upstream(rig, suffix, headers, token, code):
    assert _reply(rig, HUB_ROUTE + suffix, headers=headers, token=token)[0] == code
    assert not rig.double.calls


def test_unknown_app_and_redirect_are_explicit_and_never_replayed(rig):
    assert _speak(rig, "/api/apps/missing/stream/voice/speak") == (
        404, {"ok": False, "code": "unknown_app", "error": "unknown app"},
    )
    rig.double.mode = "redirect"
    status, error = _speak(rig)
    assert (status, error["code"]) == (502, "unexpected_upstream_status")
    assert len(rig.double.calls) == 1


def test_legacy_id_prefers_current_app_and_current_id_falls_back(rig, tmp_path):
    rig.hub._apps["cyrano"] = _manifest(tmp_path, "cyrano", 61231, "legacy-app-token")
    assert _speak(rig, "/api/apps/cyrano/stream/voice/speak")[0] == 200
    assert rig.double.calls[-1][4] == 61230
    del rig.hub._apps["boltagon"]
    assert _speak(rig)[0] == 200
    assert rig.double.calls[-1][4] == 61231
    assert rig.double.calls[-1][2]["Authorization"] == "Bearer legacy-app-token"


def test_manifest_opt_in_is_authoritative(rig):
    app = rig.hub.get("boltagon")
    app.family["stream_routes"] = []
    assert _speak(rig)[0] == 404
    app.family["stream_routes"] = [{"route": "custom", "method": "POST", "path": "/api/voice/speak", "body": "json"}]
    assert _speak(rig, HUB_ROUTE + "custom")[0] == 200
    app.family["stream_routes"][0]["path"] = "/api/../voice/speak"
    assert _speak(rig, HUB_ROUTE + "custom")[0] == 404
    assert len(rig.double.calls) == 1


@pytest.mark.parametrize("url", ["http://203.0.113.10:61230", "http://127.0.0.1:61111",
                                  "http://user@127.0.0.1:61230", "http://127.0.0.1:61230/other"])
def test_only_registered_local_origins_are_allowed(rig, url):
    rig.hub.get("boltagon").url = url
    status, error = _speak(rig)
    assert (status, error["code"]) == (502, "invalid_app_origin")
    assert not rig.double.calls


def test_passive_hub_stays_offline(rig):
    rig.hub.config.isolated = True
    assert _speak(rig)[1]["code"] == "isolated_hub"
    assert not rig.double.calls


def test_reply_byte_limits_cover_declared_and_streaming_bodies(rig, monkeypatch):
    monkeypatch.setitem(stream_gateway._REPLY_LIMIT, "application/json", 16)
    status, error = _speak(rig)
    assert (status, error["code"]) == (502, "upstream_body_too_large")

    monkeypatch.setitem(stream_gateway._REPLY_LIMIT, "application/x-ndjson", 150)
    with rig.request(HUB_ROUTE + f"voice/jobs/{JOB}/stream") as response:
        assert json.loads(response.readline())["type"] == "start"
        with pytest.raises(http.client.IncompleteRead):
            response.read()
    assert rig.double.cancelled.wait(2)


def test_upload_timeout_does_not_start_an_upstream_call(rig, monkeypatch):
    monkeypatch.setattr(stream_gateway, "_UPLOAD_S", 0.15)
    with rig.request(HUB_ROUTE + "voice/speak", b"{", headers={"Content-Length": "120"}) as response:
        assert response.status == 504 and json.loads(response.read())["code"] == "stream_timeout"
    assert not rig.double.calls


def test_concurrency_bound_preserves_active_stream(rig, monkeypatch):
    monkeypatch.setattr(stream_gateway, "_SLOTS", BoundedSemaphore(1))
    with rig.request(HUB_ROUTE + f"voice/jobs/{JOB}/stream") as response:
        assert json.loads(response.readline())["type"] == "start"
        status, error = _speak(rig)
        assert (status, error["code"]) == (429, "stream_busy")
        rig.double.release.set()
        assert b"succeeded" in response.read()
    rig.double.pairs.close()
    assert not rig.double.cancelled.is_set()
    assert _speak(rig)[0] == 200


def test_http_debug_log_omits_job_id(rig, caplog):
    with caplog.at_level(logging.DEBUG, logger="hoard_hub"):
        assert _reply(rig, HUB_ROUTE + f"voice/jobs/{JOB}/cancel")[0] == 200
    assert "app stream 200" in caplog.text
    assert JOB not in caplog.text and TOKEN_APP not in caplog.text


def test_manual_stop_leaves_the_cancel_route_available(rig):
    rig.hub._manual_stops.add("boltagon")
    status, error = _speak(rig)
    assert (status, error["code"]) == (409, "app_manually_stopped")
    assert _reply(rig, HUB_ROUTE + f"voice/jobs/{JOB}/cancel")[0] == 200
    assert [row[0] for row in rig.double.calls] == ["DELETE"]


def test_existing_json_call_still_uses_the_family_tool_contract(rig):
    def call(app, tool, arguments, *, caller, **options):
        assert (app, tool, arguments, caller) == ("boltagon", "test_tool", {"value": 2}, "consumer")
        return {"ok": True, "result": 3}

    rig.hub.call_app = call
    body = json.dumps({"tool": "test_tool", "arguments": {"value": 2}})
    assert _reply(rig, "/api/apps/boltagon/call", body) == (200, {"ok": True, "result": 3})
    assert not rig.double.calls


def test_trace_policy_can_exclude_the_gateway(rig):
    rig.hub.events.trace_enabled = lambda caller, callee: (caller, callee) != ("consumer", "boltagon")
    with rig.request(HUB_ROUTE + f"voice/jobs/{JOB}/cancel", headers={"X-Hoard-Trace": TRACE}) as response:
        assert response.status == 200 and response.getheader("X-Hoard-Trace") is None
        assert json.loads(response.read())["state"] == "cancelled"
    assert "X-Hoard-Trace" not in rig.double.calls[0][2]
    rig.double.pairs.close()
    assert not rig.traces


def test_invalid_trace_input_is_replaced(rig):
    with rig.request(HUB_ROUTE + f"voice/jobs/{JOB}/cancel", headers={
        "X-Hoard-Trace": "invalid private value",
    }) as response:
        trace_id, span_id = response.getheader("X-Hoard-Trace").split("/")
        assert len(trace_id) == len(span_id) == 32
        assert response.status == 200
        response.read()
    rig.double.pairs.close()
    _, trace = rig.traces[0]
    assert trace["parent_id"] is None and trace["trace_id"] == trace_id and trace["span_id"] == span_id
    assert "invalid private value" not in str(rig.traces)


def test_explicit_cancellation_has_separate_capacity(rig, monkeypatch):
    uploads = BoundedSemaphore(1)
    assert uploads.acquire(blocking=False)
    monkeypatch.setattr(stream_gateway, "_SLOTS", uploads)
    assert _speak(rig)[0] == 429
    assert _reply(rig, HUB_ROUTE + f"voice/jobs/{JOB}/cancel")[0] == 200
    assert [row[0] for row in rig.double.calls] == ["DELETE"]
