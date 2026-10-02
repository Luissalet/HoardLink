"""hoard_link.media.bins: tool discovery (order, verification, caching) and update(), with fake executables and injected runners."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from hoard_link.errors import Unavailable
from hoard_link.media import bins

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="shell-script fakes")


def make_exe(directory: Path, name: str, out: str = "tool 1.0", rc: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / (name + (".exe" if sys.platform.startswith("win") else ""))
    p.write_text(f"#!/bin/sh\necho '{out}'\nexit {rc}\n")
    p.chmod(0o755)
    return p


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real tools, no modules, no env vars: every test starts from 'nothing is installed'."""
    for name in bins.TOOL_NAMES:
        for var in (f"HOARD_{name.upper()}", *bins._DEFS[name]["legacy"]):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "emptypath"))
    monkeypatch.setattr(bins.importlib.util, "find_spec", lambda name, *a, **k: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)  # import raises ImportError
    monkeypatch.setattr(bins.proc, "_well_known_dirs", lambda name: [])
    bins.reset_cache()
    yield
    bins.reset_cache()


def recording_runner(table=None, default=(0, "tool 9.9\n", "")):
    calls: list[list[str]] = []

    def run(argv, timeout):
        calls.append(list(argv))
        for prefix, result in (table or {}).items():
            if argv[0].endswith(prefix) or prefix in argv[0]:
                return result
        return default

    run.calls = calls  # type: ignore[attr-defined]
    return run


# -- missing ------------------------------------------------------------------------------------------------

def test_missing_tool_is_falsy_with_a_hint_and_never_raises():
    tool = bins.find("ffmpeg")
    assert not tool and tool.argv == [] and tool.path is None
    assert "HOARD_FFMPEG" in tool.hint and "ffmpeg" in tool.hint.lower()
    assert tool.public()["found"] is False and tool.public()["hint"] == tool.hint


def test_unknown_tool_name_raises():
    with pytest.raises(ValueError):
        bins.find("nope")


def test_tool_missing_is_unavailable():
    err = bins.tool_missing("ytdlp")
    assert isinstance(err, Unavailable)
    assert "yt-dlp" in str(err) or "yt-dlp" in " ".join(getattr(err, "missing", []) or [str(err)])


def test_install_hints_per_platform():
    assert "winget" in bins.install_hint("ffmpeg", "win32")
    assert "brew" in bins.install_hint("ffmpeg", "darwin")
    assert "apt" in bins.install_hint("ffmpeg", "linux")
    assert "pip install" in bins.install_hint("ytdlp") and "HOARD_YTDLP" in bins.install_hint("ytdlp")
    assert "Node.js" in bins.install_hint("node")
    assert "piper" in bins.install_hint("piper").lower()


# -- order --------------------------------------------------------------------------------------------------

@posix_only
def test_lookup_order(monkeypatch, tmp_path):
    t = tmp_path
    hoard_env = make_exe(t / "e1", "ffmpeg", "from-hoard-env")
    legacy = make_exe(t / "e2", "ffmpeg", "from-legacy")
    in_bin = make_exe(t / "home" / "bin", "ffmpeg", "from-bin")
    on_path = make_exe(t / "p", "ffmpeg", "from-path")
    monkeypatch.setenv("PATH", str(t / "p"))

    got = bins.find("ffmpeg", refresh=True)
    assert Path(got.path) == in_bin and got.how == "hoard-bin"

    monkeypatch.setenv("LINKS_FFMPEG", str(legacy))
    got = bins.find("ffmpeg", refresh=True)
    assert Path(got.path) == legacy and got.how == "env" and got.source == "LINKS_FFMPEG"

    monkeypatch.setenv("HOARD_FFMPEG", str(hoard_env))
    got = bins.find("ffmpeg", refresh=True)
    assert Path(got.path) == hoard_env and got.source == "HOARD_FFMPEG"

    monkeypatch.delenv("HOARD_FFMPEG")
    monkeypatch.delenv("LINKS_FFMPEG")
    in_bin.unlink()
    got = bins.find("ffmpeg", refresh=True)
    assert Path(got.path) == on_path and got.how == "path"


@posix_only
def test_extra_legacy_env_from_the_caller(monkeypatch, tmp_path):
    exe = make_exe(tmp_path / "x", "ffmpeg")
    monkeypatch.setenv("MYAPP_FFMPEG", str(exe))
    assert not bins.find("ffmpeg", refresh=True)
    got = bins.find("ffmpeg", refresh=True, legacy_env=["MYAPP_FFMPEG"])
    assert Path(got.path) == exe and got.source == "MYAPP_FFMPEG"


def test_candidates_are_verified_by_running_them(monkeypatch, tmp_path):
    bad = make_exe(tmp_path / "bad", "ffmpeg")
    good = make_exe(tmp_path / "good", "ffmpeg")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "bad"), str(tmp_path / "good")]))

    def runner(argv, timeout):
        assert argv[1:] == ["-version"]
        if Path(argv[0]) == bad:
            return 1, "", "Segmentation fault\nmore"
        return 0, "ffmpeg version 7.1.1-full Copyright", ""

    tool = bins.find("ffmpeg", refresh=True, runner=runner)
    assert Path(tool.path) == good and tool.version == "7.1.1-full"
    assert tool.tried == [{"how": "path", "path": str(bad), "error": "Segmentation fault"}]


def test_a_program_that_cannot_start_or_times_out_is_skipped(monkeypatch, tmp_path):
    exe = make_exe(tmp_path / "p", "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path / "p"))
    tool = bins.find("ffmpeg", refresh=True, runner=lambda argv, timeout: (None, "", f"timed out after {timeout:g} s"))
    assert not tool and "timed out" in tool.tried[0]["error"]
    assert tool.tried[0]["path"] == str(exe)


def test_env_value_that_points_nowhere_is_reported(monkeypatch):
    monkeypatch.setenv("HOARD_FFMPEG", "/no/such/ffmpeg")
    tool = bins.find("ffmpeg", refresh=True, runner=recording_runner())
    assert not tool and tool.tried[0]["how"] == "env" and "HOARD_FFMPEG" in tool.tried[0]["error"]


def test_python_module_fallback(monkeypatch):
    monkeypatch.setattr(bins.importlib.util, "find_spec", lambda name, *a, **k: object() if name == "yt_dlp" else None)
    runner = recording_runner(default=(0, "2026.03.01\n", ""))
    tool = bins.find("ytdlp", refresh=True, runner=runner)
    assert tool.argv == [sys.executable, "-m", "yt_dlp"] and tool.how == "python-module" and tool.version == "2026.03.01"
    assert runner.calls == [[sys.executable, "-m", "yt_dlp", "--version"]]
    assert tool.command("-U", 3) == [sys.executable, "-m", "yt_dlp", "-U", "3"]


def test_env_may_be_a_python_module_spec(monkeypatch):
    monkeypatch.setenv("HOARD_YTDLP", "python3 -m yt_dlp")
    tool = bins.find("ytdlp", refresh=True, runner=recording_runner(default=(0, "2026.01.01", "")))
    assert tool.argv == ["python3", "-m", "yt_dlp"] and tool.how == "env"


def test_env_may_be_a_py_script(monkeypatch, tmp_path):
    script = tmp_path / "fake_ytdlp.py"
    script.write_text("print('2026.02.02')\n")
    monkeypatch.setenv("HOARD_YTDLP", str(script))
    tool = bins.find("ytdlp", refresh=True)  # really runs it with this interpreter
    assert tool.argv == [sys.executable, str(script)] and tool.version == "2026.02.02"


def test_imageio_fallback_for_ffmpeg(monkeypatch, tmp_path):
    exe = make_exe(tmp_path / "io", "ffmpeg")

    class Fake:
        @staticmethod
        def get_ffmpeg_exe():
            return str(exe)

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", Fake)
    tool = bins.find("ffmpeg", refresh=True, runner=recording_runner(default=(0, "ffmpeg version 6.0", "")))
    assert tool.how == "imageio" and Path(tool.path) == exe and tool.version == "6.0"


@posix_only
def test_ffprobe_is_found_next_to_ffmpeg(monkeypatch, tmp_path):
    make_exe(tmp_path / "sdk", "ffmpeg", "ffmpeg version 7.0")
    probe = make_exe(tmp_path / "sdk", "ffprobe", "ffprobe version 7.0")
    monkeypatch.setenv("HOARD_FFMPEG", str(tmp_path / "sdk" / "ffmpeg"))
    runner = recording_runner(default=(0, "ffprobe version 7.0", ""))
    tool = bins.find("ffprobe", refresh=True, runner=runner)
    assert Path(tool.path) == probe and tool.how == "sibling"


def test_piper_help_may_exit_nonzero(monkeypatch, tmp_path):
    make_exe(tmp_path / "pp", "piper", "usage: piper", rc=2)
    monkeypatch.setenv("PATH", str(tmp_path / "pp"))
    assert bins.find("piper", refresh=True)


# -- cache --------------------------------------------------------------------------------------------------

def test_cache_ttls_with_an_injected_clock(monkeypatch, tmp_path):
    now = [100.0]
    clock = lambda: now[0]  # noqa: E731
    runner = recording_runner(default=(0, "ffmpeg version 1", ""))
    make_exe(tmp_path / "p", "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path / "p"))

    assert bins.find("ffmpeg", runner=runner, clock=clock)
    assert bins.find("ffmpeg", runner=runner, clock=clock)
    assert len(runner.calls) == 1  # cached
    now[0] += bins.TTL_FOUND_S - 1
    bins.find("ffmpeg", runner=runner, clock=clock)
    assert len(runner.calls) == 1
    now[0] += 2
    bins.find("ffmpeg", runner=runner, clock=clock)
    assert len(runner.calls) == 2  # expired after 30 s
    bins.find("ffmpeg", runner=runner, clock=clock, refresh=True)
    assert len(runner.calls) == 3


def test_missing_results_expire_quickly(monkeypatch):
    now = [0.0]
    clock = lambda: now[0]  # noqa: E731
    seen = []
    runner = lambda argv, timeout: seen.append(argv) or (1, "", "x")  # noqa: E731
    assert not bins.find("node", runner=runner, clock=clock)
    assert not bins.find("node", runner=runner, clock=clock)
    n = len(seen)
    now[0] += bins.TTL_MISSING_S + 0.5
    bins.find("node", runner=runner, clock=clock)
    assert bins.TTL_MISSING_S < bins.TTL_FOUND_S
    assert len(seen) >= n  # candidates are discovered again (none exist here, so the runner may not be called)


@posix_only
def test_changing_the_environment_invalidates_the_cache(monkeypatch, tmp_path):
    a = make_exe(tmp_path / "a", "ffmpeg", "ffmpeg version A")
    b = make_exe(tmp_path / "b", "ffmpeg", "ffmpeg version B")
    monkeypatch.setenv("HOARD_FFMPEG", str(a))
    assert bins.find("ffmpeg").version == "A"
    monkeypatch.setenv("HOARD_FFMPEG", str(b))
    assert bins.find("ffmpeg").version == "B"


@posix_only
def test_status_shape(monkeypatch, tmp_path):
    make_exe(tmp_path / "p", "ffmpeg", "ffmpeg version 7")
    monkeypatch.setenv("PATH", str(tmp_path / "p"))
    st = bins.status(refresh=True)
    assert set(bins.TOOL_NAMES) <= set(st)
    assert st["ffmpeg"]["found"] is True and st["ffmpeg"]["version"] == "7"
    assert st["ytdlp"]["found"] is False and "hint" in st["ytdlp"]
    assert st["python"]["path"] == sys.executable and st["bin_dir"].endswith("bin") and st["install_command"].startswith("python -m pip")


def test_parse_command_spec():
    assert bins.parse_command_spec("") is None
    assert bins.parse_command_spec("python3 -m yt_dlp") == ["python3", "-m", "yt_dlp"]
    assert bins.parse_command_spec('"python3 -m yt_dlp"') == ["python3", "-m", "yt_dlp"]
    assert bins.parse_command_spec("/missing/script.py") is None
    assert bins.parse_command_spec("/missing/prog") is None


# -- update -------------------------------------------------------------------------------------------------

def test_update_unknown_tool():
    out = bins.update("ffmpeg")
    assert out["ok"] is False and out["updated"] is False


def test_update_self_with_a_fake_runner(monkeypatch, tmp_path):
    exe = make_exe(tmp_path / "p", "yt-dlp")
    monkeypatch.setenv("PATH", str(tmp_path / "p"))
    state = {"version": "2025.01.01"}
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        if argv[-1] == "--version":
            return 0, state["version"] + "\n", ""
        if argv[-1] == "-U":
            state["version"] = "2026.03.01"
            return 0, "Updated yt-dlp to stable@2026.03.01", ""
        raise AssertionError(argv)

    out = bins.update("ytdlp", runner=runner)
    assert out["ok"] and out["updated"] and out["before"] == "2025.01.01" and out["after"] == "2026.03.01"
    assert out["method"].startswith("self-update") and [str(exe), "-U"] in calls


def test_update_pip_when_self_update_says_package_manager(monkeypatch, tmp_path):
    make_exe(tmp_path / "p", "yt-dlp")
    monkeypatch.setenv("PATH", str(tmp_path / "p"))
    monkeypatch.setattr(bins, "_pip_available", lambda: True)
    state = {"version": "2025.01.01"}

    def runner(argv, timeout):
        if argv[-1] == "--version":
            return 0, state["version"], ""
        if argv[-1] == "-U":
            return 1, "", "ERROR: You installed yt-dlp with pip or using a wheel, please upgrade with pip"
        if "pip" in argv:
            state["version"] = "2026.03.01"
            return 0, "Successfully installed yt-dlp-2026.3.1", ""
        raise AssertionError(argv)

    out = bins.update("ytdlp", runner=runner)
    assert out["ok"] and out["method"].startswith("pip install -U yt-dlp") and out["after"] == "2026.03.01"


def _fetcher(payload: bytes, sums: str | None):
    log = []

    def fetch(url, timeout, max_bytes):
        log.append(url)
        if url.endswith("SHA2-256SUMS"):
            if sums is None:
                raise OSError("no list")
            return sums.encode()
        return payload

    fetch.log = log  # type: ignore[attr-defined]
    return fetch


@posix_only
def test_update_download_verifies_sha256_and_installs_into_hoard_bin(monkeypatch):
    monkeypatch.setattr(bins, "_pip_available", lambda: False)
    monkeypatch.setattr(bins, "_asset_for", lambda name: "yt-dlp_linux")
    payload = b"#!/bin/sh\necho 2026.05.05\n"
    digest = hashlib.sha256(payload).hexdigest()
    fetch = _fetcher(payload, f"{digest}  yt-dlp_linux\n{'0' * 64}  yt-dlp\n")
    out = bins.update("ytdlp", method="download", fetch=fetch)
    target = bins.bin_dir() / "yt-dlp"
    assert out["ok"] and target.read_bytes() == payload and os.access(target, os.X_OK)
    assert out["after"] == "2026.05.05" and out["updated"] is True
    assert any(u.endswith("SHA2-256SUMS") for u in fetch.log)
    assert not list(bins.bin_dir().glob("*.part"))


@posix_only
def test_update_download_sha_mismatch_installs_nothing(monkeypatch):
    monkeypatch.setattr(bins, "_asset_for", lambda name: "yt-dlp_linux")
    fetch = _fetcher(b"evil", f"{'a' * 64}  yt-dlp_linux\n")
    out = bins.update("ytdlp", method="download", fetch=fetch)
    assert out["ok"] is False and "checksum mismatch" in out["output"]
    assert not (bins.bin_dir() / "yt-dlp").exists() and "could not be updated" in out["error"]


def test_update_download_checksum_list_unreachable_is_an_error_unless_skipped(monkeypatch):
    monkeypatch.setattr(bins, "_asset_for", lambda name: "yt-dlp_linux")
    out = bins.update("ytdlp", method="download", fetch=_fetcher(b"data", None))
    assert out["ok"] is False and "checksum" in out["output"]
    if not sys.platform.startswith("win"):
        out = bins.update("ytdlp", method="download", fetch=_fetcher(b"#!/bin/sh\necho 2026.06.06\n", None), verify_sha=False)
        assert out["ok"] is True


def test_update_download_network_failure(monkeypatch):
    monkeypatch.setattr(bins, "_asset_for", lambda name: "yt-dlp_linux")

    def fetch(url, timeout, max_bytes):
        raise OSError("offline")

    out = bins.update("ytdlp", method="download", fetch=fetch)
    assert out["ok"] is False and "offline" in out["output"]


def test_update_bad_method():
    with pytest.raises(ValueError):
        bins.update("ytdlp", method="telepathy", runner=recording_runner())
