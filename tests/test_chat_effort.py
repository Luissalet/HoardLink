"""Link.chat(effort=...): each server gets reasoning in its own words."""
from __future__ import annotations

import json

import httpx
import pytest

from hoard_link import reasoning
from hoard_link.config import CapabilityConfig, LinkConfig
from tests.conftest import make_link


def _openai(effort=None):
    return LinkConfig(capabilities={"llm": CapabilityConfig(
        url="http://127.0.0.1:8081/v1/chat/completions", model="m1", api="openai",
        provider="llamacpp", effort=effort)})


def _ollama():
    return LinkConfig(capabilities={"llm": CapabilityConfig(
        url="http://127.0.0.1:11434", model="q", api="ollama", provider="ollama")})


def _capture(bodies, reply=None):
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return reply(len(bodies)) if reply else httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}], "message": {"content": "ok"}})
    return handler


@pytest.mark.asyncio
async def test_no_effort_sends_nothing_new():
    bodies = []
    link = make_link(_capture(bodies), config=_openai())
    res = await link.chat([{"role": "user", "content": "hi"}], max_tokens=300)
    body = bodies[0]
    assert "chat_template_kwargs" not in body and "thinking_budget_tokens" not in body
    assert body["max_tokens"] == 300
    assert res.effort is None


@pytest.mark.asyncio
async def test_max_thinks_with_budget_and_room():
    bodies = []
    link = make_link(_capture(bodies), config=_openai())
    res = await link.chat([{"role": "user", "content": "grade this"}], max_tokens=1000, effort="xhigh")
    body = bodies[0]
    assert body["chat_template_kwargs"]["enable_thinking"] is True
    assert body["thinking_budget_tokens"] == 16384 == body["reasoning_budget"]
    assert body["max_tokens"] == 1000 + 16384
    assert res.effort == "max"


@pytest.mark.asyncio
async def test_off_turns_thinking_off():
    bodies = []
    link = make_link(_capture(bodies), config=_openai())
    await link.chat([{"role": "user", "content": "title"}], max_tokens=40, effort="off")
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert bodies[0]["max_tokens"] == 40


@pytest.mark.asyncio
async def test_configured_default_applies_when_caller_is_silent():
    bodies = []
    link = make_link(_capture(bodies), config=_openai(effort="high"))
    await link.chat([{"role": "user", "content": "x"}])
    assert bodies[0]["thinking_budget_tokens"] == 8192
    await link.chat([{"role": "user", "content": "x"}], effort="off")
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_ollama_think_and_num_predict():
    bodies = []
    link = make_link(_capture(bodies), config=_ollama())
    await link.chat([{"role": "user", "content": "x"}], max_tokens=500, effort="medium")
    assert bodies[0]["think"] is True
    assert bodies[0]["options"]["num_predict"] == 500 + 4096


@pytest.mark.asyncio
async def test_server_that_refuses_reasoning_gets_the_call_without_it():
    bodies = []

    def reply(n):
        if n == 1:
            return httpx.Response(400, text='{"error":"unknown field: thinking_budget_tokens"}')
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}]})

    link = make_link(_capture(bodies, reply), config=_openai())
    res = await link.chat([{"role": "user", "content": "x"}], effort="max")
    assert res.text == "fine"
    assert len(bodies) == 2
    assert "thinking_budget_tokens" not in bodies[1] and "chat_template_kwargs" not in bodies[1]


def test_levels_and_timeouts():
    assert reasoning.normalize("deep") == "max"
    assert reasoning.normalize("auto") is None
    assert reasoning.normalize("nonsense") is None
    assert reasoning.normalize(False) == "off"
    assert reasoning.timeout_for(120, "max") > 1500
    assert reasoning.timeout_for(120, None) == 120


def test_env_sets_the_default(tmp_path):
    cfg = LinkConfig.load(None, env={"HOARD_LLM_EFFORT": "max"})
    assert cfg.capability("llm").effort == "max"
    f = tmp_path / "backend.json"
    f.write_text(json.dumps({"capabilities": {"llm": {"effort": "low"}}}))
    assert LinkConfig.load(f, env={}).capability("llm").effort == "low"


_TEMPLATE_500 = ("\n------------\nWhile executing CallExpression at line 49, column 28 in source:\n...', 'low') %}\n"
                 "    {{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ '. Supported types "
                 "are xhigh (default), medium, and low.') }}\n")


@pytest.mark.asyncio
async def test_a_template_that_knows_other_effort_names_gets_the_nearest_one():
    bodies = []

    def reply(n):
        if bodies[-1].get("reasoning_effort") not in ("xhigh", "medium", "low"):
            return httpx.Response(500, json={"error": {"code": 500, "message": _TEMPLATE_500}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    link = make_link(_capture(bodies, reply), config=_openai())
    res = await link.chat([{"role": "user", "content": "judge"}], effort="high")
    assert [b.get("reasoning_effort") for b in bodies] == ["high", "xhigh"]
    assert bodies[1]["chat_template_kwargs"]["enable_thinking"] is True
    assert res.text == "ok"


@pytest.mark.asyncio
async def test_a_template_500_without_a_usable_name_drops_the_reasoning_fields():
    bodies = []

    def reply(n):
        if "reasoning_effort" in bodies[-1]:
            return httpx.Response(500, json={"error": {"message": "While executing CallExpression: raise_exception('Unexpected reasoning effort')"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    link = make_link(_capture(bodies, reply), config=_openai())
    await link.chat([{"role": "user", "content": "x"}], effort="high")
    assert len(bodies) == 2 and "reasoning_effort" not in bodies[1]


@pytest.mark.asyncio
async def test_an_unrelated_500_is_not_retried():
    bodies = []
    link = make_link(_capture(bodies, lambda n: httpx.Response(500, text="out of memory")), config=_openai())
    with pytest.raises(Exception):
        await link.chat([{"role": "user", "content": "x"}], effort="high")
    assert len(bodies) == 1


def test_supported_efforts_and_remap():
    assert reasoning.supported_efforts(_TEMPLATE_500) == ["xhigh", "medium", "low"]
    p = {"reasoning_effort": "high"}
    assert reasoning.remap_effort(p, ["xhigh", "medium", "low"]) and p["reasoning_effort"] == "xhigh"
    p = {"reasoning_effort": "minimal"}
    assert reasoning.remap_effort(p, ["xhigh", "medium", "low"]) and p["reasoning_effort"] == "low"
    p = {"reasoning_effort": "low"}
    assert not reasoning.remap_effort(p, ["xhigh", "medium", "low"])
    assert not reasoning.looks_like_reasoning_error(500, "out of memory")
    assert reasoning.looks_like_reasoning_error(500, _TEMPLATE_500)
