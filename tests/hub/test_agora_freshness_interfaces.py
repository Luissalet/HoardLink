"""The unread-thread notice (A1) and the metadata-only inbox (A2) across real HTTP, CLI and stdio MCP.

Nothing is refused: the action goes through and the answer says what was unread.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

from ._hub_fakes import http, make_hub, serve


def test_http_cli_and_stdio_mcp_inform_without_blocking(tmp_path, monkeypatch):
    monkeypatch.delenv("HOARD_AGORA_FRESHNESS", raising=False)
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        assert ag.freshness == "warn"
        tid = ag.task_add({"agent": "codex", "title": "Informed", "claim": True})["task"]["id"]
        ag.task_submit({"agent": "codex", "task_id": tid, "summary": "ready, 3 tests", "commits": ["aaaa1111"]})
        base = hub.config.url.rstrip("/")
        headers = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}

        # HTTP: the metadata-only inbox shows who/where without bodies and moves nothing
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
        # CLI: voting without reading goes through, and says so
        voted = cli("review", str(tid), "approve", "--revision", "1", "--body", "ok", json_out=False)
        assert voted.returncode == 0 and "[approved]" in voted.stdout and "AVISO" in voted.stdout, voted.stdout

        # stdio MCP: task with agent marks the thread read; the next action carries no notice
        ag.task({"task_id": tid, "agent": "codex"})
        ag.task_submit({"agent": "codex", "task_id": tid, "summary": "second round", "commits": ["bbbb2222"]})
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_agora_task", "arguments": {
                "task_id": tid, "agent": "claude"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hub_agora_task_review", "arguments": {
                "agent": "claude", "task_id": tid, "verdict": "approve", "body": "read and checked",
                "expected_submission_revision": 2}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "hub_agora_inbox", "arguments": {
                "agent": "claude", "metadata_only": True}}},
        ]
        result = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
        tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
        assert "agent" in tools["hub_agora_task"]["inputSchema"]["properties"]
        assert "metadata_only" in tools["hub_agora_inbox"]["inputSchema"]["properties"]
        assert all("unseen_ok" not in t["inputSchema"].get("properties", {}) for t in tools.values())
        for i in (3, 4, 5):
            assert not replies[i]["result"].get("isError"), replies[i]
        vote = json.loads(replies[4]["result"]["content"][0]["text"])
        assert vote["ok"] and "unseen" not in vote
