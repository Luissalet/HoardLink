"""Faustus registry edge cases and Ollama selection policy."""

from __future__ import annotations

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from tests.conftest import FakeClock, Router, llama_health, make_link

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


def llama_ready(router: Router, model: str = "qwen3.8-27b-q8-llamacpp") -> Router:
    llama_health(8081, router=router)
    router.get(8081, "/props", httpx.Response(200, json={
        "model_path": f"C:/models/{model}.gguf",
        "default_generation_settings": {},
    }))
    router.get(8081, "/v1/models", httpx.Response(200, json={"data": [{"id": model}]}))
    router.get(8081, "/slots", httpx.Response(200, json=[]))
    return router


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

    router = llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", models))
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
    router = llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(cloud, LLAMA_ITEM)))
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
    router = llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(item)), model="b")
    cfg = LinkConfig.load(None, env={"HOARD_LLM_MODEL": "b"})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.model == "b"


@pytest.mark.asyncio
async def test_faustus_url_with_trailing_slash_still_works():
    router = llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(LLAMA_ITEM)))
    cfg = LinkConfig.load(None, env={"HOARD_FAUSTUS_URL": "http://127.0.0.1:7000/"})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.details["source"] == "faustus_registry"


@pytest.mark.asyncio
async def test_wait_idle_works_for_a_llama_server_from_faustus_registry():
    router = (
        Router()
        .get(7000, "/api/health", HEALTHY)
        .get(7000, "/api/models", registry(LLAMA_ITEM))
        .get(8081, "/health", llama_health())
        .get(8081, "/props", httpx.Response(200, json={"model_path": "m.gguf"}))
        .get(8081, "/slots", httpx.Response(200, json=[{"is_processing": True}]))
    )
    clock = FakeClock()
    link = make_link(router, fake_clock=clock)
    assert await link.wait_idle("llm", max_wait_s=3) is False
    assert sum(clock.sleeps) == pytest.approx(3.0)  # never sleeps past the deadline


@pytest.mark.asyncio
async def test_wait_idle_works_for_explicit_llamacpp_url():
    router = (
        Router()
        .get(8083, "/health", llama_health())
        .get(8083, "/props", httpx.Response(200, json={"model_path": "m.gguf"}))
        .get(8083, "/slots", httpx.Response(200, json=[{"is_processing": False}]))
    )
    cfg = LinkConfig(capabilities={"llm": CapabilityConfig(url="http://127.0.0.1:8083", provider="llamacpp")})
    assert await make_link(router, config=cfg).wait_idle("llm", max_wait_s=5) is True


@pytest.mark.asyncio
async def test_windows_model_path_gives_file_name():
    router = (
        Router()
        .get(8081, "/health", llama_health())
        .get(8081, "/props", httpx.Response(200, json={"model_path": "C:\\models\\qwen-27b.gguf"}))
    )
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
        .get(8081, "/health", llama_health())
        .get(8081, "/props", httpx.Response(200, json={"model_path": "m.gguf"}))
        .get(8081, "/slots", httpx.Response(200, json=[{"is_processing": False}, {"is_processing": True}]))
    )
    res = await make_link(router).resolve("llm")
    assert res.provider == "llamacpp" and res.details["busy"] is True


# ---- the person's own network: a model the Sparks already serve beats loading one on this PC ----------------------

SPARKS_ITEM = {
    "url": "http://192.168.0.185:8003/v1/chat/completions",
    "models": ["qwen3.8-27b-nvfp4"],
    "endpoint_id": "sparks1",
    "endpoint_name": "Sparks · qwen38-27b-1m",
    "category": "local",
    "model_type": "llm",
    "backend": "unknown",
}


def sparks_serving(router: Router, models=("qwen3.8-27b-nvfp4",)) -> Router:
    return router.get(8003, "/v1/models", httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in models]}))


def test_host_scope_tells_this_machine_the_home_network_and_the_internet_apart():
    from hoard_link._faustus import host_scope, is_local_item

    assert host_scope("http://127.0.0.1:8081/v1") == "loopback" and host_scope("http://[::1]:8081") == "loopback"
    for url in ("http://192.168.0.185:8003/v1", "http://10.100.32.2:8000", "http://172.20.1.5", "http://spark-c680.local:8003",
                "http://nas.home.arpa", "http://[fe80::1]:80"):
        assert host_scope(url) == "lan", url
    for url in ("https://api.example.com/v1", "http://8.8.8.8", "http://100.64.0.1", "", None):
        assert host_scope(url) == "remote", url
    assert is_local_item(SPARKS_ITEM) and not is_local_item(dict(SPARKS_ITEM, category="cloud"))


@pytest.mark.asyncio
async def test_a_model_served_on_the_local_network_goes_before_this_pcs_gpus():
    router = sparks_serving(llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(LLAMA_ITEM, SPARKS_ITEM))))
    res = await make_link(router).resolve("llm")
    assert res.resolved and res.url == SPARKS_ITEM["url"] and res.model == "qwen3.8-27b-nvfp4"
    assert res.details["resident"] is True and res.details["endpoint_id"] == "sparks1"


@pytest.mark.asyncio
async def test_a_silent_or_changed_network_server_falls_back_to_this_pc():
    base = llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(SPARKS_ITEM, LLAMA_ITEM)))
    res = await make_link(base).resolve("llm")             # the Sparks do not answer
    assert res.resolved and res.url == LLAMA_ITEM["url"]
    changed = sparks_serving(llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(SPARKS_ITEM, LLAMA_ITEM))),
                             models=("glm-5.3-flash-nvfp4",))
    res = await make_link(changed).resolve("llm")          # they serve something else now
    assert res.url == LLAMA_ITEM["url"]


@pytest.mark.asyncio
async def test_only_a_network_server_answers_and_it_is_used_without_any_lease():
    router = sparks_serving(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(SPARKS_ITEM)))
    res = await make_link(router).resolve("llm")
    assert res.resolved and res.provider == "unknown" and res.api == "openai" and res.details["resident"] is True


# ---- a busy Faustus: the registry it gave a moment ago still keeps work off this PC's GPUs -------------------------


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("busy", request=request)


@pytest.mark.asyncio
async def test_a_registry_timeout_uses_the_registry_faustus_gave_recently():
    first = sparks_serving(llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(LLAMA_ITEM, SPARKS_ITEM))))
    assert (await make_link(first).resolve("llm")).url == SPARKS_ITEM["url"]
    busy = sparks_serving(llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", _timeout)))
    res = await make_link(busy).resolve("llm")
    assert res.url == SPARKS_ITEM["url"]


@pytest.mark.asyncio
async def test_a_faustus_too_busy_for_its_health_check_still_routes_to_the_sparks():
    first = sparks_serving(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(SPARKS_ITEM)))
    assert (await make_link(first).resolve("llm")).url == SPARKS_ITEM["url"]
    silent = sparks_serving(llama_ready(Router().get(7000, "/api/health", _timeout)))
    res = await make_link(silent).resolve("llm")
    assert res.url == SPARKS_ITEM["url"]


@pytest.mark.asyncio
async def test_a_remembered_server_that_stopped_is_not_trusted_and_an_old_registry_expires(monkeypatch):
    import hoard_link.link as link_mod

    first = sparks_serving(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(SPARKS_ITEM)))
    await make_link(first).resolve("llm")
    stopped = llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", _timeout))
    assert (await make_link(stopped).resolve("llm")).url == LLAMA_ITEM["url"]  # the Sparks no longer answer
    data, at = link_mod._last_registry
    monkeypatch.setattr(link_mod, "_last_registry", (data, at - link_mod._REGISTRY_MEMORY_TTL_S - 1))
    assert link_mod._remembered_registry() is None


@pytest.mark.asyncio
async def test_nothing_is_remembered_before_faustus_answers_once():
    import hoard_link.link as link_mod

    assert link_mod._remembered_registry() is None
    res = await make_link(llama_ready(Router().get(7000, "/api/health", HEALTHY).get(7000, "/api/models", _timeout))).resolve("llm")
    assert res.url == LLAMA_ITEM["url"]
