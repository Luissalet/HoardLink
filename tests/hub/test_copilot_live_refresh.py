"""The real shared launcher refreshes hosts at a completed tool, before the turn ends."""
import json
import pytest
from hoard_link.copilot_launcher import LAUNCHER_JS

WIDGET = '''customElements.define('faustus-chat',class extends HTMLElement {
 connectedCallback(){if(this.childNodes.length)return;for(const [name,detail,type] of [
 ['Text',{type:'delta',delta:'working'},'faustus:event'],
 ['Tool',{type:'tool_output',call_id:'one',tool:'trainer_update',output:'{"saved":true}'},'faustus:event'],
 ['Finish',{reason:'done'},'faustus:turn-end']]){const b=document.createElement('button');b.textContent=name;
 b.onclick=()=>this.dispatchEvent(new CustomEvent(type,{detail,bubbles:true,composed:true}));this.append(b);}}
 async loadHistory(){}
});'''

@pytest.mark.parametrize('legacy', [False, True])
def test_tool_completion_refreshes_without_finishing_or_remounting(legacy):
 pw = pytest.importorskip('playwright.sync_api')
 with pw.sync_playwright() as p:
  browser=p.chromium.launch(headless=True)
  try:
   page=browser.new_page()
   hook='onTurnEnd' if legacy else 'onChange'
   html=f'''<html><body><h1>Saved view</h1><output id="count">0</output><script>
   window.hoardCopilot={{{hook}(detail){{const n=document.querySelector('#count');n.textContent=Number(n.textContent)+1;}}}};
   </script><script type="module" src="/api/copilot/launcher.js"></script></body></html>'''
   def serve(route):
    path=route.request.url.split('http://live.test')[-1]
    body,kind={
     '/':(html,'text/html'),'/api/copilot/launcher.js':(LAUNCHER_JS,'text/javascript'),
     '/api/copilot/panel.js':(WIDGET,'text/javascript'),
     '/api/copilot/info':(json.dumps({'app_id':'demo','app_name':'Demo','data_space_id':'live','panel_url':'/api/copilot/panel.js'}),'application/json')
    }.get(path,('{}','application/json'))
    route.fulfill(status=200,body=body,content_type=kind)
   page.route('http://live.test/**',serve);page.goto('http://live.test/')
   page.get_by_role('button',name='Faustus',exact=True).click()
   page.get_by_role('button',name='Text',exact=True).click()
   assert page.locator('#count').inner_text()=='0'
   page.get_by_role('button',name='Tool',exact=True).click()
   assert page.locator('#count').inner_text()=='1'
   assert page.locator('faustus-chat').count()==1
   page.get_by_role('button',name='Finish',exact=True).click()
   assert page.locator('#count').inner_text()==('2' if legacy else '1')
  finally: browser.close()
