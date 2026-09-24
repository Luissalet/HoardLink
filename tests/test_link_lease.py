"""Link takes a GPU lease only around a call that makes a server LOAD a
model; a fake hub lives on port 8810 of the MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

import importlib

# ``hoard_link.lease`` the attribute is the function; the module is in sys.modules.
lease_mod = importlib.import_module("hoard_link.lease")
from hoard_link.config import CapabilityConfig, LinkConfig
from hoard_link.errors import Unavailable
from tests.conftest import Router, make_link

GIB = 1024 * 1024 * 1024


class FakeHub:
    """Grants (or queues) every request and records what it was asked."""

    def __init__(self, state: str = "granted"):
        self.state = state
        self.requests: list[dict] = []
        self.released: list[str] = []

    def install(self, router: Router) -> Router:
        router.get(8810, "/api/health", httpx.Response(200, json={"ok": True, "service": "hoard-hub"}))
        router.post(8810, "/api/lease/request", self._request)
        router.post(8810, "/api/lease/release", self._release)
        router.post(8810, "/api/lease/renew", lambda r: httpx.Response(200, json={"ok": True, "lease_id": "L1"}))
        return router

    def _request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        return httpx.Response(200, json={"ok": True, "lease_id": "L1", "state": self.state,
                                         "gpu": 1 if self.state == "granted" else None, "position": 0 if self.state == "granted" else 3,
                                         "expires_at": 0})

    def _release(self, request: httpx.Request) -> httpx.Response:
        self.released.append(json.loads(request.content)["lease_id"])
        return httpx.Response(200, json={"ok": True, "released": True})


def ollama(router: Router, resident: list[str], tags: list[dict]) -> Router:
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": [{"model": m} for m in resident]}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": tags}))
    router.post(11434, "/api/show", httpx.Response(200, json={"capabilities": ["completion", "embedding"]}))
    router.post(11434, "/api/chat", httpx.Response(200, json={"message": {"content": "hola"}}))
    router.post(11434, "/api/embed", httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}))
    return router


LOAD_CFG = LinkConfig(app="scribe", capabilities={"llm": CapabilityConfig(allow_load=True),
                                                  "embeddings": CapabilityConfig(allow_load=True)})


@pytest.mark.asyncio
async def test_chat_that_loads_takes_and_releases_a_lease():
    hub = FakeHub()
    router = ollama(hub.install(Router()), [], [{"name": "gemma3:27b", "size": 17 * GIB}])
    link = make_link(router, config=LOAD_CFG)
    res = await link.chat([{"role": "user", "content": "hi"}])
    assert res.text == "hola"
    assert len(hub.requests) == 1 and hub.released == ["L1"]
    req = hub.requests[0]
    assert req["owner"] == "scribe" and "gemma3:27b" in req["purpose"] and req["gpu"] == "any"
    assert req["vram_mb"] == int(17 * 1024 * 1.2) + 512


@pytest.mark.asyncio
async def test_embed_that_loads_uses_the_configured_vram():
    hub = FakeHub()
    cfg = LinkConfig(app="borges", capabilities={"embeddings": CapabilityConfig(allow_load=True, vram_mb=1234)})
    router = ollama(hub.install(Router()), [], [{"name": "nomic-embed-text", "size": GIB}])
    link = make_link(router, config=cfg)
    assert await link.embed(["a"]) == [[0.1, 0.2]]
    assert hub.requests[0]["vram_mb"] == 1234 and hub.released == ["L1"]


@pytest.mark.asyncio
async def test_resident_model_needs_no_lease():
    hub = FakeHub()
    router = ollama(hub.install(Router()), ["gemma3:27b"], [{"name": "gemma3:27b", "size": 17 * GIB}])
    link = make_link(router, config=LOAD_CFG)
    await link.chat([{"role": "user", "content": "hi"}])
    assert hub.requests == [] and hub.released == []


@pytest.mark.asyncio
async def test_lease_disabled_in_config():
    hub = FakeHub()
    cfg = LinkConfig(app="x", gpu_lease=False, capabilities={"llm": CapabilityConfig(allow_load=True)})
    router = ollama(hub.install(Router()), [], [{"name": "m", "size": GIB}])
    await make_link(router, config=cfg).chat([{"role": "user", "content": "hi"}])
    assert hub.requests == []


@pytest.mark.asyncio
async def test_no_hub_falls_back_and_the_call_still_works(monkeypatch, caplog):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", lambda: [])
    router = ollama(Router(), [], [{"name": "m", "size": GIB}])   # nothing on 8810
    link = make_link(router, config=LOAD_CFG)
    with caplog.at_level("WARNING", logger="hoard_link.lease"):
        res = await link.chat([{"role": "user", "content": "hi"}])
    assert res.text == "hola"


@pytest.mark.asyncio
async def test_queued_past_timeout_is_unavailable():
    hub = FakeHub(state="queued")
    cfg = LinkConfig(app="x", lease_timeout_s=0, capabilities={"llm": CapabilityConfig(allow_load=True)})
    router = ollama(hub.install(Router()), [], [{"name": "m", "size": GIB}])
    with pytest.raises(Unavailable) as err:
        await make_link(router, config=cfg).chat([{"role": "user", "content": "hi"}])
    assert "GPU busy" in str(err.value) and hub.released == ["L1"]


@pytest.mark.asyncio
async def test_hub_refusal_loads_without_a_lease():
    router = ollama(Router(), [], [{"name": "m", "size": GIB}])
    router.get(8810, "/api/health", httpx.Response(200, json={"service": "hoard-hub"}))
    router.post(8810, "/api/lease/request", httpx.Response(400, json={"ok": False, "error": "can never fit"}))
    res = await make_link(router, config=LOAD_CFG).chat([{"role": "user", "content": "hi"}])
    assert res.text == "hola"


def test_config_file_and_env(tmp_path):
    p = tmp_path / "backend.json"
    p.write_text(json.dumps({"gpu_lease": {"enabled": True, "hub_url": "http://127.0.0.1:9999/", "timeout_s": 12,
                                           "vram_mb": 4000},
                             "capabilities": {"vision": {"vram_mb": 9000}}}))
    cfg = LinkConfig.load(p, env={})
    assert cfg.gpu_lease and cfg.hub_url == "http://127.0.0.1:9999" and cfg.lease_timeout_s == 12
    assert cfg.lease_vram_mb == 4000 and cfg.capability("vision").vram_mb == 9000
    cfg = LinkConfig.load(p, env={"HOARD_GPU_LEASE": "0", "HOARD_VISION_URL": "http://x"})
    assert cfg.gpu_lease is False and cfg.capability("vision").vram_mb == 9000
    assert LinkConfig.load(None, env={}).gpu_lease is True
