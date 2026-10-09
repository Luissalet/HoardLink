"""Bounded recent thread reading through real SQLite, HTTP and stdio MCP."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hoard_link.hub.agora import Agora, AgoraError
from ._hub_fakes import http, make_hub, serve


def populate(ag):
    thread = ag.thread_open({'agent': 'qa-author', 'title': 'Synthetic recent work', 'body': 'Opening',
                             'mentions': ['qa-reader']})['thread']
    for n in range(1, 7):
        ag.post({'agent': 'qa-author', 'thread_id': thread['id'], 'body': f'Update {n}',
                 'mentions': ['qa-reader']})
    return thread['id']


@pytest.fixture
def ag(tmp_path):
    result = Agora(tmp_path / 'agora.db')
    yield result
    result.close()


def test_latest_window_and_older_pages_do_not_skip_or_ack_history(ag):
    tid = populate(ag)
    before = ag.inbox({'agent': 'qa-reader', 'peek': True})
    whole = ag.thread({'thread_id': tid})
    first = ag.thread_recent({'thread_id': tid, 'limit': 3})
    assert [{k: v for k, v in m.items() if k not in {'body_chars', 'body_digest', 'body_truncated'}}
            for m in first['messages']] == whole['messages'][-3:]
    assert first['pagination']['has_more']
    assert first['pagination']['next_before_id'] == first['messages'][0]['id']
    ag.post({'agent': 'qa-author', 'thread_id': tid, 'body': 'New publication during pagination'})
    second = ag.thread_recent({'thread_id': tid, 'limit': 3, 'before_id': first['pagination']['next_before_id']})
    third = ag.thread_recent({'thread_id': tid, 'limit': 3, 'before_id': second['pagination']['next_before_id']})
    assert third['pagination']['has_more'] is False
    assert third['pagination']['next_before_id'] is None
    assert [m['id'] for m in third['messages'] + second['messages'] + first['messages']] == [m['id'] for m in whole['messages']]
    assert ag.db.scalar("SELECT count(*) FROM acks WHERE agent='qa-reader'") == 0
    assert ag.db.scalar('SELECT count(*) FROM reads') == 0
    after = ag.inbox({'agent': 'qa-reader', 'peek': True})
    assert {m['id'] for m in before['pending']} <= {m['id'] for m in after['pending']}


def test_sql_fetch_is_bounded_and_does_not_call_full_history(ag, monkeypatch):
    tid = populate(ag)
    for n in range(300):
        ag._msg(tid, 'qa-author', 'comment', f'Bulk {n}', [])
    queries = []
    original = ag.db.query
    def query(sql, args=()):
        if 'FROM messages' in sql:
            queries.append((sql, args))
            assert 'LIMIT ?' in sql and args[-1] <= 201
        return original(sql, args)
    monkeypatch.setattr(ag.db, 'query', query)
    monkeypatch.setattr(ag, '_messages', lambda *_: pytest.fail('Unbounded history read'))
    result = ag.thread_recent({'thread_id': tid, 'limit': 2})
    assert len(result['messages']) == 2
    assert [m['body'] for m in result['messages']] == ['Bulk 298', 'Bulk 299']
    assert result['pagination']['has_more']
    assert len(queries) == 1


@pytest.mark.parametrize('args', [
    {'limit': True}, {'limit': 0}, {'limit': 201}, {'limit': '2'},
    {'before_id': True}, {'before_id': 0}, {'before_id': '2'},
    {'agent': 'qa-reader'}, {'latest': False}, {'thread_id': True},
])
def test_recent_reader_rejects_invalid_pagination_or_authority_fields(ag, args):
    tid = populate(ag)
    with pytest.raises(AgoraError):
        ag.thread_recent({'thread_id': tid, **args})
    assert ag.db.scalar("SELECT count(*) FROM acks WHERE agent='qa-reader'") == 0


def test_default_legacy_thread_and_explicit_ack_are_unchanged(ag):
    tid = populate(ag)
    assert len(ag.thread({'thread_id': tid})['messages']) == 7
    assert ag.db.scalar("SELECT count(*) FROM acks WHERE agent='qa-reader'") == 0
    ag.thread_recent({'thread_id': tid})
    assert ag.db.scalar("SELECT count(*) FROM acks WHERE agent='qa-reader'") == 0
    ag.thread({'thread_id': tid, 'agent': 'qa-reader'})
    assert ag.db.scalar("SELECT count(*) FROM acks WHERE agent='qa-reader'") == 1


def test_legacy_large_body_and_page_budget_have_honest_digests_prioritizing_latest(ag):
    tid = populate(ag)
    oversized = 'á' * 25_000
    ag._msg(tid, 'qa-author', 'comment', oversized, [])
    one = ag.thread_recent({'thread_id': tid, 'limit': 1})['messages'][0]
    assert len(one['body']) == 20_000
    assert one['body_chars'] == 25_000 and one['body_truncated'] is True
    assert one['body_digest'] == hashlib.sha256(oversized.encode('utf-8')).hexdigest()
    for n in range(12):
        ag._msg(tid, 'qa-author', 'comment', str(n % 10) * 20_000, [])
    page = ag.thread_recent({'thread_id': tid, 'limit': 20})
    assert sum(len(m['body']) for m in page['messages']) <= 200_000
    assert page['messages'][-1]['body_truncated'] is False
    assert page['messages'][0]['body_truncated'] is True
    assert ag.db.scalar("SELECT count(*) FROM acks WHERE agent='qa-reader'") == 0


def test_stances_are_explicitly_page_local_and_empty_page_has_no_cursor(ag):
    tid = populate(ag)
    ag.post({'agent': 'qa-author', 'thread_id': tid, 'body': 'Old stance', 'kind': 'disagree'})
    ag.post({'agent': 'qa-author', 'thread_id': tid, 'body': 'Latest stance', 'kind': 'agree'})
    page = ag.thread_recent({'thread_id': tid, 'limit': 2})
    assert page['stances'] == {'qa-author': 'agree'}
    assert page['stances_scope'] == 'page'
    empty = ag.thread_recent({'thread_id': tid, 'before_id': 1})
    assert empty['messages'] == [] and empty['pagination']['next_before_id'] is None
    assert empty['pagination']['has_more'] is False


def test_real_http_and_stdio_mcp_recent_contract(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    try:
        ag = hub.facet('agora').agora
        tid = populate(ag)
        base = hub.config.url.rstrip('/')
        status, first = http(f'{base}/api/agora/threads/{tid}/recent?limit=2')
        assert status == 200 and first['ok']
        assert [m['body'] for m in first['messages']] == ['Update 5', 'Update 6']
        status, second = http(f'{base}/api/agora/threads/{tid}/recent?limit=2&before_id={first["pagination"]["next_before_id"]}')
        assert status == 200 and [m['body'] for m in second['messages']] == ['Update 3', 'Update 4']
        for query in ('limit=-1', 'limit=201', 'before_id=0', 'agent=qa-reader'):
            status, error = http(f'{base}/api/agora/threads/{tid}/recent?{query}')
            assert status == 400 and error['ok'] is False
        status, legacy = http(f'{base}/api/agora/threads/{tid}')
        assert status == 200 and len(legacy['messages']) == 7
        env = os.environ.copy()
        env.update(HOARD_HUB_URL=base, HOARD_HUB_TOKEN_FILE=str(hub.config.token_file),
                   HOARD_HUB_DATA_DIR=str(hub.config.data_dir), HOARD_HUB_AUTOSTART='0')
        root = Path(__file__).resolve().parents[2]
        env['PYTHONPATH'] = str(root) + os.pathsep + env.get('PYTHONPATH', '')
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': 'hub_agora_thread_recent', 'arguments': {'thread_id': tid, 'limit': 2}}},
        ]
        result = subprocess.run([sys.executable, '-m', 'hoard_link.hub.mcp'], cwd=root, env=env,
                                input='\n'.join(json.dumps(r) for r in requests) + '\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0, result.stderr
        replies = {r['id']: r for r in map(json.loads, result.stdout.splitlines()) if 'id' in r}
        schema = next(t for t in replies[2]['result']['tools'] if t['name'] == 'hub_agora_thread_recent')
        assert schema['annotations']['readOnlyHint'] is True
        assert schema['inputSchema']['additionalProperties'] is False
        assert 'agent' not in schema['inputSchema']['properties']
        assert schema['inputSchema']['properties']['limit']['maximum'] == 200
        payload = json.loads(replies[3]['result']['content'][0]['text'])
        assert payload == first
        assert ag.db.scalar("SELECT count(*) FROM acks WHERE agent='qa-reader'") == 0
        assert ag.db.scalar('SELECT count(*) FROM reads') == 0
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
