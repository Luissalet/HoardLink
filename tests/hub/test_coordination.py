"""Two native clients share canonical room state through actual Hub HTTP."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hoard_link import family
from hoard_link.coordination.council_contracts import CouncilSession, CouncilMessage
from hoard_link.coordination.council_store import CouncilStore, RevisionConflict
from hoard_link.coordination.groups import GroupStore
from hoard_link.coordination.rpc import RepositoryClient, encode, decode
from ._hub_fakes import make_hub, serve


@pytest.fixture
def canonical(tmp_path, monkeypatch):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        monkeypatch.setattr(family, '_state', dict(family._state))
        family.configure('faustus', hub=hub.config.url, token_file=str(hub.config.token_file))
        def transport(path, args):
            status, result = family._post(path, args, 30)
            return result
        yield hub, RepositoryClient(transport, 'council'), RepositoryClient(transport, 'groups')


def test_typed_http_clients_and_hub_view_use_same_room(canonical):
    hub, first, _ = canonical
    space = hub.facet('agora').agora.space_create({'agent': 'builder', 'id': 'project', 'title': 'Project'})['space']
    room = first.create_session(CouncilSession.parse({'id': 'room', 'owner': 'alice', 'space_id': space['id']}))
    assert isinstance(room, CouncilSession)
    first.append_message(CouncilMessage.parse({'id': 'message', 'session_id': room.id, 'author_id': 'member', 'content': 'Shared'}))
    current = hub.facet('coordination').council.get_session(room.id, owner='alice')
    assert current == room
    assert hub.facet('coordination').rooms({'space_id': 'project'})['rooms'][0]['id'] == room.id
    assert hub.facet('coordination').council.path == str(Path(hub.config.data_dir) / 'agora.db')
    assert first.list_messages(room.id, audit=True)[0].content == 'Shared'
    changed = first.update_session(room.id, {'title': 'Changed'}, owner='alice', expected_revision=room.revision)
    assert changed.revision == room.revision + 1
    with pytest.raises(RevisionConflict):
        first.update_session(room.id, {'title': 'Stale'}, owner='alice', expected_revision=room.revision)
    assert hub.facet('coordination').room({'kind': 'council', 'id': room.id, 'owner': 'bob'})['status'] == 404


def test_group_fifo_and_idempotency_are_canonical(canonical):
    hub, _, client = canonical
    definition = {'parentId': 'group', 'space_id': 'ecosystem', 'name': 'Group',
                  'participants': [{'sessionId': 'member', 'personaName': 'Member'}]}
    client.save(definition, 'alice')
    turn, fresh = client.enqueue('group', 'alice', 'Read this', 'request')
    assert fresh and client.enqueue('group', 'alice', 'Read this', 'request') == (turn, False)
    step = client.steps(turn)[0]
    client.update_step(step['id'], status='done', text='Read', detail={'outcome_unknown': False, 'actual_tools': 1})
    client.update_turn(turn, 'done')
    shared = hub.facet('coordination').ledger({'kind': 'groups', 'id': 'group', 'owner': 'alice'})
    assert len(shared['turns']) == 1
    assert shared['steps'][0]['detail']['actual_tools'] == 1
    assert client.turn_status(turn) == 'done'
    assert hub.facet('coordination').groups.path == Path(hub.config.data_dir) / 'agora.db'


def test_atomic_native_import_keeps_original_files_and_does_not_replay(canonical, tmp_path):
    hub, _, _ = canonical
    path = tmp_path / 'legacy' / 'council.db'
    legacy = CouncilStore(path=str(path))
    room = legacy.create_session(CouncilSession.parse({'id': 'legacy-room', 'owner': 'alice'}))
    legacy.append_message(CouncilMessage.parse({'id': 'legacy-message', 'session_id': room.id, 'content': 'Original'}))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = family.call('hub', 'hub_coordination_bind', {'sources': {'council': str(path)}})['result']
    assert receipt['ok'] and receipt['imports']['council']['rows'] == 2
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    shared = hub.facet('coordination').council
    shared.update_session(room.id, {'title': 'Canonical edit'}, expected_revision=room.revision, owner='alice')
    again = family.call('hub', 'hub_coordination_bind', {'sources': {'council': str(path)}})['result']
    assert again['ok'] and again['imports']['council']['already_imported']
    assert shared.get_session(room.id, owner='alice').title == 'Canonical edit'
    assert legacy.get_session(room.id, owner='alice').title == ''


def test_payload_type_shaped_keys_remain_ordinary_data():
    data = {'record_type': 'CouncilSession', 'value': {'owner': 'not-a-record'}, 'tuple': (1, 2)}
    assert decode(encode(data)) == data


def test_native_stdio_mcp_reads_canonical_rooms(canonical, tmp_path):
    hub, client, _ = canonical
    client.create_session(CouncilSession.parse({'id': 'mcp-room', 'owner': 'alice', 'title': 'Canonical'}))
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'HOARD_HUB_URL': hub.config.url, 'HOARD_HUB_TOKEN_FILE': str(hub.config.token_file),
           'HOARD_HUB_AUTOSTART': '0', 'HOARD_HUB_DATA_DIR': str(hub.config.data_dir), 'PYTHONPATH': str(root)}
    requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                    'name': 'hub_coordination_rooms', 'arguments': {'space_id': 'ecosystem'}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {
                    'name': 'hub_coordination_room', 'arguments': {'kind': 'council', 'id': 'mcp-room', 'owner': 'alice'}}}]
    result = subprocess.run([sys.executable, '-m', 'hoard_link.hub.mcp'], cwd=root, env=env,
                            input='\n'.join(json.dumps(r) for r in requests) + '\n',
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    replies = {r['id']: r for r in map(json.loads, result.stdout.splitlines()) if 'id' in r}
    assert not replies[3]['result'].get('isError') and not replies[4]['result'].get('isError')
    room = json.loads(replies[4]['result']['content'][0]['text'])['session']
    assert room['id'] == 'mcp-room' and room['title'] == 'Canonical'
    (tmp_path / 'canonical-mcp-receipt.json').write_text(json.dumps(replies, indent=2), encoding='utf-8')
