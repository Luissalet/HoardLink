import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hoard_link.hub import agora_worker_launch as launch


@pytest.fixture
def launchers(monkeypatch, tmp_path):
    native = Mock()
    previous = Mock()
    monkeypatch.setitem(sys.modules, "hoard_link.hub.windows_job",
                        SimpleNamespace(launch_on_private_desktop=native))
    monkeypatch.setattr(launch, "popen_hidden", previous)
    monkeypatch.setattr(launch, "IS_WINDOWS", True)
    return native, previous, {"cwd": str(tmp_path), "env": {}, "log": object()}


def test_windows_isolation_enabled_by_default(launchers):
    native, previous, options = launchers
    command = [options["cwd"] + "/worker.exe", "resume"]
    assert launch.launch_worker(SimpleNamespace(), command, **options) is native.return_value
    native.assert_called_once_with(command, **options)
    previous.assert_not_called()


@pytest.mark.parametrize("windows, settings", [(True, {"worker_private_desktop": False}), (False, {})])
def test_legacy_launch_only_when_disabled_or_not_windows(launchers, monkeypatch, windows, settings):
    native, previous, options = launchers
    monkeypatch.setattr(launch, "IS_WINDOWS", windows)
    command = ["worker", "resume"]
    result = launch.launch_worker(SimpleNamespace(agora=settings), command, **options)
    assert result is previous.return_value
    previous.assert_called_once_with(command, stdin=subprocess.DEVNULL, stdout=options["log"],
                                     stderr=subprocess.STDOUT, text=True, cwd=options["cwd"], env={})
    native.assert_not_called()


def test_native_failure_does_not_retry_outside_desktop(launchers):
    native, previous, options = launchers
    native.side_effect = OSError("desktop unavailable")
    with pytest.raises(OSError, match="desktop unavailable"):
        launch.launch_worker(SimpleNamespace(), [options["cwd"] + "/worker.exe"], **options)
    previous.assert_not_called()


@pytest.mark.parametrize("name,package,local", [
    ("claude", ("@anthropic-ai", "claude-code", "cli.js"), False),
    ("codex", ("@openai", "codex", "bin", "codex.js"), False),
    ("codex", ("@openai", "codex", "bin", "codex.js"), True),
])
def test_npm_workers_launch_without_batch_shell(monkeypatch, tmp_path, name, package, local):
    modules = tmp_path / "node_modules"
    prefix = modules / ".bin" if local else tmp_path
    prefix.mkdir(parents=True, exist_ok=True)
    wrapper, node = prefix / f"{name}.cmd", prefix / "node.exe"
    script = modules.joinpath(*package)
    script.parent.mkdir(parents=True)
    script.touch()
    wrapper.touch()
    if not local:
        node.touch()
    monkeypatch.setattr(launch.shutil, "which", lambda name, *, path: str(node) if path == "test-path" else None)
    assert launch._native_command([wrapper, "resume"], {"PATH": "test-path"}) == [
        str(node), str(script), "resume",
    ]


def test_unknown_wrapper_fails_without_launching_shell(launchers):
    native, previous, options = launchers
    with pytest.raises(OSError, match="native executable"):
        launch.launch_worker(SimpleNamespace(), ["arbitrary.cmd"], **options)
    native.assert_not_called()
    previous.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API declarations")
def test_private_desktop_and_job_share_process_lifetime(monkeypatch, tmp_path):
    from ctypes import cast, POINTER
    from ctypes.wintypes import DWORD
    from hoard_link.hub import windows_job

    order = []
    job = SimpleNamespace(handle=21, close=lambda: order.append(("job", 21)),
                          running=lambda: False, terminate=Mock())

    def new_job(name):
        job.name = name
        return job

    monkeypatch.setattr(windows_job, "_Job", new_job)
    monkeypatch.setattr(windows_job, "_get_station", lambda: 1)

    def station(handle, info, output, *_):
        assert (handle, info) == (1, 2)
        output.value = "TestStation"
        return True

    def create(name, device, mode, flags, access, security):
        assert cast(security, POINTER(windows_job._SecurityAttributes))[0].inherit
        order.append(("create", name))
        return 11

    monkeypatch.setattr(windows_job, "_object_info", station)
    monkeypatch.setattr(windows_job, "_create_desktop", create)
    monkeypatch.setattr(windows_job, "_close_desktop", lambda handle: order.append(("desktop", handle)))
    monkeypatch.setattr(windows_job, "_close", lambda handle: order.append(("process", handle)))
    launch_native = Mock(return_value=windows_job._ProcessInfo(process=31, pid=41))
    monkeypatch.setattr(windows_job, "_launch", launch_native)
    states = iter([windows_job._WAIT_TIMEOUT, 0, 0])
    monkeypatch.setattr(windows_job, "_wait", lambda handle, timeout: next(states))

    def exit_code(handle, output):
        assert handle == 31
        cast(output, POINTER(DWORD))[0] = 7
        return True

    monkeypatch.setattr(windows_job, "_exit_code", exit_code)
    argv, env, log = ["worker.exe"], {}, object()
    worker = windows_job.launch_on_private_desktop(argv, cwd=str(tmp_path), env=env, log=log)
    name = order[0][1]
    assert name.startswith("HoardWorker_")
    launch_native.assert_called_once_with((job,), argv, str(tmp_path), env, log,
                                          desktop="TestStation\\" + name, desktop_handle=11,
                                          creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
    assert worker.pid == 41
    assert worker.job_name == name
    assert worker.poll() is None
    assert order == [("create", name)]
    worker.terminate()
    job.terminate.assert_called_once_with()
    assert worker.wait(1) == worker.poll() == 7
    assert order == [("create", name), ("job", 21), ("desktop", 11)]
    worker._cleanup()
    assert order[-1] == ("process", 31)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API declarations")
def test_create_process_assigns_job_and_desktop_without_console_window(monkeypatch, tmp_path):
    from ctypes import cast, POINTER, c_size_t
    from hoard_link.hub import windows_job

    duplicated = iter([11, 12])
    monkeypatch.setattr(windows_job, "_inherit", lambda file, stack: next(duplicated))
    attrs = {}

    def initialize(buffer, count, flags, size):
        assert count == 2
        cast(size, POINTER(c_size_t))[0] = 64
        return buffer is not None

    def update(buffer, flags, key, value, *_):
        attrs[key] = list(value)
        return True

    def create(executable, command, proc, thread, inherit, flags, env, cwd, startup, info):
        assert executable == "runner.exe"
        assert inherit
        assert flags & subprocess.CREATE_NO_WINDOW
        assert not flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS)
        start = cast(startup, POINTER(windows_job._StartupEx))[0].info
        assert start.desktop == "Station\\Worker"
        assert (start.stdin, start.stdout, start.stderr) == (11, 12, 12)
        result = cast(info, POINTER(windows_job._ProcessInfo))[0]
        result.process, result.thread, result.pid = 41, 42, 43
        return True

    close = Mock()
    monkeypatch.setattr(windows_job, "_init_attrs", initialize)
    monkeypatch.setattr(windows_job, "_update_attrs", update)
    monkeypatch.setattr(windows_job, "_delete_attrs", Mock())
    monkeypatch.setattr(windows_job, "_create_process", create)
    monkeypatch.setattr(windows_job, "_close", close)
    info = windows_job._launch((SimpleNamespace(handle=35, name=None),), ["runner.exe"], str(tmp_path),
                               {}, object(), desktop="Station\\Worker", desktop_handle=13,
                               creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS)
    assert info.pid == 43
    assert attrs == {windows_job._HANDLE_LIST: [11, 12, 13], windows_job._JOB_LIST: [35]}
    close.assert_called_once_with(42)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API declarations")
@pytest.mark.parametrize("named, created", [(True, True), (True, False), (False, True)],
                         ids=["named-job", "launch-failed", "fixture-job"])
def test_worker_job_name_has_a_query_handle_in_the_child(monkeypatch, tmp_path, named, created):
    from ctypes import POINTER, c_size_t, cast
    from ctypes.wintypes import HANDLE
    from hoard_link.hub import windows_job

    file_handles = iter([11, 12])
    monkeypatch.setattr(windows_job, "_inherit", lambda file, stack: next(file_handles))
    monkeypatch.setattr(windows_job, "_current", lambda: 91)

    def duplicate(source, handle, destination, result, access, inheritable, options):
        assert (source, handle, destination, access, inheritable, options) == (91, 35, 91, 4, True, 0)
        cast(result, POINTER(HANDLE))[0] = 56
        return True

    def initialize(buffer, count, flags, size):
        cast(size, POINTER(c_size_t))[0] = 64
        return buffer is not None

    def create(*args):
        assert args[4] is True  # bInheritHandles
        if not created:
            windows_job.c.set_last_error(5)
            return False
        cast(args[-1], POINTER(windows_job._ProcessInfo))[0].thread = 42
        return True

    dup, update, close = Mock(side_effect=duplicate), Mock(return_value=True), Mock()
    monkeypatch.setattr(windows_job, "_duplicate", dup)
    monkeypatch.setattr(windows_job, "_init_attrs", initialize)
    monkeypatch.setattr(windows_job, "_update_attrs", update)
    monkeypatch.setattr(windows_job, "_delete_attrs", Mock())
    monkeypatch.setattr(windows_job, "_create_process", create)
    monkeypatch.setattr(windows_job, "_close", close)
    job = SimpleNamespace(name="HoardWorker_" + "a" * 32 if named else None, handle=35)
    args = ((job,), ["runner.exe"], str(tmp_path), {}, object())
    if created:
        windows_job._launch(*args, desktop_handle=13)
    else:
        with pytest.raises(OSError) as error:
            windows_job._launch(*args, desktop_handle=13)
        assert error.value.winerror == 5

    handles = [11, 12, 13, 56] if named else [11, 12, 13]
    assert {call.args[2]: list(call.args[3]) for call in update.call_args_list} == {
        windows_job._HANDLE_LIST: handles, windows_job._JOB_LIST: [35],
    }
    assert dup.call_count == int(named)
    # The Hub drops its temporary duplicate on either outcome, keeping its control handle.
    assert [call.args[0] for call in close.call_args_list] == ([42] if created else []) + ([56] if named else [])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API declarations")
def test_only_fixture_jobs_kill_when_their_handle_closes(monkeypatch):
    from hoard_link.hub import windows_job

    create = Mock(return_value=21)
    set_limits = Mock(return_value=True)
    close = Mock(return_value=True)
    stop = Mock(return_value=True)
    monkeypatch.setattr(windows_job, "_create_job", create)
    monkeypatch.setattr(windows_job, "_set_job", set_limits)
    monkeypatch.setattr(windows_job, "_close", close)
    monkeypatch.setattr(windows_job, "_terminate_job", stop)
    name = "HoardWorker_" + "0" * 32
    worker = windows_job._Job(name)
    create.assert_called_once_with(None, name)
    set_limits.assert_not_called()
    worker.close()
    close.assert_called_once_with(21)
    stop.assert_not_called()
    fixture = windows_job._Job()
    set_limits.assert_called_once()
    fixture.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API declarations")
@pytest.mark.parametrize("member", [True, False])
def test_reopen_worker_job_checks_membership(monkeypatch, member):
    from ctypes import cast, POINTER
    from ctypes.wintypes import BOOL
    from hoard_link.hub import windows_job

    name = "HoardWorker_" + "a" * 32
    close = Mock(return_value=True)
    stop = Mock(return_value=True)
    monkeypatch.setattr(windows_job, "_open_process", Mock(return_value=31))
    monkeypatch.setattr(windows_job, "_open_job", Mock(return_value=41))
    monkeypatch.setattr(windows_job, "_close", close)
    monkeypatch.setattr(windows_job, "_terminate_job", stop)

    def check(process, job, result):
        assert (process, job) == (31, 41)
        cast(result, POINTER(BOOL))[0] = member
        return True

    monkeypatch.setattr(windows_job, "_in_job", check)
    if member:
        job = windows_job.open_worker_job(name, 71)
        job.terminate()
        job.close()
        stop.assert_called_once_with(41, 1)
    else:
        with pytest.raises(OSError, match="does not belong"):
            windows_job.open_worker_job(name, 71)
        stop.assert_not_called()
    assert {call.args[0] for call in close.call_args_list} == {31, 41}


@pytest.mark.parametrize("shutting_down", [False, True])
def test_adopted_job_closes_without_killing_on_hub_shutdown(monkeypatch, shutting_down):
    from threading import Event
    from hoard_link.hub.agora_worker_processes import AdoptedWorker

    stopping = Event()
    if shutting_down:
        stopping.set()
    worker = AdoptedWorker(SimpleNamespace(pid=71, is_running=lambda: True), 10, ())
    job = Mock()
    reopen = Mock(return_value=job)
    monkeypatch.setitem(sys.modules, "hoard_link.hub.windows_job", SimpleNamespace(open_worker_job=reopen))
    monkeypatch.setattr(worker, "_wait_process", lambda _: 0)
    worker.attach_job("recorded-job")
    reopen.assert_called_once_with("recorded-job", 71)
    assert worker.wait(stopping) == 0
    assert job.terminate.call_count == int(not shutting_down)
    job.close.assert_called_once_with()
