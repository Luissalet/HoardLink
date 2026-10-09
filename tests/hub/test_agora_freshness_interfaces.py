"""The freshness guard (A1) and the metadata-only inbox (A2) across real HTTP, CLI and stdio MCP, in enforce mode."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

from ._hub_fakes import http, make_hub, serve


def test_http_cli_and_stdio_mcp_read_before_acting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_AGORA_FRESHNESS", "enforce")
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        assert ag.freshness == "enforce"
        tid = ag.task_add({"agent": "codex", "title": "Guarded", "claim": True})["task"]["id"]
        ag.task_submit({"agent": "codex", "task_id": tid, "summary": "ready, 3 tests", "commits": ["aaaa1111"]})
        base = hub.config.url.rstrip("/")
        headers = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        vote = {"agent": "claude", "task_id": tid, "verdict": "approve", "body": "ok", "expected_submission_revision": 1}

        # HTTP: refused with the list; the metadata-only inbox shows who/where without bodies and moves nothing
        status, refusal = http(base + "/api/agora/task_review", vote, headers=headers)
        assert status == 409 and refusal["error_code"] == "unseen_messages" and refusal["unseen"][0]["author"] == "codex"
        status, meta = http(base + "/api/agora/inbox?agent=claude&metadata_only=1", headers=headers)
        assert status == 200 and meta["metadata_only"] and all("body" not in m for m in meta["messages"])

        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, HOARD_HUB_URL=base, HOARD_HUB_TOKEN_FILE=str(hub.config.token_file),
                   HOARD_HUB_DATA_DIR=str(hub.config.data_dir), HOARD_HUB_AUTOSTART="0", PYTHONPATH=str(root))

        def cli(*args, json_out=True):
            return subprocess.run([sys.executable, str(root / "scripts/agora.py"), "--as", "claude",
                                   *(["--json"] if json_out else []), *args],
                                  cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)

        text = cli("inbox", "--meta", json_out=False)
        assert text.returncode == 0 and "caracteres; léelo con thread" in text.stdout, text.stdout
        refused = cli("review", str(tid), "approve", "--revision", "1", "--body", "ok")
        assert refused.returncode == 1 and json.loads(refused.stdout)["error_code"] == "unseen_messages"
        # CLI `task N` reads as claude, which marks the thread read; the vote then goes through
        assert cli("task", str(tid)).returncode == 0
        voted = cli("review", str(tid), "approve", "--revision", "1", "--body", "read and checked")
        assert voted.returncode == 0, voted.stdout + voted.stderr

        # stdio MCP: the schemas take unseen_ok / agent / metadata_only, and unseen_ok overrides with a reason
        with_read = ag.task({"task_id": tid, "agent": "codex"})        # codex reads claude's vote first
        assert with_read["messages"][-1]["kind"] == "approve"
        ag.task_submit({"agent": "codex", "task_id": tid, "summary": "second round", "commits": ["bbbb2222"]})
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_agora_task_review", "arguments": {
                **vote, "expected_submission_revision": 2, "unseen_ok": "checked the diff in my own worktree"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hub_agora_inbox", "arguments": {
                "agent": "claude", "metadata_only": True}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "hub_agora_task", "arguments": {
                "task_id": tid, "agent": "claude"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
        tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
        for name in ("hub_agora_task_claim", "hub_agora_task_submit", "hub_agora_task_review", "hub_agora_task_done",
                     "hub_agora_post"):
            assert "unseen_ok" in tools[name]["inputSchema"]["properties"], name
        assert "agent" in tools["hub_agora_task"]["inputSchema"]["properties"]
        assert "metadata_only" in tools["hub_agora_inbox"]["inputSchema"]["properties"]
        for i in (3, 4, 5):
            assert not replies[i]["result"].get("isError"), replies[i]
        lines = [m["body"] for m in ag.task({"task_id": tid})["messages"] if m["kind"] == "system"]
        assert any("claude hace «review» sin haber leído" in b and "my own worktree" in b for b in lines)
