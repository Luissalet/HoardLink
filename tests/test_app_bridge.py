"""Exercise the shared bridge in a real browser, including protocol and matching."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hoard_link.app_bridge import BRIDGE_JS

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = """<!doctype html><html data-hoard-app="fixture"><title>Bridge fixture</title>
<body><h1>Library</h1><p>A readable paragraph</p><div>Plain text in a div</div>
<button onclick="this.dataset.clicked='yes'">Ábrir vídeo</button>
<button>Open first</button><button>Open second</button><button disabled>Unavailable</button>
<label for="query">Búsqueda</label><input id="query" placeholder="Search library">
<select aria-label="Category"><option value="all">All</option><option value="saved">Saved</option></select>
<input type="checkbox" aria-label="Pinned"><button role="tab" aria-selected="false" onclick="this.setAttribute('aria-selected','true')">Details</button>
<button style="display:none">Hidden button</button><button class="hoard-copilot-toggle">Faustus</button>
<script>window.HoardApp={q:[['setContext',{view:'library',summary:'x'.repeat(450)}],['registerTool','custom_tool',{description:'An app tool',inputSchema:{type:'object',properties:{id:{type:'string'}},required:['id']}},async args=>({opened:args.id})]]};</script>
<script src="/bridge.js"></script></body></html>"""

HOST = """<!doctype html><title>Dock host</title><iframe src="/fixture" style="width:100%;height:650px"></iframe>
<script>
window.messages=[];window.pending=new Map();window.counter=0;
addEventListener('message',e=>{messages.push(e.data);if(e.data.type==='dock/result'){pending.get(e.data.id)?.(e.data);pending.delete(e.data.id)}});
window.send=(type,fields={})=>document.querySelector('iframe').contentWindow.postMessage({hoardDock:1,type,...fields},location.origin);
window.call=(tool,args={},actor={id:'one',name:'Agent one',color:'#b7472a'})=>new Promise(resolve=>{const id=String(++counter);pending.set(id,resolve);send('dock/call',{id,tool,args,actor})});
</script>"""


@pytest.fixture(scope="module")
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = {"/": HOST, "/fixture": FIXTURE, "/bridge.js": BRIDGE_JS}.get(self.path, "")
            body = content.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript" if self.path.endswith(".js") else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def page(server):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(reduced_motion="reduce")
        page.goto(server)
        page.frame_locator("iframe").locator("h1").wait_for()
        page.evaluate("send('dock/hello',{host:'faustus',session:'test',theme:'dark'})")
        page.wait_for_function("messages.some(m=>m.type==='dock/ready')")
        yield page
        browser.close()


def call(page, tool, args=None, actor=None):
    return page.evaluate("([tool,args,actor])=>call(tool,args,actor)", [tool, args or {}, actor or {"id": "one", "name": "Agent one"}])


def test_mirrors_are_identical():
    assert (ROOT / "js/hoard-commons/app-bridge.js").read_text(encoding="utf-8") == BRIDGE_JS


def test_handshake_schema_queue_context_and_inert_top_level(page, server):
    ready = page.evaluate("messages.find(m=>m.type==='dock/ready')")
    assert ready["app"] == {"id": "fixture", "name": "Bridge fixture"}
    assert len(ready["tools"]) == 9
    assert ready["context"]["view"] == "library"
    assert len(ready["context"]["summary"]) == 400
    for tool in ready["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]
        json.dumps(tool)
    assert call(page, "custom_tool", {"id": "video-1"})["result"] == {"opened": "video-1"}
    assert call(page, "custom_tool", {"id": 1})["error"]["code"] == "bad_args"
    frame = page.frames[1]
    assert frame.locator(".hoard-copilot-toggle").is_hidden()
    frame.evaluate("HoardApp.setContext(()=>({view:'detail',title:'spoof',url:'spoof'}))")
    page.wait_for_function("messages.some(m=>m.type==='dock/context'&&m.context.view==='detail')")
    ctx = page.evaluate("messages.filter(m=>m.type==='dock/context').at(-1).context")
    assert ctx["title"] == "Bridge fixture" and ctx["url"] == "/fixture"
    page.goto(server + "/fixture")
    page.evaluate("postMessage({hoardDock:1,type:'dock/hello',host:'faustus'},location.origin)")
    assert page.locator(".hoard-copilot-toggle").is_visible()
    assert page.locator("[data-hoard-dock-layer]").count() == 0


def test_matching_refs_errors_and_navigation(page):
    read = call(page, "app_read")["result"]["text"]
    assert "Library" in read and "A readable paragraph" in read
    assert "Plain text in a div" in read
    assert "Hidden button" not in read
    result = call(page, "app_click", {"text": "abrir VIDEO"})
    assert result["ok"] and result["result"]["clicked"] == "Ábrir vídeo"
    assert page.frames[1].locator("button").first.get_attribute("data-clicked") == "yes"
    error = call(page, "app_click", {"text": "open"})["error"]
    assert error["code"] == "ambiguous" and len(error["candidates"]) == 2
    first = error["candidates"][0]["ref"]
    assert call(page, "app_click", {"ref": first})["ok"]
    assert first in call(page, "app_read")["result"]["text"]
    error = call(page, "app_click", {"text": "missing"})["error"]
    assert error["code"] == "not_found" and error["candidates"]
    assert call(page, "app_click", {"text": "Unavailable"})["error"]["code"] == "disabled"
    assert call(page, "app_navigate", {"to": "https://example.org"})["error"]["code"] == "bad_args"
    assert call(page, "app_navigate", {"to": "#detail"})["ok"]
    page.wait_for_timeout(50)
    assert call(page, "app_click", {"ref": first})["error"]["code"] == "not_found"


def test_fill_select_wait_and_concurrent_cursors(page):
    frame = page.frames[1]
    frame.evaluate("window.events=[];document.querySelector('input').addEventListener('input',e=>events.push(e.target.value))")
    assert call(page, "app_fill", {"label": "busqueda", "value": "Visible text"})["ok"]
    assert frame.locator("#query").input_value() == "Visible text"
    assert frame.evaluate("events.at(-1)") == "Visible text"
    assert call(page, "app_select", {"label": "Category", "option": "Saved"})["ok"]
    assert frame.locator("select").input_value() == "saved"
    assert call(page, "app_select", {"label": "Pinned", "option": True})["ok"]
    assert frame.locator("input[type=checkbox]").is_checked()
    assert call(page, "app_select", {"text": "Details", "option": True})["ok"]
    assert frame.locator("[role=tab]").get_attribute("aria-selected") == "true"
    assert call(page, "app_wait", {"text": "Library"})["ok"]
    assert call(page, "app_wait", {"ms": 5001})["error"]["code"] == "bad_args"
    assert call(page, "app_wait", {"text": "never exists", "ms": 5})["error"]["code"] == "timeout"
    results = page.evaluate("Promise.all([call('app_highlight',{text:'Library',note:'Heading'},{id:'one',name:'Agent one',color:'#b7472a'}),call('app_highlight',{text:'Details',note:'Tab'},{id:'two',name:'Agent two',color:'#256a91'})])")
    assert all(result["ok"] for result in results)
    cursors = frame.locator("[data-hoard-dock-layer] .cursor")
    assert cursors.count() == 2
    assert frame.locator("[data-actor=one] .name").inner_text() == "Agent one"
    assert frame.locator("[data-actor=two] .note").inner_text() == "Tab"
    page.evaluate("send('dock/cursor',{actor:{id:'two'},state:'gone'})")
    frame.locator("[data-actor=two]").wait_for(state="detached")


def test_origin_changed_message_and_prehello(server):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(server)
        frame = page.frames[1]
        frame.wait_for_selector("h1")
        page.evaluate("send('dock/call',{id:'early',tool:'app_read'})")
        assert page.evaluate("messages.length") == 0
        assert frame.locator(".hoard-copilot-toggle").is_visible()
        page.evaluate("send('dock/hello',{host:'faustus'})")
        page.wait_for_function("messages.some(m=>m.type==='dock/ready')")
        frame.evaluate("window.changes=[];HoardApp.onChanged(detail=>changes.push(detail));document.addEventListener('hoard:changed',e=>changes.push(e.detail))")
        page.evaluate("send('dock/changed',{server:'links',tool:'save'})")
        page.wait_for_function("document.querySelector('iframe').contentWindow.changes.length===2")
        frame.evaluate("HoardApp.message('Ask about this',true)")
        page.wait_for_function("messages.some(m=>m.type==='dock/message'&&m.send)")
        frame.evaluate("dispatchEvent(new MessageEvent('message',{source:parent,origin:'http://wrong.test',data:{hoardDock:1,type:'dock/theme',theme:'light'}}))")
        assert frame.locator("html").get_attribute("data-theme") != "light"
        browser.close()


def test_long_cursor_annotations_stay_inside_mobile_viewport(page):
    page.set_viewport_size({"width": 320, "height": 600})
    result = call(page, "app_highlight", {"text": "Details", "note": "A longer note for the person. " * 12}, {"id": "long", "name": "Long actor name " * 6})
    assert result["ok"]
    frame = page.frames[1]
    for selector in [".name", ".note"]:
        box = frame.locator("[data-actor=long] " + selector).evaluate("e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:innerWidth,height:innerHeight}}")
        assert 0 <= box["left"] < box["right"] <= box["width"]
        assert 0 <= box["top"] < box["bottom"] <= box["height"]
