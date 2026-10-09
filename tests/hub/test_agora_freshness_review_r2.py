"""#130 review r2: the person's read is recorded only from the hub's page, and a metadata-only inbox carries no text
written by anyone (task descriptions, checkpoint summaries, submission blocks, escalation questions), also after the
web layer's enrichment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from hoard_link.hub.agora import Agora, AgoraError

from ._hub_fakes import http, make_hub, serve

SECRET = "QA_BODY_NOT_METADATA"


def _ack(ag, agent, thread_id):
    row = ag.db.one("SELECT upto FROM acks WHERE agent=? AND thread_id=?", (agent, thread_id))
    return row["upto"] if row else None


def test_task_read_as_the_person_needs_the_page(tmp_path):
    ag = Agora(tmp_path / "a.db")
    try:
        tid = ag.task_add({"agent": "claude", "title": "T", "claim": True})["task"]["id"]
        thread = ag.task({"task_id": tid})["task"]["thread_id"]
        ag.post({"agent": "codex", "thread_id": thread, "body": "note"})
        with pytest.raises(AgoraError):
            ag.task({"task_id": tid, "agent": "luis"})
        assert _ack(ag, "luis", thread) is None
        ag.task({"task_id": tid, "agent": "luis", "_person": True})
        assert _ack(ag, "luis", thread) is not None
    finally:
        ag.close()


def _seed(ag):
    tid = ag.task_add({"agent": "codex", "title": "Reviewed work", "body": SECRET + " description", "claim": True})["task"]["id"]
    ag.checkpoint({"agent": "codex", "task_id": tid, "expected_revision": 0, "payload": {"summary": SECRET + " checkpoint"}})
    ag.task_submit({"agent": "codex", "task_id": tid, "summary": SECRET + " submission", "commits": ["abc1234"]})
    ag.thread_open({"agent": "codex", "title": "Ask", "body": SECRET + " question", "mentions": ["claude"]})
    return tid


def test_metadata_only_inbox_keeps_no_text_over_http_and_mcp(tmp_path):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        ag = hub.facet("agora").agora
        tid = _seed(ag)
        thread = ag.task({"task_id": tid})["task"]["thread_id"]
        base = hub.config.url.rstrip("/")
        headers = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}

        status, out = http(base + "/api/agora/inbox?agent=claude&metadata_only=1", headers=headers)
        assert status == 200 and out["metadata_only"]
        assert out["reviews"] and out["reviews"][0]["id"] == tid
        assert out["messages"] and all("body" not in m for m in out["messages"])
        assert SECRET not in json.dumps(out, ensure_ascii=False)

        # with a token the person's read is not recorded (only the page may read as luis)
        status, _ = http(base + f"/api/agora/tasks/{tid}?agent=luis", headers=headers)
        assert status == 200 and _ack(ag, "luis", thread) is None

        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, HOARD_HUB_URL=base, HOARD_HUB_TOKEN_FILE=str(hub.config.token_file),
                   HOARD_HUB_DATA_DIR=str(hub.config.data_dir), HOARD_HUB_AUTOSTART="0", PYTHONPATH=str(root))
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "hub_agora_inbox", "arguments": {
                "agent": "claude", "metadata_only": True}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_agora_task", "arguments": {
                "task_id": tid, "agent": "luis"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
        meta = json.loads(replies[2]["result"]["content"][0]["text"])
        assert meta["metadata_only"] and meta["reviews"] and SECRET not in json.dumps(meta, ensure_ascii=False)
        assert replies[3]["result"].get("isError")              # an agent cannot read as the person
        assert _ack(ag, "luis", thread) is None


def test_the_person_metadata_inbox_hides_escalation_questions(tmp_path):
    ag = Agora(tmp_path / "a.db")
    try:
        th = ag.thread_open({"agent": "codex", "title": "Need you", "body": "context"})["thread"]["id"]
        ag.escalate({"agent": "codex", "thread_id": th, "question": SECRET + " for Luis"})
        out = ag.inbox({"agent": "luis", "_person": True, "metadata_only": True})
        assert out["escalated"] and out["escalated"][0]["question_chars"] > 0
        assert SECRET not in json.dumps(out, ensure_ascii=False)
    finally:
        ag.close()
