"""The launcher in apps with strict defences: a CSP without inline styles, a session guard and an anti-CSRF header."""
import json

import pytest

from hoard_link.copilot import COPILOT_PUBLIC_PATHS, COPILOT_REQUEST_HEADER, LAUNCHER_PATH
from hoard_link.copilot_launcher import LAUNCHER_JS

CSP = "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; font-src 'self'; base-uri 'none'"
WIDGET = '''class Chat extends HTMLElement {
  constructor(){super();this.attachShadow({mode:'open'});this.shadowRoot.innerHTML='<textarea aria-label="Mensaje"></textarea>';}
  async loadHistory(){const r=await this.fetch(location.origin+'/api/hoard-copilot/conversations/x',{method:'POST',
    headers:{'Content-Type':'application/json'},body:'{}'});this.relayStatus=r.status;}
} customElements.define('faustus-chat',Chat);'''
INFO = {'app_id': 'demo', 'app_name': 'Demo', 'data_space_id': 'csp-qa', 'panel_url': '/api/copilot/panel.js',
        'tool_server_id': 'demo'}


def _page(head='', bar=True):
    header = ('<header id="bar" class="top"><span>Demo</span><button id="save">Guardar</button></header>' if bar else '')
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8">{head}<link rel="stylesheet" href="/app.css">'
            f'</head><body>{header}<main><h1>Inicio</h1></main>'
            '<script type="module" src="/api/copilot/launcher.js"></script></body></html>')


APP_CSS = ('body{margin:0;font:14px system-ui}.top{position:sticky;top:0;display:flex;justify-content:space-between;'
           'align-items:center;height:64px;padding:0 16px;background:#223}')


def _serve(page, html, css=APP_CSS):
    seen = {}

    def handle(route):
        request = route.request
        path = request.url.replace('http://strict.test', '')
        headers = {'Content-Security-Policy': CSP}
        sent = request.all_headers()
        guarded = path.startswith('/api/') and path not in COPILOT_PUBLIC_PATHS
        if guarded and sent.get('cookie') != 'session=ok':
            return route.fulfill(status=401, body='{"error":"login"}', headers=headers, content_type='application/json')
        if path in ('/api/copilot/info', '/api/copilot/proxy') and sent.get(COPILOT_REQUEST_HEADER[0].lower()) != COPILOT_REQUEST_HEADER[1]:
            return route.fulfill(status=403, body='{"error":"request_header_required"}', headers=headers,
                                 content_type='application/json')
        seen[path] = seen.get(path, 0) + 1
        bodies = {'/': ('text/html', html), '/app.css': ('text/css', css), LAUNCHER_PATH: ('text/javascript', LAUNCHER_JS),
                  '/api/copilot/panel.js': ('text/javascript', WIDGET), '/api/copilot/info': ('application/json', json.dumps(INFO)),
                  '/api/copilot/proxy': ('application/json', '{"ok":true}')}
        kind, body = bodies.get(path, ('application/json', '{}'))
        route.fulfill(status=200, body=body, headers=headers, content_type=kind)

    page.route('http://strict.test/**', handle)
    return seen


def _browser_page(pw):
    browser = pw.chromium.launch()
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    context.add_cookies([{'name': 'session', 'value': 'ok', 'url': 'http://strict.test/'}])
    return browser, context.new_page()


def test_launcher_works_under_strict_csp_guard_and_csrf_header():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as pw:
        browser, page = _browser_page(pw)
        try:
            violations = []
            page.on('console', lambda msg: 'Content Security Policy' in msg.text and violations.append(msg.text))
            seen = _serve(page, _page())
            page.goto('http://strict.test/')
            toggle = page.get_by_role('button', name='Faustus', exact=True)
            assert toggle.evaluate('(b)=>getComputedStyle(b).width') == '48px'
            toggle.click()
            status = None
            for _ in range(50):   # polling by hand: wait_for_function needs eval, which the CSP refuses
                status = page.evaluate("document.querySelector('faustus-chat')?.relayStatus || null")
                if status:
                    break
                page.wait_for_timeout(100)
            assert status == 200
            assert seen.get('/api/copilot/info') == 1 and seen.get('/api/copilot/proxy') == 1
            top = page.locator('.hoard-copilot').evaluate('(p)=>p.getBoundingClientRect().top')
            assert top >= 64
            assert page.evaluate("(()=>{const b=document.querySelector('#save').getBoundingClientRect();"
                                 "return document.elementFromPoint(b.x+b.width/2,b.y+b.height/2).id})()") == 'save'
            assert violations == []
        finally:
            browser.close()


def test_panel_top_without_a_bar_and_with_the_apps_own_value():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as pw:
        browser, page = _browser_page(pw)
        try:
            _serve(page, _page(bar=False))
            page.goto('http://strict.test/')
            page.get_by_role('button', name='Faustus', exact=True).click()
            assert page.locator('.hoard-copilot').evaluate('(p)=>p.getBoundingClientRect().top') == 14
            page = page.context.new_page()
            _serve(page, _page(), css=APP_CSS + ':root{--hoard-copilot-top:200px}')
            page.goto('http://strict.test/')
            page.get_by_role('button', name='Faustus', exact=True).click()
            assert page.locator('.hoard-copilot').evaluate('(p)=>p.getBoundingClientRect().top') == 200
        finally:
            browser.close()


def test_page_header_below_the_edge_is_avoided_and_escape_closes():
    playwright = pytest.importorskip('playwright.sync_api')
    padded = ('body{margin:0;font:14px system-ui}main{padding:24px}.top{display:flex;justify-content:flex-end;gap:8px;'
              'height:54px;align-items:flex-end}')
    html = ('<!doctype html><html lang="es"><head><meta charset="utf-8"><link rel="stylesheet" href="/app.css"></head>'
            '<body><main><header class="top"><h1>Plazos</h1><button id="add">Añadir plazo</button></header></main>'
            '<script type="module" src="/api/copilot/launcher.js"></script></body></html>')
    with playwright.sync_playwright() as pw:
        browser, page = _browser_page(pw)
        try:
            _serve(page, html, css=padded)
            page.goto('http://strict.test/')
            page.get_by_role('button', name='Faustus', exact=True).click()
            header_bottom = page.locator('header.top').evaluate('(h)=>h.getBoundingClientRect().bottom')
            assert page.locator('.hoard-copilot').evaluate('(p)=>p.getBoundingClientRect().top') >= header_bottom
            assert page.evaluate("(()=>{const b=document.querySelector('#add').getBoundingClientRect();"
                                 "return document.elementFromPoint(b.x+b.width/2,b.y+b.height/2).id})()") == 'add'
            page.get_by_role('textbox', name='Mensaje').press('Escape')
            assert not page.locator('.hoard-copilot').is_visible()
            assert page.get_by_role('button', name='Faustus', exact=True).get_attribute('aria-expanded') == 'false'
        finally:
            browser.close()


def test_public_paths_and_header_contract():
    assert COPILOT_PUBLIC_PATHS == (LAUNCHER_PATH,)
    assert COPILOT_REQUEST_HEADER == ('X-Hoard-Request', '1')
    assert "'X-Hoard-Request': '1'" in LAUNCHER_JS and 'adoptedStyleSheets' in LAUNCHER_JS
