"""Placement discovery never waits on a navigation, and only authorizes its exact Faustus origin."""
from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard_link import guard
from tests.commons.jsrun import node

PARENT = "https://spark3.tail162e05.ts.net"
HEADERS = {"host": "localhost:5190", "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate",
           "sec-fetch-dest": "iframe", "referer": PARENT + "/studio?dock=darwin"}


def wait_for(predicate):
    deadline = time.perf_counter() + 3
    while not predicate():
        assert time.perf_counter() < deadline, "background discovery did not finish"
        time.sleep(0.005)


def test_python_cache_nonblocking_singleflight_expiry_and_failure(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    clock = [100.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: clock[0])

    def placement(url, timeout):
        calls.append((url, timeout))
        entered.set()
        assert release.wait(3)
        return io.BytesIO(json.dumps({"urls": {"faustus": PARENT + "/studio"}}).encode())

    monkeypatch.setattr(guard, "urlopen", placement)
    resolver = guard.FaustusOrigins(faustus_urls=["https://configured.example:443/path"], hub_url="http://hub:8810")
    assert resolver.current() == {"https://configured.example"}
    assert entered.wait(1)
    for _ in range(20):
        assert PARENT not in resolver.current()
    assert len(calls) == 1
    release.set()
    wait_for(lambda: not resolver.lock.locked())
    assert resolver.current() == {PARENT, "https://configured.example"}
    assert len(calls) == 1
    clock[0] += 61
    monkeypatch.setattr(guard, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")))
    assert resolver.current() == {"https://configured.example"}
    wait_for(lambda: not resolver.lock.locked())
    assert resolver.current() == {"https://configured.example"}


def test_configured_origins_and_hub_environment(tmp_path, monkeypatch):
    config = tmp_path / "backend.json"
    config.write_text(json.dumps({"faustus": {"url": PARENT + "/studio"}, "gpu_lease": {"hub_url": "http://old:8810"}}))
    monkeypatch.setenv("HOARD_HUB_URL", "http://configured-hub:8811/")
    monkeypatch.delenv("HOARD_FAUSTUS_URL", raising=False)
    resolver = guard.FaustusOrigins(config_path=config)
    assert resolver.configured == {PARENT}
    assert resolver.hub_url == "http://configured-hub:8811"


def test_http_server_and_wsgi_use_same_parent_policy():
    resolver = SimpleNamespace(current=lambda: {PARENT})
    handler = SimpleNamespace(headers=HEADERS, command="GET", server=SimpleNamespace(server_address=("127.0.0.1", 5190)))
    assert guard.check_handler(handler, faustus_origins=resolver)
    calls = []
    wrapped = guard.wsgi_middleware(lambda env, start: calls.append("app") or [b"ok"], port_getter=lambda: 5190,
                                   faustus_origins=resolver)
    environ = {"HTTP_" + key.upper().replace("-", "_"): value for key, value in HEADERS.items()}
    environ["REQUEST_METHOD"] = "GET"
    assert wrapped(environ, lambda *_: None) == [b"ok"]
    assert calls == ["app"]


def test_asgi_uses_same_parent_policy():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.get("/")(lambda: {"ok": True})
    guard.install_guard(app, port_getter=lambda: 5190, faustus_origins=SimpleNamespace(current=lambda: {PARENT}))
    client = TestClient(app)
    assert client.get("/", headers=HEADERS).status_code == 200
    assert client.get("/", headers={**HEADERS, "referer": "https://unknown.example/"}).status_code == 403
    assert client.get("/", headers={**HEADERS, "sec-fetch-dest": "object"}).status_code == 403
    assert client.post("/", headers=HEADERS).status_code == 403


def test_node_discovery_nonblocking_cache_and_failure(tmp_path):
    calls = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            assert release.wait(4)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"urls": {"faustus": PARENT + "/studio"}}).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    module = (Path(__file__).resolve().parents[2] / "js/hoard-commons/express.js").as_uri()
    script = tmp_path / "cache.mjs"
    script.write_text("""
import assert from 'node:assert/strict';
import {createFaustusOrigins, createGuard} from MODULE;
const parent = 'https://spark3.tail162e05.ts.net';
const origins = createFaustusOrigins({faustusUrls:['https://configured.example:443/path'], hubUrl:HUB});
const headers = HEADERS;
const guard = createGuard({port:5190, faustusOrigins:origins});
function request() { let status=200; guard({method:'GET',headers},{status(s){status=s;return this},json(){}},()=>{});return status; }
assert.equal(request(),403);
for(let i=0;i<20;i++) assert.deepEqual(origins(),['https://configured.example']);
console.log('pending');
for(let i=0;i<300 && !origins().includes(parent);i++) await new Promise(r=>setTimeout(r,10));
assert.equal(request(),200);
assert.equal(request(),200);
const realNow=Date.now; Date.now=()=>realNow()+61000;
assert.equal(request(),403);
Date.now=realNow;
const offline = createFaustusOrigins({faustusUrls:['https://configured.example'], hubUrl:'http://127.0.0.1:1'});
assert.deepEqual(offline(),['https://configured.example']);
await new Promise(r=>setTimeout(r,50));
assert.deepEqual(offline(),['https://configured.example']);
console.log('passed');
""".replace("MODULE", json.dumps(module)).replace("HUB", json.dumps(f"http://127.0.0.1:{server.server_port}"))
        .replace("HEADERS", json.dumps(HEADERS)), encoding="utf-8")
    proc = subprocess.Popen([node(), str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "pending"
        wait_for(lambda: len(calls) == 1)
        release.set()
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        assert "passed" in stdout
        assert calls[0] == "/api/placement" and len(calls) <= 2
    finally:
        release.set()
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        server.shutdown()
        server.server_close()
