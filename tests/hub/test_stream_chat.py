"""Streaming clients through the real private Hub HTTP route, never a live model."""
import json
import shutil
import subprocess
import http.client
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from hoard_link import family
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.linkchat import LinkService
from hoard_link.hub.server import make_server
from tests.test_stream_chat import upstream, real_link


def wait_until(check, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    assert check()


@pytest.fixture
def streaming_hub(tmp_path, upstream):
    cfg = HubConfig(port=0, data_dir=str(tmp_path / "data"), roots=[], icon_dirs=[], jobs_enabled=False,
                    faustus_urls=[], link_chat_concurrency=1)
    hub = Hub(cfg)
    hub.link.close()
    hub.link = LinkService(cfg, link_factory=lambda _: real_link(upstream))
    server = make_server(hub, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token_file = tmp_path / "token"
    token_file.write_text(hub.token)
    url = f"http://127.0.0.1:{server.server_port}"
    family.configure("test", token_file=str(token_file), hub=url)
    yield hub, url, token_file
    upstream["release"].set()
    server.shutdown()
    server.server_close()
    hub.close()


def msgs(mode="normal"):
    return [{"role": "user", "content": mode}]


def test_python_sdk_complete_finish_and_monotonic_events(streaming_hub):
    events = list(family.stream_chat(msgs("length")))
    final = events[-1]
    assert final["type"] == "finish" and final["text"] == "á fin"
    assert final["completed"] is False and final["finish_reason"] == "length"
    assert final["usage"]["completion_tokens"] == 4
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert len({e["request_id"] for e in events}) == 1


@pytest.mark.parametrize("headers,body,status", [
    ({}, {"stream": True, "messages": msgs()}, 401),
    ({"auth": True}, {"stream": True, "messages": []}, 400),
    ({"auth": True}, {"stream": True, "messages": msgs(), "idle_timeout_s": -1}, 400),
])
def test_auth_and_validation_precede_sse(streaming_hub, headers, body, status):
    hub, url, _ = streaming_hub
    connection = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=2)
    try:
        headers = {"Authorization": "Bearer " + hub.token} if headers else {}
        connection.request("POST", "/api/link/chat", json.dumps(body), headers=headers)
        response = connection.getresponse()
        assert response.status == status and "application/json" in response.getheader("Content-Type")
        assert not json.loads(response.read())["ok"]
    finally:
        connection.close()


def test_existing_agent_tool_exposes_length_without_marking_complete(streaming_hub):
    hub, url, _ = streaming_hub
    connection = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=3)
    try:
        connection.request("POST", "/api/agent/call", json.dumps({"tool": "hub_link_chat", "arguments": {"prompt": "x"}}),
                           headers={"Authorization": "Bearer " + hub.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200 and payload["result"]["finish_reason"] == "length"
        assert payload["result"]["completed"] is False and payload["result"]["text"] == "partial"
    finally:
        connection.close()


def test_python_partial_failure_is_typed_and_not_retried(streaming_hub, upstream):
    events = list(family.stream_chat(msgs("partial")))
    assert events[-1]["type"] == "error" and events[-1]["partial"] is True
    assert events[-1]["text"] == "á"
    assert len(upstream["requests"]) == 1
    assert streaming_hub[0].link.gate.active == 0


def test_tool_only_partial_retains_arguments_and_marks_partial(streaming_hub, upstream):
    tools = [{"type": "function", "function": {"name": "do", "parameters": {"type": "object"}}}]
    events = list(family.stream_chat(msgs("toolpartial"), tools=tools, tool_choice="required"))
    error = events[-1]
    assert error["type"] == "error" and error["partial"] and error["tool_call_deltas"][0]["function"]["arguments"] == "{"
    assert upstream["requests"][0]["tools"] == tools and upstream["requests"][0]["tool_choice"] == "required"


def test_client_close_cancels_upstream_and_releases_queue(streaming_hub, upstream):
    hub, *_ = streaming_hub
    with closing(family.stream_chat(msgs("hold"))) as stream:
        for event in stream:
            if event["type"] == "text":
                break
        assert hub.link.gate.active == 1
    assert upstream["closed"].wait(2)
    wait_until(lambda: hub.link.gate.active == 0)
    assert list(family.stream_chat(msgs()))[-1]["type"] == "finish"


def test_cancel_while_queued_never_starts_an_upstream_call(streaming_hub, upstream):
    hub, *_ = streaming_hub
    held = family.stream_chat(msgs("hold"))
    try:
        for event in held:
            if event["type"] == "text":
                break
        cancel = threading.Event()
        events = []
        worker = threading.Thread(target=lambda: events.extend(family.stream_chat(msgs(), cancel=cancel)))
        worker.start()
        wait_until(lambda: hub.link.gate.waiting == 1)
        cancel.set()
        worker.join(2)
        assert not worker.is_alive()
        wait_until(lambda: hub.link.gate.waiting == 0)
        assert len(upstream["requests"]) == 1
        assert events[0]["state"] == "queued" and events[-1]["error"] == "cancelled"
    finally:
        held.close()
    wait_until(lambda: hub.link.gate.active == 0)


def test_streaming_and_existing_blocking_calls_share_fifo_order(streaming_hub, upstream):
    hub, *_ = streaming_hub
    held = family.stream_chat(msgs("hold"))
    blocking, streamed = [], []
    try:
        for event in held:
            if event["type"] == "text":
                break
        first = threading.Thread(target=lambda: blocking.append(hub.link.chat("blocking", {"messages": msgs("block")})))
        first.start()
        wait_until(lambda: hub.link.gate.waiting == 1)
        second = threading.Thread(target=lambda: streamed.extend(family.stream_chat(msgs("normal"))))
        second.start()
        wait_until(lambda: hub.link.gate.waiting == 2)
    finally:
        held.close()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert blocking[0][0] == 200 and blocking[0][1]["completed"] is False
    assert streamed[-1]["type"] == "finish"
    assert [r["messages"][-1]["content"] for r in upstream["requests"]] == ["hold", "block", "normal"]
    wait_until(lambda: hub.link.gate.active == 0 and hub.link.gate.waiting == 0)


def test_heartbeats_are_identified_and_do_not_hide_upstream_idle_timeout(streaming_hub, upstream):
    hub, *_ = streaming_hub
    events = list(hub.link.stream_chat("test", {"messages": msgs("idle"), "idle_timeout_s": 0.15}, heartbeat_s=0.02))
    assert any(e["type"] == "heartbeat" and e["upstream_activity"] is False for e in events)
    assert events[-1]["error"] == "timeout" and events[-1]["timeout_kind"] == "inactivity"
    assert events[-1]["partial"] and hub.link.gate.active == 0


def test_queue_total_deadline_and_ticket_cleanup(streaming_hub):
    hub, *_ = streaming_hub
    assert hub.link.gate.acquire(1)
    try:
        events = list(hub.link.stream_chat("test", {"messages": msgs(), "timeout_s": 1}, heartbeat_s=0.1))
        assert events[0]["state"] == "queued"
        assert events[-1]["error"] == "timeout" and events[-1]["timeout_kind"] == "total"
        assert hub.link.gate.waiting == 0 and hub.link.gate.active == 1
    finally:
        hub.link.gate.release()


@pytest.mark.parametrize("pause_state", ["queued", "starting"])
def test_expired_request_never_enters_backend_after_consumer_pause(tmp_path, pause_state):
    calls = []

    class FastModel:
        async def stream_chat(self, *_args, **_kwargs):
            calls.append(True)
            yield {"type": "finish", "ok": True, "text": "late", "completed": True}

    service = LinkService(HubConfig(data_dir=str(tmp_path), link_chat_concurrency=1), link_factory=lambda _: FastModel())
    if pause_state == "queued":
        assert service.gate.acquire(1)
    stream = service.stream_chat("expired", {"messages": msgs(), "timeout_s": 1})
    try:
        assert next(stream)["state"] == pause_state
        time.sleep(1.1)
        if pause_state == "queued":
            service.gate.release()
        events = list(stream)
        assert not calls
        assert events[-1]["type"] == "error" and events[-1]["error"] == "timeout"
        assert events[-1]["timeout_kind"] == "total"
        assert not any(e["type"] == "finish" for e in events)
        assert service.gate.active == 0 and service.gate.waiting == 0
    finally:
        stream.close()
        service.close()


def test_timeout_frees_model_slot_even_when_consumer_stops_reading(tmp_path):
    closed = threading.Event()

    class FastModel:
        async def stream_chat(self, *_args, **_kwargs):
            try:
                for _ in range(200):
                    yield {"type": "text", "delta": "x"}
            finally:
                closed.set()

    service = LinkService(HubConfig(data_dir=str(tmp_path), link_chat_concurrency=1), link_factory=lambda _: FastModel())
    stream = service.stream_chat("pressure", {"messages": msgs(), "timeout_s": 1})
    try:
        assert next(stream)["state"] == "starting"
        assert next(stream)["type"] == "text"
        # More events than the delivery queue fits: leave the consumer paused.
        assert service.gate.active == 1
        wait_until(lambda: closed.is_set() and service.gate.active == 0, timeout=2)
    finally:
        stream.close()
        service.close()


def test_js_sdk_real_http_and_abort(streaming_hub):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node not installed")
    hub, url, token = streaming_hub
    module = (Path(__file__).resolve().parents[2] / "js" / "hoard-link.js").as_uri()
    code = f'''
      import * as family from {json.dumps(module)};
      family.configure({{app:"test",hub:{json.dumps(url)},tokenFile:{json.dumps(str(token))}}});
      const events=[];
      for await (const e of family.streamChat({{messages:[{{role:"user",content:"length"}}]}})) events.push(e);
      const ctl=new AbortController(), cancelled=[];
      for await (const e of family.streamChat({{messages:[{{role:"user",content:"hold"}}],signal:ctl.signal}})) {{
        cancelled.push(e); if(e.type==="text") ctl.abort();
      }}
      console.log(JSON.stringify({{events,cancelled}}));
    '''
    result = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["events"][-1]["type"] == "finish" and data["events"][-1]["completed"] is False
    assert data["cancelled"][-1]["error"] == "cancelled" and data["cancelled"][-1]["partial"]
    wait_until(lambda: hub.link.gate.active == 0)
