"""hoard_link.agentkit: tools, the catalogue, the capped result, AppError and the /api/agent router (FastAPI TestClient)."""

from __future__ import annotations

import json

import pytest

from hoard_link import agentkit, family
from hoard_link.agentkit import AppError, Tool, UnknownTool, ann, call_tool, cap_result, confirm, make_agent_router, tool_catalog, uncapped
from tests.commons.jsrun import normalise, run_js

pydantic = pytest.importorskip("pydantic")
fastapi = pytest.importorskip("fastapi")
from pydantic import BaseModel, Field  # noqa: E402


class Greet(BaseModel):
    name: str = Field(..., min_length=1)
    times: int = Field(1, ge=1, le=5)


class Ctx:
    def __init__(self):
        self.calls = []


def greet(ctx, args):
    ctx.calls.append(args.name)
    return {"greeting": " ".join([f"hello {args.name}"] * args.times)}


def listing(ctx, args):
    return {"items": [f"item-{i:04d}-" + "x" * 90 for i in range(1000)], "total": 1000}


def boom(ctx, args):
    raise RuntimeError("secret internals")


def lookup(ctx, args):
    raise KeyError("no such note")


def bad_value(ctx, args):
    raise ValueError("not a valid state")


def app_error(ctx, args):
    raise AppError("not_found", "No such document.", hint="Use docs_list.", details={"doc": "d_1"})


def as_list(ctx, args):
    return [1, 2, 3]


class Out(BaseModel):
    answer: int


def as_model(ctx, args):
    return Out(answer=42)


E = lambda: agentkit.Empty  # noqa: E731

TOOLS = [
    Tool("greet", "Say hello.", Greet, ann(read_only=True), greet),
    Tool("listing", "A long list.", E(), ann(read_only=True), listing, timeout_s=300),
    Tool("boom", "Fails.", E(), ann(), boom),
    Tool("lookup", "KeyError inside.", E(), ann(), lookup),
    Tool("bad_value", "ValueError inside.", E(), ann(), bad_value),
    Tool("app_error", "AppError inside.", E(), ann(destructive=True), app_error),
    Tool("as_list", "Returns a list.", E(), ann(read_only=True), as_list),
    Tool("as_model", "Returns a model.", None, ann(read_only=True), as_model),
]


# ------------------------------------------------------------------------------------------------ units

def test_ann_defaults_and_idempotent_follows_read_only():
    assert ann() == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
    assert ann(read_only=True)["idempotentHint"] is True
    assert ann(read_only=True, idempotent=False)["idempotentHint"] is False
    assert ann(destructive=True, open_world=True) == {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}


def test_empty_is_lazy_and_a_real_model():
    assert agentkit.Empty.model_validate({}) is not None
    assert agentkit.Empty.model_json_schema()["properties"] == {}
    with pytest.raises(AttributeError):
        agentkit.NoSuchThing


def test_app_error_status_by_code_and_body():
    assert AppError("not_found", "x").status == 404
    assert AppError("confirm_required", "x").status == 400
    assert AppError("whatever", "x").status == 400
    assert AppError("whatever", "x", status=418).status == 418
    assert AppError("conflict", "x", status=409).status == 409
    e = AppError("invalid", "Bad.", hint="Fix it.", details={"field": "a", "error": "ignored"})
    assert e.to_dict() == {"field": "a", "error": "Bad.", "code": "invalid", "hint": "Fix it."}
    assert AppError("x", "y").to_dict() == {"error": "y", "code": "x"}
    assert str(e) == "Bad."


def test_subclass_keeps_its_own_constructor():
    class KafkaError(AppError):
        def __init__(self, code, message, hint=""):
            super().__init__(code, message, hint=hint)
    assert KafkaError("not_found", "m", "h").to_dict() == {"error": "m", "code": "not_found", "hint": "h"}


def test_confirm():
    confirm(True, "this")
    with pytest.raises(AppError) as exc:
        confirm(False, "document d_1")
    assert exc.value.code == "confirm_required" and "document d_1" in exc.value.message and "confirm=true" in exc.value.hint


def test_catalog_shapes():
    cat = {t["name"]: t for t in tool_catalog(TOOLS)}
    assert set(cat["greet"]) == {"name", "description", "annotations", "inputSchema"}
    assert cat["greet"]["inputSchema"]["required"] == ["name"]
    assert cat["greet"]["annotations"]["readOnlyHint"] is True
    assert cat["listing"]["x-timeout-s"] == 300.0 and "x-timeout-s" not in cat["greet"]
    assert cat["as_model"]["inputSchema"] == {"type": "object", "properties": {}}
    raw = tool_catalog([Tool("t", "d", {"type": "object", "properties": {"a": {"type": "string"}}}, ann(), lambda c, a: a)])
    assert raw[0]["inputSchema"]["properties"]["a"] == {"type": "string"}
    json.dumps(cat)


def test_call_tool_validates_runs_wraps_and_caps():
    ctx = Ctx()
    assert call_tool(TOOLS, ctx, "greet", {"name": "Ada", "times": 2}) == {"greeting": "hello Ada hello Ada"}
    assert ctx.calls == ["Ada"]
    assert call_tool({t.name: t for t in TOOLS}, ctx, "greet", {"name": "Bo"}) == {"greeting": "hello Bo"}
    assert call_tool(TOOLS, ctx, "as_list", {}) == {"result": [1, 2, 3]}
    assert call_tool(TOOLS, ctx, "as_model", None) == {"answer": 42}
    with pytest.raises(pydantic.ValidationError):
        call_tool(TOOLS, ctx, "greet", {"name": ""})
    with pytest.raises(UnknownTool) as exc:
        call_tool(TOOLS, ctx, "nope", {})
    assert isinstance(exc.value, KeyError) and "nope" in str(exc.value)
    with pytest.raises(ValueError):
        call_tool(TOOLS, ctx, "greet", ["not", "an", "object"])
    capped = call_tool(TOOLS, ctx, "listing", {})
    assert len(capped["items"]) < 1000 and capped["truncated"]["original_lengths"] == {"items": 1000}
    assert len(call_tool(TOOLS, ctx, "listing", {}, cap=False)["items"]) == 1000
    with uncapped():
        assert len(call_tool(TOOLS, ctx, "listing", {})["items"]) == 1000
    assert len(call_tool(TOOLS, ctx, "listing", {})["items"]) < 1000          # the context manager is scoped


def test_call_tool_post_hook_runs_before_the_cap():
    seen = {}

    def mask(result, args):
        seen["args"] = args
        return {**result, "masked": True}

    out = call_tool(TOOLS, Ctx(), "greet", {"name": "Z"}, post=mask)
    assert out["masked"] is True and seen["args"].name == "Z"


def test_cap_result_semantics():
    small = {"a": [1, 2, 3]}
    assert cap_result(small) is small
    assert cap_result("text") == "text" and cap_result(None) is None
    data = {"items": ["x" * 100 for _ in range(1000)], "other": [1, 2], "total": 1000}
    out = cap_result(data)
    assert out["items"] == ["x" * 100] * 125 and out["other"] == [1, 2] and out["total"] == 1000
    assert out["truncated"] == {"reason": "result capped at ~20 KB", "original_lengths": {"items": 1000},
                                "hint": "Use limit or narrower filters to see the rest."}
    assert len(data["items"]) == 1000                                          # the input is not modified
    two = cap_result({"a": ["y" * 5000] * 5, "b": ["z" * 50] * 5}, limit=8000)
    assert len(two["a"]) == 1 and len(two["b"]) == 5 and two["truncated"]["original_lengths"] == {"a": 5}
    one = cap_result({"items": ["q" * 30_000]})
    assert len(one["items"]) == 1                                              # the last item always stays
    big = cap_result({"text": "w" * 50_000, "n": 1})
    assert len(big["text"]) < 11_000 and big["truncated"]["original_lengths"] == {"text": 50_000}
    listing = cap_result([f"line-{i}-" + "x" * 90 for i in range(1000)])
    assert set(listing) == {"result", "truncated"} and len(listing["result"]) < 1000
    assert cap_result([1, 2, 3]) == [1, 2, 3]


def test_cap_result_matches_the_node_twin():
    cases = [
        ({"items": ["x" * 100 for _ in range(300)], "other": [1, 2], "total": 300}, 8000),
        ({"a": ["y" * 500] * 5, "b": ["z" * 50] * 5}, 2000),
        ({"items": ["q" * 5_000]}, 2000),
        ({"text": "w" * 9_000, "n": 1}, 2000),
        ([f"line-{i}-" + "x" * 90 for i in range(100)], 3000),
        ({"small": [1, 2, 3]}, 20000),
        ({"unicode": ["ñandú-é" * 20] * 60}, 3000),
        ({"nested": {"k": [1, 2, 3]}, "rows": [{"id": i, "t": "r" * 40} for i in range(80)]}, 2500),
    ]
    got = run_js("express.js", [{"fn": "cap_result", "args": [data, limit]} for data, limit in cases])
    expected = [normalise(cap_result(data, limit)) for data, limit in cases]
    assert got == expected
    assert any("truncated" in e for e in expected) and expected[5] == {"small": [1, 2, 3]}


# ------------------------------------------------------------------------------------------------ router

TOKEN = "t" * 40


@pytest.fixture
def recorded(monkeypatch):
    events = []
    monkeypatch.setattr(family, "record_call", lambda tool, ok, ms=None, *, caller="", error="": events.append((tool, ok, caller, error)))
    return events


class KafkaLikeError(Exception):
    def __init__(self):
        super().__init__("custom")
        self.status = 409

    def to_dict(self):
        return {"error": "custom failure", "code": "custom"}


def build(**overrides):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    ctx = Ctx()
    kwargs = dict(tools_fn=lambda: tool_catalog(TOOLS), call_fn=lambda name, args: call_tool(TOOLS, ctx, name, args),
                  token_fn=lambda: TOKEN, instructions="Use the tools.", app_name="demo")
    kwargs.update(overrides)
    app.include_router(make_agent_router(**kwargs))
    return TestClient(app, raise_server_exceptions=False), ctx


def post(client, name, arguments=None, token=TOKEN, scheme="Bearer", **extra):
    headers = {"Authorization": f"{scheme} {token}"} if token is not None else {}
    return client.post("/api/agent/call", json={"name": name, "arguments": arguments or {}, **extra}, headers=headers)


def test_tools_route(recorded):
    client, _ = build()
    body = client.get("/api/agent/tools").json()
    assert body["instructions"] == "Use the tools." and body["app"] == "demo"
    assert [t["name"] for t in body["tools"]][:2] == ["greet", "listing"]


def test_token_checks(recorded):
    client, _ = build()
    assert post(client, "greet", {"name": "A"}).status_code == 200
    assert post(client, "greet", {"name": "A"}, scheme="bearer").status_code == 200        # case-insensitive scheme
    assert post(client, "greet", {"name": "A"}, scheme="BEARER").status_code == 200
    for r in (post(client, "greet", {"name": "A"}, token=None), post(client, "greet", {"name": "A"}, token="wrong"),
              post(client, "greet", {"name": "A"}, scheme="Basic")):
        assert r.status_code == 401 and r.json() == {"error": "Invalid MCP token.", "code": "unauthorized"}
    assert [e[0] for e in recorded] == ["greet"] * 3                                         # only authorised calls are audited
    empty, _ = build(token_fn=lambda: "")
    assert post(empty, "greet", {"name": "A"}, token="").status_code == 401                # an empty token never matches


def test_success_audit_and_caller(recorded):
    client, ctx = build()
    r = post(client, "greet", {"name": "Ada", "times": 2}, caller="faustus")
    assert r.json() == {"greeting": "hello Ada hello Ada"} and ctx.calls == ["Ada"]
    assert recorded == [("greet", True, "faustus", "")]


@pytest.mark.parametrize("name,args,status,code", [
    ("nope", {}, 404, "unknown_tool"),
    ("lookup", {}, 404, "not_found"),
    ("greet", {"name": ""}, 400, "invalid_arguments"),
    ("greet", {"times": 99}, 400, "invalid_arguments"),
    ("bad_value", {}, 400, "invalid"),
    ("app_error", {}, 404, "not_found"),
    ("boom", {}, 500, "internal"),
])
def test_error_mapping(recorded, name, args, status, code):
    client, _ = build()
    r = post(client, name, args)
    assert r.status_code == status and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["code"] == code and body["error"]
    assert recorded[-1][:2] == (name, False) and recorded[-1][3]


def test_error_bodies(recorded):
    client, _ = build()
    body = post(client, "greet", {"times": 99}).json()
    assert "name: " in body["error"] and "times: " in body["error"] and {i["loc"] for i in body["issues"]} == {"name", "times"}
    assert post(client, "app_error").json() == {"doc": "d_1", "error": "No such document.", "code": "not_found", "hint": "Use docs_list."}
    internal = post(client, "boom").json()
    assert "RuntimeError" in internal["error"]
    assert post(client, "nope").json()["error"] == "Unknown tool: nope"


def test_the_route_validates_its_own_body(recorded):
    client, _ = build()
    r = client.post("/api/agent/call", json={"arguments": {}}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 422                                                              # no error handler installed here: FastAPI's own


def test_custom_error_types_use_their_status_and_to_dict(recorded):
    def call(name, args):
        raise KafkaLikeError()
    client, _ = build(call_fn=call, error_types=(KafkaLikeError,))
    r = post(client, "x")
    assert r.status_code == 409 and r.json() == {"error": "custom failure", "code": "custom"}
    plain, _ = build(call_fn=call)
    assert post(plain, "x").status_code == 500


def test_async_call_fn_and_request_aware_callables(recorded):
    seen = {}

    async def call(name, args, request):
        seen["state"] = request.app.state.marker
        return {"async": name}

    def token(request):
        return request.app.state.token

    def tools(request):
        return [{"name": "t", "description": "d", "inputSchema": {"type": "object"}, "annotations": {}}]

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.state.marker, app.state.token = "services", "k" * 40
    app.include_router(make_agent_router(tools_fn=tools, call_fn=call, token_fn=token, instructions=lambda request: f"for {request.url.path}",
                                         app_name="demo"))
    client = TestClient(app)
    assert client.get("/api/agent/tools").json()["instructions"] == "for /api/agent/tools"
    assert post(client, "t", token="k" * 40).json() == {"async": "t"} and seen["state"] == "services"


def test_sync_tools_run_off_the_event_loop(recorded):
    import asyncio
    import threading
    main = threading.get_ident()
    where = {}

    def call(name, args):
        where["thread"] = threading.get_ident()
        try:
            asyncio.get_running_loop()
            where["loop"] = True
        except RuntimeError:
            where["loop"] = False
        return {"ok": True}

    client, _ = build(call_fn=call)
    assert post(client, "x").status_code == 200
    assert where["loop"] is False and where["thread"] != main


def test_a_tool_result_that_is_a_pydantic_model_or_datetime_serialises(recorded):
    import datetime
    client, _ = build(call_fn=lambda name, args: {"when": datetime.datetime(2026, 1, 2, 3, 4, 5), "model": Out(answer=1)})
    assert post(client, "x").json() == {"when": "2026-01-02T03:04:05", "model": {"answer": 1}}
