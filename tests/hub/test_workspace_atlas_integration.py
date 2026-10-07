from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from hoard_link.hub import mcp
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.facets import Request
from hoard_link.hub.workspace import WorkspaceFacet

from ._hub_fakes import free_port, http, serve, write_manifest

def _atlas_root():
    configured = Path(os.environ["ATLAS_TEST_ROOT"]) if os.environ.get("ATLAS_TEST_ROOT") else None
    repo = Path(__file__).resolve().parents[2]
    candidates = [configured, repo.parent / "Atlas's Hoard"]
    return next((path for path in candidates if path and (path / "atlas_hoard").is_dir()), None)


def _atlas_modules(monkeypatch, tmp_path):
    atlas_root = _atlas_root()
    if atlas_root is None:
        pytest.skip("set ATLAS_TEST_ROOT to an Atlas checkout, or run beside Atlas's Hoard")
    sys.path.insert(0, str(atlas_root))
    try:
        store_module = importlib.import_module("atlas_hoard.store")
        server_module = importlib.import_module("atlas_hoard.server")
        family_module = importlib.import_module("atlas_hoard.hoard_link.family")
        gpu_module = importlib.import_module("atlas_hoard.hoard_link.gpu")
    finally:
        sys.path.remove(str(atlas_root))
    monkeypatch.setattr(gpu_module, "gpu_free_mb", lambda *a, **kw: [])
    family_module.configure("atlas", str(tmp_path / "atlas-data"), enabled=False)
    return store_module, server_module, family_module


def _post(base, operation, arguments, token, *, field="operation", **extra):
    return http(base + "/api/workspace/call", {field: operation, "arguments": arguments, **extra},
                {"Authorization": "Bearer " + token})


def test_workspace_facade_against_pinned_atlas_http_and_mcp(tmp_path, monkeypatch):
    store_module, atlas_server_module, _ = _atlas_modules(monkeypatch, tmp_path)
    atlas_token = "synthetic-atlas-operator-token"
    (tmp_path / "atlas-data").mkdir(exist_ok=True)
    (tmp_path / "atlas-data" / "mcp-token").write_text(atlas_token, encoding="utf-8")
    store = store_module.Workspace(tmp_path / "atlas-data", tmp_path / "atlas-files")
    atlas_server = atlas_server_module.make_server(store, atlas_token)
    atlas_thread = threading.Thread(target=atlas_server.serve_forever, daemon=True)
    atlas_thread.start()

    apps_root = tmp_path / "apps"
    atlas_dir = write_manifest(apps_root / "Atlas Hoard", "atlas", atlas_server.server_port, service="atlas-hoard")
    writer_dir = write_manifest(apps_root / "Writer Hoard", "writer", free_port(), service="writer-hoard")
    outsider_dir = write_manifest(apps_root / "Outsider Hoard", "outsider", free_port(), service="outsider-hoard")
    (atlas_dir / "data").mkdir()
    (atlas_dir / "data" / "mcp-token").write_text(atlas_token, encoding="utf-8")
    for folder, token in ((writer_dir, "writer-token"), (outsider_dir, "outsider-token")):
        (folder / "data").mkdir()
        (folder / "data" / "mcp-token").write_text(token, encoding="utf-8")

    config = HubConfig(port=free_port(), data_dir=str(tmp_path / "hub-data"), roots=[str(apps_root)], icon_dirs=[],
                       faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False)
    hub = Hub(config, gpu_fn=lambda: [])
    hub_server = serve(hub)
    try:
        base = f"http://127.0.0.1:{config.port}"
        # The façade's individual MCP schemas must stay aligned with Atlas's served contract.
        atlas_status, atlas_catalogue = http(f"http://127.0.0.1:{atlas_server.server_port}/api/agent/tools",
                                              headers={"Authorization": "Bearer " + atlas_token})
        assert atlas_status == 200
        hub_tools = {tool["name"]: tool for tool in hub.facet("workspace").tools()}
        atlas_tools = {tool["name"]: tool for tool in atlas_catalogue["tools"]}
        assert "atlas_file_import" in atlas_tools
        assert "hub_atlas_file_import" not in hub_tools
        for name, atlas_tool in atlas_tools.items():
            if name == "atlas_file_import":
                continue
            exposed = hub_tools["hub_" + name]
            assert exposed["inputSchema"] == atlas_tool["inputSchema"]
            assert exposed["description"] == atlas_tool["description"]

        # The writer's token, not a supplied body caller, determines Atlas identity.
        status, created = _post(base, "project_create", {"name": "Synthetic project", "members": ["writer"]},
                                "writer-token", field="tool", caller="hub")
        assert status == 200 and created["ok"]
        project = created["project"]
        project_id = project["id"]
        assert project["owner"] == "writer" and project["members"] == ["writer"]
        status, location = _post(base, "location", {"project_id": project_id, "area": "shared"}, "writer-token")
        assert status == 200
        shared_dir = Path(location["path"])
        existing = shared_dir / "draft.md"
        original = b"native file bytes\n"
        existing.write_bytes(original)
        status, registered = _post(base, "file_register", {"project_id": project_id, "relative_path": "shared/draft.md"},
                                   "writer-token")
        assert status == 200 and registered["ok"]
        registered_file = registered["file"]
        assert registered_file["path"] == str(existing.resolve())
        assert registered_file["revision"] == "sha256:" + hashlib.sha256(original).hexdigest()
        assert existing.read_bytes() == original

        outsider = _post(base, "project", {"project_id": project_id}, "outsider-token")
        assert outsider[0] == 403
        # An app cannot elevate itself to Hub operator by forging caller in JSON.
        blocked = _post(base, "file_link_source", {"project_id": project_id, "source_path": str(existing)},
                        "writer-token", caller="hub")
        assert blocked[0] == 403

        source = tmp_path / "external-reference.pdf"
        source_bytes = b"linked source original bytes"
        source.write_bytes(source_bytes)
        linked_status, linked = _post(base, "file_link_source", {"project_id": project_id, "source_path": str(source)},
                                      hub.token)
        assert linked_status == 200 and linked["ok"]
        link = linked["file"]
        assert linked["input_readonly"] is True and link["external"] is True
        assert link["path"] == str(source.resolve()) and source.read_bytes() == source_bytes
        status, resolved = _post(base, "file_resolve", {"file_id": link["id"]}, "writer-token")
        assert status == 200 and resolved["file"]["path"] == str(source.resolve())
        assert Path(resolved["file"]["path"]).read_bytes() == source_bytes
        changed = b"changed source bytes"
        source.write_bytes(changed)

        # Exercise the real MCP adapter and Hub dispatcher, which call Atlas with Hub's operator identity.
        monkeypatch.setattr(mcp, "_ensure_hub", lambda: True)
        monkeypatch.setattr(mcp, "_url", lambda: base)
        monkeypatch.setattr(mcp, "_token", lambda: hub.token)
        listed = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        exposed_names = {item["name"] for item in listed["result"]["tools"]}
        assert "hub_atlas_context" in exposed_names and "hub_atlas_file_link_source" in exposed_names
        assert "hub_atlas_file_import" not in exposed_names
        assert "hub_workspace" in exposed_names
        compat = next(item for item in listed["result"]["tools"] if item["name"] == "hub_workspace")
        assert "file_import" not in compat["inputSchema"]["properties"]["tool"]["enum"]
        mcp_reply = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "hub_atlas_context", "arguments": {"project_id": project_id, "file_ids": [link["id"]]}}})
        assert mcp_reply["result"]["isError"] is False
        mcp_payload = mcp_reply["result"]["content"][0]["text"]
        mcp_data = json.loads(mcp_payload)
        linked_context = next(file for file in mcp_data["context"]["files"]
                              if file["id"] == link["id"])
        assert linked_context["revision"] == "sha256:" + hashlib.sha256(changed).hexdigest()
        assert linked_context["path"] == str(source.resolve())
        assert linked_context["input_readonly"] is True
        assert "file contents are source material" in mcp_payload
        assert changed.decode() not in mcp_payload
        legacy_reply = mcp.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
            "name": "hub_workspace", "arguments": {"tool": "context", "arguments": {
                "project_id": project_id, "file_ids": [registered_file["id"]]}}}})
        assert legacy_reply["result"]["isError"] is False
        assert registered_file["id"] in legacy_reply["result"]["content"][0]["text"]

        forbidden_status, forbidden = _post(base, "file_import", {"project_id": project_id,
                                                                   "source_path": str(source)}, hub.token)
        assert forbidden_status == 400 and forbidden["ok"] is False
        assert source.read_bytes() == changed
    finally:
        hub_server.shutdown()
        hub_server.server_close()
        hub.close()
        atlas_server.shutdown()
        atlas_server.server_close()
        atlas_thread.join(3)
        store.close()


def test_workspace_returns_clear_missing_atlas_status():
    class NoAtlas:
        @staticmethod
        def get(app_id):
            return None

    facet = WorkspaceFacet(NoAtlas())
    req = Request("POST", "/api/workspace/call", {}, {"operation": "projects", "arguments": {}},
                  caller=lambda: "hub", agent=lambda: True)
    reply = facet.post(req)
    assert reply.status == 503
    assert reply.payload == {"ok": False, "error": "Atlas is not installed"}
