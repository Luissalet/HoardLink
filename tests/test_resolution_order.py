"""Integration checks that the four-step resolution order is respected
even when more than one source could answer."""

from __future__ import annotations

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from tests.conftest import Router, make_link

HEALTHY = httpx.Response(200, json={"status": "healthy"})

FAUSTUS_LLM_REGISTRY = httpx.Response(
    200,
    json={
        "items": [
            {
                "url": "http://127.0.0.1:8081/v1/chat/completions",
                "models": ["from-faustus"],
                "endpoint_id": "e1",
                "endpoint_name": "n1",
                "category": "local",
                "model_type": "llm",
                "backend": "llamacpp",
            }
        ]
    },
)


def llamacpp_router_with(router: Router) -> Router:
    router.get(
        8081,
        "/props",
        httpx.Response(200, json={"model_path": "/m/x.gguf", "default_generation_settings": {}, "modalities": []}),
    )
    router.get(8081, "/v1/models", httpx.Response(200, json={"data": [{"id": "from-loopback"}]}))
    return router


@pytest.mark.asyncio
async def test_explicit_config_beats_faustus_and_loopback():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", FAUSTUS_LLM_REGISTRY)
    llamacpp_router_with(router)

    cfg = LinkConfig(
        faustus_token="ody_test",
        capabilities={"llm": CapabilityConfig(url="http://127.0.0.1:9999/v1/chat/completions", model="from-explicit")},
    )
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.model == "from-explicit"
    assert "explicit configuration" in res.reason


@pytest.mark.asyncio
async def test_faustus_beats_loopback_when_both_available():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", FAUSTUS_LLM_REGISTRY)
    llamacpp_router_with(router)

    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.model == "from-faustus"
    assert "from Faustus registry" in res.reason


@pytest.mark.asyncio
async def test_loopback_used_when_faustus_has_no_matching_capability():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", httpx.Response(200, json={"items": []}))
    llamacpp_router_with(router)

    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.model == "from-loopback"
    assert res.details["source"] == "loopback"
