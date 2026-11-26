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
from hoard_link.hub import services as family_services
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


def test_common_voice_status_is_passive_with_the_registered_provider(monkeypatch):
    names = {name for key in ("stt", "tts", "dictation") for name in family_services.SERVICE_TOOLS[key]}
    provider = SimpleNamespace(id="boltagon", name="Voice", url="http://fixture.invalid")
    apps = {"boltagon": provider, "cyrano": provider}
    observed = []

    def catalogue(app, timeout):
        observed.append(app.id)
        return {"ok": True, "tools": [{"name": name} for name in names]}

    monkeypatch.setattr(family_services.contract, "app_tools", catalogue)
    hub = SimpleNamespace(get=apps.get, facet=lambda name: None, automation_blocked=lambda app: False)
    result = family_services.ServicesFacet(hub).handlers()["hub_services_status"]({})
    voice = {row["service"]: row for row in result["services"] if row["service"] in {"stt", "tts", "dictation"}}
    assert set(voice) == {"stt", "tts", "dictation"}
    assert all(row["available"] and row["owner"] == "boltagon" for row in voice.values())
    assert observed == ["boltagon"] * 3


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


@pytest.mark.parametrize("named", [False, True])
def test_comfy_start_reserves_one_gpu_until_the_owned_process_exits(served, tmp_path, monkeypatch, named):
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
    port = free_port()
    alias = f"comfyui@{port}"
    if named:
        (install / "comfyui_version.py").write_text('__version__ = "0.46.0"', encoding="utf-8")
        profile_file = install / "depth.safetensors"
        register_args = {"name": "depth", "directory": str(install), "python": sys.executable, "port": port,
                         "templates": ["qwen21_depth"], "models": [profile_file.name], "startup_vram_mb": 768}
        registration = tools.call(hub, "hub_comfy_profile_register", register_args)
        assert registration["ok"]
        assert tools.call(hub, "hub_comfy_profile_register", register_args)["existing"]
        assert not tools.call(hub, "hub_comfy_profile_register", {**register_args, "name": "other"})["ok"]
        assert not hub.service_start(alias, gpu=1)["ok"]
        _, profiles = _req(url + "/api/services/comfy-profiles")
        item, = profiles["items"]
        assert item["state"] == "unavailable"
        assert item["profile"] == {"name": "depth", "version": "0.46.0", "templates": ["qwen21_depth"],
                                   "models": [{"name": profile_file.name, "available": False}]}
        profile_file.touch()
    else:
        hub.launcher.set_config({"comfyui": {"dir": str(install), "python": sys.executable, "startup_vram_mb": 512}})
    args = {"id": alias, "gpu": 1, "wait_s": 20}
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
            result["lease_id"], result["pid"], 1, 768 if named else 512, True)
        assert lease["purpose"] == ("comfyui:depth" if named else alias)
        if named:
            undo = registration["undo"]
            assert not tools.call(hub, undo["tool"], undo["arguments"])["ok"]
        assert _req(url + "/api/services/start", args)[1]["already"]
        assert len(hub.leases.status()["leases"]) == 1
        assert tools.call(hub, "hub_service_stop", {"id": args["id"], "expected_pid": result["pid"] + 1})["ok"] is False
        assert tools.call(hub, result["undo"]["tool"], result["undo"]["arguments"])["ok"]
        assert hub.leases.status()["leases"] == []
        if named:
            restore = tools.call(hub, undo["tool"], undo["arguments"])["undo"]
            assert tools.call(hub, restore["tool"], restore["arguments"])["ok"]
            assert not tools.call(hub, undo["tool"], undo["arguments"])["ok"]
            assert hub.launcher.get(alias).id == "comfyui:depth"
            assert hub.launcher.get("comfyui").id == "comfyui@8188"
    finally:
        hub.leases.release(full["lease_id"])
        hub.service_stop(args["id"])


def test_auto_uses_arbiter_and_a_manual_service_lease_covers_its_work(served, tmp_path, monkeypatch):
    hub, url = served
    hub.leases.protected_gpus = [0]
    monkeypatch.setattr(hub.leases, "_gpu_fn", lambda: [
        SimpleNamespace(index=i, total_mb=mb, used_mb=0) for i, mb in enumerate((20000, 16000, 12000))
    ])
    install = tmp_path / "renderer"
    (install / "comfy").mkdir(parents=True)
    (install / "comfy" / "cli_args.py").write_text('parser.add_argument("--cuda-device")', encoding="utf-8")
    (install / "main.py").write_text(
        "import argparse\nfrom http.server import HTTPServer, SimpleHTTPRequestHandler\n"
        "p = argparse.ArgumentParser()\np.add_argument('--port', type=int)\n"
        "p.add_argument('--cuda-device', type=int)\na, _ = p.parse_known_args()\n"
        "assert a.cuda_device == 2\nHTTPServer(('127.0.0.1', a.port), SimpleHTTPRequestHandler).serve_forever()\n",
        encoding="utf-8")
    hub.launcher.set_config({"comfyui": {"dir": str(install), "python": sys.executable}})
    args = {"id": f"comfyui@{free_port()}", "wait_s": 20}
    occupied = hub.leases.request(vram_mb=15500, gpu=1)["lease_id"]
    manual = work = waiting = cold_work = None
    try:
        assert _req(url + "/api/services/start", {**args, "gpu": 1})[0] == 409
        auto = tools.call(hub, "hub_service_start", args)
        assert auto["ready"] and auto["gpu"] == 2, auto
        assert tools.call(hub, auto["undo"]["tool"], auto["undo"]["arguments"])["ok"]
        manual = hub.leases.request(vram_mb=11500, gpu=2)["lease_id"]
        service = tools.call(hub, "hub_service_start", {**args, "lease_id": manual})
        assert service["ready"] and service["lease_id"] == manual
        repeated = tools.call(hub, "hub_service_start", {**args, "lease_id": manual})
        assert repeated["already"] and repeated["undo"] == service["undo"]
        request = {"owner": "prospero", "purpose": "image from shared service", "service": args["id"],
                   "vram_mb": 11000, "gpu": 2}
        _, job = _req(url + "/api/lease/request", request)
        work = job["lease_id"]
        assert job["state"] == "granted"
        _, next_job = _req(url + "/api/lease/request", request)
        waiting = next_job["lease_id"]
        assert next_job["state"] == "queued"
        status = hub.leases.status()
        gpu = next(card for card in status["gpus"] if card["index"] == 2)
        assert (gpu["reserved_mb"], gpu["leases"]) == (11500, 1)
        shared = next(lease for lease in status["leases"] if lease["id"] == manual)
        assert shared["work"]["lease_id"] == work
        hub.leases.release(work)
        assert hub.leases.get(waiting)["state"] == "granted"
        hub.leases.release(waiting)
        assert hub.leases.get(manual)["lease"]["process_bound"]
        assert tools.call(hub, service["undo"]["tool"], service["undo"]["arguments"])["ok"]
        assert not hub.leases.get(manual)["ok"]
        _, cold = _req(url + "/api/lease/request", {**request, "vram_mb": 11500})
        cold_work = cold["lease_id"]
        assert cold["state"] == "granted"
        code, cold_service = _req(url + "/api/services/start", {**args, "work_lease_id": cold_work})
        assert code == 200 and cold_service["gpu"] == 2 and cold_service["ready"], cold_service
        cold_lease = next(lease for lease in hub.leases.status()["leases"] if lease["gpu"] == 2)
        assert cold_lease["vram_mb"] == 11500 and cold_lease["work"]["lease_id"] == cold_work
        hub.leases.release(cold_work)
        assert hub.leases.get(cold_service["lease_id"])["lease"]["vram_mb"] == 512
    finally:
        for lease_id in (work, waiting, manual, occupied, cold_work):
            if lease_id:
                hub.leases.release(lease_id)
        hub.service_stop(args["id"])
