from __future__ import annotations

import asyncio

import httpx
import pytest

from hoard_link import Link, _faustus
from hoard_link._chat_routing import ChatRouting
from hoard_link.config import LinkConfig
from tests.conftest import ISOLATED_HUB_PORT, Router, make_link

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


def _moved_to(url):
    return {"location": "sparks", "job": {"running": False}, "urls": {"faustus": url}}


@pytest.mark.asyncio
@pytest.mark.parametrize("spark_healthy", [True, False])
async def test_faustus_probes_the_active_placement_before_configured_fallback(spark_healthy):
    spark = "https://spark.test"
    config = LinkConfig()
    first_local = config.faustus_urls[0] + "/api/health"
    hits = []

    async def send(request):
        address = str(request.url)
        hits.append(address)
        if address == spark + "/api/health" and not spark_healthy:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "healthy"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        link = Link(config, client=client)
        link._placement = lambda: _moved_to(spark)
        assert await link._faustus_url() == (spark if spark_healthy else config.faustus_urls[0])
    assert hits[0] == spark + "/api/health"
    assert (first_local in hits) is not spark_healthy
    if spark_healthy:
        assert len(hits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred_answers", [True, False])
async def test_local_health_probes_overlap_without_demoting_a_connected_preferred_service(preferred_answers):
    first, second = LinkConfig().faustus_urls
    second_started = asyncio.Event()
    calls = []

    async def send(request):
        calls.append(request)
        if str(request.url) == first + "/api/health":
            await asyncio.wait_for(second_started.wait(), 0.2)
            if not preferred_answers:
                raise httpx.ConnectTimeout("first local socket did not connect")
            await asyncio.sleep(0)
        else:
            second_started.set()
        return httpx.Response(200, json={"status": "healthy"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        assert await _faustus.find_reachable(client, (first, second)) == (first if preferred_answers else second)
    assert len(calls) == 2
    for call in calls:
        limits = call.extensions["timeout"]
        assert limits["connect"] <= 0.3
        assert limits["read"] == _faustus.TIMEOUT_S


@pytest.mark.asyncio
async def test_placed_registry_preparation_is_shared_with_the_first_lookup():
    spark = "https://spark.test"
    backend = "http://preferred.local"
    registry_started, release = asyncio.Event(), asyncio.Event()
    clocks = [1.0]
    hits = []
    registered = {"url": backend, "models": ["example"], "endpoint_id": "preferred",
                  "model_type": "llm", "category": "local", "backend": "openai"}

    async def send(request):
        assert request.method == "GET"
        hits.append(str(request.url))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/api/models":
            assert request.headers["Authorization"] == "Bearer synthetic"
            registry_started.set()
            await release.wait()
            return httpx.Response(200, json={"items": [registered]})
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "example"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        routing = ChatRouting()
        config = LinkConfig(faustus_token="synthetic")
        startup = Link(config, client, chat_routing=routing, placement=lambda: _moved_to(spark))
        caller = Link(config, client, chat_routing=routing, placement=lambda: _moved_to(spark))
        startup._now = caller._now = lambda: clocks[0]
        warm = asyncio.create_task(startup.warm_placed_registry(spark))
        await asyncio.wait_for(registry_started.wait(), 1)
        first = asyncio.create_task(caller._resolve_chat("llm", None, None))
        await asyncio.sleep(0)
        release.set()
        answer = await first
        await warm
        assert answer.details["endpoint_id"] == "preferred"
        assert hits == [spark + "/api/health", spark + "/api/models", backend + "/v1/models"]
        clocks[0] += 5
        await caller.resolve("llm")
    assert hits.count(spark + "/api/models") == 2


def llamacpp_ready(router: Router, port: int = 8081, model: str = "qwen3.8-27b-q8-llamacpp") -> Router:
    from tests.conftest import llama_health

    llama_health(port, router=router)
    router.get(port, "/props", httpx.Response(200, json={
        "model_path": f"C:/models/{model}.gguf",
        "default_generation_settings": {},
    }))
    router.get(port, "/v1/models", httpx.Response(200, json={"data": [{"id": model}]}))
    router.get(port, "/slots", httpx.Response(200, json=[]))
    return router


@pytest.mark.asyncio
async def test_faustus_registry_resolves_llm_with_token():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", LLM_REGISTRY)
    llamacpp_ready(router)
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
    llamacpp_ready(router)
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
    llamacpp_ready(router)
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


# ---- Faustus moved to a Spark by the Hub ("Run on"): the Hub says where it answers -------------------------------------

def _placement(location: str, running: bool = False) -> httpx.Response:
    return httpx.Response(200, json={"location": location, "job": {"running": running},
                                     "urls": {"faustus": "https://spark3.example.ts.net:7443"}})


@pytest.mark.asyncio
async def test_faustus_on_a_spark_is_found_through_the_hub():
    router = Router()
    router.get(ISOLATED_HUB_PORT, "/api/placement", _placement("sparks"))
    router.get(7443, "/api/health", HEALTHY)
    router.get(7443, "/api/models", LLM_REGISTRY)
    llamacpp_ready(router)
    link = make_link(router, config=LinkConfig(faustus_token="ody_test"))
    res = await link.resolve("llm")
    assert res.resolved and "from Faustus registry" in res.reason


@pytest.mark.asyncio
async def test_the_hub_is_not_asked_when_faustus_answers_here_and_its_answer_needs_sparks():
    from hoard_link import _faustus

    calls = []
    router = Router()
    router.get(ISOLATED_HUB_PORT, "/api/placement", lambda req: calls.append(req) or _placement("sparks"))
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", LLM_REGISTRY)
    llamacpp_ready(router)
    link = make_link(router, config=LinkConfig(faustus_token="ody_test"))
    assert (await link.resolve("llm")).resolved and not calls
    pc = Router().get(ISOLATED_HUB_PORT, "/api/placement", _placement("pc"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(pc))
    assert await _faustus.placement_urls(client) == ()
    moving = Router().get(ISOLATED_HUB_PORT, "/api/placement", _placement("sparks", running=True))
    client = httpx.AsyncClient(transport=httpx.MockTransport(moving))
    assert await _faustus.placement_urls(client) == ()                       # mid-move: nowhere yet
    client = httpx.AsyncClient(transport=httpx.MockTransport(Router()))
    assert await _faustus.placement_urls(client) == ()                       # no Hub


@pytest.mark.asyncio
async def test_a_faustus_on_another_machine_gets_a_longer_health_probe(monkeypatch):
    import asyncio

    from hoard_link import _faustus

    monkeypatch.setattr(_faustus, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(_faustus, "REMOTE_TIMEOUT_S", 2.0)

    async def slow(request):
        await asyncio.sleep(0.3)                     # TLS through the tailnet relay
        return httpx.Response(200, json={"status": "healthy"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    assert await _faustus.find_reachable(client, ("https://spark3.example.ts.net",)) == "https://spark3.example.ts.net"
    assert await _faustus.find_reachable(client, ("http://127.0.0.1:7000",)) is None    # a local one is not waited for


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [
    {"location": "sparks", "job": {"running": False}, "urls": {"faustus": "https://spark3.example.ts.net:7443/"}},
    {"location": "sparks", "job": {"running": True}, "urls": {"faustus": "https://spark3.example.ts.net:7443"}},
    {"location": "pc", "urls": {"faustus": "https://spark3.example.ts.net:7443"}},
    {"location": "sparks", "urls": {"faustus": "spark3"}},
    {"location": "sparks", "urls": {}},
])
async def test_link_reads_the_placement_with_the_rule_every_hoard_shares(document):
    from hoard_link import _faustus, where

    router = Router().get(ISOLATED_HUB_PORT, "/api/placement", httpx.Response(200, json=document))
    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    placed = where.placed("faustus", document)
    assert await _faustus.placement_urls(client) == ((placed,) if placed else ())
