"""The portable repositories retain legacy facts and partition rooms by space."""
import json
import sqlite3

from hoard_link.coordination.council_contracts import CouncilMessage, CouncilSession
from hoard_link.coordination.council_store import CouncilStore
from hoard_link.coordination.groups import GroupStore


def test_council_legacy_rows_gain_space_without_replacing_history(tmp_path):
    path = tmp_path / 'legacy-council.db'
    store = CouncilStore(path=str(path))
    first = store.create_session(CouncilSession.parse({'id': 'original', 'owner': 'owner', 'title': 'Original'}))
    store.append_message(CouncilMessage.parse({'id': 'message', 'session_id': first.id, 'author_id': 'member', 'content': 'Kept'}))
    with sqlite3.connect(path) as conn:
        original = dict(zip([d[0] for d in conn.execute('SELECT * FROM council_sessions').description],
                            conn.execute('SELECT * FROM council_sessions').fetchone()))
        conn.execute('ALTER TABLE council_sessions DROP COLUMN space_id')
    restored = store.get_session(first.id, owner='owner')
    assert restored.space_id == 'ecosystem'
    assert restored.id == first.id and restored.revision == first.revision
    assert store.list_messages(first.id, audit=True)[0].content == 'Kept'
    with sqlite3.connect(path) as conn:
        columns = [d[0] for d in conn.execute('SELECT * FROM council_sessions').description]
        actual = dict(zip(columns, conn.execute('SELECT * FROM council_sessions').fetchone()))
    assert actual == original


def test_council_space_filter_is_not_an_owner_override(tmp_path):
    store = CouncilStore(path=str(tmp_path / 'rooms.db'))
    for sid, owner in [('ecosystem', 'alice'), ('project', 'alice'), ('project', 'bob')]:
        store.create_session(CouncilSession.parse({'id': sid + '-' + owner, 'owner': owner, 'space_id': sid}))
    assert {s.id for s in store.list_sessions(owner='alice')} == {'ecosystem-alice', 'project-alice'}
    assert [s.id for s in store.list_sessions(owner='alice', space_id='project')] == ['project-alice']
    assert not store.list_sessions(owner='alice', space_id='absent')


def test_group_legacy_definition_and_pending_fifo_survive_space_migration(tmp_path):
    path = tmp_path / 'groups.db'
    store = GroupStore(path)
    definition = {'parentId': 'original', 'mode': 'round-robin',
                  'participants': [{'sessionId': 'member', 'personaName': 'Member'}]}
    store.save(definition, 'alice')
    first, _ = store.enqueue('original', 'alice', 'Original work', 'request1')
    with sqlite3.connect(path) as conn:
        payload = conn.execute('SELECT definition FROM groups').fetchone()[0]
        conn.execute('ALTER TABLE groups DROP COLUMN space_id')
    reopened = GroupStore(path)
    assert reopened.list('alice', space_id='ecosystem') == [definition]
    assert reopened.turns('original')[0]['id'] == first
    assert reopened.steps(first)[0]['status'] == 'queued'
    assert reopened.enqueue('original', 'alice', 'Original work', 'request1') == (first, False)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT definition FROM groups').fetchone()[0] == payload


def test_group_room_members_and_owner_remain_canonical_in_each_space(tmp_path):
    store = GroupStore(tmp_path / 'groups.db')
    for sid in ('ecosystem', 'project'):
        store.save({'parentId': sid, 'space_id': sid, 'participants': []}, 'alice')
    assert {g['parentId'] for g in store.list('alice')} == {'ecosystem', 'project'}
    assert [g['parentId'] for g in store.list('alice', space_id='project')] == ['project']
    assert store.list('bob', space_id='project') == []
