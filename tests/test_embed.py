from __future__ import annotations

import json

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from hoard_link.errors import Unavailable
from tests.conftest import Router, make_link


@pytest.mark.asyncio
async def test_embed_openai_dialect():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        assert body["input"] == ["a", "b"]
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]})

    cfg = LinkConfig(
        capabilities={
            "embeddings": CapabilityConfig(url="http://127.0.0.1:8081/v1/embeddings", model="embed1", api="openai")
        }
    )
    link = make_link(handler, config=cfg)
    vecs = await link.embed(["a", "b"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.asyncio
async def test_embed_ollama_dialect():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        return httpx.Response(200, json={"embeddings": [[1.0, 2.0]]})

    cfg = LinkConfig(
        capabilities={"embeddings": CapabilityConfig(url="http://127.0.0.1:11434", model="embed1", api="ollama")}
    )
    link = make_link(handler, config=cfg)
    vecs = await link.embed(["only one"])
    assert vecs == [[1.0, 2.0]]


@pytest.mark.asyncio
async def test_embed_ollama_endpoint_not_duplicated():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"embeddings": [[1.0]]})

    cfg = LinkConfig(
        capabilities={
            "embeddings": CapabilityConfig(url="http://127.0.0.1:11434/api/embed", model="e", api="ollama")
        }
    )
    link = make_link(handler, config=cfg)
    await link.embed(["x"])
    assert calls == ["http://127.0.0.1:11434/api/embed"]


@pytest.mark.asyncio
async def test_embed_raises_unavailable_when_no_embeddings_server():
    router = Router()
    link = make_link(router, config=LinkConfig())
    with pytest.raises(Unavailable) as excinfo:
        await link.embed(["a"])
    assert excinfo.value.capability == "embeddings"
