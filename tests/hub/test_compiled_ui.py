from __future__ import annotations

from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace

import pytest

from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.compiled_ui import AppUI, CompiledUIFacet, RECEIPT, _changes, _detect, _status


INITIAL = 1_700_000_000
INDEX = b'<html><head><script type="module" src="/assets/app.js"></script></head></html>'


def git(root, *args, date=INITIAL):
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{date} +0000", "GIT_AUTHOR_DATE": f"{date} +0000"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def commit(root: Path, changes: dict[str, bytes], date: int) -> str:
    for filename, data in changes.items():
        write(root / filename, data)
    git(root, "add", "--", *changes)
    git(root, "commit", "-m", f"Change at {date}", date=date)
    return git(root, "rev-parse", "HEAD")


def build(folder: Path, body: bytes, stamp: int):
    write(folder / "index.html", INDEX)
    write(folder / "assets/app.js", body)
    os.utime(folder / "index.html", (stamp, stamp))


@pytest.fixture
def demo(tmp_path):
    repo = tmp_path / "Demo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test author")
    git(repo, "config", "user.email", "test@example.invalid")
    original = commit(repo, {
        ".gitignore": b"/product/static/\n",
        "vite.config.js": b"export default { root: 'client', build: {outDir: '../product/static'} };",
        "client/src/App.jsx": b"initial",
    }, INITIAL)
    served = tmp_path / "served"
    served.mkdir()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=served, **kwargs)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        build(repo / "product/static", b"initial", INITIAL + 10)
        build(served, b"initial", INITIAL + 10)
        yield SimpleNamespace(repo=repo, original=original, served=served, url=url, output=repo / "product/static",
                              app=AppUI("demo", "Demo", repo, (url,)))
        server.shutdown()
        worker.join()


def test_declared_vite_layout_and_real_http_match(demo):
    row = _status(demo.app)
    assert row["state"] == "current"
    assert row["output"] == "product/static"
    assert row["source_commit"] == demo.original
    assert row["built_at"] == INITIAL + 10
    assert row["basis"] == "file_date"
    assert row["served_entry_sha256"] == sha256(INDEX).hexdigest()
    assert len(row["fingerprint"]) == 64

    # A stable HTML entry can still point to an old JS response.
    build(demo.output, b"fresh", INITIAL + 20)
    stale = _status(demo.app)
    assert stale["served"] == "mismatch"
    assert stale["state"] == "unverified"
    assert stale["fingerprint"] != row["fingerprint"]


def test_missing_unknown_and_tracked_artifacts(demo):
    (demo.output / "index.html").unlink()
    assert _status(demo.app)["state"] == "missing"
    assert _status(AppUI("none", "None", None, ()))["state"] == "not_declared"
    assert _status(AppUI("none", "None", demo.repo, (), False))["state"] == "no_compiled_ui"

    commit(demo.repo, {".gitignore": b"# Output kept in Git\n"}, INITIAL + 20)
    assert _status(demo.app)["state"] == "no_compiled_ui"
    commit(demo.repo, {"vite.config.js": b"export default {build: {outDir: env.OUTPUT}};"}, INITIAL + 30)
    assert _status(demo.app)["reason"] == "dynamic_config"


def test_timestamp_comparison_includes_older_topic_commits_merged_after_build(demo):
    git(demo.repo, "switch", "-c", "delivery")
    first = commit(demo.repo, {"client/src/App.jsx": b"first"}, INITIAL + 2)
    last = commit(demo.repo, {"client/src/App.jsx": b"second"}, INITIAL + 3)
    git(demo.repo, "switch", "main")
    git(demo.repo, "merge", "--no-ff", "delivery", date=INITIAL + 20)
    merge = git(demo.repo, "rev-parse", "HEAD")
    assert _changes(demo.repo, merge, _detect(demo.app).inputs)
    row = _status(demo.app)
    assert row["state"] == "behind"
    assert row["behind_by"] == 2
    assert row["behind_since"] == INITIAL + 2
    assert row["source_commit"] == last and row["source_commit"] != first


def test_receipt_overrides_file_date_and_rejects_changed_entry(demo):
    newest = commit(demo.repo, {"client/src/App.jsx": b"latest source"}, INITIAL + 20)
    build(demo.output, b"initial", INITIAL + 30)
    receipt = demo.output / RECEIPT
    record = {"source_commit": demo.original, "entry_sha256": sha256(INDEX).hexdigest(), "built_at": INITIAL + 10}
    receipt.write_text(json.dumps(record), encoding="utf-8")
    stale = _status(demo.app)
    assert stale["state"] == "behind"
    assert stale["build_commit"] == demo.original
    assert stale["built_at"] == INITIAL + 10
    assert stale["behind_since"] == INITIAL + 20
    assert stale["source_commit"] == newest
    write(demo.output / "index.html", INDEX + b"\n")
    assert _status(demo.app)["reason"] == "receipt"


def test_done_rejects_stale_and_wrong_runtime_and_allows_backend_and_rebuilt(demo, tmp_path, monkeypatch):
    ui_change = commit(demo.repo, {"client/src/App.jsx": b"new frontend"}, INITIAL + 20)
    backend = commit(demo.repo, {"product/server.py": b"new backend"}, INITIAL + 30)
    facet = CompiledUIFacet(None)
    monkeypatch.setattr(facet, "apps", lambda: [demo.app])
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: demo.repo if name == "Demo" else None)
    agora.compiled_ui_checker = facet.require_current
    args = {"agent": "worker", "force": True, "reason": "Verified in the test environment"}
    try:
        ui = agora.task_add({"agent": "worker", "title": "Frontend", "repo": "Demo", "claim": True})["task"]
        agora.task_submit({"agent": "worker", "task_id": ui["id"], "commits": [ui_change], "summary": "Frontend"})
        main = agora.task_add({"agent": "worker", "title": "Backend", "repo": "Demo", "claim": True})["task"]
        assert agora.task_done({**args, "task_id": main["id"], "commits": [backend]})["task"]["status"] == "done"

        with pytest.raises(AgoraError) as blocked:
            agora.task_done({**args, "task_id": ui["id"], "commits": [backend]})
        assert blocked.value.status == 409
        assert blocked.value.extra["error_code"] == "compiled_ui_not_current"
        assert "Recompila" in blocked.value.extra["message_es"]
        assert "Rebuild" in blocked.value.extra["message_en"]
        assert agora.task({"task_id": ui["id"]})["task"]["status"] == "review"

        build(demo.output, b"new frontend", INITIAL + 40)
        with pytest.raises(AgoraError) as mismatch:
            agora.task_done({**args, "task_id": ui["id"]})
        assert mismatch.value.extra["compiled_ui"]["reason"] == "mismatch"
        build(demo.served, b"new frontend", INITIAL + 40)
        assert agora.task_done({**args, "task_id": ui["id"]})["task"]["status"] == "done"
    finally:
        agora.close()


def test_unintegrated_interface_commit_cannot_close_a_task(demo, tmp_path, monkeypatch):
    git(demo.repo, "switch", "-c", "delivery")
    ui_change = commit(demo.repo, {"client/src/App.jsx": b"never installed"}, INITIAL + 20)
    git(demo.repo, "switch", "main")
    facet = CompiledUIFacet(None)
    monkeypatch.setattr(facet, "apps", lambda: [demo.app])
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: demo.repo)
    agora.compiled_ui_checker = facet.require_current
    try:
        task = agora.task_add({"agent": "worker", "title": "Frontend", "repo": "Demo", "claim": True})["task"]
        with pytest.raises(AgoraError) as blocked:
            agora.task_done({"agent": "worker", "task_id": task["id"], "commits": [ui_change]})
        assert blocked.value.extra["compiled_ui"]["reason"] == "not_integrated"
    finally:
        agora.close()


def test_build_manifest_and_known_service_layouts(tmp_path):
    demo = AppUI("example", "Example", tmp_path, (), {
        "output": "generated/renderer", "sources": ["web/client"],
        "route": "/studio/", "asset_base": "/studio/",
    })
    build = _detect(demo)
    assert (build.output, build.inputs, build.route, build.asset_base) == (
        "generated/renderer", ("web/client",), "/studio/", "/studio/",
    )

    write(tmp_path / "apps/agora/vite.config.ts", b"export default {}")
    hub = _detect(AppUI("hoardhub", "Hub", tmp_path, ()))
    assert hub.output == "apps/agora/dist" and hub.route == "/agora/"
    assert "hoard_link/hub/ui/production_decisions_data.js" in hub.inputs

    write(tmp_path / "vite.config.ts", b"""const configRoot = dirname(fileURLToPath(import.meta.url));
        export default { root: 'studio', build: { outDir: resolve(configRoot, 'static/studio'),
            rollupOptions: {output: {entryFileNames: 'studio.js'}}}};""")
    studio = _detect(AppUI("faustus", "Faustus", tmp_path, ()))
    assert studio.output == "static/studio" and studio.route == "/static/studio/studio.js"


def test_selection_uses_existing_placement_without_polling_it(tmp_path):
    config = SimpleNamespace(url="http://127.0.0.1:1", repos={}, faustus_dir=tmp_path,
                             faustus_urls=["http://127.0.0.1:2"], isolated=False)
    placement = SimpleNamespace(state=lambda: {"location": "sparks", "urls": {"faustus": "https://example.invalid"}})
    facet = CompiledUIFacet(SimpleNamespace(apps=[], config=config, placement=placement))
    rows = {app.id: app for app in facet.apps()}
    assert rows["faustus"].urls == ("https://example.invalid",)
    assert rows["faustus"].root == tmp_path
    config.isolated = True
    assert {app.id: app for app in facet.apps()}["faustus"].urls == ("http://127.0.0.1:2",)


def test_nested_web_build_uses_the_config_directory(tmp_path):
    write(tmp_path / "apps/web/vite.config.js", b"export default {build: {outDir: 'dist'}}")
    build = _detect(AppUI("example", "Example", tmp_path, ()))
    assert build.output == "apps/web/dist"
    assert build.method == "apps/web/vite.config.js"
    assert "apps/web/src" in build.inputs
