"""hoard_link.bridge: CatalogBridge against a fake app (list, call, refresh, timeouts, outcome_unknown, envelopes, token, autostart)
and the real stdio server (mcp 1.x or 2.x, whichever is installed) over JSON-RPC."""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from hoard_link import bridge, net, proc
from hoard_link.bridge import BridgeResult, CatalogBridge, bridge_token, ensure_running, tool_timeout

TOKEN = "k" * 40


def tool(name, *, read_only=True, timeout=None, schema=None):
    entry = {"name": name, "description": f"{name} tool", "inputSchema": schema or {"type": "object", "properties": {}},
             "annotations": {"readOnlyHint": read_only}}
    if timeout is not None:
        entry["x-timeout-s"] = timeout
    return entry


class FakeApp:
    """A tiny agent app: /api/health, /api/agent/tools (mutable catalogue) and /api/agent/call with canned behaviours per tool name."""

    def __init__(self):
        self.tools = [tool("echo"), tool("slow", timeout=0.3), tool("slow_write", read_only=False, timeout=0.3), tool("fail"), tool("pic"),
                      tool("html"), tool("hang_up", read_only=False)]
        self.token = TOKEN
        self.calls = []
        self.catalog_hits = 0
        self.token_hits = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, body, ctype="application/json"):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path == "/api/health":
                    return self.reply(200, {"service": "fake-hoard"})
                if self.path == "/api/agent/tools":
                    outer.catalog_hits += 1
                    return self.reply(200, {"instructions": "Be nice.", "tools": outer.tools})
                self.reply(404, {"error": "no"})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.token_hits.append(self.headers.get("Authorization"))
                outer.calls.append(body)
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self.reply(401, {"error": "Invalid MCP token."})
                name, args = body["name"], body["arguments"]
                if name == "echo":
                    return self.reply(200, {"echo": args})
                if name in ("slow", "slow_write"):
                    time.sleep(float(args.get("seconds", 1.5)))
                    return self.reply(200, {"late": True})
                if name == "fail":
                    return self.reply(400, {"error": "Bad input.", "code": "invalid", "hint": "Fix it.", "issues": [{"loc": "a", "msg": "x"}],
                                            "details": {"field": "a"}, "candidates": ["c1"], "secret": "must not be forwarded", "key": "k", "params": {"p": 1}})
                if name == "pic":
                    return self.reply(200, {"caption": "a frame", "_image": {"data": "aGVsbG8=", "mime": "image/png"}})
                if name == "html":
                    return self.reply(500, b"<html>Internal Server Error</html>", "text/html")
                if name == "hang_up":
                    self.connection.close()
                    return
                self.reply(404, {"error": f"Unknown tool: {name}", "code": "unknown_tool"})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def app():
    fake = FakeApp()
    yield fake
    fake.close()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "mcp-token").write_text(TOKEN)
    monkeypatch.setenv("FAKE_DATA_DIR", str(folder))
    for name in ("FAKE_TOKEN", "FAKE_TOKEN_FILE", "FAKE_URL", "FAKE_PORT", "FAKE_BRIDGE_AUTOSTART"):
        monkeypatch.delenv(name, raising=False)
    return folder


def make(app, **kw):
    base = dict(app="fake", service="fake-hoard", package="fake_pkg", default_port=app.port if app else 1, data_dir_env="FAKE_DATA_DIR",
                autostart=False, title="Fake's Hoard")
    base.update(kw)
    return CatalogBridge(**base)


def text_of(result: BridgeResult):
    assert result.content[0]["type"] == "text"
    return json.loads(result.content[0]["text"])


# ------------------------------------------------------------------------------------------------ pure helpers

def test_tool_timeout_priority_and_wait_s():
    assert tool_timeout(None, 90) == 90
    assert tool_timeout({"name": "a"}, 90) == 90
    assert tool_timeout({"name": "a", "x-timeout-s": 200}, 90) == 200
    assert tool_timeout({"name": "a", "inputSchema": {"x-timeout-s": 33}}, 90) == 33
    assert tool_timeout({"name": "pdf_merge", "x-timeout-s": 200}, 90, overrides={"pdf_*": 175}) == 175
    assert tool_timeout({"name": "pdf_merge"}, 90, overrides={"images_*": 175, "pdf_merge": 12}) == 12
    assert tool_timeout({"name": "a", "x-timeout-s": "junk"}, 90) == 90
    assert tool_timeout({"name": "a", "x-timeout-s": -5}, 90) == 90
    assert tool_timeout({"name": "a"}, 90, arguments={"wait_s": 100}) == 130            # wait_s + 30
    assert tool_timeout({"name": "a"}, 90, arguments={"wait_s": 5000}) == 180           # clamped to MAX_WAIT_S first
    assert tool_timeout({"name": "a", "x-timeout-s": 600}, 90, arguments={"wait_s": 10}) == 600
    assert tool_timeout({"name": "a"}, 90, arguments={"wait_s": True}) == 90


def test_bridge_token_sources(tmp_path, monkeypatch):
    for name in ("KAFKA_TOKEN", "KAFKA_TOKEN_FILE", "MY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(FileNotFoundError) as exc:
        bridge_token("kafka", tmp_path)
    assert str(tmp_path / "mcp-token") in str(exc.value)
    (tmp_path / "mcp-token").write_text("a" * 32 + "\n")
    assert bridge_token("kafka", tmp_path) == "a" * 32
    other = tmp_path / "other"
    other.write_text("b" * 20)
    assert bridge_token("kafka", tmp_path, token_file=other) == "b" * 20                          # explicit file beats the data folder
    monkeypatch.setenv("KAFKA_TOKEN_FILE", str(tmp_path / "mcp-token"))
    assert bridge_token("kafka", tmp_path, token_file=other) == "a" * 32                          # the environment beats it
    monkeypatch.setenv("KAFKA_TOKEN", " from-env ")
    assert bridge_token("kafka", tmp_path) == "from-env"
    monkeypatch.setenv("MY_TOKEN", "custom")
    assert bridge_token("kafka", tmp_path, token_env="MY_TOKEN") == "custom"
    monkeypatch.delenv("KAFKA_TOKEN")
    monkeypatch.delenv("KAFKA_TOKEN_FILE")
    (tmp_path / "mcp-token").write_text("short")
    with pytest.raises(FileNotFoundError):
        bridge_token("kafka", tmp_path)                                                           # a corrupt token is not a token


def test_environment_names_and_urls(tmp_path, monkeypatch):
    for name in ("DEMO_URL", "DEMO_PORT", "DEMO_TOKEN", "DEMO_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    b = CatalogBridge(app="demo-app", service="demo-hoard", package="demo", default_port=5300, data_dir_env="DEMO_DATA_DIR", root=tmp_path / "x.py",
                      env_prefix="DEMO")
    assert b.prefix == "DEMO" and CatalogBridge(app="people's hoard", service="s", package="p", default_port=1).prefix == "PEOPLE_S_HOARD"
    assert b.base_url == "http://127.0.0.1:5300" and b.port == 5300
    monkeypatch.setenv("DEMO_PORT", "5301")
    assert b.base_url == "http://127.0.0.1:5301"
    url_file = tmp_path / "url"
    url_file.write_text("http://127.0.0.1:5302\n")
    b2 = CatalogBridge(app="demo", service="s", package="p", default_port=1, url_file=url_file)
    assert b2.base_url == "http://127.0.0.1:5302"
    monkeypatch.setenv("DEMO_URL", "http://localhost:5303/")
    assert b2.base_url == "http://localhost:5303" and b.base_url == "http://localhost:5303"
    assert b.data_dir == tmp_path / "data"                                                         # <root>/data when the variable is unset
    monkeypatch.setenv("DEMO_DATA_DIR", str(tmp_path / "elsewhere"))
    assert b.data_dir == tmp_path / "elsewhere"
    b.check_local()
    monkeypatch.setenv("DEMO_URL", "http://example.com:80")
    with pytest.raises(SystemExit):
        b.check_local()
    monkeypatch.setenv("DEMO_URL", "https://localhost:80")
    with pytest.raises(SystemExit):
        b.check_local()


def test_autostart_switch(app, data_dir, monkeypatch):
    assert make(app, autostart=True).autostart_enabled() is True
    monkeypatch.setenv("FAKE_BRIDGE_AUTOSTART", "0")
    assert make(app, autostart=True).autostart_enabled() is False
    monkeypatch.setenv("FAKE_BRIDGE_AUTOSTART", "1")
    assert make(app, autostart=False).autostart_enabled() is False


# ------------------------------------------------------------------------------------------------ catalogue and calls

async def test_list_and_call(app, data_dir):
    b = make(app)
    tools = await b.tools()
    assert [t["name"] for t in tools][:2] == ["echo", "slow"] and b.instructions == "Be nice."
    result = await b.call("echo", {"a": 1})
    assert result.is_error is False and text_of(result) == {"echo": {"a": 1}} and result.body == {"echo": {"a": 1}}
    assert app.token_hits[-1] == f"Bearer {TOKEN}" and app.calls[-1] == {"name": "echo", "arguments": {"a": 1}}
    assert (await b.call("echo")).body == {"echo": {}}                                                # arguments default to {}
    assert (await b.call("echo", None)).is_error is False


async def test_catalogue_is_cached_then_refreshed(app, data_dir):
    b = make(app, refresh_tools_s=0.3)
    await b.tools()
    await b.tools()
    await b.call("echo")
    assert app.catalog_hits == 1
    await asyncio.sleep(0.4)
    await b.tools()
    assert app.catalog_hits == 2
    await b.tools(force=True)
    assert app.catalog_hits == 3


async def test_unknown_tool_refreshes_then_calls_the_new_tool(app, data_dir):
    b = make(app, refresh_tools_s=3600)
    await b.tools()
    app.tools.append(tool("brand_new"))
    app.tools.append(tool("echo2"))
    result = await b.call("echo2", {"x": 1})                      # not in the cached list: refresh, then it is known
    assert app.catalog_hits == 2 and b.tool_meta("echo2") is not None
    assert result.is_error is True and text_of(result)["code"] == "unknown_tool"                    # the app itself answered 404 for it
    assert app.calls[-1]["name"] == "echo2"
    before = len(app.calls)
    gone = await b.call("not_in_catalogue_at_all")
    assert gone.is_error and text_of(gone) == {"error": "Unknown tool: not_in_catalogue_at_all", "code": "unknown_tool"}
    assert len(app.calls) == before                                                                 # no request for what the catalogue lacks


async def test_a_failed_refresh_keeps_the_last_catalogue(app, data_dir):
    b = make(app, refresh_tools_s=0.0)
    await b.tools()
    await asyncio.sleep(0.01)
    app.close()
    tools = await b.tools()
    assert [t["name"] for t in tools][0] == "echo"


async def test_no_catalogue_and_no_app_raises(data_dir):
    b = make(None, default_port=net.free_port())
    with pytest.raises(Exception):
        await b.tools()


async def test_error_envelope_is_forwarded_without_extra_keys(app, data_dir):
    result = await make(app).call("fail", {})
    body = text_of(result)
    assert result.is_error is True
    assert body == {"error": "Bad input.", "code": "invalid", "hint": "Fix it.", "issues": [{"loc": "a", "msg": "x"}], "details": {"field": "a"},
                    "candidates": ["c1"], "key": "k", "params": {"p": 1}}


async def test_non_json_answers(app, data_dir):
    result = await make(app).call("html", {})
    assert result.is_error and "Internal Server Error" in text_of(result)["error"]


async def test_wrong_token_says_which_file(app, data_dir):
    app.token = "z" * 40
    result = await make(app).call("echo", {})
    body = text_of(result)
    assert result.is_error and body["code"] == "token_refused" and str(data_dir / "mcp-token") in body["error"] and "FAKE_TOKEN_FILE" in body["error"]
    assert "Fake's Hoard" in body["error"]


async def test_token_from_environment_and_from_a_named_file(app, data_dir, monkeypatch, tmp_path):
    app.token = "e" * 40
    monkeypatch.setenv("FAKE_TOKEN", "e" * 40)
    assert (await make(app).call("echo")).is_error is False
    monkeypatch.delenv("FAKE_TOKEN")
    named = tmp_path / "elsewhere-token"
    named.write_text("e" * 40)
    monkeypatch.setenv("FAKE_TOKEN_FILE", str(named))
    assert (await make(app).call("echo")).is_error is False
    monkeypatch.delenv("FAKE_TOKEN_FILE")
    assert (await make(app, token_file=named).call("echo")).is_error is False


async def test_missing_token_file_is_not_a_stopped_app(app, data_dir):
    (data_dir / "mcp-token").unlink()
    b = make(app)
    body = text_of(await b.call("echo"))
    assert body["code"] == "no_token" and str(data_dir / "mcp-token") in body["error"] and "FAKE_TOKEN_FILE" in body["error"]


async def test_missing_token_and_app_down_says_not_running(data_dir):
    (data_dir / "mcp-token").unlink()
    body = text_of(await make(None, default_port=net.free_port()).call("echo"))
    assert body["code"] == "not_running" and "Fake's Hoard" in body["error"] and "python -m fake_pkg" in body["error"]


async def test_app_down_is_not_outcome_unknown(data_dir):
    b = make(None, default_port=net.free_port())
    b._catalog = [tool("w", read_only=False)]
    b._fetched_at = time.monotonic()
    result = await b.call("w", {})
    body = text_of(result)
    assert result.is_error and body["code"] == "not_running" and "outcome_unknown" not in body                  # the request never left


async def test_timeout_of_a_read_only_tool_is_a_plain_error(app, data_dir):
    b = make(app)
    started = time.monotonic()
    result = await b.call("slow", {"seconds": 3})                                                               # x-timeout-s is 0.3
    body = text_of(result)
    assert time.monotonic() - started < 2.5 and result.is_error
    assert body["code"] == "timeout" and "slow" in body["error"] and "outcome_unknown" not in body


async def test_timeout_of_a_write_is_outcome_unknown(app, data_dir):
    result = await make(app).call("slow_write", {"seconds": 3})
    body = text_of(result)
    assert result.is_error and body["outcome_unknown"] is True and body["status"] == "outcome_unknown" and body["code"] == "outcome_unknown"
    assert body["reconcile_action"] == "read_current_state_before_retry" and "slow_write" in body["error"]


async def test_connection_dropped_after_sending_a_write_is_outcome_unknown(app, data_dir):
    body = text_of(await make(app).call("hang_up", {}))
    assert body["outcome_unknown"] is True


async def test_an_unknown_tool_meta_counts_as_a_write(app, data_dir):
    b = make(app, refresh_tools_s=3600)
    b._catalog = []                                                                                             # nothing known: the call goes out blind
    b._fetched_at = time.monotonic()
    body = text_of(await b.call("hang_up", {}))
    assert body.get("outcome_unknown") is True


async def test_timeout_comes_from_overrides_then_catalogue_then_default(app, data_dir):
    b = make(app, default_timeout=0.2, tool_timeouts={"echo": 5})
    await b.tools()
    app.tools[1]["x-timeout-s"] = 5                                                                              # slow now allows 5 s ...
    await b.tools(force=True)
    assert text_of(await b.call("slow", {"seconds": 0.4})) == {"late": True}
    b2 = make(app, default_timeout=0.2, tool_timeouts={"slow": 0.25})                                            # ... unless the bridge says 0.25
    await b2.tools()
    assert text_of(await b2.call("slow", {"seconds": 1.0}))["code"] == "timeout"
    b3 = make(app, default_timeout=0.3)
    await b3.tools()
    assert text_of(await b3.call("echo", {"a": 1})) == {"echo": {"a": 1}}


async def test_heartbeats_while_a_call_runs(app, data_dir):
    b = make(app, heartbeat_s=0.1)
    app.tools[1]["x-timeout-s"] = 10
    beats = []

    async def progress(n, message):
        beats.append((n, message))

    result = await b.call("slow", {"seconds": 0.55}, progress)
    assert text_of(result) == {"late": True}
    assert 3 <= len(beats) <= 6 and [n for n, _ in beats] == [float(i) for i in range(1, len(beats) + 1)]
    assert all("slow" in m for _, m in beats)
    beats.clear()
    await b.call("echo", {}, progress)
    assert beats == []                                                                                           # short calls send nothing


async def test_a_progress_callback_that_fails_does_not_break_the_call(app, data_dir):
    b = make(app, heartbeat_s=0.05)
    app.tools[1]["x-timeout-s"] = 10

    async def progress(n, message):
        raise RuntimeError("client went away")

    assert text_of(await b.call("slow", {"seconds": 0.3}, progress)) == {"late": True}


async def test_image_content(app, data_dir):
    plain = await make(app).call("pic", {})
    assert len(plain.content) == 1 and "_image" in text_of(plain)
    result = await make(app, image_content=True).call("pic", {})
    assert text_of(result) == {"caption": "a frame"}
    assert result.content[1] == {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}


async def test_environment_proxy_is_ignored_for_loopback(app, data_dir, monkeypatch):
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")                                                            # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert text_of(await make(app).call("echo", {"p": 1})) == {"echo": {"p": 1}}


@pytest.fixture
def no_httpx(monkeypatch):
    """Hide httpx so the bridge falls back to urllib."""
    monkeypatch.setitem(sys.modules, "httpx", None)


async def test_urllib_fallback_covers_the_same_cases(app, data_dir, no_httpx, monkeypatch):
    for key in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    b = make(app)
    assert text_of(await b.call("echo", {"u": 1})) == {"echo": {"u": 1}}
    assert text_of(await b.call("fail", {}))["code"] == "invalid"
    assert text_of(await b.call("slow_write", {"seconds": 3}))["outcome_unknown"] is True
    assert text_of(await b.call("slow", {"seconds": 3}))["code"] == "timeout"
    assert text_of(await b.call("hang_up", {}))["outcome_unknown"] is True
    assert "Internal Server Error" in text_of(await b.call("html", {}))["error"]
    app.token = "q" * 40
    assert text_of(await b.call("echo", {}))["code"] == "token_refused"


async def test_urllib_connection_refused_is_not_running(data_dir, no_httpx):
    b = make(None, default_port=net.free_port())
    b._catalog = [tool("echo")]
    b._fetched_at = time.monotonic()
    assert text_of(await b.call("echo", {}))["code"] == "not_running"


# ------------------------------------------------------------------------------------------------ autostart

FAKE_PACKAGE = textwrap.dedent('''
    import http.server, json, os, sys
    PORT = int(os.environ["FAKE_PORT"])
    if os.environ.get("FAKE_EXIT_WITH"):
        print("could not start", flush=True)
        sys.exit(int(os.environ["FAKE_EXIT_WITH"]))
    TOOLS = [{"name": "echo", "description": "d", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]
    class H(http.server.BaseHTTPRequestHandler):
        def send_json(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            if self.path == "/api/health":
                return self.send_json({"service": "fake-hoard", "pid": os.getpid(), "strict": os.environ.get("PORT_STRICT"), "no_browser": os.environ.get("HOARD_NO_BROWSER"), "cwd": os.getcwd()})
            self.send_json({"instructions": "", "tools": TOOLS})
        def do_POST(self):
            n = int(self.headers["Content-Length"]); body = json.loads(self.rfile.read(n))
            self.send_json({"echo": body["arguments"], "auth": self.headers.get("Authorization")})
        def log_message(self, *a): pass
    print("fake app starting on", PORT, flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
''')


@pytest.fixture
def fake_package(tmp_path, monkeypatch):
    root = tmp_path / "root with spaces"
    (root / "fake_pkg").mkdir(parents=True)
    (root / "fake_pkg" / "__init__.py").write_text("")
    (root / "fake_pkg" / "__main__.py").write_text(FAKE_PACKAGE, encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "mcp-token").write_text(TOKEN)
    for name in ("FAKE_DATA_DIR", "FAKE_PORT", "FAKE_URL", "FAKE_BRIDGE_AUTOSTART", "FAKE_EXIT_WITH"):
        monkeypatch.delenv(name, raising=False)
    yield root
    for child in list(bridge._children.values()):
        try:
            proc.kill_tree(child)
        except Exception:
            pass
    bridge._children.clear()


def health_of(port):
    return net.fetch_health(f"http://127.0.0.1:{port}/api/health", timeout=2)


def test_ensure_running_starts_the_package_detached_and_quiet(fake_package):
    port = net.free_port()
    assert health_of(port) is None
    assert ensure_running("fake_pkg", port, service="fake-hoard", data_dir=fake_package / "data", cwd=fake_package, port_env="FAKE_PORT", wait_s=30) is True
    info = health_of(port)
    assert info["service"] == "fake-hoard" and info["strict"] == "1" and info["no_browser"] == "1"
    assert Path(info["cwd"]).resolve() == fake_package.resolve()
    log = fake_package / "data" / "logs" / "fake-app.log"
    assert log.exists() and f"fake app starting on {port}" in log.read_text()
    again = ensure_running("fake_pkg", port, service="fake-hoard", data_dir=fake_package / "data", cwd=fake_package, port_env="FAKE_PORT", wait_s=5)
    assert again is True and health_of(port)["pid"] == info["pid"]                                             # no second process


def test_ensure_running_gives_up_when_the_child_dies(fake_package, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT_WITH", "3")
    port = net.free_port()
    started = time.monotonic()
    assert ensure_running("fake_pkg", port, service="fake-hoard", data_dir=fake_package / "data", cwd=fake_package, port_env="FAKE_PORT", wait_s=30) is False
    assert time.monotonic() - started < 15                                                                        # it did not wait out the 30 s
    assert "could not start" in (fake_package / "data" / "logs" / "fake-app.log").read_text()


def test_ensure_running_ignores_another_service_on_the_port(fake_package):
    other = FakeApp()
    try:
        assert ensure_running("fake_pkg", other.port, service="not-this-one", data_dir=fake_package / "data", cwd=fake_package,
                              port_env="FAKE_PORT", wait_s=1.0) is False                                          # strict start fails: port taken
    finally:
        other.close()


def test_the_launch_log_is_rotated(fake_package):
    logs = fake_package / "data" / "logs"
    logs.mkdir()
    (logs / "fake-app.log").write_bytes(b"old\n" * 700_000)                                                         # 2.8 MB
    port = net.free_port()
    assert ensure_running("fake_pkg", port, service="fake-hoard", data_dir=fake_package / "data", cwd=fake_package, port_env="FAKE_PORT", wait_s=30)
    assert (logs / "fake-app.log.1").exists() and (logs / "fake-app.log").stat().st_size < 10_000


async def test_the_bridge_starts_the_app_when_nothing_answers(fake_package, monkeypatch):
    port = net.free_port()
    b = CatalogBridge(app="fake", service="fake-hoard", package="fake_pkg", default_port=port, data_dir_env="FAKE_DATA_DIR", root=fake_package / "mcp_server.py")
    assert b.data_dir == fake_package / "data" and not b.healthy()
    tools = await b.tools()                                                                                      # starts it, then lists
    assert [t["name"] for t in tools] == ["echo"] and b.healthy()
    result = await b.call("echo", {"v": 1})
    assert text_of(result)["echo"] == {"v": 1} and text_of(result)["auth"] == f"Bearer {TOKEN}"


async def test_a_call_to_a_stopped_app_starts_it_and_retries(fake_package):
    port = net.free_port()
    b = CatalogBridge(app="fake", service="fake-hoard", package="fake_pkg", default_port=port, data_dir_env="FAKE_DATA_DIR", root=fake_package)
    b._catalog = [tool("echo")]
    b._fetched_at = time.monotonic()
    result = await b.call("echo", {"again": True})
    assert result.is_error is False and text_of(result)["echo"] == {"again": True} and b.healthy()


async def test_autostart_disabled_leaves_the_app_stopped(fake_package, monkeypatch):
    monkeypatch.setenv("FAKE_BRIDGE_AUTOSTART", "0")
    port = net.free_port()
    b = CatalogBridge(app="fake", service="fake-hoard", package="fake_pkg", default_port=port, data_dir_env="FAKE_DATA_DIR", root=fake_package)
    b._catalog = [tool("echo")]
    b._fetched_at = time.monotonic()
    assert text_of(await b.call("echo", {}))["code"] == "not_running"
    assert health_of(port) is None and not bridge._children and not (fake_package / "data" / "logs").exists()
    assert b.start_app() is False
    with pytest.raises(Exception):
        await CatalogBridge(app="fake", service="fake-hoard", package="fake_pkg", default_port=port, data_dir_env="FAKE_DATA_DIR",
                            root=fake_package).tools()


# ------------------------------------------------------------------------------------------------ the real MCP server

def rpc_session(app, data_dir, *, extra_env=None, protocol="2025-06-18", **bridge_kwargs):
    code = textwrap.dedent(f"""
        from hoard_link.bridge import CatalogBridge
        CatalogBridge(app="fake", service="fake-hoard", package="fake_pkg", default_port={app.port}, data_dir_env="FAKE_DATA_DIR",
                      autostart=False, heartbeat_s=0.1, title="Fake's Hoard", **{bridge_kwargs!r}).run_bridge()
    """)
    root = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "FAKE_DATA_DIR": str(data_dir), "PYTHONPATH": os.pathsep.join(filter(None, [*(extra_env or {}).get("path", []), root]))}
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    lines: list[dict] = []
    done = threading.Event()

    def reader():
        for line in child.stdout:
            if line.strip():
                lines.append(json.loads(line))
        done.set()

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        child.stdin.write(json.dumps(obj) + "\n")
        child.stdin.flush()

    def wait_for(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(lines):
                if predicate(message):
                    return message
            time.sleep(0.02)
        raise AssertionError(f"no message; got {lines}; stderr={child.stderr.read() if child.poll() is not None else ''}")

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": protocol, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    init = wait_for(lambda m: m.get("id") == 1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return child, send, wait_for, init, lines


def test_stdio_server_lists_calls_and_heartbeats(app, data_dir):
    pytest.importorskip("mcp")
    app.tools[1]["x-timeout-s"] = 10
    child, send, wait_for, init, lines = rpc_session(app, data_dir)
    try:
        assert init["result"]["serverInfo"]["name"] == "fake-hoard" and init["result"]["instructions"] == "Be nice."
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = wait_for(lambda m: m.get("id") == 2)["result"]["tools"]
        assert {t["name"] for t in tools} == {"echo", "slow", "slow_write", "fail", "pic", "html", "hang_up"}
        echo = next(t for t in tools if t["name"] == "echo")
        assert echo["annotations"]["readOnlyHint"] is True and echo["inputSchema"]["type"] == "object"
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"a": "b"}}})
        ok = wait_for(lambda m: m.get("id") == 3)["result"]
        assert ok["isError"] is False and json.loads(ok["content"][0]["text"]) == {"echo": {"a": "b"}}
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "slow", "arguments": {"seconds": 0.6}, "_meta": {"progressToken": "tok"}}})
        late = wait_for(lambda m: m.get("id") == 4)["result"]
        assert json.loads(late["content"][0]["text"]) == {"late": True}
        beats = [m for m in lines if m.get("method") == "notifications/progress" and m["params"]["progressToken"] == "tok"]
        assert len(beats) >= 2
        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "fail", "arguments": {}}})
        failed = wait_for(lambda m: m.get("id") == 5)["result"]
        assert failed["isError"] is True and json.loads(failed["content"][0]["text"])["code"] == "invalid"
        send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "slow_write", "arguments": {"seconds": 3}}})
        unknown = wait_for(lambda m: m.get("id") == 6)["result"]
        assert json.loads(unknown["content"][0]["text"])["outcome_unknown"] is True
        app.token = "changed" * 6
        send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
        assert json.loads(wait_for(lambda m: m.get("id") == 7)["result"]["content"][0]["text"])["code"] == "token_refused"
    finally:
        child.kill()


def test_stdio_server_sends_images(app, data_dir):
    pytest.importorskip("mcp")
    child, send, wait_for, init, lines = rpc_session(app, data_dir, image_content=True)
    try:
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "pic", "arguments": {}}})
        content = wait_for(lambda m: m.get("id") == 2)["result"]["content"]
        assert [c["type"] for c in content] == ["text", "image"] and content[1]["data"] == "aGVsbG8=" and content[1]["mimeType"] == "image/png"
    finally:
        child.kill()


@pytest.mark.skipif(not os.environ.get("HOARD_TEST_MCP1_PATH"), reason="set HOARD_TEST_MCP1_PATH to a folder with mcp 1.x installed to cover FastMCP")
def test_stdio_server_with_mcp_1x(app, data_dir):
    child, send, wait_for, init, lines = rpc_session(app, data_dir, extra_env={"path": [os.environ["HOARD_TEST_MCP1_PATH"]]})
    try:
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {"v": 1}}})
        assert json.loads(wait_for(lambda m: m.get("id") == 2)["result"]["content"][0]["text"]) == {"echo": {"v": 1}}
    finally:
        child.kill()


def test_run_bridge_exits_with_a_message_when_the_app_cannot_be_reached(data_dir):
    code = textwrap.dedent(f"""
        from hoard_link.bridge import CatalogBridge
        CatalogBridge(app="fake", service="fake-hoard", package="fake_pkg", default_port={net.free_port()}, data_dir_env="FAKE_DATA_DIR",
                      autostart=False, title="Fake's Hoard").run_bridge()
    """)
    root = str(Path(__file__).resolve().parents[2])
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                          env={**os.environ, "FAKE_DATA_DIR": str(data_dir), "PYTHONPATH": root})
    assert done.returncode != 0 and "Open Fake's Hoard (python -m fake_pkg)" in done.stderr and done.stdout == ""


def test_run_bridge_refuses_a_non_local_url(data_dir):
    code = ("from hoard_link.bridge import CatalogBridge\n"
            "CatalogBridge(app='fake', service='s', package='p', default_port=1, autostart=False).run_bridge()")
    root = str(Path(__file__).resolve().parents[2])
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                          env={**os.environ, "FAKE_URL": "http://example.com:1", "PYTHONPATH": root})
    assert done.returncode != 0 and "only connects to the local server" in done.stderr
