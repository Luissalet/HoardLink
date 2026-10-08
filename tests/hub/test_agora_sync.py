"""Durable one-call Ágora resume across the store, HTTP, CLI and stdio MCP."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub import tools

from ._hub_fakes import http, make_hub, serve


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def test_sync_returns_work_and_does_not_consume_messages_or_mentions(tmp_path):
    clock = Clock()
    db = tmp_path / "agora.db"
    ag = Agora(db, clock=clock)
    try:
        own = ag.task_add({"agent": "codex", "title": "Keep the current work", "repo": "HoardLink",
                           "paths": ["hoard_link/hub/agora.py"], "claim": True})["task"]
        ag.task_submit({"agent": "codex", "task_id": own["id"], "summary": "needs review"})
        ag.task_review({"agent": "claude", "task_id": own["id"], "expected_submission_revision": 1,
                        "verdict": "changes", "body": "keep working"})

        review = ag.task_add({"agent": "claude", "title": "Review the resume call", "repo": "HoardLink",
                              "paths": ["tests/hub/test_agora_sync.py"], "claim": True})["task"]
        ag.task_submit({"agent": "claude", "task_id": review["id"], "summary": "ready", "reviewer": "codex"})
        thread = ag.thread_open({"agent": "claude", "title": "Resume note", "body": "Codex, please read this",
                                 "kind": "handoff", "mentions": ["codex"]})["thread"]
        clock.t += 30

        result = ag.sync({"agent": "codex", "doing": "Implementing Agora sync", "note": "Task #34",
                          "since_id": 0, "thread_ids": [thread["id"]]})

        assert result["ok"]
        assert result["agent_state"]["doing"] == "Implementing Agora sync"
        assert result["agent_state"]["note"] == "Task #34"
        assert [task["id"] for task in result["pending_reviews"]] == [review["id"]]
        assert [task["id"] for task in result["requested_changes"]] == [own["id"]]
        assert [task["id"] for task in result["leased_tasks"]] == [own["id"]]
        assert result["leased_tasks"][0]["locks"][0]["resource"] == "path:hoardlink/hoard_link/hub/agora.py"
        assert result["board"]["counts"]["changes"] == 1
        assert [post["thread_id"] for post in result["posts"]] == [thread["id"]]
        assert result["next_since_id"] == result["posts"][-1]["id"]
        assert result["cursor"]["thread_ids"] == [thread["id"]]
        assert any(message["thread_id"] == thread["id"] for message in result["inbox"]["pending"])
        assert ag.db.scalar("SELECT last_msg_id FROM reads WHERE agent='codex'") is None
    finally:
        ag.close()


def test_sync_cursor_replays_across_restart_and_catches_posts_after_snapshot(tmp_path):
    db = tmp_path / "agora.db"
    ag = Agora(db)
    thread = ag.thread_open({"agent": "claude", "title": "Shared channel", "body": "First", "kind": "handoff",
                             "mentions": ["codex"]})["thread"]
    unrelated = ag.thread_open({"agent": "claude", "title": "Other", "body": "Other post", "kind": "note"})["thread"]
    try:
        first = ag.sync({"agent": "codex", "doing": "resume", "since_id": 0, "thread_ids": [thread["id"]]})
        first_id = first["posts"][0]["id"]
        assert first["next_since_id"] == first_id
        assert first["posts"][0]["thread_id"] == thread["id"]
        assert unrelated["id"] != thread["id"]
        assert first["inbox"]["counts"]["pending"] == 1
    finally:
        ag.close()

    # A restart does not persist or advance an implicit process-local cursor.
    # The caller's last returned database message id is the only cursor.
    ag = Agora(db)
    try:
        replay = ag.sync({"agent": "codex", "doing": "resume after restart", "since_id": 0,
                          "thread_ids": [thread["id"]]})
        assert [post["id"] for post in replay["posts"]] == [first_id]
        assert replay["inbox"]["counts"]["pending"] == 1

        # This post arrives after the earlier read. Sync does not call ack, so both the durable cursor and
        # the pending mention survive until the caller explicitly opens/acks the thread.
        ag.post({"agent": "claude", "thread_id": thread["id"], "body": "Second", "mentions": ["codex"]})
        next_page = ag.sync({"agent": "codex", "doing": "resume after restart", "since_id": replay["next_since_id"],
                             "thread_ids": [thread["id"]]})
        assert [post["body"] for post in next_page["posts"]] == ["Second"]
        assert next_page["next_since_id"] > replay["next_since_id"]
        assert next_page["inbox"]["counts"]["pending"] == 2

        ag.ack({"agent": "codex", "thread_id": thread["id"]})
        assert ag.inbox({"agent": "codex", "peek": True})["counts"]["pending"] == 0
    finally:
        ag.close()


def test_sync_paginates_without_skipping_and_rejects_unknown_threads(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    try:
        thread = ag.thread_open({"agent": "claude", "title": "Pages", "body": "one", "kind": "note"})["thread"]
        ag.post({"agent": "claude", "thread_id": thread["id"], "body": "two"})
        ag.post({"agent": "claude", "thread_id": thread["id"], "body": "three"})
        page1 = ag.sync({"agent": "codex", "doing": "paging", "since_id": 0, "limit": 2})
        page2 = ag.sync({"agent": "codex", "doing": "paging", "since_id": page1["next_since_id"], "limit": 2})
        assert [post["body"] for post in page1["posts"]] == ["one", "two"]
        assert page1["cursor"]["has_more"] is True
        assert [post["body"] for post in page2["posts"]] == ["three"]
        assert page2["cursor"]["has_more"] is False
        assert page2["next_since_id"] == page2["posts"][-1]["id"]
        with pytest.raises(AgoraError) as exc:
            ag.sync({"agent": "codex", "since_id": page2["next_since_id"], "thread_ids": [99999]})
        assert exc.value.status == 404
        for bad in (-1, True, "not-an-id", 1.5, 9_223_372_036_854_775_808):
            with pytest.raises(AgoraError):
                ag.sync({"agent": "codex", "since_id": bad})
        with pytest.raises(AgoraError):
            ag.sync({"agent": "codex", "limit": 1.5})
    finally:
        ag.close()


def test_combined_mcp_catalogue_rejects_duplicate_tool_names(monkeypatch):
    from hoard_link.hub import facets

    monkeypatch.setattr(tools, "catalogue", lambda: [{"name": "hub_agora_sync"}])
    monkeypatch.setattr(facets, "catalogue", lambda: [{"name": "hub_agora_sync"}])
    with pytest.raises(ValueError, match="duplicate agent tool names: hub_agora_sync"):
        tools.all_tools()


def test_sync_has_one_declared_schema_and_works_over_http_cli_and_stdio_mcp(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    token = Path(hub.config.token_file).read_text(encoding="utf-8").strip()
    try:
        names = [tool["name"] for tool in tools.all_tools()]
        assert names.count("hub_agora_sync") == 1
        schema = next(tool for tool in tools.all_tools() if tool["name"] == "hub_agora_sync")["inputSchema"]
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["agent"]
        assert schema["properties"]["since_id"]["type"] == "integer"
        assert schema["properties"]["thread_ids"]["items"]["type"] == "integer"

        ag = hub.facet("agora").agora
        thread = ag.thread_open({"agent": "claude", "title": "MCP resume", "body": "Seen by cursor",
                                 "kind": "handoff", "mentions": ["codex"]})["thread"]
        body = {"agent": "codex", "doing": "CLI resume", "since_id": 0, "thread_ids": [thread["id"]]}
        headers = {"Authorization": "Bearer " + token}
        status, _ = http(base + "/api/agora/sync", body)
        assert status == 401
        status, via_http = http(base + "/api/agora/sync", body, headers=headers)
        assert status == 200 and via_http["posts"][0]["thread_id"] == thread["id"]

        env = os.environ.copy()
        env.update({"HOARD_HUB_URL": base, "HOARD_HUB_TOKEN_FILE": str(hub.config.token_file),
                    "HOARD_HUB_DATA_DIR": str(hub.config.data_dir), "HOARD_HUB_AUTOSTART": "0"})
        root = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
        cli = subprocess.run(
            [sys.executable, str(root / "scripts" / "agora.py"), "--as", "codex", "--json", "sync",
             "CLI resume", "--since", "0", "--thread", str(thread["id"])],
            cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["next_since_id"] == via_http["next_since_id"]

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_agora_sync", "arguments": {"agent": "codex", "doing": "MCP resume",
                                                            "since_id": via_http["next_since_id"],
                                                            "thread_ids": [thread["id"]]}}},
        ]
        mcp = subprocess.run(
            [sys.executable, "-m", "hoard_link.hub.mcp"], input="\n".join(json.dumps(r) for r in requests) + "\n",
            cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        assert mcp.returncode == 0, mcp.stderr
        replies = {item["id"]: item for item in (json.loads(line) for line in mcp.stdout.splitlines()) if "id" in item}
        listed = replies[2]["result"]["tools"]
        assert sum(tool["name"] == "hub_agora_sync" for tool in listed) == 1
        call_result = replies[3]["result"]
        assert not call_result.get("isError")
        payload = json.loads(call_result["content"][0]["text"])
        assert payload["posts"] == [] and payload["next_since_id"] == via_http["next_since_id"]
        assert any(message["thread_id"] == thread["id"] for message in payload["inbox"]["pending"])
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
