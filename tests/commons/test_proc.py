"""hoard_link.proc: bounded subprocesses, tree killing, executable discovery, reveal in the file manager."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from hoard_link import proc
from hoard_link.errors import HoardLinkError

PY = sys.executable
posix_only = pytest.mark.skipif(proc.IS_WIN, reason="POSIX process groups")


def alive(pid: int) -> bool:
    return proc.pid_alive(pid)


# -- run ----------------------------------------------------------------------------------------------------

def test_run_captures_and_closes_stdin():
    done = proc.run([PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); print(repr(sys.stdin.read()))"], timeout=20)
    assert done.returncode == 0
    assert done.stdout.splitlines() == ["out", "''"] and done.stderr.strip() == "err"


def test_run_input_and_utf8():
    done = proc.run([PY, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"], input="ñandú café", timeout=20)
    assert done.stdout == "ÑANDÚ CAFÉ"


def test_explicit_child_environment_stays_authoritative():
    env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0")
    done = proc.run([PY, "-c", "import sys; print(sys.stdout.encoding)"], env=env, timeout=20)
    assert done.stdout.strip() == "ascii"


def test_run_bad_bytes_are_replaced_not_raised():
    done = proc.run([PY, "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')"], timeout=20)
    assert done.stdout == "a�b"


def test_run_bytes_mode():
    done = proc.run([PY, "-c", "import sys; sys.stdout.buffer.write(bytes([0,255,1]))"], text=False, timeout=20)
    assert done.stdout == bytes([0, 255, 1])


def test_run_check_raises():
    with pytest.raises(subprocess.CalledProcessError) as e:
        proc.run([PY, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(3)"], check=True, timeout=20)
    assert e.value.returncode == 3 and "bad" in e.value.stderr
    assert proc.run([PY, "-c", "import sys; sys.exit(3)"], timeout=20).returncode == 3


def test_run_rejects_a_string_command():
    with pytest.raises(TypeError):
        proc.run("echo hi")  # type: ignore[arg-type]


def test_run_missing_program():
    with pytest.raises(FileNotFoundError):
        proc.run(["definitely-not-a-program-hoard"], timeout=5)


def test_run_env_and_cwd(tmp_path):
    env = proc.build_env(HOARD_TEST_VAR="xyz")
    done = proc.run([PY, "-c", "import os; print(os.environ['HOARD_TEST_VAR'], os.getcwd())"], env=env, cwd=tmp_path, timeout=20)
    got, cwd = done.stdout.split(" ", 1)
    assert got == "xyz" and Path(cwd.strip()).resolve() == tmp_path.resolve()


GRANDCHILD = textwrap.dedent("""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print(child.pid, flush=True)
    time.sleep(60)
""")


def _spawn_tree():
    p = proc.popen([PY, "-c", GRANDCHILD], stdout=subprocess.PIPE, text=True)
    child = int(p.stdout.readline())
    return p, child


def test_run_timeout_kills_the_whole_tree(tmp_path):
    marker = tmp_path / "pid"
    code = GRANDCHILD.replace("print(child.pid, flush=True)", f"open({str(marker)!r}, 'w').write(str(child.pid))")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run([PY, "-c", code], timeout=1.5, grace_s=1)
    assert time.monotonic() - t0 < 15
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 5
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(grandchild)


def test_run_timeout_carries_output_so_far():
    with pytest.raises(subprocess.TimeoutExpired) as e:
        proc.run([PY, "-u", "-c", "import time; print('partial', flush=True); time.sleep(60)"], timeout=1.5, grace_s=1)
    assert "partial" in (e.value.output or "")


def test_run_cancel():
    cancel = threading.Event()
    threading.Timer(0.6, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(proc.Cancelled) as e:
        proc.run([PY, "-c", "import time; time.sleep(60)"], cancel=cancel, grace_s=1)
    assert time.monotonic() - t0 < 15
    assert isinstance(e.value, HoardLinkError) and str(e.value) == "Cancelled."


def test_cancelled_is_a_hoardlink_error_with_partial():
    err = proc.Cancelled("stop", partial=b"x")
    assert err.partial == b"x" and isinstance(err, HoardLinkError)


# -- run_streaming ------------------------------------------------------------------------------------------

def test_run_streaming_lines_and_exit_code():
    lines: list[str] = []
    code = proc.run_streaming([PY, "-u", "-c", "print('a'); print('b\\rc'); import sys; sys.exit(4)"], lines.append, timeout=20)
    assert code == 4 and lines[0] == "a" and "b" in lines[1] and lines[-1] in ("c", "b\rc")


def test_run_streaming_stderr_separate():
    out: list[str] = []
    err: list[str] = []
    proc.run_streaming([PY, "-u", "-c", "import sys; print('o'); print('e', file=sys.stderr)"], out.append, stderr_line=err.append, timeout=20)
    assert out == ["o"] and err == ["e"]


def test_run_streaming_merges_stderr_by_default():
    out: list[str] = []
    proc.run_streaming([PY, "-u", "-c", "import sys; print('e', file=sys.stderr)"], out.append, timeout=20)
    assert out == ["e"]


def test_run_streaming_callback_error_does_not_stop_the_reader():
    seen: list[str] = []

    def cb(line: str) -> None:
        seen.append(line)
        raise RuntimeError("parser bug")

    assert proc.run_streaming([PY, "-u", "-c", "print(1); print(2)"], cb, timeout=20) == 0
    assert seen == ["1", "2"]


def test_run_streaming_timeout_and_cancel():
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run_streaming([PY, "-u", "-c", "import time; time.sleep(60)"], lambda _l: None, timeout=1, grace_s=1)
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(proc.Cancelled):
        proc.run_streaming([PY, "-u", "-c", "import time; time.sleep(60)"], lambda _l: None, cancel=cancel, grace_s=1)


# -- popen / kill -------------------------------------------------------------------------------------------

def test_popen_defaults_close_stdin():
    p = proc.popen([PY, "-c", "import sys; print(sys.stdin.read() == '')"], stdout=subprocess.PIPE, text=True)
    assert p.communicate(timeout=20)[0].strip() == "True"


@posix_only
def test_popen_leads_its_own_group():
    p = proc.popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(p.pid) == p.pid != os.getpgrp()
    finally:
        proc.kill_tree(p, grace_s=1)


@posix_only
def test_popen_low_priority():
    p = proc.popen([PY, "-c", "import time; time.sleep(30)"], low_priority=True)
    try:
        assert os.getpriority(os.PRIO_PROCESS, p.pid) >= 1
    finally:
        proc.kill_tree(p, grace_s=1)


def test_popen_detached_silences_stdio():
    p = proc.popen([PY, "-c", "print('hidden')"], detached=True)
    assert p.wait(timeout=20) == 0 and p.stdout is None


def test_kill_tree_popen_and_pid():
    p, child = _spawn_tree()
    assert proc.kill_tree(p, grace_s=1) is True
    deadline = time.monotonic() + 5
    while alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(child) and p.poll() is not None
    assert proc.kill_tree(p) is True  # already gone


def test_kill_tree_by_pid_forced():
    p = subprocess.Popen([PY, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN) if hasattr(signal, 'SIGTERM') else None; time.sleep(60)"],
                         start_new_session=not proc.IS_WIN)
    time.sleep(0.4)
    try:
        assert proc.kill_tree(p.pid, grace_s=0.5) is True or p.poll() is not None
    finally:
        p.kill()
        p.wait()


def test_request_stop_posix_is_polite():
    if proc.IS_WIN:
        pytest.skip("CTRL_BREAK on Windows")
    code = "import signal, sys, time\nsignal.signal(signal.SIGTERM, lambda *a: (print('saved', flush=True), sys.exit(0)))\nprint('ready', flush=True)\ntime.sleep(60)"
    p = proc.popen([PY, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    proc.request_stop(p)
    assert p.wait(timeout=10) == 0 and p.stdout.read().strip() == "saved"


def test_kill_tree_windows_uses_taskkill_tree(monkeypatch):
    calls: list[list[str]] = []
    state = {"alive": True}

    class Fake:
        pid = 4242

        def poll(self):
            return None if state["alive"] else 1

        def kill(self):
            pass

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "/F" in cmd:
            state["alive"] = False
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(proc, "IS_WIN", True)
    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    assert proc.kill_tree(Fake(), grace_s=0.2) is True
    assert calls[0] == ["taskkill", "/PID", "4242", "/T"]
    assert calls[1] == ["taskkill", "/PID", "4242", "/T", "/F"]


def test_kill_tree_windows_zero_grace_goes_straight_to_force(monkeypatch):
    calls: list[list[str]] = []
    state = {"alive": True}

    class Fake:
        pid = 7

        def poll(self):
            return None if state["alive"] else 0

        def kill(self):
            pass

    def fake_run(cmd, **kw):
        calls.append(cmd)
        state["alive"] = False
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(proc, "IS_WIN", True)
    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    assert proc.kill_tree(Fake(), grace_s=0) is True
    assert calls == [["taskkill", "/PID", "7", "/T", "/F"]]


def test_windows_creation_flags(monkeypatch):
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen.update(kw)
            seen["argv"] = argv

    monkeypatch.setattr(proc, "IS_WIN", True)
    monkeypatch.setattr(proc.subprocess, "Popen", FakePopen)
    proc.popen(["x.exe"], low_priority=True)
    flags = seen["creationflags"]
    assert flags & 0x08000000 and flags & 0x00000200 and flags & 0x00004000 and not flags & 0x01000000
    assert proc.no_window_kwargs() == {"creationflags": 0x08000000}
    proc.popen(["x.exe"], detached=True)
    assert seen["creationflags"] & 0x01000000  # breakaway tried first


def test_pid_alive():
    assert proc.pid_alive(os.getpid())
    assert not proc.pid_alive(0) and not proc.pid_alive(-5)
    p = subprocess.Popen([PY, "-c", "pass"])
    p.wait()
    assert not proc.pid_alive(p.pid)


# -- build_env / tail_lines -----------------------------------------------------------------------------------

def test_build_env_forces_utf8_and_drops_none():
    env = proc.build_env({"A": "1", "B": "2"}, B=None, C=3)
    assert env == {"A": "1", "C": "3", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    assert proc.build_env()["PYTHONUTF8"] == "1"


def test_tail_lines():
    text = "\n".join(f"line {i}" for i in range(30)) + "\n\n"
    assert proc.tail_lines(text, 3) == "line 27\nline 28\nline 29"
    assert len(proc.tail_lines("x" * 5000, limit=100)) == 100
    assert proc.tail_lines(None) == ""


# -- finding programs ---------------------------------------------------------------------------------------

def make_exe(directory: Path, name: str, *, win: bool = False, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / (name + (".exe" if win else ""))
    p.write_text(body)
    p.chmod(0o755)
    return p


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    for var in ("HOARD_TOOL", "LEGACY_TOOL", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "USERPROFILE", "ChocolateyInstall"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@posix_only
def test_candidate_order_posix(clean_env, monkeypatch):
    t = clean_env
    explicit = make_exe(t / "explicit", "tool")
    env_one = make_exe(t / "env1", "tool")
    extra = make_exe(t / "extra", "tool")
    on_path = make_exe(t / "pathdir", "tool")
    hoard_bin = make_exe(t / "home" / "bin", "tool")
    monkeypatch.setenv("HOARD_TOOL", str(env_one))
    monkeypatch.setenv("PATH", str(t / "pathdir"))
    got = proc.exe_candidates("tool", explicit=str(explicit), env_vars=["HOARD_TOOL"], extra_dirs=[t / "extra"])
    assert [(Path(p), how) for p, how in got][:3] == [(explicit, "explicit"), (env_one, "env"), (extra, "extra")]
    hows = [how for _p, how in got]
    assert hows.index("path") > hows.index("extra") and hows[-1] == "hoard-bin"
    assert Path(got[-1][0]) == hoard_bin and Path(got[3][0]) == on_path


@posix_only
def test_env_var_may_name_a_folder_or_a_command(clean_env, monkeypatch):
    t = clean_env
    exe = make_exe(t / "folder", "tool")
    monkeypatch.setenv("HOARD_TOOL", str(t / "folder"))
    assert Path(proc.exe_candidates("tool", env_vars=["HOARD_TOOL"])[0][0]) == exe
    monkeypatch.setenv("PATH", str(t / "folder"))
    monkeypatch.setenv("HOARD_TOOL", "tool")
    assert Path(proc.exe_candidates("tool", env_vars=["HOARD_TOOL"])[0][0]) == exe
    monkeypatch.setenv("HOARD_TOOL", '"' + str(exe) + '"')  # quotes pasted from a shell
    assert Path(proc.exe_candidates("tool", env_vars=["HOARD_TOOL"])[0][0]) == exe


@posix_only
def test_non_executable_files_are_skipped(clean_env, monkeypatch):
    t = clean_env
    (t / "d").mkdir()
    f = t / "d" / "tool"
    f.write_text("x")
    f.chmod(0o644)
    monkeypatch.setenv("PATH", str(t / "d"))
    assert proc.exe_candidates("tool") == []


@posix_only
def test_find_exe_verify_skips_broken_candidates(clean_env, monkeypatch):
    t = clean_env
    bad = make_exe(t / "bad", "tool", body="#!/bin/sh\nexit 1\n")
    good = make_exe(t / "good", "tool", body="#!/bin/sh\necho v1\nexit 0\n")
    monkeypatch.setenv("PATH", os.pathsep.join([str(t / "bad"), str(t / "good")]))
    assert proc.find_exe("tool") == str(bad)
    assert proc.find_exe("tool", verify_args=["-version"]) == str(good)
    assert proc.find_exe("nothing-like-this", verify_args=["-version"]) is None
    assert proc.which("tool") == str(bad)


def test_windows_lookup_uses_exe_not_shims(clean_env, monkeypatch):
    t = clean_env
    local = t / "Local"
    winget = make_exe(local / "Microsoft" / "WinGet" / "Links", "ffmpeg", win=True)
    shim = t / "pathdir"
    shim.mkdir()
    (shim / "ffmpeg.cmd").write_text("@echo off")
    (shim / "ffmpeg.bat").write_text("@echo off")
    monkeypatch.setattr(proc, "IS_WIN", True)
    monkeypatch.setattr(proc, "_is_runnable", lambda p: p.is_file())
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("PATH", str(shim))
    got = proc.exe_candidates("ffmpeg")
    assert [(Path(p), how) for p, how in got] == [(winget, "winget")]  # the .cmd/.bat shims are not offered
    assert proc.exe_candidates("ffmpeg", allow_scripts=True)[0] == (str(shim / "ffmpeg.cmd"), "path")


def test_windows_program_files_and_hoard_bin(clean_env, monkeypatch):
    t = clean_env
    pf = t / "PF"
    node = make_exe(pf / "nodejs", "node", win=True)
    mine = make_exe(t / "home" / "bin", "node", win=True)
    monkeypatch.setattr(proc, "IS_WIN", True)
    monkeypatch.setattr(proc, "_is_runnable", lambda p: p.is_file())
    monkeypatch.setenv("ProgramFiles", str(pf))
    got = proc.exe_candidates("node")
    assert [(Path(p), how) for p, how in got] == [(node, "system"), (mine, "hoard-bin")]


def test_exe_names_on_windows(monkeypatch):
    monkeypatch.setattr(proc, "IS_WIN", True)
    assert proc._exe_names("ffmpeg", False) == ["ffmpeg.exe", "ffmpeg.com"]
    assert proc._exe_names("ffmpeg.exe", False) == ["ffmpeg.exe"]
    assert "ffmpeg.cmd" in proc._exe_names("ffmpeg", True)


# -- reveal -------------------------------------------------------------------------------------------------

def test_reveal_command_windows_is_an_argument_list(tmp_path):
    f = tmp_path / 'My odd file, 1.txt'
    f.write_text("x")
    assert proc.reveal_command(f, platform="win32") == ["explorer.exe", "/select,", str(f)]


def test_reveal_command_missing_file_opens_the_folder(tmp_path):
    ghost = tmp_path / "gone.txt"
    assert proc.reveal_command(ghost, platform="win32") == ["explorer.exe", str(tmp_path)]
    assert proc.reveal_command(tmp_path / "no" / "such" / "x", platform="win32") is None


def test_reveal_command_other_platforms(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert proc.reveal_command(f, platform="darwin") == ["open", "-R", str(f)]
    assert proc.reveal_command(f, platform="linux") == ["xdg-open", str(tmp_path)]
    assert proc.reveal_command(tmp_path, platform="linux") == ["xdg-open", str(tmp_path)]


def test_reveal_in_file_manager(monkeypatch, tmp_path):
    started: list = []
    monkeypatch.setattr(proc, "popen", lambda cmd, **kw: started.append((cmd, kw)))
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert proc.reveal_in_file_manager(f) is True and started[0][1] == {"detached": True}
    assert proc.reveal_in_file_manager(tmp_path / "x" / "y") is False

    def boom(cmd, **kw):
        raise OSError("no file manager")

    monkeypatch.setattr(proc, "popen", boom)
    assert proc.reveal_in_file_manager(f) is False
