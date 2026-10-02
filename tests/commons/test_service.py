"""hoard_link.service: logging, the SPA catch-all (and its traversal limits), the error envelope, the PWA files, /api/health and run_main."""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from hoard_link import family, net, service
from hoard_link.agentkit import AppError

pytest.importorskip("fastapi")
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402


@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    handlers, level, hook = list(root.handlers), root.level, sys.excepthook
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)
    sys.excepthook = hook


# ------------------------------------------------------------------------------------------------ logging

def ours():
    return [h for h in logging.getLogger().handlers if getattr(h, "_hoard_link_log", False)]


def test_setup_logging_writes_a_rotating_file_lazily(tmp_path, clean_logging):
    logs = tmp_path / "data" / "logs"
    path = service.setup_logging("demo-hoard", logs, console=False, max_bytes=500, backups=2)
    assert path == logs / "demo-hoard.log" and logs.is_dir()
    assert not path.exists()                                    # opened on the first record: an early exit leaves nothing
    log = logging.getLogger("demo.test")
    for i in range(60):
        log.info("line %s %s", i, "x" * 40)
    for h in ours():
        h.flush()
    assert path.exists() and (logs / "demo-hoard.log.1").exists() and (logs / "demo-hoard.log.2").exists()
    assert not (logs / "demo-hoard.log.3").exists()
    assert "line 59" in path.read_text(encoding="utf-8")


def test_setup_logging_is_idempotent_and_keeps_foreign_handlers(tmp_path, clean_logging):
    foreign = logging.NullHandler()
    logging.getLogger().addHandler(foreign)
    try:
        service.setup_logging("a", tmp_path / "l1", console=False)
        service.setup_logging("a", tmp_path / "l1", console=False)
        service.setup_logging("a", tmp_path / "l2", console=False)
        assert len(ours()) == 1 and foreign in logging.getLogger().handlers
        assert ours()[0].baseFilename.endswith(os.path.join("l2", "a.log"))
    finally:
        logging.getLogger().removeHandler(foreign)


def test_setup_logging_levels_console_and_unwritable_folder(tmp_path, clean_logging, capsys):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    assert service.setup_logging("x", blocked / "logs", console=False) is None           # no crash; a NullHandler stands in
    assert len(ours()) == 1
    path = service.setup_logging("x", tmp_path / "ok", level=logging.WARNING, loggers=("lib.noisy",), console=True)
    kinds = {type(h).__name__ for h in ours()}
    assert kinds == {"StreamHandler", "RotatingFileHandler"} and path is not None
    assert logging.getLogger().level == logging.WARNING and logging.getLogger("lib.noisy").level == logging.WARNING
    assert logging.getLogger("httpx").level >= logging.WARNING
    logging.getLogger("t").warning("to stderr")
    assert "to stderr" in capsys.readouterr().err


def test_no_console_under_pythonw(tmp_path, clean_logging, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    service.setup_logging("x", tmp_path, console=True)
    assert {type(h).__name__ for h in ours()} == {"RotatingFileHandler"}
    monkeypatch.setattr(sys, "stdout", None)
    service.say("nobody listens")                               # must not raise


def test_rotate_log(tmp_path):
    log = tmp_path / "app.log"
    assert service.rotate_log(log) is False                     # missing
    log.write_text("a" * 10)
    assert service.rotate_log(log, max_bytes=100) is False
    log.write_text("b" * 200)
    (tmp_path / "app.log.1").write_text("old1")
    (tmp_path / "app.log.2").write_text("old2")
    assert service.rotate_log(log, max_bytes=100, backups=3) is True
    assert not log.exists() and (tmp_path / "app.log.1").read_text() == "b" * 200
    assert (tmp_path / "app.log.2").read_text() == "old1" and (tmp_path / "app.log.3").read_text() == "old2"


def test_excepthook_logs_then_chains(clean_logging):
    seen = []
    previous = sys.excepthook
    sys.excepthook = lambda *a: seen.append("previous")
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("hook.test")
    log.addHandler(Catch())
    service.install_excepthook(log)
    sys.excepthook(ValueError, ValueError("bad"), None)
    assert records and records[0].levelno == logging.CRITICAL and seen == ["previous"]
    sys.excepthook = previous


# ------------------------------------------------------------------------------------------------ SPA

@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "site"
    (root / "dist" / "assets").mkdir(parents=True)
    (root / "dist" / "index.html").write_text("<!doctype html><title>app</title><script type=module src=/assets/app-AbCd1234.js></script>", encoding="utf-8")
    (root / "dist" / "assets" / "app-AbCd1234.js").write_text("export const x = 1;", encoding="utf-8")
    (root / "dist" / "assets" / "style.css").write_text("body{}", encoding="utf-8")
    (root / "dist" / "icon-192.png").write_bytes(b"\x89PNG fake")
    (root / "dist" / ".hidden").write_text("nope")
    (root / "secret.txt").write_text("TOP SECRET")
    (root / "dist" / "sub").mkdir()
    (root / "dist" / "sub" / "page.html").write_text("<p>sub</p>")
    return root / "dist"


def spa_app(dist):
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"pong": True}

    service.install_spa(app, dist)
    return app


def test_spa_serves_files_index_and_api_404(dist):
    c = TestClient(spa_app(dist))
    r = c.get("/")
    assert r.status_code == 200 and "<title>app</title>" in r.text and r.headers["cache-control"] == "no-cache"
    assert r.headers["content-type"].startswith("text/html")
    assert c.get("/some/client/route").text == r.text                              # client-side routing
    assert c.get("/some/client/route").headers["cache-control"] == "no-cache"
    js = c.get("/assets/app-AbCd1234.js")
    assert js.status_code == 200 and js.text == "export const x = 1;"
    assert js.headers["content-type"].startswith("text/javascript") and "immutable" in js.headers["cache-control"]
    css = c.get("/assets/style.css")
    assert css.headers["cache-control"] == "no-cache" and css.headers["content-type"].startswith("text/css")
    assert c.get("/icon-192.png").headers["cache-control"] == "no-cache"
    assert c.get("/sub/page.html").text == "<p>sub</p>"
    assert c.get("/api/ping").json() == {"pong": True}                              # real routes registered before are not shadowed
    for method in ("get", "post", "put", "delete"):
        r = getattr(c, method)("/api/nothing/here")
        assert r.status_code == 404 and r.json() == {"error": "Not found.", "code": "not_found"}
    assert c.get("/api").status_code == 404 and c.post("/api/").status_code == 404
    assert c.head("/").status_code == 200 and c.head("/assets/app-AbCd1234.js").status_code == 200


def test_spa_missing_assets_are_404_not_html(dist):
    c = TestClient(spa_app(dist))
    for path in ("/assets/gone-12345678.js", "/assets/", "/missing.css", "/robots.txt", "/x/y/gone.png", "/app.js.map"):
        r = c.get(path)
        assert r.status_code == 404 and r.json()["code"] == "not_found", path
    assert c.get("/docs/v1").status_code == 200                                      # no extension: a client route


@pytest.mark.parametrize("path", [
    "/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/%2e%2e%2fsecret.txt", "/assets/../../secret.txt",
    "/assets/%2e%2e/%2e%2e/secret.txt", "/..\\secret.txt", "/%5c..%5csecret.txt", "//etc/passwd", "/%2fetc/passwd", "/.hidden",
    "/sub/../.hidden", "/C:/Windows/win.ini", "/index.html%00.png", "/....//secret.txt", "/assets/..%5c..%5csecret.txt",
])
def test_spa_never_leaves_the_folder(dist, path):
    c = TestClient(spa_app(dist))
    r = c.get(path)
    assert "TOP SECRET" not in r.text and "nope" not in r.text and "root:" not in r.text
    assert r.status_code in (200, 404)


def test_spa_traversal_with_raw_asgi_paths(dist):
    app = spa_app(dist)
    for raw in ("/../secret.txt", "/assets/../../secret.txt", "/sub/../../secret.txt", "/%2e%2e/secret.txt", "/./../secret.txt"):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "path": raw, "raw_path": raw.encode(),
                 "query_string": b"", "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 1), "server": ("localhost", 80),
                 "scheme": "http", "root_path": ""}
        asyncio.run(app(scope, receive, send))
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert b"TOP SECRET" not in body, raw


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_spa_does_not_follow_symlinks_out(dist):
    (dist / "link.txt").symlink_to(dist.parent / "secret.txt")
    (dist / "linkdir").symlink_to(dist.parent, target_is_directory=True)
    c = TestClient(spa_app(dist))
    assert "TOP SECRET" not in c.get("/link.txt").text
    assert "TOP SECRET" not in c.get("/linkdir/secret.txt").text


def test_spa_unbuilt_is_503_but_the_api_still_404s(tmp_path):
    c = TestClient(spa_app(tmp_path / "nothing"))
    r = c.get("/")
    assert r.status_code == 503 and r.json()["code"] == "not_built" and "npm run build" in r.json()["error"]
    assert c.get("/some/route").status_code == 503
    assert c.get("/api/ping").json() == {"pong": True} and c.get("/api/x").status_code == 404


def test_spa_custom_api_prefix(dist):
    app = FastAPI()
    service.install_spa(app, dist, api_prefix="/v1/")
    c = TestClient(app)
    assert c.get("/v1/x").json()["code"] == "not_found" and c.get("/api/x").status_code == 200       # /api is a client route now


def test_pwa_after_spa_is_not_shadowed(dist):
    app = spa_app(dist)
    service.install_pwa(app, name="Demo", short_name="Demo", theme="#111", background="#222", cache="demo", static_dir=dist)
    c = TestClient(app)
    assert c.get("/sw.js").headers["content-type"].startswith("application/javascript")
    assert c.get("/manifest.webmanifest").json()["name"] == "Demo"
    assert c.get("/anything").status_code == 200 and c.get("/api/x").status_code == 404


# ------------------------------------------------------------------------------------------------ errors

class Body(BaseModel):
    n: int
    label: str


def error_app(**kw):
    app = FastAPI()
    service.install_error_handlers(app)

    @app.get("/http")
    def http_error():
        raise HTTPException(404, "No such thing.", headers={"X-Why": "test"})

    @app.get("/http-dict")
    def http_dict():
        raise HTTPException(409, {"error": "Busy.", "code": "busy", "hint": "Wait."})

    @app.post("/validate")
    def validate(body: Body):
        return body

    @app.get("/query")
    def query(limit: int):
        return {"limit": limit}

    @app.get("/app-error")
    def app_err():
        raise AppError("conflict", "Already there.", hint="Use update.", details={"id": 7})

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaput")

    return app


def test_one_error_envelope():
    c = TestClient(error_app(), raise_server_exceptions=False)
    r = c.get("/http")
    assert r.status_code == 404 and r.json() == {"error": "No such thing."} and r.headers["x-why"] == "test"
    assert c.get("/http-dict").json() == {"error": "Busy.", "code": "busy", "hint": "Wait."} and c.get("/http-dict").status_code == 409
    assert c.get("/unknown-route").json() == {"error": "Not Found"}
    r = c.post("/validate", json={"n": "x"})
    body = r.json()
    assert r.status_code == 400 and body["code"] == "invalid_arguments"
    assert body["error"] == "n: Input should be a valid integer, unable to parse string as an integer; label: Field required"
    assert body["issues"] == [{"loc": "n", "msg": "Input should be a valid integer, unable to parse string as an integer"},
                              {"loc": "label", "msg": "Field required"}]
    assert c.get("/query?limit=abc").json()["error"].startswith("query.limit: ")
    r = c.get("/app-error")
    assert r.status_code == 409 and r.json() == {"id": 7, "error": "Already there.", "code": "conflict", "hint": "Use update."}
    r = c.get("/boom")
    assert r.status_code == 500 and r.json()["code"] == "internal" and "RuntimeError: kaput" in r.json()["error"]
    assert r.headers["content-type"].startswith("application/json")


# ------------------------------------------------------------------------------------------------ PWA

def test_pwa_manifest_and_worker(dist):
    app = FastAPI()
    service.install_pwa(app, name="Kafka's Hoard", short_name="Kafka", theme="#1c1c14", background="#101010", cache="kafka-hoard",
                        lang="es", static_dir=dist)
    c = TestClient(app)
    m = c.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json") and m.headers["cache-control"] == "no-cache"
    data = m.json()
    assert data["name"] == "Kafka's Hoard" and data["short_name"] == "Kafka" and data["theme_color"] == "#1c1c14"
    assert data["background_color"] == "#101010" and data["start_url"] == "/" and data["display"] == "standalone" and data["lang"] == "es"
    assert [i["src"] for i in data["icons"]] == ["/icon-192.png", "/icon-512.png"]
    sw = c.get("/sw.js")
    assert sw.headers["cache-control"] == "no-cache" and sw.headers["service-worker-allowed"] == "/"
    text = sw.text
    assert 'const CACHE_NAME = "kafka-hoard-' in text
    assert 'request.mode === "navigate"' in text and "await fetch(request)" in text            # navigations: network first
    assert text.index('request.mode === "navigate"') < text.index('startsWith("/assets/")')
    assert 'startsWith("/api/")' in text and "cache.match(request)" in text
    first = text.split("CACHE_NAME = ")[1].split(";")[0]
    (dist / "index.html").write_text("<html>a new build</html>")
    second = c.get("/sw.js").text.split("CACHE_NAME = ")[1].split(";")[0]
    assert first != second                                                                         # a new build starts a new cache


def test_pwa_options(tmp_path):
    app = FastAPI()
    icons = [{"src": "/a.png", "sizes": "96x96", "type": "image/png"}]
    service.install_pwa(app, name="N", short_name="n", theme="#000", background="#fff", cache="c", icons=icons, start_url="/app", version="7")
    c = TestClient(app)
    data = c.get("/manifest.webmanifest").json()
    assert data["icons"] == icons and data["start_url"] == "/app" and data["lang"] == "en"
    assert '"c-7"' in c.get("/sw.js").text


# ------------------------------------------------------------------------------------------------ health

def test_health_router(monkeypatch):
    monkeypatch.setattr(family, "health_block", lambda: {"version": "x", "app": "demo"})
    app = FastAPI()
    app.include_router(service.health_router("demo-hoard", "1.2.3"))
    body = TestClient(app).get("/api/health").json()
    assert body == {"service": "demo-hoard", "version": "1.2.3", "hoard_link": {"version": "x", "app": "demo"}}


def test_health_extra_variants_and_protection(monkeypatch):
    monkeypatch.setattr(family, "health_block", lambda: {"ok": True})

    def client(extra):
        app = FastAPI()
        app.include_router(service.health_router("s", "1", extra=extra))
        return TestClient(app).get("/api/health")

    assert client({"counts": {"a": 1}}).json()["counts"] == {"a": 1}
    assert client(lambda: {"scheduler": True}).json()["scheduler"] is True
    assert client(lambda request: {"path": request.url.path}).json()["path"] == "/api/health"
    r = client(lambda: {"service": "evil", "hoard_link": "evil", "n": 1})
    assert r.json()["service"] == "s" and r.json()["hoard_link"] == {"ok": True} and r.json()["n"] == 1

    def broken():
        raise RuntimeError("db locked")
    r = client(broken)
    assert r.status_code == 200 and r.json()["service"] == "s" and "db locked" in r.json()["health_error"]


def test_health_without_family_configuration_has_the_block():
    app = FastAPI()
    app.include_router(service.health_router("s", "1"))
    block = TestClient(app).get("/api/health").json()["hoard_link"]
    assert "family" in block and "version" in block


# ------------------------------------------------------------------------------------------------ run_main

class FakeHealth:
    """A real listening socket whose /api/health answers {"service": <name>}."""

    def __init__(self, service_name):
        payload = json.dumps({"service": service_name}).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200 if self.path == "/api/health" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_health():
    made = []

    def make(name="demo-hoard"):
        h = FakeHealth(name)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


@pytest.fixture
def env(monkeypatch):
    for key in ("DEMO_PORT", "DEMO_DATA_DIR", "HOARD_PORT", "HOARD_DATA_DIR", "PORT_STRICT", "HOARD_NO_BROWSER"):
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    return monkeypatch


def run(**kw):
    base = dict(service="demo-hoard", package="demo_hoard", default_port=5999, app_factory=lambda: object(), data_dir_env="DEMO_DATA_DIR",
                port_env="DEMO_PORT", open_browser_default=False)
    base.update(kw)
    return service.run_main(**base)


def test_run_main_exits_zero_when_this_app_already_runs(fake_health, env, tmp_path, capsys, clean_logging):
    live = fake_health("demo-hoard")
    built = []
    data = tmp_path / "must-not-be-created"
    code = run(argv=["--port", str(live.port), "--data-dir", str(data)], app_factory=lambda: built.append(1), serve=lambda *a, **k: built.append("served"))
    assert code == 0 and built == []
    assert f"already running on http://127.0.0.1:{live.port}" in capsys.readouterr().out
    assert not data.exists()                                                        # decided before touching the data folder
    assert "DEMO_DATA_DIR" not in os.environ and "HOARD_PORT" not in os.environ and "DEMO_PORT" not in os.environ


def test_run_main_already_running_via_environment_port(fake_health, env, capsys, clean_logging):
    live = fake_health("demo-hoard")
    env.setenv("DEMO_PORT", str(live.port))
    assert run(argv=[], app_factory=lambda: pytest.fail("built")) == 0
    assert "already running" in capsys.readouterr().out


def test_run_main_opens_the_browser_at_the_running_instance(fake_health, env, clean_logging):
    live = fake_health("demo-hoard")
    opened = []
    env.setattr(net, "open_in_browser", lambda url: opened.append(url) or True)
    assert run(argv=["--port", str(live.port)], open_browser_default=True) == 0
    assert opened == [f"http://127.0.0.1:{live.port}"]
    opened.clear()
    assert run(argv=["--port", str(live.port), "--no-browser"], open_browser_default=True) == 0
    env.setenv("HOARD_NO_BROWSER", "1")
    assert run(argv=["--port", str(live.port)], open_browser_default=True) == 0
    assert opened == []
    env.delenv("HOARD_NO_BROWSER")
    assert run(argv=["--port", str(live.port), "--browser"], open_browser_default=False) == 0
    assert opened == [f"http://127.0.0.1:{live.port}"]


def test_run_main_strict_port_taken_by_another_program(fake_health, env, capsys, clean_logging):
    other = fake_health("some-other-service")
    env.setenv("PORT_STRICT", "1")
    assert run(argv=["--port", str(other.port)], app_factory=lambda: pytest.fail("built")) == 1
    assert "taken by another program" in capsys.readouterr().out


def test_run_main_finds_the_next_port_unless_strict(fake_health, env, tmp_path, clean_logging):
    other = fake_health("some-other-service")
    served = {}
    code = run(argv=["--port", str(other.port), "--data-dir", str(tmp_path / "d")], serve=lambda app, host, port: served.update(host=host, port=port))
    assert code == 0 and served["port"] > other.port and served["port"] - other.port <= 20 and served["host"] == "127.0.0.1"


def test_run_main_full_path_exports_environment_and_logs(env, tmp_path, capsys, clean_logging):
    data = tmp_path / "my data" / "ünï"
    served, seen = {}, {}

    def factory(port):
        seen["port"] = port
        seen["env"] = (os.environ["DEMO_PORT"], os.environ["DEMO_DATA_DIR"])
        return "the-asgi-app"

    free = net.free_port()
    code = run(argv=["--port", str(free), "--data-dir", str(data), "--host", "127.0.0.1"], app_factory=factory,
               serve=lambda app, host, port: served.update(app=app, host=host, port=port))
    assert code == 0
    assert seen == {"port": free, "env": (str(free), str(data))}
    assert served == {"app": "the-asgi-app", "host": "127.0.0.1", "port": free}
    log_file = data / "logs" / "demo-hoard.log"
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_file.exists() and f"listening on http://127.0.0.1:{free}" in log_file.read_text(encoding="utf-8")
    assert f"listening on http://127.0.0.1:{free}" in capsys.readouterr().out


def test_run_main_default_data_dir_and_environment_port(env, tmp_path, clean_logging):
    free = net.free_port()
    env.setenv("DEMO_PORT", str(free))
    env.setenv("DEMO_DATA_DIR", str(tmp_path / "from-env"))
    served = {}
    assert run(argv=[], serve=lambda app, host, port: served.update(port=port)) == 0
    assert served["port"] == free and (tmp_path / "from-env" / "logs").is_dir()
    env.delenv("DEMO_DATA_DIR")
    env.setenv("HOARD_PORT", "x")
    env.delenv("HOARD_PORT")
    served.clear()
    assert run(argv=["--port", str(free)], port_env=None, data_dir_env=None, default_data_dir=tmp_path / "dflt", serve=lambda app, host, port: served.update(port=port)) == 0
    assert (tmp_path / "dflt" / "logs").is_dir() and os.environ["HOARD_PORT"] == str(free)


def test_run_main_factory_forms(env, tmp_path, clean_logging):
    pkg = tmp_path / "pkgs"
    (pkg / "demo_main").mkdir(parents=True)
    (pkg / "demo_main" / "__init__.py").write_text("")
    (pkg / "demo_main" / "app.py").write_text(
        "calls = []\n"
        "instance = type('A', (), {'__call__': lambda self, *a: None})()\n"
        "def create_app():\n    calls.append('zero')\n    return 'zero-arg app'\n"
        "def with_port(port):\n    calls.append(port)\n    return f'port app {port}'\n")
    env.syspath_prepend(str(pkg))
    free = net.free_port()
    served = []
    capture = lambda app, host, port: served.append(app)  # noqa: E731
    base = dict(argv=["--port", str(free), "--data-dir", str(tmp_path / "d")], serve=capture)
    assert run(app_factory="demo_main.app:create_app", **base) == 0 and served[-1] == "zero-arg app"
    assert run(app_factory="demo_main.app:with_port", **base) == 0 and served[-1] == f"port app {free}"
    import demo_main.app as mod
    assert run(app_factory="demo_main.app:instance", **base) == 0 and served[-1] is mod.instance      # an ASGI app object is used as it is
    assert run(app_factory="not-a-spec", **base) == 1
    assert run(app_factory="demo_main.app:missing", **base) == 1


def test_run_main_failures_return_one(env, tmp_path, capsys, clean_logging):
    free = net.free_port()

    def broken_factory():
        raise RuntimeError("database is locked")

    code = run(argv=["--port", str(free), "--data-dir", str(tmp_path / "d")], app_factory=broken_factory)
    assert code == 1 and "could not start" in capsys.readouterr().out
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "database is locked" in (tmp_path / "d" / "logs" / "demo-hoard.log").read_text(encoding="utf-8")

    def crash(app, host, port):
        raise OSError("address in use")

    assert run(argv=["--port", str(net.free_port()), "--data-dir", str(tmp_path / "d")], serve=crash) == 1

    def interrupt(app, host, port):
        raise KeyboardInterrupt

    assert run(argv=["--port", str(net.free_port()), "--data-dir", str(tmp_path / "d")], serve=interrupt) == 0

    def leave(app, host, port):
        raise SystemExit(3)

    assert run(argv=["--port", str(net.free_port()), "--data-dir", str(tmp_path / "d")], serve=leave) == 3


def test_run_main_argument_errors(env, capsys, clean_logging):
    assert run(argv=["--port", "abc"], app_factory=lambda: pytest.fail("built")) == 2
    assert run(argv=["--port", "70000"], app_factory=lambda: pytest.fail("built")) == 2
    assert run(argv=["--bogus"], app_factory=lambda: pytest.fail("built")) == 2
    assert run(argv=["--help"], app_factory=lambda: pytest.fail("built")) == 0
    assert "--data-dir" in capsys.readouterr().out


def test_run_main_does_not_open_the_browser_for_a_fresh_start_unless_asked(env, tmp_path, clean_logging):
    opened = []
    env.setattr(service, "_open_when_up", lambda url, name: opened.append(url))
    free = net.free_port()
    run(argv=["--port", str(free), "--data-dir", str(tmp_path / "d")], serve=lambda *a, **k: None)
    assert opened == []
    run(argv=["--port", str(free), "--data-dir", str(tmp_path / "d"), "--browser"], serve=lambda *a, **k: None)
    assert opened == [f"http://127.0.0.1:{free}"]
    opened.clear()
    run(argv=["--port", str(free), "--data-dir", str(tmp_path / "d"), "--no-browser"], open_browser_default=True, serve=lambda *a, **k: None)
    assert opened == []


def test_run_main_with_uvicorn_missing(env, tmp_path, capsys, clean_logging, monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    assert run(argv=["--port", str(net.free_port()), "--data-dir", str(tmp_path / "d")]) == 1
    assert "uvicorn" in capsys.readouterr().out
