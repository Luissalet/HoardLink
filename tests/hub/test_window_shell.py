"""The Hoard Window shell route of desktop.open_window."""

from __future__ import annotations

import os
import sys

from hoard_link.hub import desktop


def _fake_shell(tmp_path):
    folder = tmp_path / "shell"
    dist = folder / "node_modules" / "electron" / "dist"
    if sys.platform.startswith("win"):
        exe = dist / "electron.exe"
    elif sys.platform == "darwin":
        exe = dist / "Electron.app" / "Contents" / "MacOS" / "Electron"
    else:
        exe = dist / "electron"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (folder / "main.cjs").write_text("")
    return folder, exe


def test_find_shell_needs_main_and_electron(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_WINDOW_SHELL", str(tmp_path / "nope"))
    assert desktop.find_shell() is None
    folder, exe = _fake_shell(tmp_path)
    monkeypatch.setenv("HOARD_WINDOW_SHELL", str(folder))
    assert desktop.find_shell() == (str(exe), str(folder))


def test_window_engine_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_WINDOW_SHELL", str(tmp_path / "nope"))
    assert desktop.window_engine("auto") == "chromium"
    assert desktop.window_engine("shell") == "chromium"  # not installed: fall back
    folder, _ = _fake_shell(tmp_path)
    monkeypatch.setenv("HOARD_WINDOW_SHELL", str(folder))
    assert desktop.window_engine("auto") == "shell"
    assert desktop.window_engine("chromium") == "chromium"


def test_open_window_uses_shell_with_profile_flag(tmp_path, monkeypatch):
    folder, exe = _fake_shell(tmp_path)
    monkeypatch.setenv("HOARD_WINDOW_SHELL", str(folder))
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG")
    seen = {}

    class P:
        pid = 4242

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env")
        return P()

    monkeypatch.setattr(desktop.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    res = desktop.open_window("http://127.0.0.1:5181/", "links", str(tmp_path / "profiles"),
                              name="Links Hoard", icon=str(icon), size=(1000, 700))
    assert res["ok"] and res["engine"] == "shell" and res["pid"] == 4242
    cmd = seen["cmd"]
    assert cmd[0] == str(exe) and cmd[1] == str(folder)
    assert "--hoard-url=http://127.0.0.1:5181/" in cmd
    assert "--hoard-id=links" in cmd and "--hoard-name=Links Hoard" in cmd
    assert "--hoard-size=1000x700" in cmd and f"--hoard-icon={icon}" in cmd
    profile = os.path.join(os.path.abspath(str(tmp_path / "profiles")), "links")
    assert f"--user-data-dir={profile}" in cmd
    # A stray ELECTRON_RUN_AS_NODE would make electron.exe a plain node.
    assert "ELECTRON_RUN_AS_NODE" not in seen["env"]


def test_open_window_chromium_when_forced(tmp_path, monkeypatch):
    folder, _ = _fake_shell(tmp_path)
    monkeypatch.setenv("HOARD_WINDOW_SHELL", str(folder))
    monkeypatch.setattr(desktop, "find_browser", lambda pref="auto": None)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: True)
    res = desktop.open_window("http://127.0.0.1:1/", "x", str(tmp_path / "p"), engine="chromium")
    assert res["mode"] == "browser-tab"
