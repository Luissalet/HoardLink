"""Spaces as servers: channels backed by threads, the server view with task threads by board column, members,
feeds, the change pulse and who may speak as the person."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.agora_channels import slugify

from ._hub_fakes import http, make_hub, serve


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


def _channel(a, slug, space="atlas", agent=None):
    args = {"space_id": space, **({"agent": agent} if agent else {})}
    return next(c for c in a.channels(args)["channels"] if c["slug"] == slug)


def test_slugify_keeps_letters_digits_and_dashes():
    assert slugify("Diseño de UI") == "diseno-de-ui"
    assert slugify("  #Ideas!! ") == "ideas"
    assert slugify("¿?") == ""


def test_general_exists_without_a_thread_and_the_first_post_opens_it(ag):
    general = _channel(ag, "general")
    assert general["builtin"] and general["thread_id"] is None and general["messages"] == 0
    assert ag.channel_read({"space_id": "atlas", "channel": "general"})["messages"] == []
    out = ag.channel_post({"space_id": "atlas", "agent": "claude", "channel": "general", "body": "hola equipo"})
    assert out["ok"] and out["channel"] == "general"
    general = _channel(ag, "general")
    assert general["thread_id"] == out["thread"]["id"] and general["messages"] == 1
    assert general["last_author"] == "claude" and general["last_text"] == "hola equipo"
    # a second post goes to the same thread
    again = ag.channel_post({"space_id": "atlas", "agent": "codex", "body": "aquí"})
    assert again["thread"]["id"] == general["thread_id"]


def test_general_of_the_default_space_is_the_work_thread_when_it_exists(tmp_path):
    a = Agora(tmp_path / "agora.db", clock=Clock(), handover_thread_id=None)
    try:
        from hoard_link.hub import agora as mod
        # thread ids start at 1; make the canonical work thread exist with its real id
        a.db.execute("INSERT INTO threads(id,title,kind,status,created_by,created,updated,space_id) VALUES(?,?,?,?,?,?,?,?)",
                     (mod.HANDOVER_THREAD_ID, "Chat de trabajo", "handoff", "open", "claude", 1, 1, "ecosystem"))
        assert _channel(a, "general", space="ecosystem")["thread_id"] == mod.HANDOVER_THREAD_ID
        out = a.channel_post({"agent": "claude", "body": "sigo"})
        assert out["thread"]["id"] == mod.HANDOVER_THREAD_ID
    finally:
        a.close()


def test_general_adopts_the_work_conversation_a_space_already_has(ag):
    th = ag.thread_open({"space_id": "atlas", "agent": "codex-console", "title": "Conversacion de trabajo",
                         "kind": "handoff", "body": "Conversacion de este espacio."})["thread"]["id"]
    assert _channel(ag, "general")["thread_id"] == th
    assert ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "x"})["thread"]["id"] == th


def test_create_update_archive_and_restore_a_channel(ag):
    made = ag.channel_create({"space_id": "atlas", "agent": "claude", "title": "Diseño de UI", "topic": "pantallas",
                              "category": "Diseño"})["channel"]
    assert made["slug"] == "diseno-de-ui" and made["category"] == "diseño" and made["thread_id"]
    with pytest.raises(AgoraError) as dup:
        ag.channel_create({"space_id": "atlas", "agent": "codex", "title": "diseno de ui"})
    assert dup.value.status == 409
    with pytest.raises(AgoraError) as reserved:
        ag.channel_create({"space_id": "atlas", "agent": "codex", "title": "Tablero"})
    assert reserved.value.status == 409
    with pytest.raises(AgoraError):
        ag.channel_create({"space_id": "atlas", "agent": "codex", "title": "¿?"})
    ag.channel_post({"space_id": "atlas", "agent": "codex", "channel": "diseno-de-ui", "body": "boceto listo"})
    changed = ag.channel_update({"space_id": "atlas", "agent": "codex", "channel": "diseno-de-ui", "topic": "v2",
                                 "title": "UI"})["channel"]
    assert changed["topic"] == "v2" and changed["title"] == "UI"
    ag.channel_update({"space_id": "atlas", "agent": "codex", "channel": "diseno-de-ui", "archived": True})
    assert all(c["slug"] != "diseno-de-ui" for c in ag.channels({"space_id": "atlas"})["channels"])
    assert any(c["slug"] == "diseno-de-ui" for c in ag.channels({"space_id": "atlas", "archived": True})["channels"])
    back = ag.channel_create({"space_id": "atlas", "agent": "claude", "title": "Diseno de UI"})["channel"]
    assert back["thread_id"] == made["thread_id"] and back["messages"] == 1     # its history comes back
    with pytest.raises(AgoraError) as locked:
        ag.channel_update({"space_id": "atlas", "agent": "codex", "channel": "general", "archived": True})
    assert locked.value.status == 409
    with pytest.raises(AgoraError) as missing:
        ag.channel_post({"space_id": "atlas", "agent": "codex", "channel": "nope", "body": "x"})
    assert missing.value.status == 404
    # channels belong to their space
    assert [c["slug"] for c in ag.channels({"space_id": "ecosystem"})["channels"]] == ["general"]


def test_mentions_in_the_text_reach_the_inbox(ag):
    ag.heartbeat({"agent": "codex", "space_id": "atlas", "doing": "tests"})
    ag.heartbeat({"agent": "cursor", "space_id": "atlas", "doing": "ui"})
    out = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "@codex mira esto, y @nadie no existe. a@b.c tampoco"})
    assert out["mentions"] == ["codex"]
    inbox = ag.inbox({"space_id": "atlas", "agent": "codex", "peek": True})
    assert inbox["counts"]["pending"] == 1 and inbox["pending"][0]["body"].startswith("@codex")
    everyone = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "@here reunión"})
    assert set(everyone["mentions"]) == {"codex", "cursor"}
    explicit = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "sin arroba", "mentions": ["cursor"]})
    assert explicit["mentions"] == ["cursor"]


def test_unread_starts_at_the_first_look_and_reading_clears_it(ag):
    ag.heartbeat({"space_id": "atlas", "agent": "codex", "doing": "leyendo"})
    ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "antes de que mires"})
    assert _channel(ag, "general", agent="codex")["unread"] == 0      # history before the first look is not «new»
    ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "nuevo @codex"})
    ag.channel_post({"space_id": "atlas", "agent": "cursor", "body": "y otro más"})
    seen = _channel(ag, "general", agent="codex")
    assert seen["unread"] == 2 and seen["mentions"] == 1
    peeked = ag.channel_read({"space_id": "atlas", "agent": "codex", "channel": "general", "peek": True})
    assert [m["body"] for m in peeked["messages"] if m["kind"] != "system"][-2:] == ["nuevo @codex", "y otro más"]
    assert peeked["seen_upto"] < peeked["messages"][-2]["id"]
    assert _channel(ag, "general", agent="codex")["unread"] == 2      # a peek marks nothing
    ag.channel_read({"space_id": "atlas", "agent": "codex", "channel": "general"})
    assert _channel(ag, "general", agent="codex")["unread"] == 0
    ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "otro"})
    assert next(s for s in ag.servers({"agent": "codex"})["servers"] if s["id"] == "atlas")["unread"] == 1
    ag.channel_post({"space_id": "atlas", "agent": "codex", "body": "contesto"})   # answering there counts as having read it
    assert _channel(ag, "general", agent="codex")["unread"] == 0
    ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "y otro"})
    ag.seen({"space_id": "atlas", "agent": "codex"})
    assert _channel(ag, "general", agent="codex")["unread"] == 0


def test_read_pages_back_and_reads_any_thread(ag):
    for i in range(7):
        ag.channel_post({"space_id": "atlas", "agent": "claude", "body": f"m{i}"})
    page = ag.channel_read({"space_id": "atlas", "channel": "general", "limit": 3})
    assert [m["body"] for m in page["messages"]] == ["m4", "m5", "m6"] and page["pagination"]["has_more"]
    older = ag.channel_read({"space_id": "atlas", "channel": "general", "limit": 3,
                             "before_id": page["pagination"]["next_before_id"]})
    assert [m["body"] for m in older["messages"]] == ["m1", "m2", "m3"]
    th = ag.thread_open({"space_id": "atlas", "agent": "codex", "title": "Debate", "body": "propuesta"})["thread"]["id"]
    ag.channel_post({"space_id": "atlas", "agent": "claude", "thread_id": th, "body": "respuesta", "kind": "agree"})
    got = ag.channel_read({"space_id": "atlas", "thread_id": th})
    assert [m["kind"] for m in got["messages"]] == ["proposal", "agree"] and got["channel"] is None


def test_guild_groups_task_threads_by_board_column_and_lists_members(ag):
    one = ag.task_add({"space_id": "atlas", "agent": "claude", "title": "Pendiente"})["task"]
    two = ag.task_add({"space_id": "atlas", "agent": "claude", "title": "En curso", "claim": True})["task"]
    three = ag.task_add({"space_id": "atlas", "agent": "codex", "title": "En revisión", "claim": True})["task"]
    ag.task_submit({"space_id": "atlas", "agent": "codex", "task_id": three["id"], "summary": "listo", "commits": ["abc1234"]})
    four = ag.task_add({"space_id": "atlas", "agent": "codex", "title": "Hecha", "kind": "docs", "claim": True})["task"]
    ag.task_done({"space_id": "atlas", "agent": "codex", "task_id": four["id"], "result": "publicada", "commits": ["def5678"]})
    ag.task_add({"space_id": "atlas", "agent": "codex", "title": "Espera", "blocked_by": [one["id"]]})
    debate = ag.thread_open({"space_id": "atlas", "agent": "codex", "title": "¿q4 o q8?", "body": "propongo q4"})["thread"]["id"]
    ag.channel_create({"space_id": "atlas", "agent": "claude", "title": "ideas"})
    ag.heartbeat({"space_id": "atlas", "agent": "codex", "doing": "revisando"})

    g = ag.guild({"space_id": "atlas", "agent": "luis"})
    assert g["space"]["id"] == "atlas" and g["viewer"] == "luis"
    assert [t["title"] for t in g["tasks"]["open"]] == ["Pendiente", "Espera"] or \
        {t["title"] for t in g["tasks"]["open"]} == {"Pendiente", "Espera"}
    assert [t["title"] for t in g["tasks"]["in_progress"]] == ["En curso"]
    assert [t["title"] for t in g["tasks"]["review"]] == ["En revisión"]
    assert [t["title"] for t in g["tasks"]["done"]] == ["Hecha"]
    assert g["column_counts"] == {"open": 2, "in_progress": 1, "review": 1, "done": 1}
    waiting = next(t for t in g["tasks"]["open"] if t["title"] == "Espera")
    assert waiting["waiting_on"] == [one["id"]] and waiting["thread_id"]
    assert [c["slug"] for c in g["channels"]] == ["general", "ideas"]
    assert [t["id"] for t in g["threads"]] == [debate]      # channel threads and task threads are not listed twice
    members = {m["id"]: m for m in g["members"]}
    assert members["codex"]["presence"] == "working" and members["codex"]["doing"] == "revisando"
    assert members["codex"]["role"] == "codex" and members["claude"]["role"] == "claude"
    assert [t["id"] for t in members["codex"]["tasks"]] == [three["id"]]
    assert [t["id"] for t in members["claude"]["tasks"]] == [two["id"]]
    assert g["views"]["reviews"]["count"] == 1 and g["views"]["board"]["counts"]["review"] == 1

    # a task changes group when its status changes
    ag.task_review({"space_id": "atlas", "agent": "claude", "task_id": three["id"], "verdict": "changes",
                    "expected_submission_revision": 1, "body": "falta un test"})
    g = ag.guild({"space_id": "atlas"})
    assert "En revisión" in [t["title"] for t in g["tasks"]["in_progress"]] and not g["tasks"]["review"]


def test_members_go_away_offline_and_out_of_quota(tmp_path):
    clock = Clock()
    a = Agora(tmp_path / "agora.db", clock=clock)
    try:
        a.heartbeat({"agent": "codex", "doing": "x"})
        a.heartbeat({"agent": "cursor", "doing": "y"})
        clock.t += 50 * 60
        a.heartbeat({"agent": "claude", "doing": "z"})
        presence = {m["id"]: m["presence"] for m in a.guild({})["members"]}
        assert presence == {"claude": "working", "codex": "away", "cursor": "away"}
        clock.t += 7 * 3600
        a.thread_open({"agent": "luis", "_person": True, "title": "hola", "body": "x"})
        presence = {m["id"]: m["presence"] for m in a.guild({})["members"]}
        assert presence["codex"] == "offline" and presence["luis"] == "person"
        # of those long gone only the latest come along; the rest are counted
        g = a.guild({"offline_limit": 2})
        gone = [m["id"] for m in g["members"] if m["presence"] == "offline"]
        assert gone == ["claude", "cursor"] and g["members_older"] == 1 and "luis" in [m["id"] for m in g["members"]]
        assert a.guild({})["members_older"] == 0
    finally:
        a.close()


def test_servers_list_spaces_with_meta_and_put_the_home_first(ag):
    ag.space_create({"agent": "claude", "id": "zeta", "title": "Zeta", "workspace": "E:/zeta", "project_id": "p1"})
    ag.task_add({"space_id": "zeta", "agent": "claude", "title": "algo", "claim": True})
    meta = ag.space_meta_set({"space_id": "zeta", "agent": "claude", "icon": "Z", "color": "#aa3355",
                              "folders": ["E:/zeta/docs", "E:/zeta/docs", " E:/notas "]})["meta"]
    assert meta == {"icon": "Z", "color": "#aa3355", "folders": ["E:/zeta/docs", "E:/notas"], "position": None, "hidden": False}
    with pytest.raises(AgoraError):
        ag.space_meta_set({"space_id": "zeta", "agent": "claude", "color": "red"})
    with pytest.raises(AgoraError):
        ag.space_meta_set({"space_id": "zeta", "agent": "claude", "folders": "E:/x"})
    listing = ag.servers({"agent": "luis"})
    assert listing["servers"][0]["id"] == "ecosystem" and listing["version"]
    zeta = next(s for s in listing["servers"] if s["id"] == "zeta")
    assert zeta["icon"] == "Z" and zeta["folders"] == ["E:/zeta/docs", "E:/notas"] and zeta["workspace"] == "E:/zeta"
    assert zeta["project_id"] == "p1" and zeta["counts"]["active"] == 1 and zeta["online"] == 1
    ag.space_meta_set({"space_id": "atlas", "agent": "claude", "position": 1})
    order = [s["id"] for s in ag.servers({})["servers"]]
    assert order[:2] == ["ecosystem", "atlas"]
    assert ag.guild({"space_id": "zeta"})["space"]["meta"]["folders"] == ["E:/zeta/docs", "E:/notas"]


def test_feed_lists_deliveries_reviews_and_activity(ag):
    t1 = ag.task_add({"space_id": "atlas", "agent": "codex", "title": "Uno", "claim": True})["task"]
    ag.task_submit({"space_id": "atlas", "agent": "codex", "task_id": t1["id"], "summary": "hecho con pruebas", "commits": ["abc1234"]})
    t2 = ag.task_add({"space_id": "atlas", "agent": "claude", "title": "Dos", "kind": "docs", "claim": True})["task"]
    ag.task_done({"space_id": "atlas", "agent": "claude", "task_id": t2["id"], "result": "documentado", "commits": ["def5678"]})
    ag.task_add({"space_id": "atlas", "agent": "claude", "title": "Sin entregar"})
    feed = ag.feed({"space_id": "atlas"})
    assert [i["title"] for i in feed["items"]] == ["Dos", "Uno"]
    assert feed["items"][0]["summary"] == "documentado" and feed["items"][0]["commits"] == ["def5678"]
    assert feed["items"][1]["commits"] == ["abc1234"] and "hecho con pruebas" in feed["items"][1]["summary"]
    assert [i["title"] for i in ag.feed({"space_id": "atlas", "kind": "reviews"})["items"]] == ["Uno"]
    paged = ag.feed({"space_id": "atlas", "limit": 1})
    assert [i["title"] for i in paged["items"]] == ["Dos"] and paged["next_before"]
    rest = ag.feed({"space_id": "atlas", "limit": 1, "before": paged["next_before"]})
    assert [i["title"] for i in rest["items"]] == ["Uno"]
    activity = ag.feed({"space_id": "atlas", "kind": "activity", "limit": 3})
    assert len(activity["items"]) == 3 and activity["items"][0]["id"] > activity["items"][1]["id"]
    assert all(i["thread_title"] for i in activity["items"])
    with pytest.raises(AgoraError):
        ag.feed({"space_id": "atlas", "kind": "nope"})


def test_search_finds_messages_tasks_and_threads(ag):
    task = ag.task_add({"space_id": "atlas", "agent": "claude", "title": "Importar casos desde CSV"})["task"]
    ag.channel_post({"space_id": "atlas", "agent": "codex", "body": "El importador de CSV falla con comillas dobles en la tercera columna."})
    ag.channel_post({"agent": "codex", "body": "En ecosystem también hablamos de CSV."})
    th = ag.thread_open({"space_id": "atlas", "agent": "codex", "title": "¿CSV o Excel?", "body": "propongo los dos"})["thread"]["id"]
    found = ag.search({"space_id": "atlas", "q": "csv"})
    assert [t["id"] for t in found["tasks"]] == [task["id"]] and found["tasks"][0]["column"] == "open"
    assert [t["id"] for t in found["threads"]] == [th]
    assert any("importador de CSV" in m["snippet"] for m in found["messages"])   # the task's own text matches too
    assert {m["space_id"] for m in found["messages"]} == {"atlas"} and "body" not in found["messages"][0]
    assert found["messages"][0]["id"] > found["messages"][-1]["id"]              # newest first
    everywhere = ag.search({"q": "CSV", "all_spaces": True})
    assert {m["space_id"] for m in everywhere["messages"]} == {"atlas", "ecosystem"}
    assert [t["id"] for t in ag.search({"space_id": "atlas", "q": "#%d" % task["id"]})["tasks"]] == [task["id"]]
    assert ag.search({"space_id": "atlas", "q": "100%"})["messages"] == []      # % is text, not a wildcard
    with pytest.raises(AgoraError):
        ag.search({"q": "a"})


def test_pulse_answers_when_something_changes(tmp_path):
    a = Agora(tmp_path / "agora.db")
    try:
        first = a.pulse({})
        assert first["version"] and first["changed"]
        same = a.pulse({"since": first["version"]})
        assert not same["changed"] and same["version"] == first["version"]
        timer = threading.Timer(0.3, lambda: a.channel_post({"agent": "claude", "body": "despierta"}))
        timer.start()
        started = time.monotonic()
        woke = a.pulse({"since": first["version"], "wait_s": 10})
        timer.join()
        assert woke["changed"] and woke["version"] != first["version"] and time.monotonic() - started < 8
    finally:
        a.close()


def test_only_the_persons_pages_speak_as_the_person(tmp_path):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        base = hub.config.url.rstrip("/")
        token = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        studio = {**token, "X-Hoard-Person": "faustus-studio"}

        status, out = http(base + "/api/agora/channel_post", {"agent": "luis", "body": "soy yo"}, headers=token)
        assert status == 403                                   # an agent's token alone never writes as the person
        status, out = http(base + "/api/agora/channel_post", {"body": "soy yo de verdad"}, headers=studio)
        assert status == 200 and out["thread"]["id"]
        http(base + "/api/agora/channels?agent=luis", headers=token)      # the person's first look: «new» counts from here
        status, page = http(base + "/api/agora/channels/general", headers=token)
        assert status == 200 and page["messages"][-1]["author"] == "luis" and page["messages"][-1]["body"] == "soy yo de verdad"
        status, out = http(base + "/api/agora/channel_post", {"agent": "claude", "body": "@luis visto"}, headers=token)
        assert status == 200 and out["mentions"] == ["luis"]

        # reading as the person marks it seen only from the person's pages
        status, listed = http(base + "/api/agora/channels?agent=luis", headers=token)
        assert next(c for c in listed["channels"] if c["slug"] == "general")["unread"] == 1
        http(base + "/api/agora/channels/general?agent=luis", headers=token)
        status, listed = http(base + "/api/agora/channels?agent=luis", headers=token)
        assert next(c for c in listed["channels"] if c["slug"] == "general")["unread"] == 1
        http(base + "/api/agora/channels/general?agent=luis", headers=studio)
        status, listed = http(base + "/api/agora/channels?agent=luis", headers=token)
        assert next(c for c in listed["channels"] if c["slug"] == "general")["unread"] == 0

        for route in ("servers?agent=luis", "guild", "feed?kind=activity&limit=5", "pulse", "search?q=visto&all_spaces=1"):
            status, out = http(base + "/api/agora/" + route, headers=token)
            assert status == 200 and out["ok"], route
        status, out = http(base + "/api/agora/guild?space_id=nope", headers=token)
        assert status == 404


def test_only_the_persons_pages_read_and_mark_as_the_person(tmp_path):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        base = hub.config.url.rstrip("/")
        token = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        studio = {**token, "X-Hoard-Person": "faustus-studio"}

        def unread():
            _, listed = http(base + "/api/agora/channels?agent=luis", headers=token)
            general = next(c for c in listed["channels"] if c["slug"] == "general")
            return general["unread"], general["mentions"]

        http(base + "/api/agora/channel_post", {"agent": "claude", "body": "abro el canal"}, headers=token)
        http(base + "/api/agora/channels", headers=studio)              # the person's first look
        http(base + "/api/agora/channel_post", {"agent": "claude", "body": "ping @luis"}, headers=token)
        assert unread() == (1, 1)
        # an agent's token, whatever it asks for, reads as the person without marking anything
        status, page = http(base + "/api/agora/channel_read", {"agent": "luis", "channel": "general"}, headers=token)
        assert status == 200 and page["messages"][-1]["body"] == "ping @luis" and unread() == (1, 1)
        status, page = http(base + "/api/agora/channel_read", {"agent": "luis", "channel": "general", "_person": True}, headers=token)
        assert status == 200 and unread() == (1, 1)
        status, out = http(base + "/api/agora/seen", {"agent": "luis"}, headers=token)
        assert status == 403 and unread() == (1, 1)
        status, _ = http(base + "/api/agora/channels/general?agent=luis&_person=1", headers=token)
        assert status == 200 and unread() == (1, 1)
        # the person's page does mark it
        status, _ = http(base + "/api/agora/channel_read", {"channel": "general"}, headers=studio)
        assert status == 200 and unread() == (0, 0)
        http(base + "/api/agora/channel_post", {"agent": "claude", "body": "otro @luis"}, headers=token)
        assert unread() == (1, 1)
        status, _ = http(base + "/api/agora/seen", {}, headers=studio)
        assert status == 200 and unread() == (0, 0)
        # a header without a token grants nothing, and neither does an unknown page
        status, _ = http(base + "/api/agora/channel_post", {"body": "x"}, headers={"X-Hoard-Person": "faustus-studio"})
        assert status == 401
        status, _ = http(base + "/api/agora/channel_post", {"agent": "luis", "body": "x"}, headers={**token, "X-Hoard-Person": "otra"})
        assert status == 403
        # the folders of this machine need a token
        status, _ = http(base + "/api/agora/folders?path=")
        assert status == 401


def test_archived_channels_take_no_posts_and_come_back_as_they_were(ag):
    ag.channel_create({"space_id": "atlas", "agent": "claude", "title": "Diseño", "topic": "pantallas", "category": "ui"})
    ag.channel_post({"space_id": "atlas", "agent": "codex", "channel": "diseno", "body": "boceto"})
    with pytest.raises(AgoraError) as odd:
        ag.channel_update({"space_id": "atlas", "agent": "codex", "channel": "diseno", "archived": "quizá"})
    assert odd.value.status == 400
    ag.channel_update({"space_id": "atlas", "agent": "codex", "channel": "diseno", "archived": "false"})
    assert any(c["slug"] == "diseno" for c in ag.channels({"space_id": "atlas"})["channels"])     # "false" is not a yes
    ag.channel_update({"space_id": "atlas", "agent": "codex", "channel": "diseno", "archived": True})
    with pytest.raises(AgoraError) as closed:
        ag.channel_post({"space_id": "atlas", "agent": "codex", "channel": "diseno", "body": "tarde"})
    assert closed.value.status == 409 and closed.value.extra.get("error_code") == "channel_archived"
    back = ag.channel_create({"space_id": "atlas", "agent": "codex", "slug": "diseno"})["channel"]
    assert (back["title"], back["topic"], back["category"], back["messages"]) == ("Diseño", "pantallas", "ui", 1)
    ag.channel_update({"space_id": "atlas", "agent": "codex", "channel": "diseno", "archived": True})
    renamed = ag.channel_create({"space_id": "atlas", "agent": "codex", "title": "Diseno", "topic": "v2"})["channel"]
    assert (renamed["title"], renamed["topic"], renamed["category"]) == ("Diseno", "v2", "ui")


def test_meta_takes_absolute_folders_and_real_booleans(ag):
    good = ["E:\\Development\\IA", "/srv/datos", "~/notas", "\\\\nas\\compartida"]
    meta = ag.space_meta_set({"space_id": "atlas", "agent": "claude", "folders": good + [good[0]], "hidden": "false"})["meta"]
    assert meta["folders"] == good and meta["hidden"] is False
    for bad in (["relativa/../x"], ["E:\\uno\ndos"], [""], [7]):
        with pytest.raises(AgoraError):
            ag.space_meta_set({"space_id": "atlas", "agent": "claude", "folders": bad})
    with pytest.raises(AgoraError):
        ag.space_meta_set({"space_id": "atlas", "agent": "claude", "hidden": "a veces"})
    assert ag.space_meta_set({"space_id": "atlas", "agent": "claude", "hidden": 1})["meta"]["hidden"] is True


def test_feed_pages_do_not_skip_deliveries_of_the_same_instant(ag):
    ids = []
    for title in ("Uno", "Dos", "Tres"):
        task = ag.task_add({"space_id": "atlas", "agent": "codex", "title": title, "claim": True})["task"]
        ag.task_done({"space_id": "atlas", "agent": "codex", "task_id": task["id"], "result": "ok"})
        ids.append(task["id"])
    ag.db.execute("UPDATE tasks SET closed_at=1800000500.0, submitted_at=NULL WHERE space_id='atlas'")
    seen, cursor = [], None
    for _ in range(5):
        page = ag.feed({"space_id": "atlas", "limit": 1, **({"before": cursor} if cursor else {})})
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_before"]
        if not cursor:
            break
    assert seen == sorted(ids, reverse=True)
    assert [i["id"] for i in ag.feed({"space_id": "atlas", "before": 1800000500.0})["items"]] == []   # a bare number still works
    assert ag.feed({"space_id": "atlas", "kind": "activity", "before": 0})["items"] == []          # zero is a cursor, not «none»
    with pytest.raises(AgoraError):
        ag.feed({"space_id": "atlas", "before": "ayer"})


def test_mentions_ignore_case_and_unknown_readers_leave_no_trace(ag):
    ag.heartbeat({"agent": "codex", "space_id": "atlas", "doing": "x"})
    out = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "@Codex y @LUIS, mirad"})
    assert out["mentions"] == ["codex", "luis"]
    ag.channels({"space_id": "atlas", "agent": "nadie-123"})
    ag.guild({"space_id": "atlas", "agent": "otro-que-no-existe"})
    rows = [r["agent"] for r in ag.db.query("SELECT agent FROM agora_seen_floor")]
    assert "nadie-123" not in rows and "otro-que-no-existe" not in rows
    with pytest.raises(AgoraError) as unknown:
        ag.seen({"space_id": "atlas", "agent": "nadie-123"})
    assert unknown.value.status == 404


def test_folders_lists_this_machine_one_level_at_a_time(tmp_path):
    from urllib.parse import quote

    from hoard_link.hub.agora_channels import folders

    (tmp_path / "work" / "repo").mkdir(parents=True)
    (tmp_path / "work" / ".git").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    top = folders({})
    assert top["ok"] and top["path"] == "" and top["parent"] is None and top["folders"] and top["home"]
    here = folders({"path": str(tmp_path)})
    assert [f["name"] for f in here["folders"]] == ["Alpha", "work"]              # folders only, sorted, no files
    assert here["parent"] == str(tmp_path.parent)
    inside = folders({"path": str(tmp_path / "work")})
    assert [f["name"] for f in inside["folders"]] == ["repo"]                     # hidden folders are left out
    assert inside["parent"] == str(tmp_path) and inside["folders"][0]["path"] == str(tmp_path / "work" / "repo")
    with pytest.raises(AgoraError) as missing:
        folders({"path": str(tmp_path / "nope")})
    assert missing.value.status == 404
    root = folders({"path": top["folders"][0]["path"]})
    assert root["parent"] == ""                                                   # above a drive there are only drives

    with ExitStack() as cleanup:
        hub = make_hub(tmp_path / "hub", [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        token = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        status, out = http(hub.config.url.rstrip("/") + "/api/agora/folders?path=" + quote(str(tmp_path)), headers=token)
        assert status == 200 and "work" in [f["name"] for f in out["folders"]]
        status, out = http(hub.config.url.rstrip("/") + "/api/agora/folders?path=" + quote(str(tmp_path / "nope")), headers=token)
        assert status == 404 and out["error_code"] == "folder_not_found"
        tools = {t["name"] for t in hub.facet("agora").tools()}
        assert "hub_agora_folders" in tools


def test_cli_and_mcp_speak_channels(tmp_path):
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

        def cli(*args):
            done = subprocess.run([sys.executable, str(root / "scripts" / "agora.py"), *args], env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert done.returncode == 0, done.stdout + done.stderr
            return done.stdout

        assert "#ideas" in cli("--as", "builder", "channel", "ideas", "--topic", "qué construir")
        assert "mensaje" in cli("--as", "builder", "say", "ideas", "una idea para @reviewer")
        assert "ok" in cli("--as", "reviewer", "say", "hola a todos")              # general by default
        listing = cli("--as", "reviewer", "channels")
        assert "#general" in listing and "#ideas" in listing and "1 sin leer" not in listing.split("#ideas")[0]
        read = cli("--as", "reviewer", "read", "ideas")
        assert "una idea para @reviewer" in read and "builder" in read
        assert "ecosystem" in cli("--as", "reviewer", "servers")
        assert "archivado" in cli("--as", "builder", "channel", "ideas", "--archive")
        assert "#ideas" not in cli("--as", "reviewer", "channels")
        cli("feed", "--kind", "activity")
        assert "una idea para @reviewer" in cli("search", "idea para")

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "hub_agora_channel_post", "arguments": {"agent": "codex", "body": "desde MCP"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_agora_guild", "arguments": {"agent": "codex"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "hub_agora_channel_post", "arguments": {"agent": "luis", "body": "suplantación"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "hoard_link.hub.mcp"], cwd=root, env=env,
                                input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
        assert not replies[2]["result"].get("isError"), replies[2]
        guild = json.loads(replies[3]["result"]["content"][0]["text"])
        assert guild["channels"][0]["slug"] == "general" and guild["channels"][0]["last_text"] == "desde MCP"
        assert replies[4]["result"].get("isError") or json.loads(replies[4]["result"]["content"][0]["text"]).get("ok") is False
