"""Real HTTP tests: standard-library host and isolated Faustus contract server."""
import base64
import contextlib
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hoard_link.copilot_stdlib import CopilotRelay


@contextlib.contextmanager
def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(server, method, target, body=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    headers = {}
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    connection.request(method, target, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


@pytest.fixture
def upstream():
    class Handler(BaseHTTPRequestHandler):
        records = []
        release = threading.Event()
        sent = threading.Event()
        disconnected = threading.Event()

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.records.append((self.command, self.path, dict(self.headers), b""))
            body = (b'{"contract_version":"1.0"}' if self.path.endswith("capabilities")
                    else b"export const panel = true;")
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path.endswith("capabilities") else "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", "26")
            self.end_headers()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.records.append((self.command, self.path, dict(self.headers), body))
            if self.path == "/api/chat/disconnect":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for _ in range(512):
                        self.wfile.write(b"data: " + b"x" * 65528 + b"\n\n")
                        self.wfile.flush()
                except OSError:
                    self.disconnected.set()
                return
            if self.path in ("/api/chat_stream", "/api/chat/chunked"):
                chunked = self.path.endswith("chunked")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("X-Odysseus-Run-Id", "run-fixture")
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                first, last = b'data: {"type":"start"}\n\n', b'data: {"type":"end"}\n\n'
                self.wfile.write((f"{len(first):X}\r\n".encode() + first + b"\r\n") if chunked else first)
                self.wfile.flush()
                self.sent.set()
                self.release.wait(3)
                self.wfile.write((f"{len(last):X}\r\n".encode() + last + b"\r\n0\r\n\r\n") if chunked else last)
                return
            if self.path.endswith(("/error", "/error502")):
                self.send_response(502 if self.path.endswith("502") else 422)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Faustus-Api-Version", "1")
                self.end_headers()
                self.wfile.write(b'{"error":"fixture validation"}')
                return
            self.send_response(201)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Faustus-Idempotent-Replay", "true")
            self.send_header("Set-Cookie", "must-not-forward=1")
            self.end_headers()
            self.wfile.write(body)

    with serve(Handler) as server:
        yield server, Handler
        Handler.release.set()


@pytest.fixture
def host(tmp_path, upstream):
    server, upstream_handler = upstream
    relay = CopilotRelay("disk", tmp_path, app_name="DiskHoard", token="server-secret",
                         faustus_urls=[f"http://127.0.0.1:{server.server_port}"], env={})

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if relay.handle(self):
                return
            body = relay.inject_html(b"<html><body><h1>Private QA</h1></body></html>")
            relay._reply(self, 200, body, "text/html")

        def do_POST(self):
            if not relay.handle(self):
                relay._reply(self, 404, b"{}")

        def do_HEAD(self):
            relay.handle(self)

    with serve(Handler) as server:
        yield server, relay, upstream_handler


def test_info_identity_and_secret(host, tmp_path):
    server, relay, upstream = host
    status, headers, body = request(server, "GET", "/api/copilot/info")
    assert status == 200
    info = json.loads(body)
    assert info["app_id"] == "disk"
    assert info["tool_server_id"] == "disk"
    assert info["app_name"] == "DiskHoard"
    assert info["data_space_id"] == relay.space_id
    assert b"server-secret" not in body
    assert upstream.records[0][2]["Authorization"] == "Bearer server-secret"
    again = CopilotRelay("disk", tmp_path, env={})
    assert again.space_id == relay.space_id


def test_config_env_and_explicit_precedence(tmp_path):
    config = tmp_path / "backend.json"
    config.write_text(json.dumps({"faustus": {"url": "http://config.invalid/", "token": "config-token"}}))
    file = CopilotRelay("disk", tmp_path / "file", config_path=config, env={})
    assert file.urls == ("http://config.invalid",)
    assert file.token == "config-token"
    env = {"HOARD_FAUSTUS_URL": "http://global.invalid", "DISK_FAUSTUS_URL": " http://app.invalid/ ", "HOARD_FAUSTUS_TOKEN": "env-token"}
    override = CopilotRelay("disk", tmp_path / "env", config_path=config, env=env)
    assert override.urls == ("http://app.invalid",)
    assert override.token == "env-token"
    explicit = CopilotRelay("disk", tmp_path / "explicit", config_path=config, env=env,
                            faustus_urls=["http://explicit.invalid/"], token="")
    assert explicit.urls == ("http://explicit.invalid",)
    assert explicit._auth() == {}


def test_launcher_panel_head_and_html(host):
    server, relay, _ = host
    status, _, launcher = request(server, "GET", "/api/copilot/launcher.js")
    assert status == 200
    assert b"faustus-chat" in launcher and b"contextProvider" in launcher
    assert b"server-secret" not in launcher
    assert request(server, "GET", "/api/copilot/panel.js")[2] == b"export const panel = true;"
    assert request(server, "HEAD", "/api/copilot/panel.js")[2] == b""
    assert request(server, "HEAD", "/api/copilot/info")[2] == b""
    html = request(server, "GET", "/")[2]
    assert html.count(b"/api/copilot/launcher.js") == 1
    assert relay.inject_html(html) == html


def test_proxy_json_forces_origin_and_whitelists_headers(host):
    server, relay, upstream = host
    payload = {"context": {"origin": {"app_id": "forged", "data_space_id": "wrong", "surface": "page"}, "view": {"locale": "es"}},
               "hoard_context": {"origin": "invalid-origin"}, "text": "Prueba"}
    status, headers, body = request(server, "POST", "/api/copilot/proxy", {
        "path": "/api/hoard-copilot/conversations?fixture=1", "method": "POST", "body": payload,
        "headers": {"Authorization": "Bearer browser-secret", "X-Odysseus-Run-Id": "run-browser", "X-Faustus-Client-Version": "1"}})
    assert status == 201
    result = json.loads(body)
    assert result["context"]["origin"] == {**relay.origin, "surface": "page"}
    assert result["hoard_context"]["origin"] == relay.origin
    assert result["context"]["view"] == {"locale": "es"}
    assert result["text"] == "Prueba"
    record = upstream.records[-1]
    assert record[1].endswith("?fixture=1")
    assert record[2]["Authorization"] == "Bearer server-secret"
    assert record[2]["X-Odysseus-Run-Id"] == "run-browser"
    assert headers["X-Faustus-Idempotent-Replay"] == "true"
    assert "Set-Cookie" not in headers


def test_multipart_preserves_uploaded_bytes(host):
    server, relay, upstream = host
    upload = bytes(range(256)) + b"\r\n--fixture-not-a-boundary\x00\xff"
    original = (b'--fixture\r\nContent-Disposition: form-data; name="hoard_context"\r\n\r\n'
                b'{"origin":{"app_id":"forged"},"view":{"locale":"es"}}\r\n'
                b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="raw.bin"\r\n'
                b'Content-Type: application/octet-stream\r\n\r\n' + upload + b'\r\n--fixture--\r\n')
    status, _, body = request(server, "POST", "/api/copilot/proxy", {"path": "/api/chat/upload", "method": "POST",
        "raw_body": base64.b64encode(original).decode(), "content_type": 'multipart/form-data; boundary="fixture"'})
    assert status == 201
    assert upload in body
    assert body[body.index(b'--fixture\r\nContent-Disposition: form-data; name="file"'):] == original[original.index(b'--fixture\r\nContent-Disposition: form-data; name="file"'):]
    context = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--fixture", 1)[0])
    assert context["origin"] == relay.origin
    assert context["view"] == {"locale": "es"}
    assert upstream.records[-1][2]["Content-Type"] == 'multipart/form-data; boundary="fixture"'


@pytest.mark.parametrize("target", ["/api/chat_stream", "/api/chat/chunked"])
def test_sse_first_event_arrives_before_upstream_finishes(host, target):
    server, _, upstream = host
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    connection.request("POST", "/api/copilot/proxy", json.dumps({"path": target, "method": "POST", "body": {"text": "fixture"}}), {"Content-Type": "application/json"})
    response = connection.getresponse()
    assert upstream.sent.wait(1)
    assert response.status == 200
    assert response.getheader("X-Odysseus-Run-Id") == "run-fixture"
    assert response.getheader("X-Accel-Buffering") == "no"
    assert response.readline() == b'data: {"type":"start"}\n'
    assert not upstream.release.is_set(), "relay did not buffer until stream completion"
    upstream.release.set()
    assert b'"end"' in response.read()
    connection.close()


@pytest.mark.parametrize("target, expected", [("/api/chat/error", 422), ("/api/chat/error502", 502)])
def test_upstream_http_error_keeps_status_body_and_headers(host, target, expected):
    status, headers, body = request(host[0], "POST", "/api/copilot/proxy", {"path": target, "method": "POST", "body": {}})
    assert status == expected
    assert json.loads(body) == {"error": "fixture validation"}
    assert headers["X-Faustus-Api-Version"] == "1"


def test_downstream_disconnect_closes_upstream(host):
    server, _, upstream = host
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    connection.request("POST", "/api/copilot/proxy", json.dumps({"path": "/api/chat/disconnect", "method": "POST", "body": {}}), {"Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.readline().startswith(b"data: ")
    response.close()
    connection.close()
    assert upstream.disconnected.wait(2), "Faustus connection closed when panel disconnects"


@pytest.mark.parametrize("envelope", [[], {"path": "https://elsewhere.invalid/api/chat_stream"}, {"path": "//elsewhere.invalid/api/chat_stream"},
    {"path": "/api/health"}, {"path": "/api/chat_stream#fragment"}, {"path": 42}, {"path": "/api/chat/upload", "raw_body": "not base64"},
    {"path": "/api/chat/upload", "headers": ["invalid"]}])
def test_invalid_proxy_envelopes(host, envelope):
    status, _, body = request(host[0], "POST", "/api/copilot/proxy", json.dumps(envelope).encode())
    assert status == 400
    assert json.loads(body)["code"] == "bad_request"


def test_wrong_method(host):
    status, headers, _ = request(host[0], "POST", "/api/copilot/info", {})
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"


def test_unavailable_and_cached_endpoint_failure(host):
    server, relay, _ = host
    relay.urls = ()
    status, _, body = request(server, "GET", "/api/copilot/info")
    assert status == 503
    assert json.loads(body)["code"] == "copilot_unavailable"
    relay._selected = "http://127.0.0.1:1"
    status, _, _ = request(server, "GET", "/api/copilot/panel.js")
    assert status == 503
    assert relay._selected is None
