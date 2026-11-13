"""The Hoard Window shell route of desktop.open_window."""

from __future__ import annotations

import base64
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


def _relaunch_arg(cmd):
    hits = [a for a in cmd if a.startswith("--hoard-relaunch=")]
    return base64.urlsafe_b64decode(hits[0].split("=", 1)[1]).decode("utf-8") if hits else None


def test_relaunch_command_reopens_hub_or_app(tmp_path):
    py = tmp_path / "python.exe"
    pyw = tmp_path / "pythonw.exe"
    py.write_text("")
    pyw.write_text("")
    script = os.path.join(os.path.dirname(os.path.abspath(desktop.__file__)), "relaunch.py")
    assert os.path.isfile(script)
    hub = desktop.relaunch_command(desktop.HUB_WINDOW_ID, python=str(py))
    assert hub == f'"{pyw}" "{script}"'           # console-less interpreter, no arguments: the Hub
    app = desktop.relaunch_command("links", python=str(py))
    assert app == f'"{pyw}" "{script}" --app "links"'


def test_shell_window_gets_a_pin_command_on_windows(tmp_path, monkeypatch):
    folder, exe = _fake_shell(tmp_path)
    monkeypatch.setattr(desktop, "find_shell", lambda: (str(exe), str(folder)))
    seen = {}

    class P:
        pid = 1

    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kw: seen.setdefault("cmd", cmd) and P())
    for platform, expected in (("win32", True), ("linux", False)):
        seen.clear()
        monkeypatch.setattr(desktop.sys, "platform", platform)
        desktop._open_shell_window("http://127.0.0.1:5181/", "links", str(tmp_path / "profiles"),
                                   size=(1000, 700), name="Links Hoard", icon=None)
        line = _relaunch_arg(seen["cmd"])
        assert (line is not None) is expected
        if expected:
            assert line.endswith('relaunch.py" --app "links"')


def test_relaunch_script_asks_the_hub_to_open_the_app(monkeypatch):
    from hoard_link.hub import __main__ as hub_main, procs, relaunch

    calls = []
    monkeypatch.setattr(hub_main, "_already_running", lambda url: True)
    monkeypatch.setattr(procs, "fetch_json_post", lambda url, body, **kw: calls.append(url) or (200, {"ok": True}))
    monkeypatch.setattr(relaunch.os, "chdir", lambda p: None)
    assert relaunch.main(["--app", "links"]) == 0
    assert calls and calls[0].endswith("/api/apps/links/open")
    monkeypatch.setattr(hub_main, "main", lambda argv=None: 7)
    assert relaunch.main([]) == 7                # no app: the Hub's own start-or-open path
