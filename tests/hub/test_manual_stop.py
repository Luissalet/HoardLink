"""A deliberate stop outlives recovery rules, queued starts and Hub restarts."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from hoard_link.hub import actions, procs, tools
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.faustus import FaustusRuntime
from .test_hub_and_server import served, _http


def test_manual_stop_blocks_delayed_recovery_until_explicit_start(hub, monkeypatch):
    monkeypatch.setattr(hub, "_stop_app", lambda app: {"ok": True, "app": app.id})
    launched = []
    monkeypatch.setattr(procs, "start_app", lambda app, *args, **kw: launched.append(app.id) or {"ok": True})
    hub.stop("launch")
    event = {"event": {"data": {"app": "launch"}}}
    recovery = actions.run_one(hub, {"kind": "start_app", "app": "${event.data.app}"}, event, caller="rule:recover")
    assert recovery["ok"] and recovery["skipped"] and not launched
    assert hub.start("launch", automatic=True)["ok"] is False
    assert hub.start("launch")["ok"]
    assert launched == ["launch"] and not hub.automation_blocked("launch")
    assert hub.start("launch", automatic=True)["ok"]


def test_manual_stop_survives_hub_restart(hub, monkeypatch):
    monkeypatch.setattr(hub, "_stop_app", lambda app: {"ok": True})
    hub.stop("launch")
    hub.close()
    restarted = Hub(hub.config)
    try:
        assert restarted.automation_blocked("launch")
        assert restarted.start("launch", automatic=True)["ok"] is False
    finally:
        restarted.close()


def test_start_in_flight_is_finished_before_stop_and_cannot_respawn(hub, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    order = []
    def start(app, *args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        order.append("start")
        return {"ok": True}
    monkeypatch.setattr(procs, "start_app", start)
    monkeypatch.setattr(hub, "_stop_app", lambda app: order.append("stop") or {"ok": True})
    starter = threading.Thread(target=lambda: hub.start("launch", wait=False))
    stopper = threading.Thread(target=lambda: hub.stop("launch"))
    starter.start()
    assert entered.wait(5)
    stopper.start()
    proceed.set()
    starter.join(5); stopper.join(5)
    assert not starter.is_alive() and not stopper.is_alive()
    assert order == ["start", "stop"] and hub.automation_blocked("launch")


def test_stop_all_records_intent_before_stopping_and_reports_every_failure(hub, monkeypatch):
    calls = []
    monkeypatch.setattr(hub.faustus_runtime, "available", lambda: True)
    def faustus_stop():
        assert all(hub.automation_blocked(a.id) for a in hub.apps)
        assert hub.start("launch")["ok"] is False
        calls.append("faustus")
        return {"ok": True}
    monkeypatch.setattr(hub, "faustus_stop", faustus_stop)
    def stop(app):
        assert calls[0] == "faustus"
        return {"ok": app.id != "fake", "app": app.id, "error": "access denied"}
    monkeypatch.setattr(hub, "_stop_app", stop)
    monkeypatch.setattr(hub.launcher, "statuses", lambda: [{"id": "cmd:model", "state": "running", "stoppable": True}])
    monkeypatch.setattr(hub.launcher, "stop", lambda sid: calls.append(sid) or {"ok": False, "error": "watchdog still alive"})
    result = hub.stop_all()
    assert not result["ok"]
    assert result["faustus"]["ok"] and not result["services"][0]["ok"]
    assert calls == ["faustus", "cmd:model"]
    assert not hub._stopping_all.is_set() and hub.automation_blocked("launch")


def test_stopping_app_before_it_listens_kills_its_verified_launcher(hub, monkeypatch):
    app = hub.get("launch")
    hub._spawned[app.id] = 98765
    hub._spawned_created[app.id] = 123.0
    monkeypatch.setattr(procs, "health", lambda app: procs.Health("down"))
    monkeypatch.setattr(procs, "find_app_process", lambda app: None)
    monkeypatch.setattr(procs, "pid_running", lambda pid: True)
    monkeypatch.setattr(procs, "proc_info", lambda pid: SimpleNamespace(pid=pid, created_at=123.0, cwd=app.folder, cmdline="", name="python"))
    killed = []
    monkeypatch.setattr(procs, "terminate_tree", lambda pid, **kw: killed.append(pid) or {"ok": True})
    assert hub.stop(app.id)["ok"]
    assert killed == [98765] and app.id not in hub._inflight


def test_recovery_through_hub_tool_cannot_clear_stop(hub, monkeypatch):
    monkeypatch.setattr(hub, "_stop_app", lambda app: {"ok": True})
    hub.stop("launch")
    out = actions.run_one(hub, {"kind": "hub", "tool": "hub_start_app", "args": {"app": "launch"}}, {}, caller="job:recover")
    assert out["skipped"] and hub.automation_blocked("launch")


def test_stop_closes_window_after_an_open_already_in_flight(hub, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    order = []
    def opening(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        order.append("open window")
        return {"ok": True}
    monkeypatch.setattr(hub, "_open_app", opening)
    monkeypatch.setattr(hub, "_stop_app", lambda app: order.append("stop and close") or {"ok": True})
    opener = threading.Thread(target=lambda: hub.open("launch", automatic=True))
    stopper = threading.Thread(target=lambda: hub.stop("launch"))
    opener.start()
    assert entered.wait(5)
    stopper.start()
    proceed.set()
    opener.join(5); stopper.join(5)
    assert not opener.is_alive() and not stopper.is_alive()
    assert order == ["open window", "stop and close"]


def test_command_only_profile_cannot_resume_after_manual_stop(hub, monkeypatch):
    hub.config.profiles = {"models": {"commands": [{"id": "model", "argv": ["python", "-V"]}]}}
    hub._manual_stop(["profile:models"])
    called = []
    monkeypatch.setattr(hub.commands, "start", lambda c: called.append(c.id) or {"ok": True})
    assert not hub.profile_start("models", automatic=True)["ok"]
    assert not called


def test_faustus_start_and_stop_through_http_and_tools(served, monkeypatch):
    hub, url = served
    calls = []
    monkeypatch.setattr(hub.faustus_runtime, "run", lambda action: calls.append(action) or {"ok": True})
    assert _http(url + "/api/faustus/start", {}) == (200, {"ok": True})
    assert tools.call(hub, "hub_faustus_stop", {})["ok"]
    assert calls == ["start", "stop-all"] and hub.automation_blocked("faustus")
    monkeypatch.setattr(hub.faustus_runtime, "run", lambda action: {"ok": False, "error": "access denied"})
    assert _http(url + "/api/faustus/stop", {})[0] == 409
    names = {t["name"] for t in tools.catalogue()}
    assert {"hub_faustus_start", "hub_faustus_stop", "hub_faustus_status"} <= names


def test_faustus_runtime_uses_checkout_port_and_never_returns_ownership_token(tmp_path, monkeypatch):
    import hoard_link.hub.faustus as runtime_module
    runtime = FaustusRuntime(HubConfig(faustus_urls=["http://127.0.0.1:7001"]))
    monkeypatch.setattr(runtime, "installation", lambda: (tmp_path, "python"))
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(stdout=json.dumps({"started": True, "port": 7001, "token": "private-ownership"}), returncode=0)
    monkeypatch.setattr(runtime, "_execute", run)
    out = runtime.run("start")
    assert out["ok"] and out["port"] == 7001
    assert "private-ownership" not in json.dumps(out)
    assert commands[0] == ["python", str(tmp_path / "server_runtime.py"), "start", "--port", "7001", "--owner", "web"]


def test_faustus_remaining_processes_are_a_failure(tmp_path, monkeypatch):
    import hoard_link.hub.faustus as runtime_module
    runtime = FaustusRuntime(HubConfig())
    monkeypatch.setattr(runtime, "installation", lambda: (tmp_path, "python"))
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout='{"remaining": [123]}', returncode=0))
    out = runtime.run("stop-all")
    assert not out["ok"] and out["remaining"] == [123] and out["error"]


def test_a_slow_faustus_start_can_be_cancelled_without_waiting_for_readiness(tmp_path, monkeypatch):
    import hoard_link.hub.faustus as runtime_module
    runtime = FaustusRuntime(HubConfig())
    monkeypatch.setattr(runtime, "installation", lambda: (tmp_path, "python"))
    entered, killed = threading.Event(), threading.Event()
    class Starter:
        pid = 123
        returncode = 1
        def poll(self):
            return 1 if killed.is_set() else None
        def kill(self):
            killed.set()
        def communicate(self, **kw):
            entered.set()
            assert killed.wait(5)
            return "", "cancelled"
    monkeypatch.setattr(runtime_module.subprocess, "Popen", lambda *args, **kwargs: Starter())
    monkeypatch.setattr(procs, "_psutil", lambda: None)
    results = []
    worker = threading.Thread(target=lambda: results.append(runtime.run("start")))
    worker.start()
    assert entered.wait(5)
    runtime.cancel_start()
    worker.join(5)
    assert not worker.is_alive() and killed.is_set() and results[0]["cancelled"] and not results[0]["ok"]
