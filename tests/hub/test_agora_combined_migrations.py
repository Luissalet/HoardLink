"""Upgrade from legacy or checkpoint-only databases without skipping revisions."""
import json
import pytest
from hoard_link.hub.agora import Agora, AgoraError, MIGRATIONS
from hoard_link.sqlkit import Database


@pytest.mark.parametrize("previous_version", [3, 4])
def test_review_binding_after_checkpoint_migration_and_reopen(tmp_path, previous_version):
    path = tmp_path / "old.db"
    old = Database(path, migrations=MIGRATIONS[:previous_version])
    old.execute("INSERT INTO threads(id,title,kind,task_id,status,created_by,created,updated) "
                "VALUES(1,'Historical','task',1,'open','codex',10,10)")
    old.execute("INSERT INTO tasks(id,title,owner,status,commits,reviewed_commits,reviewed,thread_id,"
                "submitted_at,created,updated) VALUES(1,'Historical','codex','approved',?, ?,1,1,10,10,10)",
                ('["a"]', '["a"]'))
    before = dict(old.one("SELECT * FROM tasks WHERE id=1"))
    if previous_version == 4:
        old.execute("INSERT INTO task_checkpoints VALUES(1,1,'codex',10,?)",
                    (json.dumps({"payload": {"summary": "Existing checkpoint", "artifacts": ["declared"]}, "task_owner": "codex", "lock_snapshot": []}),))
    old.close()
    ag = Agora(path)
    try:
        assert ag.db.schema_version == len(MIGRATIONS) == 6
        current = ag.task({"task_id": 1})["task"]
        assert current["submission_revision"] == current["reviewed_submission_revision"] == 1
        raw = dict(ag.db.one("SELECT * FROM tasks WHERE id=1"))
        assert {k: raw[k] for k in before} == before
        if previous_version == 4:
            assert current["latest_checkpoint"]["payload"]["summary"] == "Existing checkpoint"
        ag.task_submit({"agent": "codex", "task_id": 1, "summary": "Next", "commits": ["b"]})
        with pytest.raises(AgoraError) as refusal:
            ag.task_review({"agent": "claude", "task_id": 1, "expected_submission_revision": 1,
                            "verdict": "approve", "body": "Old"})
        assert refusal.value.status == 409
        ag.task_review({"agent": "claude", "task_id": 1, "expected_submission_revision": 2,
                        "verdict": "approve", "body": "Current"})
        result = ag.checkpoint({"agent": "codex", "task_id": 1,
                               "expected_revision": 1 if previous_version == 4 else 0,
                               "payload": {"summary": "Next checkpoint"}})
        assert result["checkpoint"]["revision"] == (2 if previous_version == 4 else 1)
    finally:
        ag.close()
    reopened = Agora(path)
    try:
        assert reopened.db.schema_version == 6
        current = reopened.task({"task_id": 1})["task"]
        assert current["submission_revision"] == current["reviewed_submission_revision"] == 2
        assert current["latest_checkpoint"]["payload"]["summary"] == "Next checkpoint"
    finally:
        reopened.close()
