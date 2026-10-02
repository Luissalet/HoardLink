"""hoard_link.guard: the Host / Origin / Fetch Metadata rules (shared vectors with js/hoard-commons/express.js) and the pure ASGI middleware."""

from __future__ import annotations

import asyncio
import json

import pytest

from hoard_link import guard
from tests.commons.jsrun import load_vectors, normalise, python_call, run_js

CASES = load_vectors("guard")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{json.dumps(c['args'])[:60]}:{c.get('opts', '')}")
def test_vectors_python(case):
    assert normalise(python_call(guard, case)) == case["expect"]


def test_vectors_node():
    got = run_js("express.js", CASES)
    bad = [(c["fn"], c["args"], c.get("opts"), g, c["expect"]) for c, g in zip(CASES, got) if g != c["expect"]]
    assert not bad, bad


def test_the_vectors_cover_every_message():
    messages = {c["expect"][1] for c in CASES if c["fn"] == "check_request" and c["expect"]}
    assert messages == {guard._MSG_HOST, guard._MSG_ORIGIN, guard._MSG_SITE, guard._MSG_FORM}


def test_constants():
    assert guard.LOCAL_HOSTS == ("localhost", "127.0.0.1", "[::1]")
    assert {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174",
            "http://localhost:4173", "http://127.0.0.1:4173"} == set(guard.DEV_ORIGINS)
    assert guard.FRAME_DESTS == {"iframe", "frame", "embed", "object"}


def test_importing_needs_no_web_framework():
    import subprocess
    import sys
    code = ("import sys; sys.modules['fastapi'] = None; sys.modules['starlette'] = None; sys.modules['pydantic'] = None; "
            "import hoard_link.guard, hoard_link.agentkit, hoard_link.service, hoard_link.bridge; print('ok')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-1500:]


# ------------------------------------------------------------------------------------------------ ASGI

def fastapi_app(**kwargs):
    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.post("/write")
    def write():
        return {"written": True}

    guard.install_guard(app, **kwargs)
    return app


def client(app, **kw):
    from fastapi.testclient import TestClient
    return TestClient(app, **kw)


def test_http_allows_local_and_rejects_foreign_hosts():
    app = fastapi_app(port_getter=lambda: 5190)
    c = client(app, base_url="http://localhost:5190")
    assert c.get("/ping").json() == {"ok": True}
    r = c.get("/ping", headers={"host": "evil.com"})
    assert r.status_code == 403 and r.json() == {"error": "Only local access is allowed."}
    assert r.headers["content-type"] == "application/json"


def test_http_cross_site_and_form_posts_are_rejected():
    c = client(fastapi_app(port_getter=lambda: 5190), base_url="http://localhost:5190")
    assert c.post("/write", headers={"origin": "http://localhost:5190"}).json() == {"written": True}
    assert c.post("/write", headers={"origin": "https://evil.com"}).status_code == 403
    r = c.post("/write", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"})
    assert r.status_code == 403 and "Cross-site" in r.json()["error"]
    r = c.post("/write", headers={"sec-fetch-mode": "navigate"})
    assert r.status_code == 403 and "Form" in r.json()["error"]


def test_the_port_is_read_per_request_and_only_matters_when_strict():
    port = {"value": 5190}
    app = fastapi_app(port_getter=lambda: port["value"], strict_ports=True)
    c = client(app, base_url="http://localhost:5190")
    assert c.get("/ping").status_code == 200
    port["value"] = 5191                                  # the app moved to another port (port search)
    assert c.get("/ping").status_code == 403
    loose = client(fastapi_app(port_getter=lambda: 1), base_url="http://localhost:5190")
    assert loose.get("/ping").status_code == 200


def test_allowed_hosts_from_argument_and_environment(monkeypatch):
    monkeypatch.setenv("KAFKA_ALLOWED_HOSTS", "*.ts.net")
    app = fastapi_app(port_getter=lambda: 5190, allowed_env="KAFKA_ALLOWED_HOSTS", allowed_hosts="nas.local")
    c = client(app, base_url="http://localhost:5190")
    assert c.get("/ping", headers={"host": "pc.tail1.ts.net"}).status_code == 200
    assert c.get("/ping", headers={"host": "nas.local:5190"}).status_code == 200
    assert c.get("/ping", headers={"host": "other.example"}).status_code == 403
    monkeypatch.delenv("KAFKA_ALLOWED_HOSTS")
    assert client(fastapi_app(port_getter=lambda: 5190, allowed_env="KAFKA_ALLOWED_HOSTS"), base_url="http://localhost:5190") \
        .get("/ping", headers={"host": "pc.tail1.ts.net"}).status_code == 403


def test_a_broken_port_getter_does_not_take_the_app_down():
    def boom():
        raise RuntimeError("no port yet")
    c = client(fastapi_app(port_getter=boom, strict_ports=True), base_url="http://localhost:5190")
    assert c.get("/ping").status_code == 200


def test_lifespan_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = guard.GuardMiddleware(inner, port_getter=lambda: 1)
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_websocket_upgrades_are_guarded():
    sent = []

    async def inner(scope, receive, send):
        sent.append("app")

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    mw = guard.GuardMiddleware(inner, port_getter=lambda: 5190)
    bad = {"type": "websocket", "headers": [(b"host", b"localhost:5190"), (b"origin", b"https://evil.com")]}
    asyncio.run(mw(bad, receive, send))
    assert sent and sent[0]["type"] == "websocket.close" and sent[0]["code"] == 1008 and "app" not in sent
    sent.clear()
    good = {"type": "websocket", "headers": [(b"host", b"localhost:5190"), (b"origin", b"http://localhost:5173")]}
    asyncio.run(mw(good, receive, send))
    assert sent == ["app"]


def test_head_rejection_has_no_body_and_repeated_host_headers_use_the_first():
    out = []

    async def send(message):
        out.append(message)

    async def receive():
        return {"type": "http.request"}

    async def inner(scope, receive, send):
        raise AssertionError("must not run")

    mw = guard.GuardMiddleware(inner, port_getter=lambda: 5190)
    scope = {"type": "http", "method": "HEAD", "headers": [(b"host", b"evil.com"), (b"host", b"localhost")]}
    asyncio.run(mw(scope, receive, send))
    assert out[0]["status"] == 403 and out[1]["body"] == b""
    assert dict(out[0]["headers"])[b"content-length"] != b"0"
