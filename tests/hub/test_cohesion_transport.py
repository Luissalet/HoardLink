from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
import threading

import pytest

from hoard_link import _hubclient
from hoard_link.hub import contract
from hoard_link.hub.server import MAX_BODY, PROXY_MAX_BODY
from ._hub_fakes import FakeApp, make_hub, serve, http


@pytest.mark.parametrize("client", ["fetch", "fetch_detailed", "owner_get", "owner_post"])
def test_family_tokens_never_follow_a_redirect_to_another_service(client):
    received = []
    class Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
        do_POST = do_GET
    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    class Redirect(Target):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_POST = do_GET
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    for server in (target, origin):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{origin.server_port}/call"
        if client in ("fetch", "fetch_detailed"):
            result = getattr(_hubclient, client)(url, {"sensitive": "private"}, headers={"Authorization": "Bearer fixture"})
        elif client == "owner_get":
            result = contract._get(url, "fixture", 2)
        else:
            result = contract._post(url, {}, "fixture", 2)
        assert result[0] == 302 and received == []
    finally:
        for server in (origin, target):
            server.shutdown()
            server.server_close()


def test_authenticated_handoff_accepts_large_chapter_with_bounded_body(tmp_path):
    writer = FakeApp("writer", handlers={"wh_import_story_session": lambda a: {"length": len(a["content"])}})
    hub = make_hub(tmp_path, [writer])
    server = serve(hub)
    try:
        headers = {"Authorization": "Bearer " + hub.token}
        payload = {"tool": "wh_import_story_session", "arguments": {"content": "x" * (MAX_BODY + 100)}}
        code, result = http(hub.config.url + "/api/apps/writer/call", payload, headers)
        assert code == 200 and result["result"]["length"] == MAX_BODY + 100
        # Send headers only: oversized/unauthenticated bodies are refused
        # before a body is read (closing unread multi-MB data may reset TCP).
        for auth, expected in [(headers, 413), ({}, 401)]:
            connection = HTTPConnection("127.0.0.1", hub.config.port, timeout=3)
            try:
                connection.request("POST", "/api/apps/writer/call", headers={"Content-Length": str(PROXY_MAX_BODY + 1), **auth})
                response = connection.getresponse()
                assert response.status == expected
                response.read()
            finally:
                connection.close()
        assert len(writer.calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
        writer.stop()
