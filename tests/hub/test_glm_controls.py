"""Private real HTTP + actual stdio bridge; no global Hub/model or GPU."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hoard_link import Link, family
from hoard_link.config import LinkConfig, CapabilityConfig
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.linkchat import LinkService
from hoard_link.hub.server import make_server
from hoard_link.types import Resolution
from tests.test_glm_controls import MODEL, SCHEMA, MESSAGES


@pytest.fixture
def controls_hub(tmp_path):
    state = {'posts':[], 'probes':[]}
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            state['probes'].append(self.path)
            data = {'data':[{'id':MODEL}]} if self.path == '/v1/models' else SCHEMA
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['posts'].append(body)
            self.send_response(200)
            if body.get('stream'):
                raw = ('data: '+json.dumps({'choices':[{'delta':{'reasoning_content':'idea'}}]})+'\n\n'+
                       'data: '+json.dumps({'choices':[{'delta':{'content':'ok'},'finish_reason':'stop'}]})+
                       '\n\ndata: [DONE]\n\n').encode()
                self.send_header('Content-Type','text/event-stream')
            else:
                raw = json.dumps({'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}).encode()
                self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    provider = ThreadingHTTPServer(('127.0.0.1',0), Provider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    backend = f'http://127.0.0.1:{provider.server_port}/v1/chat/completions'
    cfg = HubConfig(port=0,data_dir=str(tmp_path/'hub'), roots=[],icon_dirs=[],jobs_enabled=False,faustus_urls=[])
    hub = Hub(cfg)
    hub.link.close()
    hub.link = LinkService(cfg,link_factory=lambda app: Link(LinkConfig(app=app,faustus_urls=(),
        capabilities={'llm':CapabilityConfig(url=backend,model=MODEL,provider='unknown',api='openai')})))
    server = make_server(hub,port=0)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    token = Path(cfg.data_dir)/'mcp-token'
    family.configure('qa843',hub=url,token_file=str(token))
    try:
        yield hub,url,token,state
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
        provider.shutdown()
        provider.server_close()


@pytest.mark.parametrize('streaming', [False,True])
def test_real_family_sdk_through_hub_preserves_ctk_and_effective_metadata(controls_hub, streaming):
    hub,url,token,state = controls_hub
    kwargs = dict(effort='off',chat_template_kwargs={'clear_thinking':False})
    result = list(family.stream_chat(MESSAGES,**kwargs))[-1] if streaming else family.chat(MESSAGES,**kwargs)
    assert result['ok'] and result['reasoning_controls']['requested_effort'] == 'off'
    assert result['reasoning_controls']['effective_effort'] == 'low'
    assert result['reasoning_controls']['thinking_can_disable'] is False
    assert state['posts'][-1]['chat_template_kwargs'] == {'enable_thinking':True,'clear_thinking':False}
    assert 'thinking_token_budget' not in state['posts'][-1]
    assert hub.link.gate.active == 0


def test_actual_stdio_mcp_uses_real_hub_parse_and_link(controls_hub):
    hub,url,token,state = controls_hub
    messages = [{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
                {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'hub_link_chat','arguments':{
                    'prompt':'fixture','effort':'max','chat_template_kwargs':{'clear_thinking':False}}}}]
    env = dict(os.environ, HOARD_HUB_URL=url, HOARD_HUB_TOKEN_FILE=str(token), HOARD_HUB_AUTOSTART='0')
    process = subprocess.run([sys.executable,'-m','hoard_link.hub.mcp'],input=''.join(json.dumps(m)+'\n' for m in messages),
                             text=True,capture_output=True,encoding='utf-8',env=env,timeout=20)
    assert process.returncode == 0,process.stderr
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    result = json.loads(replies[-1]['result']['content'][0]['text'])
    assert result['ok'] and result['reasoning_controls']['effective_effort'] == 'max'
    assert state['posts'][-1]['thinking_token_budget'] == 16384
    assert state['posts'][-1]['reasoning_effort'] == 'max'
    assert state['posts'][-1]['chat_template_kwargs']['clear_thinking'] is False


def test_invalid_template_kwargs_rejected_by_real_hub_before_backend(controls_hub):
    import httpx
    _,url,token,state = controls_hub
    reply = httpx.post(url+'/api/link/chat',json={'messages':MESSAGES,'chat_template_kwargs':'invalid'},
                       headers={'Authorization':'Bearer '+token.read_text()})
    assert reply.status_code == 400 and not state['posts']


def test_status_advertises_verified_capability_with_unknown_provider(controls_hub):
    hub,_,_,state = controls_hub
    link = hub.link._link('hoard-hub')
    resolve = link.resolve
    async def only_fixture(cap):
        if cap == 'llm':
            return await resolve(cap)
        return Resolution(cap,None,None,None,None,'unavailable','fixture has no such capability')
    link.resolve = only_fixture
    status = hub.link.status(force=True)
    assert status['llm']['provider'] == 'unknown'
    assert status['llm']['reasoning_controls']['thinking_can_disable'] is False
    assert not state['posts']


def test_node_clients_preserve_options_and_effective_metadata(controls_hub):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node not installed')
    _,url,token,state = controls_hub
    module = (Path(__file__).resolve().parents[2]/'js'/'hoard-link.js').as_uri()
    code = f'''
      import * as family from {json.dumps(module)};
      family.configure({{app:'qa843',hub:{json.dumps(url)},tokenFile:{json.dumps(str(token))}}});
      const args={{messages:[{{role:'user',content:'fixture'}}],effort:'medium',chatTemplateKwargs:{{clear_thinking:false}}}};
      const block=await family.chat(args), events=[];
      for await(const e of family.streamChat(args)) events.push(e);
      console.log(JSON.stringify({{block,final:events.at(-1)}}));
    '''
    process = subprocess.run([node,'--input-type=module','-e',code],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert process.returncode == 0,process.stderr
    result = json.loads(process.stdout)
    for row in result.values():
        assert row['reasoning_controls']['effective_effort'] == 'low'
        assert row['reasoning_controls']['requested_effort'] == 'medium'
    assert len(state['posts']) == 2
    assert all(row['thinking_token_budget'] == 4096 and row['chat_template_kwargs']['clear_thinking'] is False
               for row in state['posts'])
