"""The app directory exercises its normal routes without opening sockets."""

from __future__ import annotations

import json
import socket
import time
from types import SimpleNamespace

import pytest

from hoard_link.hub import core, procs
from hoard_link.hub.config import HubConfig
from hoard_link.hub.directory import AppDirectory
from hoard_link.hub.registry import App
from hoard_link.hub.server import _HubHandler


def make_hub(tmp_path, monkeypatch, settings):
    path = tmp_path / "hub.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    hub = core.Hub.__new__(core.Hub)
    hub.config = HubConfig.load(path, env={"HOARD_HUB_DATA_DIR": str(tmp_path)})
    # Exercise the normal snapshot path with injected OS, network and service observations.
    hub.config.isolated = False
    hub._snapshot_epoch = object()
    hub._apps = {
        app_id: App(app_id, app_id.title(), "Fixture", str(tmp_path),
                    f"http://127.0.0.1:{port}", "/api/health", app_id)
        for app_id, port in (("hidden-probe", 18731), ("observed", 18732))
    }
    hub.started_at = time.time()
    hub.events = SimpleNamespace(last_id=0)
    hub.rules = SimpleNamespace(rules=[])
    hub.jobs = SimpleNamespace(jobs=[])
    hub.backups = SimpleNamespace(root=str(tmp_path))
    hub.placement = SimpleNamespace(remote_app=lambda _id: None, status=lambda: {"location": "pc"})
    hub.faustus_status = lambda: {"reachable": False}
    hub.profiles_status = lambda states: {"profiles": []}
    hub.facet = lambda _name: None
    hub.automation_blocked = lambda _id: False
    monkeypatch.setattr(core.desktop, "list_windows", lambda _dir: {})
    monkeypatch.setattr(core.desktop, "find_browser", lambda _browser: None)
    monkeypatch.setattr(core.desktop, "window_engine", lambda _window_engine: "headless")
    monkeypatch.setattr(procs, "_psutil", lambda: None)
    monkeypatch.setattr(procs, "listening_pids", lambda: {18731: 41, 18732: 42})
    monkeypatch.setattr(procs, "find_app_process", lambda _app, _listeners: None)

    def no_socket(*args, **kwargs):
        pytest.fail("The app directory test opened a socket")

    probes = []

    def fetch(url):
        probes.append(url)
        service = next(app.id for app in hub.apps if app.health_url() == url)
        return 200, {"service": service}

    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(procs, "fetch_json", fetch)
    return hub, probes


def request(directory, path):
    handler = _HubHandler.__new__(_HubHandler)
    handler.hub = directory.hub
    handler.server = SimpleNamespace(directory=directory)
    handler.path = path
    handler._guard = lambda: True
    replies = []
    handler._json = lambda body, status=200: replies.append((status, body))
    handler.do_GET()
    [(status, body)] = replies
    assert status == 200, body
    return body


def wait_for_snapshot(directory):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        latest = directory._latest
        if latest is not None and latest.epoch is directory.hub._snapshot_epoch:
            return
        time.sleep(0.01)
    pytest.fail("The app directory did not publish the expected snapshot")


@pytest.mark.parametrize("excluded", [False, True])
def test_startup_and_listing_refresh_share_the_poll_exclusion(tmp_path, monkeypatch, excluded):
    options = {"directory_health_excluded_apps": [" HIDDEN-PROBE "]} if excluded else {}
    hub, probes = make_hub(tmp_path, monkeypatch, options)
    target = hub.get("hidden-probe")
    expected_urls = [hub.get("observed").health_url()]
    if not excluded:
        expected_urls.append(target.health_url())
    directory = AppDirectory(hub)
    directory.start()
    try:
        wait_for_snapshot(directory)
        assert sorted(probes) == sorted(expected_urls)
        listing = request(directory, "/api/apps")
        assert listing["counts"] == {"total": 2, "running": 1 if excluded else 2, "windows": 0}
        row, observed = listing["apps"]
        assert row["id"] == target.id
        assert request(directory, f"/api/apps/{target.id}/status") == row
        assert observed["state"] == "running" and observed["status_checked_at"] is not None
        if excluded:
            assert row["health_check_excluded"] is True
            assert row["health"]["state"] == row["state"] == "unknown"
            assert row["status_checked_at"] is None
        else:
            assert not row.get("health_check_excluded")
            assert row["state"] == "running" and row["status_checked_at"] is not None

        hub.invalidate_snapshot()
        request(directory, "/api/apps")
        wait_for_snapshot(directory)
        assert sorted(probes) == sorted(expected_urls * 2)
        assert request(directory, f"/api/apps/{target.id}/status").get("health_check_excluded", False) == excluded

        # A caller that explicitly checks health still reaches the normal probe.
        assert hub.app_status(target)["state"] == "running"
        assert probes[-1] == target.health_url()
        assert len(probes) == len(expected_urls) * 2 + 1
    finally:
        directory.close()
        directory._thread.join(timeout=3)


@pytest.mark.parametrize("invalid", ["hidden-probe", ["hidden-probe", 12], [" "]])
def test_invalid_exclusion_setting_cannot_fall_back_to_probing(tmp_path, monkeypatch, invalid):
    hub, probes = make_hub(tmp_path, monkeypatch, {"directory_health_excluded_apps": invalid})
    with pytest.raises(ValueError, match="directory_health_excluded_apps"):
        hub.snapshot()
    assert probes == []
