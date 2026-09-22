from __future__ import annotations

import httpx
import pytest

from hoard_link._comfy import ComfyClient
from hoard_link.config import LinkConfig
from hoard_link.types import CAPABILITIES
from tests.conftest import Router, make_link


@pytest.mark.asyncio
async def test_status_covers_every_capability():
    router = Router()
    link = make_link(router, config=LinkConfig())
    status = await link.status()
    assert set(status.keys()) == set(CAPABILITIES)
    for cap, entry in status.items():
        assert entry["capability"] == cap
        assert entry["state"] in ("resolved", "unavailable")
        assert isinstance(entry["reason"], str) and entry["reason"]


@pytest.mark.asyncio
async def test_link_comfy_returns_client_when_configured_url_set():
    router = Router()
    cfg = LinkConfig(comfy_url="http://127.0.0.1:8199")
    link = make_link(router, config=cfg)
    client = await link.comfy()
    assert isinstance(client, ComfyClient)
    assert client.url == "http://127.0.0.1:8199"


@pytest.mark.asyncio
async def test_link_comfy_returns_none_when_unreachable():
    router = Router()
    link = make_link(router, config=LinkConfig())
    client = await link.comfy()
    assert client is None


@pytest.mark.asyncio
async def test_link_comfy_resolves_via_loopback_probe():
    router = Router()
    router.get(8188, "/system_stats", httpx.Response(200, json={}))
    router.get(
        8188,
        "/object_info/CheckpointLoaderSimple",
        httpx.Response(200, json={"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [[]]}}}}),
    )
    link = make_link(router, config=LinkConfig())
    client = await link.comfy()
    assert isinstance(client, ComfyClient)
    assert client.url == "http://127.0.0.1:8188"
