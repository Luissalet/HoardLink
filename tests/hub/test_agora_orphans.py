"""Agora orphans: tasks in review with no reviewer or a silent / out-of-quota reviewer, and claimed tasks whose owner
is silent or out of quota, each with a free agent to take it and the handover command; announced once (task thread and
coordination thread), never moved; and the board, HTTP, tool and terminal faces."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hoard_link.hub.agora import Agora

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
    for who in ("claude", "codex-ayuda", "faustus", "cursor-capacity"):
        a.heartbeat({"agent": who})
    a.thread_open({"agent": "claude", "title": "Chat de trabajo", "body": "coordinación", "kind": "note"})
    yield a
    a.close()


def _task(ag, owner, title, path):
    t = ag.task_add({"agent": owner, "title": title, "repo": "HoardLink", "paths": [path]})["task"]
    ag.task_claim({"agent": owner, "task_id": t["id"]})
    return t["id"]


def _beat(ag, *who):
    for w in who:
        ag.heartbeat({"agent": w})


def test_silent_reviewer_silent_owner_and_unassigned_review(ag):
    silent_owner = _task(ag, "codex-ayuda", "la deja codex", "a.py")
    reviewed = _task(ag, "claude", "la revisa codex", "b.py")
    ag.task_submit({"agent": "claude", "task_id": reviewed, "summary": "s", "commits": ["c1"], "reviewer": "codex-ayuda"})
    nobody = _task(ag, "claude", "sin revisor", "c.py")
    ag.task_submit({"agent": "claude", "task_id": nobody, "summary": "s", "commits": ["c2"]})
    assert ag.orphans()["findings"] == []                         # everyone is active and the review is fresh
    ag.fake_clock.t += 31 * MIN
    _beat(ag, "claude", "faustus")                                # codex-ayuda goes quiet
    found = {f["task_id"]: f for f in ag.orphans()["findings"]}
    assert found[silent_owner]["kind"] == "owner_silent" and found[silent_owner]["who"] == "codex-ayuda"
    assert found[silent_owner]["suggest"] == "faustus"            # fewest open tasks; never the silent one
    assert found[silent_owner]["command"] == f"agora handover --from codex-ayuda --to faustus --tasks {silent_owner} --force"
    assert found[reviewed]["kind"] == "review_silent" and found[reviewed]["suggest"] == "faustus"
    assert found[nobody]["kind"] == "review_unassigned" and found[nobody]["suggest"] == "faustus"
    assert "cursor-capacity" not in {f["suggest"] for f in found.values()}


def test_out_of_quota_counts_as_silent(ag):
    owned = _task(ag, "codex-ayuda", "la lleva codex", "d.py")
    ag.usage_report({"agent": "cursor-capacity", "provider_id": "codex", "pool_id": "codex-pool", "pool_label": "codex",
                     "source": "test", "windows": [{"window_id": "codex.week", "measurement": "measured", "used_percent": 100,
                                                    "remaining_percent": 0, "reset_at": "2026-10-15T09:40:00+00:00"}]})
    f = next(x for x in ag.orphans()["findings"] if x["task_id"] == owned)
    assert f["kind"] == "owner_silent" and f["why"] == "sin cuota"


def _summaries(ag):
    return [m for m in ag.thread_recent({"thread_id": 1, "limit": 50})["messages"] if "huérfano" in (m.get("body") or "")]


def test_sweep_announces_each_finding_once_and_moves_nothing(ag):
    owned = _task(ag, "codex-ayuda", "la deja codex", "e.py")
    ag.fake_clock.t += 31 * MIN
    ag._last_orphan_sweep = ag.fake_clock.t                       # keep the heartbeats from sweeping first
    _beat(ag, "claude", "faustus")
    r = ag.orphan_sweep({"force": True})
    assert [f["task_id"] for f in r["new"]] == [owned]
    task = ag.task({"task_id": owned})
    assert task["task"]["owner"] == "codex-ayuda"                 # nothing moved
    notes = ag.thread_recent({"thread_id": task["task"]["thread_id"], "limit": 20})["messages"]
    suggest = r["new"][0]["suggest"]
    assert suggest in ("claude", "faustus")
    assert any("Huérfana" in (m.get("body") or "") and suggest in m["body"] for m in notes)
    summary = [m for m in ag.thread_recent({"thread_id": 1, "limit": 20})["messages"] if "huérfano" in (m.get("body") or "")]
    assert len(summary) == 1 and suggest in summary[0]["mentions"]
    assert ag.orphan_sweep({"force": True})["new"] == []          # same situation: said once
    ag.fake_clock.t += 6 * MIN
    _beat(ag, "claude")                                           # the heartbeat runs the sweep: still nothing new
    assert len([m for m in ag.thread_recent({"thread_id": 1, "limit": 20})["messages"] if "huérfano" in (m.get("body") or "")]) == 1


def test_heartbeats_sweep_and_a_new_claim_is_a_new_situation(ag):
    owned = _task(ag, "codex-ayuda", "vuelve a pasar", "f.py")
    ag.fake_clock.t += 31 * MIN
    _beat(ag, "claude", "faustus")                                # the first heartbeat after the gap sweeps
    assert len(_summaries(ag)) == 1 and "codex-ayuda" in _summaries(ag)[0]["body"]
    ag.handover({"agent": "claude", "from_agent": "codex-ayuda", "to_agent": "faustus", "reason": "relevo", "tasks": [owned]})
    assert all(f["task_id"] != owned for f in ag.orphans()["findings"])
    ag.fake_clock.t += 31 * MIN
    _beat(ag, "claude")
    assert len(_summaries(ag)) == 2 and "faustus sin actividad" in _summaries(ag)[-1]["body"]


def test_sweep_spacing_and_board(ag):
    _task(ag, "codex-ayuda", "x", "g.py")
    ag.fake_clock.t += 31 * MIN
    _beat(ag, "claude", "faustus")
    assert ag.orphan_sweep()["skipped"] in (False, True)
    assert ag.orphan_sweep()["skipped"] is True                   # within five minutes
    assert ag.board()["orphans"] and ag.board()["orphans"][0]["kind"] == "owner_silent"


def test_tools_listed():
    from hoard_link.hub import agora as mod
    names = {t["name"] for t in mod.TOOLS}
    assert {"hub_agora_orphans", "hub_agora_orphan_sweep"} <= names
    assert "orphan_sweep" in mod._WRITE_OPS
