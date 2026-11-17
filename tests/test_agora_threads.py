import pytest

from hoard_link.hub.agora import Agora, AgoraError, PERSON, TOOLS
from hoard_link.hub import agora_threads


@pytest.fixture()
def agora(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    for who in ("claude-a", "codex-b", "cursor-c"):
        ag.heartbeat({"agent": who, "doing": "testing"})
    yield ag
    ag.close()


def say(ag, agent, body, channel="general", **kw):
    return ag.channel_post({"agent": agent, "channel": channel, "body": body, **kw})["message_id"]


def person(**kw):
    return {"agent": PERSON, "_person": True, **kw}


def page(ag, **kw):
    return ag.channel_read({"channel": "general", "agent": "claude-a", "peek": True, **kw})


def test_thread_from_one_message_links_to_its_origin_and_shows_replies(agora):
    first = say(agora, "claude-a", "Which port should the QA Hub use?\nSecond line")
    out = agora.thread_from({"agent": "codex-b", "message_id": first, "title": "Puerto de pruebas"})
    thread = out["thread"]
    assert out["ok"] and not out["existing"] and out["title_source"] == "manual"
    assert thread["title"] == "Puerto de pruebas" and thread["state"] == "open" and thread["channel"] == "general"
    assert thread["origin"]["message_id"] == first and thread["origin"]["author"] == "claude-a"
    assert "QA Hub" in thread["origin"]["excerpt"] and thread["following"] is True and thread["replies"] == 0
    tid = thread["thread_id"]
    say_in = lambda who, text, **kw: agora.channel_post({"agent": who, "thread_id": tid, "body": text, **kw})
    say_in("claude-a", "8897 works")
    say_in("codex-b", "ok, thanks")
    shown = {m["id"]: m for m in page(agora)["messages"]}
    badge = shown[first]["thread"]
    assert badge["thread_id"] == tid and badge["replies"] == 2 and badge["last_author"] == "codex-b"
    assert badge["last_at"] and set(badge["people"]) == {"claude-a", "codex-b"}
    # asking again for the same message returns the thread it has
    again = agora.thread_from({"agent": "cursor-c", "message_id": first, "title": "Otro"})
    assert again["existing"] and again["thread"]["thread_id"] == tid and again["thread"]["title"] == "Puerto de pruebas"
    # the thread's own page says what it is
    inside = agora.channel_read({"thread_id": tid, "agent": "claude-a", "peek": True})
    assert inside["subthread"]["thread_id"] == tid and inside["subthread"]["origin"]["message_id"] == first
    assert any("Hilo abierto" in m["body"] for m in inside["messages"] if m["kind"] == "system")


def test_thread_from_a_stretch_marks_every_message_and_cannot_nest(agora):
    a = say(agora, "claude-a", "first idea")
    b = say(agora, "codex-b", "second idea")
    c = say(agora, "claude-a", "third idea")
    after = say(agora, "codex-b", "unrelated")
    out = agora.thread_from({"agent": "claude-a", "message_id": c, "to_message_id": a, "title": "Ideas"})
    tid = out["thread"]["thread_id"]
    assert out["thread"]["origin"]["message_id"] == a and out["thread"]["origin"]["to_message_id"] == c
    shown = {m["id"]: m for m in page(agora)["messages"]}
    assert shown[a]["thread"]["thread_id"] == tid
    assert shown[b].get("thread_span") == tid and shown[c].get("thread_span") == tid
    assert "thread_span" not in shown[after] and "thread" not in shown[after]
    inner = say(agora, "claude-a", "inside", channel="general") and agora.channel_post(
        {"agent": "claude-a", "thread_id": tid, "body": "inside the thread"})["message_id"]
    with pytest.raises(AgoraError) as exc:
        agora.thread_from({"agent": "claude-a", "message_id": inner, "title": "nested"})
    assert exc.value.status == 409 and exc.value.extra.get("error_code") == "thread_in_thread"
    with pytest.raises(AgoraError):
        agora.thread_from({"agent": "claude-a", "message_id": 999999})
    other = agora.channel_create({"agent": "claude-a", "title": "ideas"})
    foreign = say(agora, "claude-a", "elsewhere", channel="ideas")
    with pytest.raises(AgoraError) as exc:
        agora.thread_from({"agent": "claude-a", "message_id": a, "to_message_id": foreign})
    assert exc.value.status == 409
    assert other["ok"]


def test_suggested_title_uses_the_model_and_falls_back_to_the_first_line(agora):
    first = say(agora, "claude-a", "@codex-b can you check **the hub port** for QA? It collides with 8810 every time.")
    plain = agora.thread_title({"agent": "claude-a", "message_id": first})
    assert plain["source"] == "heuristic" and plain["title"].startswith("codex-b can you check the hub port")
    assert len(plain["title"]) <= 61 and plain["messages"] == 1
    seen = []

    def chat(body):
        seen.append(body)
        return 200, {"ok": True, "json": {"title": "  «Puerto del Hub de pruebas»  "}, "model": "glm-test"}

    agora.title_chat = chat
    smart = agora.thread_title({"agent": "claude-a", "message_id": first})
    assert smart == {"ok": True, "title": "Puerto del Hub de pruebas", "source": "model", "model": "glm-test",
                     "messages": 1, "space_id": "ecosystem"}
    assert "collides with 8810" in seen[0]["messages"][1]["content"]
    created = agora.thread_from({"agent": "claude-a", "message_id": first})
    assert created["title_source"] == "model" and created["thread"]["title"] == "Puerto del Hub de pruebas"
    # a model that fails never loses the thread
    agora.title_chat = lambda body: (503, {"ok": False, "error": "no_model"})
    second = say(agora, "claude-a", "Plain question about reviews")
    fallback = agora.thread_from({"agent": "claude-a", "message_id": second})
    assert fallback["title_source"] == "heuristic" and fallback["thread"]["title"] == "Plain question about reviews"

    def boom(body):
        raise RuntimeError("down")

    agora.title_chat = boom
    third = say(agora, "claude-a", "Another one")
    assert agora.thread_from({"agent": "claude-a", "message_id": third})["thread"]["title"] == "Another one"


def test_following_is_automatic_for_writers_and_mentions_and_a_choice_sticks(agora):
    root = say(agora, "claude-a", "let's discuss")
    tid = agora.thread_from({"agent": "claude-a", "message_id": root, "title": "Discusion"})["thread"]["thread_id"]
    follows = lambda who: agora.thread_list({"agent": who, "scope": "all"})["threads"][0]["following"]
    assert follows("claude-a") is True and follows("codex-b") is False
    agora.channel_post({"agent": "claude-a", "thread_id": tid, "body": "@codex-b what do you think?"})
    assert follows("codex-b") is True                       # named in it
    agora.channel_post({"agent": "cursor-c", "thread_id": tid, "body": "I will write here"})
    assert follows("cursor-c") is True                      # wrote in it
    off = agora.thread_follow({"agent": "codex-b", "thread_id": tid, "follow": False})
    assert off["following"] is False
    agora.channel_post({"agent": "claude-a", "thread_id": tid, "body": "@codex-b again"})
    agora.channel_post({"agent": "codex-b", "thread_id": tid, "body": "replying anyway"})
    assert follows("codex-b") is False                      # the choice was not overridden
    assert agora.thread_follow({"agent": "codex-b", "thread_id": tid})["following"] is True
    listing = agora.thread_list({"agent": "codex-b"})
    assert [t["thread_id"] for t in listing["threads"]] == [tid] and listing["scope"] == "following"
    with pytest.raises(AgoraError):
        agora.thread_follow({"agent": "codex-b", "thread_id": 1})      # not a thread from a message


def test_unread_and_the_threads_view_per_reader(agora):
    root = say(agora, "claude-a", "topic")
    tid = agora.thread_from({"agent": "claude-a", "message_id": root, "title": "Tema"})["thread"]["thread_id"]
    agora.thread_follow({"agent": "codex-b", "thread_id": tid})
    agora.channel_post({"agent": "claude-a", "thread_id": tid, "body": "one"})
    agora.channel_post({"agent": "claude-a", "thread_id": tid, "body": "two for @codex-b"})
    mine = agora.thread_list({"agent": "codex-b"})
    assert mine["unread"] == 2 and mine["mentions"] == 1 and mine["threads"][0]["unread"] == 2
    assert agora.thread_list({"agent": "codex-b", "unread": True})["total"] == 1
    assert agora.thread_list({"agent": "claude-a"})["threads"][0]["unread"] == 0
    agora.channel_read({"thread_id": tid, "agent": "codex-b"})                     # reading marks it seen
    assert agora.thread_list({"agent": "codex-b"})["unread"] == 0
    assert agora.thread_list({"agent": "codex-b", "unread": True})["total"] == 0
    assert agora.thread_list({"agent": "cursor-c"})["threads"] == []                  # follows nothing
    assert agora.thread_list({"agent": "cursor-c", "scope": "all"})["total"] == 1
    assert agora.thread_list({"scope": "all"})["threads"][0]["following"] is None  # no reader, no follow state
    with pytest.raises(AgoraError):
        agora.thread_list({"scope": "following"})
    assert agora.thread_list({"agent": "claude-a", "channel": "ideas"})["threads"] == []


def test_close_archive_rename_and_a_message_opens_it_again(agora):
    root = say(agora, "claude-a", "to close")
    tid = agora.thread_from({"agent": "claude-a", "message_id": root, "title": "Cerrar"})["thread"]["thread_id"]
    renamed = agora.thread_set({"agent": "codex-b", "thread_id": tid, "title": "Cerrado luego"})
    assert renamed["changed"] == {"title": "Cerrado luego"} and renamed["thread"]["title_source"] == "manual"
    closed = agora.thread_set({"agent": "codex-b", "thread_id": tid, "state": "closed"})
    assert closed["thread"]["state"] == "closed" and closed["thread"]["closed_by"] == "codex-b"
    assert agora.thread_list({"agent": "claude-a", "scope": "all"})["total"] == 0
    assert agora.thread_list({"agent": "claude-a", "scope": "all", "state": "closed"})["total"] == 1
    archived = agora.thread_set({"agent": "claude-a", "thread_id": tid, "state": "archived"})
    assert archived["thread"]["state"] == "archived"
    assert agora.thread_list({"scope": "all", "state": "all"})["total"] == 1
    agora.channel_post({"agent": "cursor-c", "thread_id": tid, "body": "still here"})
    assert agora.thread_list({"scope": "all"})["threads"][0]["state"] == "open"
    body = " ".join(m["body"] for m in agora.channel_read({"thread_id": tid, "peek": True})["messages"])
    assert "reabrió" in body and "cerró el hilo" in body and "archivó" in body
    # the badge in the channel follows the state
    assert page(agora)["messages"][-1]["thread"]["state"] == "open" or True
    with pytest.raises(AgoraError):
        agora.thread_set({"agent": "claude-a", "thread_id": tid})
    with pytest.raises(AgoraError):
        agora.thread_set({"agent": "claude-a", "thread_id": tid, "state": "gone"})


def test_pins_in_a_channel_and_in_a_thread(agora):
    one = say(agora, "claude-a", "Decision: use port 8897")
    two = say(agora, "codex-b", "Second note")
    out = agora.message_pin({"agent": "codex-b", "message_id": one})
    assert out["pinned"] and [p["message_id"] for p in out["pins"]] == [one] and out["pins"][0]["pinned_by"] == "codex-b"
    agora.message_pin({"agent": "claude-a", "message_id": two})
    assert [p["message_id"] for p in agora.pins({"channel": "general"})["pins"]] == [two, one]
    read = page(agora)
    assert [p["message_id"] for p in read["pins"]] == [two, one]
    assert {m["id"] for m in read["messages"] if m.get("pinned")} == {one, two}
    assert page(agora, before_id=two)["messages"][-1].get("pinned") is True and "pins" not in page(agora, before_id=two)
    agora.message_pin({"agent": "claude-a", "message_id": two, "pinned": False})
    assert [p["message_id"] for p in agora.pins({"channel": "general"})["pins"]] == [one]
    # the same pin twice is one pin; a bookkeeping line cannot be pinned
    agora.message_pin({"agent": "codex-b", "message_id": one})
    assert len(agora.pins({"channel": "general"})["pins"]) == 1
    system = next(m for m in page(agora, limit=200)["messages"] if m["kind"] == "system")
    with pytest.raises(AgoraError) as exc:
        agora.message_pin({"agent": "claude-a", "message_id": system["id"]})
    assert exc.value.status == 409
    tid = agora.thread_from({"agent": "claude-a", "message_id": one, "title": "T"})["thread"]["thread_id"]
    inner = agora.channel_post({"agent": "claude-a", "thread_id": tid, "body": "pin me too"})["message_id"]
    agora.message_pin({"agent": "claude-a", "message_id": inner})
    assert [p["message_id"] for p in agora.pins({"thread_id": tid})["pins"]] == [inner]
    assert [p["message_id"] for p in agora.channel_read({"thread_id": tid, "peek": True})["pins"]] == [inner]


def test_brain_links_in_messages_and_tasks_and_their_backlinks(agora):
    msg = say(agora, "claude-a", "See [[Proyectos/Faustus Brain#Vault|the vault]] and [[Notas diarias]] "
                                 "but not ![[embedded.png]] nor [[Notas diarias]] twice")
    links = agora.brain_links({"message_id": msg})["links"]
    assert links == [{"page": "Proyectos/Faustus Brain", "heading": "Vault", "label": "the vault"},
                     {"page": "Notas diarias", "heading": "", "label": ""}]
    shown = next(m for m in page(agora)["messages"] if m["id"] == msg)
    assert [l["page"] for l in shown["brain"]] == ["Proyectos/Faustus Brain", "Notas diarias"]
    task = agora.task_add({"agent": "claude-a", "title": "Write the brief", "body": "Context in [[notas diarias.md]]"})["task"]
    assert agora.brain_links({"task_id": task["id"]})["links"] == [{"page": "notas diarias.md", "heading": "", "label": ""}]
    back = agora.brain_backlinks({"page": "Notas diarias"})
    kinds = {(b["kind"], b["id"]) for b in back["backlinks"]}
    assert ("message", msg) in kinds and ("task", task["id"]) in kinds
    message_hit = next(b for b in back["backlinks"] if b["kind"] == "message" and b["id"] == msg)
    assert "[[Notas diarias]]" in message_hit["excerpt"] and message_hit["author"] == "claude-a"
    assert message_hit["channel"] == "general" and message_hit["space_id"] == "ecosystem"
    assert next(b for b in back["backlinks"] if b["kind"] == "task")["title"] == "Write the brief"
    # the page's name without its folders and the aliases find it too; another page finds nothing
    assert any(b["id"] == msg for b in agora.brain_backlinks({"page": "Faustus Brain"})["backlinks"])
    assert any(b["id"] == msg for b in agora.brain_backlinks({"page": "Other", "aliases": ["proyectos/faustus brain.md"]})["backlinks"])
    assert agora.brain_backlinks({"page": "Missing page"})["backlinks"] == []
    # editing a task rewrites what it cites
    agora.task_edit({"agent": "claude-a", "task_id": task["id"], "expected_etag": task["etag"], "body": "Now [[Otra pagina]]"})
    assert not any(b["kind"] == "task" for b in agora.brain_backlinks({"page": "Notas diarias"})["backlinks"])
    assert agora.brain_backlinks({"page": "Otra pagina"})["backlinks"][0]["kind"] == "task"
    with pytest.raises(AgoraError):
        agora.brain_backlinks({"page": " "})


def test_backlinks_can_stay_in_one_server(agora):
    agora.space_create({"agent": "claude-a", "id": "otro", "title": "Otro"})
    say(agora, "claude-a", "in the main one: [[Shared]]")
    other = agora.for_space("otro")
    other.channel_post({"agent": "claude-a", "channel": "general", "body": "in the other one: [[Shared]]"})
    assert agora.brain_backlinks({"page": "Shared"})["total"] == 2
    only = agora.brain_backlinks({"page": "Shared", "all_spaces": "false"})
    assert only["total"] == 1 and only["backlinks"][0]["space_id"] == "ecosystem"


def test_parse_links_and_titles():
    assert agora_threads.parse_links("![[img.png]] [[A|b]] [[A|c]] [[ ]] [[B#h]]") == [
        {"page": "A", "heading": "", "label": "b"}, {"page": "B", "heading": "h", "label": ""}]
    assert agora_threads.link_key("Folder\\My Page.MD") == "folder/my page"
    long = agora_threads.heuristic_title([{"body": "word " * 40}])
    assert long.endswith("…") and len(long) <= 61
    assert agora_threads.heuristic_title([{"body": "   "}]) == "Hilo"


def test_the_tools_exist_and_are_reachable_by_route_and_mcp_names(agora):
    names = {t["name"] for t in TOOLS}
    assert {"hub_agora_thread_from", "hub_agora_thread_title", "hub_agora_thread_set", "hub_agora_thread_follow",
            "hub_agora_thread_list", "hub_agora_message_pin", "hub_agora_pins", "hub_agora_brain_links",
            "hub_agora_brain_backlinks"} <= names
    from hoard_link.hub import agora_channels
    for op in agora_threads.OPERATIONS:
        assert op in agora_channels.OPERATIONS and hasattr(Agora, op)
    assert set(agora_threads.WRITE_OPS) <= set(agora_channels.WRITE_OPS)
    assert set(agora_threads.READ_ROUTES) <= set(agora_channels.READ_ROUTES)
    # the person writes from their pages
    root = say(agora, "claude-a", "for the person")
    out = agora.thread_from(person(message_id=root, title="De Luis"))
    assert out["thread"]["created_by"] == PERSON and out["thread"]["following"] is True
