"""A recorded decision keeps its thread, author and history through reversible edits."""

import pytest

from hoard_link.hub.agora import MIGRATIONS, Agora, AgoraError
from hoard_link.sqlkit import Database

from ._hub_fakes import http, make_hub, serve, ui_headers


@pytest.fixture
def agora(tmp_path):
    store = Agora(tmp_path / "agora.db")
    yield store
    store.close()


def decision(store, *, space=None):
    scope = {"space_id": space} if space else {}
    thread = store.thread_open({**scope, "agent": "writer", "kind": "decision",
                               "title": "Título inicial", "body": "Propuesta."})["thread"]
    call = {**scope, "thread_id": thread["id"]}
    store.resolve({**call, "agent": "recorder", "resolution": "Decisión inicial."})
    return call


def test_retitle_amend_withdraw_and_undo_keep_a_durable_trail(agora):
    call = decision(agora)
    thread_author = {**call, "agent": "writer"}
    decision_author = {**call, "agent": "recorder"}
    retitle = agora.thread_retitle({**thread_author, "title": "Título revisado"})
    amend = agora.decision_amend({**decision_author, "resolution": "Decisión corregida."})
    withdraw = agora.decision_withdraw(decision_author)

    current = agora.thread(call)
    assert current["thread"]["status"] == "resolved"
    assert current["thread"]["title"] == "Título revisado"
    assert current["thread"]["resolution"] is None
    assert current["thread"]["resolved_by"] == "recorder"
    assert current["thread"]["withdrawn"]
    assert [event["action"] for event in current["history"]] == ["retitle", "amend", "withdraw"]
    assert [event["before_text"] for event in current["history"]] == [
        "Título inicial", "Decisión inicial.", "Decisión corregida."
    ]
    assert all(event["created"] > 0 for event in current["history"])
    assert [event["author"] for event in current["history"]] == ["writer", "recorder", "recorder"]
    listed = agora.decisions()["decisions"][0]
    assert listed["withdrawn"] and listed["resolution"] is None
    assert [event["action"] for event in listed["history"]] == ["recorded", "amend", "withdraw"]

    with pytest.raises(AgoraError, match="newer"):
        agora.decision_history_undo({**decision_author, "undo_token": amend["undo_token"]})
    agora.decision_history_undo({**thread_author, "undo_token": retitle["undo_token"]})
    for outcome, restored in ((withdraw, "Decisión corregida."), (amend, "Decisión inicial.")):
        result = agora.decision_history_undo({**decision_author, "undo_token": outcome["undo_token"]})
        assert result["thread"]["resolution"] == restored
        assert result["change"]["undo_of"] == outcome["change"]["id"]

    current = agora.thread(call)
    assert current["thread"]["title"] == "Título inicial"
    assert current["thread"]["status"] == "resolved" and not current["thread"]["withdrawn"]
    assert len(current["history"]) == 6
    assert all("undo_token" not in event for event in current["history"])
    assert {event["message_id"] for event in current["history"]} <= {row["id"] for row in current["messages"]}
    with pytest.raises(AgoraError, match="already been undone"):
        agora.decision_history_undo({**decision_author, "undo_token": amend["undo_token"]})


def test_retitle_keeps_an_open_thread_and_its_messages(agora):
    thread = agora.thread_open({"agent": "writer", "title": "Old title", "body": "First post"})["thread"]
    args = {"agent": "writer", "thread_id": thread["id"]}
    result = agora.thread_retitle({**args, "title": "New title"})
    assert result["thread"]["status"] == "open"
    assert agora.threads()["threads"][0]["title"] == "New title"
    restored = agora.decision_history_undo({**args, "undo_token": result["undo_token"]})
    assert restored["thread"]["status"] == "open" and restored["thread"]["title"] == "Old title"
    current = agora.thread(args)
    assert current["messages"][0]["body"] == "First post"
    assert [event["action"] for event in current["history"]] == ["retitle", "undo"]


def test_permissions_respect_space_original_author_director_and_person(agora):
    for space in ("fiction", "other"):
        agora.space_create({"agent": "writer", "id": space, "title": space.title()})
    for actor, rank in (("manager", "director"), ("responsible", "lead")):
        agora.heartbeat({"space_id": "fiction", "agent": actor})
        agora.member_rank_set({"space_id": "fiction", "agent": agora.person_id, "_person": True,
                               "member": actor, "rank": rank})
    call = decision(agora, space="fiction")
    for operation, fields in (
        (agora.thread_retitle, {"title": "Otro título"}),
        (agora.decision_amend, {"resolution": "Texto de otro"}),
        (agora.decision_withdraw, {}),
    ):
        with pytest.raises(AgoraError) as error:
            operation({**call, **fields, "agent": "responsible"})
        assert error.value.status == 403
        with pytest.raises(AgoraError) as error:
            operation({**call, **fields, "agent": "manager", "space_id": "other"})
        assert error.value.status == 404
    edit = agora.decision_amend({**call, "agent": "manager", "resolution": "Texto de dirección"})
    with pytest.raises(AgoraError) as error:
        agora.decision_history_undo({**call, "agent": "writer", "undo_token": edit["undo_token"]})
    assert error.value.status == 403
    result = agora.decision_history_undo({**call, "agent": agora.person_id, "_person": True,
                                          "undo_token": edit["undo_token"]})
    assert result["thread"]["resolution"] == "Decisión inicial."


def test_undo_cannot_cross_a_later_recording_even_if_text_matches(agora):
    call = {**decision(agora), "agent": "recorder"}
    assert agora.decision_amend({**call, "resolution": "Decisión inicial."})["change"] is None
    result = agora.decision_amend({**call, "resolution": "Nueva decisión."})
    agora.reopen({**call, "reason": "Revisit"})
    agora.resolve({**call, "resolution": "Nueva decisión."})
    with pytest.raises(AgoraError, match="recorded decision has changed"):
        agora.decision_history_undo({**call, "undo_token": result["undo_token"]})
    history = agora.decisions()["decisions"][0]["history"]
    assert [version["action"] for version in history] == ["recorded", "amend", "recorded"]
    assert history[-1]["after_text"] == "Nueva decisión."


def test_migration_keeps_a_preexisting_decision_editable(tmp_path):
    path = tmp_path / "historical.db"
    legacy = Database(path, migrations=MIGRATIONS[:-1])
    legacy.migrate()
    thread_id = legacy.insert("threads", {
        "title": "Historical title", "kind": "decision", "status": "resolved", "created_by": "writer",
        "resolution": "Historical decision", "resolved_by": "recorder", "created": 10, "updated": 11,
    })
    legacy.insert("messages", {
        "thread_id": thread_id, "kind": "resolution", "author": "recorder", "body": "Historical decision",
        "mentions": "[]", "created": 11,
    })
    legacy.close()

    store = Agora(path)
    try:
        call = {"thread_id": thread_id, "agent": "recorder"}
        entry = store.decisions()["decisions"][0]
        assert entry["history"][0]["after_text"] == "Historical decision"
        outcome = store.decision_withdraw(call)
        assert outcome["thread"]["withdrawn"]
        restored = store.decision_history_undo({**call, "undo_token": outcome["undo_token"]})
        assert restored["thread"]["resolution"] == "Historical decision"
    finally:
        store.close()


def test_http_and_tool_share_undo_and_reject_forged_person(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    try:
        headers = {"Authorization": f"Bearer {hub.token}"}
        person_headers = ui_headers(hub)
        root = hub.config.url
        _, response = http(root + "/api/agora/thread_open", {
            "agent": "writer", "kind": "decision", "title": "Initial", "body": "Proposal."
        }, headers)
        call = {"thread_id": response["thread"]["id"]}
        http(root + "/api/agora/resolve", {**call, "agent": "writer", "resolution": "Initial text"}, headers)
        person_id = hub.config.person_profile["id"]
        spoof = {**call, "agent": person_id, "_person": True, "resolution": "Impersonation"}
        status, _ = http(root + "/api/agora/decision_amend", spoof, {**headers, "X-Hoard-Person": "faustus-studio"})
        assert status == 403
        tool_path = root + "/api/agent/tools/hub_agora_decision_amend"
        status, _ = http(tool_path, spoof, headers)
        assert status == 400
        status, denied = http(tool_path, {key: value for key, value in spoof.items() if key != "_person"}, headers)
        assert status == 400 and denied["result"]["status"] == 403
        status, envelope = http(root + "/api/agent/tools/hub_agora_decision_amend", {
            **call, "agent": "writer", "resolution": "Tool amendment"
        }, headers)
        assert status == 200, envelope
        token = envelope["result"]["undo_token"]
        status, restored = http(root + "/api/agora/decision_history_undo", {**call, "undo_token": token}, person_headers)
        assert status == 200, restored
        assert restored["thread"]["resolution"] == "Initial text" and restored["change"]["author"] == person_id
    finally:
        server.shutdown()
        server.server_close()
