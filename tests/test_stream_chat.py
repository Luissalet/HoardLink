"""Real HTTP chunks: incremental delivery, terminal semantics and upstream close."""
import asyncio
import json
import select
import socket
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hoard_link import Link, LinkConfig
from hoard_link.errors import BackendError
from hoard_link.types import Resolution
from tests.conftest import make_link
from tests.test_chat import openai_config


@pytest.fixture
def upstream():
    state = {"requests": [], "closed": threading.Event(), "release": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            mode = body["messages"][-1]["content"]
            if not body.get("stream"):
                raw = json.dumps({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def send(data):
                raw = ("data: " + (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)) + "\r\n\r\n").encode()
                # Splitting UTF-8 and SSE fields across actual TCP writes must be harmless.
                cuts = sorted({0, 8, 13, len(raw), raw.find("á".encode()) + 1 if "á".encode() in raw else 13})
                for lo, hi in zip(cuts, cuts[1:]):
                    part = raw[lo:hi]
                    self.wfile.write(part)
                    self.wfile.flush()

            try:
                if self.path == "/api/chat":
                    self.wfile.write((json.dumps({"message": {"content": "hola", "thinking": "idea"}}) + "\n").encode())
                    self.wfile.write((json.dumps({"message": {}, "done": True, "done_reason": "length", "eval_count": 2}) + "\n").encode())
                    self.wfile.flush()
                    return
                if mode in ("empty-json", "empty-usage", "tool-activity", "usage-activity"):
                    for _ in range(10):
                        chunk = {"choices": [{"delta": {}}]}
                        if mode == "tool-activity":
                            chunk["choices"][0]["delta"] = {"tool_calls": [{"index": 0, "function": {"arguments": " "}}]}
                        elif mode == "usage-activity":
                            chunk["usage"] = {"completion_tokens": 1}
                        elif mode == "empty-usage":
                            chunk["usage"] = {}
                        send(chunk)
                        time.sleep(0.025)
                elif mode == "reasoning":
                    for _ in range(8):
                        send({"choices": [{"delta": {"reasoning_content": "thinking "}}]})
                        time.sleep(0.035)
                elif mode == "toolpartial":
                    send({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call", "function": {"arguments": "{"}}]}}]})
                    return
                else:
                    send({"choices": [{"delta": {"content": "á"}}]})
                if mode == "keepalive":
                    for _ in range(16):
                        self.wfile.write(b": waiting\n\n")
                        self.wfile.flush()
                        time.sleep(0.025)
                if mode in ("hold", "idle", "total"):
                    end = time.monotonic() + 4
                    while time.monotonic() < end and not state["release"].is_set():
                        ready, _, _ = select.select([self.connection], [], [], 0.02)
                        if ready and not self.connection.recv(1, socket.MSG_PEEK):
                            state["closed"].set()
                            return
                if mode == "partial":
                    return
                send({"choices": [{"delta": {"reasoning_content": "idea"}}]})
                send({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "do", "arguments": "{\"x\":"}}]}}]})
                send({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}}]})
                send({"choices": [{"delta": {"content": " fin"}, "finish_reason": "length" if mode == "length" else "stop"}]})
                send({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})
                send("[DONE]")
            except (OSError, ConnectionError):
                state["closed"].set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    state["release"].set()
    server.shutdown()
    server.server_close()


def real_link(upstream, api="openai"):
    link = Link(LinkConfig.load(None, env={}, app="stream-test"))

    async def resolve(*_):
        return Resolution("llm", "test", upstream["url"], "test-model", api, "resolved", "test", {})

    @asynccontextmanager
    async def lease(_):
        yield

    link.resolve = resolve
    link._load_lease = lease
    return link


@pytest.mark.asyncio
async def test_real_chunks_arrive_before_finish_and_close_upstream(upstream):
    link = real_link(upstream)
    stream = link.stream_chat([{"role": "user", "content": "hold"}])
    try:
        assert (await anext(stream))["state"] == "resolving"
        assert (await anext(stream))["state"] == "waiting_backend"
        first = await asyncio.wait_for(anext(stream), 1)
        assert first["type"] == "text" and first["delta"] == "á"
        assert not upstream["release"].is_set()
    finally:
        await stream.aclose()
        await link.aclose()
    assert await asyncio.to_thread(upstream["closed"].wait, 1)
    assert len(upstream["requests"]) == 1


@pytest.mark.asyncio
async def test_fields_usage_tools_and_length_preserved(upstream):
    link = real_link(upstream)
    try:
        events = [e async for e in link.stream_chat([{"role": "user", "content": "length"}], effort="low")]
    finally:
        await link.aclose()
    assert [e["delta"] for e in events if e["type"] == "reasoning"] == ["idea"]
    assert len([e for e in events if e["type"] == "tool_call"]) == 2
    finish = events[-1]
    assert finish["type"] == "finish" and finish["finish_reason"] == "length" and finish["completed"] is False
    assert finish["text"] == "á fin" and finish["usage"]["total_tokens"] == 7
    assert finish["tool_calls"][0]["function"] == {"name": "do", "arguments": '{"x":1}'}
    assert upstream["requests"][0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_interrupted_stream_never_retried_or_completed(upstream):
    link = real_link(upstream)
    seen = []
    try:
        with pytest.raises(BackendError, match="without a finish"):
            async for item in link.stream_chat([{"role": "user", "content": "partial"}]):
                seen.append(item)
    finally:
        await link.aclose()
    assert any(e["type"] == "text" for e in seen) and not any(e["type"] == "finish" for e in seen)
    assert len(upstream["requests"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["idle", "total"])
async def test_inactivity_and_total_deadlines_are_independent(upstream, kind):
    link = real_link(upstream)
    try:
        with pytest.raises(TimeoutError, match="inactivity" if kind == "idle" else None):
            async for _ in link.stream_chat([{"role": "user", "content": kind}],
                    idle_timeout=0.1 if kind == "idle" else 10, timeout=10 if kind == "idle" else 0.15):
                pass
    finally:
        await link.aclose()
    assert await asyncio.to_thread(upstream["closed"].wait, 1)


@pytest.mark.asyncio
async def test_ollama_ndjson_real_stream(upstream):
    link = real_link(upstream, "ollama")
    try:
        events = [e async for e in link.stream_chat([{"role": "user", "content": "ollama"}], max_tokens=2)]
    finally:
        await link.aclose()
    assert events[-1]["finish_reason"] == "length" and events[-1]["completed"] is False
    assert events[-1]["text"] == "hola" and events[-1]["reasoning"] == "idea"
    assert upstream["requests"][0]["options"]["num_predict"] == 2


@pytest.mark.asyncio
async def test_reasoning_is_activity_without_visible_answer(upstream):
    link = real_link(upstream)
    try:
        events = [e async for e in link.stream_chat([{"role": "user", "content": "reasoning"}], idle_timeout=0.1)]
    finally:
        await link.aclose()
    assert events[-1]["type"] == "finish"
    assert len([e for e in events if e["type"] == "reasoning"]) == 9


@pytest.mark.asyncio
async def test_total_deadline_not_extended_by_continuous_reasoning(upstream):
    link = real_link(upstream)
    seen = []
    try:
        with pytest.raises(TimeoutError):
            async for event in link.stream_chat([{"role": "user", "content": "reasoning"}], idle_timeout=0.15, timeout=0.16):
                seen.append(event)
    finally:
        await link.aclose()
    assert any(e["type"] == "reasoning" for e in seen)
    assert not any(e["type"] == "finish" for e in seen)


@pytest.mark.asyncio
async def test_provider_comment_keepalives_are_not_generation_activity(upstream):
    link = real_link(upstream)
    try:
        with pytest.raises(TimeoutError, match="inactivity"):
            async for _ in link.stream_chat([{"role": "user", "content": "keepalive"}], idle_timeout=0.1):
                pass
    finally:
        await link.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty-json", "empty-usage"])
async def test_empty_provider_json_does_not_renew_inactivity(upstream, mode):
    link = real_link(upstream)
    try:
        with pytest.raises(TimeoutError, match="inactivity"):
            async for _ in link.stream_chat([{"role": "user", "content": mode}], idle_timeout=0.1):
                pass
    finally:
        await link.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tool-activity", "usage-activity"])
async def test_real_tool_or_usage_chunks_renew_inactivity(upstream, mode):
    link = real_link(upstream)
    try:
        events = [e async for e in link.stream_chat([{"role": "user", "content": mode}], idle_timeout=0.1)]
    finally:
        await link.aclose()
    assert events[-1]["type"] == "finish" and events[-1]["completed"]
    assert events[-1]["usage"]["total_tokens"] == 7
    assert len([e for e in events if e["type"] == ("tool_call" if mode == "tool-activity" else "usage")]) >= 10


@pytest.mark.asyncio
async def test_native_total_timeout_does_not_cancel_paused_consumers_work(upstream):
    link = real_link(upstream)
    stream = link.stream_chat([{"role": "user", "content": "hold"}], timeout=0.2)
    try:
        while (await anext(stream))["type"] != "text":
            pass
        # A generator must not leave a timeout attached to its caller's task.
        await asyncio.sleep(0.25)
        with pytest.raises(TimeoutError, match="total"):
            await anext(stream)
    finally:
        await stream.aclose()
        await link.aclose()
    assert await asyncio.to_thread(upstream["closed"].wait, 1)


@pytest.mark.asyncio
async def test_blocking_still_returns_result_and_exposes_truncation():
    link = make_link(lambda request: httpx.Response(200, json={"choices": [
        {"message": {"content": "partial"}, "finish_reason": "length"}]}), config=openai_config())
    try:
        result = await link.chat([{"role": "user", "content": "x"}])
    finally:
        await link.aclose()
    assert result.text == "partial" and result.finish_reason == "length" and result.completed is False
    assert result.to_dict()["completed"] is False
