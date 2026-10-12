"""install_copilot: one call gives a FastAPI Hoard the relay, the launcher and the launcher injected into its pages."""
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from hoard_link.copilot import LAUNCHER_PATH, inject_launcher, install_copilot
from hoard_link.copilot_launcher import LAUNCHER_JS

PAGE = "<!doctype html><html lang='es'><head><title>App</title></head><body><div id='root'></div></body></html>"


@pytest.fixture
def faustus():
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"contract_version":"1.0"}' if self.path.endswith("/capabilities") else b"export const ok = 1;"
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path.endswith("/capabilities") else "application/javascript")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _app(tmp_path, url, **kw):
    app = FastAPI()

    @app.get("/api/things")
    def things():
        return JSONResponse({"ok": True})

    @app.get("/{path:path}")  # the SPA catch-all most Hoards end with
    def spa(path: str):
        return HTMLResponse(PAGE)

    install_copilot(app, "argus", tmp_path, app_name="Argus's Hoard", faustus_urls=[url], token="", **kw)
    return app


def test_relay_and_launcher_win_over_the_spa_catch_all(tmp_path, faustus):
    with TestClient(_app(tmp_path, faustus)) as client:
        info = client.get("/api/copilot/info").json()
        assert info["app_id"] == "argus" and info["app_name"] == "Argus's Hoard" and info["data_space_id"]
        launcher = client.get(LAUNCHER_PATH)
        assert launcher.status_code == 200 and launcher.headers["content-type"].startswith("application/javascript")
        assert "faustus-chat" in launcher.text and launcher.text == LAUNCHER_JS
        assert client.get("/api/copilot/panel.js").text == "export const ok = 1;"


def test_pages_get_the_launcher_once_and_apis_are_untouched(tmp_path, faustus):
    with TestClient(_app(tmp_path, faustus)) as client:
        page = client.get("/watchers/42")
        assert page.text.count(LAUNCHER_PATH) == 1
        assert page.text.index(LAUNCHER_PATH) < page.text.index("</body>")
        assert int(page.headers["content-length"]) == len(page.content)
        assert client.get("/api/things").json() == {"ok": True}


def test_inject_off_leaves_pages_alone(tmp_path, faustus):
    with TestClient(_app(tmp_path, faustus, inject=False)) as client:
        assert LAUNCHER_PATH not in client.get("/").text
        assert client.get(LAUNCHER_PATH).status_code == 200


def test_inject_launcher_is_idempotent_and_handles_pages_without_body():
    once = inject_launcher(PAGE.encode())
    assert inject_launcher(once) == once
    assert inject_launcher(b"<p>fragment</p>").endswith(b'launcher.js"></script>')


def test_identity_survives_restarts(tmp_path, faustus):
    with TestClient(_app(tmp_path, faustus)) as client:
        first = client.get("/api/copilot/info").json()["data_space_id"]
    with TestClient(_app(tmp_path, faustus)) as client:
        assert client.get("/api/copilot/info").json()["data_space_id"] == first


def test_unreachable_faustus_is_a_clear_503(tmp_path):
    with TestClient(_app(tmp_path, "http://127.0.0.1:9")) as client:
        response = client.get("/api/copilot/info")
        assert response.status_code == 503 and "unavailable" in response.text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_launcher_guesses_open_objects_from_the_route(tmp_path):
    script = tmp_path / "probe.mjs"
    script.write_text(
        "globalThis.window = globalThis; globalThis.document = {readyState: 'loading', addEventListener() {}};\n"
        + LAUNCHER_JS + "\n"
        "const g = window.hoardCopilotGuessObjects;\n"
        "console.log(JSON.stringify([g('/p/proj_01M4PME5M12QYJ9E89PBXDTVM2/overview'), g('/watchers/new'),"
        " g('/targets/17/edit'), g('/decks/01M4P6TT5H5PJY96F82A11GBGX/slides/3'), g('/settings')]));\n",
        encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    project, new, target, slides, settings = json.loads(out.stdout)
    assert project == [{"kind": "p", "object_id": "proj_01M4PME5M12QYJ9E89PBXDTVM2"}]
    assert new == [] and settings == []                                   # «new» is never an object
    assert target == [{"kind": "target", "object_id": "17"}]
    assert slides == [{"kind": "deck", "object_id": "01M4P6TT5H5PJY96F82A11GBGX"}, {"kind": "slide", "object_id": "3"}]


def _spa_app(tmp_path, monkeypatch, url, *, own_relay=False, copilot=None, configure=True):
    from hoard_link import family
    from hoard_link.copilot import make_copilot_router
    from hoard_link.service import install_spa

    monkeypatch.setenv("HOARD_FAUSTUS_URL", url)
    monkeypatch.delenv("HOARD_COPILOT", raising=False)
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text(PAGE, encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    family.configure("argus" if configure else "", str(data), token_file=str(data / "mcp-token"))
    app = FastAPI(title="Argus's Hoard")
    if own_relay:
        app.include_router(make_copilot_router("argus", data, faustus_urls=[url], token=""))
    install_spa(app, static, copilot=copilot)
    return app


def test_install_spa_gives_family_apps_the_copilot_by_itself(tmp_path, monkeypatch, faustus):
    with TestClient(_spa_app(tmp_path, monkeypatch, faustus)) as client:
        assert LAUNCHER_PATH in client.get("/inventory/7").text
        info = client.get("/api/copilot/info").json()
        assert info["app_id"] == "argus" and info["app_name"] == "Argus's Hoard"
        assert (tmp_path / "data" / "copilot-space-id").read_text(encoding="utf-8").strip() == info["data_space_id"]
        assert client.get("/api/nothing").status_code == 404                 # the API 404 of the SPA still answers


def test_install_spa_respects_own_relay_opt_out_and_unconfigured_apps(tmp_path, monkeypatch, faustus):
    with TestClient(_spa_app(tmp_path, monkeypatch, faustus, own_relay=True)) as client:
        assert LAUNCHER_PATH not in client.get("/").text                     # Cicero/Tantalus keep their component
    with TestClient(_spa_app(tmp_path, monkeypatch, faustus, copilot=False)) as client:
        assert LAUNCHER_PATH not in client.get("/").text and client.get("/api/copilot/info").status_code == 404
    monkeypatch.setenv("HOARD_COPILOT", "0")
    from hoard_link.service import install_spa
    app = FastAPI()
    install_spa(app, tmp_path / "static")
    with TestClient(app) as client:
        assert LAUNCHER_PATH not in client.get("/").text
    monkeypatch.delenv("HOARD_COPILOT")
    with TestClient(_spa_app(tmp_path, monkeypatch, faustus, configure=False)) as client:
        assert client.get("/api/copilot/info").status_code == 404
    with pytest.raises(RuntimeError):
        _spa_app(tmp_path, monkeypatch, faustus, configure=False, copilot=True)


STUB_PANEL = """
class FakeChat extends HTMLElement {
  connectedCallback() { this.innerHTML = '<textarea aria-label="Mensaje"></textarea>'; }
  loadHistory() { return Promise.resolve(); }
}
if (!customElements.get('faustus-chat')) customElements.define('faustus-chat', FakeChat);
"""


def test_launcher_opens_closes_and_keeps_keys_in_a_real_browser():
    """The panel really hides when closed (a display rule must not beat [hidden]), reopens, and keys typed in it never
    reach the app's own shortcuts. Runs in Chromium through Playwright; skipped where Playwright is not installed."""
    sync_api = pytest.importorskip("playwright.sync_api")
    info = {"app_id": "demo", "app_name": "Demo Hoard", "data_space_id": "space-1", "panel_url": "/api/copilot/panel.js",
            "server": "/", "tool_server_id": "demo", "settings_url": "http://127.0.0.1:7000/studio/settings"}
    page_html = inject_launcher(PAGE.encode()).decode()
    routes = {
        "http://hoard.test/": ("text/html", page_html),
        "http://hoard.test" + LAUNCHER_PATH: ("application/javascript", LAUNCHER_JS),
        "http://hoard.test/api/copilot/info": ("application/json", json.dumps(info)),
        "http://hoard.test/api/copilot/panel.js": ("application/javascript", STUB_PANEL),
    }
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browsers not downloaded on this machine
            pytest.skip(f"no Chromium for Playwright: {exc}")
        try:
            page = browser.new_page()
            page.route("http://hoard.test/**", lambda route: route.fulfill(
                status=200 if route.request.url in routes else 404,
                content_type=routes.get(route.request.url, ("text/plain", ""))[0],
                body=routes.get(route.request.url, ("", "not found"))[1]))
            page.goto("http://hoard.test/")
            page.evaluate("window.__appKeys = 0; document.addEventListener('keydown', () => { window.__appKeys += 1; })")
            # An app whose own controls sit bottom right moves the button with CSS variables or to the left.
            assert page.evaluate("getComputedStyle(document.querySelector('.hoard-copilot-toggle')).bottom") == "18px"
            page.evaluate("document.documentElement.style.setProperty('--hoard-copilot-bottom', '90px')")
            assert page.evaluate("getComputedStyle(document.querySelector('.hoard-copilot-toggle')).bottom") == "90px"
            page.evaluate("document.documentElement.dataset.hoardCopilotPosition = 'left'")
            assert page.evaluate("getComputedStyle(document.querySelector('.hoard-copilot-toggle')).left") == "18px"
            page.evaluate("document.documentElement.removeAttribute('data-hoard-copilot-position');"
                          "document.documentElement.style.removeProperty('--hoard-copilot-bottom')")
            toggle = page.locator(".hoard-copilot-toggle")
            toggle.click()
            panel = page.locator("aside.hoard-copilot")
            panel.wait_for(state="visible")
            page.locator("faustus-chat textarea").wait_for(state="visible")
            assert page.locator("aside.hoard-copilot header strong").inner_text() == "Faustus · Demo Hoard"
            page.locator("faustus-chat textarea").press("k")
            assert page.evaluate("window.__appKeys") == 0, "a key typed in the panel reached the app"
            page.get_by_role("button", name="Cerrar Faustus").click()
            panel.wait_for(state="hidden")
            assert not panel.is_visible() and toggle.get_attribute("aria-expanded") == "false"
            toggle.click()
            panel.wait_for(state="visible")
            toggle.click()                                   # the button closes it too
            panel.wait_for(state="hidden")
            assert page.locator("aside.hoard-copilot").count() == 1, "reopening reuses the same panel"
        finally:
            browser.close()


PROJECT_PAGE = """<!doctype html><html lang='es'><head><title>Projects</title></head><body><h1>Proyecto</h1>
<script>
window.__project = null;
window.hoardCopilot = { context: async () => {
  const fromHash = (location.hash.match(/#\\/p\\/(\\w+)/) || [])[1];
  const id = window.__project || fromHash;
  return id ? { project: { external_project_id: 'proj-' + id, title: 'Project ' + id } } : { project: null };
} };
</script></body></html>"""

RECORDING_PANEL = """
let next = 0;
class RecordingChat extends HTMLElement {
  constructor() { super(); this._session = null; this.history = 0; }
  get session() { return this._session; }
  set session(v) { this._session = v || null; }
  connectedCallback() {
    // A panel without a conversation makes one, the way <faustus-chat> does on its first send.
    if (!this._session) {
      this._session = 'sid-' + (++next);
      this.dispatchEvent(new CustomEvent('faustus:session-change', { detail: { sessionId: this._session } }));
    }
  }
  loadHistory() { this.history += 1; return Promise.resolve(); }
}
if (!customElements.get('faustus-chat')) customElements.define('faustus-chat', RecordingChat);
"""


def test_a_different_project_is_a_different_conversation_in_a_real_browser():
    """Moving from project A to B in the app shows B's conversation (never A's history under B), coming back shows A's
    again, and a project change nothing announced still routes the next turn to that project's conversation."""
    sync_api = pytest.importorskip("playwright.sync_api")
    info = {"app_id": "demo", "app_name": "Demo Hoard", "data_space_id": "space-p", "panel_url": "/api/copilot/panel.js",
            "server": "/", "tool_server_id": "demo", "settings_url": "http://127.0.0.1:7000/studio/settings"}
    routes = {
        "http://hoard.test/": ("text/html", inject_launcher(PROJECT_PAGE.encode()).decode()),
        "http://hoard.test" + LAUNCHER_PATH: ("application/javascript", LAUNCHER_JS),
        "http://hoard.test/api/copilot/info": ("application/json", json.dumps(info)),
        "http://hoard.test/api/copilot/panel.js": ("application/javascript", RECORDING_PANEL),
    }

    def fulfill(route):
        url = route.request.url.split("#")[0]
        if url == "http://hoard.test/api/copilot/proxy":    # saved conversations still exist upstream
            return route.fulfill(status=200, content_type="application/json", body='{"messages": []}')
        kind, body = routes.get(url, ("text/plain", "not found"))
        route.fulfill(status=200 if url in routes else 404, content_type=kind, body=body)

    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {exc}")
        try:
            page = browser.new_page()
            page.route("http://hoard.test/**", fulfill)
            page.goto("http://hoard.test/#/p/A")
            page.locator(".hoard-copilot-toggle").click()
            session = lambda: page.evaluate("document.querySelector('faustus-chat') && document.querySelector('faustus-chat').session")
            page.wait_for_function("document.querySelector('faustus-chat') && document.querySelector('faustus-chat').session")
            first = session()
            assert page.evaluate("localStorage.getItem('hoard-copilot:space-p:project:proj-A')") == first
            page.evaluate("location.hash = '#/p/B'")
            page.wait_for_function(f"document.querySelector('faustus-chat').session && document.querySelector('faustus-chat').session !== {json.dumps(first)}")
            second = session()
            assert second != first
            assert page.evaluate("localStorage.getItem('hoard-copilot:space-p:project:proj-B')") == second
            page.evaluate("history.pushState({}, '', '/#/p/A')")
            page.wait_for_function(f"document.querySelector('faustus-chat').session === {json.dumps(first)}")
            assert page.evaluate("document.querySelector('faustus-chat').history") >= 1, "A's history is loaded again"
            assert page.locator("faustus-chat").count() == 1
            # No navigation and no event: the next turn's context still moves it to project C's (new) conversation.
            page.evaluate("window.__project = 'C'")
            page.evaluate("document.querySelector('faustus-chat').contextProvider()")
            assert session() is None, "project C has no conversation yet: the panel makes a fresh one"
            # A view without a project keeps the data space's own conversation and never sends project: null.
            ctx = page.evaluate("window.__project = null; history.replaceState({}, '', '/#/home'); document.querySelector('faustus-chat').contextProvider()")
            assert "project" not in ctx and ctx["view"]["route"] == "/home"
        finally:
            browser.close()
