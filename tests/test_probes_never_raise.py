from __future__ import annotations

import httpx
import pytest

from hoard_link import _probes
from tests.conftest import Router


@pytest.mark.asyncio
async def test_probe_llamacpp_returns_empty_list_when_all_ports_refuse():
    router = Router()
    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    try:
        result = await _probes.probe_llamacpp(client)
        assert result == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_ollama_returns_none_on_connection_refused():
    router = Router()
    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    try:
        result = await _probes.probe_ollama(client)
        assert result is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_openai_compat_returns_none_on_connection_refused():
    router = Router()
    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    try:
        result = await _probes.probe_openai_compat(client)
        assert result is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_comfy_returns_none_on_connection_refused():
    router = Router()
    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    try:
        result = await _probes.probe_comfy(client)
        assert result is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_ollama_handles_bad_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await _probes.probe_ollama(client)
        assert result is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_llamacpp_handles_non_200_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await _probes.probe_llamacpp(client)
        assert result == []
    finally:
        await client.aclose()
