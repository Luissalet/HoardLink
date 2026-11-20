"""What an Ágora message says its author did: tools used, routine behind it, and how far a task's owner is in its turn."""

from __future__ import annotations

import pytest

from hoard_link.hub import agora_work
from hoard_link.hub.agora import Agora, AgoraError

from ._hub_fakes import make_hub
from ._transcripts import (NOW, CLAUDE_ID, claude_file, cl_asst, cl_result, cl_text, cl_use, cl_user, clock,  # noqa: F401
                           one, roots, watch)

S = {"space_id": "atlas"}


class Ticks:
    def __init__(self, t=NOW + 1000):
        self.t = t

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def ag(tmp_path):
    a = Agora(tmp_path / "agora.db", clock=Ticks())
    a.space_create({"agent": "claude", "id": "atlas", "title": "Atlas"})
    yield a
    a.close()


def _say(a, agent="claude", body="Listo.", **extra):
    return a.channel_post({**S, "agent": agent, "body": body, **extra})


def _message(a, mid):
    page = a.channel_read({**S, "channel": "general", "peek": True, "limit": 50})
    return next(m for m in page["messages"] if m["id"] == mid)


TOOLS = [{"name": "Bash", "ok": True, "ms": 1200, "summary": "pytest tests -q"},
         {"name": "Edit", "ok": True, "ms": 40, "summary": "src/a.py"},
         {"name": "Bash", "ok": False, "ms": 900, "summary": "npm run check"}]


def test_declared_work_is_stored_and_travels_with_the_message(ag):
    out = _say(ag, work={"tools": TOOLS, "started": NOW, "ended": NOW + 90})
    assert out["work"]["count"] == 3 and out["work"]["failed"] == 1 and out["work"]["source"] == "declared"
    msg = _message(ag, out["message_id"])
    assert [t["name"] for t in msg["work"]["tools"]] == ["Bash", "Edit", "Bash"]
    assert msg["work"]["duration_s"] == 90.0 and msg["work"]["ok"] == 2 and msg["work"]["more"] == 0
    thread = ag.thread({**S, "thread_id": out["thread"]["id"]})
    assert next(m for m in thread["messages"] if m["id"] == out["message_id"])["work"]["count"] == 3
    feed = ag.feed({**S, "kind": "activity"})
    item = next(i for i in feed["items"] if i["id"] == out["message_id"])
    assert item["work"]["count"] == 3 and item["work"]["tools"] == []
    full = ag.work({**S, "message_id": out["message_id"]})
    assert full["has_work"] and full["message"]["work"]["tools"][1]["summary"] == "src/a.py"
    assert [m["id"] for m in ag.work({**S, "for_agent": "claude"})["messages"]] == [out["message_id"]]


def test_a_message_without_work_carries_none(ag):
    out = _say(ag)
    assert "work" not in out and "work" not in _message(ag, out["message_id"])
    assert ag.work({**S, "message_id": out["message_id"]})["has_work"] is False


def test_summaries_are_masked_and_the_list_is_checked(ag):
    out = _say(ag, work={"tools": [{"name": "Bash", "summary": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789' x"}]})
    summary = ag.work({**S, "message_id": out["message_id"]})["message"]["work"]["tools"][0]["summary"]
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in summary
    for bad in ({"tools": "x"}, {"tools": [{"ok": True}]}, {"tools": [{"name": "Bash", "ok": "yes"}]},
                {"tools": [{"name": "Bash", "ms": -3}]}, {"tools": [{"name": "Bash"}], "started": 10, "ended": 5}, 5):
        with pytest.raises(AgoraError):
            _say(ag, work=bad)
    with pytest.raises(AgoraError):
        _say(ag, work={"tools": [{"name": "t"}] * (agora_work.MAX_TOOLS + 1)})
    assert _say(ag, work={"tools": ["Read", "Grep"]})["work"]["count"] == 2     # bare names are fine
    assert _say(ag, work={"tools": []}).get("work") is None


def test_a_long_work_is_cut_on_pages_and_whole_in_work(ag):
    tools = [{"name": f"t{i}", "ok": True, "ms": 5} for i in range(agora_work.PAGE_TOOLS + 15)]
    out = _say(ag, work={"tools": tools})
    shown = _message(ag, out["message_id"])["work"]
    assert shown["count"] == len(tools) and len(shown["tools"]) == agora_work.PAGE_TOOLS and shown["more"] == 15
    assert len(ag.work({**S, "message_id": out["message_id"]})["message"]["work"]["tools"]) == len(tools)


def test_what_the_person_writes_carries_neither(ag):
    out = ag.channel_post({**S, "agent": "person", "_person": True, "body": "Hola", "work": {"tools": TOOLS},
                           "routine": {"name": "Resumen"}})
    assert "work" not in out and "routine" not in out
    assert "routine" not in _message(ag, out["message_id"])


def test_routine_label_and_declared_schedule(ag):
    ag.agent_routine_set({**S, "agent": "claude", "name": "Resumen diario", "schedule": "cada día a las 8:00"})
    out = _say(ag, routine="Resumen diario")
    assert out["routine"] == {"id": "resumen-diario", "name": "Resumen diario", "schedule": "cada día a las 8:00"}
    assert _message(ag, out["message_id"])["routine"]["schedule"] == "cada día a las 8:00"
    other = _say(ag, routine={"id": "Radar 2", "name": "Radar diario", "schedule": "08:00"})
    assert other["routine"]["id"] == "radar-2" and other["routine"]["schedule"] == "08:00"
    ids = [m["id"] for m in ag.work({**S, "routine": "resumen-diario"})["messages"]]
    assert ids == [out["message_id"]]
    for bad in ({"schedule": "x"}, 7, {"name": "***"}):
        with pytest.raises(AgoraError):
            _say(ag, routine=bad)


def _lines(now, calls, *, open_call=None):
    lines = [cl_user(now, "Haz la tarea")]
    t = now
    for call, name, command, secs in calls:
        t += 2
        lines.append(cl_asst(t, cl_use(call, name, command=command)))
        t += secs
        lines.append(cl_result(t, call))
    if open_call:
        t += 1
        lines.append(cl_asst(t, cl_use(*open_call[:2], command=open_call[2])))
    return lines, t


def test_observed_session_tells_each_call_once_and_never_the_posting_call(ag, roots, clock):
    clock.t = NOW + 2000
    lines, last = _lines(NOW + 1500, [("u1", "Bash", "pytest tests -q", 30), ("u2", "Edit", "src/a.py", 1)],
                         open_call=("u3", "Bash", "agora.cmd say hola"))
    claude_file(roots["claude"], lines, last)
    w = watch(roots, clock)
    key = one(w)["key"]
    w.bind(key, "claude")
    ag.work_hook = lambda agent, since: w.claim_work(agent, since)
    first = _say(ag, body="Primera")
    assert [t["name"] for t in first["work"]["tools"]] == ["Bash", "Edit"] and first["work"]["source"] == "observed"
    assert first["work"]["tools"][0]["ms"] == 30_000 and first["work"]["tools"][0]["summary"] == "pytest tests -q"
    assert "agora.cmd say" not in str(first["work"])
    # the same calls are not told again; what ran after them is
    assert "work" not in _say(ag, body="Segunda")
    more, last = _lines(NOW + 1500, [("u1", "Bash", "pytest tests -q", 30), ("u2", "Edit", "src/a.py", 1),
                                     ("u3", "Bash", "agora.cmd say hola", 1), ("u4", "Grep", "buscar", 2)])
    claude_file(roots["claude"], more, last + 5)
    clock.t = last + 6
    third = _say(ag, body="Tercera")
    assert [t["name"] for t in third["work"]["tools"]] == ["Grep"]       # the call that posted «Primera» is nobody's work
    # an agent nobody observes carries nothing
    assert "work" not in _say(ag, agent="otro", body="Hola")


def test_a_declared_work_wins_over_the_observed_one(ag, roots, clock):
    clock.t = NOW + 2000
    lines, last = _lines(NOW + 1500, [("u1", "Bash", "ls", 2)])
    claude_file(roots["claude"], lines, last)
    w = watch(roots, clock)
    w.bind(one(w)["key"], "claude")
    ag.work_hook = lambda agent, since: w.claim_work(agent, since)
    out = _say(ag, work={"tools": [{"name": "Declared"}]})
    assert [t["name"] for t in out["work"]["tools"]] == ["Declared"] and out["work"]["source"] == "declared"


def test_a_broken_observer_never_loses_the_message(ag):
    def boom(agent, since):
        raise RuntimeError("transcripts unreadable")

    ag.work_hook = boom
    out = _say(ag)
    assert out["ok"] and "work" not in out


def test_turn_progress_counts_calls_of_the_running_turn(roots, clock):
    clock.t = NOW + 2000
    lines, last = _lines(NOW + 1500, [("u1", "Bash", "a", 3), ("u2", "Bash", "b", 3)], open_call=("u3", "Edit", "src/x.py"))
    claude_file(roots["claude"], lines, last)
    w = watch(roots, clock)
    w.bind(one(w)["key"], "claude")
    live = w.turn_progress(["claude", "nadie"])
    assert set(live) == {"claude"} and live["claude"]["steps"] == 3 and live["claude"]["done"] == 2
    assert live["claude"]["phase"] == "act" and live["claude"]["tool"] == "Edit" and live["claude"]["since"]
    summary = one(w)["loop"]
    assert (summary["calls"], summary["calls_done"]) == (3, 2)


def test_live_for_owners_adds_the_elapsed_time(ag):
    ag.live_hook = lambda owners: {"claude": {"steps": 5, "done": 4, "since": ag.clock() - 120, "phase": "act", "tool": "Bash"}}
    got = agora_work.live(ag, ["claude", "codex"])
    assert got["claude"]["steps"] == 5 and 120 <= got["claude"]["elapsed_s"] <= 130 and "codex" not in got
    ag.live_hook = None
    assert agora_work.live(ag, ["claude"]) == {}


def test_the_server_view_carries_live_steps_on_the_cards_of_working_tasks(tmp_path, roots, clock):
    clock.t = NOW + 2000
    lines, last = _lines(NOW + 1500, [("u1", "Bash", "a", 3)], open_call=("u2", "Edit", "src/x.py"))
    claude_file(roots["claude"], lines, last)
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"roots": {e: str(p) for e, p in roots.items()}}
    facet = hub.facet("agora")
    ag = facet.agora
    ag.space_create({"agent": "claude", "id": "atlas", "title": "Atlas"})
    task = ag.task_add({**S, "agent": "claude", "title": "Trabajo", "kind": "feature"})["task"]
    ag.task_claim({**S, "agent": "claude", "task_id": task["id"]})
    other = ag.task_add({**S, "agent": "claude", "title": "Sin sesión", "kind": "feature"})["task"]
    ag.task_claim({**S, "agent": "otro", "task_id": other["id"]})
    facet.watch.bind(next(s["key"] for s in facet.watch.view(force=True)["sessions"]), "claude")
    guild = facet._guild({**S})
    cards = {t["id"]: t for t in guild["tasks"]["in_progress"]}
    assert cards[task["id"]]["live"]["steps"] == 2 and cards[task["id"]]["live"]["done"] == 1
    assert cards[task["id"]]["live"]["elapsed_s"] is not None and "live" not in cards[other["id"]]
    # the posting path of the facet claims the same session
    told = facet._work_since("claude", 0.0)
    assert [c["name"] for c in told["calls"]] == ["Bash"]
    assert facet._work_since("claude", 0.0)["calls"] == []


def test_the_tool_and_its_schemas_are_offered():
    from hoard_link.hub import agora

    by = {t["name"]: t for t in agora.TOOLS}
    assert by["hub_agora_work"]["read"] is True
    for name in ("hub_agora_post", "hub_agora_channel_post"):
        props = by[name]["inputSchema"]["properties"]
        assert props["work"]["required"] == ["tools"] and props["routine"]["required"] == ["name"]
