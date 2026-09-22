from __future__ import annotations

import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from tests.conftest import Router, make_link


@pytest.mark.asyncio
async def test_explicit_config_wins_without_any_probing():
    router = Router()  # nothing registered: any probe would ConnectError
    cfg = LinkConfig(
        capabilities={
            "llm": CapabilityConfig(
                url="http://127.0.0.1:8081/v1/chat/completions",
                model="qwen3.8-27b-q8-llamacpp",
                api="openai",
                provider="llamacpp",
            )
        }
    )
    link = make_link(router, config=cfg)
    res = await link.resolve("llm")
    assert res.resolved
    assert res.provider == "llamacpp"
    assert res.url == "http://127.0.0.1:8081/v1/chat/completions"
    assert res.model == "qwen3.8-27b-q8-llamacpp"
    assert res.api == "openai"
    assert "explicit configuration" in res.reason


@pytest.mark.asyncio
async def test_explicit_command_config_for_tts():
    router = Router()
    cfg = LinkConfig(capabilities={"tts": CapabilityConfig(command=["echo", "{text}"])})
    link = make_link(router, config=cfg)
    res = await link.resolve("tts")
    assert res.resolved
    assert res.details["command"] == ["echo", "{text}"]
    assert res.provider == "configured-command"


@pytest.mark.asyncio
async def test_explicit_config_defaults_provider_and_api():
    router = Router()
    cfg = LinkConfig(capabilities={"vision": CapabilityConfig(url="http://127.0.0.1:5001/v1/chat/completions")})
    link = make_link(router, config=cfg)
    res = await link.resolve("vision")
    assert res.resolved
    assert res.provider == "configured"
    assert res.api == "openai"
