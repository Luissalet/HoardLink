"""Ágora: resources and conflicts, tasks with locks (all or nothing), review flow, threads, escalation, inbox
(long poll, read marks), the person-only identity, HTTP routes and the agent tools through the hub."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub import agora as agora_mod
from hoard_link.hub.agora import (
    Agora,
    AgoraError,
    _normalize_repo_relpath,
    conflicts,
    normalize_resource,
    normalize_submission_blocks,
    verify_code_peek,
)

from ._hub_fakes import http, make_hub, serve


def _git_repo(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True)
    return sha.stdout.strip()


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
        ag.task_review({"agent": "claude", "task_id": tid, "expected_submission_revision": 1,
                        "verdict": "approve", "body": "self"})
    ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                    "verdict": "changes", "body": "missing mobile layout"})
    assert ag.inbox({"agent": "claude", "peek": True})["counts"]["changes"] == 1
    with pytest.raises(AgoraError):
        ag.task_done({"agent": "claude", "task_id": tid})
    ag.task_submit({"agent": "claude", "task_id": tid, "summary": "mobile fixed"})
    ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 2,
                    "verdict": "approve", "body": "checked at 390 px"})
    with pytest.raises(AgoraError) as exc:               # def456 is not what codex approved
        ag.task_done({"agent": "claude", "task_id": tid, "result": "merged", "commits": ["def456"]})
    assert "were not reviewed" in str(exc.value)
    done = ag.task_done({"agent": "claude", "task_id": tid, "result": "merged", "commits": ["def456"], "force": True,
                         "reason": "def456 is abc123 rebased onto main, same diff"})["task"]
    assert done["status"] == "done" and done["commits"] == ["def456"] and done["reviewed_commits"] == ["abc123"]
    assert done["review_state"] == "equivalent"           # not an approval of def456: codex never saw it
    resolution = ag.thread({"thread_id": done["thread_id"]})["messages"][-1]["body"]
    assert "abc123" in resolution and "def456" in resolution and "same diff" in resolution
    assert ag.digest({"hours": 1})["done_by_review"]["equivalent"] == 1
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
    assert person["escalated"][0]["question"] == "q8 500k or q4?" and person["escalated"][0]["escalated_by"] == "claude"
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


def test_person_answer_takes_thread_out_of_escalation_and_task_threads_show(ag):
    tid = ag.task_add({"agent": "codex", "title": "t", "claim": True})["task"]["id"]
    task = ag.task({"task_id": tid})["task"]
    ag.escalate({"agent": "codex", "thread_id": task["thread_id"], "question": "keep or drop?"})
    listed = ag.threads()["threads"]
    assert [t["id"] for t in listed] == [task["thread_id"]]            # escalated task threads are listed
    ag.post({"agent": "luis", "thread_id": task["thread_id"], "body": "keep", "_person": True})
    assert ag.thread({"thread_id": task["thread_id"]})["thread"]["status"] == "open"
    assert ag.threads()["threads"] == []
    ag.task_submit({"agent": "codex", "task_id": tid, "summary": "s"})
    ag.escalate({"agent": "codex", "thread_id": task["thread_id"], "question": "approve?"})
    ag.task_review({"agent": "luis", "task_id": tid, "expected_submission_revision": 1,
                    "verdict": "approve", "body": "ok", "_person": True})
    assert ag.board()["escalated"] == 0


def test_cli_repairs_windows_double_encoding():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("agora_cli", Path(__file__).resolve().parents[2] / "scripts" / "agora.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.fix_text("Â¿q4 o q8?") == "¿q4 o q8?"
    assert cli.fix_text("Probando el \u00c3\u0081gora") == "Probando el Ágora"
    assert cli.fix_text("c├│mo, due├▒o") == "cómo, dueño"
    assert cli.fix_text("ya bien: ¿Ágora?") == "ya bien: ¿Ágora?"


def test_own_bookkeeping_never_reaches_own_inbox(ag):
    tid = ag.task_add({"agent": "claude", "title": "t", "claim": True})["task"]["id"]
    ag.task_update({"agent": "claude", "task_id": tid, "status": "in_progress"})
    assert ag.inbox({"agent": "claude"})["counts"]["messages"] == 0
    other = ag.inbox({"agent": "codex"})["messages"]
    assert [m["kind"] for m in other] == ["proposal", "system", "system"] and all(m["author"] == "claude" for m in other)


def test_done_over_requested_changes_needs_force_and_reason(ag):
    tid = ag.task_add({"agent": "claude", "title": "t", "claim": True})["task"]["id"]
    ag.task_submit({"agent": "claude", "task_id": tid, "summary": "s"})
    ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                    "verdict": "changes", "body": "fix x"})
    with pytest.raises(AgoraError):
        ag.task_done({"agent": "claude", "task_id": tid, "force": True})            # force alone is not enough
    with pytest.raises(AgoraError):
        ag.task_done({"agent": "claude", "task_id": tid, "force": True, "reason": "   "})
    done = ag.task_done({"agent": "claude", "task_id": tid, "force": True, "reason": "x was a false alarm"})["task"]
    assert done["status"] == "done" and not done["reviewed"]
    assert "x was a false alarm" in ag.task({"task_id": tid})["messages"][-1]["body"]


# ---- second round: grouped locks, pending mentions, review exemption, digest, migration ------------------------

def test_group_locks_folds_long_lists():
    from hoard_link.hub.agora import group_locks
    locks = [{"resource": f"path:faustus/src/f{i}.py", "owner": "codex", "task_id": 1, "expires": 10} for i in range(13)]
    locks += [{"resource": f"path:faustus/tests/t{i}.py", "owner": "codex", "task_id": 1, "expires": 20} for i in range(15)]
    locks += [{"resource": "path:faustus/vite.config.ts", "owner": "codex", "task_id": 1, "expires": 5},
              {"resource": "model:principal", "owner": "codex", "task_id": None, "expires": 5},
              {"resource": "path:hoardlink/scripts/agora.py", "owner": "claude", "task_id": 4, "expires": 5}]
    groups = group_locks(locks)
    big = [g for g in groups if g["owner"] == "codex" and g["kind"] == "path"][0]
    assert big["count"] == 29 and big["folders"] == {"src/": 13, "tests/": 15, "vite.config.ts": 1}
    assert big["label"].startswith("path:faustus/ · 29 rutas (tests/ 15, src/ 13")
    assert big["expires"] == 20 and len(big["resources"]) == 29
    single = [g for g in groups if g["owner"] == "claude"][0]
    assert single["label"] == "path:hoardlink/scripts/agora.py"
    assert any(g["label"] == "model:principal" for g in groups)


def test_mentions_stay_pending_until_seen(ag):
    th = ag.thread_open({"agent": "claude", "title": "q", "body": "codex?", "mentions": ["codex"]})["thread"]
    ag.inbox({"agent": "codex"})                                   # e.g. an MCP read moves the cursor
    again = ag.inbox({"agent": "codex"})
    assert again["counts"]["messages"] == 0 and again["counts"]["pending"] == 1
    assert again["pending"][0]["thread_id"] == th["id"]
    ag.heartbeat({"agent": "codex"})
    assert ag.inbox({"agent": "codex", "peek": True})["counts"]["pending"] == 1        # renewing locks loses nothing
    ag.thread({"thread_id": th["id"], "agent": "codex"})                               # opening the thread = seen
    assert ag.inbox({"agent": "codex"})["counts"]["pending"] == 0
    ag.post({"agent": "claude", "thread_id": th["id"], "body": "ping", "mentions": ["codex"]})
    assert ag.inbox({"agent": "codex", "peek": True})["counts"]["pending"] == 1
    ag.post({"agent": "codex", "thread_id": th["id"], "body": "pong"})                 # replying = seen
    assert ag.inbox({"agent": "codex", "peek": True})["counts"]["pending"] == 0
    ag.post({"agent": "claude", "thread_id": th["id"], "body": "again", "mentions": ["codex"]})
    assert ag.ack({"agent": "codex", "all": True})["acked"] == [th["id"]]
    assert ag.inbox({"agent": "codex", "peek": True})["counts"]["pending"] == 0
    with pytest.raises(AgoraError):
        ag.ack({"agent": "luis", "all": True})                     # only the page acks for the person


def test_long_poll_does_not_return_early_for_standing_reviews(tmp_path):
    a = Agora(tmp_path / "a.db")
    try:
        tid = a.task_add({"agent": "codex", "title": "t", "claim": True})["task"]["id"]
        a.task_submit({"agent": "codex", "task_id": tid, "summary": "s"})
        a.inbox({"agent": "claude"})
        start = time.time()
        out = a.inbox({"agent": "claude", "wait_s": 1})
        assert out["counts"]["reviews"] == 1 and time.time() - start >= 0.9
    finally:
        a.close()


def test_review_exemption_only_for_non_code_kinds(ag):
    docs = ag.task_add({"agent": "codex", "title": "docs", "kind": "docs", "claim": True})["task"]["id"]
    d = ag.task_done({"agent": "codex", "task_id": docs, "result": "FAUSTUS.md"})["task"]
    assert d["review_state"] == "exempt" and not d["reviewed"]
    assert "Exenta de revisión: docs" in ag.task({"task_id": docs})["messages"][-1]["body"]
    code = ag.task_add({"agent": "codex", "title": "code", "kind": "feature", "claim": True})["task"]["id"]
    with pytest.raises(AgoraError):
        ag.task_done({"agent": "codex", "task_id": code, "exempt": "small"})
    c = ag.task_done({"agent": "codex", "task_id": code, "result": "x"})["task"]
    assert c["review_state"] == "unreviewed"
    ev = ag.task_add({"agent": "codex", "title": "probe", "kind": "eval", "claim": True})["task"]["id"]
    ag.task_submit({"agent": "codex", "task_id": ev, "summary": "s"})
    ag.task_review({"agent": "claude", "task_id": ev, "expected_submission_revision": 1,
                    "verdict": "approve", "body": "ok"})
    assert ag.task_done({"agent": "codex", "task_id": ev})["task"]["review_state"] == "approved"


def test_digest_counts_by_agent_and_review_state(ag):
    a1 = ag.task_add({"agent": "codex", "title": "a", "kind": "docs", "claim": True})["task"]["id"]
    ag.task_done({"agent": "codex", "task_id": a1})
    a2 = ag.task_add({"agent": "claude", "title": "b", "claim": True})["task"]["id"]
    ag.task_submit({"agent": "claude", "task_id": a2, "summary": "s"})
    ag.task_review({"agent": "codex", "task_id": a2, "expected_submission_revision": 1,
                    "verdict": "approve", "body": "ok"})
    ag.task_done({"agent": "claude", "task_id": a2})
    ag.thread_open({"agent": "claude", "title": "d", "body": "b", "kind": "decision"})
    d = ag.digest({"hours": 1})
    assert d["agents"]["codex"]["done"] == 1 and d["agents"]["claude"]["submitted"] == 1
    assert d["agents"]["codex"]["reviews"] == 1
    assert d["done_by_review"] == {"approved": 1, "equivalent": 0, "exempt": 1, "unreviewed": 0}
    ag.fake_clock.t += 2 * 3600
    assert ag.digest({"hours": 1})["done"] == []


def test_schema_1_database_migrates(tmp_path):
    import sqlite3
    from hoard_link.hub.agora import MIGRATIONS
    from hoard_link.sqlkit import Database
    old = Database(tmp_path / "old.db", migrations=MIGRATIONS[:1])
    old.execute("INSERT INTO tasks(title, status, reviewed, created, updated) VALUES('x','done',1,1,1)")
    old.execute("INSERT INTO tasks(title, status, reviewed, created, updated) VALUES('y','done',0,1,1)")
    old.close()
    a = Agora(tmp_path / "old.db")
    try:
        states = [t["review_state"] for t in a.tasks({"status": "done"})["tasks"]]
        assert sorted(states) == ["approved", "unreviewed"]
    finally:
        a.close()


def test_resubmitting_new_commits_clears_the_previous_approval(ag):
    tid = ag.task_add({"agent": "claude", "title": "fix", "repo": "HoardLink", "paths": ["a.py"], "claim": True})["task"]["id"]
    ag.task_submit({"agent": "claude", "task_id": tid, "summary": "first", "commits": ["aaa111"]})
    ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                    "verdict": "approve", "body": "ok"})
    assert ag._task_row(tid)["reviewed"] is True
    ag.task_submit({"agent": "claude", "task_id": tid, "summary": "one more fix", "commits": ["aaa111", "bbb222"],
                    "reviewer": "codex"})
    task = ag._task_row(tid)
    assert task["status"] == "review" and task["reviewed"] is False
    assert [t["id"] for t in ag.inbox({"agent": "codex", "peek": True})["reviews"]] == [tid]
    ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 2,
                    "verdict": "approve", "body": "bbb222 checked"})
    done = ag.task_done({"agent": "claude", "task_id": tid, "commits": ["aaa111", "bbb222"]})["task"]
    assert done["review_state"] == "approved" and done["reviewed_commits"] == ["aaa111", "bbb222"]


def test_submit_without_blocks_stays_compatible(ag):
    tid = ag.task_add({"agent": "claude", "title": "plain", "claim": True})["task"]["id"]
    task = ag.task_submit({"agent": "claude", "task_id": tid, "summary": "text only", "commits": ["abc"]})["task"]
    assert task["submission_blocks"] is None and task["submission_revision"] == 1


def test_structured_submit_verifies_code_peek(tmp_path):
    repo = tmp_path / "DemoRepo"
    sha = _git_repo(repo, {"src/hello.py": "a = 1\nb = 2\nc = 3\n"})
    clock = Clock()
    events = []
    ag = Agora(tmp_path / "agora.db", clock=clock, emit=lambda t, d: events.append((t, d)),
               repo_lookup=lambda name: repo if name.lower() == "demorepo" else None)
    try:
        tid = ag.task_add({"agent": "claude", "title": "peek", "repo": "DemoRepo", "paths": ["src/hello.py"],
                           "claim": True})["task"]["id"]
        with pytest.raises(AgoraError):
            ag.task_submit({"agent": "claude", "task_id": tid, "summary": "bad lines", "commits": [sha],
                            "blocks": [{"kind": "code_peek", "peek": f"src/hello.py#L1-L99@{sha}"}]})
        submitted = ag.task_submit({
            "agent": "claude", "task_id": tid, "summary": "with peek", "commits": [sha],
            "blocks": [
                {"id": "why", "kind": "what_why", "title": "Why", "body": "Keep the constant exact."},
                {"id": "paths1", "kind": "paths", "paths": ["src/hello.py"]},
                {"id": "peek1", "kind": "code_peek", "title": "Constant",
                 "peek": f"src/hello.py#L2-L2@{sha[:12]}"},
                {"id": "seq1", "kind": "sequence", "steps": ["edit", "verify", "submit"]},
            ],
        })["task"]
        blocks = submitted["submission_blocks"]
        assert len(blocks) == 4
        peek = next(b for b in blocks if b["kind"] == "code_peek")["peek"]
        assert peek["verified"] and peek["text"] == "b = 2" and peek["start"] == 2
        msg = ag.task({"task_id": tid})["messages"][-1]["body"]
        assert "Structured review blocks:" in msg and "b = 2" in msg
        reviewed = ag.task_review({
            "agent": "codex", "task_id": tid, "expected_submission_revision": 1,
            "verdict": "approve", "body": "peek matches",
            "block_comments": [{"block_id": "peek1", "body": "line 2 is correct"}],
        })["task"]
        assert reviewed["status"] == "approved"
        note = ag.task({"task_id": tid})["messages"][-1]["body"]
        assert "Per-block notes:" in note and "peek1" in note
    finally:
        ag.close()


def test_verify_code_peek_helper(tmp_path):
    repo = tmp_path / "R"
    sha = _git_repo(repo, {"f.txt": "one\ntwo\nthree\n"})
    out = verify_code_peek(f"f.txt#L1-L2@{sha}", roots=[repo], commits=[])
    assert out["text"] == "one\ntwo" and out["verified"]


def test_reject_duplicate_and_empty_block_ids(tmp_path):
    with pytest.raises(AgoraError, match="unique"):
        normalize_submission_blocks(
            [{"id": "same", "kind": "what_why", "body": "a"},
             {"id": "same", "kind": "what_why", "body": "b"}],
            repo_name="X", commits=[], repo_lookup=lambda n: None)
    with pytest.raises(AgoraError, match="empty"):
        normalize_submission_blocks(
            [{"id": "  ", "kind": "what_why", "body": "a"}],
            repo_name="X", commits=[], repo_lookup=lambda n: None)
    with pytest.raises(AgoraError, match="unknown block_id"):
        repo = tmp_path / "R"
        sha = _git_repo(repo, {"a.py": "x = 1\n"})
        ag = Agora(tmp_path / "a.db", repo_lookup=lambda name: repo)
        try:
            tid = ag.task_add({"agent": "claude", "title": "t", "repo": "R", "claim": True})["task"]["id"]
            ag.task_submit({"agent": "claude", "task_id": tid, "summary": "s", "commits": [sha],
                            "blocks": [{"id": "why", "kind": "what_why", "body": "because"}]})
            ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                            "verdict": "changes", "body": "no",
                            "block_comments": [{"block_id": "missing", "body": "x"}]})
        finally:
            ag.close()


def test_submit_conflict_when_revision_changes_during_slow_verify(tmp_path):
    repo = tmp_path / "R"
    sha = _git_repo(repo, {"a.py": "ok\n"})
    ag = Agora(tmp_path / "a.db", repo_lookup=lambda name: repo if name.lower() == "r" else None)
    gate = threading.Event()
    entered = threading.Event()

    def hook():
        entered.set()
        gate.wait(5)

    agora_mod._GIT_SHOW_HOOK = hook
    try:
        tid = ag.task_add({"agent": "claude", "title": "race", "repo": "R", "claim": True})["task"]["id"]
        result = {"err": None, "ok": None}

        def slow_submit():
            try:
                result["ok"] = ag.task_submit({
                    "agent": "claude", "task_id": tid, "summary": "slow", "commits": [sha],
                    "blocks": [{"id": "p", "kind": "code_peek", "peek": f"a.py#L1-L1@{sha}"}],
                })
            except AgoraError as exc:
                result["err"] = exc

        th = threading.Thread(target=slow_submit)
        th.start()
        assert entered.wait(3)
        # Concurrent plain submit bumps submission_revision while peek verify is paused.
        ag.task_submit({"agent": "claude", "task_id": tid, "summary": "fast plain", "commits": [sha]})
        gate.set()
        th.join(5)
        assert result["ok"] is None and result["err"] is not None
        assert getattr(result["err"], "extra", {}).get("error_code") == "submit_conflict"
        assert ag._task_row(tid)["submission_revision"] == 1
        assert ag._task_row(tid)["submission_blocks"] is None
    finally:
        agora_mod._GIT_SHOW_HOOK = None
        ag.close()


def test_isolated_clone_checkout_with_matching_origin(tmp_path):
    primary = tmp_path / "primary"
    sha = _git_repo(primary, {"src/x.py": "valor = 'café'\n"})
    # Bare remote so a second clone shares origin URL identity without being a worktree.
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(primary), str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=primary, check=True, capture_output=True)
    isolated = tmp_path / "isolated"
    subprocess.run(["git", "clone", str(bare), str(isolated)], check=True, capture_output=True)
    # Commit only exists as object in both (same history); ensure accented file verifies with UTF-8.
    ag = Agora(tmp_path / "a.db", repo_lookup=lambda name: primary if name.lower() == "demo" else None)
    try:
        tid = ag.task_add({"agent": "claude", "title": "iso", "repo": "Demo", "claim": True})["task"]["id"]
        # Without checkout, primary can read the object (shared history via clone from bare).
        # Force isolated path: make primary miss the tip by using only isolated for a new commit.
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=isolated, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=isolated, check=True, capture_output=True)
        (isolated / "src" / "y.py").write_text("# sólo en el clon\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/y.py"], cwd=isolated, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "isolated only"], cwd=isolated, check=True, capture_output=True)
        iso_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=isolated, check=True,
                                 capture_output=True, text=True).stdout.strip()
        with pytest.raises(AgoraError, match="cannot read|unknown"):
            ag.task_submit({"agent": "claude", "task_id": tid, "summary": "no checkout", "commits": [iso_sha],
                            "blocks": [{"id": "p", "kind": "code_peek", "peek": f"src/y.py#L1-L1@{iso_sha}"}]})
        submitted = ag.task_submit({
            "agent": "claude", "task_id": tid, "summary": "with checkout", "commits": [iso_sha],
            "blocks": [{"id": "p", "kind": "code_peek", "peek": f"src/y.py#L1-L1@{iso_sha}",
                        "checkout": str(isolated)}],
        })["task"]
        peek = submitted["submission_blocks"][0]["peek"]
        assert peek["verified"] and "clon" in peek["text"] and peek["checkout"] == str(isolated.resolve())
        # Accents from primary history still round-trip as UTF-8.
        utf = verify_code_peek(f"src/x.py#L1-L1@{sha}", roots=[primary], commits=[])
        assert "café" in utf["text"]
    finally:
        ag.close()


def test_path_normalize_rejects_lstrip_trap():
    # str.lstrip("./") would turn ".../secret.py" into "secret.py"; keep the prefix.
    assert _normalize_repo_relpath(".../secret.py") == ".../secret.py"
    assert _normalize_repo_relpath("./scripts/agora.py") == "scripts/agora.py"
    with pytest.raises(AgoraError, match="invalid"):
        _normalize_repo_relpath("../secret.py")
    with pytest.raises(AgoraError, match="invalid"):
        _normalize_repo_relpath("foo/../../secret.py")
