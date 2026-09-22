from __future__ import annotations

import httpx
import pytest

from hoard_link.config import LinkConfig
from tests.conftest import Router, make_link

HEALTHY = httpx.Response(200, json={"status": "healthy"})

LLM_REGISTRY = httpx.Response(
    200,
    json={
        "items": [
            {
                "url": "http://127.0.0.1:8081/v1/chat/completions",
                "models": ["qwen3.8-27b-q8-llamacpp"],
                "endpoint_id": "19e14b1c",
                "endpoint_name": "local-llm",
                "category": "local",
                "model_type": "llm",
                "backend": "llamacpp",
            }
        ]
    },
)

EMPTY_REGISTRY = httpx.Response(200, json={"items": []})


@pytest.mark.asyncio
async def test_faustus_registry_resolves_llm_with_token():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", LLM_REGISTRY)
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.provider == "llamacpp"
    assert res.url == "http://127.0.0.1:8081/v1/chat/completions"
    assert res.model == "qwen3.8-27b-q8-llamacpp"
    assert res.api == "openai"
    assert "from Faustus registry" in res.reason
    assert "resident" in res.reason
    assert res.details["endpoint_id"] == "19e14b1c"


@pytest.mark.asyncio
async def test_faustus_second_candidate_port_used_when_first_is_down():
    router = Router()
    router.get(7001, "/api/health", HEALTHY)
    router.get(7001, "/api/models", LLM_REGISTRY)
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.details["endpoint_id"] == "19e14b1c"


@pytest.mark.asyncio
async def test_faustus_token_rejected_records_reason_and_falls_through():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", httpx.Response(401, json={"error": "unauthorized"}))
    cfg = LinkConfig(faustus_token="ody_bad")
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.state == "unavailable"
    assert any("401/403" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_without_token_tries_unauthenticated_request():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", LLM_REGISTRY)
    cfg = LinkConfig(faustus_token=None)
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.provider == "llamacpp"


@pytest.mark.asyncio
async def test_faustus_reachable_but_no_matching_capability():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", EMPTY_REGISTRY)
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve("embeddings")
    assert res.state == "unavailable"
    assert any("no server for capability" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_tts_service_resolves_when_registry_has_no_tts_item():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", EMPTY_REGISTRY)
    router.get(7000, "/api/tts/capabilities", httpx.Response(200, json={"voices": ["es-ES"]}))
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve("tts")
    assert res.resolved
    assert res.provider == "faustus_tts"
    assert "native Faustus service" in res.reason


@pytest.mark.asyncio
async def test_faustus_tts_forbidden_records_reason_and_becomes_unavailable():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", EMPTY_REGISTRY)
    router.get(7000, "/api/tts/capabilities", httpx.Response(403, json={"error": "forbidden"}))
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve("tts")
    assert res.state == "unavailable"
    assert any("browser session" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_not_reachable_records_reason_and_continues():
    router = Router()  # both 7000 and 7001 refuse
    cfg = LinkConfig()
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.state == "unavailable"
    assert any("Faustus not reachable" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_url_lookup_is_cached():
    calls = {"n": 0}

    def health(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return HEALTHY

    router = Router()
    router.get(7000, "/api/health", health)
    router.get(7000, "/api/models", EMPTY_REGISTRY)
    cfg = LinkConfig()
    link = make_link(router, config=cfg)

    await link.resolve("llm")
    await link.resolve("vision")
    assert calls["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["tts", "stt"])
async def test_faustus_voice_service_switched_off_is_not_resolved(capability):
    # Shape answered by a real Faustus whose voice services are off.
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", EMPTY_REGISTRY)
    router.get(
        7000,
        f"/api/{capability}/capabilities",
        httpx.Response(200, json={"provider": "disabled", "configured": False, "ready": False}),
    )
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    res = await link.resolve(capability)
    assert res.state == "unavailable"
    assert any("switched off" in r for r in res.details["reasons"])
