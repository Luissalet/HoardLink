"""The Hoard Window shell route of desktop.open_window."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hoard_link.hub import __main__ as hub_main, desktop, ui_session
from hoard_link.hub.config import HubConfig

from ._hub_fakes import free_port, http, make_hub, serve, wait_for

SHELL_MAIN = Path(__file__).resolve().parents[2] / "shell" / "main.cjs"
PREVIOUS_SHELL = "fcfe52c63ac0c95a5b4538a49b8d71fbfc771a2e"


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


def test_hub_launcher_only_fetches_a_ticket_for_the_browser_fallback(tmp_path, monkeypatch):
    cfg = HubConfig(data_dir=str(tmp_path))
    monkeypatch.setattr(desktop, "window_engine", lambda _: "shell")
    shell_urls, fallback_urls, issued = [], [], []
    shell_result = {"ok": True, "mode": "app-window", "pid": 42}
    monkeypatch.setattr(desktop, "_open_shell_window", lambda url, *a, **kw: shell_urls.append(url) or shell_result)
    monkeypatch.setattr(desktop, "find_browser", lambda _: None)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: fallback_urls.append(url) or True)
    ticketed = cfg.url + "/#hub-session=test"
    monkeypatch.setattr(ui_session, "launch_url", lambda *args: issued.append(args) or ticketed)

    assert hub_main._open_own_window(cfg, "window") == 42
    assert shell_urls == [cfg.url] and not issued
    shell_result["ok"] = False
    assert hub_main._open_own_window(cfg, "window") is None
    assert fallback_urls == [ticketed] and issued == [(cfg.url, cfg.data_dir)]


def _native_dependencies():
    if sys.platform.startswith("linux") and not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        pytest.skip("Electron needs a window system even for a hidden native window")
    exe = os.environ.get("HOARD_WINDOW_TEST_ELECTRON")
    if not exe:
        installed = desktop.find_shell()
        if not installed:
            pytest.skip("Install the window shell or set HOARD_WINDOW_TEST_ELECTRON to run the native test")
        exe = installed[0]
    return exe, pytest.importorskip("playwright.sync_api")


def _hidden_shell_command(exe, bootstrap, shell_main, debug_port, url, profile):
    bootstrap.mkdir()
    (bootstrap / "package.json").write_text('{"main":"start.cjs"}', encoding="utf-8")
    (bootstrap / "start.cjs").write_text(f"""
const {{ app, BaseWindow }} = require('electron');
app.commandLine.appendSwitch('remote-debugging-port', '{debug_port}');
app.commandLine.appendSwitch('lang', 'en-GB');
app.on('second-instance', () => console.log('Second launch reached the existing process'));
BaseWindow.prototype.show = function () {{}};
BaseWindow.prototype.focus = function () {{}};
if (process.argv.includes('--test-window-close')) {{
  const fs = require('node:fs');
  const signal = require('node:path').join(__dirname, 'close');
  const timer = setInterval(() => {{
    if (!fs.existsSync(signal)) return;
    clearInterval(timer);
    for (const window of BaseWindow.getAllWindows()) window.close();
  }}, 50);
}}
require({json.dumps(str(shell_main))});
""", encoding="utf-8")
    return [exe, str(bootstrap), f"--hoard-url={url}", "--hoard-id=_hub", f"--user-data-dir={profile}"]


def _shell_context(pw, debug_port, log_path):
    debug_url = f"http://127.0.0.1:{debug_port}"

    def shell_listening():
        try:
            return http(debug_url + "/json/version")[0] == 200
        except OSError:
            return False

    assert wait_for(shell_listening, timeout=15), log_path.read_text(errors="replace")
    return pw.chromium.connect_over_cdp(debug_url)


def _shell_page(context, url):
    page = next((tab for tab in context.pages if tab.url.startswith(url)), None)
    if page is None:
        page = context.wait_for_event("page", predicate=lambda tab: not tab.url.startswith("file:"))
    page.set_default_timeout(10_000)
    return page


@pytest.mark.parametrize(("lang", "keep_label", "restore_label"), [
    ("en", "Keep", "Restore session"),
    ("es", "Conservar", "Recuperar sesión"),
])
def test_hub_shell_without_ticket_can_reload_and_recover_a_write_session(
    tmp_path, monkeypatch, lang, keep_label, restore_label,
):
    exe, playwright = _native_dependencies()
    monkeypatch.setenv("AGORA_SPACE", "ecosystem")
    hub = make_hub(tmp_path)
    hub.config.language = "en"
    ag = hub.facet("agora").agora
    ag.mention_hook = None
    ag.escalate_hook = None
    agent = {"agent": "shell-qa"}
    thread = ag.thread_open({**agent, "title": "Shell conversation", "body": "Ready for QA"})["thread"]
    order = ag.order_create({**agent, "title": "Shell order", "folder": str(tmp_path)})["order"]

    def open_gate(filename, title):
        (tmp_path / filename).write_text(title, encoding="utf-8")
        piece = ag.piece_add({**agent, "order_id": order["id"], "title": title, "kind": "text",
                              "path": filename})["piece"]
        return ag.gate_open({**agent, "order_id": order["id"], "title": title,
                             "piece_ids": [piece["id"]]})["gate"]

    first_gate = open_gate("first.txt", "Review after reload")
    gate = open_gate("second.txt", "Review the QA document")

    debug_port = free_port()
    url = hub.config.url
    thread_url = url + f"/#agora-{thread['id']}"
    profile = Path(hub.config.profiles_dir) / "_hub"
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    cmd = _hidden_shell_command(exe, tmp_path / "hidden-shell", SHELL_MAIN, debug_port, thread_url, profile)
    server = serve(hub)
    process = None
    try:
        token_path = Path(hub.config.data_dir) / ui_session.LAUNCH_FILE
        token = token_path.read_bytes()
        token_path.unlink()
        with (tmp_path / "shell.log").open("wb") as log:
            process = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)

        with playwright.sync_playwright() as pw:
            browser = _shell_context(pw, debug_port, tmp_path / "shell.log")
            context = browser.contexts[0]
            page = _shell_page(context, url)
            page.add_init_script("if (location.protocol === 'http:' && !localStorage.getItem('hub.lang')) { "
                                 "localStorage.setItem('hub.lang', 'en'); }")
            page.goto(thread_url)
            form = page.locator(".ag-detail .ag-compose")
            form.locator("textarea").fill("Reply from the shell")
            send = form.get_by_role("button", name="Send", exact=True)
            with page.expect_response(lambda result: result.url.endswith("/api/agora/post")) as refused:
                send.click()
            assert refused.value.status == 403

            token_path.write_bytes(token)
            bar = next(tab for tab in context.pages if tab.url.startswith("file:"))
            with page.expect_response(lambda result: result.url.endswith(ui_session.SESSION_PATH)) as session:
                bar.evaluate("hoardBar.reload()")
            assert session.value.status == 200
            form.locator("textarea").fill("Reply after reload")
            with page.expect_response(lambda result: result.url.endswith("/api/agora/post")) as posted:
                send.click()
            assert posted.value.status == 200
            messages = http(url + f"/api/agora/threads/{thread['id']}")[1]["messages"]
            assert [message["body"] for message in messages] == ["Ready for QA", "Reply after reload"]
            assert messages[-1]["author"] == ag.person_id

            page.get_by_role("link", name="Your pending decisions").click()
            first_row = page.locator(".hub-decision-row").filter(has_text=first_gate["title"])
            with page.expect_response(lambda result: result.url.endswith("/api/agora/production_decide")) as decided:
                first_row.get_by_role("button", name="Keep", exact=True).click()
            assert decided.value.status == 200
            assert decided.value.json()["gate"]["status"] == "decided"

            token_path.unlink()
            page.evaluate("lang => { localStorage.setItem('hub.lang', lang); "
                          "localStorage.setItem('hub.ui.session', 'stale'); }", lang)
            page.goto(url + "/?space_id=ecosystem#hub/pending_decisions")
            page.reload()
            row = page.locator(".hub-decision-row").filter(has_text=gate["title"])
            note = row.locator("textarea")
            note.fill("Keep this note")
            keep = row.get_by_role("button", name=keep_label, exact=True)
            with page.expect_response(lambda result: result.url.endswith("/api/agora/production_decide")) as refused:
                keep.click()
            assert refused.value.status == 403
            restore = page.get_by_role("button", name=restore_label, exact=True)
            playwright.expect(restore).to_be_visible()
            page.screenshot(path=str(tmp_path / "restore-session.png"))
            token_path.write_bytes(token)
            restore.click()
            playwright.expect(restore).to_have_count(0)
            playwright.expect(note).to_have_value("Keep this note")
            assert http(url + "/api/agora/pending_decisions")[1]["rows"][0]["key"] == f"gate-{gate['id']}"
            with page.expect_response(lambda result: result.url.endswith("/api/agora/production_decide")) as decided:
                keep.click()
            assert decided.value.status == 200
            recorded = http(url + "/api/agora/decisions")[1]["production_decisions"]
            assert len(recorded) == 2 and all(row["decided_by"]["id"] == ag.person_id for row in recorded)
            assert any(row["note"] == "Keep this note" for row in recorded)

            # The next local launch reuses this window and renews the session in place.
            page.evaluate("localStorage.setItem('hub.ui.session', 'stale')")
            subprocess.run(cmd, env=env, capture_output=True, timeout=10, check=True)
            assert wait_for(lambda: "Second launch reached" in (tmp_path / "shell.log").read_text(errors="replace"))
            page.wait_for_function("localStorage.getItem('hub.ui.session') && "
                                   "localStorage.getItem('hub.ui.session') !== 'stale'")
            assert page.url.endswith("#hub/pending_decisions")

            with page.expect_response(lambda result: result.url.endswith(ui_session.SESSION_PATH)) as session:
                bar.evaluate("hoardBar.home()")
            assert session.value.status == 200 and page.url == thread_url
            browser.close()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        hub.close()


def _previous_shell(tmp_path):
    folder = tmp_path / "previous-shell"
    folder.mkdir()
    for filename in ("main.cjs", "preload-app.cjs", "preload-bar.cjs", "bar.html", "bar.js"):
        content = subprocess.run(
            ["git", "show", f"{PREVIOUS_SHELL}:shell/{filename}"],
            cwd=SHELL_MAIN.parents[1], capture_output=True, timeout=10,
        )
        if content.returncode:
            pytest.skip(f"The native upgrade test needs shell files from {PREVIOUS_SHELL}")
        (folder / filename).write_bytes(content.stdout)
    return folder / "main.cjs"


@contextmanager
def _hidden_shell(pw, exe, bootstrap, main, url, profile):
    debug_port = free_port()
    cmd = _hidden_shell_command(exe, bootstrap, main, debug_port, url, profile)
    env = {key: value for key, value in os.environ.items() if key != "ELECTRON_RUN_AS_NODE"}
    log_path = bootstrap / "shell.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([*cmd, "--test-window-close"], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        browser = _shell_context(pw, debug_port, log_path)
        yield browser
        browser.close()
        (bootstrap / "close").touch()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_previous_hub_shell_can_read_and_reopening_restores_writes(tmp_path, monkeypatch):
    exe, playwright = _native_dependencies()
    prior_main = _previous_shell(tmp_path)
    monkeypatch.setenv("AGORA_SPACE", "ecosystem")
    hub = make_hub(tmp_path)
    hub.config.language = "en"
    ag = hub.facet("agora").agora
    ag.mention_hook = None
    ag.escalate_hook = None
    agent = {"agent": "shell-upgrade-qa"}
    thread = ag.thread_open({**agent, "title": "Upgrade conversation", "body": "Available before reopening"})["thread"]
    order = ag.order_create({**agent, "title": "Upgrade order", "folder": str(tmp_path)})["order"]
    (tmp_path / "piece.txt").write_text("A piece for the upgrade test", encoding="utf-8")
    piece = ag.piece_add({**agent, "order_id": order["id"], "title": "Upgrade piece", "kind": "text",
                         "path": "piece.txt"})["piece"]
    gate = ag.gate_open({**agent, "order_id": order["id"], "title": "Upgrade decision",
                        "piece_ids": [piece["id"]]})["gate"]
    decision_selector = f".hub-decision-row[data-decision='gate-{gate['id']}']:visible"

    url = hub.config.url
    thread_url = f"{url}/#agora-{thread['id']}"
    profile = Path(hub.config.profiles_dir) / "_hub"
    server = serve(hub)
    try:
        with playwright.sync_playwright() as pw:
            with _hidden_shell(pw, exe, tmp_path / "old", prior_main, thread_url, profile) as browser:
                context = browser.contexts[0]
                page = _shell_page(context, url)
                bar = next(tab for tab in context.pages if tab.url.startswith("file:"))
                with page.expect_navigation():
                    bar.evaluate("hoardBar.reload()")
                assert page.evaluate("window.hoardWindow.shell") == "hoard-window"
                assert page.evaluate("typeof window.hoardHubSession") == "undefined"
                notice = page.locator(".hub-session-recovery")
                playwright.expect(notice).to_have_text(
                    "This window is from an earlier version. Close it and reopen the Hub.")
                playwright.expect(page.get_by_text("Available before reopening", exact=True)).to_be_visible()
                assert notice.evaluate("(element) => getComputedStyle(element.parentElement).position") == "sticky"
                heading = page.locator("#hub-page-title").bounding_box()
                box = notice.bounding_box()
                assert box["y"] + box["height"] <= heading["y"]
                page.screenshot(path=str(tmp_path / "previous-window-en.png"))
                page.locator("#btn-lang").click()
                playwright.expect(notice).to_have_text(
                    "Esta ventana es de una versión anterior. Ciérrala y vuelve a abrir el Hub.")
                page.get_by_role("link", name="Tus decisiones pendientes").click()
                playwright.expect(page.locator(decision_selector)).to_be_visible()
                page.screenshot(path=str(tmp_path / "previous-window-es.png"))

            with _hidden_shell(pw, exe, tmp_path / "current", SHELL_MAIN, thread_url, profile) as browser:
                page = _shell_page(browser.contexts[0], url)
                page.wait_for_function("localStorage.getItem('hub.ui.session')")
                playwright.expect(page.locator(".hub-session-recovery")).to_have_count(0)
                form = page.locator(".ag-detail .ag-compose")
                form.locator("textarea").fill("After reopening")
                with page.expect_response(lambda result: result.url.endswith("/api/agora/post")) as posted:
                    form.get_by_role("button", name="Enviar", exact=True).click()
                assert posted.value.status == 200
                messages = http(url + f"/api/agora/threads/{thread['id']}")[1]["messages"]
                assert messages[-1]["body"] == "After reopening" and messages[-1]["author"] == ag.person_id
                page.get_by_role("link", name="Tus decisiones pendientes").click()
                row = page.locator(decision_selector)
                with page.expect_response(lambda result: result.url.endswith("/api/agora/production_decide")) as decided:
                    row.get_by_role("button", name="Conservar", exact=True).click()
                assert decided.value.status == 200
                assert decided.value.json()["gate"]["status"] == "decided"
                record = http(url + "/api/agora/decisions")[1]["production_decisions"][0]
                assert record["decided_by"]["id"] == ag.person_id
    finally:
        server.shutdown()
        server.server_close()
        hub.close()


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
