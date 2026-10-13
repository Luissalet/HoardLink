from hoard_link.hub.agora import Agora


def test_console_scopes_http_mcp_threads_and_worker_context(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hoard_link.hub.agora_console import AgoraConsoleFacet
    from hoard_link.hub.facets import Request
    agora = Agora(tmp_path / 'agora.db')
    for sid in ('project-a', 'project-b'):
        agora.space_create({'agent':'tester','id':sid,'title':sid})
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path),token_file=str(tmp_path/'token'),
        url='http://127.0.0.1:63496',faustus_urls=[]),
        facet=lambda name: SimpleNamespace(agora=agora) if name == 'agora' else None,
        faustus_status=lambda: {'reachable':False}, get=lambda ident:None)
    facet = AgoraConsoleFacet(hub)
    monkeypatch.setattr(AgoraConsoleFacet, '_provider_data', lambda self, *args, **kw: {
        ident:{'catalog':[{'id':'fixture','efforts':['low'],'default_effort':'low'}], 'detail':'fixture'}
        for ident in ('codex','claude','cursor','faustus')})
    commands = []
    monkeypatch.setattr(AgoraConsoleFacet, '_codex_executable', lambda self:'private-fixture.exe')
    monkeypatch.setattr('hoard_link.hub.agora_console.subprocess.Popen', lambda cmd, **kw:
        commands.append(cmd) or SimpleNamespace(pid=1234))
    class DeferredThread:
        def __init__(self, *args, **kw):pass
        def start(self):pass
    monkeypatch.setattr('hoard_link.hub.agora_console.threading.Thread', DeferredThread)
    try:
        a, b = facet.for_space('project-a'), facet.for_space('project-b')
        assert a.thread_id != b.thread_id
        assert a._leader_identity('codex') != b._leader_identity('codex')
        a.send_message({'body':'A scoped message','recipients':['claude']}, 'tester')
        assert any(m['body'] == 'A scoped message' for m in a._agora().thread_recent({'thread_id':a.thread_id})['messages'])
        assert not any(m['body'] == 'A scoped message' for m in b._agora().thread_recent({'thread_id':b.thread_id})['messages'])
        result = facet.handlers()['hub_agora_console_color']({'actor':'tester','space_id':'project-a','leader_id':'codex','color':'#123456'})
        assert result['ok']
        request = Request(method='POST',path='/api/agora/console/color',query={},
            body={'space_id':'project-b','leader_id':'codex','color':'#654321'},caller=lambda:'tester',agent=lambda:True)
        assert facet.post(request)['ok']
        assert a._colors()['codex'] == '#123456' and b._colors()['codex'] == '#654321'
        worker = a.worker({'leader_id':'codex','model':'fixture','effort':'low','task':'Private scoped task'},'tester')
        assert worker['ok'] and '--space project-a --as ' in commands[-1][-1]
        assert not b._worker_rows()
        assert all('space_id' in tool['inputSchema']['properties'] for tool in facet.tools())
    finally:
        facet.close()
        agora.close()


def test_all_space_receiver_preserves_scoped_cursors_and_context(tmp_path):
    agora = Agora(tmp_path / 'agora.db')
    try:
        agora.space_create({'agent': 'writer', 'id': 'project', 'title': 'Project'})
        for sid in ('ecosystem', 'project'):
            local = agora.for_space(sid)
            local.thread_open({'agent': 'writer', 'title': sid, 'kind': 'handoff', 'body': sid, 'mentions': ['receiver']})
        peek = agora.inbox({'agent': 'receiver', 'all_spaces': True, 'peek': True, 'limit': 1})
        first = peek['messages'][0]
        assert first['space_id'] == 'ecosystem'
        assert agora.space_store.read_upto('ecosystem', 'receiver') == 0
        read = agora.inbox({'agent': 'receiver', 'all_spaces': True, 'limit': 1})
        assert read['messages'] == peek['messages']
        assert agora.space_store.read_upto('ecosystem', 'receiver') == first['id']
        assert agora.space_store.read_upto('project', 'receiver') == 0
        second = agora.inbox({'agent': 'receiver', 'all_spaces': True, 'metadata_only': True})
        assert second['messages'][0]['space_id'] == 'project'
        assert 'body' not in second['messages'][0]
        assert agora.space_store.read_upto('project', 'receiver') == 0
        assert not agora.inbox({'agent': 'receiver', 'space_id': 'ecosystem'})['messages']
        project = agora.inbox({'agent': 'receiver', 'space_id': 'project'})
        assert project['messages'][0]['body'] == 'project'
    finally:
        agora.close()
