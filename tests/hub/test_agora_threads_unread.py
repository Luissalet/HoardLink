"""#132: the thread list says, per thread, what a given agent (or the person) has not seen yet, without marking it read."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

import pytest

from hoard_link.hub.agora import Agora

from ._hub_fakes import http, make_hub, serve


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def ag(tmp_path):
    a = Agora(tmp_path / "agora.db", clock=Clock())
    yield a
    a.close()


def _row(a, thread_id, agent):
    return next(t for t in a.threads({"agent": agent})["threads"] if t["id"] == thread_id)


def test_unread_counts_others_messages_since_the_last_read_and_listing_marks_nothing(ag):
    th = ag.thread_open({"agent": "claude", "title": "Plan", "body": "first idea"})["thread"]["id"]
    assert _row(ag, th, "luis")["unread"] == 1           # the opening message by someone else
    assert _row(ag, th, "claude")["unread"] == 0         # own messages never count
    ag.post({"agent": "codex", "thread_id": th, "body": "a second view"})
    row = _row(ag, th, "luis")
    assert row["unread"] == 2 and row["seen_upto"] == 0
    assert _row(ag, th, "luis")["unread"] == 2           # listing twice changes nothing

    ag.thread({"thread_id": th, "agent": "luis", "_person": True})   # reading the thread acks it
    row = _row(ag, th, "luis")
    assert row["unread"] == 0 and row["seen_upto"] > 0
    ag.post({"agent": "claude", "thread_id": th, "body": "update"})
    assert _row(ag, th, "luis")["unread"] == 1
    assert _row(ag, th, "codex")["unread"] == 1          # codex has seen its own post but not claude's update


def test_inbox_read_mark_counts_and_without_agent_there_is_no_unread(ag):
    th = ag.thread_open({"agent": "claude", "title": "Q", "body": "question", "mentions": ["codex"]})["thread"]["id"]
    assert _row(ag, th, "codex")["unread"] == 1
    ag.inbox({"agent": "codex"})                          # a delivering inbox moves the read mark
    assert _row(ag, th, "codex")["unread"] == 0
    plain = next(t for t in ag.threads()["threads"] if t["id"] == th)
    assert "unread" not in plain and "seen_upto" not in plain
    bad = next(t for t in ag.threads({"agent": "not valid!"})["threads"] if t["id"] == th)
    assert "unread" not in bad


def test_http_and_mcp_list_threads_with_unread(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        th = ag.thread_open({"agent": "claude", "title": "Listed", "body": "hello"})["thread"]["id"]
        base = hub.config.url.rstrip("/")
        headers = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        status, out = http(base + "/api/agora/threads?agent=luis", headers=headers)
        assert status == 200 and next(t for t in out["threads"] if t["id"] == th)["unread"] == 1
        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, HOARD_HUB_URL=base, HOARD_HUB_TOKEN_FILE=str(hub.config.token_file),
                   HOARD_HUB_DATA_DIR=str(hub.config.data_dir), HOARD_HUB_AUTOSTART="0", PYTHONPATH=str(root))
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "hub_agora_threads",
                                                                           "arguments": {"agent": "codex"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
        assert not replies[2]["result"].get("isError"), replies[2]
        listed = json.loads(replies[2]["result"]["content"][0]["text"])
        assert next(t for t in listed["threads"] if t["id"] == th)["unread"] == 1
        assert _row(ag, th, "codex")["unread"] == 1      # listing over MCP marked nothing
