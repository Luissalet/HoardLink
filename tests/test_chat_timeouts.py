"""Private TCP backend: caller budgets survive slow prefill and real idle gaps."""
import asyncio
import json
import select
import socket
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hoard_link import Link, LinkConfig
from hoard_link.errors import BackendError
from hoard_link.types import Resolution


@pytest.fixture
def delayed_backend():
    state = {"requests": [], "delay": .421, "closed": threading.Event(), "release": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def pause(self, duration):
            until = time.monotonic() + duration
            while time.monotonic() < until and not state["release"].is_set():
                ready, _, _ = select.select([self.connection], [], [], .005)
                if ready and not self.connection.recv(1, socket.MSG_PEEK):
                    state["closed"].set()
                    return False
            return True

        def send_json(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            mode = body["messages"][-1]["content"]
            try:
                if not body.get("stream"):
                    if not self.pause(state["delay"]):
                        return
                    if mode == "negotiate" and "reasoning_effort" in body:
                        self.send_json({"error": "unknown reasoning_effort"}, 400)
                    elif self.path == "/api/chat":
                        self.send_json({"message": {"content": "ok"}, "done": True, "done_reason": "stop"})
                    else:
                        self.send_json({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
                    return
                if mode == "late-headers" and not self.pause(state["delay"]):
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                def send(delta, finish=None):
                    if self.path == "/api/chat":
                        chunk = {"message": delta}
                        if finish:
                            chunk.update(done=True, done_reason=finish)
                        raw = json.dumps(chunk) + "\n"
                    else:
                        chunk = {"choices": [{"delta": delta}]}
                        if finish:
                            chunk["choices"][0]["finish_reason"] = finish
                        raw = "data: " + json.dumps(chunk) + "\n\n"
                    self.wfile.write(raw.encode())
                    self.wfile.flush()

                if mode == "real-then-empty":
                    send({"reasoning_content": "thinking"})
                if mode != "late-headers":
                    until = time.monotonic() + state["delay"]
                    while time.monotonic() < until:
                        send({"role": "assistant"})
                        send({})
                        if not self.pause(.025):
                            return
                if mode == "tool":
                    send({"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]})
                    if not self.pause(.05):
                        return
                    send({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]})
                else:
                    send({"reasoning_content": "thinking"})
                send({"content": "ok"}, "stop")
            except (OSError, ConnectionError):
                state["closed"].set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    state["release"].set()
    server.shutdown()
    server.server_close()


def tcp_link(backend, api="openai"):
    link = Link(LinkConfig.load(None, env={}, app="timeout846"))

    async def resolve(*_):
        return Resolution("llm", "private-test", backend["url"], "fixture", api, "resolved", "test", {})

    @asynccontextmanager
    async def lease(_):
        yield

    link.resolve = resolve
    link._load_lease = lease
    return link


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["openai", "ollama"])
async def test_explicit_blocking_budget_survives_short_default_read(delayed_backend, monkeypatch, api):
    monkeypatch.setattr("hoard_link.link._reasoning.timeout_for", lambda *_: .3)
    link = tcp_link(delayed_backend, api)
    try:
        result = await link.chat([{"role": "user", "content": "normal"}], timeout=.9)
        assert result.text == "ok" and result.finish_reason == "stop"
    finally:
        await link.aclose()
    assert len(delayed_backend["requests"]) == 1


@pytest.mark.asyncio
async def test_omitted_blocking_timeout_keeps_existing_default(delayed_backend, monkeypatch):
    monkeypatch.setattr("hoard_link.link._reasoning.timeout_for", lambda *_: .3)
    link = tcp_link(delayed_backend)
    try:
        with pytest.raises(BackendError, match="ReadTimeout"):
            await link.chat([{"role": "user", "content": "normal"}])
    finally:
        await link.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "late-headers", "tool"])
@pytest.mark.parametrize("api", ["openai", "ollama"])
async def test_first_real_stream_data_can_follow_long_prefill(delayed_backend, mode, api):
    link = tcp_link(delayed_backend, api)
    try:
        events = [e async for e in link.stream_chat([{"role": "user", "content": mode}], timeout=.9, idle_timeout=.3)]
    finally:
        await link.aclose()
    assert events[-1]["type"] == "finish" and events[-1]["text"] == "ok"
    assert len(delayed_backend["requests"]) == 1


@pytest.mark.asyncio
async def test_empty_frames_before_first_output_remain_bounded_by_total(delayed_backend):
    link = tcp_link(delayed_backend)
    try:
        with pytest.raises(TimeoutError, match="total"):
            async for _ in link.stream_chat([{"role": "user", "content": "normal"}], timeout=.22, idle_timeout=.07):
                pass
    finally:
        await link.aclose()
    assert delayed_backend["closed"].wait(1)


@pytest.mark.asyncio
async def test_empty_frames_after_real_reasoning_do_not_renew_idle(delayed_backend):
    link = tcp_link(delayed_backend)
    try:
        with pytest.raises(TimeoutError, match="inactivity"):
            async for _ in link.stream_chat([{"role": "user", "content": "real-then-empty"}], timeout=.9, idle_timeout=.1):
                pass
    finally:
        await link.aclose()
    assert delayed_backend["closed"].wait(1)


@pytest.mark.asyncio
async def test_cancel_during_prefill_closes_upstream_once(delayed_backend):
    link = tcp_link(delayed_backend)
    stream = link.stream_chat([{"role": "user", "content": "normal"}], timeout=.9, idle_timeout=.1)
    try:
        async def consume():
            return [e async for e in stream]
        task = asyncio.create_task(consume())
        while not delayed_backend["requests"]:
            await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(delayed_backend["closed"].wait, 1)
    finally:
        await stream.aclose()
        await link.aclose()
    assert len(delayed_backend["requests"]) == 1


@pytest.mark.asyncio
async def test_reasoning_negotiation_does_not_reset_explicit_budget(delayed_backend):
    delayed_backend["delay"] = .22
    link = tcp_link(delayed_backend)
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, BackendError)):
            await link.chat([{"role": "user", "content": "negotiate"}], effort="high", timeout=.35)
    finally:
        await link.aclose()
    assert time.monotonic() - started < .6
    assert len(delayed_backend["requests"]) == 2
