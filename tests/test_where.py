"""hoard_link.where: a Hoard on the PC finds Faustus (and Hermes, Prometheus) where the Hub placed it.

The real copilot transports are driven against two small servers: a Hub that answers ``/api/placement`` and a Faustus
that answers the copilot capabilities, while the configured address (127.0.0.1:7000 in production) refuses connections.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hoard_link import where
from hoard_link.config import LinkConfig

REAL_FETCH = where.fetch            # the conftest replaces where.fetch for every test; these tests need the real one

SPARKS = {"location": "sparks", "job": {"running": False},
          "urls": {"faustus": "https://spark3.example.ts.net/", "hermes": "https://spark3.example.ts.net:5221/products/"}}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Servers:
    """A Hub and a Faustus on loopback ports; ``hub_doc`` is what the Hub says right now."""

    def __init__(self):
        outer = self
        self.hub_doc: dict = {}
        self.hub_hits = 0
        self.faustus_hits = 0

        class Hub(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.hub_hits += 1
                body = json.dumps(outer.hub_doc).encode()
                self.send_response(200 if self.path == "/api/placement" else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        class Faustus(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.faustus_hits += 1
                ok = self.path == "/api/hoard-copilot/capabilities"
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"contract_version":"1.0"}' if ok else b"{}")

        self.hub = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
        self.faustus = ThreadingHTTPServer(("127.0.0.1", 0), Faustus)
        for server in (self.hub, self.faustus):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.hub_url = f"http://127.0.0.1:{self.hub.server_port}"
        self.faustus_url = f"http://127.0.0.1:{self.faustus.server_port}"
        self.dead = f"http://127.0.0.1:{free_port()}"

    def place_on_spark(self):
        self.hub_doc = {**SPARKS, "urls": {**SPARKS["urls"], "faustus": self.faustus_url}}

    def close(self):
        for server in (self.hub, self.faustus):
            server.shutdown()
            server.server_close()


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setattr(where, "fetch", REAL_FETCH)
    where.forget()
    found = Servers()
    yield found
    found.close()
    where.forget()


def test_placed_only_when_the_hub_says_sparks_and_nothing_is_moving():
    assert where.placed("faustus", SPARKS) == "https://spark3.example.ts.net"
    assert where.placed("hermes", SPARKS) == "https://spark3.example.ts.net:5221/products"
    assert where.placed("prometheus", SPARKS) == ""
    assert where.placed("faustus", {**SPARKS, "location": "pc"}) == ""
    assert where.placed("faustus", {**SPARKS, "job": {"running": True}}) == ""
    assert where.placed("faustus", {"location": "sparks", "urls": {"faustus": "not a url"}}) == ""
    assert where.placed("faustus", {}) == ""


def test_the_configured_addresses_come_first_and_the_placed_one_follows():
    configured = ["http://127.0.0.1:7000/", "http://127.0.0.1:7001"]
    assert where.faustus_urls(configured, SPARKS) == ["http://127.0.0.1:7000", "http://127.0.0.1:7001", "https://spark3.example.ts.net"]
    assert where.faustus_urls(configured, {}) == ["http://127.0.0.1:7000", "http://127.0.0.1:7001"]
    assert where.faustus_urls(["https://spark3.example.ts.net"], SPARKS) == ["https://spark3.example.ts.net"]
    assert where.app_url("hermes", "http://127.0.0.1:5221", hub=None) == "http://127.0.0.1:5221"


def test_the_answer_is_cached_and_a_silent_hub_is_not_asked_again_at_once():
    calls = []

    def fetcher():
        calls.append(1)
        return SPARKS if len(calls) == 1 else {}

    where.forget()
    assert where.current("http://hub.test", fetcher=fetcher, now=100.0) == SPARKS
    assert where.current("http://hub.test", fetcher=fetcher, now=110.0) == SPARKS
    assert len(calls) == 1
    assert where.current("http://hub.test", fetcher=fetcher, now=130.0) == {}          # expired: asked again, silent
    assert where.current("http://hub.test", fetcher=fetcher, now=135.0) == {}          # silent hub: not asked for 10 s
    assert len(calls) == 2
    where.forget()


def test_a_hub_that_does_not_answer_gives_an_empty_document(servers):
    assert where.fetch(servers.dead, timeout=0.5) == {}


def test_link_config_adds_the_placed_address_after_the_configured_ones(servers):
    servers.place_on_spark()
    config = LinkConfig(faustus_urls=(servers.dead,), hub_url=servers.hub_url)
    assert config.faustus_candidates() == [servers.dead, servers.faustus_url]
    servers.hub_doc = {"location": "pc"}
    where.forget()
    assert config.faustus_candidates() == [servers.dead]


def test_stdlib_relay_finds_faustus_on_the_spark_and_follows_it_back(servers, tmp_path, monkeypatch):
    from hoard_link.copilot_stdlib import CopilotRelay
    monkeypatch.setenv("HOARD_HUB_URL", servers.hub_url)
    servers.place_on_spark()
    relay = CopilotRelay("disk", tmp_path, faustus_urls=[servers.dead], token="", env={})
    assert relay._locate() == servers.faustus_url
    hits = servers.faustus_hits
    assert relay._locate() == servers.faustus_url and servers.faustus_hits == hits      # remembered for a few seconds
    # Faustus goes back to the PC: the choice is dropped after the time limit and the configured address wins again.
    servers.hub_doc = {"location": "pc"}
    where.forget()
    relay._until = 0
    with pytest.raises(ConnectionError):
        relay._locate()


def test_stdlib_relay_without_a_hub_keeps_the_configured_behaviour(servers, tmp_path, monkeypatch):
    from hoard_link.copilot_stdlib import CopilotRelay
    monkeypatch.setenv("HOARD_HUB_URL", servers.dead)
    relay = CopilotRelay("disk", tmp_path, faustus_urls=[servers.faustus_url], token="", env={})
    assert relay._locate() == servers.faustus_url
    unreachable = CopilotRelay("other", tmp_path / "x", faustus_urls=[servers.dead], token="", env={})
    with pytest.raises(ConnectionError):
        unreachable._locate()


def test_fastapi_relay_finds_faustus_on_the_spark(servers, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hoard_link.copilot import install_copilot
    monkeypatch.setenv("HOARD_HUB_URL", servers.hub_url)
    servers.place_on_spark()
    app = FastAPI()
    install_copilot(app, "qa", tmp_path, faustus_urls=[servers.dead], token="")
    with TestClient(app) as client:
        reply = client.get("/api/copilot/info")
        assert reply.status_code == 200
        assert reply.json()["settings_url"] == servers.faustus_url + "/studio/settings"


def test_hub_proxy_uses_the_hubs_own_placement_without_calling_itself(servers):
    from hoard_link.hub.copilot import CopilotProxy
    servers.place_on_spark()
    state = {"document": servers.hub_doc}
    proxy = CopilotProxy([servers.dead], placement=lambda: state["document"])
    assert proxy.resolve() == servers.faustus_url
    assert servers.hub_hits == 0                                                         # it read its own state
    plain = CopilotProxy([servers.dead])
    with pytest.raises(ConnectionError):
        plain.resolve()


def test_faustus_origin_is_the_first_healthy_candidate_and_falls_back_to_the_configured_one(servers):
    class Health(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","service":"faustus"}' if self.path == "/api/health" else b"{}")

    health = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    try:
        servers.hub_doc = {**SPARKS, "urls": {"faustus": f"http://127.0.0.1:{health.server_port}"}}
        config = LinkConfig(faustus_urls=(servers.dead,), hub_url=servers.hub_url)
        assert config.faustus_origin() == f"http://127.0.0.1:{health.server_port}"
        # Back on the PC with nothing running: the old answer (the configured address) comes back, not an empty string.
        servers.hub_doc = {"location": "pc"}
        where.forget()
        assert config.faustus_origin() == servers.dead
    finally:
        health.shutdown()
        health.server_close()
