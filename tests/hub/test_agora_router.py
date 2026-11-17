"""The Ágora router: who answers what the person writes without naming anyone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from hoard_link.hub import agora_router
from hoard_link.hub.agora import Agora, AgoraError

from ._hub_fakes import make_hub, serve


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def ag(tmp_path):
    a = Agora(tmp_path / "agora.db", clock=Clock())
    a.space_create({"agent": "claude", "id": "atlas", "title": "Atlas"})
    yield a
    a.close()


S = {"space_id": "atlas"}


def _around(a, *names):
    for n in names:
        a.heartbeat({**S, "agent": n, "state": "working", "doing": f"trabajando como {n}"})


def _task(a, owner, title, **extra):
    t = a.task_add({**S, "agent": owner, "title": title, "kind": "feature", **extra})["task"]
    a.task_claim({**S, "agent": owner, "task_id": t["id"]})
    return a.task_show({**S, "task_id": t["id"]})["task"] if hasattr(a, "task_show") else t


def _say(a, body, *, thread=None, agent="luis", person=True, **extra):
    args = {**S, "agent": agent, "body": body, **extra}
    if person:
        args["_person"] = True
    if thread:
        args["thread_id"] = thread
    return a.channel_post(args)


def _picked(out):
    return [p["agent"] for p in (out.get("route") or {}).get("picks", [])]


class FakeModel:
    """A stand-in for the family's model: answers with what it is given, and records what it was asked."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, body):
        self.calls.append(body)
        if isinstance(self.answer, Exception):
            raise self.answer
        return 200, self.answer


def test_a_message_in_a_task_thread_goes_to_the_owner_of_the_task(ag):
    _around(ag, "claude-a", "codex-b")
    task = _task(ag, "claude-a", "Rehacer la pantalla de ajustes")
    out = _say(ag, "¿cómo va esto?", thread=task["thread_id"])
    assert _picked(out) == ["claude-a"]
    assert "claude-a" in out["mentions"]
    assert out["route"]["label"].startswith("Lo atiende @claude-a")
    assert "enrutador" in out["route"]["label"]
    # it reaches the inbox of the chosen one as a mention, and not the other's
    def mine(agent):
        return [m for m in ag.inbox({**S, "agent": agent})["pending"] if m["author"] == "luis"]
    assert [m["body"] for m in mine("claude-a")] == ["¿cómo va esto?"] and mine("codex-b") == []
    # the message read back carries who was chosen and why
    page = ag.channel_read({**S, "thread_id": task["thread_id"]})
    mine = next(m for m in page["messages"] if m["id"] == out["message_id"])
    assert mine["route"]["picks"][0]["agent"] == "claude-a" and "tarea" in mine["route"]["picks"][0]["why"]
    assert mine["route"]["engine"] == "rules" and "ranking" not in mine["route"]


def test_naming_someone_or_an_agent_speaking_is_not_routed(ag):
    _around(ag, "claude-a", "codex-b")
    named = _say(ag, "@codex-b mira esto de ajustes")
    assert "route" not in named
    quiet = _say(ag, "ajustes de la pantalla", agent="claude-a", person=False)
    assert "route" not in quiet
    assert ag.routes(S)["routes"] == []


def test_the_router_picks_who_works_on_the_subject_and_at_most_the_maximum(ag):
    _around(ag, "claude-a", "codex-b", "cursor-c", "codex-d", "claude-e")
    for owner, title in (("claude-a", "Pantalla de ajustes del copiloto"), ("codex-b", "Ajustes del copiloto en móvil"),
                         ("cursor-c", "Tema oscuro de ajustes del copiloto"), ("codex-d", "Ajustes del copiloto: tests"),
                         ("claude-e", "Informe trimestral de ventas")):
        _task(ag, owner, title)
    out = _say(ag, "los ajustes del copiloto se ven mal")
    picks = _picked(out)
    assert 1 <= len(picks) <= 3 and "claude-e" not in picks
    ag.route_settings_set({**S, "agent": "claude", "max_agents": 1})
    assert len(_picked(_say(ag, "los ajustes del copiloto se ven mal otra vez"))) == 1
    with pytest.raises(AgoraError):
        ag.route_settings_set({**S, "agent": "claude", "max_agents": 9})


def test_locks_and_the_last_to_speak_count(ag):
    _around(ag, "claude-a", "codex-b", "cursor-c")
    t = _task(ag, "claude-a", "Otra cosa distinta")
    ag.lock({**S, "agent": "codex-b", "resource": "path:Atlas/src/exportador", "task_id": None})
    found = agora_router.candidates(ag.for_space("atlas"), "el exportador falla", None, "luis")
    assert found[0]["agent"] == "codex-b" and any("bloqueado" in r for r in found[0]["reasons"])
    # the last to talk in a thread is a candidate there
    _say(ag, "empiezo con la revisión", agent="cursor-c", person=False, thread=t["thread_id"])
    again = {c["agent"]: c for c in agora_router.candidates(ag.for_space("atlas"), "sigue?", t["thread_id"], "luis")}
    assert "fue el último en hablar" in " ".join(again["cursor-c"]["reasons"])
    assert again["claude-a"]["score"] > again["cursor-c"]["score"]


def test_agents_without_quota_or_long_gone_are_not_chosen(ag):
    _around(ag, "claude-a", "codex-b")
    ag.member_add({**S, "agent": "claude", "id": "codex-invited"})
    task = _task(ag, "codex-b", "Ajustes")
    # the owner stops being around (offline): an offline owner of the task still gets it, the invited one never
    ag.db.execute("UPDATE agents SET last_seen=? WHERE id='claude-a'", (ag.clock() - 7 * 3600,))
    found = [c["agent"] for c in agora_router.candidates(ag.for_space("atlas"), "ajustes", task["thread_id"], "luis")]
    assert "claude-a" not in found and "codex-invited" not in found and "codex-b" in found


def test_a_name_or_a_task_number_in_the_text_decides(ag):
    _around(ag, "claude-a", "codex-b")
    t = _task(ag, "codex-b", "Algo")
    by_name = agora_router.candidates(ag.for_space("atlas"), "claude-a, ¿puedes mirar el menú?", None, "luis")
    assert by_name[0]["agent"] == "claude-a" and "lo nombras" in by_name[0]["reasons"]
    by_task = agora_router.candidates(ag.for_space("atlas"), f"qué pasa con la #{t['id']}", None, "luis")
    assert by_task[0]["agent"] == "codex-b"


def test_the_model_chooses_among_the_candidates_and_cannot_invent_one(ag):
    _around(ag, "claude-a", "codex-b", "cursor-c")
    for owner in ("claude-a", "codex-b", "cursor-c"):
        _task(ag, owner, "Pantalla de ajustes del copiloto")
    model = FakeModel({"ok": True, "json": {"picks": [{"agent": "cursor-c", "why": "lleva el tema oscuro"},
                                                     {"agent": "ghost-x", "why": "no existe"}]}})
    ag.route_chat = model
    out = _say(ag, "los ajustes del copiloto se ven mal")
    assert _picked(out) == ["cursor-c"] and out["route"]["engine"] == "model"
    assert "modelo" in out["route"]["label"] and model.calls and model.calls[0]["timeout_s"] <= 30
    assert "cursor-c" in model.calls[0]["messages"][1]["content"]


def test_the_model_may_say_nobody_and_the_message_names_no_one(ag):
    _around(ag, "claude-a", "codex-b")
    for owner in ("claude-a", "codex-b"):
        _task(ag, owner, "Pantalla de ajustes del copiloto")
    ag.route_chat = FakeModel({"ok": True, "json": {"picks": [], "nobody": True}})
    out = _say(ag, "gracias por lo de los ajustes del copiloto")
    assert _picked(out) == [] and out["route"]["label"].startswith("Nadie tiene que contestar")
    assert out["mentions"] == []


def test_without_a_usable_model_the_rules_decide_and_the_note_says_so(ag):
    _around(ag, "claude-a", "codex-b")
    for owner in ("claude-a", "codex-b"):
        _task(ag, owner, "Pantalla de ajustes del copiloto")
    for broken in (RuntimeError("down"), (503, {"ok": False, "error": "no_model"}), (200, {"ok": True, "json": "x"})):
        ag.route_chat = (lambda b, e=broken: (_ for _ in ()).throw(e)) if isinstance(broken, Exception) else (lambda b, r=broken: r)
        out = _say(ag, "los ajustes del copiloto se ven mal")
        assert _picked(out) and out["route"]["engine"] == "rules"
        assert "modelo no contestó" in out["route"]["note"]
    ag.route_chat = None
    assert _picked(_say(ag, "los ajustes del copiloto, otra vez"))


def test_the_rules_setting_never_asks_the_model(ag):
    _around(ag, "claude-a", "codex-b")
    for owner in ("claude-a", "codex-b"):
        _task(ag, owner, "Pantalla de ajustes del copiloto")
    model = FakeModel({"ok": True, "json": {"picks": [{"agent": "codex-b"}]}})
    ag.route_chat = model
    ag.route_settings_set({**S, "agent": "claude", "model": "rules"})
    assert _picked(_say(ag, "los ajustes del copiloto se ven mal")) and model.calls == []
    with pytest.raises(AgoraError):
        ag.route_settings_set({**S, "agent": "claude", "model": "gpt-9"})


def test_turned_off_the_router_does_nothing_and_the_setting_is_per_space(ag):
    ag.space_create({"agent": "claude", "id": "cicero", "title": "Cicero"})
    _around(ag, "claude-a")
    _task(ag, "claude-a", "Pantalla de ajustes")
    assert ag.route_settings(S)["settings"] == {**agora_router._defaults("atlas")}
    ag.route_settings_set({**S, "agent": "claude", "enabled": False})
    assert ag.route_settings(S)["settings"]["enabled"] is False
    assert "route" not in _say(ag, "la pantalla de ajustes")
    assert ag.route_settings({"space_id": "cicero"})["settings"]["enabled"] is True
    ag.route_settings_set({**S, "agent": "claude", "enabled": True})
    assert "route" in _say(ag, "la pantalla de ajustes")
    with pytest.raises(AgoraError):
        ag.route_settings_set({**S, "agent": "claude", "enabled": "quizá"})


def test_an_agent_can_pass_and_the_router_tries_the_next_one(ag):
    _around(ag, "claude-a", "codex-b", "cursor-c")
    for owner in ("claude-a", "codex-b", "cursor-c"):
        _task(ag, owner, "Pantalla de ajustes del copiloto")
    ag.route_settings_set({**S, "agent": "claude", "max_agents": 1})
    out = _say(ag, "los ajustes del copiloto se ven mal")
    first = _picked(out)[0]
    other = next(x for x in ("claude-a", "codex-b", "cursor-c") if x != first)
    with pytest.raises(AgoraError) as err:
        ag.route_pass({**S, "agent": other, "message_id": out["message_id"]})
    assert err.value.status == 409
    done = ag.route_pass({**S, "agent": first, "message_id": out["message_id"], "reason": "no es mi tema"})
    assert done["passed"] == first and done["next"] and done["next"] != first
    route = done["route"]
    assert [p["status"] for p in route["picks"]] == ["passed", "assigned"]
    assert route["label"].startswith(f"Lo atiende @{done['next']}")
    # the one who passed no longer has the mention; the next one does, and is told in the thread
    row = ag.db.one("SELECT mentions FROM messages WHERE id=?", (out["message_id"],))
    assert first not in json.loads(row["mentions"]) and done["next"] in json.loads(row["mentions"])
    page = ag.channel_read({**S, "channel": "general"})["messages"]
    assert any(m["kind"] == "system" and f"@{done['next']}" in m["body"] and "no atiende" in m["body"] for m in page)
    # passing until nobody is left
    last = done["next"]
    while last:
        step = ag.route_pass({**S, "agent": last, "message_id": out["message_id"]})
        last = step["next"]
    assert step["route"]["label"].startswith("Nadie lo atiende")


def test_route_simulates_writes_nothing_and_applies_to_an_existing_message(ag):
    _around(ag, "claude-a", "codex-b")
    task = _task(ag, "claude-a", "Ajustes del copiloto")
    sim = ag.route({**S, "agent": "codex-b", "text": "mira los ajustes del copiloto", "thread_id": task["thread_id"]})
    assert sim["picks"][0]["agent"] == "claude-a" and sim["applied"] is False and sim["candidates"]
    assert ag.routes(S)["routes"] == []
    ag.route_settings_set({**S, "agent": "claude", "enabled": False})
    plain = _say(ag, "los ajustes del copiloto", thread=task["thread_id"])
    assert "route" not in plain
    with pytest.raises(AgoraError):
        ag.route({**S, "agent": "codex-b", "message_id": plain["message_id"], "apply": False, "text": ""} | {"message_id": 999})
    applied = ag.route({**S, "agent": "codex-b", "message_id": plain["message_id"], "apply": True})
    assert applied["applied"] and applied["route"]["picks"][0]["agent"] == "claude-a"
    assert "claude-a" in applied["mentions"]
    assert [r["message_id"] for r in ag.routes(S)["routes"]] == [plain["message_id"]]
    assert ag.routes({**S, "for_agent": "codex-b"})["routes"] == []
    with pytest.raises(AgoraError):
        ag.route({**S, "agent": "codex-b", "text": "algo", "apply": True})


def test_the_threads_post_also_routes_and_nobody_around_means_nobody(ag):
    thread = ag.thread_open({**S, "agent": "luis", "_person": True, "title": "Debate", "body": "abro"})["thread"]
    out = ag.post({**S, "agent": "luis", "_person": True, "thread_id": thread["id"], "body": "¿qué opináis?"})
    assert out["route"]["picks"] == [] and "route" in out
    assert out["route"]["label"].startswith("Nadie")


def test_a_thread_the_person_opens_without_naming_anyone_is_routed(ag):
    _around(ag, "claude-a", "codex-b")
    _task(ag, "claude-a", "Pantalla de ajustes del copiloto")
    out = ag.thread_open({**S, "agent": "luis", "_person": True, "title": "Ajustes del copiloto",
                          "body": "Hay que repasar la pantalla de ajustes del copiloto", "kind": "question"})
    assert [p["agent"] for p in out["route"]["picks"]] == ["claude-a"]
    first = ag.thread({**S, "thread_id": out["thread"]["id"]})["messages"][0]
    assert first["mentions"] == ["claude-a"] and first["route"]["label"].startswith("Lo atiende @claude-a")


def test_hub_exposes_the_router_as_tools_and_routes(tmp_path):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        base = hub.config.url.rstrip("/")
        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, HOARD_HUB_URL=base, HOARD_HUB_TOKEN_FILE=str(hub.config.token_file),
                   HOARD_HUB_DATA_DIR=str(hub.config.data_dir), HOARD_HUB_AUTOSTART="0", PYTHONPATH=str(root),
                   PYTHONIOENCODING="utf-8")
        env.pop("AGORA_SPACE", None)
        calls = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "hub_agora_heartbeat", "arguments": {"agent": "claude-a", "state": "working", "doing": "ajustes del copiloto"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_agora_route", "arguments": {"agent": "codex-b", "text": "los ajustes del copiloto se ven mal"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "hub_agora_route_settings_set", "arguments": {"agent": "codex-b", "max_agents": 2, "model": "rules"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "hub_agora_route_settings", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
                "name": "hub_agora_routes", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
                "name": "hub_agora_route_pass", "arguments": {"agent": "codex-b", "message_id": 1}}},
        ]
        result = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                input="\n".join(json.dumps(r) for r in calls) + "\n", capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr
        replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
        listed = {t["name"] for t in replies[1]["result"]["tools"]}
        assert {"hub_agora_route", "hub_agora_route_pass", "hub_agora_routes", "hub_agora_route_settings",
                "hub_agora_route_settings_set"} <= listed
        body = lambda i: json.loads(replies[i]["result"]["content"][0]["text"])  # noqa: E731
        assert body(3)["picks"][0]["agent"] == "claude-a" and body(3)["applied"] is False
        assert body(4)["settings"]["max_agents"] == 2 and body(5)["settings"]["model"] == "rules"
        assert body(6)["routes"] == []
        assert replies[7]["result"].get("isError")
