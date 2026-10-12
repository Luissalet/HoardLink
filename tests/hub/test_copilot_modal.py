import json

import pytest

from hoard_link.copilot_launcher import LAUNCHER_JS


def test_native_dialog_keeps_the_same_interactive_chat_and_draft():
    playwright = pytest.importorskip('playwright.sync_api')
    page_html = '''<!doctype html><html lang="es"><meta charset="utf-8"><body><button id="edit">Editar</button>
    <dialog id="edit-dialog" style="transform:translateX(5px);overflow:hidden">
    <form method="dialog"><label>Nombre<input id="name" value="Inicial"></label><button>Cerrar editor</button></form>
    <button id="nested">Más opciones</button></dialog><dialog id="nested-dialog"><button id="close-nested">Volver</button></dialog>
    <script>document.querySelector('#edit').onclick=()=>document.querySelector('#edit-dialog').showModal();
    document.querySelector('#nested').onclick=()=>document.querySelector('#nested-dialog').showModal();
    document.querySelector('#close-nested').onclick=()=>document.querySelector('#nested-dialog').close();</script>
    <script src="/api/copilot/launcher.js"></script></body></html>'''
    widget = '''class Chat extends HTMLElement {
      constructor(){super();this.attachShadow({mode:'open'});this.session='sid-modal';this.connections=0;this.moves=0;
        this.shadowRoot.innerHTML='<textarea aria-label="Mensaje"></textarea><button>Enviar</button>';}
      connectedCallback(){this.connections++;this.session='sid-modal';queueMicrotask(()=>this.dispatchEvent(new CustomEvent('faustus:session-change',{detail:{sessionId:this.session}})));}
      connectedMoveCallback(){this.moves++;}
      loadHistory(){return Promise.resolve();}
    } customElements.define('faustus-chat',Chat);'''
    info = {'app_id': 'demo', 'app_name': 'Demo', 'data_space_id': 'modal-qa',
            'panel_url': '/api/copilot/panel.js', 'tool_server_id': 'demo'}
    routes = {'/': ('text/html', page_html), '/api/copilot/launcher.js': ('text/javascript', LAUNCHER_JS),
              '/api/copilot/panel.js': ('text/javascript', widget), '/api/copilot/info': ('application/json', json.dumps(info))}
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.route('http://modal.test/**', lambda route: route.fulfill(status=200,
                content_type=routes.get(route.request.url.replace('http://modal.test', ''), ('application/json', '{}'))[0],
                body=routes.get(route.request.url.replace('http://modal.test', ''), ('application/json', '{}'))[1]))
            page.goto('http://modal.test/')
            page.get_by_role('button', name='Faustus', exact=True).click()
            composer = page.get_by_role('textbox', name='Mensaje')
            composer.fill('Borrador antes de editar')
            page.evaluate("window.originalChat=document.querySelector('faustus-chat')")
            page.get_by_role('button', name='Editar', exact=True).click()
            page.get_by_role('button', name='Faustus', exact=True).click()
            assert not page.locator('.hoard-copilot').is_visible()
            page.get_by_role('button', name='Faustus', exact=True).click()
            assert composer.input_value() == 'Borrador antes de editar'
            composer.fill('Borrador dentro del editor')
            composer.press('Escape')
            assert page.locator('#edit-dialog').evaluate('(dialog)=>dialog.open')
            assert page.evaluate("document.querySelector('faustus-chat')===window.originalChat")
            assert page.locator('.hoard-copilot-layer').evaluate('(root)=>root.parentElement.id') == 'edit-dialog'
            page.get_by_role('button', name='Más opciones', exact=True).click()
            composer.fill('Borrador anidado')
            assert page.locator('.hoard-copilot-layer').evaluate('(root)=>root.parentElement.id') == 'nested-dialog'
            page.get_by_role('button', name='Volver', exact=True).click()
            assert composer.input_value() == 'Borrador anidado'
            page.get_by_role('button', name='Cerrar editor', exact=True).click()
            assert page.locator('.hoard-copilot-layer').evaluate('(root)=>root.parentElement.tagName') == 'BODY'
            assert composer.input_value() == 'Borrador anidado'
            assert page.evaluate("document.querySelector('faustus-chat')===window.originalChat")
            assert page.locator('faustus-chat').evaluate('(chat)=>chat.session') == 'sid-modal'
            assert page.locator('faustus-chat').evaluate('(chat)=>chat.connections') == 1
            assert page.locator('faustus-chat').evaluate('(chat)=>chat.moves') >= 4
        finally:
            browser.close()
