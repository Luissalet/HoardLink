"""Agora quota state: each agent's quota from its provider's latest report (ok, low, exhausted, unknown, stale, none),
the warnings when someone mentions, hands work to or asks a review of an agent out of quota, the badge in agents and
inbox, and the coordination-thread notice when an agent runs out with work in its name. Warnings only: nothing blocks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hoard_link.hub.agora import Agora


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
    for who in ("cursor-agora", "codex-ayuda", "claude", "cursor-capacity"):
        a.heartbeat({"agent": who, "doing": "working"})
    a.thread_open({"agent": "claude", "title": "Chat de trabajo", "body": "aquí nos coordinamos", "kind": "note"})
    yield a
    a.close()


def _iso(ag, hours):
    return datetime.fromtimestamp(ag.fake_clock.t + hours * 3600, timezone.utc).isoformat()


def _report(ag, remaining, reset_hours=24, measurement="measured", provider="cursor", agent="cursor-capacity"):
    window = {"window_id": f"{provider}.models", "label": "models", "measurement": measurement,
              "reset_at": _iso(ag, reset_hours) if reset_hours is not None else None}
    if measurement != "unknown":
        window.update(used_percent=100 - remaining, remaining_percent=remaining)
    return ag.usage_report({"agent": agent, "provider_id": provider, "pool_id": f"{provider}-pool",
                            "pool_label": f"{provider} account", "source": "test", "windows": [window]})


def test_provider_of_an_agent_id():
    assert Agora._quota_provider("cursor-agora") == "cursor" and Agora._quota_provider("codex-ayuda") == "codex"
    assert Agora._quota_provider("claude") == "claude" and Agora._quota_provider("luis") is None
    assert Agora._quota_provider("cursorish") is None


def test_states(ag):
    assert ag.agent_quota_state({"agent": "cursor-agora"})["state"] == "none"
    _report(ag, 60)
    assert ag.agent_quota_state({"agent": "cursor-agora"})["state"] == "ok"
    _report(ag, 10)
    low = ag.agent_quota_state({"agent": "cursor-agora"})
    assert low["state"] == "low" and low["remaining_percent"] == 10 and "poca cuota" in low["note"]
    _report(ag, 0, reset_hours=88)
    out = ag.agent_quota_state({"agent": "cursor-agora"})
    assert out["state"] == "exhausted" and out["reset_at"] == _iso(ag, 88)
    assert out["note"].startswith("cursor-agora sin cuota hasta ")
    assert ag.agent_quota_state({"agent": "cursor"})["state"] == "exhausted"       # every id of the provider
    assert ag.agent_quota_state({"agent": "codex-ayuda"})["state"] == "none"
    _report(ag, 0, measurement="unknown")
    assert ag.agent_quota_state({"agent": "cursor-agora"})["state"] == "unknown"


def test_a_passed_reset_or_an_old_report_never_reads_as_exhausted(ag):
    _report(ag, 0, reset_hours=1)
    ag.fake_clock.t += 2 * 3600
    assert ag.agent_quota_state({"agent": "cursor-agora"})["state"] == "unknown"   # reset passed: window expired
    _report(ag, 0, reset_hours=48)
    ag.fake_clock.t += 25 * 3600
    assert ag.agent_quota_state({"agent": "cursor-agora"})["state"] == "stale"


def test_warnings_on_mention_handover_and_reviewer(ag):
    _report(ag, 0, reset_hours=88)
    r = ag.post({"agent": "claude", "thread_id": 1, "body": "@cursor-agora mira esto", "mentions": ["cursor-agora", "codex-ayuda"]})
    assert r["ok"] and r["quota_warnings"] == [ag.agent_quota_state({"agent": "cursor-agora"})["note"]]
    r = ag.thread_open({"agent": "claude", "title": "Duda", "body": "¿quién?", "mentions": ["cursor-agora"]})
    assert len(r["quota_warnings"]) == 1
    assert "quota_warnings" not in ag.post({"agent": "claude", "thread_id": 1, "body": "hola", "mentions": ["codex-ayuda"]})
    t = ag.task_add({"agent": "claude", "title": "t", "repo": "HoardLink", "paths": ["a.py"]})["task"]
    ag.task_claim({"agent": "claude", "task_id": t["id"]})
    r = ag.task_submit({"agent": "claude", "task_id": t["id"], "summary": "s", "commits": ["c1"], "reviewer": "cursor-agora"})
    assert r["ok"] and r["task"]["status"] == "review" and r["quota_warnings"]                    # warned, not refused
    r = ag.handover({"agent": "claude", "from_agent": "claude", "to_agent": "cursor-agora", "reason": "prueba",
                     "tasks": [t["id"]]})
    assert r["ok"] and r["quota_warnings"]


def test_agents_and_inbox_show_the_quota(ag):
    _report(ag, 0, reset_hours=88)
    agents = {a["id"]: a for a in ag.agents()["agents"]}
    assert agents["cursor-agora"]["quota"]["state"] == "exhausted" and agents["claude"]["quota"]["state"] == "none"
    inbox = ag.inbox({"agent": "claude", "peek": True})
    assert {q["agent"] for q in inbox["out_of_quota"]} == {"cursor-agora", "cursor-capacity"}


def test_running_out_with_work_posts_the_list_once(ag):
    t = ag.task_add({"agent": "cursor-agora", "title": "tarea de cursor", "repo": "HoardLink", "paths": ["b.py"]})["task"]
    ag.task_claim({"agent": "cursor-agora", "task_id": t["id"]})
    mine = ag.task_add({"agent": "claude", "title": "la revisa cursor", "repo": "HoardLink", "paths": ["c.py"]})["task"]
    ag.task_claim({"agent": "claude", "task_id": mine["id"]})
    ag.task_submit({"agent": "claude", "task_id": mine["id"], "summary": "s", "commits": ["c2"], "reviewer": "cursor-agora"})
    _report(ag, 40)

    def notices():
        return [m for m in ag.thread_recent({"thread_id": 1, "limit": 50})["messages"]
                if m["author"] == "system" and "sin cuota" in (m.get("body") or "")]
    assert notices() == []
    _report(ag, 0, reset_hours=88)
    found = notices()
    assert len(found) == 1
    body = found[0]["body"]
    assert f"#{t['id']}" in body and f"revisión de #{mine['id']}" in body and "handover" in body
    _report(ag, 0, reset_hours=88)                                                # still out: not repeated
    assert len(notices()) == 1
    assert ag.task({"task_id": t["id"]})["task"]["owner"] == "cursor-agora"       # nothing moved by itself


def test_the_tool_is_listed():
    from hoard_link.hub import agora as mod
    tool = next(t for t in mod.TOOLS if t["name"] == "hub_agora_agent_quota")
    assert tool.get("read") is True and tool["inputSchema"]["required"] == ["agent"]
