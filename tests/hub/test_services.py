"""Hub: the shared backend servers (hoard_link.launch) as tools, routes and events."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from hoard_link.hub import tools
from hoard_link.hub.server import make_server
from hoard_link.launch import Launcher

from .conftest import free_port


@pytest.fixture
def served(hub):
    server = make_server(hub, port=hub.config.port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield hub, hub.config.url
    server.shutdown()


def _req(url: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_services_are_listed_as_tools(hub):
    names = {t["name"] for t in tools.catalogue()}
    assert {"hub_services", "hub_service_start", "hub_service_stop", "hub_gpu_memory"} <= names
    mem = tools.call(hub, "hub_gpu_memory", {})
    assert mem["ok"] and "gpus" in mem and "services" in mem
    ro = {t["name"]: t.get("annotations", {}).get("readOnlyHint") for t in tools.catalogue()}
    assert ro["hub_services"] is True and not ro.get("hub_service_start")
    out = tools.call(hub, "hub_services", {})
    ids = {s["id"] for s in out["items"]}
    assert "comfyui@8188" in ids and "ollama" in ids


def test_start_and_stop_a_configured_server_from_the_hub(served, tmp_path):
    hub, url = served
    port = free_port()
    Launcher().set_config({"commands": [{"id": "web", "label": "Web", "argv": [sys.executable, "-m", "http.server", str(port),
                                                                              "--bind", "127.0.0.1"],
                                         "cwd": str(tmp_path), "health": f"http://127.0.0.1:{port}/"}]})
    code, res = _req(url + "/api/services/start", {"id": "cmd:web", "wait_s": 20})
    try:
        assert code == 200 and res["ok"] and res["ready"], res
        code, listing = _req(url + "/api/services")
        web = next(s for s in listing["items"] if s["id"] == "cmd:web")
        assert web["state"] == "running" and web["started_by"] == "hub" and web["stoppable"]
        assert any(e["type"] == "hub.service.started" for e in hub.events.query(type="hub.service.*"))
        res = tools.call(hub, "hub_service_stop", {"id": "cmd:web"})
        assert res["ok"], res
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and Launcher().status(Launcher().get("cmd:web"))["state"] != "down":
            time.sleep(0.3)
        assert Launcher().status(Launcher().get("cmd:web"))["state"] == "down"
    finally:
        Launcher().stop("cmd:web")


def test_unknown_service_is_a_409(served):
    _, url = served
    code, res = _req(url + "/api/services/start", {"id": "nope"})
    assert code == 409 and res["ok"] is False


def test_comfy_start_reserves_one_gpu_until_the_owned_process_exits(served, tmp_path, monkeypatch):
    hub, url = served
    monkeypatch.setattr(hub.leases, "_gpu_fn", lambda: [SimpleNamespace(index=1, total_mb=4096, used_mb=0)])
    install = tmp_path / "ComfyUI"
    (install / "comfy").mkdir(parents=True)
    (install / "comfy" / "cli_args.py").write_text('parser.add_argument("--cuda-device")', encoding="utf-8")
    (install / "main.py").write_text(
        "import argparse\nfrom http.server import HTTPServer, SimpleHTTPRequestHandler\n"
        "p = argparse.ArgumentParser()\np.add_argument('--port', type=int)\n"
        "p.add_argument('--cuda-device', type=int)\na, _ = p.parse_known_args()\n"
        "assert a.cuda_device == 1\nHTTPServer(('127.0.0.1', a.port), SimpleHTTPRequestHandler).serve_forever()\n",
        encoding="utf-8")
    hub.launcher.set_config({"comfyui": {"dir": str(install), "python": sys.executable, "startup_vram_mb": 512}})
    port = free_port()
    args = {"id": f"comfyui@{port}", "gpu": 1, "wait_s": 20}
    full = hub.leases.request(owner="job", vram_mb=3800, gpu=1)
    try:
        code, denied = _req(url + "/api/services/start", args)
        assert code == 409 and "GPU" in denied["error"]
        assert not hub.leases.status()["queue"]
        hub.leases.release(full["lease_id"])
        code, result = _req(url + "/api/services/start", args)
        assert code == 200 and result["ready"], result
        assert result["undo"]["tool"] == "hub_service_stop"
        status = tools.call(hub, "hub_lease_status", {})
        lease, = status["leases"]
        assert (lease["lease_id"], lease["pid"], lease["gpu"], lease["vram_mb"], lease["process_bound"]) == (
            result["lease_id"], result["pid"], 1, 512, True)
        assert _req(url + "/api/services/start", args)[1]["already"]
        assert len(hub.leases.status()["leases"]) == 1
        assert tools.call(hub, "hub_service_stop", {"id": args["id"], "expected_pid": result["pid"] + 1})["ok"] is False
        assert tools.call(hub, result["undo"]["tool"], result["undo"]["arguments"])["ok"]
        assert hub.leases.status()["leases"] == []
    finally:
        hub.leases.release(full["lease_id"])
        hub.service_stop(args["id"])
