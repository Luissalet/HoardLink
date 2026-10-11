"""Actual HTTP relay preserves response status, UTF-8 SSE and native context."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hoard_link.copilot import CopilotClient
from hoard_link.hub.config import HubConfig
from hoard_link.hub.copilot import CopilotProxy
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server
from .conftest import free_port


@pytest.fixture
def relay(tmp_path):
    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path.endswith('/capabilities'):
                data = json.dumps({'contract_version': '1.0'}).encode()
            elif '/resume/' in self.path:
                data = 'data: {"delta":"áudio y capítulo"}\n\ndata: [DONE]\n\n'.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('X-Odysseus-Run-Id', 'same-run')
                self.end_headers()
                for part in (data[:19], data[19:23], data[23:]):
                    self.wfile.write(part)
                    self.wfile.flush()
                return
            else:
                data = json.dumps({'conversations': []}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            received.append({'path': self.path, 'raw': raw, 'caller': self.headers.get('X-Hoard-Caller'), 'type': self.headers.get('Content-Type')})
            if self.path.endswith('/imports'):
                self.send_response(409)
                data = b'{"detail":{"code":"source_conflict"}}'
            else:
                self.send_response(200)
                data = json.dumps({'session_id': 'canonical-one', 'request': json.loads(raw)}).encode()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    upstream = ThreadingHTTPServer(('127.0.0.1', free_port()), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_url = f'http://127.0.0.1:{upstream.server_port}'
    config = HubConfig(port=free_port(), data_dir=str(tmp_path / 'hub'), roots=[], icon_dirs=[], jobs_enabled=False, faustus_urls=[upstream_url])
    hub = Hub(config, gpu_fn=lambda: [])
    hub.copilot = CopilotProxy([upstream_url])
    server = make_server(hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = CopilotClient(f'http://127.0.0.1:{server.server_port}', hub.token)
    yield client, hub.copilot, received
    server.shutdown()
    server.server_close()
    hub.close()
    upstream.shutdown()
    upstream.server_close()


def test_native_conversation_request_and_context_pass_through(relay):
    client, _, received = relay
    result = client.request('/api/hoard-copilot/conversations', method='POST', body={'context': {'origin': {'app_id': 'writer', 'data_space_id': 'qa'}, 'selection': {'text': 'ámbito'}}})
    assert result['session_id'] == 'canonical-one'
    assert result['request']['context']['selection']['text'] == 'ámbito'
    assert received[0]['caller'] == 'hub'


def test_stream_is_utf8_and_has_same_run_identity(relay):
    client, _, _ = relay
    with client.open('/api/chat/resume/sid') as response:
        assert response.headers['X-Odysseus-Run-Id'] == 'same-run'
        assert response.headers['Content-Type'] == 'text/event-stream'
        data = response.read().decode('utf-8')
    assert 'áudio y capítulo' in data and '[DONE]' in data


def test_http_error_is_preserved_and_not_retried(relay):
    client, _, received = relay
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as failure:
        client.request('/api/hoard-copilot/imports', method='POST', body={'messages': []})
    assert failure.value.code == 409
    assert len(received) == 1


def test_application_identity_comes_from_existing_family_token_owner(relay):
    _, proxy, received = relay
    with proxy.open('writer', {'path': '/api/hoard-copilot/conversations', 'method': 'POST', 'body': {'context': {'origin': {'app_id': 'wrong-app', 'data_space_id': 'qa'}}}}) as response:
        data = json.load(response)
    assert data['request']['context']['origin']['app_id'] == 'writer'
    assert received[0]['caller'] == 'writer'


def test_multipart_bytes_and_boundary_remain_exact(relay):
    _, proxy, received = relay
    original = b'--qa\r\nContent-Disposition: form-data; name="message"\r\n\r\ntext\r\n--qa--\r\n'
    # The fixture expects JSON for POST, so exercise transport with its native
    # HTTP-error response which still captures the original request bytes.
    with proxy.open('writer', {'path': '/api/hoard-copilot/imports', 'method': 'POST', 'raw_body': base64.b64encode(original).decode(), 'content_type': 'multipart/form-data; boundary=qa'}) as response:
        assert response.status == 409
    assert received[0]['raw'] == original
    assert received[0]['type'] == 'multipart/form-data; boundary=qa'


@pytest.mark.parametrize('path', ['http://outside.invalid/', '//outside.invalid/', None, '/not-a-copilot-operation'])
def test_invalid_operation_path_is_an_error_without_network(path):
    proxy = CopilotProxy([])
    with pytest.raises(ValueError):
        proxy.open('writer', {'path': path})
