"""Scoped discovery over the standard stdio bridge and the Hub's HTTP routes."""

import json
import os
import sys
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from hoard_link._process import run_hidden
from hoard_link.hub import app_catalogue, agora_faustus
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server

from .conftest import free_port
from .test_hub_and_server import _http


APP = "vitruvius"
ALLOWED = [
    "app_project_create", "app_project_get", "app_project_edit_source", "app_project_canvas_edit",
    "app_project_undo", "app_project_preview", "app_project_canvas_view", "app_project_canvas_act",
    "app_project_selection", "app_project_comments", "app_project_render", "design_lint",
    "design_critique", "tokens_get", "design_brief", "app_project_archive",
]


@pytest.fixture
def app_catalogue_server(tmp_path):
    names = [*ALLOWED, "app_project_list", *[f"fixture_tool_{n}" for n in range(244)]]
    catalogue = {
        "instructions": "Global instructions for all tools. Keep edits in the active project. \U0001f6e0\n" * 700,
        "tools": [{
            "name": name,
            "description": f"Read {name}.\n" + "More details. " * 20,
            "inputSchema": {
                "type": "object", "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "depth": {"type": "number", "description": "Optional details. " * 46},
                },
            },
            "annotations": {"readOnlyHint": True},
        } for name in names],
    }
    upstream = json.dumps(catalogue).encode("utf-8")
    app_reads = []

    class AppHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/agent/tools":
                self.send_error(404)
                return
            app_reads.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(upstream)))
            self.end_headers()
            self.wfile.write(upstream)

        def log_message(self, *args):
            pass

    with ExitStack() as stack:
        app_server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), AppHandler))
        config = HubConfig.load(env={
            "HOARD_HUB_PORT": str(free_port()),
            "HOARD_HUB_DATA_DIR": str(tmp_path / "hub"),
            "FAUSTUS_DIR": str(tmp_path / "faustus"),
        })
        assert config.isolated
        hub = Hub(config)
        # Give the isolated Hub a single synthetic upstream on its allocated loopback port.
        hub._apps[APP] = SimpleNamespace(
            id=APP, url=f"http://127.0.0.1:{app_server.server_port}", token_file="",
        )
        server = stack.enter_context(make_server(hub))
        for listener in (app_server, server):
            thread = Thread(target=listener.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(listener.shutdown)
        yield config, hub, catalogue, app_reads


@pytest.mark.parametrize("tool", [None, "allowed"])
def test_scoped_catalogue_keeps_app_instructions(tool):
    instructions = "Open the project before editing.\nUse the current project ID."
    allowed = {"name": "allowed", "description": "Read.\nDetails.", "inputSchema": {"required": ["id"]}}
    catalogue = {"ok": True, "app": APP, "contract": "shared", "instructions": instructions,
                 "tools": [allowed, {"name": "outside_scope"}]}
    hub = SimpleNamespace(app_tools=lambda _app: catalogue)
    args = app_catalogue.request_arguments({"app": APP, "tool": tool}, json.dumps({APP: ["allowed"]}))

    result = app_catalogue.list_tools(hub, args)

    assert result["instructions"] == instructions
    assert "instructions_truncated" not in result
    assert result["tools"] == ([allowed] if tool else [{"name": "allowed", "description": "Read.",
                                                       "required": ["id"]}])


@pytest.mark.parametrize("tool", [None, "allowed"])
def test_scoped_catalogue_caps_long_app_instructions(tool):
    instructions = "\U0001f6e0" * 12_000
    allowed = {"name": "allowed", "description": "Read."}
    catalogue = {"ok": True, "app": APP, "contract": "shared", "instructions": instructions, "tools": [allowed]}
    hub = SimpleNamespace(app_tools=lambda _app: catalogue)
    args = app_catalogue.request_arguments({"app": APP, "tool": tool}, json.dumps({APP: ["allowed"]}))

    preview = app_catalogue.list_tools(hub, args)
    full = app_catalogue.list_tools(hub, {**args, "include_instructions": True})

    assert preview["instructions"] == instructions[:512]
    assert preview["instructions_truncated"] is True
    assert len(json.dumps(preview).encode("utf-8")) < 20_000
    assert full["instructions"] == instructions
    assert "instructions_truncated" not in full
    assert full["tools"] == preview["tools"]


def test_scoped_worker_uses_compact_catalogue_via_stdio(app_catalogue_server):
    config, _, catalogue, app_reads = app_catalogue_server
    apps = agora_faustus.file_scope("fixture", {"apps": {APP: ALLOWED}})["apps"]
    env = {
        **os.environ, "HOARD_HUB_URL": config.url, "HOARD_HUB_TOKEN_FILE": config.token_file,
        "HOARD_HUB_DATA_DIR": config.data_dir, "HOARD_HUB_AUTOSTART": "0",
        app_catalogue.SCOPE_ENV: json.dumps(apps), "PYTHONUTF8": "1",
    }
    calls = [
        {"app": APP},
        {"app": APP, "summary": False, "_worker_app_scope": {APP: ["app_project_list"]}},
        {"app": APP, "tool": ALLOWED[0]},
        {"app": APP, "include_instructions": True},
        {"app": APP, "tool": "app_project_list", "include_instructions": True},
        {"app": "another_app", "include_instructions": True},
    ]
    incoming = [
        {"jsonrpc": "2.0", "id": index, "method": "tools/call",
         "params": {"name": app_catalogue.NAME, "arguments": args}}
        for index, args in enumerate(calls, start=1)
    ]
    ipc = run_hidden([sys.executable, "-m", "hoard_link.hub.mcp"],
                     input="".join(json.dumps(line) + "\n" for line in incoming),
                     env=env, encoding="utf-8", capture_output=True, check=True, timeout=25)
    wire = [json.loads(line) for line in ipc.stdout.splitlines()]
    assert [row["id"] for row in wire] == [row["id"] for row in incoming]
    result = [row["result"] for row in wire]
    texts = [row["content"][0]["text"] for row in result]
    summary, forced, single, full, denied_tool, denied_app = map(json.loads, texts)
    assert [row["isError"] for row in result] == [False, False, False, False, True, True]
    assert len(texts[0].encode("utf-8")) < 20_000
    assert summary == forced
    assert summary["instructions"] == catalogue["instructions"][:512]
    assert summary["instructions_truncated"] is True
    assert summary["tools"] == [
        {"name": name, "description": f"Read {name}.", "required": ["id"]} for name in ALLOWED
    ]
    assert single["tools"] == catalogue["tools"][:1]
    assert single["instructions"] == summary["instructions"]
    assert single["instructions_truncated"] is True
    assert full["instructions"] == catalogue["instructions"]
    assert "instructions_truncated" not in full
    assert full["tools"] == summary["tools"]
    assert denied_tool["code"] == "worker_app_tool_not_allowed"
    assert denied_app["code"] == "worker_app_not_allowed"
    assert len(app_reads) == 4
    print(f"Scoped MCP {len(summary['tools'])} entries, {len(texts[0].encode('utf-8'))} bytes. "
          f"One full schema: {len(texts[2].encode('utf-8'))} bytes. "
          f"Full instructions on request: {len(full['instructions'].encode('utf-8'))} bytes.")


def test_legacy_and_opt_in_catalogue_over_http(app_catalogue_server):
    config, hub, catalogue, app_reads = app_catalogue_server
    url = f"{config.url}/api/agent/tools/{app_catalogue.NAME}"
    headers = {"Authorization": f"Bearer {hub.token}"}
    status, response = _http(url, {"app": APP}, headers)
    assert status == 200
    legacy = response["result"]
    assert legacy == {"ok": True, "app": APP, "contract": "shared", **catalogue}
    assert len(legacy["tools"]) == 261
    assert len(json.dumps(legacy).encode("utf-8")) > 300_000

    status, response = _http(url, {"app": APP, "summary": True}, headers)
    assert status == 200
    assert [item["name"] for item in response["result"]["tools"]] == [item["name"] for item in catalogue["tools"]]
    assert set(response["result"]["tools"][-1]) == {"name", "description", "required"}

    status, response = _http(url, {"app": APP, "tool": "app_project_list"}, headers)
    assert status == 200
    assert response["result"]["tools"] == [catalogue["tools"][16]]
    status, response = _http(url, {"app": APP, "tool": "unknown"}, headers)
    assert status == 400 and response["result"]["code"] == "unknown_app_tool"

    status, response = _http(url, {"app": APP}, {**headers, app_catalogue.SCOPE_HEADER: "{bad"})
    assert status == 400 and response["result"]["code"] == "worker_app_not_allowed"
    assert len(app_reads) == 4
