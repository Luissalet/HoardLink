"""The reusable host relay streams native responses without owning an agent."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hoard_link.copilot import make_copilot_router


@pytest.fixture
def host(tmp_path):
    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"contract_version":"1.0"}')
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            received.append((self.path, raw, self.headers.get('Content-Type')))
            self.send_response(409 if self.path.endswith('/imports') else 200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('X-Odysseus-Run-Id', 'native-run')
            self.end_headers()
            self.wfile.write('data: {"delta":"Capítulo y ámbito"}\n\ndata: [DONE]\n\n'.encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    app = FastAPI()
    app.include_router(make_copilot_router('cicero', tmp_path, faustus_urls=[url]))
    with TestClient(app) as client:
        yield client, received, url
    server.shutdown()
    server.server_close()


def test_host_identity_is_stable_and_settings_open_faustus(host):
    client, _, url = host
    first = client.get('/api/copilot/info').json()
    assert first['data_space_id'] == client.get('/api/copilot/info').json()['data_space_id']
    assert first['settings_url'] == url + '/studio/settings'
    assert 'token' not in first


def test_host_stream_and_native_context(host):
    client, received, _ = host
    with client.stream('POST', '/api/copilot/proxy', json={
        'path': '/api/chat_stream', 'method': 'POST',
        'body': {'hoard_context': {'origin': {'app_id': 'wrong', 'data_space_id': 'wrong'},
                                   'selection': {'text': 'ámbito'}}}}) as response:
        assert response.status_code == 200
        assert response.headers['x-odysseus-run-id'] == 'native-run'
        assert 'Capítulo y ámbito' in ''.join(response.iter_text())
    context = json.loads(received[0][1])['hoard_context']
    assert context['origin']['app_id'] == 'cicero'
    assert context['origin']['data_space_id'] != 'wrong'
    assert context['selection']['text'] == 'ámbito'


def test_host_preserves_raw_multipart_and_upstream_error(host):
    import base64
    client, received, _ = host
    raw = b'--qa\r\nContent-Disposition: form-data; name="file"\r\n\r\n\x00\xff\r\n--qa--\r\n'
    response = client.post('/api/copilot/proxy', json={
        'path': '/api/hoard-copilot/imports', 'method': 'POST',
        'raw_body': base64.b64encode(raw).decode(), 'content_type': 'multipart/form-data; boundary=qa'})
    assert response.status_code == 409
    assert len(received) == 1
    assert received[0][1] == raw
    assert received[0][2] == 'multipart/form-data; boundary=qa'


def test_host_multipart_uses_the_same_native_namespace_as_json(host):
    import base64
    client, received, _ = host
    info = client.get('/api/copilot/info').json()
    raw = b'--qa\r\nContent-Disposition: form-data; name="hoard_context"\r\n\r\n{"origin":{"app_id":"old-app","data_space_id":"old-space"},"selection":{"text":"keep"}}\r\n--qa\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n\r\n\x00\xff\r\n--qa--\r\n'
    response = client.post('/api/copilot/proxy', json={'path':'/api/chat_stream', 'method':'POST', 'raw_body':base64.b64encode(raw).decode(), 'content_type':'multipart/form-data; boundary=qa'})
    assert response.status_code == 200
    sent = received[0][1]
    context = json.loads(sent.split(b'\r\n\r\n')[1].split(b'\r\n--qa')[0])
    assert context['origin'] == {'app_id':'cicero', 'data_space_id':info['data_space_id']}
    assert context['selection']['text'] == 'keep'
    assert sent.split(b'name="file"')[1] == raw.split(b'name="file"')[1]
