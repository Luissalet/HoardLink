"""Windows launch flags and readoption use process doubles."""

from threading import RLock
from types import SimpleNamespace

import pytest

from hoard_link.hub import agora_worker_processes, procs
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.core import Hub
from hoard_link.hub.registry import App


@pytest.mark.parametrize("kind", ["app", "window-app"])
def test_service_priority_at_launch_keeps_the_hidden_flags(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(procs.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(procs.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(procs, "health", lambda _app: procs.Health("down"))
    monkeypatch.setattr(procs, "proc_info", lambda _pid: SimpleNamespace(created_at=123.0))

    launches = []

    def spawn(argv, **kwargs):
        launches.append(kwargs["creationflags"])
        return SimpleNamespace(pid=45, poll=lambda: None)

    monkeypatch.setattr(procs.subprocess, "Popen", spawn)
    launch = SimpleNamespace(executable="fixture", argv=[], cwd=str(tmp_path), env={}, readiness_url=None)
    app = SimpleNamespace(id="fixture", launchable=True, launch=launch, kind=kind)
    assert procs.start_app(app, str(tmp_path), wait=False)["ok"]
    expected = 0x08000200 | (procs.NORMAL_PRIORITY_CLASS if kind == "app" else 0)
    assert launches == [expected]


class FakeProcess:
    def __init__(self, pid, priority, *, children=()):
        self.pid = pid
        self.priority = priority
        self.descendants = children
        self.writes = []

    def create_time(self):
        return 123.0

    def is_running(self):
        return True

    def children(self, *, recursive):
        assert recursive
        return self.descendants

    def nice(self, priority=None):
        if priority is not None:
            self.priority = priority
            self.writes.append(priority)
        return self.priority


def test_readopted_worker_corrects_live_tree_with_verified_identity(monkeypatch):
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    normal, below = procs.NORMAL_PRIORITY_CLASS, procs.BELOW_NORMAL_PRIORITY_CLASS
    child, already, protected = (FakeProcess(42, normal), FakeProcess(43, below), FakeProcess(44, normal))
    root = FakeProcess(41, normal, children=[child, already, protected])
    monkeypatch.setattr(procs, "_protected_pids", lambda: {44})
    monkeypatch.setattr(procs, "_psutil", lambda: SimpleNamespace(Process=lambda _pid: root))
    agora_worker_processes.restore_priority(41, 122.0)
    assert root.writes == child.writes == []
    agora_worker_processes.restore_priority(41, 123.0)
    assert root.writes == child.writes == [below]
    assert already.writes == protected.writes == []


@pytest.mark.parametrize("failure", [None, "AccessDenied", "NoSuchProcess"])
def test_worker_tree_priority_failure_is_logged_and_other_processes_are_corrected(monkeypatch, caplog, failure):
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(procs, "_protected_pids", lambda: set())
    normal, below = procs.NORMAL_PRIORITY_CLASS, procs.BELOW_NORMAL_PRIORITY_CLASS
    child = FakeProcess(42, normal)
    root = SimpleNamespace(pid=41, create_time=lambda: 123.0, is_running=lambda: True,
                           children=lambda *, recursive: [child])
    if failure is not None:
        def nice(priority=None):
            if priority is not None:
                raise getattr(psutil, failure)(root.pid)
            return normal
        root.nice = nice
    monkeypatch.setattr(procs, "_psutil", lambda: SimpleNamespace(Process=lambda _pid: root))

    agora_worker_processes.restore_priority(41, 123.0)

    assert child.writes == [below]
    assert "Could not correct priority for process 41" in caplog.text


def test_worker_runtime_is_readopted_at_below_normal_once(monkeypatch):
    console = AgoraConsoleFacet.__new__(AgoraConsoleFacet)
    console._faustus_runtimes = {}
    restored = []
    monkeypatch.setattr(procs, "proc_info", lambda pid: procs.ProcInfo(pid, created_at=123.0))
    monkeypatch.setattr(procs, "set_tree_priority", lambda *args: restored.append(args))
    row = {"id": "fixture", "runtime_pid": 42, "runtime_started_at": 122.0, "runtime_job": "fixture-job"}
    console._adopt_worker_runtime(row)
    assert not restored
    row["runtime_started_at"] = 123.0
    console._adopt_worker_runtime(row)
    console._adopt_worker_runtime(row)
    assert restored == [(42, 123.0, procs.BELOW_NORMAL_PRIORITY_CLASS)]
    assert console._faustus_runtimes["fixture"]["job_name"] == "fixture-job"


def test_status_readopts_only_verified_local_services_once_per_identity(tmp_path, monkeypatch):
    hub = Hub.__new__(Hub)
    hub._lock = RLock()
    hub._adopted_services = {}
    hub.config = SimpleNamespace(isolated=False, logs_dir=str(tmp_path))
    hub.automation_blocked = lambda _id: False
    hub.placement = SimpleNamespace(remote_app=lambda _id: None)
    app = App("fixture", "Fixture", "", str(tmp_path), "http://127.0.0.1:18897", "/api/health",
              "fixture", launchable=True)
    process = procs.ProcInfo(42, created_at=123.0)
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(procs, "find_app_process", lambda _app, _listeners: process)
    prioritized = []
    monkeypatch.setattr(procs, "set_tree_priority", lambda *args: prioritized.append(args))
    healthy = procs.Health("healthy", service="fixture")
    hub.app_status(app, windows={}, health=procs.Health("foreign"))
    app.url = "http://192.0.2.1:18897"
    hub.app_status(app, windows={}, health=healthy)
    app.url = "http://127.0.0.1:18897"
    process.protected = True
    hub.app_status(app, windows={}, health=healthy)
    assert not prioritized
    process.protected = False
    hub.app_status(app, windows={}, health=healthy)
    hub.app_status(app, windows={}, health=healthy)
    process.created_at = 124.0
    hub.app_status(app, windows={}, health=healthy)
    assert prioritized == [(42, 123.0, procs.NORMAL_PRIORITY_CLASS), (42, 124.0, procs.NORMAL_PRIORITY_CLASS)]
