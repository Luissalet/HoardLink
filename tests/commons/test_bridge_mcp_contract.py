"""Real SDK stdio -> private HTTP. Run under MCP1.9,1.30 and2.x separately."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

mcp = pytest.importorskip('mcp')
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
SDK = version('mcp')
MODERN = SDK != '1.9.0'  # Explicit matrix expectation, not product feature detection.
PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='

@contextmanager
def private_api():
    calls, entered, finished = [], threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, body, status=200):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        def do_GET(self):
            if self.path == '/api/health': return self.reply({'service': 'contract860'})
            tools = [{'name': name, 'description': name,
                      'inputSchema': {'type': 'object', 'properties': {'value': {}}, 'additionalProperties': False},
                      'x-timeout-s': 900, 'annotations': {'readOnlyHint': name != 'slow_write'}}
                     for name in ('echo', 'image', 'error', 'slow', 'slow_write', 'cancel')]
            self.reply({'tools': tools})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            name = body['name']
            entered.set()
            if name in ('slow', 'slow_write', 'cancel'): time.sleep(.25)
            if name == 'error': self.reply({'error': 'fixture refused', 'code': 'fixture_error'}, 400)
            elif name == 'image': self.reply({'value': 'pictured', '_image': {'data': PNG, 'mime': 'image/png'}})
            else: self.reply({'echo': body['arguments']})
            finished.set()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield server.server_port, calls, entered, finished
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

@asynccontextmanager
async def native_session(tmp_path, port, overrides=None):
    token = tmp_path / 'mcp-token'
    token.write_text('synthetic-contract860-token')
    code = f'''
import socket,sys
from pathlib import Path
sys.path.insert(0,{str(ROOT)!r})
from hoard_link.bridge import CatalogBridge
import hoard_link.bridge as module
assert Path(module.__file__).resolve()==Path({str(ROOT / 'hoard_link/bridge.py')!r}).resolve()
original=socket.socket.connect
def connect(sock,address):
    if sys._getframe(1).f_code is socket._fallback_socketpair.__code__:return original(sock,address)
    if isinstance(address,tuple) and (address[0] not in ('127.0.0.1','localhost','::1') or address[1]!={port}):
        raise ConnectionRefusedError('Only the private contract860 API is permitted')
    return original(sock,address)
socket.socket.connect=connect
CatalogBridge(app='contract860',service='contract860',package='fixture',default_port={port},
 base_url='http://127.0.0.1:{port}',token_file={str(token)!r},autostart=False,
 default_timeout=.08,tool_timeouts={overrides or {}!r},heartbeat_s=.02,image_content=True).run_bridge()
'''
    env = {**os.environ, 'HOARD_HUB_AUTOSTART': '0', 'HOARD_EVENTS': '0', 'HOARD_NOTIFY_DRY_RUN': '1',
           'PYTHONDONTWRITEBYTECODE': '1'}
    params = StdioServerParameters(command=sys.executable, args=['-B', '-c', code], cwd=str(ROOT), env=env)
    async with asyncio.timeout(15):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

def wire(result): return result.model_dump(by_alias=True, exclude_none=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['echo', 'image', 'error'])
async def test_native_result_preserves_text_images_error_and_supported_structure(tmp_path, name):
    with private_api() as (port, calls, _, __):
        async with native_session(tmp_path, port) as session:
            tools = [wire(tool) for tool in (await session.list_tools()).tools]
            entry = next(tool for tool in tools if tool['name'] == name)
            assert entry['inputSchema']['x-timeout-s'] == 900
            if SDK.startswith('1.'): assert entry['x-timeout-s'] == 900
            result = wire(await session.call_tool(name, {'value': 'Unicode revisión'}))
            assert result.get('isError', False) is (name == 'error'), result
            text = json.loads(result['content'][0]['text'])
            if name == 'error': assert text == {'error': 'fixture refused', 'code': 'fixture_error'}
            elif name == 'image':
                assert text == {'value': 'pictured'}
                assert result['content'][1] == {'type': 'image', 'data': PNG, 'mimeType': 'image/png'}
            else: assert text == {'echo': {'value': 'Unicode revisión'}}
            if MODERN: assert result['structuredContent'] == text
            else: assert 'structuredContent' not in result
        assert [call['name'] for call in calls] == [name]

@pytest.mark.asyncio
async def test_native_declared900_outlives_short_default(tmp_path):
    with private_api() as (port, calls, _, __):
        async with native_session(tmp_path, port) as session:
            result = wire(await session.call_tool('slow', {}))
            assert not result.get('isError') and json.loads(result['content'][0]['text']) == {'echo': {}}
        assert [call['name'] for call in calls] == ['slow']

@pytest.mark.asyncio
async def test_native_explicit_write_cap_is_unknown_never_replayed(tmp_path):
    with private_api() as (port, calls, _, finished):
        async with native_session(tmp_path, port, {'slow_write': .08}) as session:
            result = wire(await session.call_tool('slow_write', {}))
            assert result['isError'] is True
            body = json.loads(result['content'][0]['text'])
            assert body['outcome_unknown'] is True and body['reconcile_action'] == 'read_current_state_before_retry'
            assert await asyncio.to_thread(finished.wait, 2)
        assert [call['name'] for call in calls] == ['slow_write']

@pytest.mark.asyncio
async def test_native_client_cancellation_keeps_session_usable_without_replay(tmp_path):
    with private_api() as (port, calls, entered, finished):
        async with native_session(tmp_path, port) as session:
            task = asyncio.create_task(session.call_tool('cancel', {}))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert await asyncio.to_thread(finished.wait, 2)
            result = wire(await session.call_tool('echo', {'value': 'after cancel'}))
            assert not result.get('isError')
            assert json.loads(result['content'][0]['text']) == {'echo': {'value': 'after cancel'}}
        assert [call['name'] for call in calls] == ['cancel', 'echo']
