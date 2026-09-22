from __future__ import annotations

import httpx
import pytest

from hoard_link.config import LinkConfig
from tests.conftest import FakeClock, Router, make_link


def props():
    return httpx.Response(
        200, json={"model_path": "/models/m.gguf", "default_generation_settings": {"n_ctx": 4096}, "modalities": []}
    )


def v1_models():
    return httpx.Response(200, json={"data": [{"id": "m"}]})


@pytest.mark.asyncio
async def test_wait_idle_becomes_true_once_slot_frees_up():
    calls = {"n": 0}

    def slots(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        busy = calls["n"] <= 2
        return httpx.Response(200, json=[{"id": 0, "is_processing": busy}])

    router = Router()
    router.get(8081, "/props", props())
    router.get(8081, "/v1/models", v1_models())
    router.get(8081, "/slots", slots)

    clock = FakeClock()
    link = make_link(router, config=LinkConfig(), fake_clock=clock)

    result = await link.wait_idle("llm", max_wait_s=30)
    assert result is True
    assert calls["n"] >= 3
    assert clock.sleeps  # it did poll with a sleep in between


@pytest.mark.asyncio
async def test_wait_idle_returns_false_after_timeout_when_always_busy():
    router = Router()
    router.get(8081, "/props", props())
    router.get(8081, "/v1/models", v1_models())
    router.get(8081, "/slots", httpx.Response(200, json=[{"id": 0, "is_processing": True}]))

    clock = FakeClock()
    link = make_link(router, config=LinkConfig(), fake_clock=clock)

    result = await link.wait_idle("llm", max_wait_s=5)
    assert result is False


@pytest.mark.asyncio
async def test_wait_idle_true_for_ollama_no_busy_signal():
    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": [{"model": "llama3.1:8b"}]}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": []}))
    router.post(11434, "/api/show", httpx.Response(200, json={"capabilities": []}))

    clock = FakeClock()
    link = make_link(router, config=LinkConfig(), fake_clock=clock)

    result = await link.wait_idle("llm", max_wait_s=30)
    assert result is True
    assert clock.sleeps == []  # never had to poll


@pytest.mark.asyncio
async def test_wait_idle_true_when_capability_unavailable():
    router = Router()
    clock = FakeClock()
    link = make_link(router, config=LinkConfig(), fake_clock=clock)

    result = await link.wait_idle("music", max_wait_s=5)
    assert result is True
