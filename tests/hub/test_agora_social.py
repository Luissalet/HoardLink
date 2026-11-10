"""The social side of a server: adding and removing agents, reactions, quoting, files, a server's own picture and
the order of its channels."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

import pytest

from hoard_link.hub import agora_social
from hoard_link.hub.agora import Agora, AgoraError

from ._hub_fakes import http, make_hub, serve

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


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


def _member(a, ident, space="atlas"):
    return next((m for m in a.members({"space_id": space})["members"] if m["id"] == ident), None)


def _general(a, space="atlas", **extra):
    return a.channel_read({"space_id": space, "channel": "general", **extra})


# ---- members ---------------------------------------------------------------------------------------------------

def test_an_agent_added_by_name_is_invited_until_it_shows_up(ag):
    out = ag.member_add({"space_id": "atlas", "agent": "claude", "id": "Codex-Docs", "note": "para la documentación"})
    assert out["ok"] and out["added"] and out["registered"]
    assert out["member"]["id"] == "codex-docs" and out["member"]["presence"] == "invited"
    assert out["member"]["role"] == "codex" and out["member"]["last_seen"] is None
    assert _member(ag, "codex-docs")["note"] == "para la documentación"
    # it is in the server view, among those who are around, and can be mentioned and given work at once
    guild = ag.guild({"space_id": "atlas"})
    assert any(m["id"] == "codex-docs" and m["presence"] == "invited" for m in guild["members"])
    said = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "@codex-docs mira esto"})
    assert said["mentions"] == ["codex-docs"]
    # the channel says who came
    assert any("añade a codex-docs" in m["body"] for m in _general(ag)["messages"] if m["kind"] == "system")
    # adding it again changes nothing but the note
    again = ag.member_add({"space_id": "atlas", "agent": "claude", "id": "codex-docs", "note": "otra"})
    assert again["added"] is False and again["registered"] is False and again["member"]["note"] == "otra"
    # its first sign of life makes it one more
    ag.heartbeat({"space_id": "atlas", "agent": "codex-docs", "state": "working", "doing": "leyendo"})
    assert _member(ag, "codex-docs")["presence"] == "working"


def test_names_that_are_not_agents_are_refused(ag):
    for bad in ("", "x", "con espacios", "luis", "hub", "system"):
        with pytest.raises(AgoraError):
            ag.member_add({"space_id": "atlas", "agent": "claude", "id": bad})
    with pytest.raises(AgoraError) as exc:
        ag.member_remove({"space_id": "atlas", "agent": "claude", "id": "luis"})
    assert exc.value.status == 409
    with pytest.raises(AgoraError) as exc:
        ag.member_remove({"space_id": "atlas", "agent": "claude", "id": "nadie-aqui"})
    assert exc.value.status == 404


def test_removing_an_agent_frees_its_work_and_drops_its_leases(ag):
    ag.heartbeat({"space_id": "atlas", "agent": "codex-ui", "state": "working"})
    doing = ag.task_add({"space_id": "atlas", "agent": "codex-ui", "title": "Pantalla"})["task"]["id"]
    ag.task_claim({"space_id": "atlas", "agent": "codex-ui", "task_id": doing, "locks": ["path:atlas/ui.tsx"]})
    sent = ag.task_add({"space_id": "atlas", "agent": "codex-ui", "title": "Entregada"})["task"]["id"]
    ag.task_claim({"space_id": "atlas", "agent": "codex-ui", "task_id": sent})
    ag.task_submit({"space_id": "atlas", "agent": "codex-ui", "task_id": sent, "summary": "hecho", "commits": ["abc1234"]})
    assert ag.locks({"space_id": "atlas"})["locks"]

    # another agent may not take a lively one off the server, unless it says force; the person and itself always may
    with pytest.raises(AgoraError) as exc:
        ag.member_remove({"space_id": "atlas", "agent": "claude", "id": "codex-ui"})
    assert exc.value.status == 409 and exc.value.extra.get("error_code") == "member_active"
    out = ag.member_remove({"space_id": "atlas", "agent": "luis", "_person": True, "id": "codex-ui", "reason": "ya no hace falta"})
    assert out["ok"] and out["removed"] == "codex-ui"
    assert out["freed_tasks"] == [doing] and out["left_in_review"] == [sent] and out["handed_tasks"] == []
    assert out["forgotten"] is True                       # it was on no other server: the Ágora forgets it
    assert _member(ag, "codex-ui") is None
    task = ag.task({"space_id": "atlas", "task_id": doing})["task"]
    assert task["status"] == "open" and task["owner"] is None
    assert ag.task({"space_id": "atlas", "task_id": sent})["task"]["status"] == "review"   # a delivery stays as it is
    assert not ag.locks({"space_id": "atlas"})["locks"]
    assert any("da de baja a codex-ui" in m["body"] and "ya no hace falta" in m["body"]
               for m in _general(ag)["messages"] if m["kind"] == "system")
    # showing up again is joining again
    ag.heartbeat({"space_id": "atlas", "agent": "codex-ui", "state": "idle"})
    assert _member(ag, "codex-ui")["presence"] == "idle"


def test_removing_an_agent_can_hand_its_work_to_another(ag):
    for agent in ("codex-a", "codex-b"):
        ag.heartbeat({"space_id": "atlas", "agent": agent, "state": "working"})
    tid = ag.task_add({"space_id": "atlas", "agent": "codex-a", "title": "A medias"})["task"]["id"]
    ag.task_claim({"space_id": "atlas", "agent": "codex-a", "task_id": tid, "locks": ["path:atlas/x.py"]})
    out = ag.member_remove({"space_id": "atlas", "agent": "codex-a", "id": "codex-a", "to_agent": "codex-b"})
    assert out["handed_tasks"] == [tid] and out["handed_to"] == "codex-b" and out["freed_tasks"] == []
    task = ag.task({"space_id": "atlas", "task_id": tid})["task"]
    assert task["owner"] == "codex-b" and task["status"] == "claimed"
    assert [lk["owner"] for lk in ag.locks({"space_id": "atlas"})["locks"]] == ["codex-b"]
    with pytest.raises(AgoraError):                        # nobody is handed its own work
        ag.member_remove({"space_id": "atlas", "agent": "codex-b", "id": "codex-b", "to_agent": "codex-b"})


def test_an_agent_on_two_servers_leaves_one_and_stays_in_the_other(ag):
    ag.space_create({"agent": "claude", "id": "coase", "title": "Coase"})
    for space in ("atlas", "coase"):
        ag.heartbeat({"space_id": space, "agent": "codex-x", "state": "idle"})
    out = ag.member_remove({"space_id": "atlas", "agent": "codex-x", "id": "codex-x"})
    assert out["forgotten"] is False
    assert _member(ag, "codex-x") is None and _member(ag, "codex-x", "coase") is not None


def test_prune_takes_out_only_who_is_long_gone_and_holds_nothing(ag):
    for agent in ("codex-old", "codex-busy", "codex-new"):
        ag.heartbeat({"space_id": "atlas", "agent": agent, "state": "idle"})
    tid = ag.task_add({"space_id": "atlas", "agent": "codex-busy", "title": "Suya"})["task"]["id"]
    ag.task_claim({"space_id": "atlas", "agent": "codex-busy", "task_id": tid})
    ag.clock.t += 40 * 86400
    ag.heartbeat({"space_id": "atlas", "agent": "codex-new", "state": "idle"})
    ag.heartbeat({"space_id": "atlas", "agent": "claude", "state": "idle"})
    plan = ag.members_prune({"space_id": "atlas", "agent": "claude", "dry_run": True})
    assert plan["removed"] == ["codex-old"] and plan["kept"] == [{"id": "codex-busy", "why": "tasks"}] and plan["dry_run"]
    assert _member(ag, "codex-old") is not None            # a dry run only tells
    done = ag.members_prune({"space_id": "atlas", "agent": "claude", "older_than_days": 30})
    assert done["removed"] == ["codex-old"] and not done["dry_run"]
    assert _member(ag, "codex-old") is None and _member(ag, "codex-busy") is not None and _member(ag, "codex-new") is not None
    assert ag.members_prune({"space_id": "atlas", "agent": "claude"})["removed"] == []      # nothing left to prune


def test_the_full_list_carries_everyone_and_filters_by_name(ag):
    for agent in ("codex-a", "claude-b"):
        ag.heartbeat({"space_id": "atlas", "agent": agent, "state": "idle"})
    everyone = ag.members({"space_id": "atlas"})
    assert everyone["total"] == 3 and {m["id"] for m in everyone["members"]} == {"claude", "codex-a", "claude-b"}
    assert [m["id"] for m in ag.members({"space_id": "atlas", "q": "codex"})["members"]] == ["codex-a"]


# ---- reactions, quoting, files ---------------------------------------------------------------------------------

def test_reactions_are_put_taken_back_and_counted_per_emoji(ag):
    mid = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "¿lo subo?"})["message_id"]
    ag.heartbeat({"space_id": "atlas", "agent": "codex-a", "state": "idle"})
    assert ag.react({"space_id": "atlas", "agent": "codex-a", "message_id": mid, "emoji": "👍"})["on"] is True
    out = ag.react({"space_id": "atlas", "agent": "claude", "message_id": mid, "emoji": "👍"})
    assert out["reactions"] == [{"emoji": "👍", "count": 2, "agents": ["codex-a", "claude"], "mine": True}]
    ag.react({"space_id": "atlas", "agent": "claude", "message_id": mid, "emoji": "visto"})
    page = _general(ag, agent="codex-a", peek=True)
    seen = next(m for m in page["messages"] if m["id"] == mid)["reactions"]
    assert [(r["emoji"], r["count"], r["mine"]) for r in seen] == [("👍", 2, True), ("visto", 1, False)]
    # without ``on`` it toggles; with it, it is what it says
    assert ag.react({"space_id": "atlas", "agent": "codex-a", "message_id": mid, "emoji": "👍"})["on"] is False
    assert ag.react({"space_id": "atlas", "agent": "codex-a", "message_id": mid, "emoji": "👍", "on": False})["on"] is False
    assert ag.react({"space_id": "atlas", "agent": "claude", "message_id": mid, "emoji": "👍", "on": True})["reactions"][0]["count"] == 1
    for bad in ("", "dos palabras", "x" * 17, "<b>"):
        with pytest.raises(AgoraError):
            ag.react({"space_id": "atlas", "agent": "claude", "message_id": mid, "emoji": bad})
    with pytest.raises(AgoraError) as exc:                  # a message of another space is not here
        ag.react({"space_id": "ecosystem", "agent": "claude", "message_id": mid, "emoji": "👍"})
    assert exc.value.status == 404


def test_quoting_a_message_shows_it_and_tells_its_author(ag):
    ag.heartbeat({"space_id": "atlas", "agent": "codex-a", "state": "idle"})
    first = ag.channel_post({"space_id": "atlas", "agent": "codex-a", "body": "Propongo  cambiar\nel esquema"})["message_id"]
    out = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "de acuerdo", "reply_to": first})
    assert out["mentions"] == ["codex-a"]
    answer = next(m for m in _general(ag)["messages"] if m["id"] == out["message_id"])
    assert answer["reply"] == {"id": first, "author": "codex-a", "excerpt": "Propongo cambiar el esquema"}
    inbox = ag.inbox({"space_id": "atlas", "agent": "codex-a", "peek": True})
    assert any(m["id"] == out["message_id"] and "codex-a" in m["mentions"] and m["for_you"] for m in inbox["messages"])
    # quoting oneself tells nobody; a message of another conversation cannot be quoted here
    own = ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "me corrijo", "reply_to": out["message_id"]})
    assert own["mentions"] == []
    ag.channel_create({"space_id": "atlas", "agent": "claude", "title": "ideas"})
    with pytest.raises(AgoraError) as exc:
        ag.channel_post({"space_id": "atlas", "agent": "claude", "channel": "ideas", "body": "x", "reply_to": first})
    assert exc.value.status == 409
    assert _general(ag)["messages"][-1]["id"] == own["message_id"]      # and nothing was written by the refused post


def test_a_message_can_carry_files_and_a_file_goes_on_one_message_only(ag, tmp_path):
    text = ag.file_add({"space_id": "atlas", "agent": "claude", "name": "..\\notas de hoy.txt",
                        "content_base64": base64.b64encode("hola\n".encode()).decode()})["file"]
    assert text["name"] == "notas de hoy.txt" and text["type"] == "text/plain" and text["size"] == 5 and text["inline"]
    image = ag.file_add({"space_id": "atlas", "agent": "claude", "name": "captura.png",
                         "content_base64": base64.b64encode(PNG).decode()})["file"]
    assert image["name"] == "captura.png" and image["type"] == "image/png"
    out = ag.channel_post({"space_id": "atlas", "agent": "claude", "files": [image["id"], text["id"]]})
    message = next(m for m in _general(ag)["messages"] if m["id"] == out["message_id"])
    assert message["body"] == "captura.png · notas de hoy.txt"            # a post of files alone is named after them
    assert [f["id"] for f in message["files"]] == [image["id"], text["id"]]
    row, blob = agora_social.file_read(ag, image["id"])
    assert blob == PNG and row["message_id"] == out["message_id"] and row["space_id"] == "atlas"
    with pytest.raises(AgoraError) as exc:                  # the same file cannot go on a second message
        ag.channel_post({"space_id": "atlas", "agent": "claude", "body": "otra vez", "files": [image["id"]]})
    assert exc.value.status == 409
    with pytest.raises(AgoraError) as exc:                  # nor a file of another space
        ag.channel_post({"space_id": "ecosystem", "agent": "claude", "body": "x", "files": [text["id"]]})
    assert exc.value.status == 404
    with pytest.raises(AgoraError):
        ag.channel_post({"space_id": "atlas", "agent": "claude"})          # neither text nor files
    for bad in ({"name": "a.txt", "content_base64": "no es base64 !!"}, {"name": "", "content_base64": "aG9sYQ=="},
                {"name": "vacio.txt", "content_base64": ""}):
        with pytest.raises(AgoraError):
            ag.file_add({"space_id": "atlas", "agent": "claude", **bad})
    secret = tmp_path / "clave.txt"                         # the hub never reads a path, whoever asks
    secret.write_text("no se lee", encoding="utf-8")
    for who in ({"agent": "claude"}, {"agent": "luis", "_person": True}):
        with pytest.raises(AgoraError) as exc:
            ag.file_add({"space_id": "atlas", **who, "path": str(secret)})
        assert exc.value.status == 400 and "content_base64" in str(exc.value)
    with pytest.raises(AgoraError) as exc:
        ag.file_add({"space_id": "atlas", "agent": "claude", "name": "grande.bin",
                     "content_base64": base64.b64encode(b"0" * (agora_social.MAX_FILE_BYTES + 1)).decode()})
    assert exc.value.status == 413


def test_a_file_never_attached_is_forgotten_after_a_day(ag):
    lost = ag.file_add({"space_id": "atlas", "agent": "claude", "name": "a.txt", "content_base64": "aG9sYQ=="})["file"]["id"]
    ag.clock.t += agora_social.UNATTACHED_KEEP_S + 60
    ag.file_add({"space_id": "atlas", "agent": "claude", "name": "b.txt", "content_base64": "aG9sYQ=="})
    with pytest.raises(AgoraError) as exc:
        agora_social.file_read(ag, lost)
    assert exc.value.status == 404
    assert not (Path(ag.db.path).parent / "agora-files" / lost).exists()


# ---- how a server looks ----------------------------------------------------------------------------------------

def test_a_server_takes_a_picture_as_its_icon_and_another_icon_forgets_it(ag):
    out = ag.space_icon_set({"space_id": "atlas", "agent": "claude", "image_base64": "data:image/png;base64," + base64.b64encode(PNG).decode()})
    assert out["meta"]["icon"].startswith("img:")
    blob, ctype = agora_social.icon_read(ag.for_space("atlas"))
    assert blob == PNG and ctype == "image/png"
    assert next(s for s in ag.servers()["servers"] if s["id"] == "atlas")["icon"].startswith("img:")
    for bad in ("", "aG9sYQ==", base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * agora_social.MAX_ICON_BYTES).decode()):
        with pytest.raises(AgoraError):
            ag.space_icon_set({"space_id": "atlas", "agent": "claude", "image_base64": bad})
    ag.space_meta_set({"space_id": "atlas", "agent": "claude", "icon": "🚀"})
    with pytest.raises(AgoraError) as exc:
        agora_social.icon_read(ag.for_space("atlas"))
    assert exc.value.status == 404


def test_channels_are_put_in_order_in_one_call(ag):
    for name in ("ideas", "bugs", "diseno"):
        ag.channel_create({"space_id": "atlas", "agent": "claude", "title": name})
    out = ag.channels_reorder({"space_id": "atlas", "agent": "claude", "order": ["diseno", "general", "#Ideas"]})
    assert out["order"] == ["general", "diseno", "ideas", "bugs"]
    assert [c["slug"] for c in ag.channels({"space_id": "atlas"})["channels"]] == ["general", "diseno", "ideas", "bugs"]
    with pytest.raises(AgoraError) as exc:
        ag.channels_reorder({"space_id": "atlas", "agent": "claude", "order": ["no-existe"]})
    assert exc.value.status == 404
    with pytest.raises(AgoraError):
        ag.channels_reorder({"space_id": "atlas", "agent": "claude", "order": []})


# ---- over HTTP -------------------------------------------------------------------------------------------------

def _raw(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_files_pictures_and_members_over_http(tmp_path):
    with ExitStack() as cleanup:
        hub = make_hub(tmp_path, [])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        base = hub.config.url.rstrip("/")
        token = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
        studio = {**token, "X-Hoard-Person": "faustus-studio"}

        # a file bigger than the hub's usual small body goes through, with a token only
        big = base64.b64encode(b"x" * 600_000).decode()
        status, out = http(base + "/api/agora/file_add", {"agent": "claude", "name": "volcado.bin", "content_base64": big}, headers=token)
        assert status == 200 and out["ok"] and out["file"]["size"] == 600_000 and out["file"]["inline"] is False
        blob_id = out["file"]["id"]
        status, _ = http(base + "/api/agora/file_add", {"agent": "claude", "name": "x.txt", "content_base64": "aG9sYQ=="})
        assert status == 401
        status, page = http(base + "/api/agora/file_add", {"agent": "claude", "name": "pagina.html",
                                                           "content_base64": base64.b64encode(b"<script>1</script>").decode()}, headers=token)
        html_id = page["file"]["id"]
        status, posted = http(base + "/api/agora/channel_post", {"body": "mirad", "files": [blob_id, html_id]}, headers=studio)
        assert status == 200 and posted["ok"]

        status, body, headers = _raw(base + "/api/agora/file/" + blob_id, token)
        assert status == 200 and body == b"x" * 600_000 and headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Disposition"] == 'attachment; filename="volcado.bin"'
        status, body, headers = _raw(base + "/api/agora/file/" + html_id, token)       # a page is never shown in place
        assert status == 200 and headers["Content-Type"] == "application/octet-stream" and "attachment" in headers["Content-Disposition"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert _raw(base + "/api/agora/file/" + blob_id)[0] == 401
        assert _raw(base + "/api/agora/file/" + "0" * 32, token)[0] == 404

        # the person reacts and quotes from Studio as the person; an agent's token alone does not
        mid = posted["message_id"]
        status, out = http(base + "/api/agora/react", {"message_id": mid, "emoji": "✅"}, headers=studio)
        assert status == 200 and out["reactions"][0]["agents"] == ["luis"]
        status, out = http(base + "/api/agora/react", {"agent": "luis", "message_id": mid, "emoji": "✅"}, headers=token)
        assert status == 403
        status, page = http(base + "/api/agora/channels/general", headers=studio)
        shown = next(m for m in page["messages"] if m["id"] == mid)
        assert [f["name"] for f in shown["files"]] == ["volcado.bin", "pagina.html"] and shown["reactions"][0]["mine"] is True

        # a picture for the server, and the picture back
        status, out = http(base + "/api/agora/space_icon_set", {"image_base64": base64.b64encode(PNG).decode()}, headers=studio)
        assert status == 200 and out["meta"]["icon"].startswith("img:")
        status, body, headers = _raw(base + "/api/agora/space_icon?space_id=ecosystem", token)
        assert status == 200 and body == PNG and headers["Content-Type"] == "image/png"
        assert _raw(base + "/api/agora/space_icon?space_id=ecosystem")[0] == 401

        # members: add, list, remove
        status, out = http(base + "/api/agora/member_add", {"id": "codex-nuevo", "note": "pruebas"}, headers=studio)
        assert status == 200 and out["member"]["presence"] == "invited"
        status, out = http(base + "/api/agora/members?q=codex", headers=token)
        assert status == 200 and [m["id"] for m in out["members"]] == ["codex-nuevo"]
        status, out = http(base + "/api/agora/member_remove", {"id": "codex-nuevo"}, headers=studio)
        assert status == 200 and out["removed"] == "codex-nuevo" and out["forgotten"] is True
        status, out = http(base + "/api/agora/members_prune", {"agent": "claude", "dry_run": True}, headers=token)
        assert status == 200 and out["dry_run"] is True

        # the same through the agents' tools
        status, out = http(base + "/api/agent/call", {"tool": "hub_agora_member_add",
                                                      "args": {"agent": "claude", "id": "cursor-qa"}}, headers=token)
        assert status == 200 and json.dumps(out).count("cursor-qa") >= 1
        status, out = http(base + "/api/agent/call", {"tool": "hub_agora_react",
                                                      "args": {"agent": "claude", "message_id": mid, "emoji": "👀"}}, headers=token)
        assert status == 200


def test_a_thread_and_a_task_can_be_read_as_a_peek(ag):
    """An agent that only looks (a conversation that reads the Agora without taking part) leaves its read mark alone."""
    ag.heartbeat({"space_id": "atlas", "agent": "codex-a", "state": "working"})
    tid = ag.task_add({"space_id": "atlas", "agent": "claude", "title": "Mirar sin marcar"})["task"]
    thread = ag.task({"space_id": "atlas", "task_id": tid["id"]})["task"]["thread_id"]
    ag.post({"space_id": "atlas", "agent": "claude", "thread_id": thread, "body": "@codex-a mira esto", "mentions": ["codex-a"]})
    waiting = lambda: ag.inbox({"space_id": "atlas", "agent": "codex-a", "peek": True})["counts"]["pending"]
    assert waiting() == 1
    ag.thread({"space_id": "atlas", "agent": "codex-a", "thread_id": thread, "peek": True})
    ag.task({"space_id": "atlas", "agent": "codex-a", "task_id": tid["id"], "peek": True})
    assert waiting() == 1                                   # looked at twice, still waiting
    ag.thread({"space_id": "atlas", "agent": "codex-a", "thread_id": thread})
    assert waiting() == 0                                   # read: no longer waiting
