"""Login autostart (Windows Startup folder, a temporary APPDATA here) and
``--profile`` on startup."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hoard_link.hub import __main__ as cli
from hoard_link.hub import autostart
from hoard_link.hub.server import make_server


@pytest.fixture
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "_is_windows", lambda: True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    return tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def test_install_status_uninstall(windows):
    res = autostart.install("video", repo_dir="C:/apps/Hoard Link", executable="C:/py/python.exe")
    assert res["ok"] and Path(res["path"]) == windows / "Hoard Hub.cmd"
    text = (windows / "Hoard Hub.cmd").read_bytes().decode("utf-8")
    assert text.startswith("@echo off\r\n") and autostart.MARKER in text   # CRLF for cmd.exe
    assert 'cd /d "C:/apps/Hoard Link"' in text
    exe = str(Path("C:/py/python.exe"))                  # C:\py\python.exe on Windows
    assert f'start "" {exe} -m hoard_link.hub --no-window --profile video\r\n' in text
    st = autostart.status()
    assert st["installed"] and st["ours"] and st["profile"] == "video" and st["window"] is False
    assert "installed, headless, profile video" in autostart.describe(st)
    gone = autostart.uninstall()
    assert gone["removed"] and not (windows / "Hoard Hub.cmd").exists()
    assert autostart.uninstall()["removed"] is False
    assert autostart.status()["installed"] is False


def test_pythonw_is_preferred(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert autostart.pythonw(str(tmp_path / "python.exe")).endswith("pythonw.exe")
    (tmp_path / "pythonw.exe").unlink()
    assert autostart.pythonw(str(tmp_path / "python.exe")).endswith("python.exe")


def test_window_mode_and_quoting(windows):
    autostart.install("my profile", window=True, repo_dir="C:/r", executable="C:/Program Files/Py/python.exe")
    text = (windows / "Hoard Hub.cmd").read_bytes().decode("utf-8")
    exe = str(Path("C:/Program Files/Py/python.exe"))
    assert f'start "" "{exe}" -m hoard_link.hub --stay --profile "my profile"\r\n' in text
    assert "\n" not in text.replace("\r\n", "")          # CRLF only
    st = autostart.status()
    assert st["window"] is True and st["profile"] == "my profile"


def test_foreign_startup_file_is_left_alone(windows):
    windows.mkdir(parents=True)
    (windows / "Hoard Hub.cmd").write_text("@echo off\r\necho mine\r\n", encoding="utf-8")
    res = autostart.uninstall()
    assert res["ok"] is False and (windows / "Hoard Hub.cmd").exists()


def test_cli_on_windows(windows, capsys, tmp_path):
    assert cli.main(["--install-autostart", "--profile", "video", "--data-dir", str(tmp_path / "d")]) == 0
    out = capsys.readouterr().out
    assert "installed" in out and "not defined in hub.json" in out
    text = (windows / "Hoard Hub.cmd").read_text(encoding="utf-8")
    assert "--data-dir" in text and "--profile video" in text
    assert cli.main(["--autostart-status"]) == 0 and "profile video" in capsys.readouterr().out
    assert cli.main(["--uninstall-autostart"]) == 0 and "removed" in capsys.readouterr().out


def test_cli_elsewhere_is_a_clear_no_op(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert cli.main(["--install-autostart"]) == 1
    out = capsys.readouterr().out
    assert "only automated on Windows" in out and "--no-window" in out
    assert not any(tmp_path.iterdir())
    assert cli.main(["--autostart-status"]) == 0
    assert cli.main(["--uninstall-autostart"]) == 0


def test_profile_on_startup(hub, monkeypatch):
    hub.config.profiles = {"p": {"apps": ["launch"]}}
    res = cli._start_profile(hub, "p")
    assert res["ok"], res
    assert cli._start_profile(hub, "nope")["ok"] is False
    hub.stop("launch")


def test_profile_on_second_launch_goes_to_the_running_hub(hub):
    hub.config.profiles = {"p": {"apps": ["launch"]}}
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert cli._start_profile_remote(hub.config.url, "p")["ok"]
        assert cli._start_profile_remote(hub.config.url, "nope")["ok"] is False
    finally:
        server.shutdown()
        hub.stop("launch")
