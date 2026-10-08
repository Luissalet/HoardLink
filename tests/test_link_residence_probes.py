"""Optional hardenings from Agora #16 / task #27: health readiness and short base-probe cache."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from hoard_link.config import LinkConfig
from tests.conftest import FakeClock, Router, llama_health, make_link


def _registry(url: str = "http://127.0.0.1:8081/v1/chat/completions", model: str = "model.gguf"):
    return {
        "items": [{
            "url": url,
            "models": [model],
            "endpoint_id": "ep",
            "category": "local",
            "model_type": "llm",
            "backend": "llamacpp",
        }]
    }


def _ready_props(model: str = "model.gguf") -> dict:
    return {"model_path": f"C:/models/{model}", "default_generation_settings": {}}


@pytest.mark.asyncio
async def test_health_503_with_props_200_is_not_resident():
    router = Router()
    router.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    router.get(7000, "/api/models", httpx.Response(200, json=_registry()))
    router.get(8081, "/health", httpx.Response(503, json={"error": "loading"}))
    router.get(8081, "/props", httpx.Response(200, json=_ready_props()))
    router.get(8081, "/v1/models", httpx.Response(200, json={"data": [{"id": "model.gguf"}]}))
    router.get(8081, "/slots", httpx.Response(200, json=[]))

    res = await make_link(router, config=LinkConfig(faustus_token="ody_test")).resolve("llm")
    assert res.state == "unavailable"
    assert any("offline, not ready" in reason for reason in res.details["reasons"])


@pytest.mark.asyncio
async def test_base_probe_is_single_flight_and_cached_briefly_per_url():
    hits = {"health": 0, "props": 0}

    def health(request: httpx.Request) -> httpx.Response:
        hits["health"] += 1
        return httpx.Response(200, json={"status": "ok"})

    def props(request: httpx.Request) -> httpx.Response:
        hits["props"] += 1
        return httpx.Response(200, json=_ready_props())

    router = Router()
    router.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    router.get(7000, "/api/models", httpx.Response(200, json=_registry()))
    router.get(8081, "/health", health)
    router.get(8081, "/props", props)
    router.get(8081, "/v1/models", httpx.Response(200, json={"data": [{"id": "model.gguf"}]}))
    router.get(8081, "/slots", httpx.Response(200, json=[]))

    clock = FakeClock()
    link = make_link(router, config=LinkConfig(faustus_token="ody_test"), fake_clock=clock)

    first = await asyncio.gather(*(link.resolve("llm") for _ in range(5)))
    assert all(r.state == "resolved" for r in first)
    assert hits["health"] == 1 and hits["props"] == 1

    await link.resolve("llm")
    assert hits["health"] == 1 and hits["props"] == 1

    clock.t += 2.6
    await link.resolve("llm")
    assert hits["health"] == 2 and hits["props"] == 2


@pytest.mark.asyncio
async def test_ready_health_still_requires_llamacpp_props_signature():
    router = Router()
    router.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    router.get(7000, "/api/models", httpx.Response(200, json=_registry()))
    llama_health(8081, router=router)
    router.get(8081, "/props", httpx.Response(200, json={"status": "ok"}))
    router.get(8081, "/v1/models", httpx.Response(200, json={"data": [{"id": "model.gguf"}]}))

    res = await make_link(router, config=LinkConfig(faustus_token="ody_test")).resolve("llm")
    assert res.state == "unavailable"
    assert any("did not identify itself as llama.cpp" in reason for reason in res.details["reasons"])


@pytest.mark.asyncio
async def test_base_probe_single_flight_survives_ttl_while_in_flight(monkeypatch):
    from hoard_link import _probes

    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_probe(client, base):
        calls["n"] += 1
        started.set()
        await release.wait()
        return {"url": base, "n": calls["n"]}

    monkeypatch.setattr(_probes, "probe_llamacpp_base", slow_probe)
    clock = FakeClock()
    link = make_link(Router(), fake_clock=clock)

    first = asyncio.create_task(link._probe_llamacpp_base("http://127.0.0.1:8081/"))
    await started.wait()
    clock.t += 2.6
    second = asyncio.create_task(link._probe_llamacpp_base("http://127.0.0.1:8081"))
    third = asyncio.create_task(link._probe_llamacpp_base("http://127.0.0.1:8081"))
    release.set()
    a, b, c = await asyncio.gather(first, second, third)
    assert calls["n"] == 1
    assert a == b == c == {"url": "http://127.0.0.1:8081", "n": 1}


@pytest.mark.asyncio
async def test_base_probe_cancelled_waiter_does_not_cancel_shared_flight(monkeypatch):
    from hoard_link import _probes

    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_probe(client, base):
        calls["n"] += 1
        started.set()
        await release.wait()
        return {"url": base}

    monkeypatch.setattr(_probes, "probe_llamacpp_base", slow_probe)
    link = make_link(Router())

    first = asyncio.create_task(link._probe_llamacpp_base("http://127.0.0.1:8081"))
    await started.wait()
    second = asyncio.create_task(link._probe_llamacpp_base("http://127.0.0.1:8081"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == {"url": "http://127.0.0.1:8081"}
    assert calls["n"] == 1
