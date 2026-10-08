"""Hoard Link 0.6: the hub serves chat/vision to every app (POST /api/link/chat, GET /api/link/status),
its agent tools, and the two clients (Python ``family.chat`` and Node ``chat()``)."""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hoard_link.errors import BackendError, Unavailable
from hoard_link.hub import tools
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.linkchat import (BadRequest, Gate, LinkService, parse_json_loose, parse_request,
                                     with_json_instruction)
from hoard_link.hub.server import make_server
from hoard_link.types import ChatResult, Resolution, Usage

from .conftest import free_port
from .test_hub_and_server import _http

JS = Path(__file__).resolve().parents[2] / "js" / "hoard-link.js"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image-bytes").decode()


# ---- fakes ----------------------------------------------------------------------------

class FakeLink:
    """Stands in for hoard_link.Link: records calls, answers like a model."""

    def __init__(self, app: str, script=None):
        self.app = app
        self.calls: list[dict] = []
        self.script = script          # async (kwargs) -> text | raises
        self.resolve_calls = 0
        self.active = 0
        self.peak = 0

    async def chat(self, messages, images=None, max_tokens=None, temperature=None, capability="llm",
                   response_format=None, effort=None):
        kw = dict(messages=messages, images=images, max_tokens=max_tokens, temperature=temperature,
                  capability=capability, response_format=response_format, effort=effort)
        self.calls.append(kw)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            text = await self.script(kw) if self.script else "hola"
        finally:
            self.active -= 1
        return ChatResult(text=text, model="fake-model", provider="fakeserver", usage=Usage(1, 2, 3), elapsed_ms=12.0,
                          effort=effort)

    async def resolve(self, capability):
        self.resolve_calls += 1
        if capability == "vision":
            return Resolution(capability, None, None, None, None, "unavailable", "no vision model resident", {})
        if capability == "tts":
            raise RuntimeError("probe blew up")
        return Resolution(capability, "fakeserver", "http://127.0.0.1:1", f"{capability}-model", "openai", "resolved",
                          f"{capability} -> fakeserver", {})


@pytest.fixture
def lhub(tmp_path):
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(tmp_path / "apps")], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False)
    h = Hub(cfg)
    links: dict[str, FakeLink] = {}

    def factory(app: str) -> FakeLink:
        links.setdefault(app, FakeLink(app))
        return links[app]

    h.link.close()
    h.link = LinkService(cfg, emit=lambda t, d: h.events.emit(t, d, source="hub"), link_factory=factory)
    h.fake_links = links
    server = make_server(h, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield h, cfg.url
    server.shutdown()
    h.close()


def _auth(hub) -> dict:
    return {"Authorization": "Bearer " + hub.token}


def _msgs(text="hola") -> list[dict]:
    return [{"role": "user", "content": text}]


# ---- JSON parsing -----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('  [1, 2, 3]  ', [1, 2, 3]),
    ('```json\n{"a": {"b": [1, 2]}}\n```', {"a": {"b": [1, 2]}}),
    ('```\n{"a": 1}\n```', {"a": 1}),
    ('Claro, aquí tienes: {"nombre": "Ana", "nota": "con } llave"} y nada más.', {"nombre": "Ana", "nota": "con } llave"}),
    ('Resultado: [{"x": 1}, {"x": 2}] fin', [{"x": 1}, {"x": 2}]),
    ('{not json} pero luego {"ok": true}', {"ok": True}),
    ('texto {"a": "comilla \\" escapada"} fin', {"a": 'comilla " escapada'}),
    ('42', 42),
])
def test_parse_json_loose_finds_the_document(text, expected):
    assert parse_json_loose(text) == (expected, True)


@pytest.mark.parametrize("text", ["", "   ", "no json aquí", "{sin cerrar", '{"a": }', None, 5])
def test_parse_json_loose_reports_nothing_found(text):
    assert parse_json_loose(text) == (None, False)


def test_json_instruction_goes_into_the_system_message():
    out = with_json_instruction([{"role": "system", "content": "Eres útil."}, {"role": "user", "content": "x"}],
                                {"type": "object", "properties": {"a": {"type": "string"}}})
    assert out[0]["content"].startswith("Eres útil.") and "JSON Schema" in out[0]["content"] and '"properties"' in out[0]["content"]
    assert len(out) == 2
    out = with_json_instruction([{"role": "user", "content": "x"}], None)
    assert out[0]["role"] == "system" and "JSON" in out[0]["content"] and "Schema" not in out[0]["content"]


# ---- request validation -------------------------------------------------------------------

def test_parse_request_defaults_and_caps():
    req = parse_request({"messages": _msgs(), "timeout_s": 99999})
    assert req.capability == "llm" and req.timeout_s == 1800.0 and not req.json_mode and req.images == []
    assert parse_request({"messages": _msgs()}).timeout_s == 300.0
    assert parse_request({"messages": _msgs(), "timeout_s": 0.2}).timeout_s == 1.0
    req = parse_request({"messages": _msgs(), "json": {"type": "object"}, "effort": "HIGH", "max_tokens": 50, "temperature": 0})
    assert req.json_mode and req.schema == {"type": "object"} and req.effort == "high" and req.max_tokens == 50 and req.temperature == 0.0
    assert parse_request({"messages": _msgs(), "effort": "auto"}).effort is None


def test_parse_request_images_decode_and_need_vision():
    req = parse_request({"capability": "vision", "messages": _msgs("qué ves"), "images": [PNG, "data:image/png;base64," + PNG]})
    assert [i[:4] for i in req.images] == [b"\x89PNG", b"\x89PNG"]


@pytest.mark.parametrize("body,fragment", [
    (None, "JSON object"), ([], "JSON object"), ({}, "messages"), ({"messages": []}, "messages"),
    ({"messages": ["x"]}, "messages[0]"), ({"messages": [{"role": "bot", "content": "x"}]}, "role"),
    ({"messages": [{"role": "user", "content": 5}]}, "content"),
    ({"messages": [{"role": "system", "content": "x"}]}, "user message"),
    ({"messages": _msgs(), "capability": "tts"}, "capability"),
    ({"messages": _msgs(), "images": [PNG]}, "only accepted with capability 'vision'"),
    ({"messages": _msgs(), "capability": "vision"}, "at least one image"),
    ({"messages": _msgs(), "capability": "vision", "images": ["###"]}, "base64"),
    ({"messages": _msgs(), "capability": "vision", "images": [""]}, "base64 string"),
    ({"messages": _msgs(), "json": "yes"}, "json"), ({"messages": _msgs(), "effort": "turbo"}, "effort"),
    ({"messages": _msgs(), "max_tokens": 0}, "max_tokens"), ({"messages": _msgs(), "max_tokens": "9"}, "max_tokens"),
    ({"messages": _msgs(), "temperature": 3}, "temperature"), ({"messages": _msgs(), "timeout_s": -1}, "timeout_s"),
])
def test_parse_request_rejects(body, fragment):
    with pytest.raises(BadRequest) as exc:
        parse_request(body)
    assert fragment in str(exc.value)


# ---- the gate ------------------------------------------------------------------------------

def test_gate_is_fifo_and_bounded():
    gate = Gate(1)
    assert gate.acquire(1) is True
    order: list[int] = []

    def waiter(n: int) -> None:
        if gate.acquire(5):
            order.append(n)
            time.sleep(0.02)
            gate.release()

    threads = []
    for n in range(3):
        t = threading.Thread(target=waiter, args=(n,))
        t.start()
        threads.append(t)
        time.sleep(0.05)               # arrival order 0, 1, 2
    assert gate.waiting == 3 and gate.active == 1
    gate.release()
    for t in threads:
        t.join(5)
    assert order == [0, 1, 2] and gate.active == 0 and gate.waiting == 0


def test_gate_timeout_leaves_the_line():
    gate = Gate(1)
    assert gate.acquire(1)
    t0 = time.monotonic()
    assert gate.acquire(0.2) is False and time.monotonic() - t0 < 2
    assert gate.waiting == 0 and gate.active == 1
    gate.release()
    assert gate.acquire(0.2) is True


# ---- the endpoint ----------------------------------------------------------------------------

def test_chat_needs_a_family_token(lhub):
    hub, url = lhub
    body = {"messages": _msgs()}
    assert _http(url + "/api/link/chat", body)[0] == 401
    assert _http(url + "/api/link/chat", body, headers={"Authorization": "Bearer nope"})[0] == 401
    assert hub.fake_links == {}                       # nothing reached a model
    assert hub.events.query(type="hub.link.chat") == []
    status, res = _http(url + "/api/link/chat", body, headers=_auth(hub))
    assert status == 200 and res["ok"] and res["text"] == "hola"


def test_chat_accepts_an_apps_own_token_and_records_who(lhub, tmp_path):
    hub, url = lhub
    folder = Path(hub.config.roots[0]) / "Notes Hoard"
    from .conftest import write_manifest
    write_manifest(folder, "notes", free_port(), service="notes-hoard")
    (folder / "data").mkdir()
    (folder / "data" / "mcp-token").write_text("notes-token-xyz", encoding="utf-8")
    hub.rescan()
    status, res = _http(url + "/api/link/chat", {"messages": _msgs("resume esto"), "effort": "low", "max_tokens": 64},
                        headers={"Authorization": "Bearer notes-token-xyz"})
    assert status == 200 and res["ok"] is True
    assert res["model"] == "fake-model" and res["provider"] == "fakeserver" and res["ms"] == 12
    assert res["usage"] == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3} and res["json"] is None
    assert list(hub.fake_links) == ["notes"]           # one Link per calling app (its own lease owner)
    call = hub.fake_links["notes"].calls[0]
    assert call["effort"] == "low" and call["max_tokens"] == 64 and call["capability"] == "llm" and call["response_format"] is None
    ev = hub.events.query(type="hub.link.chat")[0]
    assert ev["source"] == "hub" and ev["data"]["app"] == "notes" and ev["data"]["ok"] is True
    assert ev["data"]["capability"] == "llm" and ev["data"]["model"] == "fake-model" and isinstance(ev["data"]["ms"], int)
    assert "resume esto" not in json.dumps(ev)           # contents never reach the bus


def test_chat_bad_requests_are_400(lhub):
    hub, url = lhub
    status, res = _http(url + "/api/link/chat", {"messages": []}, headers=_auth(hub))
    assert status == 400 and res["error"] == "bad_request" and "messages" in res["detail"]
    conn = http.client.HTTPConnection(url.split("//")[1])
    conn.request("POST", "/api/link/chat", body=b"{not json", headers={"Authorization": "Bearer " + hub.token})
    resp = conn.getresponse()
    assert resp.status == 400 and json.loads(resp.read())["error"] == "bad_request"
    ev = hub.events.query(type="hub.link.chat")[0]
    assert ev["data"]["ok"] is False and ev["data"]["error"] == "bad_request"


def test_chat_refuses_oversized_bodies_before_reading_them(lhub):
    hub, url = lhub
    conn = http.client.HTTPConnection(url.split("//")[1])
    conn.putrequest("POST", "/api/link/chat")
    conn.putheader("Authorization", "Bearer " + hub.token)
    conn.putheader("Content-Length", str(13 * 1024 * 1024))
    conn.endheaders()                                  # no body is ever sent
    resp = conn.getresponse()
    assert resp.status == 413 and json.loads(resp.read())["error"] == "too_large"


def test_no_model_is_503(lhub):
    hub, url = lhub

    async def nothing(kw):
        raise Unavailable("llm", ["no llm server is running", "Faustus not reachable"])

    hub.link._link("hub").script = nothing
    status, res = _http(url + "/api/link/chat", {"messages": _msgs()}, headers=_auth(hub))
    assert status == 503 and res == {"ok": False, "error": "no_model", "detail": "no llm server is running; Faustus not reachable"}
    ev = hub.events.query(type="hub.link.chat")[0]
    assert ev["data"]["ok"] is False and ev["data"]["error"] == "no_model"


def test_gpu_busy_is_503_with_its_own_word(lhub):
    hub, url = lhub

    async def busy(kw):
        raise Unavailable("llm", ["GPU busy: waited 300s for a lease"])

    hub.link._link("hub").script = busy
    status, res = _http(url + "/api/link/chat", {"messages": _msgs()}, headers=_auth(hub))
    assert status == 503 and res["error"] == "gpu_busy"


def test_backend_error_is_502_and_a_backend_timeout_is_504(lhub):
    hub, url = lhub
    box = {"exc": BackendError("llamacpp", 500, "model crashed")}

    async def boom(kw):
        raise box["exc"]

    hub.link._link("hub").script = boom
    status, res = _http(url + "/api/link/chat", {"messages": _msgs()}, headers=_auth(hub))
    assert status == 502 and res["error"] == "backend_error" and res["backend_status"] == 500 and "model crashed" in res["detail"]
    box["exc"] = BackendError("llamacpp", 0, "ReadTimeout at 127.0.0.1:8081: timed out")
    status, res = _http(url + "/api/link/chat", {"messages": _msgs()}, headers=_auth(hub))
    assert status == 504 and res["error"] == "timeout"


def test_slow_model_is_504_inside_the_callers_timeout(lhub):
    hub, url = lhub
    cancelled = threading.Event()

    async def slow(kw):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    hub.link._link("hub").script = slow
    t0 = time.monotonic()
    status, res = _http(url + "/api/link/chat", {"messages": _msgs(), "timeout_s": 1}, headers=_auth(hub))
    assert status == 504 and res["error"] == "timeout" and time.monotonic() - t0 < 6
    assert cancelled.wait(3)                           # the call was cancelled, not left running
    assert hub.link.gate.active == 0                   # and its place in the gate was given back


def test_json_true_asks_for_a_json_object_and_parses_a_fenced_answer(lhub):
    hub, url = lhub

    async def fenced(kw):
        return 'Aquí está:\n```json\n{"titulo": "Informe", "etiquetas": ["a", "b"]}\n```'

    hub.link._link("hub").script = fenced
    status, res = _http(url + "/api/link/chat", {"messages": [{"role": "system", "content": "Sé breve."}] + _msgs(), "json": True},
                        headers=_auth(hub))
    assert status == 200 and res["json"] == {"titulo": "Informe", "etiquetas": ["a", "b"]} and "json_error" not in res
    assert "```" in res["text"]                        # the evidence (the raw answer) is kept
    call = hub.fake_links["hub"].calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["role"] == "system" and call["messages"][0]["content"].startswith("Sé breve.")
    assert "JSON" in call["messages"][0]["content"]


def test_json_schema_is_passed_and_retried_without_it_when_the_server_rejects_it(lhub):
    hub, url = lhub
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}

    async def picky(kw):
        if kw["response_format"] is not None:
            raise BackendError("openai", 400, "response_format json_schema is not supported")
        return '{"n": 3}'

    hub.link._link("hub").script = picky
    status, res = _http(url + "/api/link/chat", {"messages": _msgs(), "json": schema}, headers=_auth(hub))
    assert status == 200 and res["json"] == {"n": 3}
    first, second = hub.fake_links["hub"].calls
    assert first["response_format"]["type"] == "json_schema" and first["response_format"]["json_schema"]["schema"] == schema
    assert second["response_format"] is None and '"required":["n"]' in second["messages"][0]["content"]
    # a 500 is not retried
    hub.fake_links["hub"].calls.clear()

    async def dies(kw):
        raise BackendError("openai", 500, "boom")

    hub.fake_links["hub"].script = dies
    assert _http(url + "/api/link/chat", {"messages": _msgs(), "json": schema}, headers=_auth(hub))[0] == 502
    assert len(hub.fake_links["hub"].calls) == 1


def test_unparseable_json_answer_is_ok_with_null_json_and_says_so(lhub):
    hub, url = lhub

    async def prose(kw):
        return "No puedo darte JSON, lo siento."

    hub.link._link("hub").script = prose
    status, res = _http(url + "/api/link/chat", {"messages": _msgs(), "json": True}, headers=_auth(hub))
    assert status == 200 and res["ok"] and res["json"] is None and res["text"].startswith("No puedo") and "json_error" in res


def test_vision_decodes_images_and_needs_the_vision_capability(lhub):
    hub, url = lhub
    status, res = _http(url + "/api/link/chat", {"capability": "vision", "messages": _msgs("describe"), "images": [PNG]},
                        headers=_auth(hub))
    assert status == 200 and res["ok"]
    call = hub.fake_links["hub"].calls[0]
    assert call["capability"] == "vision" and call["images"] == [base64.b64decode(PNG)]
    status, res = _http(url + "/api/link/chat", {"messages": _msgs(), "images": [PNG]}, headers=_auth(hub))
    assert status == 400 and "vision" in res["detail"]


def test_concurrency_is_capped_and_the_rest_queue(lhub):
    hub, url = lhub
    assert hub.link.gate.limit == 2                    # the default of link_chat_concurrency
    release = threading.Event()

    async def work(kw):
        # Hold the two active slots until the rest are visibly queued. A fixed
        # sleep + queued_ms>=500 races under suite load (historical 430 < 500).
        await asyncio.get_running_loop().run_in_executor(None, release.wait)
        return "ok"

    hub.link._link("hub").script = work
    results: list = []

    def ask() -> None:
        results.append(_http(url + "/api/link/chat", {"messages": _msgs(), "timeout_s": 30}, headers=_auth(hub)))

    threads = [threading.Thread(target=ask) for _ in range(5)]
    for t in threads:
        t.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (hub.link.gate.active == 2 and hub.link.gate.waiting >= 3):
            time.sleep(0.02)
        assert hub.link.gate.active == 2 and hub.link.gate.waiting >= 3
    finally:
        # Always unblock FakeLink holders so a failed wait does not leave pytest hanging.
        release.set()
        for t in threads:
            t.join(30)
    assert [s for s, _ in results] == [200] * 5
    assert hub.fake_links["hub"].peak == 2             # never more than two model calls at once
    assert max(r["queued_ms"] for _, r in results) > 0  # at least one caller waited behind the gate


def test_a_caller_that_waits_too_long_in_the_queue_gets_504(tmp_path):
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "d"), roots=[], icon_dirs=[], link_chat_concurrency=1,
                    jobs_enabled=False)
    link = FakeLink("x")

    async def hold(kw):
        await asyncio.sleep(2)
        return "late"

    link.script = hold
    svc = LinkService(cfg, link_factory=lambda app: link)
    first = threading.Thread(target=lambda: svc.chat("a", {"messages": _msgs(), "timeout_s": 30}))
    first.start()
    time.sleep(0.3)
    status, res = svc.chat("b", {"messages": _msgs(), "timeout_s": 1})
    assert status == 504 and res["error"] == "timeout" and "queue" in res["detail"]
    first.join(10)
    assert len(link.calls) == 1                        # the one that gave up never reached the model
    svc.close()


# ---- status ------------------------------------------------------------------------------------

def test_status_reports_each_capability_and_caches(lhub):
    hub, url = lhub
    status, res = _http(url + "/api/link/status")
    assert status == 200 and res["ok"] is True
    assert res["llm"] == {"available": True, "model": "llm-model", "provider": "fakeserver", "reason": "llm -> fakeserver"}
    assert res["vision"]["available"] is False and res["vision"]["model"] is None and "no vision model" in res["vision"]["reason"]
    assert res["embed"]["available"] is True and res["embed"]["model"] == "embeddings-model"
    assert res["tts"]["available"] is False and "probe blew up" in res["tts"]["reason"]
    assert res["chat"] == {"concurrency": 2, "active": 0, "queued": 0}
    link = hub.fake_links["hoard-hub"]
    before = link.resolve_calls
    assert _http(url + "/api/link/status")[0] == 200 and link.resolve_calls == before       # cached for a few seconds
    assert _http(url + "/api/link/status?force=1")[0] == 200 and link.resolve_calls == before + 4


def test_status_cache_expires(tmp_path):
    cfg = HubConfig(port=1, data_dir=str(tmp_path / "d"), roots=[], icon_dirs=[], jobs_enabled=False)
    clock = {"t": 1000.0}
    link = FakeLink("x")
    svc = LinkService(cfg, link_factory=lambda app: link, now=lambda: clock["t"])
    svc.status()
    n = link.resolve_calls
    clock["t"] += 2
    svc.status()
    assert link.resolve_calls == n
    clock["t"] += 10
    svc.status()
    assert link.resolve_calls == 2 * n
    svc.close()


# ---- the agent tools ------------------------------------------------------------------------------

def test_link_tools_are_in_the_catalogue():
    cat = {t["name"]: t for t in tools.catalogue()}
    for name in ("hub_link_status", "hub_link_chat"):
        first = cat[name]["description"].split("\n")[0]
        assert len(first) <= 110, (name, len(first))
        assert "Sinónimos:" in cat[name]["description"]
    assert cat["hub_link_status"]["annotations"] == {"readOnlyHint": True}
    assert cat["hub_link_chat"]["annotations"].get("readOnlyHint") is False
    assert cat["hub_link_chat"]["annotations"].get("destructiveHint") is False


def test_link_tools_through_the_agent_route(lhub):
    hub, url = lhub
    status, res = _http(url + "/api/agent/call", {"tool": "hub_link_status", "arguments": {}}, headers=_auth(hub))
    assert status == 200 and res["result"]["llm"]["available"] is True
    status, res = _http(url + "/api/agent/call",
                        {"tool": "hub_link_chat", "arguments": {"system": "Responde corto.", "prompt": "¿Hola?", "effort": "off"}},
                        headers=_auth(hub))
    assert status == 200 and res["result"]["ok"] and res["result"]["text"] == "hola"
    call = hub.fake_links["hub-tool"].calls[0]
    assert [m["role"] for m in call["messages"]] == ["system", "user"] and call["effort"] == "off"
    # a refusal comes back as ok:false with the same words as the endpoint
    status, res = _http(url + "/api/agent/call", {"tool": "hub_link_chat", "arguments": {"prompt": "x", "capability": "vision"}},
                        headers=_auth(hub))
    assert status == 400 and res["result"]["error"] == "bad_request"


# ---- the real Link, end to end through backend.json ---------------------------------------------------

class _FakeLlama(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):  # noqa: D102
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n))
        type(self).seen.append(payload)
        if payload.get("response_format", {}).get("type") == "json_schema" and "reject-schema" in self.path:
            self.send_response(400)
            self.end_headers()
            return
        body = json.dumps({"choices": [{"message": {"content": '{"ok": true, "n": 7}'}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_real_link_resolved_from_the_hubs_backend_json(tmp_path):
    port = free_port()
    _FakeLlama.seen = []
    srv = HTTPServer(("127.0.0.1", port), _FakeLlama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = tmp_path / "data"
    data.mkdir()
    (data / "backend.json").write_text(json.dumps({"capabilities": {
        "llm": {"url": f"http://127.0.0.1:{port}/v1/chat/completions", "model": "qwen-test", "api": "openai", "provider": "llamacpp"},
        "vision": {"url": f"http://127.0.0.1:{port}/v1/chat/completions", "model": "qwen-vl-test", "api": "openai"}}}), encoding="utf-8")
    cfg = HubConfig(port=free_port(), data_dir=str(data), roots=[], icon_dirs=[], faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False)
    hub = Hub(cfg)
    server = make_server(hub, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lc = hub.link.link_config("notes")
        assert lc.app == "notes" and lc.hub_url == cfg.url and lc.faustus_urls == ("http://127.0.0.1:1",)
        status, res = _http(cfg.url + "/api/link/chat", {"messages": [{"role": "system", "content": "S"}] + _msgs("dame json"),
                                                           "json": True, "effort": "off", "max_tokens": 100, "temperature": 0.1},
                            headers=_auth(hub))
        assert status == 200, res
        assert res["json"] == {"ok": True, "n": 7} and res["model"] == "qwen-test" and res["provider"] == "llamacpp"
        assert res["usage"]["total_tokens"] == 9
        sent = _FakeLlama.seen[-1]
        assert sent["model"] == "qwen-test" and sent["response_format"] == {"type": "json_object"} and sent["max_tokens"] >= 100
        assert sent["messages"][-1] == {"role": "user", "content": "dame json"}
        status, res = _http(cfg.url + "/api/link/chat", {"capability": "vision", "messages": _msgs("qué es"), "images": [PNG]},
                            headers=_auth(hub))
        assert status == 200 and res["model"] == "qwen-vl-test"
        part = _FakeLlama.seen[-1]["messages"][-1]["content"]
        assert part[0] == {"type": "text", "text": "qué es"} and part[1]["image_url"]["url"].startswith("data:image/png;base64,")
        st = _http(cfg.url + "/api/link/status")[1]
        assert st["llm"]["available"] and st["llm"]["model"] == "qwen-test" and st["llm"]["provider"] == "llamacpp"
        assert st["vision"]["model"] == "qwen-vl-test"
    finally:
        server.shutdown()
        hub.close()
        srv.shutdown()


def test_real_link_with_nothing_available_is_503_no_model(tmp_path):
    """The real Link over a transport that refuses every connection (no server, no Faustus): the hub answers
    no_model with the reasons it collected, and the status says why each capability is unavailable."""
    from hoard_link.link import Link

    from ..conftest import make_client, refused

    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[], icon_dirs=[], faustus_urls=["http://127.0.0.1:1"],
                    jobs_enabled=False)
    svc = LinkService(cfg, link_factory=lambda app: Link(svc.link_config(app), client=make_client(refused)))
    status, res = svc.chat("notes", {"messages": _msgs(), "timeout_s": 30})
    assert status == 503 and res["ok"] is False and res["error"] == "no_model" and "Faustus" in res["detail"]
    st = svc.status(force=True)
    assert st["ok"] and st["llm"]["available"] is False and st["llm"]["model"] is None and st["llm"]["reason"]
    assert all(st[k]["available"] is False for k in ("llm", "vision", "embed", "tts"))
    status, res = svc.chat("notes", {"capability": "vision", "messages": _msgs(), "images": [PNG]})
    assert status == 503 and res["error"] == "no_model"
    svc.close()


def test_link_chat_concurrency_setting(tmp_path):
    cfg = HubConfig.load(None, env={"HOARD_HUB_DATA_DIR": str(tmp_path), "HOARD_HUB_LINK_CHAT_CONCURRENCY": "4"})
    assert cfg.link_chat_concurrency == 4
    (tmp_path / "hub.json").write_text(json.dumps({"link_chat_concurrency": 3}), encoding="utf-8")
    assert HubConfig.load(None, env={"HOARD_HUB_DATA_DIR": str(tmp_path)}).link_chat_concurrency == 3
    (tmp_path / "hub.json").write_text(json.dumps({"link_chat_concurrency": 0}), encoding="utf-8")
    assert HubConfig.load(None, env={"HOARD_HUB_DATA_DIR": str(tmp_path)}).link_chat_concurrency == 1
    assert HubConfig.load(None, env={"HOARD_HUB_DATA_DIR": str(tmp_path / "none")}).link_chat_concurrency == 2


# ---- the Python client --------------------------------------------------------------------------------

def test_python_client_chat_and_status(lhub, tmp_path, monkeypatch):
    hub, url = lhub
    from hoard_link import family
    monkeypatch.delenv("HOARD_HUB_URL", raising=False)
    tok = tmp_path / "tok"
    tok.write_text(hub.token, encoding="utf-8")
    family.configure("pyapp", None, token_file=str(tok), hub=url)

    async def script(kw):
        return '{"a": 1}' if kw["response_format"] else "texto"

    hub.link._link("hub").script = script
    r = family.chat(_msgs("hola"), effort="off", max_tokens=32)
    assert r["ok"] and r["text"] == "texto" and r["json"] is None and r["model"] == "fake-model" and r["provider"] == "fakeserver"
    assert r["error"] is None
    r = family.chat(_msgs("dame json"), json=True)
    assert r["ok"] and r["json"] == {"a": 1}
    r = family.chat(_msgs("mira"), capability="vision", images=[b"\x89PNGbytes", PNG])
    assert r["ok"]
    assert hub.fake_links["hub"].calls[-1]["images"][0] == b"\x89PNGbytes"
    bad = family.chat([])
    assert bad["ok"] is False and bad["error"] == "http_400" and bad["code"] == "bad_request" and bad["text"] == ""
    st = family.link_status()
    assert st["ok"] and st["llm"]["available"] and st["vision"]["available"] is False

    async def none(kw):
        raise Unavailable("llm", ["nothing resident"])

    hub.fake_links["hub"].script = none
    r = family.chat(_msgs())
    assert r["ok"] is False and r["error"] == "no_model" and "nothing resident" in r["detail"]
    # a token the hub does not know
    tok.write_text("wrong", encoding="utf-8")
    r = family.chat(_msgs())
    assert r["ok"] is False and r["error"] == "http_401" and "refused" in r["detail"]


def test_python_client_never_raises_when_the_hub_is_down_or_slow(monkeypatch):
    from hoard_link import family
    family.configure("pyapp", None, hub=f"http://127.0.0.1:{free_port()}")
    r = family.chat(_msgs(), timeout=1)
    assert r["ok"] is False and r["error"] == "hub_down" and r["text"] == "" and r["json"] is None
    assert family.link_status()["error"] == "hub_down"
    monkeypatch.setattr(family, "fetch_detailed", lambda *a, **k: (None, None, "timeout"))
    assert family.chat(_msgs(), timeout=1)["error"] == "timeout"
    assert family.link_status()["error"] == "timeout"


# ---- the Node client -----------------------------------------------------------------------------------

JS_TEST = r"""
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
const family = await import(process.argv[2]);
const seen = [];
let mode = "ok";
const srv = http.createServer((req, res) => {
  let raw = "";
  req.on("data", (c) => (raw += c));
  req.on("end", () => {
    const body = raw ? JSON.parse(raw) : null;
    seen.push({ url: req.url, auth: req.headers.authorization, body });
    const send = (st, obj) => { res.writeHead(st, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
    if (req.url.startsWith("/api/link/status")) return send(200, { ok: true, llm: { available: true, model: "m", provider: "p", reason: "r" } });
    if (mode === "hang") return;
    if (mode === "no_model") return send(503, { ok: false, error: "no_model", detail: "nothing resident" });
    if (mode === "busy") return send(503, { ok: false, error: "gpu_busy", detail: "waited" });
    if (mode === "timeout") return send(504, { ok: false, error: "timeout", detail: "slow" });
    if (mode === "401") return send(401, { ok: false, error: "token" });
    return send(200, { ok: true, text: "hola", json: body.json ? { a: 1 } : null, model: "m", provider: "p", ms: 5, usage: { total_tokens: 3 } });
  });
});
await new Promise((r) => srv.listen(0, "127.0.0.1", r));
const port = srv.address().port;
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "hl-"));
fs.writeFileSync(path.join(dir, "mcp-token"), "app-token");
family.configure({ app: "jsapp", dataDir: dir, hub: `http://127.0.0.1:${port}` });
const out = {};
out.ok = await family.chat({ messages: [{ role: "user", content: "hi" }], json: { type: "object" }, effort: "low", maxTokens: 20, temperature: 0.1 });
out.sent = seen[seen.length - 1];
out.vision = await family.chat({ capability: "vision", messages: [{ role: "user", content: "x" }], images: [Buffer.from("png-bytes"), "QUJD"] });
out.visionSent = seen[seen.length - 1].body.images;
mode = "no_model"; out.no_model = await family.chat({ messages: [{ role: "user", content: "hi" }] });
mode = "busy"; out.busy = await family.chat({ messages: [{ role: "user", content: "hi" }] });
mode = "timeout"; out.timeout504 = await family.chat({ messages: [{ role: "user", content: "hi" }] });
mode = "401"; out.refused = await family.chat({ messages: [{ role: "user", content: "hi" }] });
mode = "hang"; out.hang = await family.chat({ messages: [{ role: "user", content: "hi" }], timeoutMs: 200, graceMs: 100 });
out.status = await family.linkStatus({ force: true });
out.statusUrl = seen.filter((s) => s.url.startsWith("/api/link/status")).pop().url;
out.auth = seen[0].auth;
await new Promise((r) => srv.close(r));
srv.closeAllConnections?.();
family.configure({ app: "jsapp", dataDir: dir, hub: "http://127.0.0.1:1" });
out.down = await family.chat({ messages: [{ role: "user", content: "hi" }] });
out.downStatus = await family.linkStatus();
console.log(JSON.stringify(out));
process.exit(0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_client_chat_and_status(tmp_path):
    script = tmp_path / "t.mjs"
    script.write_text(JS_TEST, encoding="utf-8")
    run = subprocess.run(["node", str(script), JS.as_uri()], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["ok"] == {"ok": True, "text": "hola", "json": {"a": 1}, "model": "m", "provider": "p", "ms": 5,
                         "usage": {"total_tokens": 3}, "error": None, "detail": None}
    assert out["auth"] == "Bearer app-token"
    sent = out["sent"]["body"]
    assert out["sent"]["url"] == "/api/link/chat"
    assert sent["messages"] == [{"role": "user", "content": "hi"}] and sent["capability"] == "llm" and sent["json"] == {"type": "object"}
    assert sent["effort"] == "low" and sent["max_tokens"] == 20 and sent["temperature"] == 0.1 and sent["timeout_s"] == 300
    assert out["vision"]["ok"] and out["visionSent"] == [base64.b64encode(b"png-bytes").decode(), "QUJD"]
    assert out["no_model"]["ok"] is False and out["no_model"]["error"] == "no_model" and out["no_model"]["detail"] == "nothing resident"
    assert out["no_model"]["text"] == "" and out["no_model"]["json"] is None
    assert out["busy"]["error"] == "http_503" and out["busy"]["code"] == "gpu_busy"
    assert out["timeout504"]["error"] == "timeout"
    assert out["refused"]["error"] == "http_401" and "refused" in out["refused"]["detail"]
    assert out["hang"]["error"] == "timeout"
    assert out["status"]["llm"]["model"] == "m" and out["statusUrl"] == "/api/link/status?force=1"
    assert out["down"]["ok"] is False and out["down"]["error"] == "hub_down"
    assert out["downStatus"]["error"] == "hub_down"
