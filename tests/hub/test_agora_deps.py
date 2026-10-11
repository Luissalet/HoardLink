"""Agora task dependencies (blocked_by): a chain A → B → C wakes each level once when its blockers are approved, done
or dropped; loops, self-waits and unknown ids are refused; claiming early only warns; a blocker nobody claims while
someone waits on it is an orphan; and the backup sweep never repeats a wake-up."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hoard_link.hub.agora import Agora, AgoraError

MIN = 60.0


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def ag(tmp_path):
    clock = Clock()
    a = Agora(tmp_path / "agora.db", clock=clock, handover_thread_id=1)
    a.fake_clock = clock
    for who in ("claude", "codex-ayuda", "faustus"):
        a.heartbeat({"agent": who})
    a.thread_open({"agent": "claude", "title": "Chat de trabajo", "body": "coordinación", "kind": "note"})
    yield a
    a.close()


def _add(ag, title, owner=None, blocked_by=None):
    t = ag.task_add({"agent": "claude", "title": title, "repo": "HoardLink", "paths": [title + ".py"],
                     "blocked_by": blocked_by or []})["task"]
    if owner:
        ag.task_claim({"agent": owner, "task_id": t["id"]})
    return t["id"]


def _approve(ag, tid, owner="claude", reviewer="faustus"):
    ag.task_submit({"agent": owner, "task_id": tid, "summary": "hecho", "commits": [f"c{tid}"], "reviewer": reviewer})
    return ag.task_review({"agent": reviewer, "task_id": tid, "verdict": "approve", "body": "ok",
                           "expected_submission_revision": ag.task({"task_id": tid})["task"]["submission_revision"]})


def _wakes(ag, tid):
    th = ag.task({"task_id": tid})["task"]["thread_id"]
    return [m for m in ag.thread_recent({"thread_id": th, "limit": 50})["messages"] if (m.get("body") or "").startswith("Desbloqueada")]


def test_chain_wakes_each_level_once(ag):
    a = _add(ag, "a", owner="claude")
    b = _add(ag, "b", owner="codex-ayuda", blocked_by=[a])
    c = _add(ag, "c", blocked_by=[b])
    tb = ag.task({"task_id": b})["task"]
    assert [x["id"] for x in tb["blocked_by"]] == [a] and tb["waiting_on"] == [a]
    assert ag.task({"task_id": a})["task"]["dependents"] == [b]
    ag.task_update({"agent": "codex-ayuda", "task_id": b, "status": "blocked"})

    r = _approve(ag, a)
    assert r["unblocked"] == [b]
    tb = ag.task({"task_id": b})["task"]
    assert tb["waiting_on"] == [] and tb["status"] == "claimed"           # blocked goes back to work
    wakes = _wakes(ag, b)
    assert len(wakes) == 1 and "codex-ayuda" in wakes[0]["mentions"] and f"#{a}" in wakes[0]["body"]
    assert ag.task({"task_id": c})["task"]["waiting_on"] == [b]          # the next level still waits

    assert ag.task_done({"agent": "claude", "task_id": a, "commits": [f"c{a}"]})["unblocked"] == []   # approved → done: once
    assert len(_wakes(ag, b)) == 1

    assert _approve(ag, b, owner="codex-ayuda", reviewer="claude")["unblocked"] == [c]
    wc = _wakes(ag, c)
    assert len(wc) == 1 and "queda libre para reclamar" in wc[0]["body"] and "claude" in wc[0]["mentions"]  # creator


def test_a_second_cycle_of_the_same_blocker_wakes_again(ag):
    a = _add(ag, "a", owner="claude")
    b = _add(ag, "b", owner="codex-ayuda", blocked_by=[a])
    ag.task_update({"agent": "codex-ayuda", "task_id": b, "status": "blocked"})
    assert _approve(ag, a)["unblocked"] == [b]
    ag.task_update({"agent": "claude", "task_id": a, "status": "claimed"})          # a goes back to work
    ag.task_update({"agent": "codex-ayuda", "task_id": b, "status": "blocked"})
    assert ag.task({"task_id": b})["task"]["waiting_on"] == [a]
    assert _approve(ag, a)["unblocked"] == [b]                                     # second cycle: woken again
    tb = ag.task({"task_id": b})["task"]
    assert tb["status"] == "claimed" and tb["waiting_on"] == [] and len(_wakes(ag, b)) == 2
    assert ag.task_done({"agent": "claude", "task_id": a, "commits": [f"c{a}"]})["unblocked"] == []   # same cycle
    assert len(_wakes(ag, b)) == 2


def test_a_missed_rearm_is_caught_by_the_sweep(ag):
    a = _add(ag, "a", owner="claude")
    b = _add(ag, "b", owner="codex-ayuda", blocked_by=[a])
    _approve(ag, a)
    ag.db.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (a,))        # changed behind the hooks' back
    assert ag.orphan_sweep({"force": True})["unblocked"] == []                    # waiting again: re-armed
    ag.db.execute("UPDATE tasks SET status='approved' WHERE id=?", (a,))
    assert ag.orphan_sweep({"force": True})["unblocked"] == [b] and len(_wakes(ag, b)) == 2


def test_removing_the_last_pending_blocker_unblocks(ag):
    a = _add(ag, "a", owner="claude")
    b = _add(ag, "b", owner="codex-ayuda", blocked_by=[a])
    ag.task_update({"agent": "codex-ayuda", "task_id": b, "status": "blocked"})
    ag.task_deps({"agent": "faustus", "task_id": b, "set": []})
    assert ag.task({"task_id": b})["task"]["status"] == "claimed"
    c = _add(ag, "c", owner="codex-ayuda")
    ag.task_update({"agent": "codex-ayuda", "task_id": c, "status": "blocked"})
    ag.task_deps({"agent": "faustus", "task_id": c, "add": [a]})                  # now it waits on a pending blocker
    ag.task_deps({"agent": "faustus", "task_id": c, "set": []})
    assert ag.task({"task_id": c})["task"]["status"] == "claimed"                 # it was waiting on a: cleared
    d = _add(ag, "d", owner="codex-ayuda")
    _approve(ag, a)
    ag.task_deps({"agent": "faustus", "task_id": d, "add": [a]})                  # a already through
    ag.task_update({"agent": "codex-ayuda", "task_id": d, "status": "blocked"})   # blocked by something else
    ag.task_deps({"agent": "faustus", "task_id": d, "set": []})
    assert ag.task({"task_id": d})["task"]["status"] == "blocked"                 # a never held it back: untouched


def test_loops_self_and_unknown_are_refused(ag):
    a = _add(ag, "a")
    b = _add(ag, "b", blocked_by=[a])
    c = _add(ag, "c", blocked_by=[b])
    with pytest.raises(AgoraError, match="loop") as e:
        ag.task_deps({"agent": "claude", "task_id": a, "add": [c]})
    assert "#" + str(a) in str(e.value) and "#" + str(c) in str(e.value)
    with pytest.raises(AgoraError, match="itself"):
        ag.task_deps({"agent": "claude", "task_id": a, "add": [a]})
    with pytest.raises(AgoraError):
        ag.task_deps({"agent": "claude", "task_id": a, "add": [9999]})
    with pytest.raises(AgoraError, match="loop"):
        ag.task_deps({"agent": "claude", "task_id": a, "set": [b]})       # direct two-step loop too
    assert ag.task({"task_id": a})["task"]["blocked_by"] == []          # nothing half-written


def test_deps_edit_add_remove_set_and_text_ids(ag):
    a, b, c = _add(ag, "a"), _add(ag, "b"), _add(ag, "c")
    r = ag.task_deps({"agent": "faustus", "task_id": c, "add": f"#{a}, {b}"})   # anyone, ids as text
    assert r["added"] == [a, b] and r["task"]["waiting_on"] == [a, b]
    assert ag.task_deps({"agent": "faustus", "task_id": c, "remove": [a]})["removed"] == [a]
    assert ag.task_deps({"agent": "faustus", "task_id": c, "set": []})["removed"] == [b]
    assert ag.task({"task_id": c})["task"]["blocked_by"] == []
    with pytest.raises(AgoraError, match="add, remove or set"):
        ag.task_deps({"agent": "faustus", "task_id": c})


def test_claim_early_warns_and_dropped_blocker_clears(ag):
    a = _add(ag, "a", owner="claude")
    b = _add(ag, "b", blocked_by=[a])
    r = ag.task_claim({"agent": "codex-ayuda", "task_id": b})
    assert r["ok"] and r["dep_warnings"] and f"#{a}" in r["dep_warnings"][0]       # informs, never refuses
    rel = ag.task_release({"agent": "claude", "task_id": a, "drop": True, "reason": "no hace falta"})
    assert rel["unblocked"] == [b] and ag.task({"task_id": b})["task"]["waiting_on"] == []
    with pytest.raises(AgoraError, match="already dropped"):                        # a dropped task stays dropped
        ag.task_release({"agent": "claude", "task_id": a})
    assert ag.task({"task_id": a})["task"]["status"] == "dropped"


def test_already_clear_blockers_never_wake_later(ag):
    a = _add(ag, "a", owner="claude")
    _approve(ag, a)
    b = _add(ag, "b", owner="codex-ayuda", blocked_by=[a])
    assert ag.orphan_sweep({"force": True})["unblocked"] == []
    assert _wakes(ag, b) == []


def test_backup_sweep_and_turn_close_wake_once(ag):
    a = _add(ag, "a", owner="claude")
    b = _add(ag, "b", owner="codex-ayuda", blocked_by=[a])
    ag.db.execute("UPDATE tasks SET status='approved' WHERE id=?", (a,))   # a transition the hooks never saw
    ag.heartbeat({"agent": "claude", "state": "idle"})                    # closing a turn wakes right away
    assert len(_wakes(ag, b)) == 1
    assert ag.orphan_sweep({"force": True})["unblocked"] == []
    ag.heartbeat({"agent": "faustus", "state": "idle"})
    assert len(_wakes(ag, b)) == 1


def test_blocker_nobody_claims_is_an_orphan(ag):
    a = _add(ag, "a")
    b = _add(ag, "b", owner="codex-ayuda", blocked_by=[a])
    f = next(x for x in ag.orphans()["findings"] if x["kind"] == "blocker_unowned")
    assert f["task_id"] == a and f["dependents"] == [b] and f"#{b}" in f["why"]
    assert f["suggest"] and f["command"] == f"agora --as {f['suggest']} claim {a}"
    r = ag.orphan_sweep({"force": True})
    assert any(x["kind"] == "blocker_unowned" for x in r["new"])
    summary = [m for m in ag.thread_recent({"thread_id": 1, "limit": 20})["messages"] if "huérfano" in (m.get("body") or "")]
    assert summary and "None" not in summary[-1]["body"] and "nadie la ha reclamado" in summary[-1]["body"]
    ag.task_claim({"agent": "faustus", "task_id": a})
    assert all(x["kind"] != "blocker_unowned" for x in ag.orphans()["findings"])


def test_board_tools_and_write_op():
    from hoard_link.hub import agora as mod
    names = {t["name"] for t in mod.TOOLS}
    assert "hub_agora_task_deps" in names and "task_deps" in mod._WRITE_OPS
    add = next(t for t in mod.TOOLS if t["name"] == "hub_agora_task_add")
    assert "blocked_by" in add["inputSchema"]["properties"]
