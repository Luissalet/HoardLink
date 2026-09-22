from __future__ import annotations

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from tests.conftest import Router, make_link


def props(model_path: str = "/models/qwen3.8-27b-q8.gguf", modalities=None, n_ctx=8192):
    return httpx.Response(
        200,
        json={
            "model_path": model_path,
            "default_generation_settings": {"n_ctx": n_ctx},
            "modalities": modalities or [],
        },
    )


def v1_models(model_id: str):
    return httpx.Response(200, json={"data": [{"id": model_id}]})


def slots(processing: bool):
    return httpx.Response(200, json=[{"id": 0, "is_processing": processing}])


@pytest.mark.asyncio
async def test_llamacpp_resolves_llm():
    router = Router()
    router.get(8081, "/props", props())
    router.get(8081, "/v1/models", v1_models("qwen3.8-27b-q8-llamacpp"))
    router.get(8081, "/slots", slots(False))
    link = make_link(router)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.provider == "llamacpp"
    assert res.model == "qwen3.8-27b-q8-llamacpp"
    assert res.url == "http://127.0.0.1:8081/v1/chat/completions"
    assert res.api == "openai"
    assert res.details["resident"] is True
    assert res.details["busy"] is False


@pytest.mark.asyncio
async def test_llamacpp_falls_back_to_model_path_basename_without_v1_models():
    router = Router()
    router.get(8085, "/props", props(model_path="/models/qwen3.8-27b-q8.gguf"))
    link = make_link(router)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.model == "qwen3.8-27b-q8.gguf"


@pytest.mark.asyncio
async def test_llamacpp_busy_slot_is_reflected_in_reason():
    router = Router()
    router.get(8081, "/props", props())
    router.get(8081, "/v1/models", v1_models("m"))
    router.get(8081, "/slots", slots(True))
    link = make_link(router)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.details["busy"] is True
    assert "busy" in res.reason


@pytest.mark.asyncio
async def test_llamacpp_without_vision_modality_is_skipped_for_vision():
    router = Router()
    router.get(8081, "/props", props(modalities=[]))
    router.get(8081, "/v1/models", v1_models("m"))
    link = make_link(router)

    res = await link.resolve("vision")
    assert res.state == "unavailable"
    assert any("no vision modality" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_llamacpp_with_vision_modality_resolves_vision():
    router = Router()
    router.get(8081, "/props", props(modalities=["vision"]))
    router.get(8081, "/v1/models", v1_models("vl-model"))
    link = make_link(router)

    res = await link.resolve("vision")
    assert res.resolved
    assert res.model == "vl-model"


@pytest.mark.asyncio
async def test_ollama_resident_model_resolves_llm():
    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": [{"model": "llama3.1:8b"}]}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]}))
    router.post(11434, "/api/show", httpx.Response(200, json={"capabilities": ["completion"]}))
    link = make_link(router)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.provider == "ollama"
    assert res.model == "llama3.1:8b"
    assert res.api == "ollama"
    assert "resident" in res.reason


@pytest.mark.asyncio
async def test_ollama_picks_resident_model_with_vision_capability():
    router = Router()
    router.get(
        11434,
        "/api/ps",
        httpx.Response(200, json={"models": [{"model": "text-only:8b"}, {"model": "vision:8b"}]}),
    )
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": []}))

    def show(request: httpx.Request) -> httpx.Response:
        body = request.read()
        if b"vision:8b" in body:
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return httpx.Response(200, json={"capabilities": ["completion"]})

    router.post(11434, "/api/show", show)
    link = make_link(router)

    res = await link.resolve("vision")
    assert res.resolved
    assert res.model == "vision:8b"


@pytest.mark.asyncio
async def test_ollama_embeddings_requires_embedding_capability():
    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": [{"model": "embed:latest"}]}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": []}))
    router.post(11434, "/api/show", httpx.Response(200, json={"capabilities": ["embedding"]}))
    link = make_link(router)

    res = await link.resolve("embeddings")
    assert res.resolved
    assert res.model == "embed:latest"


@pytest.mark.asyncio
async def test_only_resident_default_ignores_non_resident_tags():
    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": []}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]}))
    link = make_link(router)

    res = await link.resolve("llm")
    assert res.state == "unavailable"
    assert any("only_resident=True" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_allow_load_permits_non_resident_ollama_model():
    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": []}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]}))
    cfg = LinkConfig(only_resident=False, capabilities={"llm": CapabilityConfig(allow_load=True)})
    link = make_link(router, config=cfg)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.model == "llama3.1:8b"
    assert "would load" in res.reason
    assert res.details["resident"] is False


@pytest.mark.asyncio
async def test_openai_compat_fallback_for_llm_only():
    router = Router()
    router.get(1234, "/v1/models", httpx.Response(200, json={"data": [{"id": "local-model"}]}))
    link = make_link(router)

    res = await link.resolve("llm")
    assert res.resolved
    assert res.provider == "openai_compat"
    assert res.model == "local-model"


@pytest.mark.asyncio
async def test_comfy_resolves_image_capability():
    router = Router()
    router.get(8188, "/system_stats", httpx.Response(200, json={"system": {}}))
    router.get(
        8188,
        "/object_info/CheckpointLoaderSimple",
        httpx.Response(
            200,
            json={"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["sdxl.safetensors"]]}}}},
        ),
    )
    link = make_link(router)

    res = await link.resolve("image")
    assert res.resolved
    assert res.provider == "comfyui"
    assert res.details["checkpoints"] == ["sdxl.safetensors"]


@pytest.mark.asyncio
async def test_music_capability_has_no_loopback_provider():
    router = Router()
    link = make_link(router)

    res = await link.resolve("music")
    assert res.state == "unavailable"
    assert any("no loopback provider" in r for r in res.details["reasons"])


@pytest.mark.asyncio
async def test_nothing_available_returns_unavailable_with_reasons():
    router = Router()
    link = make_link(router)

    res = await link.resolve("llm")
    assert res.state == "unavailable"
    assert not res.resolved
    assert len(res.details["reasons"]) >= 2


@pytest.mark.asyncio
async def test_llamacpp_probe_is_cached_across_resolves():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return props()

    router = Router()
    router.get(8081, "/props", handler)
    link = make_link(router)

    await link.resolve("llm")
    await link.resolve("llm")
    assert calls["n"] == 1
