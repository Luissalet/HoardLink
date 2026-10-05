from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from hoard_link.hub import autostart, profile_editor, procs
from hoard_link.hub.config import HubConfig
from hoard_link.hub.ports import PortAssignments
from hoard_link.hub.registry import App, LaunchSpec
from hoard_link.hub.server import make_server
from .conftest import free_port
from .test_hub_and_server import _http


@pytest.fixture
def windows(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, '_is_windows', lambda: True)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'appdata'))
    return autostart.script_path()


def test_groups_http_persist_start_and_windows_roundtrip(hub, windows):
    server = make_server(hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url, name = hub.config.url, 'Creativo tarde'
    base = url + '/api/profiles/' + quote(name)
    try:
        assert _http(base+'/save', {'apps':['launch'], 'desktop':[], 'create':True})[0] == 200
        assert HubConfig.load(env={'HOARD_HUB_DATA_DIR':hub.config.data_dir}).profiles[name]['apps'] == ['launch']
        assert _http(base+'/save', {'apps':['fake'], 'create':True})[0] == 400
        assert _http(base+'/save', {'apps':['ghost']})[0] == 400
        assert _http(base+'/save', {'apps':['launch'], 'commands':[{'cmd':'bad'}]})[0] == 400
        assert _http(base+'/start', {})[1]['ok']
        assert procs.health(hub.get('launch')).state == 'healthy'
        assert _http(url+'/api/autostart', {'enabled':True, 'profile':'unknown'})[0] == 400
        assert not windows.exists()
        assert _http(url+'/api/autostart', {'enabled':True, 'profile':name})[1]['ok']
        text = windows.read_text(encoding='utf-8')
        assert '--profile "Creativo tarde"' in text and '--data-dir' in text
        assert _http(url+'/api/autostart')[1]['profile'] == name
        assert _http(base+'/remove', {})[0] == 400
        assert _http(base+'/remove')[0] == 404  # GET cannot mutate
        assert _http(url+'/api/autostart', {'enabled':False}, headers={'Sec-Fetch-Site':'cross-site'})[0] == 403
        assert windows.exists()
        assert _http(url+'/api/autostart', {'enabled':False})[1]['removed']
        assert _http(base+'/stop', {})[1]['ok']
        assert _http(base+'/remove', {})[1]['ok']
        assert name not in HubConfig.load(env={'HOARD_HUB_DATA_DIR':hub.config.data_dir}).profiles
    finally:
        server.shutdown()
        hub.stop('launch')


@pytest.mark.parametrize('name', ['bad&echo', '%TEMP%', '-option', 'bad\nname', ' bad', 'x'*65])
def test_bad_group_names_do_not_write(hub, name):
    before = copy.deepcopy(hub.config.profiles)
    assert not profile_editor.save(hub, name, {'apps':['fake']})['ok']
    assert hub.config.profiles == before


def test_group_edit_preserves_commands_and_rolls_back_on_save_error(hub, monkeypatch):
    original = {'apps':['fake'], 'commands':[{'name':'configured', 'cmd':['echo','ok']}]}
    hub.config.profiles = {'Existing':copy.deepcopy(original)}
    assert profile_editor.save(hub,'Existing',{'apps':['launch']})['ok']
    assert hub.config.profiles['Existing']['commands'] == original['commands']
    before = copy.deepcopy(hub.config.profiles)
    monkeypatch.setattr(hub.config,'save',lambda: (_ for _ in ()).throw(OSError('disk full')))
    assert not profile_editor.save(hub,'Existing',{'apps':['fake']})['ok']
    assert hub.config.profiles == before
    assert not profile_editor.remove(hub,'Existing')['ok']
    assert hub.config.profiles == before


def test_running_group_cannot_be_removed_or_lose_command_controls(hub, monkeypatch):
    hub.config.profiles = {'Running':{'apps':['launch'],'commands':[{'cmd':['echo','test']}]}}
    monkeypatch.setattr(hub,'profile_status',lambda name:{'members':[{'kind':'command','state':'running'}]})
    before = copy.deepcopy(hub.config.profiles)
    assert not profile_editor.remove(hub,'Running')['ok']
    assert hub.config.profiles == before and 'Running' in hub.profiles()


@pytest.mark.parametrize('content', ['echo custom startup\n', 'echo rem hoard-hub autostart\n'])
def test_missing_group_and_foreign_startup_never_overwrite(hub, windows, content):
    assert not profile_editor.startup(hub, {'enabled':True})['ok']
    assert not windows.exists()
    windows.parent.mkdir(parents=True)
    windows.write_text(content,encoding='utf-8')
    hub.config.profiles = {'Group':{'apps':['fake']}}
    assert not profile_editor.startup(hub, {'enabled':True,'profile':'Group'})['ok']
    assert not autostart.status()['ours']
    assert not profile_editor.startup(hub, {'enabled':False})['ok']
    assert windows.read_text(encoding='utf-8') == content


def test_disk_manifest_opt_in_is_expanded_without_guessing(tmp_path):
    from hoard_link.hub.registry import read_manifest
    manifest = {'id':'diskhoard', 'app':{
        'url_default':'http://127.0.0.1:8817', 'x-url-file':'{DISKHOARD_DIR}/data/url',
        'launch_hint':{'kind':'process', 'executable':sys.executable,
                       'argv':['-m','diskhoard'], 'cwd':'{DISKHOARD_DIR}', 'port_argument':'--port'}}}
    path = tmp_path/'faustus-plugin.json'
    path.write_text(json.dumps(manifest),encoding='utf-8')
    app = read_manifest(str(path))
    assert Path(app.runtime_url_file) == tmp_path/'data/url'
    assert app.launch.port_argument == '--port' and app.to_dict()['port_adaptable']
    manifest['app']['launch_hint']['port_argument']='--port; bad'
    path.write_text(json.dumps(manifest),encoding='utf-8')
    assert not read_manifest(str(path)).to_dict()['port_adaptable']


def _app(app_id, port, tmp_path):
    return App(app_id, app_id, '', str(tmp_path), f'http://127.0.0.1:{port}', '/api/health',
               'launch-hoard', launch=LaunchSpec(sys.executable, [], str(tmp_path),
               f'http://127.0.0.1:{port}/api/health', 10, {}, '--port'), launchable=True)


def test_busy_port_allocates_skips_registered_ports_persists_and_reuses(tmp_path, fake_app):
    port, _ = fake_app
    book = PortAssignments(tmp_path/'ports.json')
    a, b = _app('a',port,tmp_path), _app('b',port+1,tmp_path)
    book.restore([a,b])
    result = book.prepare(a,[a,b],port+2)
    assert result['ok'] and a.port not in {port,port+1,port+2}
    assert a.launch.argv[-2:] == ['--port',str(a.port)]
    assert a.launch.readiness_url == a.health_url()
    restored = _app('a',port,tmp_path)
    PortAssignments(tmp_path/'ports.json').restore([restored])
    assert restored.port == a.port
    assert book.prepare(a,[a,b],port+2)['ok'] and len(a.launch.argv)==2
    assert procs.fetch_json(f'http://127.0.0.1:{port}/api/health')[1]['service'] == 'fake-hoard'


def test_parallel_allocations_never_choose_same_or_other_default(tmp_path, fake_app):
    port, _ = fake_app
    book = PortAssignments(tmp_path/'ports.json')
    apps = [_app('a',port,tmp_path), _app('b',port,tmp_path)]
    book.restore(apps)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda app:book.prepare(app,apps,port+1),apps))
    assert all(r['ok'] for r in results)
    assert len({a.port for a in apps}) == 2 and port not in {a.port for a in apps}
    assert len(json.loads((tmp_path/'ports.json').read_text()))==2


def test_declared_sidecar_requires_containment_loopback_and_service(tmp_path, fake_app):
    port, _ = fake_app
    book = PortAssignments(tmp_path/'ports.json')
    a = _app('a', free_port(),tmp_path)
    a.data_dir = str(tmp_path/'data'); Path(a.data_dir).mkdir()
    a.runtime_url_file = str(Path(a.data_dir)/'url')
    hint = Path(a.runtime_url_file)
    for url in ['https://example.com','http://user:pass@127.0.0.1:'+str(port),f'http://127.0.0.1:{port}/wrong',f'http://127.0.0.1:{port}']:
        hint.write_text(url,encoding='utf-8')
        assert not book.discover(a)  # wrong service on the final candidate
    a.expect_service = 'fake-hoard'
    assert book.discover(a) and a.port==port
    a.runtime_url_file = str(tmp_path/'outside')
    Path(a.runtime_url_file).write_text(f'http://127.0.0.1:{port}')
    assert not book.discover(a)


def test_readiness_race_does_not_call_foreign_service_ready(tmp_path, monkeypatch):
    a = _app('race',free_port(),tmp_path)
    states = iter([procs.Health('down'), procs.Health('foreign',200,'other','wrong service')])
    monkeypatch.setattr(procs,'health',lambda app:next(states))
    monkeypatch.setattr(procs,'fetch_json',lambda *a,**k:(200,{'service':'other'}))
    child=SimpleNamespace(pid=987654,poll=lambda:None)
    monkeypatch.setattr(procs.subprocess,'Popen',lambda *a,**k:child)
    monkeypatch.setattr(procs,'proc_info',lambda pid:SimpleNamespace(created_at=123.0))
    stopped=[]
    monkeypatch.setattr(procs,'terminate_tree',lambda pid,**k:stopped.append(pid) or {'ok':True})
    result=procs.start_app(a,str(tmp_path/'logs'))
    assert not result['ok'] and not result['ready'] and stopped==[child.pid]


def test_readiness_cleanup_uses_spawn_identity_not_current_pid(tmp_path, monkeypatch):
    a = _app('race',free_port(),tmp_path)
    states=iter([procs.Health('down'),procs.Health('foreign',200,'other','wrong service')])
    monkeypatch.setattr(procs,'health',lambda app:next(states))
    monkeypatch.setattr(procs,'fetch_json',lambda *a,**k:(200,{}))
    monkeypatch.setattr(procs.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=12345,poll=lambda:None))
    seen=[]
    def identity(pid):
        seen.append(pid)
        return SimpleNamespace(created_at=10.0 if len(seen)==1 else 20.0)
    monkeypatch.setattr(procs,'proc_info',identity)
    def terminate(pid,*,created_at):
        assert created_at==10.0 and identity(pid).created_at==20.0
        return {'ok':False,'error':'pid recycled; no termination'}
    monkeypatch.setattr(procs,'terminate_tree',terminate)
    result=procs.start_app(a,str(tmp_path/'logs'))
    assert not result['cleanup']['ok'] and 'recycled' in result['cleanup']['error']
