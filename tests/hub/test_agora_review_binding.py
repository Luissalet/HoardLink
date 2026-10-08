"""A review vote must apply to the submission revision the reviewer inspected."""

from __future__ import annotations

import pytest
import threading

from hoard_link.hub.agora import Agora, AgoraError, MIGRATIONS, TOOLS
from hoard_link.sqlkit import Database


def test_stale_vote_after_same_commit_resubmission_changes_nothing(tmp_path):
    events = []
    ag = Agora(tmp_path / "agora.db", emit=lambda kind, data: events.append((kind, data)))
    try:
        tid = ag.task_add({"agent": "claude", "title": "Fix", "claim": True})["task"]["id"]
        ag.task_submit({"agent": "claude", "task_id": tid, "summary": "first", "commits": ["abc"]})
        observed_revision = 1
        ag.task_submit({"agent": "claude", "task_id": tid, "summary": "same tree again", "commits": ["abc"]})

        before = ag.task({"task_id": tid})
        events_before = list(events)
        agents_before = [dict(row) for row in ag.db.query("SELECT * FROM agents ORDER BY id")]
        with pytest.raises(AgoraError) as exc:
            ag.task_review({"agent": "codex", "task_id": tid, "verdict": "approve", "body": "checked",
                            "expected_submission_revision": observed_revision})

        assert exc.value.status == 409
        assert exc.value.extra == {"error_code": "stale_submission", "current_revision": observed_revision + 1,
                                   "current_commits": ["abc"]}
        assert ag.task({"task_id": tid}) == before
        assert events == events_before
        assert [dict(row) for row in ag.db.query("SELECT * FROM agents ORDER BY id")] == agents_before
    finally:
        ag.close()


@pytest.mark.parametrize("expected", [None, True, "1", 1.0, 0, -1])
def test_invalid_expected_revision_refuses_vote_without_mutation(tmp_path, expected):
    ag = Agora(tmp_path / "agora.db")
    try:
        tid = ag.task_add({"agent": "claude", "title": "Fix", "claim": True})["task"]["id"]
        submitted = ag.task_submit({"agent": "claude", "task_id": tid, "summary": "ready"})["task"]
        before = ag.task({"task_id": tid})
        before_agents = [dict(row) for row in ag.db.query("SELECT * FROM agents ORDER BY id")]
        args = {"agent": "codex", "task_id": tid, "verdict": "approve", "body": "checked"}
        if expected is not None:
            args["expected_submission_revision"] = expected
        with pytest.raises(AgoraError) as exc:
            ag.task_review(args)
        assert exc.value.status == 400
        assert ag.task({"task_id": tid}) == before
        assert [dict(row) for row in ag.db.query("SELECT * FROM agents ORDER BY id")] == before_agents
        assert submitted["submission_revision"] == 1
    finally:
        ag.close()


def test_vote_binds_revision_and_commits_in_message_and_event(tmp_path):
    events = []
    ag = Agora(tmp_path / "agora.db", emit=lambda kind, data: events.append((kind, data)))
    try:
        tid = ag.task_add({"agent": "claude", "title": "Fix", "claim": True})["task"]["id"]
        submitted = ag.task_submit({"agent": "claude", "task_id": tid, "summary": "ready",
                                    "commits": ["abc", "def"]})["task"]
        assert submitted["submission_revision"] == 1
        assert "Submission revision: 1" in ag.task({"task_id": tid})["messages"][-1]["body"]
        assert "Commits: abc, def" in ag.task({"task_id": tid})["messages"][-1]["body"]
        voted = ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                                "verdict": "changes", "body": "fix edge case"})["task"]
        assert voted["status"] == "changes" and voted["reviewed"] is False
        assert voted["reviewed_submission_revision"] == 1
        review_message = ag.task({"task_id": tid})["messages"][-1]["body"]
        assert "Reviewed submission revision: 1" in review_message
        assert "Commits: abc, def" in review_message and "fix edge case" in review_message
        reviewed_event = next(data for kind, data in reversed(events) if kind == "agora.task.reviewed")
        assert reviewed_event["submission_revision"] == 1 and reviewed_event["commits"] == ["abc", "def"]

        resubmitted = ag.task_submit({"agent": "claude", "task_id": tid, "summary": "fixed",
                                      "commits": ["abc", "def"]})["task"]
        assert resubmitted["submission_revision"] == 2
        assert resubmitted["reviewed_submission_revision"] is None
        approved = ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 2,
                                   "verdict": "approve", "body": "checked"})["task"]
        assert approved["reviewed_submission_revision"] == 2 and approved["reviewed_commits"] == ["abc", "def"]
        done = ag.task_done({"agent": "claude", "task_id": tid})["task"]
        assert done["review_state"] == "approved"
    finally:
        ag.close()


def test_migration_maps_only_historical_submissions_and_reopens(tmp_path):
    path = tmp_path / "old.db"
    old = Database(path, migrations=MIGRATIONS[:3])
    old.execute("INSERT INTO tasks(title,status,reviewed,submitted_at,commits,created,updated) "
                "VALUES('approved','approved',1,10,'[\"a\"]',1,1)")
    old.execute("INSERT INTO tasks(title,status,reviewed,submitted_at,commits,created,updated) "
                "VALUES('done approved','done',1,10,'[\"b\"]',1,1)")
    old.execute("INSERT INTO tasks(title,status,reviewed,submitted_at,commits,created,updated) "
                "VALUES('done unreviewed','done',0,10,'[\"c\"]',1,1)")
    old.execute("INSERT INTO tasks(title,status,reviewed,created,updated) VALUES('never submitted','done',0,1,1)")
    old.close()

    ag = Agora(path)
    try:
        tasks = {task["title"]: task for task in ag.tasks()["tasks"]}
        assert tasks["approved"]["submission_revision"] == 1
        assert tasks["approved"]["reviewed_submission_revision"] == 1
        assert tasks["done approved"]["submission_revision"] == 1
        assert tasks["done approved"]["reviewed_submission_revision"] == 1
        assert tasks["done unreviewed"]["submission_revision"] == 1
        assert tasks["done unreviewed"]["reviewed_submission_revision"] is None
        assert tasks["never submitted"]["submission_revision"] == 0
        assert tasks["never submitted"]["reviewed_submission_revision"] is None
    finally:
        ag.close()

    reopened = Agora(path)
    try:
        assert reopened._task_row(tasks["done approved"]["id"])["reviewed_submission_revision"] == 1
    finally:
        reopened.close()


def test_review_tool_requires_observed_revision():
    schema = next(spec for spec in TOOLS if spec["name"] == "hub_agora_task_review")
    assert "expected_submission_revision" in schema["inputSchema"]["required"]
    assert schema["inputSchema"]["properties"]["expected_submission_revision"]["type"] == "integer"
    assert "read its submission_revision" in schema["description"]


def test_done_refuses_approved_status_with_mismatched_review_revision(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    try:
        tid = ag.task_add({"agent": "claude", "title": "Fix", "claim": True})["task"]["id"]
        ag.task_submit({"agent": "claude", "task_id": tid, "summary": "ready", "commits": ["abc"]})
        ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                        "verdict": "approve", "body": "checked"})
        ag.db.execute("UPDATE tasks SET reviewed_submission_revision=0 WHERE id=?", (tid,))
        before = ag._task_row(tid)
        with pytest.raises(AgoraError) as exc:
            ag.task_done({"agent": "claude", "task_id": tid, "force": True, "reason": "same hashes"})
        assert exc.value.status == 409
        assert ag._task_row(tid) == before
    finally:
        ag.close()


def test_concurrent_resubmit_and_review_never_approve_new_revision(tmp_path):
    path = tmp_path / "race.db"
    setup = Agora(path)
    tid = setup.task_add({"agent": "claude", "title": "Race", "claim": True})["task"]["id"]
    setup.task_submit({"agent": "claude", "task_id": tid, "summary": "first", "commits": ["old"]})
    setup.close()

    gate = threading.Barrier(2)
    outcomes = []
    failures = []

    def review():
        ag = Agora(path)
        try:
            gate.wait()
            outcomes.append(("review", ag.task_review({"agent": "codex", "task_id": tid,
                                                        "expected_submission_revision": 1,
                                                        "verdict": "approve", "body": "checked old"})))
        except AgoraError as exc:
            outcomes.append(("review_error", exc.status, exc.extra))
        except BaseException as exc:
            failures.append(exc)
        finally:
            ag.close()

    def resubmit():
        ag = Agora(path)
        try:
            gate.wait()
            outcomes.append(("submit", ag.task_submit({"agent": "claude", "task_id": tid,
                                                        "summary": "new", "commits": ["new"]})))
        except BaseException as exc:
            failures.append(exc)
        finally:
            ag.close()

    threads = [threading.Thread(target=review), threading.Thread(target=resubmit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    final = Agora(path)
    try:
        task = final._task_row(tid)
        assert task["submission_revision"] == 2
        assert task["commits"] == ["new"]
        assert task["status"] == "review" and task["reviewed"] is False
        assert task["reviewed_submission_revision"] is None
        review_result = next(item for item in outcomes if item[0].startswith("review"))
        assert review_result[0] == "review" or review_result[1:] == (409, {"error_code": "stale_submission",
                                                                              "current_revision": 2,
                                                                              "current_commits": ["new"]})
    finally:
        final.close()
