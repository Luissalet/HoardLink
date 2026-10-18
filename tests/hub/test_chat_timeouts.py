"""The normal private Hub routes forward the caller's remaining budget to TCP."""
import json
import threading
import time
from http.client import HTTPConnection

import pytest

from hoard_link import family
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.linkchat import LinkService
from hoard_link.hub.server import make_server
from tests.test_chat_timeouts import delayed_backend, tcp_link


@pytest.fixture
def private_hub(tmp_path, delayed_backend, monkeypatch):
    monkeypatch.setattr("hoard_link.link._reasoning.timeout_for", lambda *_: .3)
    config = HubConfig(port=0, data_dir=str(tmp_path / "hub"), roots=[], icon_dirs=[], jobs_enabled=False,
                       faustus_urls=[], link_chat_concurrency=1)
    hub = Hub(config)
    hub.link.close()
    hub.link = LinkService(config, link_factory=lambda _: tcp_link(delayed_backend))
    server = make_server(hub, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / "token"
    token.write_text(hub.token)
    family.configure("timeout846", hub=f"http://127.0.0.1:{server.server_port}", token_file=str(token))
    yield hub, server.server_port
    server.shutdown()
    server.server_close()
    hub.close()


def test_family_explicit_budget_reaches_real_backend(private_hub):
    result = family.chat([{"role": "user", "content": "normal"}], timeout=1)
    assert result["ok"] and result["text"] == "ok"


def test_family_stream_waits_for_initial_prefill_inside_total(private_hub):
    events = list(family.stream_chat([{"role": "user", "content": "normal"}], timeout=1, idle_timeout=.3))
    assert events[-1]["type"] == "finish" and events[-1]["text"] == "ok"
    assert private_hub[0].link.gate.active == 0


def test_agent_mcp_http_tool_honors_explicit_timeout(private_hub):
    hub, port = private_hub
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("POST", "/api/agent/call", json.dumps({"tool": "hub_link_chat", "arguments": {
            "prompt": "normal", "timeout_s": 1}}), headers={"Authorization": "Bearer " + hub.token})
        response = connection.getresponse()
        result = json.loads(response.read())["result"]
        assert response.status == 200 and result["ok"] and result["text"] == "ok"
    finally:
        connection.close()


def test_queue_time_is_not_added_back_to_generation_budget(private_hub, delayed_backend):
    hub, _ = private_hub
    delayed_backend["delay"] = .8
    hub.link._link("hub")  # Client construction is not the queue delay under test.
    assert hub.link.gate.acquire(1)
    timer = threading.Timer(.35, hub.link.gate.release)
    timer.start()
    started = time.monotonic()
    try:
        result = family.chat([{"role": "user", "content": "normal"}], timeout=1)
    finally:
        timer.join()
    assert not result["ok"] and result["error"] == "timeout"
    # Neither the short backend default nor a fresh post-queue budget wins.
    assert .9 <= time.monotonic() - started < 1.3
    assert len(delayed_backend["requests"]) == 1
    assert delayed_backend["closed"].wait(1)
    assert hub.link.gate.active == 0
