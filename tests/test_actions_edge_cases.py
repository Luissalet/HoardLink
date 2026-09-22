"""chat / embed / tts edge cases: think tags, URL shapes, error types."""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from hoard_link.errors import BackendError
from hoard_link.link import _strip_think
from tests.conftest import make_link


@pytest.mark.parametrize(
    "raw, text, reasoning",
    [
        ("plain answer", "plain answer", None),
        ("<think>a</think>answer", "answer", "a"),
        ("<think>cut off by max_tokens", "", "cut off by max_tokens"),
        ("Sure. <think>half a thought", "Sure.", "half a thought"),
        ("template-opened reasoning</think>\n\nanswer", "answer", "template-opened reasoning"),
        ("<think>\n\n</think>\n\nanswer", "answer", None),
        ("<THINK>x</THINK>y", "y", "x"),
        ("<think>a</think>mid<think>b", "mid", "a\n\nb"),
        ("", "", None),
        (None, "", None),
    ],
)
def test_strip_think_shapes(raw, text, reasoning):
    assert _strip_think(raw) == (text, reasoning)


def llm_cfg(url: str, api: str | None = None, cap: str = "llm") -> LinkConfig:
    return LinkConfig(capabilities={cap: CapabilityConfig(url=url, model="m", api=api)})


def chat_ok(content="ok", **message):
    return httpx.Response(200, json={"choices": [{"message": {"content": content, **message}}]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured",
    [
        "http://127.0.0.1:8081",
        "http://127.0.0.1:8081/",
        "http://127.0.0.1:8081/v1",
        "http://127.0.0.1:8081/v1/chat/completions",
    ],
)
async def test_openai_chat_accepts_base_or_full_url(configured):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return chat_ok()

    await make_link(handler, config=llm_cfg(configured)).chat([{"role": "user", "content": "hi"}])
    assert seen == ["/v1/chat/completions"]


@pytest.mark.asyncio
async def test_explicit_url_under_api_path_infers_ollama_dialect():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"message": {"content": "hola"}})

    cfg = llm_cfg("http://127.0.0.1:11434/api/chat")
    res = await make_link(handler, config=cfg).chat([{"role": "user", "content": "hi"}])
    assert res.text == "hola" and seen == ["/api/chat"]


@pytest.mark.asyncio
async def test_stale_explicit_url_raises_backend_error_not_httpx():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(BackendError) as exc:
        await make_link(handler, config=llm_cfg("http://127.0.0.1:8099")).chat([{"role": "user", "content": "x"}])
    assert exc.value.status == 0
    assert "unreachable" in str(exc.value)


@pytest.mark.asyncio
async def test_non_json_or_odd_chat_response_raises_backend_error():
    for resp in (httpx.Response(200, text="<html>"), httpx.Response(200, json={"choices": []})):
        with pytest.raises(BackendError):
            await make_link(lambda r, resp=resp: resp, config=llm_cfg("http://127.0.0.1:8081")).chat(
                [{"role": "user", "content": "x"}]
            )


@pytest.mark.asyncio
async def test_null_content_and_out_of_band_reasoning():
    link = make_link(lambda r: chat_ok(None, reasoning_content="why"), config=llm_cfg("http://127.0.0.1:8081"))
    res = await link.chat([{"role": "user", "content": "x"}])
    assert res.text == "" and res.reasoning == "why"


@pytest.mark.asyncio
async def test_ollama_thinking_field_and_response_format_mapping():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}", "thinking": "hmm"}})

    link = make_link(handler, config=llm_cfg("http://127.0.0.1:11434", api="ollama"))
    res = await link.chat([{"role": "user", "content": "x"}], response_format={"type": "json_object"})
    assert res.reasoning == "hmm"
    assert seen["format"] == "json"
    assert "keep_alive" not in seen

    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    await link.chat(
        [{"role": "user", "content": "x"}],
        response_format={"type": "json_schema", "json_schema": {"name": "s", "schema": schema}},
    )
    assert seen["format"] == schema


@pytest.mark.asyncio
async def test_png_image_gets_png_data_url():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return chat_ok()

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    link = make_link(handler, config=llm_cfg("http://127.0.0.1:8081", cap="vision"))
    await link.chat([{"role": "user", "content": "what"}], images=[png], capability="vision")
    part = seen["messages"][-1]["content"][1]
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_openai_embeddings_go_to_embeddings_endpoint_even_from_chat_url():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(
            200, json={"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}
        )

    cfg = llm_cfg("http://127.0.0.1:8082/v1/chat/completions", cap="embeddings")
    vecs = await make_link(handler, config=cfg).embed(["a", "b"])
    assert seen == ["/v1/embeddings"]
    assert vecs == [[1.0], [2.0]]  # ordered by index, not arrival


@pytest.mark.asyncio
async def test_ollama_embed_from_full_endpoint_url_is_not_doubled():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"embeddings": [[0.1]]})

    cfg = llm_cfg("http://127.0.0.1:11434/api/chat", cap="embeddings")
    await make_link(handler, config=cfg).embed(["a"])
    assert seen == ["/api/embed"]


def tts_cfg(command, provider=None):
    return LinkConfig(capabilities={"tts": CapabilityConfig(command=command, provider=provider)})


@pytest.mark.asyncio
async def test_tts_command_without_text_placeholder_reads_stdin():
    # Piper reads the text from stdin; without feeding it the process would
    # wait forever on an inherited console.
    code = "import sys; sys.stdout.buffer.write(b'<' + sys.stdin.buffer.read() + b'>')"
    link = make_link(lambda r: httpx.Response(404), config=tts_cfg([sys.executable, "-c", code]))
    assert await link.tts("hola ñ") == "<hola ñ>".encode("utf-8")


@pytest.mark.asyncio
async def test_tts_command_with_custom_provider_name_and_literal_braces():
    code = "import sys; open(sys.argv[2], 'wb').write(('{x}'+sys.argv[1]).encode())"
    cfg = tts_cfg([sys.executable, "-c", code, "{text}", "{out}"], provider="piper")
    link = make_link(lambda r: httpx.Response(404), config=cfg)
    assert await link.tts("hi") == b"{x}hi"


@pytest.mark.asyncio
async def test_tts_missing_command_is_backend_error():
    link = make_link(lambda r: httpx.Response(404), config=tts_cfg(["definitely-not-a-real-tts-binary"]))
    with pytest.raises(BackendError) as exc:
        await link.tts("hi")
    assert exc.value.status == 0 and "not found" in exc.value.body_excerpt
