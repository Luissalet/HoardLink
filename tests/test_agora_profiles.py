import pytest

from hoard_link.hub.agora import Agora, AgoraError, TOOLS
from hoard_link.hub.config import PERSON_ALIAS as PERSON
from hoard_link.hub import agora_profiles


@pytest.fixture()
def agora(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    ag.heartbeat({"agent": "claude-worker-1", "doing": "writing tests", "state": "working"})
    ag.heartbeat({"agent": "codex-bucle", "doing": "waiting"})
    yield ag
    ag.close()


def person(**kw):
    return {"agent": PERSON, "_person": True, **kw}


def test_profile_header_activity_and_empty_tabs(agora):
    task = agora.task_add({"agent": "claude-worker-1", "title": "Profile page", "kind": "feature"})
    agora.task_claim({"agent": "claude-worker-1", "task_id": task["task"]["id"]})
    agora.channel_post({"agent": "claude-worker-1", "channel": "general", "body": "hello from the worker"})
    out = agora.agent_profile({"id": "claude-worker-1"})
    head = out["profile"]
    assert head["id"] == "claude-worker-1" and head["provider"] == "claude" and head["engine"] == "Claude"
    assert head["initials"] == "CW" and head["rank"] == "agent" and head["presence"] == "working"
    assert head["since"] and head["in_server"] is True
    act = out["activity"]
    assert any("hello from the worker" in m["body"] for m in act["messages"])
    assert any(t["id"] == task["task"]["id"] and t["role"] == "owner" for t in act["tasks"])
    assert {e["type"] for e in act["timeline"]} >= {"message", "task"}
    assert out["instructions"]["body"] == "" and out["memory"] == [] and out["routines"] == []
    assert len(out["usage"]["days"]) == agora_profiles.USAGE_DAYS and out["usage"]["tokens"] is None
    assert sum(d["messages"] for d in out["usage"]["days"]) == len(act["messages"])
    with pytest.raises(AgoraError) as exc:
        agora.agent_profile({"id": "nobody-here"})
    assert exc.value.status == 404


def test_tell_reaches_the_inbox_and_pairs_share_one_channel(agora):
    sent = agora.agent_tell(person(to="claude-worker-1", text="Use English commit messages"))
    assert sent["ok"] and sent["channel"].startswith("dm-") and sent["delivery"]["status"] == "queued"
    box = agora.inbox({"agent": "claude-worker-1", "peek": True})
    assert any("Use English commit messages" in str(m) for m in box["messages"]), box
    again = agora.agent_tell({"agent": "claude-worker-1", "to": PERSON, "text": "Understood"})
    assert again["channel"] == sent["channel"] and again["thread_id"] == sent["thread_id"]
    mine = agora.dm_list({"agent": "claude-worker-1"})["dms"]
    assert [d["with_agent"] for d in mine] == [PERSON] and mine[0]["category"] == "dm"
    assert agora.dm_list({"agent": "codex-bucle"})["dms"] == []
    with pytest.raises(AgoraError):
        agora.agent_tell({"agent": "claude-worker-1", "to": "claude-worker-1", "text": "x"})
    with pytest.raises(AgoraError) as exc:
        agora.agent_tell({"agent": "claude-worker-1", "to": "ghost-agent", "text": "x"})
    assert exc.value.status == 404
    # the conversation is also in the profile, as the channel to open
    assert agora.agent_profile({"id": "claude-worker-1"})["dm"]["channel"] == sent["channel"]


def test_tell_calls_the_wake_hook_and_survives_its_failure(agora):
    seen = []
    agora.tell_hook = lambda target, mid, text, space: seen.append((target, space)) or {"status": "queued"}
    out = agora.agent_tell(person(to="codex-bucle", text="please look at 3328"))
    assert seen == [("codex-bucle", agora.space_id)] and out["delivery"]["wake"] == {"status": "queued"}
    agora.tell_hook = lambda *a: 1 / 0
    out = agora.agent_tell(person(to="codex-bucle", text="again"))
    assert out["ok"] and out["delivery"]["wake"]["status"] == "unavailable"


def test_a_correction_goes_to_memory_and_asks_for_a_proposal(agora):
    out = agora.agent_tell(person(to="claude-worker-1", text="Stop adding trailers to commits", correction=True))
    assert out["correction"] is True
    mem = agora.agent_profile({"id": "claude-worker-1"})["memory"]
    assert mem[0]["kind"] == "correction" and "trailers" in mem[0]["text"]
    box = agora.inbox({"agent": "claude-worker-1", "peek": True})
    assert any("agent_instruction_propose" in str(m) for m in box["messages"])
    # an agent cannot mark a correction: only the person does
    quiet = agora.agent_tell({"agent": "codex-bucle", "to": "claude-worker-1", "text": "psst", "correction": True})
    assert quiet["correction"] is False


def test_instructions_are_the_persons_and_proposals_are_the_agents(agora):
    with pytest.raises(AgoraError) as exc:
        agora.agent_instructions_set({"agent": "claude-worker-1", "id": "claude-worker-1", "body": "I rule"})
    assert exc.value.status == 403
    agora.agent_instructions_set(person(id="claude-worker-1", body="Rule one."))
    assert agora.agent_profile({"id": "claude-worker-1"})["instructions"]["body"] == "Rule one."

    proposal = agora.agent_instruction_propose({"agent": "claude-worker-1", "add": "Rule two.", "reason": "the person corrected me",
                                                 "source": "task 7", "trigger": "correction"})["proposal"]
    assert proposal["status"] == "pending" and proposal["body"] == "Rule one.\nRule two."
    assert "+Rule two." in proposal["diff"] and proposal["stale"] is False
    with pytest.raises(AgoraError) as exc:
        agora.agent_instruction_propose({"agent": "claude-worker-1", "add": "Rule two.", "reason": "again"})
    assert exc.value.status == 409
    with pytest.raises(AgoraError) as exc:
        agora.agent_instruction_propose({"agent": "codex-bucle", "id": "claude-worker-1", "add": "x", "reason": "y"})
    assert exc.value.status == 403
    with pytest.raises(AgoraError):
        agora.agent_instruction_propose({"agent": "claude-worker-1", "body": "Rule one.", "reason": "no change"})

    listed = agora.agent_instruction_proposals({"id": "claude-worker-1", "status": "pending"})
    assert listed["pending"] == 1 and listed["proposals"][0]["id"] == proposal["id"]
    prof = agora.agent_profile({"id": "claude-worker-1"})
    assert prof["proposals_pending"] == 1

    with pytest.raises(AgoraError) as exc:
        agora.agent_instruction_decide({"agent": "claude-worker-1", "proposal_id": proposal["id"], "decision": "accept"})
    assert exc.value.status == 403
    done = agora.agent_instruction_decide(person(proposal_id=proposal["id"], decision="accept",
                                                 body="Rule one.\nRule two, edited.", note="ok"))
    assert done["proposal"]["status"] == "accepted" and done["instructions"]["body"].endswith("edited.")
    with pytest.raises(AgoraError) as exc:
        agora.agent_instruction_decide(person(proposal_id=proposal["id"], decision="discard"))
    assert exc.value.status == 409
    prof = agora.agent_profile({"id": "claude-worker-1"})
    assert prof["proposals_pending"] == 0 and any(m["kind"] == "correction" for m in prof["memory"])
    box = agora.inbox({"agent": "claude-worker-1", "peek": True})
    assert any("aceptado" in str(m) for m in box["messages"])

    second = agora.agent_instruction_propose({"agent": "claude-worker-1", "add": "Rule three.", "reason": "again"})["proposal"]
    gone = agora.agent_instruction_decide(person(proposal_id=second["id"], decision="discard"))
    assert gone["proposal"]["status"] == "discarded"
    assert agora.agent_profile({"id": "claude-worker-1"})["instructions"]["body"].endswith("edited.")


def test_sending_a_delivery_back_notes_the_correction(agora):
    task = agora.task_add({"agent": "claude-worker-1", "title": "Deliver", "kind": "feature"})["task"]
    agora.task_claim({"agent": "claude-worker-1", "task_id": task["id"]})
    agora.task_submit({"agent": "claude-worker-1", "task_id": task["id"], "summary": "done", "commits": ["abc1234"]})
    sub = agora.task_submit  # noqa: F841 - the revision is read from the task below
    row = agora._task_row(task["id"])
    agora.task_review(person(task_id=task["id"], verdict="changes", body="Missing tests",
                             expected_submission_revision=row["submission_revision"]))
    mem = agora.agent_profile({"id": "claude-worker-1"})["memory"]
    assert any(m["kind"] == "correction" and "Missing tests" in m["text"] for m in mem)
    box = agora.inbox({"agent": "claude-worker-1", "peek": True})
    assert any("agent_instruction_propose" in str(m) for m in box["messages"])


def test_memory_and_routines_belong_to_their_agent(agora):
    agora.agent_memory_add({"agent": "claude-worker-1", "text": "The Hub test suite is slow", "kind": "learned"})
    with pytest.raises(AgoraError) as exc:
        agora.agent_memory_add({"agent": "codex-bucle", "id": "claude-worker-1", "text": "x"})
    assert exc.value.status == 403
    agora.agent_routine_set({"agent": "claude-worker-1", "name": "Inbox sweep", "schedule": "every 2 h"})
    agora.agent_routine_set({"agent": "claude-worker-1", "name": "Inbox sweep", "note": "reads the board"})
    prof = agora.agent_profile({"id": "claude-worker-1"})
    assert prof["memory"][0]["text"] == "The Hub test suite is slow"
    assert prof["routines"] == [{"name": "Inbox sweep", "schedule": "every 2 h", "note": "reads the board",
                                 "enabled": True, "updated_by": "claude-worker-1", "updated": prof["routines"][0]["updated"]}]
    agora.agent_routine_remove({"agent": "claude-worker-1", "name": "Inbox sweep"})
    assert agora.agent_profile({"id": "claude-worker-1"})["routines"] == []
    note_id = prof["memory"][0]["id"]
    with pytest.raises(AgoraError):
        agora.agent_memory_remove({"agent": "claude-worker-1", "memory_id": note_id})
    agora.agent_memory_remove(person(memory_id=note_id))
    assert agora.agent_profile({"id": "claude-worker-1"})["memory"] == []


def test_profile_hook_fills_model_and_runs(agora):
    import time
    now = time.time()
    agora.profile_hook = lambda ident, space: {"model": "claude-sonnet-5-5", "effort": "high", "since": now - 600,
                                               "runs": [{"started": now - 7200, "ended": now - 3600, "state": "completed"}]}
    out = agora.agent_profile({"id": "claude-worker-1"})
    assert out["profile"]["model"] == "claude-sonnet-5-5" and out["profile"]["effort"] == "high"
    assert out["profile"]["since_source"] == "session"
    assert round(sum(d["run_minutes"] for d in out["usage"]["days"])) == 60


def test_the_tools_exist_and_the_person_only_ones_are_not_tools():
    names = {t["name"] for t in TOOLS}
    assert {"hub_agora_agent_profile", "hub_agora_agent_tell", "hub_agora_agent_instruction_proposals",
            "hub_agora_agent_instruction_propose", "hub_agora_dm_list"} <= names
    assert not {"hub_agora_agent_instructions_set", "hub_agora_agent_instruction_decide"} & names


def test_a_started_worker_is_briefed_with_its_saved_instructions(tmp_path):
    from types import SimpleNamespace
    from hoard_link.hub.agora_console import AgoraConsoleFacet
    ag = Agora(tmp_path / "agora.db")
    try:
        ag.heartbeat({"agent": "claude-worker-9"})
        ag.agent_instructions_set(person(id="claude-worker-9", body="Always read CRITERIOS first."))
        view = SimpleNamespace(_agora=lambda: ag, space_id=ag.space_id)
        brief = AgoraConsoleFacet._saved_instructions(view, "claude-worker-9")
        assert "Always read CRITERIOS first." in brief and "saved by the person" in brief
        assert AgoraConsoleFacet._saved_instructions(view, "claude-worker-unknown") == ""
    finally:
        ag.close()
