"""Native MCP observation budgets over private real HTTP; no model or live Hub."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import pytest

from hoard_link.hub import contract, mcp, tools


@contextmanager
def serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown(); server.server_close(); thread.join()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body):
        raw = json.dumps(body).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def call(name='hub_call_app', arguments=None):
    return mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': name, 'arguments': arguments or {}}})


@pytest.mark.parametrize('transport_failure', [False, True])
def test_native_appcall_observes_explicit_work_or_preserves_unknown_without_retry(tmp_path, monkeypatch, transport_failure):
    scale = .004
    requests, observed = [], []
    completed = threading.Event()
    token = tmp_path / 'token'
    token.write_text('private-fixture-token')

    class AppHandler(Handler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(('app', self.path, body))
            time.sleep(421 * scale)
            self.reply({'ok': True, 'result': {'fixture': 'completed'}})
            completed.set()

    post = contract._post
    def scaled_post(url, body, token, timeout, headers=None):
        observed.append(('app', timeout))
        return post(url, body, token, timeout * scale, headers)
    monkeypatch.setattr(contract, '_post', scaled_post)

    with serve(AppHandler) as app_url:
        app = SimpleNamespace(id='fixture', url=app_url, token_file=str(token))
        hub = SimpleNamespace(call_app=lambda app_id, tool, args, **kw: contract.call_app(app, tool, args, **kw))

        class HubHandler(Handler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(('hub', self.path, body))
                self.reply({'result': tools.call(hub, body['tool'], body['arguments'])})

        with serve(HubHandler) as hub_url:
            request = mcp._request
            def scaled_request(method, path, body=None, timeout=90):
                observed.append(('mcp', timeout))
                return request(method, path, body, .05 if transport_failure else timeout * scale)
            monkeypatch.setattr(mcp, '_request', scaled_request)
            monkeypatch.setattr(mcp, '_url', lambda: hub_url)
            monkeypatch.setattr(mcp, '_token', lambda: 'private-fixture-token')
            monkeypatch.setattr(mcp, '_ensure_hub', lambda: True)
            result = call(arguments={'app': 'fixture', 'tool': 'blocking_fixture', 'timeout_s': 900})['result']
            assert completed.wait(3)
    assert [r[0] for r in requests] == ['hub', 'app']
    assert ('app', 900) in observed
    if transport_failure:
        assert result['isError'] is True
    else:
        assert result['isError'] is False
        assert json.loads(result['content'][0]['text'])['result'] == {'fixture': 'completed'}


@pytest.mark.parametrize('value,expected', [(None, 135), (900, 915), (9999, 915), (1, 90), (-1, 90), ('900', 915), ('bad', 135)])
def test_appcall_observation_matches_official_tool_budget_and_grace(monkeypatch, value, expected):
    captured = []
    monkeypatch.setattr(mcp, '_ensure_hub', lambda: True)
    def request(method, path, body=None, timeout=90):
        captured.append((body, timeout))
        return 200, {'result': {'ok': True}}
    monkeypatch.setattr(mcp, '_request', request)
    args = {} if value is None else {'timeout_s': value}
    call(arguments=args)
    assert len(captured) == 1 and captured[0][1] == expected
    assert captured[0][0]['arguments'] == args


def test_other_tools_keep_their_default_and_chat_keeps_existing_override(monkeypatch):
    waits = []
    monkeypatch.setattr(mcp, '_ensure_hub', lambda: True)
    monkeypatch.setattr(mcp, '_request', lambda *a, timeout=90: (waits.append(timeout) or 200, {'result': {'ok': True}}))
    call('hub_jobs', {'timeout_s': 900})
    call('hub_link_chat', {'timeout_s': 900})
    assert waits == [90, 915]
