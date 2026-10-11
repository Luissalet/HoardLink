"""Turn liveness: pure classification/continuation rules and their wiring into the Agora console."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hoard_link.hub import agora_liveness as lv
from hoard_link.hub.agora import Agora
from hoard_link.hub.agora_console import AgoraConsoleFacet, THREAD_ID


def classify(answer="", *, state="completed", exit_code=0, **evidence):
    return lv.classify_turn(state=state, exit_code=exit_code, answer=answer, evidence=evidence)


# -- classify_turn ---------------------------------------------------------------------------------------------------

def test_failed_cli_or_timeout_is_failed_even_with_evidence():
    assert classify("ok", state="failed", exit_code=None, done=1)["liveness"] == "failed"
    assert classify("ok", exit_code=2)["liveness"] == "failed"
    assert lv.classify_turn(state="completed", exit_code=0, answer="x", evidence={}, provider_error="quota")["liveness"] == "failed"


@pytest.mark.parametrize("answer", ["", "   \n\t "])
def test_empty_answer_without_evidence_is_empty_response(answer):
    assert classify(answer)["liveness"] == "empty_response"
    # a bare heartbeat is not progress
    assert classify(answer, heartbeats_with_doing=1)["liveness"] == "empty_response"


def test_empty_answer_with_evidence_is_not_empty_response():
    assert classify("", claims=1)["liveness"] == "advanced"


@pytest.mark.parametrize("answer", [
    "I am blocked by a missing credential.",
    "Estoy bloqueado: el repo no compila.",
    "Waiting for Luis to pick the branch.",
    "Necesito que Luis decida entre A y B.",
    "I need the owner to approve the lock.",
    "Tarea bloqueada por la revisión pendiente.",
])
def test_explicit_block_words_are_blocked(answer):
    assert classify(answer)["liveness"] == "blocked"


def test_done_or_submit_evidence_is_completed():
    assert classify("Listo.", done=1)["liveness"] == "completed"
    assert classify("Enviado a revisión.", submits=1, claims=1)["liveness"] == "completed"


@pytest.mark.parametrize("evidence", [{"claims": 1}, {"task_status_changes": 2}, {"messages_posted": 1},
                                      {"commits_reported": 1}, {"reviews": 1}])
def test_other_concrete_evidence_is_advanced(evidence):
    result = classify("Next I will continue.", **evidence)
    assert result["liveness"] == "advanced" and result["reasons"]


@pytest.mark.parametrize("answer", [
    "Next I will open the task and fix the parser.",
    "I'll now claim the task.",
    "I will start with the migration.",
    "Plan: 1) read, 2) patch.",
    "Voy a revisar la tarea 311.",
    "A continuación abriré la rama.",
    "El siguiente paso es tomar los locks.",
    "Ahora empezaré por los tests.",
])
def test_announced_future_work_without_evidence_is_plan_only(answer):
    result = classify(answer, heartbeats_with_doing=1)
    assert result["liveness"] == "plan_only", answer
    assert any("future work" in reason for reason in result["reasons"])


def test_anything_else_needs_followup():
    assert classify("The build is green and the sky is blue.")["liveness"] == "needs_followup"
    assert classify("No pude completar la revisión: falta la herramienta.")["liveness"] == "needs_followup"


def test_block_wins_over_plan():
    assert classify("I'm blocked. Next I will wait.")["liveness"] == "blocked"


def test_evidence_is_tolerant_of_bad_values():
    assert lv.classify_turn(state="completed", exit_code=0, answer="Voy a mirar.", evidence=None)["liveness"] == "plan_only"
    assert lv.classify_turn(state="completed", exit_code=0, answer="x", evidence={"done": "n/a"})["liveness"] == "needs_followup"


# -- decide_continuation ---------------------------------------------------------------------------------------------

def decide(liveness="plan_only", prior=0, streak=1, last=None, now=1000.0, event=False, root="r1"):
    return lv.decide_continuation(liveness=liveness, prior_continuations=prior, consecutive_no_progress=streak,
                                  last_continuation_at=last, now=now, real_event_since=event, root_turn_id=root)


@pytest.mark.parametrize("liveness", ["completed", "advanced", "blocked", "failed", "needs_followup"])
def test_only_plan_only_and_empty_response_continue(liveness):
    assert decide(liveness)["continue"] is False


@pytest.mark.parametrize("liveness", ["plan_only", "empty_response"])
def test_first_continuation_is_immediate_with_key(liveness):
    out = decide(liveness)
    assert out["continue"] is True and out["delay_s"] == 0.0
    assert out["key"] == f"liveness:r1:{liveness}:1"


def test_at_most_two_continuations_per_chain():
    assert decide(prior=1)["continue"] is True
    assert decide(prior=1)["key"] == "liveness:r1:plan_only:2"
    stopped = decide(prior=2)
    assert stopped["continue"] is False and "2" in stopped["reason"]


def test_throttle_waits_120_then_doubles_up_to_1800():
    assert lv.throttle_wait(0) == lv.throttle_wait(1) == 0
    assert [lv.throttle_wait(n) for n in (2, 3, 4, 5, 6, 9)] == [120, 240, 480, 960, 1800, 1800]
    assert decide(streak=2, last=None)["delay_s"] == 120.0
    assert decide(streak=3, last=None)["delay_s"] == 240.0


def test_throttle_credits_time_already_elapsed():
    assert decide(streak=2, last=1000.0 - 100)["delay_s"] == pytest.approx(20.0)
    assert decide(streak=2, last=1000.0 - 500)["delay_s"] == 0.0


def test_real_event_bypasses_throttle():
    out = decide(streak=5, last=999.0, event=True)
    assert out["continue"] is True and out["delay_s"] == 0.0 and "real event" in out["reason"]
    # but a real event does not lift the per-chain cap
    assert decide(prior=2, event=True)["continue"] is False


def test_continuation_prompt_keeps_instruction_first_and_context_bounded():
    prompt = lv.continuation_prompt("plan_only", ["no evidence"], "x" * 50_000)
    assert prompt.startswith("Your previous Agora console turn ended with plan_only (no evidence).")
    assert "claim/advance a task, post evidence, or say exactly what blocks you" in prompt
    assert len(prompt) <= 30_000


# -- console wiring ---------------------------------------------------------------------------------------------------

@pytest.fixture
def console(tmp_path):
    agora = Agora(tmp_path / "agora.db")
    for index in range(THREAD_ID):  # the console posts into canonical thread 32
        agora.thread_open({"agent": "seed-agent", "title": f"seed {index}", "body": "seed", "kind": "debate"})
    agora.db.execute("UPDATE messages SET created=created-3600")  # old history: not a "new" event for any turn
    hub = SimpleNamespace(
        config=SimpleNamespace(data_dir=str(tmp_path), token_file=str(tmp_path / "t"), url="http://127.0.0.1:1",
                               faustus_urls=[]),
        facet=lambda facet_id: SimpleNamespace(agora=agora) if facet_id == "agora" else None,
    )
    facet = AgoraConsoleFacet(hub)
    facet.agora = agora
    facet._drains = []
    facet._ensure_leader_drain = lambda leader_id: facet._drains.append(leader_id)
    yield facet
    facet.close()
    agora.close()


def _turn(facet, turn_id, *, leader="codex", created=None, root="", n=0, prompt="ctx", state="working"):
    created = time.time() - 30 if created is None else created
    with facet._db() as db:
        db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at,started_at,root_turn_id,continuation_n) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (turn_id, leader, "902", prompt, "m", "high", state, created, created, created, root, n))


def _row(facet, turn_id):
    with facet._db() as db:
        return dict(db.execute("SELECT * FROM leader_turns WHERE id=?", (turn_id,)).fetchone())


def _continuations(facet):
    with facet._db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM leader_turns WHERE continuation_n>0 ORDER BY continuation_n")]


def test_migration_adds_liveness_columns_and_unique_key(console):
    with console._db() as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(leader_turns)")}
    assert {"liveness", "liveness_reasons", "root_turn_id", "continuation_n", "continuation_key", "not_before"} <= columns
    _turn(console, "a")
    _turn(console, "b")
    with console._db() as db:
        db.execute("UPDATE leader_turns SET continuation_key='liveness:x:plan_only:1' WHERE id='a'")
        with pytest.raises(Exception):
            db.execute("UPDATE leader_turns SET continuation_key='liveness:x:plan_only:1' WHERE id='b'")


def test_plan_only_turn_enqueues_exactly_one_continuation_then_chain_stops(console):
    _turn(console, "t0", prompt="ORIGINAL CONTEXT")
    console._finish_leader_turn("t0", "codex", "completed", "ok", "Next I will claim the task.", "sess", process_exit_code=0)
    first = _row(console, "t0")
    assert first["liveness"] == "plan_only" and "future work" in first["liveness_reasons"]
    assert first["root_turn_id"] == "t0"
    conts = _continuations(console)
    assert len(conts) == 1
    c1 = conts[0]
    assert c1["state"] == "queued" and c1["continuation_n"] == 1 and c1["root_turn_id"] == "t0"
    assert c1["continuation_key"] == "liveness:t0:plan_only:1"
    assert c1["prompt"].startswith("Your previous Agora console turn ended with plan_only") and "ORIGINAL CONTEXT" in c1["prompt"]
    assert (c1["model"], c1["effort"], c1["message_id"]) == ("m", "high", "902") and c1["not_before"] == 0
    assert console._drains == ["codex"]

    # the continuation runs and again only announces work: second (throttled) continuation
    with console._db() as db:
        db.execute("UPDATE leader_turns SET state='working', started_at=? WHERE id=?", (time.time(), c1["id"]))
    console._finish_leader_turn(c1["id"], "codex", "completed", "ok", "Voy a revisar la tarea.", "sess", process_exit_code=0)
    conts = _continuations(console)
    assert [c["continuation_n"] for c in conts] == [1, 2]
    c2 = conts[1]
    assert c2["continuation_key"] == "liveness:t0:plan_only:2" and c2["root_turn_id"] == "t0"
    assert c2["not_before"] > time.time() + 60  # two no-progress turns in a row: throttled

    # the third plan-only turn does not trigger a third continuation
    with console._db() as db:
        db.execute("UPDATE leader_turns SET state='working', started_at=? WHERE id=?", (time.time(), c2["id"]))
    console._finish_leader_turn(c2["id"], "codex", "completed", "ok", "I'll now start.", "sess", process_exit_code=0)
    assert len(_continuations(console)) == 2
    assert _row(console, c2["id"])["liveness"] == "plan_only"


def test_agora_activity_makes_the_turn_advanced_and_stops_the_chain(console):
    task = console.agora.task_add({"agent": "luis-helper", "title": "Do the thing"})["task"]
    _turn(console, "t1")
    console.agora.task_claim({"agent": "codex-agora", "task_id": task["id"]})
    console._finish_leader_turn("t1", "codex", "completed", "ok", "Next I will continue.", "sess", process_exit_code=0)
    row = _row(console, "t1")
    assert row["liveness"] == "advanced" and "claims=1" in row["liveness_reasons"]
    assert _continuations(console) == []


def test_posted_messages_count_but_the_consoles_own_auto_post_does_not(console):
    _turn(console, "t2")
    console._finish_leader_turn("t2", "codex", "completed", "ok", "Next I will do it.", "sess", process_exit_code=0)
    # the answer was posted by codex-agora in thread 32, yet the turn is still plan_only: it was not counted as activity
    assert _row(console, "t2")["liveness"] == "plan_only"
    assert console.agora.db.scalar("SELECT COUNT(*) FROM messages WHERE author='codex-agora' AND kind<>'system'") == 1
    console.agora.post({"agent": "codex-agora", "thread_id": THREAD_ID, "body": "evidence: commit abc1234d pushed", "kind": "comment"})
    evidence = console._liveness_evidence("codex", time.time() - 60, time.time() + 1)
    assert evidence["messages_posted"] == 2 and evidence["commits_reported"] == 1
    assert console._liveness_evidence("cursor", time.time() - 60, time.time() + 1)["messages_posted"] == 0


def test_real_event_for_the_agent_bypasses_the_throttle(console):
    for index, prior in enumerate(("p1", "p2")):
        _turn(console, prior, created=time.time() - 100 + index, state="completed")
        with console._db() as db:
            db.execute("UPDATE leader_turns SET liveness='plan_only' WHERE id=?", (prior,))
    _turn(console, "t4", created=time.time() - 20)
    console.agora.post({"agent": "luis-helper", "thread_id": THREAD_ID, "body": "codex-agora please look", "kind": "comment",
                        "mentions": ["codex-agora"]})
    console._finish_leader_turn("t4", "codex", "completed", "ok", "I'll now look.", "sess", process_exit_code=0)
    conts = _continuations(console)
    assert len(conts) == 1 and conts[0]["not_before"] == 0


def test_without_a_real_event_the_same_streak_is_throttled(console):
    for index, prior in enumerate(("p1", "p2")):
        _turn(console, prior, created=time.time() - 100 + index, state="completed")
        with console._db() as db:
            db.execute("UPDATE leader_turns SET liveness='plan_only' WHERE id=?", (prior,))
    _turn(console, "t5", created=time.time() - 20)
    console._finish_leader_turn("t5", "codex", "completed", "ok", "I'll now look.", "sess", process_exit_code=0)
    conts = _continuations(console)
    assert len(conts) == 1 and conts[0]["not_before"] > time.time() + 100


def test_failed_blocked_and_unreadable_agora_never_continue(console):
    _turn(console, "f1")
    console._finish_leader_turn("f1", "codex", "failed", "boom", "", "sess")
    _turn(console, "b1")
    console._finish_leader_turn("b1", "codex", "completed", "ok", "Estoy bloqueado esperando a Luis.", "sess", process_exit_code=0)
    assert (_row(console, "f1")["liveness"], _row(console, "b1")["liveness"]) == ("failed", "blocked")
    assert _continuations(console) == []
    # without a readable Agora the turn is classified but nothing is relaunched
    console.hub.facet = lambda _id: None
    _turn(console, "u1")
    console._finish_leader_turn("u1", "codex", "completed", "ok", "Next I will start.", "sess", process_exit_code=0)
    assert "evidence unavailable" in _row(console, "u1")["liveness_reasons"]
    assert _continuations(console) == []


def test_continuation_is_idempotent_and_skipped_when_work_is_already_queued(console):
    _turn(console, "q0")
    with console._db() as db:
        db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at) "
                   "VALUES('newer','codex','1','p','m','high','queued',?,?)", (time.time(), time.time()))
    console._finish_leader_turn("q0", "codex", "completed", "ok", "Voy a hacerlo.", "sess", process_exit_code=0)
    assert _continuations(console) == []  # a real queued turn already gives the leader work
    with console._db() as db:
        db.execute("UPDATE leader_turns SET state='completed' WHERE id='newer'")
        db.execute("UPDATE leader_turns SET started_at=? WHERE id='q0'", (time.time(),))  # after the console's own auto-post
    # replaying the same finish must not create a duplicate key
    console._continue_if_idle("q0", "codex", console._assess_liveness("q0", "codex", "completed", "Voy a hacerlo.", 0, {}))
    console._continue_if_idle("q0", "codex", console._assess_liveness("q0", "codex", "completed", "Voy a hacerlo.", 0, {}))
    assert len(_continuations(console)) == 1


def test_delayed_continuation_is_not_run_before_not_before_and_wakes_up(console):
    _turn(console, "d0")
    with console._db() as db:
        db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at,not_before) "
                   "VALUES('later','codex','1','p','m','high','queued',?,?,?)", (time.time(), time.time(), time.time() + 3600))
    started = []
    console._run_leader_turn = lambda turn: started.append(turn["id"])
    AgoraConsoleFacet._drain_leader(console, "codex")
    assert started == []
    timer = console._wakeups["codex"]
    assert timer.is_alive()
    with console._db() as db:
        db.execute("UPDATE leader_turns SET not_before=? WHERE id='later'", (time.time() - 1,))
    # once due, the drain picks it up
    def finish_after_run(turn):
        started.append(turn["id"])
        with console._db() as db:
            db.execute("UPDATE leader_turns SET state='completed' WHERE id=?", (turn["id"],))
    console._run_leader_turn = finish_after_run
    AgoraConsoleFacet._drain_leader(console, "codex")
    assert started == ["later"]


def test_overview_lists_liveness_on_console_turns(console):
    _turn(console, "o1")
    console._finish_leader_turn("o1", "codex", "completed", "ok", "Next I will start.", "sess", process_exit_code=0)
    console._provider_cache = {k: {"state": "unavailable", "detail": "", "catalog": []} for k in ("codex", "claude", "cursor", "faustus")}
    console._provider_at = 10**9
    turns = {t["id"]: t for t in console.overview()["console_turns"]}
    assert turns["o1"]["liveness"] == "plan_only" and turns["o1"]["root_turn_id"] == "o1"
    assert "liveness" in AgoraConsoleFacet.tools()[0]["description"]


# -- review fixes: plan-only messages, completed beats blocked, no relaunch without quota -------------------------

def test_message_that_only_announces_work_is_not_progress():
    from hoard_link.hub import agora_liveness as lv
    res = lv.classify_turn(state="completed", exit_code=0, answer="Voy a revisar la #312 ahora.",
                           evidence={"messages_posted": 1, "plan_messages": 1})
    assert res["liveness"] == "plan_only"


def test_message_with_commit_reference_is_progress():
    from hoard_link.hub import agora_liveness as lv
    assert lv.is_plan_text("voy a seguir") and not lv.is_plan_text("hecho en 7a11e80, voy a seguir")
    res = lv.classify_turn(state="completed", exit_code=0, answer="Hecho en 7a11e80.",
                           evidence={"messages_posted": 1, "plan_messages": 0, "commits_reported": 1})
    assert res["liveness"] == "advanced"


def test_submit_wins_over_a_blocked_word_in_the_answer():
    from hoard_link.hub import agora_liveness as lv
    res = lv.classify_turn(state="completed", exit_code=0,
                           answer="The test was blocked by a lock earlier; submitted #311 for review.",
                           evidence={"submits": 1})
    assert res["liveness"] == "completed"


def test_exhausted_quota_never_continues():
    from hoard_link.hub import agora_liveness as lv
    d = lv.decide_continuation(liveness="plan_only", prior_continuations=0, consecutive_no_progress=0,
                               last_continuation_at=None, now=1000.0, real_event_since=True,
                               root_turn_id="r", quota_exhausted=True)
    assert d["continue"] is False and "quota" in d["reason"]


def test_lock_vocabulary_is_not_a_block():
    from hoard_link.hub import agora_liveness as lv
    for text in ("Tomo el bloqueo path:Faustus/src y sigo.", "Reclamada con sus bloqueos; voy a implementar.",
                 "The task was blocked by a lock, now free; I'll start."):
        assert lv.classify_turn(state="completed", exit_code=0, answer=text, evidence={})["liveness"] != "blocked", text
    for text in ("Estoy bloqueado: necesito la clave.", "Blocked on Luis for the GPU decision.", "No puedo seguir sin acceso."):
        assert lv.classify_turn(state="completed", exit_code=0, answer=text, evidence={})["liveness"] == "blocked", text


# -- review of r1: activity by OTHER agents on the leader's tasks is not the leader's progress ----------------------

def test_a_review_assigned_during_the_turn_is_not_a_review_done(console):
    """codex-review-writer's repro: another agent submits a task with reviewer codex-agora while the turn is open; the
    leader only says it will review. That is a plan (and a real event), not progress."""
    task = console.agora.task_add({"agent": "builder-agent", "title": "Needs a review"})["task"]
    console.agora.task_claim({"agent": "builder-agent", "task_id": task["id"]})
    _turn(console, "rv1")
    console.agora.task_submit({"agent": "builder-agent", "task_id": task["id"], "summary": "ready",
                               "reviewer": "codex-agora"})
    console._finish_leader_turn("rv1", "codex", "completed", "ok", "I'll now review this task.", "sess",
                                process_exit_code=0)
    row = _row(console, "rv1")
    assert row["liveness"] == "plan_only", row["liveness_reasons"]
    assert "reviews=" not in row["liveness_reasons"]
    conts = _continuations(console)
    assert len(conts) == 1  # continued, and the new review assignment is a real event: no throttle delay
    assert float(conts[0]["not_before"] or 0) == 0.0


def test_a_vote_by_the_leader_is_a_review(console):
    task = console.agora.task_add({"agent": "builder-agent", "title": "Needs a review"})["task"]
    console.agora.task_claim({"agent": "builder-agent", "task_id": task["id"]})
    sub = console.agora.task_submit({"agent": "builder-agent", "task_id": task["id"], "summary": "ready",
                                     "reviewer": "codex-agora"})
    revision = (sub.get("task") or {}).get("submission_revision") or 1
    _turn(console, "rv2")
    console.agora.task_review({"agent": "codex-agora", "task_id": task["id"], "verdict": "approve",
                               "body": "tests pass", "expected_submission_revision": revision})
    console._finish_leader_turn("rv2", "codex", "completed", "ok", "Reviewed.", "sess", process_exit_code=0)
    row = _row(console, "rv2")
    assert row["liveness"] == "advanced" and "reviews=1" in row["liveness_reasons"]


def test_another_agent_moving_the_leaders_task_is_not_its_progress(console):
    task = console.agora.task_add({"agent": "builder-agent", "title": "Mine"})["task"]
    console.agora.task_claim({"agent": "codex-agora", "task_id": task["id"]})
    time.sleep(0.02)
    _turn(console, "rv3", created=time.time())  # the leader's own claim happened before this turn
    console.agora.handover({"agent": "luis", "_person": True, "from_agent": "codex-agora", "to_agent": "builder-agent",
                            "tasks": [task["id"]], "force": True, "reason": "test"})
    console._finish_leader_turn("rv3", "codex", "completed", "ok", "Voy a seguir con la tarea.", "sess",
                                process_exit_code=0)
    assert _row(console, "rv3")["liveness"] == "plan_only"
