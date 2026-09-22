"""Faustus registry edge cases and Ollama selection policy."""

from __future__ import annotations

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from tests.conftest import Router, make_link

HEALTHY = httpx.Response(200, json={"status": "healthy"})


def registry(*items):
    return httpx.Response(200, json={"items": list(items)})


LLAMA_ITEM = {
    "url": "http://127.0.0.1:8081/v1/chat/completions",
    "models": ["qwen3.8-27b-q8-llamacpp"],
    "endpoint_id": "19e14b1c",
    "category": "local",
    "model_type": "llm",
    "backend": "llamacpp",
}


@pytest.mark.asyncio
async def test_faustus_401_without_token_says_a_token_is_needed():
    router = Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", httpx.Response(401, json={}))
    res = await make_link(router).resolve("llm")
    assert any("needs a token" in r and "HOARD_FAUSTUS_TOKEN" in r for r in res.details["reasons"])
    assert not any("rejected the token" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_403_with_token_says_token_rejected():
    router = Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", httpx.Response(403, json={}))
    res = await make_link(router, config=LinkConfig(faustus_token="ody_x")).resolve("llm")
    assert any("rejected the token" in r and "403" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_without_token_sends_no_authorization_header():
    seen = {}

    def models(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return registry(LLAMA_ITEM)

    router = Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", models)
    res = await make_link(router).resolve("llm")
    assert res.resolved and seen["auth"] is None


@pytest.mark.asyncio
async def test_faustus_cloud_endpoint_is_never_used():
    cloud = dict(LLAMA_ITEM, url="https://api.example.com/v1/chat/completions", category="cloud", backend="openai")
    router = Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(cloud))
    res = await make_link(router).resolve("llm")
    assert res.state == "unavailable"
    assert any("non-local" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_registry_prefers_local_item_over_cloud():
    cloud = dict(LLAMA_ITEM, url="https://api.example.com/v1/chat/completions", category="cloud")
    router = Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(cloud, LLAMA_ITEM))
    res = await make_link(router).resolve("llm")
    assert res.url == LLAMA_ITEM["url"]


@pytest.mark.asyncio
async def test_faustus_registry_ollama_model_is_checked_for_residency():
    item = {
        "url": "http://127.0.0.1:11434",
        "models": ["big:70b", "qwen3:8b"],
        "category": "local",
        "model_type": "llm",
        "backend": "ollama",
    }
    router = (
        Router()
        .get(7000, "/api/health", HEALTHY)
        .get(7000, "/api/models", registry(item))
        .get(11434, "/api/ps", httpx.Response(200, json={"models": [{"model": "qwen3:8b"}]}))
        .post(11434, "/api/show", httpx.Response(200, json={"capabilities": ["completion"]}))
    )
    res = await make_link(router).resolve("llm")
    assert res.model == "qwen3:8b"
    assert res.api == "ollama"
    assert res.details["resident"] is True


@pytest.mark.asyncio
async def test_faustus_registry_ollama_nothing_resident_is_skipped_by_default():
    item = {"url": "http://127.0.0.1:11434", "models": ["big:70b"], "category": "local",
            "model_type": "llm", "backend": "ollama"}
    router = (
        Router()
        .get(7000, "/api/health", HEALTHY)
        .get(7000, "/api/models", registry(item))
        .get(11434, "/api/ps", httpx.Response(200, json={"models": []}))
    )
    res = await make_link(router).resolve("llm")
    assert res.state == "unavailable"
    assert any("none is resident" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_registry_honours_preferred_model():
    item = dict(LLAMA_ITEM, models=["a", "b"])
    router = Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(item))
    cfg = LinkConfig.load(None, env={"HOARD_LLM_MODEL": "b"})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.model == "b"


@pytest.mark.asyncio
async def test_faustus_url_with_trailing_slash_still_works():
    router = Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(LLAMA_ITEM))
    cfg = LinkConfig.load(None, env={"HOARD_FAUSTUS_URL": "http://127.0.0.1:7000/"})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.details["source"] == "faustus_registry"


@pytest.mark.asyncio
async def test_windows_model_path_gives_file_name():
    router = Router().get(8081, "/props", httpx.Response(200, json={"model_path": "C:\\models\\qwen-27b.gguf"}))
    res = await make_link(router).resolve("llm")
    assert res.model == "qwen-27b.gguf"


def ollama_router(ps_models, show_caps, tags=()):
    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": [{"model": m} for m in ps_models]}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": [{"name": t} for t in tags]}))

    def show(request: httpx.Request) -> httpx.Response:
        import json

        name = json.loads(request.content)["model"]
        return httpx.Response(200, json={"capabilities": show_caps.get(name, [])})

    router.post(11434, "/api/show", show)
    return router


@pytest.mark.asyncio
async def test_embedding_only_resident_model_is_never_chosen_for_chat():
    router = ollama_router(["nomic-embed-text"], {"nomic-embed-text": ["embedding"]})
    res = await make_link(router).resolve("llm")
    assert res.state == "unavailable"
    assert any("none support 'llm'" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_chat_model_chosen_even_when_embedder_listed_first():
    router = ollama_router(
        ["nomic-embed-text", "qwen3:8b"],
        {"nomic-embed-text": ["embedding"], "qwen3:8b": ["completion", "tools"]},
    )
    link = make_link(router)
    assert (await link.resolve("llm")).model == "qwen3:8b"
    assert (await link.resolve("embeddings")).model == "nomic-embed-text"


@pytest.mark.asyncio
async def test_allow_load_alone_permits_loading_under_default_only_resident():
    router = ollama_router([], {"qwen3:8b": ["completion"]}, tags=["qwen3:8b"])
    cfg = LinkConfig(capabilities={"llm": CapabilityConfig(allow_load=True)})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.model == "qwen3:8b"
    assert res.details["resident"] is False


@pytest.mark.asyncio
async def test_allow_load_for_vision_checks_the_candidate_capabilities():
    router = ollama_router(
        [], {"llama3:8b": ["completion"], "qwen2.5vl:7b": ["completion", "vision"]},
        tags=["llama3:8b", "qwen2.5vl:7b"],
    )
    cfg = LinkConfig(capabilities={"vision": CapabilityConfig(allow_load=True)})
    res = await make_link(router, config=cfg).resolve("vision")
    assert res.model == "qwen2.5vl:7b"


@pytest.mark.asyncio
async def test_allow_load_on_one_capability_does_not_leak_to_another():
    router = ollama_router([], {"qwen3:8b": ["completion"]}, tags=["qwen3:8b"])
    cfg = LinkConfig(capabilities={"vision": CapabilityConfig(allow_load=True)})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.state == "unavailable"


@pytest.mark.asyncio
async def test_preferred_model_env_picks_among_resident_models():
    router = ollama_router(["qwen3:8b", "gemma3:12b"], {"qwen3:8b": ["completion"], "gemma3:12b": ["completion"]})
    cfg = LinkConfig.load(None, env={"HOARD_LLM_MODEL": "gemma3:12b"})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.model == "gemma3:12b"


@pytest.mark.asyncio
async def test_preferred_model_not_resident_falls_back_and_says_so():
    router = ollama_router(["qwen3:8b"], {"qwen3:8b": ["completion"]}, tags=["qwen3:8b", "gemma3:12b"])
    cfg = LinkConfig.load(None, env={"HOARD_LLM_MODEL": "gemma3:12b"})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.model == "qwen3:8b"
    assert "preferred 'gemma3:12b' not available" in res.reason


@pytest.mark.asyncio
async def test_busy_llama_server_is_still_resolved_and_flagged():
    router = (
        Router()
        .get(8081, "/props", httpx.Response(200, json={"model_path": "m.gguf"}))
        .get(8081, "/slots", httpx.Response(200, json=[{"is_processing": False}, {"is_processing": True}]))
    )
    res = await make_link(router).resolve("llm")
    assert res.provider == "llamacpp" and res.details["busy"] is True
