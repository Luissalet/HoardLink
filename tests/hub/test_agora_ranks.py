"""Ranks: the person gives each agent a rank on each server, and a higher rank decides. Its word closes a
disagreement, its review stands, it hands out the work, and questions go to it before they reach the person."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub import agora_ranks

from ._hub_fakes import http, make_hub, serve


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        self.t += 1
        return self.t


FABLE, OPUS, SONNET, HAIKU = "claude-fable", "claude-opus", "claude-sonnet", "claude-haiku"
TEAM = {FABLE: "director", OPUS: "lead", HAIKU: "support"}          # SONNET keeps the rank nobody gave it: agent


@pytest.fixture
def ag(tmp_path):
    clock = Clock()
    a = Agora(tmp_path / "agora.db", clock=clock)
    a.fake_clock = clock
    a.space_create({"agent": "claude", "id": "atlas", "title": "Atlas"})
    for agent in (FABLE, OPUS, SONNET, HAIKU):
        a.heartbeat({"space_id": "atlas", "agent": agent, "state": "working"})
    for agent, rank in TEAM.items():
        a.member_rank_set({"space_id": "atlas", "agent": "luis", "_person": True, "member": agent, "rank": rank})
    yield a
    a.close()


def _refused(call, *args, status=403):
    with pytest.raises(AgoraError) as caught:
        call(*args)
    assert caught.value.status == status, caught.value
    return caught.value


def _alive(a, *agents):
    for agent in agents:
        a.heartbeat({"space_id": "atlas", "agent": agent, "state": "working"})


def test_only_the_person_gives_ranks_and_everyone_starts_as_an_agent(ag):
    ladder = ag.ranks({"space_id": "atlas"})
    assert [r["id"] for r in ladder["ladder"]] == ["director", "lead", "agent", "support"] and ladder["default"] == "agent"
    assert len(ladder["rules"]) == 4
    held = {m["id"]: m["rank"] for m in ladder["members"]}
    assert held[FABLE] == "director" and held[OPUS] == "lead" and held[SONNET] == "agent" and held[HAIKU] == "support"
    assert [m["id"] for m in ladder["members"]][:2] == [FABLE, OPUS]                 # the highest first

    # an agent decides neither its own rank nor another's; the person does, among the ranks there are
    for attempt in ({"agent": SONNET, "member": SONNET, "rank": "director"}, {"agent": FABLE, "member": HAIKU, "rank": "lead"},
                    {"agent": "luis", "member": SONNET, "rank": "director"}):
        assert _refused(ag.member_rank_set, {"space_id": "atlas", **attempt}).status == 403
    assert agora_ranks.rank_of(ag.for_space("atlas"), SONNET) == "agent"
    person = {"space_id": "atlas", "agent": "luis", "_person": True}
    _refused(ag.member_rank_set, {**person, "member": SONNET, "rank": "emperor"}, status=400)
    _refused(ag.member_rank_set, {**person, "member": "codex-nobody", "rank": "lead"}, status=404)
    _refused(ag.member_rank_set, {**person, "member": "luis", "rank": "lead"}, status=409)

    out = ag.member_rank_set({**person, "member": SONNET, "rank": "lead"})
    assert out["changed"] and out["rank"] == "lead" and out["rank_level"] == 3
    assert not ag.member_rank_set({**person, "member": SONNET, "rank": "lead"})["changed"]       # said once
    lines = [m["body"] for m in ag.channel_read({"space_id": "atlas", "channel": "general"})["messages"]]
    assert lines.count(f"luis pone a {SONNET} como Responsable de este servidor.") == 1
    # the member list and the server view carry it; the person stands above the ladder
    members = {m["id"]: m for m in ag.members({"space_id": "atlas"})["members"]}
    assert members[SONNET]["rank"] == "lead" and members[SONNET]["rank_level"] == 3
    assert members[FABLE]["rank"] == "director" and members[SONNET]["role"] != members[SONNET]["rank"]
    guild = {m["id"]: m for m in ag.guild({"space_id": "atlas", "agent": "luis", "_person": True})["members"]}
    assert guild[HAIKU]["rank"] == "support" and guild.get("luis", {"rank": "person"})["rank"] == "person"

    # a rank is of one server, and leaves with the agent
    ag.space_create({"agent": "claude", "id": "zeta", "title": "Zeta"})
    ag.heartbeat({"space_id": "zeta", "agent": FABLE, "state": "working"})
    assert {m["id"]: m["rank"] for m in ag.ranks({"space_id": "zeta"})["members"]}[FABLE] == "agent"
    ag.member_remove({**person, "id": FABLE})
    ag.heartbeat({"space_id": "atlas", "agent": FABLE, "state": "working"})
    assert {m["id"]: m["rank"] for m in ag.ranks({"space_id": "atlas"})["members"]}[FABLE] == "agent"


def test_the_word_of_a_higher_rank_closes_a_disagreement(ag):
    S = {"space_id": "atlas"}
    th = ag.thread_open({**S, "agent": SONNET, "title": "¿Canvas o SVG?", "kind": "debate", "body": "Propongo canvas."})["thread"]["id"]
    ag.post({**S, "agent": FABLE, "thread_id": th, "kind": "disagree", "body": "SVG: accesibilidad."})
    ag.post({**S, "agent": HAIKU, "thread_id": th, "kind": "disagree", "body": "Yo tampoco lo veo."})
    refused = _refused(ag.resolve, {**S, "agent": SONNET, "thread_id": th, "resolution": "Canvas."})
    assert refused.extra["error_code"] == "rank" and refused.extra["outranked_by"] == FABLE      # not Haiku: it is below
    assert ag.thread({**S, "thread_id": th})["thread"]["status"] == "open"
    # convinced, or the higher rank closing it itself: both end the disagreement
    ag.post({**S, "agent": FABLE, "thread_id": th, "kind": "agree", "body": "Con virtualización, vale."})
    assert ag.resolve({**S, "agent": SONNET, "thread_id": th, "resolution": "Canvas con virtualización."})["thread"]["status"] == "resolved"

    th2 = ag.thread_open({**S, "agent": SONNET, "title": "¿Idioma de los informes?", "kind": "question", "body": "?"})["thread"]["id"]
    ag.post({**S, "agent": SONNET, "thread_id": th2, "kind": "disagree", "body": "Inglés no."})
    assert ag.resolve({**S, "agent": FABLE, "thread_id": th2, "resolution": "Castellano."})["thread"]["resolved_by"] == FABLE
    # from below it is objected to, not reopened nor resolved again
    said = ag.post({**S, "agent": SONNET, "thread_id": th2, "kind": "disagree", "body": "Sigo sin verlo."})
    assert said["stays_resolved"] and said["thread"]["status"] == "resolved" and FABLE in said["note"]
    assert any(m["body"] == "Sigo sin verlo." for m in ag.inbox({**S, "agent": FABLE, "peek": True})["messages"])   # and is told
    assert _refused(ag.reopen, {**S, "agent": OPUS, "thread_id": th2, "reason": "revisar"}).extra["outranked_by"] == FABLE
    _refused(ag.resolve, {**S, "agent": SONNET, "thread_id": th2, "resolution": "Inglés."})
    assert ag.thread({**S, "thread_id": th2})["thread"]["resolution"] == "Castellano."
    # an equal of whoever resolved it, and the person, may
    ag.heartbeat({**S, "agent": "codex-sol", "state": "working"})
    ag.member_rank_set({**S, "agent": "luis", "_person": True, "member": "codex-sol", "rank": "director"})
    assert ag.reopen({**S, "agent": "codex-sol", "thread_id": th2, "reason": "otra mirada"})["thread"]["status"] == "open"
    ag.resolve({**S, "agent": FABLE, "thread_id": th2, "resolution": "Castellano, visto otra vez."})
    assert ag.reopen({**S, "agent": "luis", "_person": True, "thread_id": th2, "reason": "lo miro yo"})["thread"]["status"] == "open"

    # what a lower rank resolved is opened again by an objection from above, as it always was
    th3 = ag.thread_open({**S, "agent": HAIKU, "title": "Nombre del canal", "kind": "decision", "body": "ideas"})["thread"]["id"]
    ag.resolve({**S, "agent": HAIKU, "thread_id": th3, "resolution": "ideas"})
    again = ag.post({**S, "agent": SONNET, "thread_id": th3, "kind": "disagree", "body": "mejor propuestas"})
    assert again["thread"]["status"] == "open" and "stays_resolved" not in again
    # and without ranks in play nothing changes: equals resolve over each other's disagreement
    ag.heartbeat({**S, "agent": "codex-terra", "state": "working"})
    th4 = ag.thread_open({**S, "agent": SONNET, "title": "Entre iguales", "kind": "debate", "body": "?"})["thread"]["id"]
    ag.post({**S, "agent": "codex-terra", "thread_id": th4, "kind": "disagree", "body": "no"})
    assert ag.resolve({**S, "agent": SONNET, "thread_id": th4, "resolution": "sí"})["thread"]["status"] == "resolved"


def test_the_review_of_a_higher_rank_stands(ag):
    S = {"space_id": "atlas"}
    tid = ag.task_add({**S, "agent": SONNET, "title": "Importar CSV", "claim": True})["task"]["id"]
    ag.task_submit({**S, "agent": SONNET, "task_id": tid, "summary": "hecho", "commits": ["abc1234"]})
    ag.task_review({**S, "agent": FABLE, "task_id": tid, "verdict": "changes", "body": "falta la vista previa",
                    "expected_submission_revision": 1})
    refused = _refused(ag.task_review, {**S, "agent": OPUS, "task_id": tid, "verdict": "approve", "body": "a mí me vale",
                                         "expected_submission_revision": 1})
    assert refused.extra["error_code"] == "rank" and refused.extra["outranked_by"] == FABLE
    # not even force closes the task over it
    closing = _refused(ag.task_done, {**S, "agent": SONNET, "task_id": tid, "result": "lo cierro", "force": True, "reason": "prisa"})
    assert closing.extra["outranked_by"] == FABLE and ag.task({**S, "task_id": tid})["task"]["status"] == "changes"
    # a new delivery is a new review: anyone may approve it, and whoever asked for changes may approve their own
    ag.task_submit({**S, "agent": SONNET, "task_id": tid, "summary": "con vista previa", "commits": ["def5678"]})
    assert ag.task_review({**S, "agent": OPUS, "task_id": tid, "verdict": "approve", "body": "ahora sí",
                           "expected_submission_revision": 2})["task"]["status"] == "approved"
    ag.task_review({**S, "agent": FABLE, "task_id": tid, "verdict": "changes", "body": "un detalle más", "expected_submission_revision": 2})
    assert ag.task_review({**S, "agent": FABLE, "task_id": tid, "verdict": "approve", "body": "visto",
                           "expected_submission_revision": 2})["task"]["status"] == "approved"
    ag.task_review({**S, "agent": FABLE, "task_id": tid, "verdict": "changes", "body": "no, falta", "expected_submission_revision": 2})
    assert ag.task_review({**S, "agent": "luis", "_person": True, "task_id": tid, "verdict": "approve", "body": "vale así",
                           "expected_submission_revision": 2})["task"]["status"] == "approved"   # the person, always

    # changes asked from below, or by an equal, are overruled as they always were: with another approval, or force
    other = ag.task_add({**S, "agent": SONNET, "title": "Otra", "claim": True})["task"]["id"]
    ag.task_submit({**S, "agent": SONNET, "task_id": other, "summary": "hecho", "commits": ["aaa1111"]})
    ag.task_review({**S, "agent": HAIKU, "task_id": other, "verdict": "changes", "body": "no sé", "expected_submission_revision": 1})
    _refused(ag.task_done, {**S, "agent": SONNET, "task_id": other, "result": "x"}, status=409)        # still needs the reason
    assert ag.task_done({**S, "agent": SONNET, "task_id": other, "result": "x", "force": True, "reason": "revisado aparte"})["ok"]


def test_the_person_is_on_no_rung_and_a_verdict_is_not_replaced_from_below(ag):
    S = {"space_id": "atlas"}
    person = {**S, "agent": "luis", "_person": True}
    # what the person resolved is treated among the agents as it always was: objected to, it opens again
    th = ag.thread_open({**S, "agent": HAIKU, "title": "Decidido por la persona", "kind": "question", "body": "?"})["thread"]["id"]
    ag.resolve({**person, "thread_id": th, "resolution": "A"})
    again = ag.post({**S, "agent": HAIKU, "thread_id": th, "kind": "disagree", "body": "hay un hecho nuevo"})
    assert again["thread"]["status"] == "open" and "stays_resolved" not in again
    ag.resolve({**person, "thread_id": th, "resolution": "A"})
    assert ag.reopen({**S, "agent": HAIKU, "thread_id": th, "reason": "hecho nuevo"})["thread"]["status"] == "open"
    ag.post({**person, "thread_id": th, "kind": "disagree", "body": "no"})
    assert ag.resolve({**S, "agent": HAIKU, "thread_id": th, "resolution": "B"})["thread"]["status"] == "resolved"
    # and so is their review: another approval or a forced close with its reason, as before
    tid = ag.task_add({**S, "agent": SONNET, "title": "Revisada por la persona", "claim": True})["task"]["id"]
    ag.task_submit({**S, "agent": SONNET, "task_id": tid, "summary": "hecho", "commits": ["abc1234"]})
    ag.task_review({**person, "task_id": tid, "verdict": "changes", "body": "falta algo", "expected_submission_revision": 1})
    assert ag.task_done({**S, "agent": SONNET, "task_id": tid, "result": "x", "force": True, "reason": "hablado con él"})["ok"]

    # the verdict of a higher rank is not replaced from below: nobody below signs that delivery again, neither to
    # approve it nor to become its reviewer and approve it then
    other = ag.task_add({**S, "agent": SONNET, "title": "Entrega", "claim": True})["task"]["id"]
    ag.task_submit({**S, "agent": SONNET, "task_id": other, "summary": "hecho", "commits": ["abc1234"]})
    ag.task_review({**S, "agent": FABLE, "task_id": other, "verdict": "changes", "body": "falta", "expected_submission_revision": 1})
    for verdict in ("changes", "approve"):
        refused = _refused(ag.task_review, {**S, "agent": HAIKU, "task_id": other, "verdict": verdict, "body": "yo",
                                             "expected_submission_revision": 1})
        assert refused.extra["outranked_by"] == FABLE
    assert ag.task({**S, "task_id": other})["task"]["reviewer"] == FABLE
    _refused(ag.task_done, {**S, "agent": SONNET, "task_id": other, "result": "x", "force": True, "reason": "prisa"})
    # what it approved is not sent back from below either; an equal and whoever is above still may
    ag.task_review({**S, "agent": FABLE, "task_id": other, "verdict": "approve", "body": "vale", "expected_submission_revision": 1})
    _refused(ag.task_review, {**S, "agent": OPUS, "task_id": other, "verdict": "changes", "body": "no", "expected_submission_revision": 1})
    assert ag.task({**S, "task_id": other})["task"]["status"] == "approved"
    ag.heartbeat({**S, "agent": "codex-sol", "state": "working"})
    ag.member_rank_set({**person, "member": "codex-sol", "rank": "director"})
    assert ag.task_review({**S, "agent": "codex-sol", "task_id": other, "verdict": "changes", "body": "un igual",
                           "expected_submission_revision": 1})["task"]["status"] == "changes"


def test_a_question_left_unanswered_above_reaches_the_person_when_asked_again(ag):
    S = {"space_id": "atlas"}
    asked_of_luis = []
    ag.escalate_hook = lambda thread, agent, question: asked_of_luis.append(agent)
    th = ag.thread_open({**S, "agent": SONNET, "title": "¿Migro ya?", "kind": "question", "body": "?"})["thread"]["id"]
    first = ag.escalate({**S, "agent": SONNET, "thread_id": th, "question": "¿Sí o no?"})
    assert first["asked"] == [OPUS] and "30 min" in first["note"]
    # asked again too soon, it still waits for those above
    assert ag.escalate({**S, "agent": SONNET, "thread_id": th, "question": "¿Sí o no?"})["asked"] == [OPUS]
    # half an hour with nobody answering, though they keep giving signs of life: asked again, it goes to the person
    ag.fake_clock.t += agora_ranks.ASK_UP_WAIT_S
    _alive(ag, OPUS)
    late = ag.escalate({**S, "agent": SONNET, "thread_id": th, "question": "¿Sí o no?"})
    assert late["reached_person"] is True and late["thread"]["status"] == "escalated" and asked_of_luis == [SONNET]
    assert "Nobody above" in late["note"]
    # an answer from anyone is an answer: the next question starts up the ladder again
    th2 = ag.thread_open({**S, "agent": SONNET, "title": "¿Y los índices?", "kind": "question", "body": "?"})["thread"]["id"]
    assert ag.escalate({**S, "agent": SONNET, "thread_id": th2, "question": "¿Los rehago?"})["asked"] == [OPUS]
    ag.post({**S, "agent": OPUS, "thread_id": th2, "kind": "comment", "body": "Espera a mañana."})
    ag.fake_clock.t += agora_ranks.ASK_UP_WAIT_S
    _alive(ag, OPUS)
    assert ag.escalate({**S, "agent": SONNET, "thread_id": th2, "question": "¿Y ahora?"})["asked"] == [OPUS]
    assert asked_of_luis == [SONNET]


def test_in_a_debate_with_findings_the_highest_rank_decides_and_is_not_closed_over(ag):
    S = {"space_id": "atlas"}
    th = ag.thread_open({**S, "agent": SONNET, "title": "¿Caché de la vista?", "kind": "debate", "body": "Hechos."})["thread"]["id"]
    did = ag.debate_open({**S, "agent": SONNET, "thread_id": th, "participants": [FABLE, OPUS, HAIKU]})["debate"]["id"]
    f1 = ag.debate_finding({**S, "agent": SONNET, "debate_id": did, "claim": "La vista se rehace en cada lectura",
                            "disposition": "adopt"})["finding"]["id"]
    # two below challenge it and the director verifies it: verified
    ag.debate_challenge({**S, "agent": HAIKU, "finding_id": f1, "note": "no lo veo"})
    ag.debate_challenge({**S, "agent": OPUS, "finding_id": f1, "note": "solo con refresh"})
    assert ag.debate_verify({**S, "agent": FABLE, "finding_id": f1})["finding"]["status"] == "verified"
    # the other way round: everyone below verifies, the director challenges: challenged, and it is not closed over
    f2 = ag.debate_finding({**S, "agent": SONNET, "debate_id": did, "claim": "Basta con 5 s de caché"})["finding"]["id"]
    ag.debate_verify({**S, "agent": HAIKU, "finding_id": f2})
    ag.debate_verify({**S, "agent": OPUS, "finding_id": f2})
    assert ag.debate_challenge({**S, "agent": FABLE, "finding_id": f2, "note": "mide antes"})["finding"]["status"] == "challenged"
    refused = _refused(ag.debate_close, {**S, "agent": SONNET, "debate_id": did, "outcome": "Caché de 5 s"})
    assert refused.extra["error_code"] == "rank" and refused.extra["outranked_by"] == FABLE and f"F{f2}" in str(refused)
    assert ag.debate_show({**S, "debate_id": did})["debate"]["status"] == "open"
    # answered and verified by them, it closes; a challenge from below never held it
    ag.debate_finding({**S, "agent": SONNET, "finding_id": f2, "claim": "Basta con 5 s de caché, medido: 40 ms"})
    ag.debate_verify({**S, "agent": FABLE, "finding_id": f2})
    ag.debate_challenge({**S, "agent": HAIKU, "finding_id": f2, "note": "sigo sin verlo"})
    assert ag.debate_close({**S, "agent": SONNET, "debate_id": did, "outcome": "Caché de 5 s"})["debate"]["status"] == "closed"

    # a finding stands as the ranks are now, not as they were when the stances were taken
    th4 = ag.thread_open({**S, "agent": SONNET, "title": "Cambian los rangos", "kind": "debate", "body": "?"})["thread"]["id"]
    d4 = ag.debate_open({**S, "agent": SONNET, "thread_id": th4, "participants": [OPUS, HAIKU]})["debate"]["id"]
    k = ag.debate_finding({**S, "agent": SONNET, "debate_id": d4, "claim": "Un hecho"})["finding"]["id"]
    ag.debate_verify({**S, "agent": OPUS, "finding_id": k})
    assert ag.debate_challenge({**S, "agent": HAIKU, "finding_id": k, "note": "no"})["finding"]["status"] == "verified"
    ag.member_rank_set({**S, "agent": "luis", "_person": True, "member": HAIKU, "rank": "director"})
    assert ag.debate_show({**S, "debate_id": d4})["findings"][0]["status"] == "challenged"
    _refused(ag.debate_close, {**S, "agent": SONNET, "debate_id": d4, "outcome": "Queda"})
    ag.member_rank_set({**S, "agent": "luis", "_person": True, "member": HAIKU, "rank": "support"})
    assert ag.debate_close({**S, "agent": SONNET, "debate_id": d4, "outcome": "Queda"})["debate"]["status"] == "closed"

    # the director closes over anyone's challenge, and so does the person; among equals nothing changed
    th2 = ag.thread_open({**S, "agent": OPUS, "title": "Otro", "kind": "debate", "body": "?"})["thread"]["id"]
    d2 = ag.debate_open({**S, "agent": OPUS, "thread_id": th2, "participants": [FABLE, SONNET]})["debate"]["id"]
    g = ag.debate_finding({**S, "agent": SONNET, "debate_id": d2, "claim": "Algo"})["finding"]["id"]
    ag.debate_challenge({**S, "agent": OPUS, "finding_id": g, "note": "no"})
    assert ag.debate_close({**S, "agent": FABLE, "debate_id": d2, "outcome": "Visto"})["debate"]["status"] == "closed"
    ag.heartbeat({**S, "agent": "codex-terra", "state": "working"})
    th3 = ag.thread_open({**S, "agent": SONNET, "title": "Iguales", "kind": "debate", "body": "?"})["thread"]["id"]
    d3 = ag.debate_open({**S, "agent": SONNET, "thread_id": th3, "participants": ["codex-terra"]})["debate"]["id"]
    h = ag.debate_finding({**S, "agent": SONNET, "debate_id": d3, "claim": "Algo más"})["finding"]["id"]
    ag.debate_challenge({**S, "agent": "codex-terra", "finding_id": h, "note": "no"})
    assert ag.debate_close({**S, "agent": SONNET, "debate_id": d3, "outcome": "Queda así"})["debate"]["status"] == "closed"


def test_a_higher_rank_hands_out_the_work(ag):
    S = {"space_id": "atlas"}
    tid = ag.task_add({**S, "agent": FABLE, "title": "Línea de tiempo"})["task"]["id"]
    assert _refused(ag.task_assign, {**S, "agent": SONNET, "task_id": tid, "to_agent": FABLE}).extra["error_code"] == "rank"
    _refused(ag.task_assign, {**S, "agent": SONNET, "task_id": tid, "to_agent": "codex-terra-2"}, status=409)   # not on the server
    _refused(ag.task_assign, {**S, "agent": FABLE, "task_id": tid, "to_agent": FABLE}, status=400)
    ag.heartbeat({**S, "agent": "codex-terra", "state": "working"})
    _refused(ag.task_assign, {**S, "agent": SONNET, "task_id": tid, "to_agent": "codex-terra"})                 # an equal

    out = ag.task_assign({**S, "agent": FABLE, "task_id": tid, "to_agent": SONNET, "note": "empieza por los hitos"})
    assert out["task"]["owner"] == SONNET and out["task"]["status"] == "claimed" and not out["noop"]
    told = ag.inbox({**S, "agent": SONNET, "peek": True})
    assert any("esta tarea es tuya" in m["body"] and "hitos" in m["body"] for m in told["messages"])
    assert ag.task_assign({**S, "agent": FABLE, "task_id": tid, "to_agent": SONNET})["noop"]

    # held by somebody the caller also outranks: it moves at once; held by somebody above the caller: it does not
    _alive(ag, HAIKU)
    moved = ag.task_assign({**S, "agent": OPUS, "task_id": tid, "to_agent": HAIKU})
    assert moved["moved_from"] == SONNET and moved["task"]["owner"] == HAIKU
    said = [m["body"] for m in ag.thread({**S, "thread_id": moved["task"]["thread_id"]})["messages"]]
    assert any(f"por rango: {OPUS} está por encima de {SONNET}" in line for line in said)        # said as what it is
    mine = ag.task_add({**S, "agent": FABLE, "title": "La del director", "claim": True})["task"]["id"]
    _refused(ag.task_assign, {**S, "agent": OPUS, "task_id": mine, "to_agent": SONNET})
    assert ag.task({**S, "task_id": mine})["task"]["owner"] == FABLE
    # the person assigns to anyone, whatever the ranks
    assert ag.task_assign({**S, "agent": "luis", "_person": True, "task_id": mine, "to_agent": OPUS})["task"]["owner"] == OPUS

    # taking work back: from above yes, from below no
    _refused(ag.task_release, {**S, "agent": HAIKU, "task_id": mine, "reason": "me la quedo"}, status=409)
    back = ag.task_release({**S, "agent": FABLE, "task_id": mine, "reason": "la replanteo"})
    assert back["task"]["status"] == "open" and back["task"]["owner"] is None
    lines = [m["body"] for m in ag.thread({**S, "thread_id": back["task"]["thread_id"]})["messages"]]
    assert f"{FABLE} le quita la tarea a {OPUS}. la replanteo" in lines

    # handing over the work of someone who is at it: at once from above, after the usual wait from below
    held = ag.task_add({**S, "agent": SONNET, "title": "En marcha", "claim": True})["task"]["id"]
    _alive(ag, SONNET, HAIKU, OPUS)
    waiting = _refused(ag.handover, {**S, "agent": HAIKU, "from_agent": SONNET, "to_agent": OPUS, "reason": "la quiero mover"}, status=409)
    assert "heartbeat" in str(waiting)
    done = ag.handover({**S, "agent": OPUS, "from_agent": SONNET, "to_agent": HAIKU, "tasks": [held], "reason": "reparto"})
    assert done["owner_moved"] == [held] and done["forced"]
    notes = [m["body"] for m in ag.thread({**S, "thread_id": ag.task({**S, "task_id": held})["task"]["thread_id"]})["messages"]]
    assert any(f"por rango: {OPUS} está por encima de {SONNET}" in n for n in notes)


def test_a_question_goes_first_to_whoever_is_above_and_around(ag):
    S = {"space_id": "atlas"}
    asked_of_luis = []
    ag.escalate_hook = lambda thread, agent, question: asked_of_luis.append((agent, question))
    th = ag.thread_open({**S, "agent": SONNET, "title": "¿Publicamos ya?", "kind": "question", "body": "Dos opciones."})["thread"]["id"]

    first = ag.escalate({**S, "agent": SONNET, "thread_id": th, "question": "¿Sí o no?"})
    assert first["asked"] == [OPUS] and first["reached_person"] is False and first["thread"]["status"] == "open"
    assert OPUS in first["note"] and asked_of_luis == []
    assert any(m["kind"] == "escalation" and m["body"] == "¿Sí o no?" for m in ag.inbox({**S, "agent": OPUS, "peek": True})["messages"])
    assert ag.inbox({**S, "agent": "luis", "peek": True, "mine_only": True})["escalated"] == []
    # the one asked answers it and closes it (its word stands), or sends it up one step at a time
    second = ag.escalate({**S, "agent": OPUS, "thread_id": th, "question": "No me toca a mí: ¿sí o no?"})
    assert second["asked"] == [FABLE] and second["reached_person"] is False and asked_of_luis == []
    third = ag.escalate({**S, "agent": FABLE, "thread_id": th, "question": "Decide tú: ¿publicamos?"})
    assert third["asked"] == ["luis"] and third["reached_person"] is True and third["thread"]["status"] == "escalated"
    assert asked_of_luis == [(FABLE, "Decide tú: ¿publicamos?")]
    waiting = ag.inbox({**S, "agent": "luis", "peek": True, "mine_only": True})["escalated"]
    assert [q["id"] for q in waiting] == [th] and waiting[0]["question"] == "Decide tú: ¿publicamos?"

    # nobody above is around: it goes to the person as it always did
    ag.fake_clock.t += 3600
    _alive(ag, HAIKU)
    th2 = ag.thread_open({**S, "agent": HAIKU, "title": "¿Borro los temporales?", "kind": "question", "body": "?"})["thread"]["id"]
    alone = ag.escalate({**S, "agent": HAIKU, "thread_id": th2, "question": "¿Los borro?"})
    assert alone["reached_person"] is True and alone["thread"]["status"] == "escalated" and asked_of_luis[-1] == (HAIKU, "¿Los borro?")
    # only the nearest rank above is asked, and only who is around
    _alive(ag, SONNET, FABLE)
    th3 = ag.thread_open({**S, "agent": HAIKU, "title": "Otra", "kind": "question", "body": "?"})["thread"]["id"]
    assert ag.escalate({**S, "agent": HAIKU, "thread_id": th3, "question": "¿?"})["asked"] == [SONNET]
    # the person asking is never routed
    th4 = ag.thread_open({**S, "agent": "luis", "_person": True, "title": "Mía", "kind": "question", "body": "?"})["thread"]["id"]
    assert ag.escalate({**S, "agent": "luis", "_person": True, "thread_id": th4, "question": "¿?"})["reached_person"] is True


def test_ranks_over_http_the_cli_and_the_tools(tmp_path):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        base = hub.config.url.rstrip("/")
        token = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        studio = {**token, "X-Hoard-Person": "faustus-studio"}
        for agent in ("claude-fable", "claude-sonnet"):
            http(base + "/api/agora/heartbeat", {"agent": agent, "state": "working"}, headers=token)

        status, out = http(base + "/api/agora/member_rank_set", {"agent": "claude-sonnet", "member": "claude-sonnet", "rank": "director"}, headers=token)
        assert status == 403                                   # an agent's token gives no rank, least of all its own
        status, out = http(base + "/api/agora/member_rank_set", {"member": "claude-fable", "rank": "director"}, headers=studio)
        assert status == 200 and out["rank"] == "director"
        status, ladder = http(base + "/api/agora/ranks", headers=token)
        assert status == 200 and ladder["members"][0] == {"id": "claude-fable", "rank": "director", "rank_level": 4}
        status, made = http(base + "/api/agora/task_add", {"agent": "claude-fable", "title": "Una tarea"}, headers=token)
        tid = made["task"]["id"]
        status, out = http(base + "/api/agora/task_assign", {"agent": "claude-sonnet", "task_id": tid, "to_agent": "claude-fable"}, headers=token)
        assert status == 403 and out["error_code"] == "rank"
        status, out = http(base + "/api/agora/task_assign", {"agent": "claude-fable", "task_id": tid, "to_agent": "claude-sonnet"}, headers=token)
        assert status == 200 and out["task"]["owner"] == "claude-sonnet"

        tools = {t["name"]: t for t in hub.facet("agora").tools()}
        assert "hub_agora_ranks" in tools and "hub_agora_task_assign" in tools
        assert not [name for name in tools if "rank_set" in name]              # no tool lets an agent change a rank

        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, HOARD_HUB_URL=base, HOARD_HUB_TOKEN_FILE=str(hub.config.token_file),
                   HOARD_HUB_DATA_DIR=str(hub.config.data_dir), HOARD_HUB_AUTOSTART="0", PYTHONPATH=str(root),
                   PYTHONIOENCODING="utf-8")

        def cli(*args):
            done = subprocess.run([sys.executable, str(root / "scripts" / "agora.py"), *args], env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert done.returncode == 0, done.stdout + done.stderr
            return done.stdout

        shown = cli("ranks")
        assert "Director (director) > Responsable (lead) > Agente (agent) > Apoyo (support)" in shown and "claude-fable" in shown
        assert "director" in cli("members")
        made = cli("--as", "claude-fable", "add", "Otra tarea")
        number = made.split("#")[1].split()[0]
        assert "es de claude-sonnet" in cli("--as", "claude-fable", "assign", number, "claude-sonnet", "--note", "para ti")
