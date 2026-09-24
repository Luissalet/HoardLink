"""Profiles: named sets of apps, external commands and desktop windows."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from hoard_link.hub import core, tools
from hoard_link.hub.profiles import CommandRunner, parse
from hoard_link.hub.server import make_server

from .conftest import free_port
from .test_hub_and_server import _http


def test_parse_keeps_good_entries_and_reports_bad_ones():
    profiles, problems = parse({
        "ok": {"apps": ["a", "b"], "desktop": "b",
               "commands": [{"name": "comfy", "cmd": "python main.py", "cwd": "~/x", "health": "http://h/s",
                             "env": {"CUDA_VISIBLE_DEVICES": 1}},
                            {"name": "no cmd"}]},
        "bad": "nope",
        "weird": {"apps": 5},
    })
    assert set(profiles) == {"ok", "weird"}
    p = profiles["ok"]
    assert p.apps == ["a", "b"] and p.desktop == ["b"] and p.members == ["a", "b"]
    assert len(p.commands) == 1 and p.commands[0].env == {"CUDA_VISIBLE_DEVICES": "1"}
    assert p.commands[0].id == "ok--comfy" and not p.commands[0].cwd.startswith("~")
    assert len(problems) == 3
    assert parse(None) == ({}, []) and parse([])[1]


def _wait(pred, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def prof_hub(hub, family, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(core.desktop, "open_window",
                        lambda url, app_id, profiles_root, **kw: opened.append(app_id) or {"ok": True, "mode": "app-window"})
    cmd_port = free_port()
    hub.config.profiles = {
        "work": {
            "apps": ["launch"],
            "desktop": ["launch"],
            "commands": [
                {"name": "web", "cmd": [sys.executable, str(tmp_path / "serve.py"), str(cmd_port)],
                 "cwd": str(tmp_path), "health": f"http://127.0.0.1:{cmd_port}/"},
                {"name": "sleeper", "cmd": f'"{sys.executable}" -c "import time; time.sleep(120)"'},
            ],
        },
        "broken": {"apps": ["launch", "ghost"]},
        "foreign": {"commands": [{"name": "elsewhere", "cmd": "true",
                                  "health": f"http://127.0.0.1:{family['fake_port']}/api/health"}]},
    }
    yield hub, opened
    for name in ("work",):
        hub.profile_stop(name)


def test_profile_start_status_stop(prof_hub):
    hub, opened = prof_hub
    st = {p["name"]: p for p in hub.profiles_status()["profiles"]}
    assert st["work"]["state"] == "stopped" and st["work"]["total"] == 3
    assert st["foreign"]["state"] == "running"          # its health URL answers
    res = hub.profile_start("work")
    assert res["ok"], res
    assert opened == ["launch"]
    assert _wait(lambda: {p["name"]: p for p in hub.profiles_status()["profiles"]}["work"]["state"] == "running")
    work = hub.profile_status("work")
    kinds = {m["name"]: (m["kind"], m["state"]) for m in work["members"]}
    assert kinds == {"Launch's Hoard": ("app", "running"), "web": ("command", "running"),
                     "sleeper": ("command", "running")}
    again = hub.profile_start("work")
    assert all(c.get("already") for c in again["commands"])
    # the snapshot carries the profiles too
    assert {p["name"] for p in hub.snapshot()["profiles"]} == {"work", "broken", "foreign"}
    # a fresh runner (a restarted hub) still knows the pids it started
    runner = CommandRunner(hub.commands.state_file, hub.config.logs_dir)
    assert all(runner.status(c)["pid"] for c in hub.profiles()["work"].commands)
    stopped = hub.profile_stop("work")
    assert stopped["ok"], stopped
    assert _wait(lambda: hub.profile_status("work")["state"] == "stopped", 10)


def test_profile_errors(prof_hub):
    hub, _ = prof_hub
    assert hub.profile_start("nope")["ok"] is False
    res = hub.profile_start("broken")
    assert res["ok"] is False and res["unknown"] == ["ghost"]
    hub.stop("launch")
    st = hub.profile_status("broken")
    assert {m["id"]: m["state"] for m in st["members"]}["ghost"] == "unknown"
    # a command that answers but was not started by the hub is never stopped
    res = hub.profile_stop("foreign")
    assert res["ok"] is False and "not started by the hub" in res["error"]
    assert hub.profile_status("nope")["ok"] is False


def test_profiles_over_http_and_tools(prof_hub):
    hub, _ = prof_hub
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = hub.config.url
        status, body = _http(url + "/api/profiles")
        assert status == 200 and {p["name"] for p in body["profiles"]} == {"work", "broken", "foreign"}
        status, one = _http(url + "/api/profiles/work")
        assert status == 200 and one["name"] == "work"
        assert _http(url + "/api/profiles/nope")[0] == 404
        assert _http(url + "/api/profiles/nope/start", {})[0] == 404
        status, res = _http(url + "/api/profiles/broken/start", {})
        assert status == 409 and res["unknown"] == ["ghost"]
        status, res = _http(url + "/api/profiles/work/start", {})
        assert status == 200 and res["ok"]
        status, res = _http(url + "/api/profiles/work/stop", {})
        assert status == 200 and res["ok"]
        names = {t["name"] for t in tools.catalogue()}
        assert {"hub_profile_list", "hub_profile_start", "hub_profile_stop"} <= names
        lst = tools.call(hub, "hub_profile_list", {})
        assert {p["name"] for p in lst["profiles"]} == {"work", "broken", "foreign"}
        assert tools.call(hub, "hub_profile_start", {"name": "nope"})["ok"] is False
        assert tools.call(hub, "hub_profile_stop", {"name": "work"})["ok"]
        html = _http_text(url + "/")
        assert 'id="profiles"' in html and 'id="gpu-panel"' in html
    finally:
        server.shutdown()


def _http_text(url: str) -> str:
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5).read().decode()


def test_profiles_load_from_hub_json(tmp_path):
    import json
    from hoard_link.hub.config import HubConfig
    (tmp_path / "hub.json").write_text(json.dumps({"profiles": {"p": {"apps": ["x"]}}}), encoding="utf-8")
    cfg = HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path)})
    assert cfg.profiles == {"p": {"apps": ["x"]}}
    assert HubConfig().profiles == {}                    # none by default


def test_start_while_starting_does_not_spawn_twice(hub):
    first = hub.start("launch", wait=False)
    assert first["ok"] and first["pid"]
    again = hub.start("launch", wait=False)
    assert again.get("already") and again["pid"] == first["pid"]
    ready = hub.start("launch", wait=True)
    assert ready["ok"] and ready["pid"] == first["pid"] and ready.get("ready") is not False
    assert hub.app_status(hub.get("launch"))["process"]["pid"] == first["pid"]
