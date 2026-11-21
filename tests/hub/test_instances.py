"""Instances: a copy of an app on another data folder speaks to the hub as that app."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

import pytest

from hoard_link.hub import tools
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server

from .conftest import free_port, write_manifest


def _http(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


@pytest.fixture
def qa_dir(tmp_path):
    d = tmp_path / "qa-data"
    d.mkdir()
    (d / "mcp-token").write_text("qa-secret\n", encoding="utf-8")
    return d


def test_listed_instance_token_counts_as_the_app(hub, qa_dir, monkeypatch):
    from hoard_link.hub import contract
    monkeypatch.setattr(contract, "TOKEN_CACHE_S", 0.0)
    inst = hub.facet("instances")
    assert inst is not None
    assert hub.token_owner("qa-secret") is None
    assert inst.add("nobody", data_dir=str(qa_dir))["ok"] is False
    assert inst.add("fake")["ok"] is False  # no folder, no file
    res = inst.add("fake", data_dir=str(qa_dir), label="QA copy")
    assert res["ok"] and res["instance"]["token_present"] and res["instance"]["token_file"].endswith("mcp-token")
    assert hub.token_owner("qa-secret") == "fake"
    assert hub.token_owner("wrong") is None and hub.token_owner("") is None
    # adding it again does not duplicate; the list says what each row is
    inst.add("fake", token_file=str(qa_dir / "mcp-token"))
    rows = inst.list()["instances"]
    assert len(rows) == 1 and rows[0]["registered"] and rows[0]["token_present"]
    # a rotated token is read again on the next call
    (qa_dir / "mcp-token").write_text("rotated", encoding="utf-8")
    assert hub.token_owner("rotated") == "fake"
    assert inst.remove(app="fake", data_dir=str(qa_dir))["removed"] == 1
    assert hub.token_owner("rotated") is None
    assert inst.remove()["ok"] is False


def test_the_apps_own_token_file_is_not_listed_twice(hub):
    own = hub.get("fake").token_file
    res = hub.facet("instances").add("fake", token_file=own)
    assert res["ok"] and "own token file" in res["note"]
    assert hub.facet("instances").list()["instances"] == []


def test_instances_over_http_and_as_tools(hub, qa_dir):
    server = make_server(hub, port=hub.config.port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = hub.config.url
    try:
        status, _ = _http(url + "/api/instances", headers={"Authorization": "Bearer qa-secret"})
        assert status == 401  # not listed yet: the hub does not know this token
        status, _ = _http(url + "/api/instances/add", {"app": "fake", "data_dir": str(qa_dir)})
        assert status == 403  # neither the page nor the hub's token
        status, body = _http(url + "/api/instances/add", {"app": "fake", "data_dir": str(qa_dir)},
                             headers={"Authorization": "Bearer " + hub.token})
        assert status == 200 and body["ok"], body
        status, body = _http(url + "/api/instances", headers={"Authorization": "Bearer qa-secret"})
        assert status == 200 and body["instances"][0]["app"] == "fake"
        status, body = _http(url + "/api/instances/remove", {"app": "fake"},
                             headers={"Authorization": "Bearer " + hub.token})
        assert status == 200 and body["removed"] == 1
    finally:
        server.shutdown()
    names = {tool["name"] for tool in tools.all_tools()}
    assert {"hub_instance_list", "hub_instance_add", "hub_instance_remove"} <= names
    assert tools.call(hub, "hub_instance_add", {"app": "fake", "data_dir": str(qa_dir)})["ok"]
    assert tools.call(hub, "hub_instance_list", {})["instances"][0]["token_present"]
    assert tools.call(hub, "hub_instance_remove", {"app": "fake"})["removed"] == 1


def test_private_catalog_starts_without_desktop_or_token_copy(tmp_path, fake_app, monkeypatch):
    app_root = tmp_path / "apps"
    app_port, _ = fake_app
    script = tmp_path / "isolated.py"
    script.write_text("""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

assert os.environ["HOARD_NO_BROWSER"] == "1"
assert "HOARD_HUB_TOKEN_FILE" not in os.environ
Path(os.environ["FAKE_TOKEN_FILE"]).write_text("private-fake", encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"service": "fake-hoard"}).encode())

HTTPServer(("127.0.0.1", int(os.environ["FAKE_PORT"])), Handler).serve_forever()
""", encoding="utf-8")
    launch = {"executable": sys.executable, "argv": [str(script)], "env": {"FAKE_PORT": str(app_port)}}
    book = write_manifest(app_root / "fake", "fake", app_port, launch=launch, extra={
        "x-family": {"isolated_instance": {"data_env": "FAKE_DATA", "port_env": "FAKE_PORT",
                                            "token_env": "FAKE_TOKEN_FILE"}}})
    normal_token = book / "data" / "mcp-token"
    normal_token.parent.mkdir()
    normal_token.write_text("real-fake", encoding="utf-8")
    write_manifest(app_root / "desktop", "desktop", app_port, launch={**launch, "opens_window": True})
    data = tmp_path / "hub"
    config = HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(data), "HOARD_HUB_PORT": str(free_port()),
                                 "HOARD_HUB_ROOTS": str(app_root), "HOARD_HUB_JOBS": "0"})
    monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", str(normal_token))
    from hoard_link.hub import desktop

    def fail_on_desktop(*args, **kwargs):
        pytest.fail("the isolated Hub invoked the desktop")

    monkeypatch.setattr(desktop, "open_window", fail_on_desktop)
    monkeypatch.setattr(desktop, "open_in_browser", fail_on_desktop)
    monkeypatch.setattr(desktop, "close_windows", fail_on_desktop)
    hub = Hub(config)
    try:
        assert hub.token_owner("real-fake") is None
        assert not hub.start("desktop")["ok"]
        assert hub.get("desktop").url == ""
        opened = hub.open("fake", mode="browser")
        assert opened["ok"] and opened["ready"] and opened["mode"] == "headless", opened
        assert hub.get("fake").port != app_port
        assert hub.token_owner("private-fake") == "fake"
        hub.rescan()
        assert hub.get("fake").url == opened["url"]
        assert hub.stop("fake")["ok"]
        assert _http(f"http://127.0.0.1:{app_port}/api/health")[0] == 200
    finally:
        hub.close()


def test_isolated_command_rejects_shared_data_and_never_opens_hub(tmp_path, monkeypatch):
    from hoard_link.hub import __main__ as cli

    tested = tmp_path / "hub"
    port = free_port()
    monkeypatch.delenv("HOARD_HUB_DATA_DIR", raising=False)
    monkeypatch.delenv("HOARD_HUB_ISOLATED_CATALOG", raising=False)
    assert cli.main(["--port", str(port)]) == 2
    tested.mkdir()
    marker = tested / "url"
    marker.write_text("http://127.0.0.1:8810", encoding="utf-8")
    assert cli.main(["--port", str(port), "--data-dir", str(tested)]) == 2
    monkeypatch.setattr(cli, "_already_running", lambda url: True)
    monkeypatch.setattr(cli, "_open_own_window", lambda *a: pytest.fail("opened the Hub"))
    marker.write_text(f"http://127.0.0.1:{port}", encoding="utf-8")
    assert cli.main(["--port", str(port), "--data-dir", str(tested), "--browser"]) == 0
