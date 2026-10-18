"""Transport contracts without sockets, servers, certificate stores or models."""
from __future__ import annotations

import ast
import asyncio
import http.client
import inspect
import io
import json
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.response
from concurrent.futures import ThreadPoolExecutor
from email.message import Message

import pytest

from hoard_link import _hubclient as client


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Transport tests forbid sockets and real certificate stores")
    def guarded(operation):
        def call(*args, **kwargs):
            # Windows asyncio creates its internal wakeup socketpair in Python.
            fallback = getattr(socket, "_fallback_socketpair", None)
            if fallback is not None and sys._getframe(1).f_code is fallback.__code__:
                return operation(*args, **kwargs)
            return forbidden(*args, **kwargs)
        return call
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, guarded(getattr(socket.socket, name)))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", forbidden)


def reply(raw=b'{"ok": true}', status=200, url="http://127.0.0.1:9/api", headers=None):
    info = Message()
    for key, value in (headers or {}).items():
        info[key] = value
    response = urllib.response.addinfourl(io.BytesIO(raw), info, url, status)
    response.msg = "fixture"
    return response


def legacy_fetch(name):
    """Restore only the exact former opener assignment, keeping the caller identical."""
    tree = ast.parse(inspect.getsource(getattr(client, name)))
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "opener" for t in node.targets)]
    assert len(assignments) == 1
    assignments[0].value = ast.parse(
        "urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())", mode="eval").body
    namespace = dict(vars(client))
    exec(compile(ast.fix_missing_locations(tree), "<legacy-opener-in-RAM>", "exec"), namespace)
    return namespace[name]


def fake_http(monkeypatch, raw=b'{"ok": true}', status=200, headers=None):
    calls = []
    def opened(self, request):
        calls.append({"request": request, "timeout": request.timeout, "opener": self.parent})
        return reply(raw, status, request.full_url, headers)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", opened)
    return calls


def fake_tls(monkeypatch):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    calls = []
    def created(version):
        calls.append(version)
        return context
    monkeypatch.setattr(http.client, "_create_https_context", created, raising=False)
    return context, calls


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
def test_http_never_constructs_https_handler(monkeypatch, name):
    calls = fake_http(monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP must not construct HTTPSHandler")
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", forbidden)
    result = getattr(client, name)("http://127.0.0.1:9/api")
    assert result == ((200, {"ok": True}) if name == "fetch" else (200, {"ok": True}, ""))
    assert len(calls) == 1


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
@pytest.mark.asyncio
async def test_http_tls_gate_no_longer_blocks_heartbeat(monkeypatch, name, record_property):
    fake_http(monkeypatch)
    tls_calls = []
    entered = threading.Event()
    released = threading.Event()
    def gated_context(version):
        tls_calls.append(version)
        entered.set()
        assert released.wait(2), "The bounded TLS fixture must be released"
        return object()
    monkeypatch.setattr(http.client, "_create_https_context", gated_context, raising=False)
    # Older Python versions may not eagerly create TLS in HTTPSHandler.__init__.
    released.set()
    urllib.request.build_opener(urllib.request.ProxyHandler({}), client.NoRedirect())
    if not tls_calls:
        pytest.skip("This Python does not eagerly create an HTTPS context")
    released.clear(); entered.clear(); tls_calls.clear()

    async def observe(function):
        ticks = []
        done = asyncio.Event()
        async def heartbeat():
            while not done.is_set():
                ticks.append(time.perf_counter())
                await asyncio.sleep(.01)
        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(.03)
        started = time.perf_counter()
        result = function("http://127.0.0.1:9/api")
        elapsed = time.perf_counter() - started
        await asyncio.sleep(.03)
        done.set(); await pulse
        return result, elapsed, max(b - a for a, b in zip(ticks, ticks[1:]))

    def release_gate():
        if entered.wait(1):
            time.sleep(.2)
        released.set()
    watchdog = threading.Thread(target=release_gate)
    watchdog.start()
    try:
        before = await observe(legacy_fetch(name))
    finally:
        released.set(); watchdog.join(2)
    assert not watchdog.is_alive()
    assert entered.is_set() and len(tls_calls) == 1
    assert before[1] >= .18 and before[2] >= .18
    entered.clear(); tls_calls.clear()
    after = await observe(getattr(client, name))
    assert after[0] == before[0]
    assert not entered.is_set() and tls_calls == []
    assert after[2] < before[2] / 2
    record_property("legacy_call_seconds", before[1])
    record_property("legacy_heartbeat_gap_seconds", before[2])
    record_property("current_call_seconds", after[1])
    record_property("current_heartbeat_gap_seconds", after[2])


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
def test_https_keeps_native_constructor_context_and_open(monkeypatch, name):
    context, contexts = fake_tls(monkeypatch)
    constructor_calls = []
    real_constructor = urllib.request.HTTPSHandler.__init__
    def constructed(self, *args, **kwargs):
        constructor_calls.append((args, kwargs))
        real_constructor(self, *args, **kwargs)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", constructed)
    opened = []
    def do_open(self, connection, request, **kwargs):
        opened.append((connection, request.full_url, request.timeout, kwargs))
        return reply(url=request.full_url)
    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", do_open)
    before = legacy_fetch(name)("https://example.invalid/api", timeout=7)
    previous = (list(constructor_calls), list(contexts), list(opened))
    constructor_calls.clear(); contexts.clear(); opened.clear()
    after = getattr(client, name)("https://example.invalid/api", timeout=7)
    assert after == before
    assert (constructor_calls, contexts, opened) == previous
    assert constructor_calls == [((), {})]
    assert opened[0][0] is http.client.HTTPSConnection
    assert opened[0][3]["context"] is (context if contexts else None)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
def test_https_constructor_exception_stays_outside_fetch_try(monkeypatch, name):
    failure = ValueError("fixture TLS constructor failure")
    def failed(*args, **kwargs):
        raise failure
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", failed)
    for function in (legacy_fetch(name), getattr(client, name)):
        with pytest.raises(ValueError) as result:
            function("https://example.invalid/api")
        assert result.value is failure


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
@pytest.mark.parametrize("body,method", [(None, None), ({"text": "á漢🙂"}, None), ({}, "PUT")])
def test_http_request_timeout_proxy_headers_and_handlers_are_preserved(monkeypatch, name, body, method):
    fake_tls(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("NO_PROXY", "")
    calls = fake_http(monkeypatch)
    arguments = {"body": body, "method": method, "timeout": 7.25, "headers": {"Authorization": "Bearer fixture"}}
    previous = legacy_fetch(name)("http://example.invalid/api?x=1", **arguments)
    previous_request = calls[-1]["request"]
    current = getattr(client, name)("http://example.invalid/api?x=1", **arguments)
    current_request = calls[-1]["request"]
    assert current == previous
    for request in (previous_request, current_request):
        assert request.full_url == "http://example.invalid/api?x=1"
        assert request.get_method() == (method or ("POST" if body is not None else "GET"))
        assert request.data == (json.dumps(body).encode("utf8") if body is not None else None)
        assert request.timeout == 7.25
        assert request.get_header("Authorization") == "Bearer fixture"
        assert request.get_header("User-agent") == "hoard-link"
        assert not request.has_proxy()
    old_handlers = [type(h) for h in calls[-2]["opener"].handlers if not isinstance(h, urllib.request.HTTPSHandler)]
    assert [type(h) for h in calls[-1]["opener"].handlers] == old_handlers


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_http_redirect_never_forwards_credentials(monkeypatch, name, status):
    fake_tls(monkeypatch)
    calls = fake_http(monkeypatch, b'{"redirect": true}', status,
                      {"Location": "https://other.invalid/capture"})
    results = [function("http://example.invalid/api", headers={"Authorization": "Bearer fixture"})
               for function in (legacy_fetch(name), getattr(client, name))]
    assert results[0] == results[1]
    assert results[1][:2] == (status, {"redirect": True})
    assert len(calls) == 2 and all(c["request"].full_url == "http://example.invalid/api" for c in calls)


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
@pytest.mark.parametrize("raw,status", [(b"", 200), (b"invalid JSON", 200), (b'{"error": "fixture"}', 403)])
def test_http_response_and_error_body_contract(monkeypatch, name, raw, status):
    fake_tls(monkeypatch)
    fake_http(monkeypatch, raw, status)
    results = [function("http://127.0.0.1:9/api") for function in (legacy_fetch(name), getattr(client, name))]
    assert results[0] == results[1]
    assert results[1][0] == status


@pytest.mark.parametrize("name", ["fetch", "fetch_detailed"])
@pytest.mark.parametrize("failure", [TimeoutError("fixture"), urllib.error.URLError(TimeoutError()),
                                     urllib.error.URLError("unreachable")])
def test_http_network_error_classification_is_unchanged(monkeypatch, name, failure):
    fake_tls(monkeypatch)
    def opened(self, request):
        raise failure
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", opened)
    results = [function("http://127.0.0.1:9/api") for function in (legacy_fetch(name), getattr(client, name))]
    assert results[0] == results[1]
    assert results[1][:2] == (None, None)
    if name == "fetch_detailed":
        assert results[1][2] == ("timeout" if "unreachable" not in str(failure) else "unreachable")


def test_http_openers_are_separate_across_threads(monkeypatch):
    fake_http(monkeypatch)
    # Keep references so object IDs cannot be recycled between requests.
    openers = []
    guard = threading.Lock()
    barrier = threading.Barrier(2)
    def opened(self, request):
        with guard:
            openers.append(self.parent)
        barrier.wait(timeout=2)
        return reply()
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", opened)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(client.fetch, ["http://127.0.0.1:9/one", "http://127.0.0.1:9/two"]))
    assert results == [(200, {"ok": True})] * 2
    assert len(openers) == 2 and openers[0] is not openers[1]
    assert not ({id(h) for h in openers[0].handlers} & {id(h) for h in openers[1].handlers})
