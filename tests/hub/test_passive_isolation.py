"""The isolated startup contract and kernel-owned fixture lifetimes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hoard_link.hub import __main__ as cli, actions, isolation, profile_editor, tools
from hoard_link.hub.config import HubConfig, REPO_DIR
from hoard_link.hub.core import Hub
from hoard_link.hub.server import serve_in_thread

from .conftest import free_port, write_manifest
from .test_instances import _http


@pytest.fixture
def isolated_config(tmp_path):
    apps = tmp_path / "apps"
    write_manifest(apps / "Fixture", "fixture", free_port(), launch={"executable": sys.executable, "argv": ["-c", "pass"]},
                   extra={"x-family": {"isolated_instance": {"data_env": "TEST_DATA_DIR", "port_env": "TEST_PORT"}}})
    data = tmp_path / "hub"
    data.mkdir()
    (data / "hub.json").write_text(json.dumps({"roots": [str(apps)], "profiles": {
        "qa": {"apps": ["fixture"], "commands": [{"id": "example", "argv": ["ignored"]}]},
    }}), encoding="utf-8")
    (data / "keep-alive.json").write_text('{"apps": ["fixture"]}', encoding="utf-8")
    return HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(data), "HOARD_HUB_PORT": str(free_port()),
                               "FAUSTUS_DIR": str(tmp_path / "faustus")})


def test_p1_automatic_entry_points_are_passive(isolated_config, monkeypatch):
    from hoard_link.hub import desktop, facets, procs
    from hoard_link.hub.placement_link import HubLink

    launched = Mock(side_effect=AssertionError("unexpected launch or probe"))
    for module, names in ((procs, ("start_app", "health", "find_app_process", "terminate_tree")),
                          (desktop, ("open_window", "open_in_browser", "close_windows")),
                          (facets, ("start_all",)), (HubLink, ("start",)),
                          (profile_editor.autostart, ("install", "uninstall"))):
        for name in names:
            monkeypatch.setattr(module, name, launched)
    hub = Hub(isolated_config)
    try:
        for obj, attr in ((hub.commands, "start"), (hub.faustus_runtime, "run"),
                          (hub.launcher, "start"), (hub.placement, "local_block"),
                          (hub.facet("purchases"), "_fetch")):
            monkeypatch.setattr(obj, attr, launched)
        orders = [
            lambda: hub.start("fixture", automatic=True), lambda: hub.start("fixture"),
            lambda: hub.restart("fixture", automatic=True), lambda: hub.restart("fixture"),
            lambda: hub.stop("fixture"), lambda: hub.start_all(automatic=True), lambda: hub.stop_all(),
            lambda: hub.open("fixture", autostart=True, automatic=True),
            lambda: hub.profile_start("qa", startup=True), lambda: hub.profile_stop("qa"),
            lambda: hub.faustus_start(automatic=True), lambda: hub.faustus_stop(),
            lambda: hub.service_start("dummy", automatic=True), lambda: hub.service_stop("dummy"),
            lambda: hub.facet("purchases").prepare_home(123),
            lambda: profile_editor.startup(hub, {"enabled": True, "profile": "qa"}),
            lambda: tools.call(hub, "hub_placement_switch", {"to": "sparks"}),
            lambda: tools.call(hub, "hub_isolated_app", {"action": "start", "app": "fixture", "_automatic": True}),
            lambda: hub.rules.run({"id": "watchdog", "then": []}, {}, manual=False),
            lambda: hub.jobs._run({"id": "scheduled", "then": []}, manual=False),
            lambda: actions.run_one(hub, {"kind": "hub", "tool": "hub_isolated_app"}, {}, caller="rule:watchdog"),
        ]
        for order in orders:
            assert order()["code"] == "isolated_hub"
        keepalive = hub.facet("keepalive")
        assert keepalive.set("fixture", True)["code"] == "isolated_hub"
        assert not keepalive.serve()
        assert keepalive.check(boot=True) == []
        assert hub.facet("agora_console") is None
        assert hub.jobs._thread is None and hub.rules._off is None
        assert hub.snapshot()["counts"]["running"] == 0
        launched.assert_not_called()
    finally:
        hub.close()


@pytest.mark.parametrize("override", ["roots", "faustus", "masked-faustus", "json", "session", "profile", "data"])
def test_p1_rejection_precedes_probe_launch_and_construction(override, isolated_config, tmp_path, monkeypatch, capsys):
    args = ["--port", str(isolated_config.port), "--data-dir", isolated_config.data_dir]
    unapproved = str(tmp_path.parent / "outside-this-test")
    monkeypatch.setenv("FAUSTUS_DIR", isolated_config.faustus_dir)
    for key in ("HOARD_HUB_ROOTS", "HOARD_HUB_FAUSTUS_DIR", "HOARD_HUB_TOKEN_FILE", "HOARD_HUB_BACKUP_DIR"):
        monkeypatch.delenv(key, raising=False)
    if override == "roots":
        args += ["--roots", unapproved]
    elif override in ("faustus", "masked-faustus"):
        monkeypatch.setenv("FAUSTUS_DIR", unapproved)
        if override == "masked-faustus":
            monkeypatch.setenv("HOARD_HUB_FAUSTUS_DIR", isolated_config.faustus_dir)
    elif override == "json":
        (Path(isolated_config.data_dir) / "hub.json").write_text(
            json.dumps({"backup": {"dir": unapproved}}), encoding="utf-8")
    elif override == "session":
        args += ["--install-autostart"]
    elif override == "profile":
        args += ["--profile", "qa"]
    else:
        args[-1] = str(REPO_DIR / "data")
    side_effect = Mock(side_effect=AssertionError("a rejected Hub touched the runtime"))
    for name in ("Hub", "_already_running", "_start_profile_remote", "_autostart_command", "enter_linux_service"):
        monkeypatch.setattr(cli, name, side_effect)
    assert cli.main(args) == 2
    assert capsys.readouterr().err
    side_effect.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Linux startup is exercised with a transient unit in P4")
def test_p1_bootstrap_starts_only_http(isolated_config, monkeypatch):
    monkeypatch.setenv("FAUSTUS_DIR", isolated_config.faustus_dir)
    hub = Mock(apps=[])
    server = Mock()
    constructor = Mock(return_value=hub)
    forbidden = Mock(side_effect=AssertionError("isolated bootstrap touched the installed Hub or desktop"))
    monkeypatch.setattr(cli, "Hub", constructor)
    monkeypatch.setattr(cli, "make_server", Mock(return_value=server))
    monkeypatch.setattr(cli, "_already_running", forbidden)
    monkeypatch.setattr(cli, "_open_own_window", forbidden)
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=Mock(side_effect=KeyboardInterrupt)))
    assert cli.main(["--port", str(isolated_config.port), "--data-dir", isolated_config.data_dir, "--browser"]) == 0
    constructor.assert_called_once()
    assert constructor.call_args.kwargs == {"fixture_jobs": None}
    forbidden.assert_not_called()
    assert [name for name, *_ in hub.method_calls] == ["write_url_file", "close"]
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()


def test_p1_agent_route_requires_an_explicit_fixture(isolated_config, tmp_path):
    fixture = Path(isolated_config.roots[0]) / "Fixture"
    process = SimpleNamespace(running=lambda: True)
    jobs = Mock()
    jobs.launch.return_value = process
    hub = Hub(isolated_config, fixture_jobs=jobs)
    server, _ = serve_in_thread(hub)
    headers = {"Authorization": f"Bearer {hub.token}"}

    def call(name, **arguments):
        status, envelope = _http(hub.config.url + "/api/agent/tools/" + name, arguments, headers)
        return status, envelope["result"]

    try:
        assert call("hub_start_app", app="fixture")[1]["code"] == "isolated_hub"
        assert call("hub_placement_switch", to="sparks")[1]["code"] == "isolated_hub"
        assert call("hub_isolated_app", action="start", app="fixture", worktree=str(tmp_path.parent))[0] == 400
        jobs.launch.assert_not_called()

        status, result = call("hub_isolated_app", action="start", app="fixture", worktree=str(fixture))
        assert status == 200 and result["ok"], result
        jobs.launch.assert_called_once()
        argv, options = jobs.launch.call_args
        assert argv == ([sys.executable, "-c", "pass"],)
        assert options["cwd"] == str(fixture)
        assert options["env"]["FAUSTUS_DIR"] == isolated_config.faustus_dir
        assert Path(options["env"]["TEST_DATA_DIR"]).is_relative_to(tmp_path)
        assert options["env"]["HOARD_HUB_URL"] == isolated_config.url
        assert not {"HOARD_HUB_TOKEN_FILE", "HOARD_HUB_TOKEN"} & options["env"].keys()
        assert call("hub_isolated_app", **result["undo"])[1]["instance"]["state"] == "stopped"
        jobs.stop.assert_called_once_with(process)
    finally:
        server.shutdown()
        server.server_close()
        hub.close()


def _await(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("fixture did not reach the expected state")
        time.sleep(0.02)


_LAUNCHER = """
import ctypes, os, pathlib, subprocess, sys, time
assert not ctypes.windll.kernel32.GetConsoleWindow()
base = pathlib.Path(sys.argv[1])
(base / 'parent').write_text(str(os.getpid()))
subprocess.Popen([sys.executable, '-I', '-S', '-c', sys.argv[2], str(base)],
                 creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
while not (base / 'exit-parent').exists():
    time.sleep(0.02)
"""
_SLEEPER = """
import ctypes, os, pathlib, sys, time
assert not ctypes.windll.kernel32.GetConsoleWindow()
(pathlib.Path(sys.argv[1]) / 'grandchild').write_text(str(os.getpid()))
time.sleep(60)
"""


def _orphan(jobs, base):
    import psutil

    base.mkdir()
    with (base / "log").open("wb") as log:
        launch = jobs.launch([sys.executable, "-I", "-S", "-c", _LAUNCHER, str(base), _SLEEPER],
                             cwd=str(base), env=dict(os.environ), log=log)
    parent_file, grandchild_file = base / "parent", base / "grandchild"
    _await(lambda: parent_file.exists() and grandchild_file.exists() and grandchild_file.stat().st_size > 0)
    parent = psutil.Process(int(parent_file.read_text()))
    grandchild = psutil.Process(int(grandchild_file.read_text()))
    (base / "exit-parent").touch()
    assert parent.wait(timeout=10) == 0
    assert launch.running() and grandchild.is_running()
    return launch, grandchild


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real Windows Job Object")
def test_p2_closing_hub_job_reaps_orphan(tmp_path):
    from hoard_link.hub.windows_job import WindowsJobs

    jobs = WindowsJobs()
    try:
        fixture, grandchild = _orphan(jobs, tmp_path / "fixture")
        jobs._hub.close()
        assert fixture.handle is not None  # only the parent Job has closed
        grandchild.wait(timeout=10)
        assert not grandchild.is_running()
    finally:
        jobs.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real nested Windows Job Objects")
def test_p3_stopping_a_leaves_b_and_later_foreign_sibling_alive(tmp_path):
    import psutil

    from hoard_link._process import popen_hidden
    from hoard_link.hub.windows_job import WindowsJobs

    jobs = WindowsJobs()
    foreign = None
    try:
        a, grandchild_a = _orphan(jobs, tmp_path / "fixture-a")
        b, grandchild_b = _orphan(jobs, tmp_path / "fixture-b")
        foreign = popen_hidden([sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"],
                               cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        assert psutil.Process(foreign.pid).ppid() == os.getpid()
        jobs.stop(a)
        grandchild_a.wait(timeout=10)
        assert not grandchild_a.is_running()
        assert b.running() and grandchild_b.is_running() and foreign.poll() is None
        jobs.close()
        grandchild_b.wait(timeout=10)
        assert not grandchild_b.is_running() and foreign.poll() is None
    finally:
        jobs.close()
        if foreign is not None:
            foreign.kill()
            foreign.wait(timeout=10)


@pytest.mark.parametrize("manager", [True, False])
def test_p4_linux_bootstrap_uses_transient_service_or_fails(manager, isolated_config, monkeypatch, capsys):
    from hoard_link import _process

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "systemctl":
            return SimpleNamespace(returncode=0 if manager else 1, stdout="255\n" if manager else "")
        assert argv[0] == "systemd-run"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    monkeypatch.setenv("FAUSTUS_DIR", isolated_config.faustus_dir)
    monkeypatch.setattr(_process, "run_hidden", run)
    monkeypatch.setattr(cli, "Hub", Mock(side_effect=AssertionError("Hub constructed outside service")))
    args = ["--port", str(isolated_config.port), "--data-dir", isolated_config.data_dir, "--no-window"]
    assert cli.main(args) == (0 if manager else 2)
    assert calls[0] == ["systemctl", "--user", "show", "--property=Version", "--value"]
    if manager:
        argv = calls[1]
        assert argv[:5] == ["systemd-run", "--user", "--wait", "--pipe", "--collect"]
        assert {"--property=Type=exec", "--property=KillMode=control-group", "--property=Restart=no"} <= set(argv)
        unit = argv[5].split("=", 1)[1]
        assert argv[argv.index("--") + 1:] == [sys.executable, "-m", "hoard_link.hub", "--isolated-unit", unit, *args]
    else:
        assert len(calls) == 1
        assert "systemd user manager" in capsys.readouterr().err


def test_p4_service_flag_requires_actual_membership(monkeypatch, tmp_path):
    unit = "hoard-hub-test-a.service"
    group = f"/user.slice/app.slice/{unit}"
    record = {"MainPID": str(os.getpid()), "ControlGroup": group, **isolation._SERVICE_PROPERTIES}
    result = lambda: SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={v}" for k, v in record.items()))
    monkeypatch.setattr(isolation, "_systemctl", lambda *args: result())
    monkeypatch.setattr(isolation, "_CGROUP_MOUNT", tmp_path)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: f"0::{group}\n")
    assert isolation.service_cgroup(unit) == tmp_path / group.lstrip("/")
    record["MainPID"] = "0"
    with pytest.raises(OSError, match="main process"):
        isolation.service_cgroup(unit)
