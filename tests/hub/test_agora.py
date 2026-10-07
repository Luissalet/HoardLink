"""Ágora: resources and conflicts, tasks with locks (all or nothing), review flow, threads, escalation, inbox
(long poll, read marks), the person-only identity, HTTP routes and the agent tools through the hub."""

from __future__ import annotations

import threading
import time

import pytest

from hoard_link.hub import tools
from hoard_link.hub.agora import Agora, AgoraError, conflicts, normalize_resource

from ._hub_fakes import http, make_hub, serve


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def ag(tmp_path):
    events, escalations = [], []
    clock = Clock()
    a = Agora(tmp_path / "agora.db", clock=clock, emit=lambda t, d: events.append((t, d)),
              escalate_hook=lambda th, who, q: escalations.append((th["id"], who, q)))
    a.events, a.escalations, a.fake_clock = events, escalations, clock
    yield a
    a.close()


# ---- resources -------------------------------------------------------------------------------------------------

def test_normalize_resource():
    assert normalize_resource("Path:Faustus\\src\\Agent_Loop.py") == "path:faustus/src/agent_loop.py"
    assert normalize_resource("path:Faustus") == "path:faustus/"
    assert normalize_resource("merge:HoardLink") == "merge:hoardlink"
    assert normalize_resource("model:principal") == "model:principal"
    for bad in ("nocolon", "path:", "repo:a/b", "path:x/../y", "Bad Kind:x"):
        with pytest.raises(AgoraError):
            normalize_resource(bad)


def test_conflicts():
    n = normalize_resource
    assert conflicts(n("path:Faustus/src/"), n("path:Faustus/src/agent_loop.py"))
    assert conflicts(n("path:Faustus/src/a.py"), n("path:faustus/SRC/A.py"))
    assert not conflicts(n("path:Faustus/src/a.py"), n("path:Faustus/src/ab.py"))
    assert not conflicts(n("path:Faustus/src/a.py"), n("path:HoardLink/src/a.py"))
    assert conflicts(n("path:Faustus/src/*.py"), n("path:Faustus/src/x.py"))
    assert conflicts(n("repo:Faustus"), n("path:Faustus/README.md"))
    assert conflicts(n("repo:Faustus"), n("merge:Faustus"))
    assert conflicts(n("merge:Faustus"), n("merge:faustus"))
    assert not conflicts(n("merge:Faustus"), n("path:Faustus/x.py"))
    assert conflicts(n("model:principal"), n("model:principal")) and not conflicts(n("gpu:1"), n("gpu:2"))


# ---- tasks and locks -------------------------------------------------------------------------------------------

def test_claim_takes_locks_all_or_nothing(ag):
    t1 = ag.task_add({"agent": "codex", "title": "Fix loop", "repo": "Faustus", "paths": ["src/agent_loop.py"]})["task"]
    t2 = ag.task_add({"agent": "claude", "title": "Docs", "repo": "Faustus", "paths": ["README.md", "src/"]})["task"]
    r1 = ag.task_claim({"agent": "codex", "task_id": t1["id"]})
    assert r1["ok"] and r1["locks"] == ["path:faustus/src/agent_loop.py"] and r1["task"]["status"] == "claimed"
    r2 = ag.task_claim({"agent": "claude", "task_id": t2["id"]})
    assert not r2["ok"] and r2["status"] == 409
    assert r2["conflicts"][0]["owner"] == "codex" and r2["conflicts"][0]["resource"] == "path:faustus/src/"
    # nothing was taken: README.md is still free
    assert ag.locks({"check": "path:Faustus/README.md"})["free"]
    assert ag.task({"task_id": t2["id"]})["task"]["owner"] is None
    # someone else's active task can not be claimed
    with pytest.raises(AgoraError) as exc:
        ag.task_claim({"agent": "claude", "task_id": t1["id"]})
    assert exc.value.status == 409
    assert ("agora.lock.conflict" in [e[0] for e in ag.events])


def test_heartbeat_renews_and_ttl_expires(ag):
    ag.lock({"agent": "codex", "resources": ["model:principal"], "ttl_s": 600, "note": "eval"})
    ag.fake_clock.t += 500
    ag.heartbeat({"agent": "codex", "doing": "evaluating"})
    ag.fake_clock.t += 500
    assert not ag.locks({"check": "model:principal"})["free"]          # renewed at +500 → expires at +1100
    ag.fake_clock.t += 200
    assert ag.locks({"check": "model:principal"})["free"]               # expired: nobody renewed it
    assert ag.lock({"agent": "claude", "resources": ["model:principal"]})["ok"]
    with pytest.raises(AgoraError):
        ag.unlock({"agent": "codex", "resources": ["model:principal"]})


def test_stale_owner_can_be_taken_over_with_force(ag):
    t = ag.task_add({"agent": "codex", "title": "X", "repo": "R", "paths": ["a.py"], "claim": True})
    assert t["claim"]["ok"]
    tid = t["task"]["id"]
    ag.fake_clock.t += 7 * 3600
    with pytest.raises(AgoraError):
        ag.task_claim({"agent": "claude", "task_id": tid})
    r = ag.task_claim({"agent": "claude", "task_id": tid, "force": True})
    assert r["ok"] and r["task"]["owner"] == "claude"
    msgs = ag.task({"task_id": tid})["messages"]
    assert any("6 h" in m["body"] for m in msgs)


def test_review_flow(ag):
    tid = ag.task_add({"agent": "claude", "title": "Agora UI", "repo": "HoardLink", "paths": ["hoard_link/hub/ui/agora.js"],
                       "claim": True})["task"]["id"]
    ag.task_update({"agent": "claude", "task_id": tid, "status": "in_progress", "note": "half way", "branch": "agora"})
    with pytest.raises(AgoraError):
        ag.task_submit({"agent": "codex", "task_id": tid, "summary": "not mine"})
    ag.task_submit({"agent": "claude", "task_id": tid, "summary": "done, 12 tests", "commits": ["abc123"]})
    # codex sees the review in its inbox
    inbox = ag.inbox({"agent": "codex"})
    assert [t["id"] for t in inbox["reviews"]] == [tid] and inbox["counts"]["reviews"] == 1
    with pytest.raises(AgoraError) as exc:
        ag.task_done({"agent": "claude", "task_id": tid})
    assert "waiting for review" in str(exc.value)
    with pytest.raises(AgoraError):
        ag.task_review({"agent": "claude", "task_id": tid, "verdict": "approve", "body": "self"})
    ag.task_review({"agent": "codex", "task_id": tid, "verdict": "changes", "body": "missing mobile layout"})
    assert ag.inbox({"agent": "claude", "peek": True})["counts"]["changes"] == 1
    with pytest.raises(AgoraError):
        ag.task_done({"agent": "claude", "task_id": tid})
    ag.task_submit({"agent": "claude", "task_id": tid, "summary": "mobile fixed"})
    ag.task_review({"agent": "codex", "task_id": tid, "verdict": "approve", "body": "checked at 390 px"})
    done = ag.task_done({"agent": "claude", "task_id": tid, "result": "merged", "commits": ["def456"]})["task"]
    assert done["status"] == "done" and done["reviewed"] and done["commits"] == ["def456"]
    assert ag.locks()["locks"] == []


def test_review_grace_and_force(ag):
    tid = ag.task_add({"agent": "claude", "title": "t", "claim": True})["task"]["id"]
    ag.task_submit({"agent": "claude", "task_id": tid, "summary": "s"})
    with pytest.raises(AgoraError):
        ag.task_done({"agent": "claude", "task_id": tid, "force": True})          # force needs a reason
    ag.fake_clock.t += 2 * 3600 + 1
    done = ag.task_done({"agent": "claude", "task_id": tid, "result": "ok"})["task"]
    assert done["status"] == "done" and not done["reviewed"]
    assert "sin revisión" in ag.task({"task_id": tid})["messages"][-1]["body"]


def test_release_and_drop(ag):
    tid = ag.task_add({"agent": "codex", "title": "t", "repo": "R", "paths": ["x"], "claim": True})["task"]["id"]
    r = ag.task_release({"agent": "codex", "task_id": tid, "reason": "no time"})["task"]
    assert r["status"] == "open" and r["owner"] is None and ag.locks()["locks"] == []
    ag.task_claim({"agent": "claude", "task_id": tid})
    assert ag.task_release({"agent": "claude", "task_id": tid, "drop": True, "reason": "dup of 3"})["task"]["status"] == "dropped"


# ---- threads, escalation, inbox ---------------------------------------------------------------------------------

def test_debate_escalation_and_decision_log(ag):
    th = ag.thread_open({"agent": "codex", "title": "q8 or q4 for tests", "body": "I propose q8",
                         "kind": "debate", "mentions": ["claude"]})["thread"]
    ag.post({"agent": "claude", "thread_id": th["id"], "body": "q4: criteria say so", "kind": "disagree"})
    detail = ag.thread({"thread_id": th["id"]})
    assert detail["stances"] == {"codex": "proposal", "claude": "disagree"}
    ag.escalate({"agent": "claude", "thread_id": th["id"], "question": "q8 500k or q4?"})
    assert ag.escalations == [(th["id"], "claude", "q8 500k or q4?")]
    person = ag.inbox({"agent": "luis", "peek": True})
    assert [t["id"] for t in person["escalated"]] == [th["id"]]
    with pytest.raises(AgoraError) as exc:
        ag.post({"agent": "luis", "thread_id": th["id"], "body": "q4"})
    assert exc.value.status == 403
    ag.resolve({"agent": "luis", "thread_id": th["id"], "resolution": "q4 for tests", "_person": True})
    assert ag.decisions()["decisions"][0]["resolution"] == "q4 for tests"
    assert ag.board()["escalated"] == 0


def test_inbox_read_marks_and_for_you(ag):
    th = ag.thread_open({"agent": "codex", "title": "note", "body": "fyi", "kind": "note"})["thread"]
    ag.thread_open({"agent": "codex", "title": "ask", "body": "@claude?", "kind": "note", "mentions": ["claude"]})
    first = ag.inbox({"agent": "claude"})
    assert first["counts"]["messages"] == 2 and first["counts"]["for_you"] == 1
    assert ag.inbox({"agent": "claude"})["counts"]["total"] == 0          # read mark moved
    ag.post({"agent": "claude", "thread_id": th["id"], "body": "ok"})
    ag.post({"agent": "codex", "thread_id": th["id"], "body": "thanks"})
    again = ag.inbox({"agent": "claude", "peek": True})
    assert again["counts"]["for_you"] == 1                                 # took part in that thread
    assert ag.inbox({"agent": "claude", "peek": True})["counts"]["messages"] == 1   # peek did not move it


def test_inbox_long_poll_wakes_up(tmp_path):
    a = Agora(tmp_path / "a.db")
    try:
        th = a.thread_open({"agent": "codex", "title": "t", "body": "b"})["thread"]
        a.inbox({"agent": "claude"})
        out = {}

        def waiter():
            out["r"] = a.inbox({"agent": "claude", "wait_s": 10})

        w = threading.Thread(target=waiter)
        start = time.time()
        w.start()
        time.sleep(0.3)
        a.post({"agent": "codex", "thread_id": th["id"], "body": "reply"})
        w.join(5)
        assert out["r"]["counts"]["messages"] == 1 and time.time() - start < 4
    finally:
        a.close()


def test_agent_ids_and_validation(ag):
    for bad in ("", "X Y", "hub", "ui", "a"):
        with pytest.raises(AgoraError):
            ag.heartbeat({"agent": bad})
    with pytest.raises(AgoraError):
        ag.task_add({"agent": "codex", "title": "t", "priority": 9})
    with pytest.raises(AgoraError):
        ag.thread_open({"agent": "codex", "title": "t", "body": "b", "kind": "task"})
    hb = ag.heartbeat({"agent": "codex-craft", "doing": "Lumiere", "state": "working"})
    assert hb["ok"] and ag.agents()["agents"][0]["kind"] == "agent"


# ---- through the hub: HTTP and tools ----------------------------------------------------------------------------

def test_http_and_tools(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    token = open(hub.config.token_file, encoding="utf-8").read().strip() if hasattr(hub.config, "token_file") else None
    try:
        assert hub.facet("agora") is not None
        names = {t["name"] for t in tools.all_tools()}
        assert {"hub_agora_board", "hub_agora_inbox", "hub_agora_task_claim", "hub_agora_escalate"} <= names
        r = tools.call(hub, "hub_agora_task_add", {"agent": "codex", "title": "From a tool", "repo": "HoardLink"})
        assert r["ok"]
        # a tool can never speak as the person
        assert tools.call(hub, "hub_agora_task_add", {"agent": "luis", "title": "x", "_person": True})["ok"] is False
        # writes without a token or the page are refused
        status, body = http(base + "/api/agora/heartbeat", {"agent": "claude"})
        assert status == 401
        hdr = {"Authorization": "Bearer " + token} if token else {}
        status, body = http(base + "/api/agora/heartbeat", {"agent": "claude", "doing": "testing"}, headers=hdr)
        assert status == 200 and body["ok"], body
        # the page writes as the person
        page = {"Sec-Fetch-Site": "same-origin"}
        status, body = http(base + "/api/agora/thread_open", {"title": "Hola", "body": "¿Qué tal?", "kind": "question"},
                            headers=page)
        assert status == 200 and body["thread"]["created_by"] == "luis", body
        status, board = http(base + "/api/agora/board")
        assert status == 200 and board["counts"]["open"] == 1 and len(board["threads"]) == 1
        status, inbox = http(base + "/api/agora/inbox?agent=codex&peek=1")
        assert inbox["counts"]["messages"] == 1 and inbox["messages"][0]["author"] == "luis"
        status, err = http(base + "/api/agora/tasks/999")
        assert status == 404
        evs = hub.events.query(type="agora.*", limit=50)
        evs = evs.get("events", evs) if isinstance(evs, dict) else evs
        assert any(e["type"] == "agora.thread.opened" for e in evs)
    finally:
        server.shutdown()
        hub.close()
