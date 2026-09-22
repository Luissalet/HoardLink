from __future__ import annotations

import base64
import json

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from hoard_link.errors import BackendError, Unavailable
from tests.conftest import Router, make_link


def openai_config(url="http://127.0.0.1:8081/v1/chat/completions", model="m1"):
    return LinkConfig(capabilities={"llm": CapabilityConfig(url=url, model=model, api="openai", provider="llamacpp")})


def ollama_config(url="http://127.0.0.1:11434", model="llama3.1:8b"):
    return LinkConfig(capabilities={"llm": CapabilityConfig(url=url, model=model, api="ollama", provider="ollama")})


@pytest.mark.asyncio
async def test_chat_openai_basic():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hola"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    link = make_link(handler, config=openai_config())
    result = await link.chat([{"role": "user", "content": "hi"}])
    assert result.text == "hola"
    assert result.model == "m1"
    assert result.provider == "llamacpp"
    assert result.usage.prompt_tokens == 3
    assert result.usage.completion_tokens == 2
    assert result.usage.total_tokens == 5
    assert result.reasoning is None
    assert result.elapsed_ms >= 0


@pytest.mark.asyncio
async def test_chat_openai_strips_think_tags():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "<think>pondering...</think>final answer"}}]},
        )

    link = make_link(handler, config=openai_config())
    result = await link.chat([{"role": "user", "content": "hi"}])
    assert result.text == "final answer"
    assert result.reasoning == "pondering..."


@pytest.mark.asyncio
async def test_chat_openai_embeds_images_as_data_urls():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    link = make_link(handler, config=openai_config())
    await link.chat([{"role": "user", "content": "describe this"}], images=[b"fake-jpeg-bytes"])

    msgs = captured["body"]["messages"]
    last = msgs[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][0] == {"type": "text", "text": "describe this"}
    image_part = last["content"][1]
    assert image_part["type"] == "image_url"
    b64 = base64.b64encode(b"fake-jpeg-bytes").decode()
    assert image_part["image_url"]["url"] == f"data:image/jpeg;base64,{b64}"


@pytest.mark.asyncio
async def test_chat_openai_passes_max_tokens_temperature_and_response_format():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    link = make_link(handler, config=openai_config())
    await link.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=42,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    body = captured["body"]
    assert body["max_tokens"] == 42
    assert body["temperature"] == 0.1
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False


@pytest.mark.asyncio
async def test_chat_openai_backend_error_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    link = make_link(handler, config=openai_config())
    with pytest.raises(BackendError) as excinfo:
        await link.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status == 500
    assert excinfo.value.provider == "llamacpp"


@pytest.mark.asyncio
async def test_chat_ollama_basic():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={"message": {"content": "hola"}, "prompt_eval_count": 7, "eval_count": 4},
        )

    link = make_link(handler, config=ollama_config())
    result = await link.chat([{"role": "user", "content": "hi"}])
    assert result.text == "hola"
    assert result.usage.prompt_tokens == 7
    assert result.usage.completion_tokens == 4
    assert result.usage.total_tokens == 11


@pytest.mark.asyncio
async def test_chat_ollama_embeds_images_as_base64_list():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"message": {"content": "ok"}})

    link = make_link(handler, config=ollama_config())
    await link.chat([{"role": "user", "content": "look"}], images=[b"png-bytes"])
    last = captured["body"]["messages"][-1]
    assert last["images"] == [base64.b64encode(b"png-bytes").decode()]


@pytest.mark.asyncio
async def test_chat_ollama_endpoint_not_duplicated_when_already_full():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    cfg = ollama_config(url="http://127.0.0.1:11434/api/chat")
    link = make_link(handler, config=cfg)
    await link.chat([{"role": "user", "content": "hi"}])
    assert calls == ["http://127.0.0.1:11434/api/chat"]


@pytest.mark.asyncio
async def test_chat_ollama_options_from_max_tokens_and_temperature():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"message": {"content": "ok"}})

    link = make_link(handler, config=ollama_config())
    await link.chat([{"role": "user", "content": "hi"}], max_tokens=50, temperature=0.7)
    options = captured["body"]["options"]
    assert options["num_predict"] == 50
    assert options["temperature"] == 0.7


@pytest.mark.asyncio
async def test_chat_raises_unavailable_when_nothing_resolves():
    router = Router()
    link = make_link(router, config=LinkConfig())
    with pytest.raises(Unavailable) as excinfo:
        await link.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.capability == "llm"
    assert len(excinfo.value.reasons) >= 1
