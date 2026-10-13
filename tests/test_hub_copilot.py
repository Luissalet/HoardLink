"""Hub host relay and profile edits use the shared copilot without owning a chat."""
import base64
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.copilot import CopilotProxy
from hoard_link.hub.server import make_server


@pytest.fixture
def private_host(tmp_path, monkeypatch):
    monkeypatch.setenv('HOARD_HUB_AUTO_RULES','0')
    monkeypatch.setenv('HOARD_AGENT_WATCH_HOME',str(tmp_path/'empty-home'))
    monkeypatch.setenv('HOARD_NOTIFY_DRY_RUN','1')
    received=[]
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type','text/javascript' if self.path.endswith('panel.js') else 'application/json')
            self.end_headers()
            self.wfile.write(b'export const sharedPanel=true;' if self.path.endswith('panel.js') else b'{"contract_version":"1.0"}')
        def do_POST(self):
            raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
            received.append((self.path,raw,self.headers))
            self.send_response(409 if self.path.endswith('/imports') else 200)
            self.send_header('Content-Type','text/event-stream');self.send_header('X-Odysseus-Run-Id','fixture-run')
            self.end_headers();self.wfile.write('data: {"delta":"ámbito"}\n\ndata: [DONE]\n\n'.encode());self.wfile.flush()
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    cfg=HubConfig(port=0,data_dir=str(tmp_path/'data'),roots=[],icon_dirs=[],jobs_enabled=False,
                  faustus_urls=[f'http://127.0.0.1:{upstream.server_port}'],fleet={'enabled':False},agent_watch={'enabled':False})
    hub=Hub(cfg);hub.copilot=CopilotProxy(cfg.faustus_urls,token='fixture-backend-secret')
    server=make_server(hub,port=0);cfg.port=server.server_port
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:yield hub,cfg.url,received,tmp_path
    finally:
        server.shutdown();server.server_close();hub.close();upstream.shutdown();upstream.server_close()


def request(base,path,body=None):
    req=urllib.request.Request(base+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
        headers={'Content-Type':'application/json','Sec-Fetch-Site':'same-origin','Origin':base})
    try:return urllib.request.urlopen(req,timeout=15)
    except urllib.error.HTTPError as response:return response


def test_info_panel_and_common_launcher(private_host):
    hub,base,_,_=private_host
    first=json.load(request(base,'/api/copilot/info'))
    second=json.load(request(base,'/api/copilot/info'))
    assert first==second and first['app_id']=='hoardhub'
    assert first['data_space_id'] and 'token' not in first
    assert 'fixture-backend-secret' not in json.dumps(first)
    assert request(base,'/api/copilot/panel.js').read()==b'export const sharedPanel=true;'
    from hoard_link.copilot_launcher import LAUNCHER_JS
    assert request(base,'/api/copilot/launcher.js').read()==LAUNCHER_JS.encode()
    with request(base,'/') as response:assert b'/ui/copilot.js' in response.read()


def test_native_json_origin_and_stream_header(private_host):
    _,base,received,_=private_host
    info=json.load(request(base,'/api/copilot/info'))
    with request(base,'/api/copilot/proxy',{'path':'/api/chat_stream','method':'POST','body':{
        'hoard_context':{'origin':{'app_id':'wrong','data_space_id':'wrong'},'view':{'section':'agora'}}}}) as response:
        assert response.status==200 and response.headers['X-Odysseus-Run-Id']=='fixture-run'
        assert 'ámbito' in response.read().decode()
    context=json.loads(received[0][1])['hoard_context']
    assert context['origin']=={'app_id':'hoardhub','data_space_id':info['data_space_id']}
    assert context['view']['section']=='agora'
    assert received[0][2]['Authorization']=='Bearer fixture-backend-secret'


def test_multipart_bytes_and_native_error(private_host):
    _,base,received,_=private_host
    info=json.load(request(base,'/api/copilot/info'))
    raw=(b'--qa\r\nContent-Disposition: form-data; name="hoard_context"\r\n\r\n'
         b'{"origin":{"app_id":"wrong","data_space_id":"wrong"}}\r\n--qa\r\n'
         b'Content-Disposition: form-data; name="file"; filename="a.bin"\r\n\r\n\x00\xff\x80\r\n--qa--\r\n')
    with request(base,'/api/copilot/proxy',{'path':'/api/hoard-copilot/imports','method':'POST',
            'raw_body':base64.b64encode(raw).decode(),'content_type':'multipart/form-data; boundary="qa"'}) as response:
        assert response.status==409
    sent=received[0][1]
    assert sent.split(b'name="file"')[1]==raw.split(b'name="file"')[1]
    context=json.loads(sent.split(b'\r\n\r\n')[1].split(b'\r\n--qa')[0])
    assert context['origin']=={'app_id':'hoardhub','data_space_id':info['data_space_id']}


def test_native_upload_route_preserves_the_uploaded_bytes(private_host):
    _, base, received, _ = private_host
    raw = (b'--upload343\r\nContent-Disposition: form-data; name="files"; filename="a.bin"\r\n'
           b'Content-Type: application/octet-stream\r\n\r\n\x00\xff\x80\r\n--upload343--\r\n')
    with request(base, '/api/copilot/proxy', {'path': '/api/upload', 'method': 'POST',
            'raw_body': base64.b64encode(raw).decode(), 'content_type': 'multipart/form-data; boundary=upload343'}) as response:
        assert response.status == 200
    assert received[0][0] == '/api/upload'
    assert received[0][1] == raw


def profile_hub(tmp_path):
    from types import SimpleNamespace
    cfg=HubConfig(data_dir=str(tmp_path),roots=[],profiles={'Group':{'apps':['base'],'desktop':[],
        'commands':[{'name':'kept','cmd':['not-executed']}],'extra':{'kept':True}},'Other':{'apps':['other']}})
    cfg.save()
    return SimpleNamespace(config=cfg,_lock=threading.RLock(),get=lambda app:object())


def test_changed_members_preserve_other_agent_changes(tmp_path):
    from hoard_link.hub.profile_editor import patch
    hub=profile_hub(tmp_path)
    original=copy.deepcopy(hub.config.profiles)
    assert patch(hub,'Group',{'alpha':True},{})['ok']
    assert patch(hub,'Group',{'beta':True},{'beta':True})['ok']
    cfg=HubConfig.load(env={'HOARD_HUB_DATA_DIR':str(tmp_path)})
    assert set(cfg.profiles['Group']['apps'])=={'base','alpha','beta'}
    assert cfg.profiles['Group']['desktop']==['beta']
    assert cfg.profiles['Group']['commands']==original['Group']['commands']
    assert cfg.profiles['Group']['extra']==original['Group']['extra'] and cfg.profiles['Other']==original['Other']


def test_two_processes_preserve_members_and_config(tmp_path):
    hub=profile_hub(tmp_path)
    before=json.loads((tmp_path/'hub.json').read_text())
    code="""import sys,time,threading,json
from pathlib import Path
from types import SimpleNamespace
from hoard_link.hub.config import HubConfig
from hoard_link.hub.profile_editor import patch
root=Path(sys.argv[1]);app=sys.argv[2];cfg=HubConfig.load(env={'HOARD_HUB_DATA_DIR':str(root)})
hub=SimpleNamespace(config=cfg,_lock=threading.RLock(),get=lambda app:object())
(root/(app+'.ready')).write_text('ready')
while not (root/'go').exists():time.sleep(.01)
result=patch(hub,'Group',{app:True},{app:True});assert result['ok'],result
"""
    processes=[]
    try:
        for app in ('alpha','beta'):
            processes.append(subprocess.Popen([sys.executable,'-c',code,str(tmp_path),app],
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
        deadline=time.monotonic()+10
        while not all((tmp_path/(app+'.ready')).exists() for app in ('alpha','beta')):
            assert time.monotonic()<deadline
            time.sleep(.01)
        (tmp_path/'go').write_text('go')
        for process in processes:assert process.wait(timeout=10)==0
        after=json.loads((tmp_path/'hub.json').read_text())
        assert set(after['profiles']['Group']['apps'])=={'base','alpha','beta'}
        assert set(after['profiles']['Group']['desktop'])=={'alpha','beta'}
        assert after['profiles']['Group']['commands']==before['profiles']['Group']['commands']
        assert {k:v for k,v in after.items() if k!='profiles'}=={k:v for k,v in before.items() if k!='profiles'}
        assert after['profiles']['Other']==before['profiles']['Other']
    finally:
        for process in processes:
            if process.poll() is None:process.terminate();process.wait(timeout=10)


def test_configuration_save_keeps_concurrent_unedited_fields(tmp_path):
    hub=profile_hub(tmp_path)
    left=HubConfig.load(env={'HOARD_HUB_DATA_DIR':str(tmp_path)})
    right=HubConfig.load(env={'HOARD_HUB_DATA_DIR':str(tmp_path)})
    left.jobs_enabled=False;left.save()
    right.profiles['New']={'apps':['gamma']};right.save()
    current=json.loads((tmp_path/'hub.json').read_text())
    assert current['jobs_enabled'] is False and current['profiles']['New']['apps']==['gamma']
    assert current['profiles']['Group']==hub.config.profiles['Group']


def test_profile_patch_does_not_persist_unrelated_runtime_settings(tmp_path):
    from hoard_link.hub.profile_editor import patch
    hub=profile_hub(tmp_path)
    before=json.loads((tmp_path/'hub.json').read_text())
    hub.config.port=12345
    hub.config.faustus_urls=['http://example.invalid/not-persisted']
    assert patch(hub,'Group',{'alpha':True},{})['ok']
    after=json.loads((tmp_path/'hub.json').read_text())
    assert after['port']==before['port'] and after['faustus_urls']==before['faustus_urls']


def test_context_and_dirty_forms_in_real_browser(private_host):
    playwright=pytest.importorskip('playwright.sync_api')
    from hoard_link.web import browser
    from tempfile import TemporaryDirectory
    _,base,_,_=private_host
    with request(base,'/api/agora/task_add',{'agent':'qa387','title':'Domain context fixture','body':'Original'}) as response:
        task=json.load(response)['task']
    with playwright.sync_playwright() as p, TemporaryDirectory(prefix='hub387-dom-') as profile:
        try:
            context,_=browser.launch_context(p,profile,headless=True,viewport={'width':1440,'height':1000})
        except browser.BrowserUnavailable:
            pytest.skip('Chromium unavailable')
        try:
            page=context.new_page()
            page.goto(base,wait_until='networkidle')
            page.locator('[data-tab="agora"]').click()
            page.locator('.ag-card').filter(has_text='Domain context fixture').first.click()
            textarea=page.locator('.ag-compose textarea');textarea.fill('Unsent reply')
            page.locator('.ag-compose select').select_option('proposal')
            page.evaluate('()=>window.savedTextarea=document.querySelector(".ag-compose textarea")')
            active=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert active['active_objects'][0]['object_id']==str(task['id'])
            assert active['editor']['forms'][0]['fields']['body']=='Unsent reply'
            with request(base,'/api/agora/post',{'agent':'qa387','thread_id':task['thread_id'],'body':'Fresh message'}) as response:
                assert json.load(response)['ok']
            page.evaluate("""()=>{const fetch=window.fetch;let release;const gate=new Promise(resolve=>release=resolve);window.releaseRefresh=release;
                window.fetch=async(input,init)=>{if(String(input).includes('/api/agora/tasks/'))await gate;return fetch(input,init)};
                window.refresh=window.hoardCopilot.onTurnEnd({reason:'completed'});} """)
            textarea.fill('Typed during refresh')
            page.evaluate('async()=>{window.releaseRefresh();await window.refresh}')
            assert page.evaluate('()=>window.savedTextarea===document.querySelector(".ag-compose textarea")')
            assert textarea.input_value()=='Typed during refresh'
            assert page.locator('.ag-compose select').input_value()=='proposal'
            page.get_by_role('button',name='+ New task',exact=True).click()
            page.locator('[data-copilot-form] input[name="title"]').fill('New unsent task')
            new=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert new['active_objects']==[]
            assert any(form['fields'].get('title')=='New unsent task' for form in new['editor']['forms'])
            page.locator('[data-copilot-form="agora_task_new"]').get_by_role('button',name='Cancel',exact=True).click()
            rule_body={'name':'Pending rule fixture','enabled':False,'when':{'type':'private.none'},
                'then':[{'kind':'event','type':'private.fixture','data':{'message':'Keep this',
                    'env':{'API_KEY':'private-env-secret'},'api_key':'private-form-secret','clientSecret':'private-client-secret'}}]}
            with request(base,'/api/rules',rule_body) as response:
                assert json.load(response)['ok']
            with request(base,'/api/rules') as response:
                rule=next(row for row in json.load(response)['rules'] if row['name']==rule_body['name'])
            page.locator('[data-tab="rules"]').click()
            page.locator('#rules-list .rule').filter(has_text=rule_body['name']).get_by_role('button',name='Edit',exact=True).click()
            edited=json.loads(page.locator('#edit-body').input_value());edited['name']='Manually changed rule'
            page.locator('#edit-body').fill(json.dumps(edited))
            current=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert current['active_objects'][0]['object_id']==rule['id']
            assert current['editor']['forms'][0]['dirty']==['name']
            assert 'private-env-secret' not in json.dumps(current)
            assert 'private-form-secret' not in json.dumps(current)
            assert 'private-client-secret' not in json.dumps(current)
            assert current['editor']['forms'][0]['fields']['then'][0]['data']['message']=='Keep this'
            with request(base,f"/api/rules/{rule['id']}/update",{'enabled':True}) as response:
                assert json.load(response)['ok']
            page.evaluate('async()=>await window.hoardCopilot.onTurnEnd({reason:"completed"})')
            assert page.locator('#edit-body').input_value()==json.dumps(edited)
            page.locator('#edit-save').click()
            page.wait_for_function('!document.querySelector("#edit-dialog").open')
            with request(base,'/api/rules') as response:
                saved=next(row for row in json.load(response)['rules'] if row['id']==rule['id'])
            assert saved['enabled'] is True and saved['name']==edited['name']
            assert saved['then']==rule['then']
        finally:
            context.close()


def test_native_facet_forms_keep_pending_fields_and_save_only_changes(private_host):
    playwright=pytest.importorskip('playwright.sync_api')
    from hoard_link.web import browser
    from tempfile import TemporaryDirectory
    _,base,_,tmp_path=private_host
    with request(base,'/api/chats/sources',{'kind':'jsonl','name':'Offline source','path':str(tmp_path/'missing.jsonl'),'enabled':False}) as response:
        source=json.load(response)['source']
    with playwright.sync_playwright() as p, TemporaryDirectory(prefix='hub387-facets-') as profile:
        context,_=browser.launch_context(p,profile,headless=True,viewport={'width':1440,'height':1000})
        try:
            page=context.new_page();page.goto(base,wait_until='networkidle')
            page.locator('[data-tab="mailgate"]').click()
            page.locator('#tab-mailgate').get_by_role('button',name='Settings',exact=True).click()
            owner=page.locator('.mg-form input[name="owner"]');owner.fill('Pending owner')
            page.evaluate('()=>window.mailInput=document.querySelector(".mg-form input[name=owner]")')
            with request(base,'/api/mail/config',{'retention_days':45}) as response:assert json.load(response)['ok']
            await_refresh='async()=>await window.hoardCopilot.onTurnEnd({reason:"completed"})'
            page.evaluate(await_refresh)
            assert page.evaluate('()=>window.mailInput===document.querySelector(".mg-form input[name=owner]")')
            mail=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert any(f['kind']=='mail_config' and f['changes']=={'owner':'Pending owner'} for f in mail['editor']['forms'])
            with page.expect_response(lambda response:response.url.endswith('/api/mail/config') and response.request.method=='POST') as saved:
                page.locator('.mg-form').get_by_role('button',name='Save',exact=True).click()
            assert saved.value.json()['ok']
            with request(base,'/api/mail/config') as response:
                cfg=json.load(response)['config'];assert cfg['owner']=='Pending owner' and cfg['retention_days']==45
            page.locator('#tab-mailgate').get_by_role('button',name='Settings',exact=True).click()
            page.locator('[data-tab="chats"]').click()
            page.locator('#tab-chats .rule').filter(has_text='Offline source').get_by_role('button',name='Edit',exact=True).click()
            name=page.locator('.ch-form input[name="name"]');name.fill('Pending source')
            with request(base,'/api/chats/sources',{'id':source['id'],'interval_min':31}) as response:assert json.load(response)['ok']
            page.evaluate('()=>window.chatInput=document.querySelector(".ch-form input[name=name]")')
            page.evaluate(await_refresh)
            chat=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert chat['active_objects'][0]['object_id']==source['id']
            assert page.evaluate('()=>window.chatInput===document.querySelector(".ch-form input[name=name]")')
            with page.expect_response(lambda response:response.url.endswith('/api/chats/sources') and response.request.method=='POST') as saved:
                page.locator('.ch-form').get_by_role('button',name='Save',exact=True).click()
            assert saved.value.json()['ok']
            with request(base,'/api/chats/sources') as response:
                row=next(s for s in json.load(response)['sources'] if s['id']==source['id'])
                assert row['name']=='Pending source' and row['interval_min']==31 and row['enabled'] is False
            page.locator('[data-tab="notify"]').click();page.locator('.nt-settings').click()
            topic=page.locator('.nt-set input[name="topic"]');topic.fill('pending-topic')
            page.locator('.nt-set input[name="token"]').fill('private-form-token')
            with request(base,'/api/notify/config',{'rate_per_app':33}) as response:assert json.load(response)['ok']
            page.evaluate('()=>window.notifyInput=document.querySelector(".nt-set input[name=topic]")')
            page.evaluate(await_refresh)
            notify=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert 'private-form-token' not in json.dumps(notify)
            assert page.evaluate('()=>window.notifyInput===document.querySelector(".nt-set input[name=topic]")')
            with page.expect_response(lambda response:response.url.endswith('/api/notify/config') and response.request.method=='POST') as saved:
                page.locator('.nt-set [data-action="save"]').click()
            assert saved.value.json()['ok'],saved.value.json()
            with request(base,'/api/notify/config') as response:
                cfg=json.load(response);assert cfg['rate_per_app']==33 and cfg['channels']['ntfy']['topic']=='pending-topic'
            page.locator('.nt-settings').click()
            page.locator('.today-head').get_by_role('button',name='Calendar',exact=True).click()
            folder=page.locator('.today-ics input[name="export_path"]');folder.fill(str(tmp_path/'pending-export'))
            page.evaluate('()=>window.calendarInput=document.querySelector(".today-ics input[name=export_path]")')
            with request(base,'/api/today/ics/config',{'lan_port':0}) as response:assert json.load(response)['ok']
            page.evaluate(await_refresh)
            calendar=page.evaluate('async()=>await window.hoardCopilot.context()')
            form=next(f for f in calendar['editor']['forms'] if f['kind']=='calendar_config')
            assert form['changes']=={'export_path':str(tmp_path/'pending-export')}
            assert page.evaluate('()=>window.calendarInput===document.querySelector(".today-ics input[name=export_path]")')
            assert page.evaluate('()=>document.activeElement===window.calendarInput')
            assert 'urls' not in form['fields'] and 'url' not in form['fields']
            with page.expect_response(lambda response:response.url.endswith('/api/today/ics/config') and response.request.method=='POST') as saved:
                page.locator('.today-ics').get_by_role('button',name='Save',exact=True).click()
            assert saved.value.json()['ok']
            with request(base,'/api/today/ics') as response:assert json.load(response)['export_path']==str(tmp_path/'pending-export')
            page.locator('.today-head').get_by_role('button',name='Calendar',exact=True).click()
            page.locator('#sphere-chip').click();page.locator('.sphere-menu [data-action="configure"]').click()
            english=page.locator('#spheres-dialog').get_by_label('Name (English)',exact=True)
            english.fill('Pending personal name')
            page.evaluate('()=>window.sphereInput=document.querySelector("#spheres-dialog .sp-main input[type=text]")')
            with request(base,'/api/spheres',{'patches':[{'id':'personal','keywords':['Agent keyword']},
                {'id':'agent-added','name':'Agent-added sphere'}]}) as response:assert json.load(response)['ok']
            page.evaluate(await_refresh)
            spheres=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert spheres['active_objects'][0]['kind']=='sphere' and spheres['active_objects'][0]['object_id']=='personal'
            assert page.evaluate('()=>window.sphereInput===document.querySelector("#spheres-dialog .sp-main input[type=text]")')
            assert english.input_value()=='Pending personal name'
            with page.expect_response(lambda response:response.url.endswith('/api/spheres') and response.request.method=='POST') as saved:
                page.locator('#spheres-dialog .sp-save').click()
            assert saved.value.json()['ok']
            with request(base,'/api/spheres') as response:
                data=json.load(response)['spheres'];personal=next(s for s in data if s['id']=='personal')
                assert personal['name']['en']=='Pending personal name' and personal['keywords']==['Agent keyword']
                assert any(s['id']=='agent-added' for s in data)
            page.locator('#sphere-chip').click();page.locator('.sphere-menu [data-action="configure"]').click()
            page.locator('#spheres-dialog .sp-add').click()
            new_sphere=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert new_sphere['active_objects']==[]
            assert new_sphere['editor']['forms'][0]['is_new'] is True
        finally:context.close()


def test_active_modal_and_top_editors_win_without_losing_background_forms(private_host):
    playwright=pytest.importorskip('playwright.sync_api')
    from hoard_link.web import browser
    from tempfile import TemporaryDirectory
    hub,base,_,tmp_path=private_host
    family=tmp_path/'family'
    for app_id in ('alpha','beta'):
        folder=family/app_id;folder.mkdir(parents=True)
        manifest={'schema':1,'id':app_id,'name':app_id.title(),'purpose':'Private context fixture',
            'capabilities':['fixture'],'app':{'url_default':hub.config.faustus_urls[0],
            'health':{'path':'/api/health','expect':{'contract_version':'1.0'}}},
            'mcp':{'transport':'stdio','command':'python','args':[],'env':{}}}
        (folder/'faustus-plugin.json').write_text(json.dumps(manifest),encoding='utf8')
    hub.config.roots=[str(family)];hub.config.profiles={'Group':{'apps':['alpha'],'desktop':[]}}
    hub.config.save();hub.rescan()
    with request(base,'/api/rules',{'name':'Other modal fixture','enabled':False,'when':{'type':'private.none'},
        'then':[{'kind':'event','type':'private.fixture','data':{}}]}) as response:assert json.load(response)['ok']
    with playwright.sync_playwright() as p, TemporaryDirectory(prefix='hub387-mixed-editors-') as profile:
        context,_=browser.launch_context(p,profile,headless=True,viewport={'width':1440,'height':1000})
        try:
            page=context.new_page();page.goto(base,wait_until='networkidle')
            page.locator('.profile').filter(has=page.get_by_text('Group',exact=True)).get_by_role('button',name='Edit group',exact=True).click()
            alpha=page.locator('#group-members .group-member').filter(has=page.locator('input[value="alpha"]')).locator('.group-window')
            alpha.check()
            # A later editor opening must not alter an already requested snapshot.
            page.evaluate('''()=>{const original=window.fetch;let release;const gate=new Promise(resolve=>release=resolve);
                window.releaseSnapshot=release;window.fetch=async(input,init)=>{if(String(input).includes('/api/profiles/Group'))await gate;return original(input,init)};
                window.snapshot=window.hoardCopilot.context();}''')
            page.locator('#sphere-chip').click();page.locator('.sphere-menu [data-action="configure"]').click()
            english=page.locator('#spheres-dialog').get_by_label('Name (English)',exact=True);english.fill('Pending sphere with profile below')
            before=page.evaluate('async()=>{window.releaseSnapshot();return await window.snapshot}')
            assert before['active_objects'][0]['kind']=='profile' and before['view']['section']=='profiles'
            assert [form['kind'] for form in before['editor']['forms']]==['profile']
            modal=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert modal['active_objects'][0]['kind']=='sphere' and modal['active_objects'][0]['object_id']=='personal'
            assert modal['view']['section']=='spheres'
            assert {form['kind'] for form in modal['editor']['forms']}=={'profile','spheres'}
            page.locator('#spheres-dialog .sp-close').click()
            restored=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert restored['active_objects'][0]['object_id']=='Group' and restored['view']['section']=='profiles'
            assert alpha.is_checked()
            # Today is outside the family panel, and multiple pending editors remain available.
            page.locator('#family-toggle').click();assert page.locator('#family-body').evaluate('(body)=>body.hidden')
            page.locator('.today-head').get_by_role('button',name='Calendar',exact=True).click()
            folder=page.locator('.today-ics input[name="export_path"]');folder.fill(str(tmp_path/'pending-calendar'))
            calendar=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert calendar['view']['section']=='today' and calendar['active_objects']==[]
            assert {form['kind'] for form in calendar['editor']['forms']}=={'profile','calendar_config'}
            page.locator('[data-tab="mailgate"]').click()
            page.locator('#tab-mailgate').get_by_role('button',name='Settings',exact=True).click()
            page.locator('.mg-form input[name="owner"]').fill('Other pending editor')
            mail=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert mail['view']['section']=='mailgate'
            assert {form['kind'] for form in mail['editor']['forms']}=={'profile','calendar_config','mail_config'}
            # Base-family modals retain priority even with a top editor open.
            page.locator('[data-tab="rules"]').click()
            page.locator('#rules-list .rule').filter(has_text='Other modal fixture').get_by_role('button',name='Edit',exact=True).click()
            rule=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert rule['view']['section']=='rules' and rule['active_objects'][0]['kind']=='rule'
            assert {form['kind'] for form in rule['editor']['forms']}=={'profile','calendar_config','rules'}
            page.locator('#edit-close').click()
            page.locator('#sphere-chip').click();page.locator('.sphere-menu [data-action="configure"]').click()
            mixed=page.evaluate('async()=>await window.hoardCopilot.context()')
            assert mixed['view']['section']=='spheres' and mixed['active_objects'][0]['object_id']=='personal'
            assert {form['kind'] for form in mixed['editor']['forms']}=={'profile','calendar_config','spheres'}
            page.locator('#spheres-dialog .sp-close').click()
            assert alpha.is_checked() and folder.input_value()==str(tmp_path/'pending-calendar')
        finally:context.close()
