import pytest
from hoard_link.hub.agora import Agora, AgoraError, AgoraFacet


@pytest.fixture
def store(tmp_path):
    value = Agora(tmp_path / 'agora.db', clock=lambda: 1800000000.0)
    yield value
    value.close()


def add(store, **fields):
    return store.task_add({'agent': 'builder', 'title': 'Shared task', **fields})['task']


def test_same_task_edited_by_other_agent_with_acceptance_and_clarification(store):
    before = add(store, body='Original', owner='builder')
    after = store.task_edit({'agent': 'faustus', 'task_id': before['id'], 'expected_etag': before['etag'],
        'title': 'Clarified', 'body': 'One objective', 'acceptance': 'Verify the outcome', 'priority': 0,
        'note': 'Please keep the original conversation.'})['task']
    assert (after['body'], after['acceptance'], after['priority']) == ('One objective', 'Verify the outcome', 0)
    assert after['etag'] != before['etag']
    detail = store.task({'task_id': after['id']})
    assert detail['messages'][-1]['body'] == 'Please keep the original conversation.'
    with pytest.raises(AgoraError) as stale:
        store.task_edit({'agent': 'builder', 'task_id': before['id'], 'expected_etag': before['etag'], 'body': 'Lost update'})
    assert stale.value.status == 409
    assert store.task({'task_id': before['id']})['task']['body'] == 'One objective'


def test_assignment_transfers_leases_and_unassignment_releases(store):
    task = add(store, claim=True, locks=['path:Repo/a'])
    task = store.task_edit({'agent': 'faustus', 'task_id': task['id'], 'expected_etag': task['etag'], 'owner': 'reviewer'})['task']
    assert store.task({'task_id': task['id']})['locks'][0]['owner'] == 'reviewer'
    task = store.task_edit({'agent': 'faustus', 'task_id': task['id'], 'expected_etag': task['etag'], 'owner': None})['task']
    assert task['status'] == 'open'
    assert store.task({'task_id': task['id']})['locks'] == []


def test_text_changes_invalidate_approved_revision(store):
    task = add(store, claim=True)
    task = store.task_submit({'agent': 'builder', 'task_id': task['id'], 'summary': 'Ready', 'commits': ['abc123']})['task']
    revision = task['submission_revision']
    task = store.task_edit({'agent': 'faustus', 'task_id': task['id'], 'expected_etag': task['etag'], 'acceptance': 'Additional check'})['task']
    assert task['status'] == 'changes'
    assert task['submission_revision'] == revision + 1
    with pytest.raises(AgoraError):
        store.task_review({'agent': 'reviewer', 'task_id': task['id'], 'verdict': 'approve', 'expected_submission_revision': revision})


def test_idempotent_import_preserves_later_edits_and_space_boundaries(store):
    task = add(store, origin_ref='faustus:p:objective:one', origin_status='done', acceptance='Keep me')
    changed = store.task_edit({'agent': 'faustus', 'task_id': task['id'], 'expected_etag': task['etag'], 'body': 'Edited later'})['task']
    repeat = add(store, origin_ref='faustus:p:objective:one', body='Old source')
    assert repeat['id'] == changed['id']
    assert repeat['body'] == 'Edited later'
    assert repeat['status'] == 'done'
    sid = store.space_create({'agent': 'builder', 'title': 'Other', 'id': 'other'})['space']['id']
    with pytest.raises(AgoraError) as absent:
        store.task_edit({'agent': 'faustus', 'space_id': sid, 'task_id': task['id'], 'expected_etag': changed['etag'], 'priority': 0})
    assert absent.value.status == 404


def test_all_task_pages_and_native_tool_contract(store):
    for index in range(5):
        add(store, title=str(index))
    first = store.tasks({'limit': 3})
    second = store.tasks({'limit': 3, 'cursor': first['next_cursor']})
    assert len({task['id'] for task in first['tasks'] + second['tasks']}) == 5
    assert second['next_cursor'] is None
    assert any(tool['name'] == 'hub_agora_task_edit' for tool in AgoraFacet.tools())


@pytest.mark.parametrize('fields', [{'priority': True}, {'priority': 4}, {'title': ''}, {'owner': 'Bad Actor'}])
def test_invalid_edit_is_atomic(store, fields):
    task = add(store)
    with pytest.raises(AgoraError):
        store.task_edit({'agent': 'faustus', 'task_id': task['id'], 'expected_etag': task['etag'], **fields})
    assert store.task({'task_id': task['id']})['task']['etag'] == task['etag']
