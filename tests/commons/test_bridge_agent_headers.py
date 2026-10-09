"""The MCP bridge forwards who is calling: HOARD_AGENT_ID / HOARD_AGENT_SESSION become X-Agent-Id / X-Agent-Session."""

from __future__ import annotations

import http.server
import json
import sys
import threading

import pytest

from hoard_link.bridge import CatalogBridge, agent_headers

TOKEN = "k" * 40


class Recorder:
    def __init__(self):
        self.calls = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path == "/api/agent/tools":
                    return self._reply({"tools": [{"name": "note_add", "description": "d", "inputSchema": {"type": "object", "properties": {}},
                                                   "annotations": {"readOnlyHint": False}}]})
                self._reply({"service": "fake-hoard"})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append({"body": body, "agent": self.headers.get("X-Agent-Id"), "session": self.headers.get("X-Agent-Session"),
                                    "auth": self.headers.get("Authorization")})
                self._reply({"ok": True})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    rec = Recorder()
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "mcp-token").write_text(TOKEN)
    monkeypatch.setenv("FAKE_DATA_DIR", str(folder))
    for name in ("HOARD_AGENT_ID", "HOARD_AGENT_SESSION", "FAKE_TOKEN", "FAKE_TOKEN_FILE", "FAKE_URL", "FAKE_PORT"):
        monkeypatch.delenv(name, raising=False)
    yield rec
    rec.close()


def bridge(rec):
    return CatalogBridge(app="fake", service="fake-hoard", package="fake_pkg", default_port=rec.port, data_dir_env="FAKE_DATA_DIR", autostart=False)


def test_agent_headers_from_the_environment():
    assert agent_headers({}) == {}
    assert agent_headers({"HOARD_AGENT_ID": "codex-sparks", "HOARD_AGENT_SESSION": "run-42"}) == {"X-Agent-Id": "codex-sparks", "X-Agent-Session": "run-42"}
    assert agent_headers({"HOARD_AGENT_ID": "  cursor  ", "HOARD_AGENT_SESSION": "   "}) == {"X-Agent-Id": "cursor"}
    assert agent_headers({"HOARD_AGENT_ID": "ágent\n\x07x" + "y" * 200})["X-Agent-Id"] == "gentx" + "y" * 75         # printable ASCII, 80 characters
    assert len(agent_headers({"HOARD_AGENT_SESSION": "s" * 500})["X-Agent-Session"]) == 120


async def test_the_bridge_sends_the_identity_with_every_call(recorder, monkeypatch):
    monkeypatch.setenv("HOARD_AGENT_ID", "codex-sparks")
    monkeypatch.setenv("HOARD_AGENT_SESSION", "run-42")
    result = await bridge(recorder).call("note_add", {"text": "hi"})
    assert not result.is_error
    call = recorder.calls[0]
    assert call["agent"] == "codex-sparks" and call["session"] == "run-42" and call["auth"] == f"Bearer {TOKEN}"
    assert call["body"] == {"name": "note_add", "arguments": {"text": "hi"}, "caller": "codex-sparks"}


async def test_without_the_variables_nothing_extra_is_sent(recorder):
    await bridge(recorder).call("note_add", {"text": "hi"})
    call = recorder.calls[0]
    assert call["agent"] is None and call["session"] is None and call["body"] == {"name": "note_add", "arguments": {"text": "hi"}}


async def test_the_urllib_fallback_sends_it_too(recorder, monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", None)
    monkeypatch.setenv("HOARD_AGENT_ID", "cursor")
    monkeypatch.setenv("HOARD_AGENT_SESSION", "chat-7")
    await bridge(recorder).call("note_add", {})
    assert (recorder.calls[0]["agent"], recorder.calls[0]["session"]) == ("cursor", "chat-7")
