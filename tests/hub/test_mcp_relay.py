"""Real stdio children and an SDK client over the Hub's normal HTTP server."""
import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

from hoard_link.hub import registry
from hoard_link.hub.mcp_relay import McpRelayFacet, launch_spec
from hoard_link.hub.server import make_server


BRIDGE = '''import json, os, sys
def send(message):
    print(json.dumps({"jsonrpc":"2.0", **message}), flush=True)
pending = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    ident = message.get("id")
    if method == "initialize":
        send({"id":ident, "result":{"protocolVersion":message["params"]["protocolVersion"], "capabilities":{"tools":{"listChanged":True}, "prompts":{}, "resources":{}}, "serverInfo":{"name":"fixture", "version":"1"}, "instructions":"Read the app skills before acting"}})
    elif method == "tools/list":
        send({"id":ident, "result":{"tools":[{"name":"read", "description":"Read fixture", "inputSchema":{"type":"object"}, "annotations":{"readOnlyHint":True}}]}})
    elif method == "prompts/list":
        send({"id":ident, "result":{"prompts":[{"name":"skills", "description":"App procedures"}]}})
    elif method == "resources/list":
        send({"id":ident, "result":{"resources":[]}})
    elif method == "tools/call":
        if message["params"].get("arguments", {}).get("exit"):
            sys.exit(2)
        pending = ident
        send({"method":"notifications/tools/list_changed"})
        send({"id":"server-ping", "method":"ping"})
    elif ident == "server-ping":
        send({"id":pending, "result":{"content":[{"type":"text", "text":"read from PC"}], "structuredContent":{"cwd":os.getcwd(), "url":os.environ["APP_ENDPOINT"], "token_file":os.environ["APP_TOKEN_FILE"]}, "isError":False}})
    elif method == "ping":
        send({"id":ident, "result":{}})
'''


@pytest.fixture
def relay(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text(BRIDGE, encoding="utf-8")
    manifest = {"schema": 1, "id": "fixture", "name": "Fixture", "defaults": {"TOKEN_FILE": "{FIXTURE_DIR}/data/token"},
                "app": {"url_default": "http://127.0.0.1:59381", "ui_url": "{APP_URL}"},
                "mcp": {"transport": "stdio", "command": sys.executable, "args": ["{FIXTURE_DIR}/bridge.py"],
                        "env": {"APP_ENDPOINT": "{APP_URL}", "APP_TOKEN_FILE": "{TOKEN_FILE}"}}}
    path = tmp_path / "faustus-plugin.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    app = registry.read_manifest(path)
    from hoard_link.hub.config import HubConfig
    hub = SimpleNamespace(config=HubConfig(port=0, data_dir=str(tmp_path / "hub"), faustus_python=sys.executable),
                          token="hub-secret", apps=[app], get=lambda ident: app if ident == app.id else None)
    facet = McpRelayFacet(hub)
    hub.facets = [facet]
    hub.facet = lambda ident: facet if ident == facet.id else None
    hub.token_owner = lambda value: "hub" if value == hub.token else None
    server = make_server(hub, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", facet=facet, app=app, root=tmp_path, hub=hub)
    facet.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_manifest_resolution_uses_installed_pc_paths_and_assigned_port(relay):
    relay.app.url = "http://127.0.0.1:59382"
    argv, env = launch_spec(relay.hub, relay.app)
    assert argv == [sys.executable, str(relay.root) + "/bridge.py"]
    assert env["APP_ENDPOINT"] == relay.app.url
    assert env["APP_TOKEN_FILE"] == relay.app.token_file


def test_metadata_requires_existing_hub_credential_and_never_starts_child(relay):
    with httpx.Client(trust_env=False) as client:
        assert client.get(relay.url + "/api/mcp/apps/fixture").status_code == 401
        response = client.get(relay.url + "/api/mcp/apps/fixture", headers={"Authorization": "Bearer hub-secret"})
        assert response.status_code == 200
        assert response.json()["app_url"] == relay.app.url
        assert "token" not in response.text
        assert relay.facet._sessions == {}
        own = client.get(relay.url + "/api/mcp/apps/hoardhub", headers={"Authorization": "Bearer hub-secret"})
        assert own.status_code == 200
        assert own.json()["app_url"] == relay.hub.config.url


def test_legacy_resolution_matches_only_installed_manifest(relay):
    argv, _ = launch_spec(relay.hub, relay.app)
    with httpx.Client(trust_env=False) as client:
        headers = {"Authorization": "Bearer hub-secret"}
        payload = {"command": argv[0], "args": argv[1:]}
        response = client.post(relay.url + "/api/mcp/resolve", json=payload, headers=headers)
        assert response.json()["app"]["id"] == "fixture"
        payload["args"] = ["someone-elses-server.py"]
        assert client.post(relay.url + "/api/mcp/resolve", json=payload, headers=headers).json() == {"app": None}
        assert client.post(relay.url + "/api/mcp/resolve", json=payload).status_code == 401
    assert relay.facet._sessions == {}


@pytest.mark.asyncio
async def test_sdk_initialization_tools_skills_server_ping_and_cleanup(relay):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(relay.url + "/api/mcp/apps/fixture/mcp", headers={"Authorization": "Bearer hub-secret"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.instructions == "Read the app skills before acting"
            assert (await session.list_tools()).tools[0].annotations.readOnlyHint is True
            assert (await session.list_prompts()).prompts[0].name == "skills"
            assert (await session.list_resources()).resources == []
            child = next(iter(relay.facet._sessions.values()))
            result = await asyncio.wait_for(session.call_tool("read", {}), timeout=10)
            assert result.content[0].text == "read from PC"
            assert result.structuredContent["url"] == relay.app.url
            assert result.structuredContent["token_file"] == relay.app.token_file
            assert str(relay.root).lower() == result.structuredContent["cwd"].lower()
            assert next(iter(relay.facet._sessions.values())) is child
    assert relay.facet._sessions == {}
    assert child.process.poll() is not None


@pytest.mark.asyncio
async def test_child_exit_unblocks_inflight_request(relay):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(relay.url + "/api/mcp/apps/fixture/mcp", headers={"Authorization": "Bearer hub-secret"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            from mcp.shared.exceptions import McpError
            with pytest.raises(McpError, match="bridge exited"):
                await asyncio.wait_for(session.call_tool("read", {"exit": True}), timeout=5)
    assert relay.facet._sessions == {}


def test_missing_session_auth_and_cross_site_requests(relay):
    endpoint = relay.url + "/api/mcp/apps/fixture/mcp"
    with httpx.Client(trust_env=False) as client:
        for _ in range(10):
            unauthorized = client.post(endpoint, json={})
            assert unauthorized.status_code == 401
        headers = {"Authorization": "Bearer hub-secret"}
        assert client.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 404
        assert client.delete(endpoint, headers=headers).status_code == 404
        assert client.post(endpoint, headers={**headers, "Sec-Fetch-Site": "cross-site"}, json={}).status_code == 403
    assert relay.facet._sessions == {}


def test_document_sized_messages_reuse_the_session(relay):
    endpoint = relay.url + "/api/mcp/apps/fixture/mcp"
    headers = {"Authorization": "Bearer hub-secret"}
    with httpx.Client(trust_env=False) as client:
        response = client.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
        assert response.status_code == 200
        headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        try:
            response = client.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "ping",
                "params": {"document": "x" * (5 * 1024 * 1024)}})
            assert response.json() == {"jsonrpc": "2.0", "id": 2, "result": {}}
            assert len(relay.facet._sessions) == 1
            wrong_app = client.post(relay.url + "/api/mcp/apps/another/mcp", headers=headers,
                json={"jsonrpc": "2.0", "id": 3, "method": "ping"})
            assert wrong_app.status_code == 404
        finally:
            assert client.delete(endpoint, headers=headers).status_code == 200
    assert relay.facet._sessions == {}
