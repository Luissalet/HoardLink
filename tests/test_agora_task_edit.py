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


@pytest.mark.parametrize('fields', [{'priority': True}, {'priority': 4}, {'title': ''}, {'owner': 'x' * 121}])
def test_invalid_edit_is_atomic(store, fields):
    task = add(store)
    with pytest.raises(AgoraError):
        store.task_edit({'agent': 'faustus', 'task_id': task['id'], 'expected_etag': task['etag'], **fields})
    assert store.task({'task_id': task['id']})['task']['etag'] == task['etag']


def test_text_versions_keep_original_and_intermediate_values_with_actor(store):
    task = add(store, acceptance='Original criterion', body='Original goal')
    for text in ['Intermediate', 'Current']:
        task = store.task_edit({'agent': 'editor', 'task_id': task['id'], 'expected_etag': task['etag'],
            'body': text + ' goal', 'acceptance': text + ' criterion'})['task']
    history = store.task({'task_id': task['id']})['edits']
    assert [edit['revision'] for edit in history] == [0, 1, 2]
    assert history[0]['author'] == 'builder' and history[0]['kind'] == 'created'
    assert history[0]['after']['acceptance'] == 'Original criterion'
    assert history[1]['before'] == history[0]['after']
    assert history[1]['after']['body'] == 'Intermediate goal'
    assert history[2]['before'] == history[1]['after']
    assert history[2]['author'] == 'editor' and history[2]['created'] == store.clock()


def test_preexisting_tasks_receive_truthful_baseline_on_schema_upgrade(tmp_path):
    from hoard_link.sqlkit import Database
    from hoard_link.hub.agora import MIGRATIONS
    path = tmp_path / 'old.db'
    db = Database(path, migrations=MIGRATIONS[:next(i for i,step in enumerate(MIGRATIONS) if 'CREATE TABLE task_edits' in step)])
    tid = db.insert('tasks', {'title': 'Old', 'body': 'Existing goal', 'acceptance': 'Existing criterion',
        'priority': 1, 'created_by': 'builder', 'space_id': 'ecosystem', 'updated': 123.0})
    db.close()
    upgraded = Agora(path)
    try:
        baseline = upgraded.task({'task_id': tid})['edits'][0]
        assert baseline['kind'] == 'baseline' and baseline['author'] == 'system'
        assert baseline['created'] == 123.0 and baseline['after']['acceptance'] == 'Existing criterion'
    finally:
        upgraded.close()


def test_valid_legacy_title_and_body_fit_shared_editor(store):
    task = add(store, title='X' * 300, body='Y' * 200000, acceptance='Z' * 200000)
    changed = store.task_edit({'agent': 'editor', 'task_id': task['id'], 'expected_etag': task['etag'],
        'title': 'A' * 250})['task']
    assert changed['title'] == 'A' * 250 and changed['body'] == task['body']


def test_editor_status_kind_labels_and_review_contract(store):
    task = add(store, claim=True, locks=['path:Repo/edited'])
    task = store.task_edit({'agent':'editor','task_id':task['id'],'expected_etag':task['etag'],
        'status':'done','kind':'idea','labels':['urgent','urgent','review']})['task']
    assert task['status']=='done' and task['kind']=='idea' and task['labels']==['urgent','review']
    assert store.task({'task_id':task['id']})['locks']==[]
    task=store.task_edit({'agent':'editor','task_id':task['id'],'expected_etag':task['etag'],'status':'open'})['task']
    assert task['closed_at'] is None and not task['reviewed']
    store.task_claim({'agent':'builder','task_id':task['id']})
    task=store.task_submit({'agent':'builder','task_id':task['id'],'summary':'Review me','commits':['abc123']})['task']
    with pytest.raises(AgoraError): store.task_edit({'agent':'editor','task_id':task['id'],'expected_etag':task['etag'],'status':'done'})
    with pytest.raises(AgoraError): store.task_edit({'agent':'editor','task_id':task['id'],'expected_etag':task['etag'],'status':'approved'})
    assert store.task({'task_id':task['id']})['task']['etag']==task['etag']


def test_compatibility_assignment_and_long_comments_preserve_text(store):
    task = add(store, owner='Ana García', note='A' * 21000, labels=['long label ' * 100])
    assert task['owner']=='Ana García'
    task=store.task_edit({'agent':'editor','task_id':task['id'],'expected_etag':task['etag'],'owner':'Ana','note':'B' * 21000})['task']
    assert task['owner']=='Ana'
    before=store.task({'task_id':task['id']})
    assert len(before['messages'][-1]['body'])==21000
    with pytest.raises(AgoraError): store.task_edit({'agent':'editor','task_id':task['id'],'expected_etag':task['etag'],'status':'done','note':'C' * 200001})
    assert store.task({'task_id':task['id']})['task']['etag']==task['etag']
