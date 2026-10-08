"""Real Atlas and Hub on loopback; stand-ins represent two consumer identities.

Run against the sibling Atlas checkout. No user data, model or live app involved.
"""
import json
from pathlib import Path
import sys
import threading
import shutil
import subprocess

import pytest

ATLAS = Path(__file__).resolve().parents[2].parent / "Atlas's Hoard"
if not ATLAS.is_dir():
    pytest.skip("Atlas sibling checkout is required for ecosystem integration", allow_module_level=True)
sys.path.insert(0, str(ATLAS))
from atlas_hoard.store import Workspace
from atlas_hoard.server import make_server
from atlas_hoard.hoard_link import family
from ._hub_fakes import FakeApp, make_hub, serve, http


@pytest.fixture
def ecosystem(tmp_path, monkeypatch):
    monkeypatch.setitem(family._state, "enabled", False)
    store = Workspace(tmp_path / "private", tmp_path / "ssd")
    atlas = make_server(store, "atlas-operator")
    thread = threading.Thread(target=atlas.serve_forever, daemon=True)
    thread.start()
    writer, lumiere = FakeApp("writer"), FakeApp("lumiere")
    from types import SimpleNamespace
    atlas_identity = SimpleNamespace(app_id="atlas", port=atlas.server_port, token="atlas-operator")
    hub = make_hub(tmp_path, [atlas_identity, writer, lumiere])
    hub_server = serve(hub)
    def call(tool, arguments=None, caller="hub", **extra):
        token = hub.token if caller == "hub" else f"token-{caller}"
        code, body = http(hub.config.url + "/api/workspace/call",
            {"tool": tool, "arguments": arguments or {}, **extra}, {"Authorization": "Bearer " + token})
        return code, body
    yield store, hub, call
    hub_server.shutdown()
    hub_server.server_close()
    hub.close()
    writer.stop()
    lumiere.stop()
    atlas.shutdown()
    atlas.server_close()
    thread.join(3)
    store.close()


def test_same_live_file_and_cache_between_two_authenticated_consumers(ecosystem):
    store, hub, call = ecosystem
    code, created = call("project_create", {"name": "Integration fixture", "owner": "writer", "members": ["lumiere"]})
    assert code == 200 and created["ok"]
    project = created["project"]
    path = Path(project["shared_path"]) / "portada.png"
    path.write_bytes(b"synthetic initial PNG content")
    file = call("file_register", {"project_id": project["id"], "relative_path": "shared/portada.png"}, "writer")[1]["file"]
    first = call("file_resolve", {"file_id": file["id"]}, "writer")[1]["file"]
    second = call("file_resolve", {"file_id": file["id"]}, "lumiere")[1]["file"]
    assert Path(first["path"]) == Path(second["path"]) == path
    Path(second["path"]).write_bytes(b"edited by another native application")
    changed = call("file_resolve", {"file_id": file["id"]}, "writer")[1]["file"]
    assert changed["revision"] != first["revision"]
    assert Path(first["path"]).read_bytes() == b"edited by another native application"
    recipe = {"operation": "fixture-extraction", "version": "1", "model_revision": "synthetic"}
    args = {"project_id": project["id"], "source_ids": [file["id"]], "recipe": recipe}
    miss = call("derived_lookup", args, "writer")[1]
    output = Path(project["shared_path"]) / "result.json"
    output.write_text(json.dumps({"fixture": "extraction result"}), encoding="utf-8")
    result_file = call("file_register", {"project_id": project["id"], "relative_path": "shared/result.json"}, "writer")[1]["file"]
    assert call("derived_publish", {**args, "output_id": result_file["id"],
        "source_revisions": {s["id"]: s["revision"] for s in miss["sources"]}}, "writer")[0] == 200
    hit = call("derived_lookup", args, "lumiere")[1]
    assert hit["hit"] and Path(hit["file"]["path"]) == output
    assert len(list(Path(project["path"]).rglob("portada.png"))) == 1
    path.write_bytes(b"another source edit")
    assert call("derived_lookup", args, "lumiere")[1]["hit"] is False
    output.write_text("human revision", encoding="utf-8")
    assert output.read_text(encoding="utf-8") == "human revision"


def test_hub_authentication_does_not_allow_forged_membership(ecosystem):
    store, hub, call = ecosystem
    private = call("project_create", {"name": "Private fixture", "owner": "writer"})[1]["project"]
    code, denied = call("project", {"project_id": private["id"]}, "lumiere", caller_identity="hub")
    # Passing arbitrary body identity is tested separately below; the token
    # always determines the effective caller at the Hub boundary.
    assert code == 403 and not denied["ok"]
    code, _ = http(hub.config.url + "/api/workspace/call", {"tool": "project", "arguments": {"project_id": private["id"]}, "caller": "hub"},
                   {"Authorization": "Bearer token-lumiere"})
    assert code == 403
    assert http(hub.config.url + "/api/workspace/projects")[0] == 401


def test_backup_contains_shared_disk_and_restore_keeps_live_original(ecosystem, tmp_path):
    store, hub, call = ecosystem
    atlas_data = Path(hub.get("atlas").data_dir)
    (atlas_data / "storage.json").write_text(json.dumps({"root": str(store.root)}), encoding="utf-8")
    project = call("project_create", {"name": "Backup fixture", "owner": "writer"})[1]["project"]
    original = Path(project["shared_path"]) / "native-project.txt"
    original.write_text("before backup", encoding="utf-8")
    snapshot = hub.backup_run(["atlas"], "isolated fixture")
    assert snapshot["ok"]
    manifest = hub.backups.load_snapshot(snapshot["snapshot"])
    assert {"atlas", "atlas-files"} <= manifest["apps"].keys()
    original.write_text("ongoing native edit", encoding="utf-8")
    assert not hub.backup_restore(snapshot["snapshot"], "atlas-files", in_place=True)["ok"]
    restored = hub.backup_restore(snapshot["snapshot"], "atlas-files", dest=str(tmp_path / "review-restore"))
    assert restored["ok"]
    assert original.read_text(encoding="utf-8") == "ongoing native edit"
    recovered = Path(restored["dest"]) / original.relative_to(store.root)
    assert recovered.read_text(encoding="utf-8") == "before backup"


def test_python_and_node_clients_resolve_the_same_live_path(ecosystem, monkeypatch):
    store, hub, call = ecosystem
    project = call("project_create", {"name": "Client parity fixture", "owner": "writer", "members": ["lumiere"]})[1]["project"]
    path = Path(project["shared_path"]) / "fixture.txt"
    path.write_text("one original", encoding="utf-8")
    file = call("file_register", {"project_id": project["id"], "relative_path": "shared/fixture.txt"}, "writer")[1]["file"]
    from hoard_link import family as client_family, fam_workspace
    monkeypatch.setattr(client_family, "_state", dict(client_family._state))
    client_family.configure("lumiere", hub.get("lumiere").data_dir, hub=hub.config.url, enabled=True)
    python = fam_workspace.resolve(file["id"])
    assert python["ok"] and Path(python["file"]["path"]) == path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for cross-language live client verification")
    config = json.dumps({"app": "writer", "dataDir": hub.get("writer").data_dir, "hub": hub.config.url})
    script = "import {configure} from './js/hoard-link.js'; import {workspaceResolve} from './js/hoard-commons/fam-workspace.js'; "
    script += f"configure({config}); console.log(JSON.stringify(await workspaceResolve({json.dumps(file['id'])})));"
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=15, check=True)
    javascript = json.loads(result.stdout)
    assert javascript["ok"] and Path(javascript["file"]["path"]) == path
    assert javascript["file"]["revision"] == python["file"]["revision"]
