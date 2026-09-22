"""Probes stay quiet and bounded whatever a port answers."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from hoard_link import _faustus, _probes
from hoard_link.config import LinkConfig
from tests.conftest import Router, make_link


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_ollama_ps_returning_a_list_yields_none():
    router = Router().get(11434, "/api/ps", httpx.Response(200, json=[1, 2]))
    async with client_for(router) as c:
        assert await _probes.probe_ollama(c) is None


@pytest.mark.asyncio
async def test_ollama_models_with_junk_entries_are_skipped():
    router = (
        Router()
        .get(11434, "/api/ps", httpx.Response(200, json={"models": [None, "x", {"name": "qwen"}]}))
        .get(11434, "/api/tags", httpx.Response(200, json={"models": "nope"}))
        .post(11434, "/api/show", httpx.Response(200, json={"capabilities": None}))
    )
    async with client_for(router) as c:
        out = await _probes.probe_ollama(c)
    assert [m["name"] for m in out["resident"]] == ["qwen"]
    assert out["resident"][0]["capabilities"] == []
    assert out["tags"] == []


@pytest.mark.asyncio
async def test_openai_compat_models_as_list_yields_none():
    router = Router().get(1234, "/v1/models", httpx.Response(200, json=["a"]))
    async with client_for(router) as c:
        assert await _probes.probe_openai_compat(c) is None


@pytest.mark.asyncio
async def test_non_llamacpp_app_answering_props_is_not_a_llama_server():
    # Another local web app on 8085 that answers any path with JSON.
    router = Router().get(8085, "/props", httpx.Response(200, json={"hello": "world"}))
    async with client_for(router) as c:
        assert await _probes.probe_llamacpp(c) == []


@pytest.mark.asyncio
async def test_llamacpp_props_as_list_does_not_break_vision_resolution():
    router = Router().get(8081, "/props", httpx.Response(200, json=["x"]))
    link = make_link(router)
    res = await link.resolve("vision")
    assert res.state == "unavailable"


def test_llamacpp_modalities_dict_shape_is_understood():
    assert _probes.llamacpp_supports_vision({"props": {"modalities": {"vision": True}}})
    assert not _probes.llamacpp_supports_vision({"props": {"modalities": {"vision": False}}})
    assert not _probes.llamacpp_supports_vision({"props": None})


def test_busy_check_tolerates_junk_slots():
    assert _probes.llamacpp_busy({"slots": ["x", None, {"is_processing": True}]})
    assert not _probes.llamacpp_busy({"slots": "nope"})


@pytest.mark.asyncio
async def test_probe_is_bounded_by_wall_clock_even_if_server_hangs(monkeypatch):
    monkeypatch.setattr(_probes, "PROBE_TIMEOUT_S", 0.05)

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json={})

    async with client_for(hang) as c:
        t0 = time.perf_counter()
        results = await asyncio.gather(
            _probes.probe_llamacpp(c),
            _probes.probe_ollama(c),
            _probes.probe_openai_compat(c),
            _probes.probe_comfy(c),
        )
        elapsed = time.perf_counter() - t0
    assert results == [[], None, None, None]
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_faustus_request_is_bounded_and_never_raises(monkeypatch):
    monkeypatch.setattr(_faustus, "TIMEOUT_S", 0.05)

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json={})

    async with client_for(hang) as c:
        t0 = time.perf_counter()
        assert await _faustus.get(c, "http://127.0.0.1:7000/api/health") == (None, None)
        assert time.perf_counter() - t0 < 1.0


@pytest.mark.asyncio
async def test_invalid_url_in_config_does_not_raise_from_probe():
    link = make_link(Router(), config=LinkConfig(faustus_urls=("http://127.0.0.1:notaport",)))
    res = await link.resolve("llm")
    assert res.state == "unavailable"


@pytest.mark.asyncio
async def test_status_probes_each_server_once_single_flight():
    hits: dict[str, int] = {}

    def counting(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.port}{request.url.path}"
        hits[key] = hits.get(key, 0) + 1
        raise httpx.ConnectError("refused", request=request)

    link = make_link(counting)
    await link.status()
    assert hits["8081/props"] == 1
    assert hits["11434/api/ps"] == 1
    assert hits["7000/api/health"] == 1


def test_owned_client_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    from hoard_link import Link

    link = Link(LinkConfig())
    assert link._client._trust_env is False
