"""The blocking facade next to real async use, with a real HTTP server."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hoard_link import Link
from hoard_link.config import CapabilityConfig, LinkConfig


class _Chat(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: pooled connections get reused

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps({"choices": [{"message": {"content": "<think>t</think>hi"}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def chat_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Chat)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def cfg(url: str) -> LinkConfig:
    return LinkConfig(capabilities={"llm": CapabilityConfig(url=url, model="m")})


def test_sync_and_async_mixed_on_one_link_inside_a_running_loop(chat_server):
    link = Link(cfg(chat_server))  # owns its client: the realistic app setup

    async def app():
        assert (await link.chat([{"role": "user", "content": "a"}])).text == "hi"
        # A sync helper called from async code (e.g. a FastAPI handler).
        assert link.sync.chat([{"role": "user", "content": "b"}]).text == "hi"
        assert (await link.chat([{"role": "user", "content": "c"}])).text == "hi"
        await link.aclose()

    asyncio.run(app())


def test_sync_first_then_async(chat_server):
    link = Link(cfg(chat_server))
    assert link.sync.chat([{"role": "user", "content": "a"}]).text == "hi"

    async def app():
        assert (await link.chat([{"role": "user", "content": "b"}])).text == "hi"
        await link.aclose()

    asyncio.run(app())


def test_sync_from_several_threads(chat_server):
    link = Link(cfg(chat_server))
    out: list[str] = []
    threads = [
        threading.Thread(target=lambda: out.append(link.sync.chat([{"role": "user", "content": "x"}]).text))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    link.sync.close()
    assert out == ["hi"] * 4


def test_sync_call_from_its_own_loop_thread_raises_instead_of_deadlocking():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    link = Link(LinkConfig(), client=client)
    loop = link.sync._ensure_loop()
    box: dict = {}

    def inside():
        try:
            link.sync.resolve("llm")
        except RuntimeError as exc:
            box["err"] = str(exc)

    done = threading.Event()
    loop.call_soon_threadsafe(lambda: (inside(), done.set()))
    assert done.wait(5)
    assert "own event loop thread" in box["err"]
    link.sync.close()
