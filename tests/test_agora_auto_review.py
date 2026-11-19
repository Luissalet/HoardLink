from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hoard_link.hub import agora_auto_review as auto, agora_worker_profiles as profiles
from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.events import EventLog


@pytest.fixture
def room(tmp_path, monkeypatch):
    clock = [time.time()]
    events = EventLog(None, now=lambda: clock[0])
    agora = Agora(tmp_path / "agora.db", clock=lambda: clock[0], emit=events.emit)
    agora.space_create({"id": "estudio", "title": "Estudio", "agent": "codex-builder"})
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)), events=events,
                          facet=lambda _: SimpleNamespace(agora=agora))
    root = AgoraConsoleFacet(hub)
    console = root.for_space("estudio")
    monkeypatch.setattr(auto, "codex_observation", lambda: {})
    monkeypatch.setattr(console, "_provider_data", lambda: {
        "codex": {"state": "available", "catalog": [{"id": "small"}]},
        "claude": {"state": "available", "catalog": [{"id": "haiku"}]},
    })
    launched = []

    def create(args, actor, *, worker_id):
        assert actor == auto.SERVICE
        launched.append((worker_id, args))
        agent = f"{args['leader_id']}-worker-{worker_id[:8]}"
        with console._db_lock, console._db() as db:
            db.execute("INSERT INTO workers(id,agent_id,parent_id,model,effort,task,state,created_at,updated_at) "
                       "VALUES(?,?,?,?,'',?,'running',?,?)", (worker_id, agent, args["leader_id"], args["model"],
                                                               args["task"], clock[0], clock[0]))
        return {"ok": True, "worker": {"id": worker_id, "state": "running"}}

    monkeypatch.setattr(console, "_create_worker", create)
    yield console, agora.for_space("estudio"), clock, launched
    root.close()
    agora.close()
    events.close()


def enable(console, enabled=True):
    current = profiles.read(console)
    revision = current.pop("revision")
    assert profiles.save(console, {"expected_revision": revision, "profile": {**current, "auto_review": enabled}})["ok"]


def quota(agora, name, remaining):
    return agora.usage_report({"agent": name + "-capacity", "provider_id": name, "pool_id": name + "-pool",
                               "pool_label": "Local account", "source": "fixture", "windows": [{
                                   "window_id": "primary", "measurement": "measured", "used_percent": 100 - remaining,
                                   "remaining_percent": remaining,
                               }]})


def submit(agora, *locks):
    task = agora.task_add({"agent": "codex-builder", "title": "Test the handoff", "body": "Run the check"})["task"]
    assert agora.task_claim({"agent": "codex-builder", "task_id": task["id"], "locks": list(locks)})["ok"]
    return agora.task_submit({"agent": "codex-builder", "task_id": task["id"], "summary": "Receipt A"})["task"]


def test_delivery_launches_exactly_one_cross_provider_review_and_requires_a_real_vote(room):
    console, agora, _, launched = room
    submit(agora)
    enable(console)
    quota(agora, "claude", 30)
    quota(agora, "codex", 60)
    task = submit(agora)
    console._auto_review.process(console)
    console._auto_review.process(console)
    assert len(launched) == 1
    worker_id, args = launched[0]
    assert args["leader_id"] == "claude" and args["model"] == "haiku"
    assert args["template"] == "review" and args["rank"] == "support"
    assert f"#{task['id']} r1" in args["task"] and "Receipt A" in args["task"]
    row = auto.status(console)["jobs"][0]
    worker = {"id": worker_id, "agent_id": row["agent_id"]}
    agora.post({"agent": row["agent_id"], "thread_id": task["thread_id"], "body": "Reviewed submission revision: 1"})
    assert console._verify_worker_delivery(worker)["issues"]
    with pytest.raises(AgoraError, match="changed"):
        agora.task_review({"agent": row["agent_id"], "task_id": task["id"], "expected_submission_revision": 2,
                           "body": "Wrong revision", "verdict": "approve"})
    agora.task_review({"agent": row["agent_id"], "task_id": task["id"], "expected_submission_revision": 1,
                       "body": "Verified", "verdict": "approve"})
    verified = console._verify_worker_delivery(worker)
    assert verified["issues"] == [] and verified["reviewed_revision"] == 1 and verified["verdict"] == "approve"


def test_waits_for_quota_and_cancellation_survives_a_new_report(room):
    console, agora, _, launched = room
    enable(console)
    quota(agora, "claude", 0)
    quota(agora, "codex", 80)
    submit(agora)
    console._auto_review.process(console)
    [job] = auto.status(console)["jobs"]
    assert job["state"] == "waiting_quota" and not launched
    assert auto.cancel(console, job["id"])["jobs"][0]["state"] == "cancelled"
    quota(agora, "claude", 80)
    console._auto_review.process(console)
    assert not launched


def test_changes_wait_for_author_then_handover_only_that_revision_with_leases(room):
    console, agora, clock, launched = room
    enable(console)
    task = submit(agora, "path:testing/receipt.md")
    task_id = task["id"]
    agora.task_review({"agent": "claude-reviewer", "task_id": task_id, "expected_submission_revision": 1,
                       "body": "The label should say Done", "verdict": "changes"})
    quota(agora, "codex", 30)
    console._auto_review.process(console)
    assert auto.status(console)["jobs"][0]["state"] == "waiting_author"
    assert not launched
    clock[0] += agora.handover_idle_s + 1
    quota(agora, "codex", 30)
    console._auto_review.process(console)
    [correction] = [job for job in auto.status(console)["jobs"] if job["kind"] == "fix"]
    assert correction["state"] == "handover"
    assert "The label should say Done" in launched[0][1]["task"]
    assert agora.task({"task_id": task_id})["task"]["owner"] == "codex-builder"
    agora.heartbeat({"agent": correction["agent_id"], "state": "working"})
    console._auto_review.process(console)
    result = agora.task({"task_id": task_id})
    assert result["task"]["owner"] == correction["agent_id"]
    assert result["task"]["status"] == "changes" and result["task"]["submission_revision"] == 1
    assert result["locks"][0]["owner"] == correction["agent_id"]
    assert auto.status(console)["jobs"][0]["state"] == "launched"
