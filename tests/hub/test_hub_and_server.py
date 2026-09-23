from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from hoard_link.hub import SERVICE
from hoard_link.hub import procs, tools
from hoard_link.hub.server import make_server


def _http(url: str, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


@pytest.fixture
def served(hub):
    server = make_server(hub, port=hub.config.port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield hub, hub.config.url
    server.shutdown()


# ---- core ------------------------------------------------------------------

def test_snapshot_states(hub):
    snap = hub.snapshot()
    by = {a["id"]: a for a in snap["apps"]}
    assert by["fake"]["state"] == "running" and by["fake"]["health"]["service"] == "fake-hoard"
    assert by["dead"]["state"] == "down" and by["dead"]["process"] is None
    assert by["launch"]["state"] == "down"
    assert snap["counts"]["running"] == 1
    assert snap["faustus"]["reachable"] is False


def test_foreign_port_is_not_ours(hub, family, tmp_path):
    from .conftest import write_manifest
    # A manifest claiming the fake server's port under another service name.
    write_manifest(family["root"] / "Impostor", "impostor", family["fake_port"], service="other-hoard")
    hub.rescan()
    st = hub.app_status(hub.get("impostor"))
    assert st["state"] == "foreign" and st["stoppable"] is False
    assert hub.stop("impostor")["ok"] is False


def test_start_stop_launchable(hub, family):
    res = hub.start("launch")
    assert res["ok"] and res.get("ready"), res
    st = hub.app_status(hub.get("launch"))
    assert st["state"] == "running" and st["process"]["pid"]
    again = hub.start("launch")
    assert again.get("already")
    log = hub.log_tail("launch")
    assert log["ok"] and any("hoard-hub start" in line for line in log["lines"])
    stopped = hub.stop("launch")
    assert stopped["ok"] and st["process"]["pid"] in stopped["stopped"]
    time.sleep(0.3)
    assert hub.app_status(hub.get("launch"))["state"] == "down"
    assert hub.stop("launch")["detail"] == "not running"


def test_start_refuses_unlaunchable_and_unknown(hub):
    assert hub.start("dead")["ok"] is False
    assert "unknown app" in hub.start("nope")["error"]
    assert hub.open("dead")["ok"] is False


def test_terminate_tree_protects_itself():
    import os
    assert procs.terminate_tree(os.getpid())["ok"] is False


def test_tool_catalogue_first_lines_are_short():
    for tool in tools.catalogue():
        first = tool["description"].splitlines()[0]
        assert len(first) <= 110, (tool["name"], len(first))
        assert tool["inputSchema"]["type"] == "object"


def test_tools_call_roundtrip(hub):
    out = tools.call(hub, "hub_list_apps", {})
    assert {a["id"] for a in out["apps"]} == {"fake", "dead", "launch"}
    assert tools.call(hub, "hub_app_status", {"app": "fake"})["state"] == "running"
    assert tools.call(hub, "nope", {})["ok"] is False
    assert tools.call(hub, "hub_rescan", {})["ok"]


# ---- http ------------------------------------------------------------------

def test_health_and_ui(served):
    hub, url = served
    status, body = _http(url + "/api/health")
    assert status == 200 and body["service"] == SERVICE
    req = urllib.request.Request(url + "/")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as resp:
        html = resp.read().decode()
    assert "Hoard Hub" in html and "/ui/app.js" in html
    req = urllib.request.Request(url + "/api/apps/fake/icon")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as resp:
        assert resp.headers["Content-Type"].startswith("image/png")
    req = urllib.request.Request(url + "/api/apps/dead/icon")  # has an icon too; a missing one falls back
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as resp:
        assert resp.status == 200


def test_api_apps_and_actions(served):
    hub, url = served
    status, snap = _http(url + "/api/apps")
    assert status == 200 and {a["id"] for a in snap["apps"]} == {"fake", "dead", "launch"}
    status, one = _http(url + "/api/apps/fake")
    assert status == 200 and one["state"] == "running"
    assert _http(url + "/api/apps/nope")[0] == 404
    status, res = _http(url + "/api/apps/launch/start", {})
    assert status == 200 and res["ok"], res
    status, res = _http(url + "/api/apps/launch/log?lines=5")
    assert status == 200 and res["lines"]
    status, res = _http(url + "/api/apps/launch/stop", {})
    assert status == 200 and res["ok"]
    status, res = _http(url + "/api/apps/dead/start", {})
    assert status == 409 and res["ok"] is False
    status, res = _http(url + "/api/apps/fake/bogus", {})
    assert status == 404


def test_cross_site_and_agent_token(served):
    hub, url = served
    status, _ = _http(url + "/api/apps/launch/start", {}, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})
    assert status == 403
    status, _ = _http(url + "/api/apps", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})
    assert status == 403
    status, _ = _http(url + "/api/agent/tools")
    assert status == 401
    status, body = _http(url + "/api/agent/tools", headers={"Authorization": "Bearer " + hub.token})
    assert status == 200 and any(t["name"] == "hub_list_apps" for t in body["tools"])
    status, body = _http(url + "/api/agent/call", {"tool": "hub_list_apps", "arguments": {}},
                         headers={"Authorization": "Bearer " + hub.token})
    assert status == 200 and body["ok"] and body["result"]["counts"]["total"] == 3
    status, body = _http(url + "/api/agent/call", {"tool": "hub_start_app", "arguments": {"app": "dead"}},
                         headers={"Authorization": "Bearer " + hub.token})
    assert status == 400 and body["ok"] is False


def test_token_persists(hub):
    from hoard_link.hub.core import Hub
    other = Hub(hub.config)
    assert other.token == hub.token


def test_config_roundtrip(tmp_path):
    from hoard_link.hub.config import HubConfig
    cfg = HubConfig(data_dir=str(tmp_path), roots=["/a"], browser="chrome", port=1234)
    cfg.save()
    loaded = HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path), "HOARD_HUB_PORT": "4321"})
    assert loaded.browser == "chrome" and loaded.port == 4321 and loaded.data_dir == str(tmp_path)
    loaded = HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path), "HOARD_HUB_ROOTS": "/x;/y"})
    assert [r.replace("\\", "/")[-2:] for r in loaded.roots] == ["/x", "/y"]
