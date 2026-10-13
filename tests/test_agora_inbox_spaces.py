from hoard_link.hub.agora import Agora


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
