"""Agora handover: the half-done work of an agent that died moves to its successor (ownership, live locks, reviewer
role), who may do it and when, what it records, atomicity, idempotency, and the HTTP / tool / terminal faces."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub.agora import HANDOVER_IDLE_S, STALE_AGENT_S, Agora, AgoraError

from ._hub_fakes import http, make_hub, serve

OLD, NEW = "codex-bucle", "codex-relevo"
MIN = 60.0


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def ag(tmp_path):
    events = []
    clock = Clock()
    a = Agora(tmp_path / "agora.db", clock=clock, emit=lambda t, d: events.append((t, d)))
    a.events, a.fake_clock = events, clock
    yield a
    a.close()


def _add(ag, owner, title, path, **extra):
    t = ag.task_add({"agent": owner, "title": title, "repo": "HoardLink", "paths": [path], **extra})["task"]
    assert ag.task_claim({"agent": owner, "task_id": t["id"]})["ok"]
    return t["id"]


def _world(ag):
    """codex-bucle owns six tasks (one done) and reviews two of claude's; then 31 minutes pass in silence."""
    for who in (OLD, NEW, "claude", "other"):
        ag.heartbeat({"agent": who, "doing": "working"})
    ids = {}
    ids["claimed"] = _add(ag, OLD, "claimed", "a.py")
    ids["changes"] = _add(ag, OLD, "changes", "b.py")
    ag.task_submit({"agent": OLD, "task_id": ids["changes"], "summary": "s", "commits": ["c1"], "reviewer": "claude"})
    ag.task_review({"agent": "claude", "task_id": ids["changes"], "expected_submission_revision": 1,
                    "verdict": "changes", "body": "fix it"})
    ag.checkpoint({"agent": OLD, "task_id": ids["changes"], "expected_revision": 0, "payload": {"summary": "half way"}})
    ids["approved"] = _add(ag, OLD, "approved", "c.py")
    ag.task_submit({"agent": OLD, "task_id": ids["approved"], "summary": "s", "commits": ["c2"], "reviewer": "claude"})
    ag.task_review({"agent": "claude", "task_id": ids["approved"], "expected_submission_revision": 1,
                    "verdict": "approve", "body": "ok"})
    ids["done"] = _add(ag, OLD, "done", "d.py", kind="docs")
    ag.task_done({"agent": OLD, "task_id": ids["done"], "result": "finished", "exempt": "docs"})
    ids["working"] = _add(ag, OLD, "working", "e.py")
    ag.task_update({"agent": OLD, "task_id": ids["working"], "status": "in_progress", "locks": ["model:principal"]})
    ids["rev1"] = _add(ag, "claude", "claude one", "f.py")
    ag.task_submit({"agent": "claude", "task_id": ids["rev1"], "summary": "s", "commits": ["c3"], "reviewer": OLD})
    ids["rev2"] = _add(ag, "claude", "claude two", "g.py")
    ag.task_submit({"agent": "claude", "task_id": ids["rev2"], "summary": "s", "commits": ["c4"], "reviewer": OLD})
    ag.task_review({"agent": OLD, "task_id": ids["rev2"], "expected_submission_revision": 1,
                    "verdict": "changes", "body": "no"})
    ids["other"] = _add(ag, "other", "other", "h.py")
    ag.task_submit({"agent": "other", "task_id": ids["other"], "summary": "s", "commits": ["c5"], "reviewer": "claude"})
    ag.fake_clock.t += 31 * MIN
    ag.heartbeat({"agent": NEW, "doing": "taking over"})
    ag.heartbeat({"agent": "claude", "doing": "working"})
    ag.heartbeat({"agent": "other", "doing": "working"})
    return ids


def _args(**kw):
    return {"agent": "claude", "from_agent": OLD, "to_agent": NEW, "reason": "context exhausted", **kw}


def _task(ag, tid):
    return ag.task({"task_id": tid})


def _fingerprint(ag):
    return {"tasks": [dict(r) for r in ag.db.query("SELECT * FROM tasks ORDER BY id")],
            "locks": [dict(r) for r in ag.db.query("SELECT * FROM locks ORDER BY resource")],
            "messages": ag.db.scalar("SELECT COUNT(*) FROM messages", default=0),
            "threads": ag.db.scalar("SELECT COUNT(*) FROM threads", default=0),
            "events": len(ag.events)}


# ---- who may do it ------------------------------------------------------------------------------------------------

def test_constants():
    assert HANDOVER_IDLE_S == 30 * 60 and STALE_AGENT_S == 6 * 3600


def test_person_can_hand_over_while_the_agent_is_fresh(ag):
    ids = _world(ag)
    ag.heartbeat({"agent": OLD, "doing": "still here"})
    r = ag.handover({**_args(agent="luis"), "_person": True})
    assert r["ok"] and r["by"] == "luis" and not r["noop"]
    assert _task(ag, ids["claimed"])["task"]["owner"] == NEW


def test_agent_blocked_while_the_source_is_fresh_and_allowed_after_30_minutes(ag):
    ids = _world(ag)
    ag.heartbeat({"agent": OLD, "doing": "alive"})
    before = _fingerprint(ag)
    with pytest.raises(AgoraError) as err:
        ag.handover(_args())
    assert err.value.status == 409 and "luis" in str(err.value) and "30 min" in str(err.value)
    assert err.value.extra["retry_after_s"] == 30 * 60 and err.value.extra["idle_s"] == 0
    assert _fingerprint(ag) == before
    ag.fake_clock.t += 29 * MIN
    ag.heartbeat({"agent": NEW})
    with pytest.raises(AgoraError) as err:
        ag.handover(_args())
    assert err.value.status == 409 and "about 1 min" in str(err.value)
    ag.fake_clock.t += 1 * MIN
    ag.heartbeat({"agent": NEW})
    r = ag.handover(_args())                                   # exactly 30 minutes of silence
    assert r["ok"] and _task(ag, ids["claimed"])["task"]["owner"] == NEW


def test_idle_threshold_is_configurable(tmp_path):
    clock = Clock()
    a = Agora(tmp_path / "agora.db", clock=clock, handover_idle_s=5 * MIN)
    try:
        for who in (OLD, NEW, "claude"):
            a.heartbeat({"agent": who})
        _add(a, OLD, "t", "a.py")
        clock.t += 6 * MIN
        a.heartbeat({"agent": NEW})
        a.heartbeat({"agent": "claude"})
        assert a.handover(_args())["owner_moved"]
    finally:
        a.close()


def test_the_agent_itself_can_hand_over_its_own_work_while_fresh(ag):
    ids = _world(ag)
    ag.heartbeat({"agent": OLD})
    r = ag.handover(_args(agent=OLD))
    assert r["ok"] and ids["claimed"] in r["owner_moved"]


def test_unregistered_caller_is_refused(ag):
    _world(ag)
    with pytest.raises(AgoraError) as err:
        ag.handover(_args(agent="stranger"))
    assert err.value.status == 403
    assert ag.db.one("SELECT id FROM agents WHERE id='stranger'") is None


def test_unknown_source_counts_as_idle(ag):
    ag.heartbeat({"agent": NEW})
    ag.heartbeat({"agent": "claude"})
    ag.db.execute("INSERT INTO tasks(title, repo, paths, kind, priority, status, created_by, owner, created, updated) "
                  "VALUES('ghost', 'HoardLink', '[]', 'feature', 2, 'claimed', 'ghost', 'ghost-agent', 1, 1)")
    r = ag.handover(_args(from_agent="ghost-agent"))
    assert r["ok"] and len(r["owner_moved"]) == 1


def test_target_must_be_registered_and_fresh(ag):
    _world(ag)
    with pytest.raises(AgoraError) as err:
        ag.handover(_args(to_agent="codex-nobody"))
    assert err.value.status == 409 and "not registered" in str(err.value)
    ag.fake_clock.t += STALE_AGENT_S + 60
    ag.heartbeat({"agent": "claude"})
    with pytest.raises(AgoraError) as err:
        ag.handover(_args())                                     # codex-relevo has been silent for over 6 h
    assert err.value.status == 409 and "no recent heartbeat" in str(err.value)
    # the person may choose a stale (but registered) successor, never an unknown one
    r = ag.handover({**_args(agent="luis"), "_person": True})
    assert r["ok"] and r["owner_moved"]
    with pytest.raises(AgoraError):
        ag.handover({**_args(agent="luis", to_agent="codex-nobody"), "_person": True})


def test_bad_arguments(ag):
    _world(ag)
    for kw in ({"reason": ""}, {"reason": "   "}, {"to_agent": OLD}, {"from_agent": "luis"}, {"to_agent": "luis"},
               {"from_agent": ""}, {"tasks": ["x"]}, {"tasks": [0]}):
        with pytest.raises(AgoraError) as err:
            ag.handover(_args(**kw))
        assert err.value.status == 400, kw
    with pytest.raises(AgoraError):
        ag.handover({k: v for k, v in _args().items() if k != "reason"})
    with pytest.raises(AgoraError) as err:
        ag.handover(_args(agent="luis"))                         # without the page's mark nobody speaks as the person
    assert err.value.status == 403


# ---- what moves ---------------------------------------------------------------------------------------------------

def test_ownership_locks_and_reviewer_move_and_everything_else_stays(ag):
    ids = _world(ag)
    before = {k: _task(ag, v)["task"] for k, v in ids.items()}
    old_locks = {lk["resource"]: lk for lk in ag.locks()["locks"] if lk["owner"] == OLD}
    assert set(old_locks) == {"path:hoardlink/a.py", "path:hoardlink/b.py", "path:hoardlink/c.py",
                              "path:hoardlink/e.py", "model:principal"}
    ag.fake_clock.t += 5 * MIN
    r = ag.handover(_args(reason="chat died"))
    assert sorted(r["owner_moved"]) == sorted(ids[k] for k in ("claimed", "changes", "approved", "working"))
    assert sorted(r["reviewer_moved"]) == sorted([ids["rev1"], ids["rev2"]])
    now = ag.fake_clock.t
    for key in ("claimed", "changes", "approved", "working"):
        t = _task(ag, ids[key])["task"]
        b = before[key]
        assert t["owner"] == NEW
        for field in ("status", "review_state", "submission_revision", "reviewed_submission_revision", "reviewed",
                      "reviewed_commits", "commits", "branch", "claimed_at", "submitted_at", "reviewer", "thread_id", "kind"):
            assert t[field] == b[field], (key, field)
    # live locks of the moved tasks now belong to the successor: same resources, TTL renewed from now
    new_locks = {lk["resource"]: lk for lk in ag.locks()["locks"] if lk["owner"] == NEW}
    assert set(new_locks) == set(old_locks) and set(r["locks_moved"]) == set(old_locks)
    for res, lk in new_locks.items():
        assert lk["task_id"] == old_locks[res]["task_id"] and lk["ttl_s"] == old_locks[res]["ttl_s"]
        assert lk["acquired"] == old_locks[res]["acquired"] and lk["expires"] == now + lk["ttl_s"]
    assert not [lk for lk in ag.locks()["locks"] if lk["owner"] == OLD]
    # checkpoints and their author stay; the successor continues the same revision chain
    cp = ag.checkpoints({"task_id": ids["changes"]})["checkpoints"][0]
    assert cp["author"] == OLD and cp["current_lock_status"]["owner_changed"] is True
    assert ag.checkpoint({"agent": NEW, "task_id": ids["changes"], "expected_revision": 1,
                          "payload": {"summary": "continued"}})["ok"]
    # the successor can finish what was approved
    done = ag.task_done({"agent": NEW, "task_id": ids["approved"], "commits": ["c2"], "result": "merged"})
    assert done["task"]["status"] == "done" and done["task"]["review_state"] == "approved"


def test_reviews_move_only_for_review_and_changes(ag):
    ids = _world(ag)
    r = ag.handover(_args())
    assert sorted(r["reviewer_moved"]) == sorted([ids["rev1"], ids["rev2"]])
    for key in ("rev1", "rev2"):
        t = _task(ag, ids[key])["task"]
        assert t["reviewer"] == NEW and t["owner"] == "claude"
    assert _task(ag, ids["rev2"])["task"]["status"] == "changes"
    # the approving/rejecting reviewer recorded on the owner's own tasks is history and stays
    assert _task(ag, ids["changes"])["task"]["reviewer"] == "claude"
    assert _task(ag, ids["other"])["task"]["reviewer"] == "claude"
    assert _task(ag, ids["other"])["task"]["owner"] == "other"
    # the successor sees the reviews in its inbox
    inbox = ag.inbox({"agent": NEW, "peek": True})
    assert ids["rev1"] in [t["id"] for t in inbox["reviews"]]


def test_without_reviews(ag):
    ids = _world(ag)
    r = ag.handover(_args(include_reviews=False))
    assert r["reviewer_moved"] == [] and _task(ag, ids["rev1"])["task"]["reviewer"] == OLD
    assert _task(ag, ids["claimed"])["task"]["owner"] == NEW
    assert ag.handover(_args(include_reviews="false"))["noop"] is True
    assert ag.handover(_args(include_reviews=True))["reviewer_moved"]


def test_a_new_owner_never_reviews_its_own_task(ag):
    ids = _world(ag)
    # codex-bucle owns a task under review by the successor, and reviews a task that the successor owns
    t_a = _add(ag, OLD, "reviewed by the relevo", "i.py")
    ag.task_submit({"agent": OLD, "task_id": t_a, "summary": "s", "commits": ["c6"], "reviewer": NEW})
    t_b = _add(ag, NEW, "owned by the relevo", "j.py")
    ag.task_submit({"agent": NEW, "task_id": t_b, "summary": "s", "commits": ["c7"], "reviewer": OLD})
    ag.heartbeat({"agent": NEW})
    ag.fake_clock.t += 31 * MIN
    ag.heartbeat({"agent": NEW})
    ag.heartbeat({"agent": "claude"})
    r = ag.handover(_args())
    assert t_a in r["owner_moved"] and t_b in r["reviewer_moved"] and ids["claimed"] in r["owner_moved"]
    for tid in (t_a, t_b):
        t = _task(ag, tid)["task"]
        assert t["status"] == "review" and t["reviewer"] is None and t["owner"] == NEW
    # anyone but the owner can review them now
    assert ag.task_review({"agent": "claude", "task_id": t_a, "expected_submission_revision": 1,
                           "verdict": "approve", "body": "ok"})["ok"]


def test_done_tasks_are_never_touched(ag):
    ids = _world(ag)
    done_before = _task(ag, ids["done"])
    msgs_before = len(done_before["messages"])
    r = ag.handover(_args())
    assert ids["done"] not in r["owner_moved"]
    after = _task(ag, ids["done"])
    assert after["task"] == done_before["task"] and after["task"]["owner"] == OLD
    assert len(after["messages"]) == msgs_before
    # asking for it by number is skipped with a reason, not moved
    r2 = ag.handover(_args(tasks=[ids["done"]]))
    assert r2["noop"] and r2["skipped"][0]["task_id"] == ids["done"] and "final" in r2["skipped"][0]["reason"]
    assert _task(ag, ids["done"])["task"]["owner"] == OLD


def test_explicit_task_list(ag):
    ids = _world(ag)
    r = ag.handover(_args(tasks=[ids["claimed"], ids["rev1"]]))
    assert r["owner_moved"] == [ids["claimed"]] and r["reviewer_moved"] == [ids["rev1"]]
    assert _task(ag, ids["changes"])["task"]["owner"] == OLD                      # not listed
    assert _task(ag, ids["rev2"])["task"]["reviewer"] == OLD
    # the same list again: already with the successor, nothing happens
    again = ag.handover(_args(tasks=[ids["claimed"], ids["rev1"]]))
    assert again["noop"] and {s["task_id"] for s in again["skipped"]} == {ids["claimed"], ids["rev1"]}
    # a task that has nothing to do with the source is refused as a whole (nothing moves)
    before = _fingerprint(ag)
    with pytest.raises(AgoraError) as err:
        ag.handover(_args(tasks=[ids["changes"], ids["other"]]))
    assert err.value.status == 409 and str(ids["other"]) in str(err.value)
    with pytest.raises(AgoraError) as err:
        ag.handover(_args(tasks=[ids["changes"], 9999]))
    assert err.value.status == 404
    assert _fingerprint(ag) == before
    # accepted as comma text and #n as well
    assert ag.handover(_args(tasks=f"#{ids['changes']}, {ids['approved']}"))["owner_moved"] == [ids["changes"], ids["approved"]]


# ---- what it records ----------------------------------------------------------------------------------------------

def test_messages_event_and_announcement(ag):
    ids = _world(ag)
    r = ag.handover(_args(reason="Codex died"))
    for key in ("claimed", "changes", "approved", "working", "rev1", "rev2"):
        msgs = _task(ag, ids[key])["messages"]
        last = msgs[-1]
        assert last["kind"] == "system" and last["author"] == "claude"
        assert last["body"].splitlines()[0] == f"claude traspasa de {OLD} a {NEW}: Codex died"
    assert "Bloqueos: path:hoardlink/a.py" in _task(ag, ids["claimed"])["messages"][-1]["body"]
    assert "Revisi" in _task(ag, ids["rev1"])["messages"][-1]["body"]
    assert _task(ag, ids["done"])["messages"][-1]["kind"] == "resolution"
    # no thread 32 here: a note thread is opened and mentions the successor
    note = ag.thread({"thread_id": r["thread_id"]})
    assert note["thread"]["kind"] == "note" and OLD in note["thread"]["title"] and NEW in note["thread"]["title"]
    post = note["messages"][0]
    assert post["author"] == "claude" and post["mentions"] == [NEW] and "Codex died" in post["body"]
    assert f"#{ids['claimed']}" in post["body"]
    pending = ag.inbox({"agent": NEW, "peek": True})["pending"]
    assert any(m["thread_id"] == r["thread_id"] for m in pending)
    handover_events = [d for t, d in ag.events if t == "agora.handover"]
    assert len(handover_events) == 1
    ev = handover_events[0]
    assert ev["by"] == "claude" and ev["from"] == OLD and ev["to"] == NEW and ev["reason"] == "Codex died"
    assert sorted(ev["tasks"]) == sorted(r["owner_moved"]) and ev["thread_id"] == r["thread_id"]
    # the successor is touched by its own heartbeat, the caller is registered activity
    assert ag.agents()["agents"]


def test_summary_goes_to_the_coordination_thread_when_it_exists(tmp_path):
    clock = Clock()
    a = Agora(tmp_path / "agora.db", clock=clock)
    try:
        for who in (OLD, NEW, "claude"):
            a.heartbeat({"agent": who})
        coord = a.thread_open({"agent": "claude", "title": "coordination", "body": "hi", "kind": "debate"})["thread"]["id"]
        a.handover_thread_id = coord
        _add(a, OLD, "t", "a.py")
        clock.t += 31 * MIN
        a.heartbeat({"agent": NEW})
        a.heartbeat({"agent": "claude"})
        n_threads = a.db.scalar("SELECT COUNT(*) FROM threads", default=0)
        r = a.handover(_args())
        assert r["thread_id"] == coord and a.db.scalar("SELECT COUNT(*) FROM threads", default=0) == n_threads
        last = a.thread({"thread_id": coord})["messages"][-1]
        assert last["kind"] == "comment" and last["mentions"] == [NEW] and f"traspasa de {OLD} a {NEW}" in last["body"]
    finally:
        a.close()


def test_idempotent(ag):
    ids = _world(ag)
    first = ag.handover(_args())
    assert sorted(first["owner_moved"]) == sorted(
        ids[k] for k in ("claimed", "changes", "approved", "working")) and not first["noop"]
    state = _fingerprint(ag)
    again = ag.handover(_args())
    assert again["ok"] and again["noop"] and again["tasks"] == [] and again["thread_id"] is None
    assert again["owner_moved"] == [] and again["locks_moved"] == []
    assert _fingerprint(ag) == state                                      # no rows, messages, threads or events
    again_p = ag.handover({**_args(agent="luis"), "_person": True})
    assert again_p["noop"] and _fingerprint(ag) == state


def test_atomic_when_something_fails_half_way(ag, monkeypatch):
    _world(ag)
    before = _fingerprint(ag)
    real = ag._msg
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full")
        return real(*a, **kw)

    monkeypatch.setattr(ag, "_msg", flaky)
    with pytest.raises(RuntimeError):
        ag.handover(_args())
    assert calls["n"] == 3
    assert _fingerprint(ag) == before                                      # tasks, locks, messages, events: untouched
    monkeypatch.setattr(ag, "_msg", real)
    assert ag.handover(_args())["owner_moved"]                            # and it works once the fault is gone


def test_other_locks_and_tasks_of_the_source_that_are_not_moved_stay(ag):
    ids = _world(ag)
    ag.lock({"agent": OLD, "resources": ["merge:HoardLink"]})              # a merge lock with no task
    ag.fake_clock.t += 31 * MIN
    ag.heartbeat({"agent": NEW})
    ag.heartbeat({"agent": "claude"})
    ag.handover(_args(tasks=[ids["claimed"]]))
    held_old = {lk["resource"] for lk in ag.locks()["locks"] if lk["owner"] == OLD}
    assert "path:hoardlink/a.py" not in held_old and "merge:hoardlink" in held_old and "path:hoardlink/b.py" in held_old
    ag.handover(_args())
    assert {lk["resource"] for lk in ag.locks()["locks"] if lk["owner"] == OLD} == {"merge:hoardlink"}


# ---- through the hub: HTTP, tool, terminal ------------------------------------------------------------------------

def _cli(root, env, *args, agent="codex-bucle"):
    return subprocess.run([sys.executable, str(root / "scripts" / "agora.py"), "--as", agent, "--json", *args],
                          cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)


def test_http_tool_cli_and_mcp_smoke(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    try:
        token = Path(hub.config.token_file).read_text(encoding="utf-8").strip()
        hdr = {"Authorization": "Bearer " + token}
        page = {"Sec-Fetch-Site": "same-origin"}
        ag = hub.facet("agora").agora
        names = [t["name"] for t in tools.all_tools()]
        assert names.count("hub_agora_handover") == 1
        schema = next(t for t in tools.all_tools() if t["name"] == "hub_agora_handover")["inputSchema"]
        assert schema["required"] == ["agent", "from_agent", "to_agent", "reason"] and schema["additionalProperties"] is False
        for who in (OLD, NEW, "claude"):
            ag.heartbeat({"agent": who})
        ids = [_add(ag, OLD, f"t{i}", f"f{i}.py") for i in range(5)]

        # an agent with the token is refused while the source is fresh (409 over HTTP); no token, no entry
        body = {"agent": "claude", "from_agent": OLD, "to_agent": NEW, "reason": "test", "tasks": [ids[0]]}
        assert http(base + "/api/agora/handover", body)[0] == 401
        status, refused = http(base + "/api/agora/handover", body, headers=hdr)
        assert status == 409 and not refused["ok"] and "luis" in refused["error"]
        # the tool is refused the same way and can never speak as the person
        assert tools.call(hub, "hub_agora_handover", body)["status"] == 409
        assert tools.call(hub, "hub_agora_handover", {**body, "agent": "luis", "_person": True})["ok"] is False
        # the hub's page acts as the person
        status, done = http(base + "/api/agora/handover", {"from_agent": OLD, "to_agent": NEW, "reason": "page", "tasks": [ids[0]]},
                            headers=page)
        assert status == 200 and done["ok"] and done["by"] == "luis" and done["owner_moved"] == [ids[0]]
        assert ag.task({"task_id": ids[0]})["task"]["owner"] == NEW
        evs = hub.events.query(type="agora.handover", limit=10)
        evs = evs.get("events", evs) if isinstance(evs, dict) else evs
        assert len(evs) == 1

        # the tool, as the source itself (voluntary handover), for one task
        res = tools.call(hub, "hub_agora_handover", {"agent": OLD, "from_agent": OLD, "to_agent": NEW, "reason": "tool",
                                                     "tasks": [ids[1]]})
        assert res["ok"] and res["owner_moved"] == [ids[1]]

        env = os.environ.copy()
        env.update({"HOARD_HUB_URL": base, "HOARD_HUB_TOKEN_FILE": str(hub.config.token_file),
                    "HOARD_HUB_DATA_DIR": str(hub.config.data_dir), "HOARD_HUB_AUTOSTART": "0"})
        root = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")

        blocked = _cli(root, env, "handover", "--from", OLD, "--to", NEW, "--reason", "cli", agent="claude")
        assert blocked.returncode == 1 and json.loads(blocked.stdout)["status"] == 409
        cli = _cli(root, env, "handover", "--from", OLD, "--to", NEW, "--reason", "cli", "--tasks", f"{ids[2]}")
        assert cli.returncode == 0, cli.stderr + cli.stdout
        assert json.loads(cli.stdout)["owner_moved"] == [ids[2]]
        text = subprocess.run([sys.executable, str(root / "scripts" / "agora.py"), "--as", OLD, "handover", "--from", OLD,
                               "--to", NEW, "--reason", "cli text", "--no-reviews"],
                              cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert text.returncode == 0, text.stderr
        assert f"Traspaso de {OLD} a {NEW}" in text.stdout and f"#{ids[3]}" in text.stdout and f"#{ids[4]}" in text.stdout
        again = _cli(root, env, "handover", "--from", OLD, "--to", NEW, "--reason", "cli again")
        assert again.returncode == 0 and json.loads(again.stdout)["noop"] is True

        # the MCP bridge (stdio) lists and runs it
        ids2 = [_add(ag, OLD, f"m{i}", f"m{i}.py") for i in range(1)]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_agora_handover", "arguments": {"agent": OLD, "from_agent": OLD, "to_agent": NEW,
                                                               "reason": "mcp", "tasks": ids2}}},
        ]
        mcp = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"],
                             input="\n".join(json.dumps(r) for r in requests) + "\n",
                             cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert mcp.returncode == 0, mcp.stderr
        replies = {item["id"]: item for item in (json.loads(line) for line in mcp.stdout.splitlines()) if "id" in item}
        assert sum(t["name"] == "hub_agora_handover" for t in replies[2]["result"]["tools"]) == 1
        payload = json.loads(replies[3]["result"]["content"][0]["text"])
        assert payload["ok"] and payload["owner_moved"] == ids2
        assert ag.task({"task_id": ids2[0]})["task"]["owner"] == NEW
    finally:
        server.shutdown()
        server.server_close()
        hub.close()


# ---- locks that follow the reviewer role ---------------------------------------------------------------------------

def _review_world(ag, n=2):
    """claude owns n tasks (own lock each) under review by codex-bucle, who holds a notes lock tied to each; plus a
    task-less lock of codex-bucle and an unrelated lock of the successor."""
    for who in (OLD, NEW, "claude"):
        ag.heartbeat({"agent": who})
    ids = []
    for i in range(n):
        tid = _add(ag, "claude", f"authored {i}", f"author{i}.py")
        ag.task_submit({"agent": "claude", "task_id": tid, "summary": "s", "commits": [f"c{i}"], "reviewer": OLD})
        assert ag.lock({"agent": OLD, "resources": [f"path:HoardLink/review-notes{i}.md"], "task_id": tid})["ok"]
        ids.append(tid)
    assert ag.lock({"agent": OLD, "resources": ["merge:HoardLink"]})["ok"]
    assert ag.lock({"agent": NEW, "resources": ["model:principal"]})["ok"]
    ag.fake_clock.t += 31 * MIN
    ag.heartbeat({"agent": NEW})
    ag.heartbeat({"agent": "claude"})
    return ids


def _owners(ag):
    return {lk["resource"]: lk["owner"] for lk in ag.locks()["locks"]}


def test_reviewer_only_move_transfers_the_source_lock_tied_to_the_task(ag):
    (tid, other) = _review_world(ag)
    before = {lk["resource"]: lk for lk in ag.locks()["locks"]}
    ag.fake_clock.t += 5 * MIN
    r = ag.handover(_args(tasks=[tid]))
    assert r["owner_moved"] == [] and r["reviewer_moved"] == [tid]
    assert r["locks_moved"] == ["path:hoardlink/review-notes0.md"]
    owners = _owners(ag)
    assert owners["path:hoardlink/review-notes0.md"] == NEW
    moved = next(lk for lk in ag.locks()["locks"] if lk["resource"] == "path:hoardlink/review-notes0.md")
    old = before["path:hoardlink/review-notes0.md"]
    assert moved["task_id"] == tid and moved["acquired"] == old["acquired"] and moved["expires"] == ag.fake_clock.t + moved["ttl_s"]
    # the author's locks, the successor's own, task-less locks and other tasks' locks stay where they were
    assert owners["path:hoardlink/author0.py"] == "claude" and owners["path:hoardlink/author1.py"] == "claude"
    assert owners["model:principal"] == NEW and owners["merge:hoardlink"] == OLD
    assert owners["path:hoardlink/review-notes1.md"] == OLD
    assert _task(ag, tid)["task"]["owner"] == "claude" and _task(ag, other)["task"]["reviewer"] == OLD
    assert "Bloqueos: path:hoardlink/review-notes0.md" in _task(ag, tid)["messages"][-1]["body"]
    # everything at once moves the rest of the task-bound locks too, never the author's
    ag.handover(_args())
    owners = _owners(ag)
    assert owners["path:hoardlink/review-notes1.md"] == NEW and owners["merge:hoardlink"] == OLD
    assert owners["path:hoardlink/author1.py"] == "claude"


def test_without_reviews_reviewer_only_tasks_and_their_locks_stay(ag):
    (tid, other) = _review_world(ag)
    before = _owners(ag)
    r = ag.handover(_args(include_reviews=False))
    assert r["noop"] and r["locks_moved"] == []
    assert _owners(ag) == before and _task(ag, tid)["task"]["reviewer"] == OLD
    # an explicit list with --no-reviews on a reviewer-only task is refused: nothing to move for it
    with pytest.raises(AgoraError) as err:
        ag.handover(_args(tasks=[tid], include_reviews=False))
    assert err.value.status == 409 and _owners(ag) == before


def test_task_subset_moves_only_the_locks_of_that_subset(ag):
    (tid, other) = _review_world(ag)
    mine = _add(ag, OLD, "owned by the source", "owned.py")
    third = _add(ag, OLD, "also owned", "owned2.py")
    ag.fake_clock.t += 31 * MIN
    ag.heartbeat({"agent": NEW})
    ag.heartbeat({"agent": "claude"})
    r = ag.handover(_args(tasks=[other, mine]))
    assert r["owner_moved"] == [mine] and r["reviewer_moved"] == [other]
    assert sorted(r["locks_moved"]) == ["path:hoardlink/owned.py", "path:hoardlink/review-notes1.md"]
    owners = _owners(ag)
    assert owners["path:hoardlink/owned.py"] == NEW and owners["path:hoardlink/review-notes1.md"] == NEW
    assert owners["path:hoardlink/owned2.py"] == OLD and owners["path:hoardlink/review-notes0.md"] == OLD
    assert _task(ag, third)["task"]["owner"] == OLD and _task(ag, tid)["task"]["reviewer"] == OLD
