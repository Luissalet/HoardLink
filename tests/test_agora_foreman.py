from types import SimpleNamespace

import pytest

from hoard_link.hub.agora import Agora
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.agora_foreman import AgoraForemanFacet
from hoard_link.hub.agora_ranks import set_rank
from hoard_link.hub.config import HubConfig
from hoard_link.hub.events import EventLog
from hoard_link.hub.notify import NotifyFacet


@pytest.fixture
def room(tmp_path, monkeypatch):
    now = [1_790_000_000.0]
    events = EventLog(None, now=lambda: now[0])
    config = HubConfig(data_dir=str(tmp_path), language="es")
    agora = Agora(tmp_path / "agora.db", clock=lambda: now[0], config=config, emit=events.emit)
    agora.space_create({"id": "estudio", "title": "Studio", "agent": "builder"})
    studio = agora.for_space("estudio")
    facets = {"agora": SimpleNamespace(agora=agora)}
    hub = SimpleNamespace(config=config, events=events, facet=facets.get)
    notify = facets["notify"] = NotifyFacet(hub)
    # The same production pass runs on a simulated clock without a background-thread race.
    monkeypatch.setattr(AgoraForemanFacet, "_run", lambda self: None)
    monitor = AgoraForemanFacet(hub)
    monitor.start()
    yield SimpleNamespace(now=now, agora=agora, studio=studio, hub=hub,
                          notify=notify, monitor=monitor, facets=facets)
    monitor.close()
    notify.close()
    agora.close()
    events.close()


def heartbeat(room):
    return room.studio.heartbeat({"agent": "claude-capataz", "renew_resources": []})


def claim(agora):
    task = agora.task_add({"agent": "builder", "title": "Test delivery"})["task"]
    return agora.task_claim({"agent": "builder", "task_id": task["id"]})["task"]


def status(monitor):
    return monitor.status({"space_id": "estudio"})["foreman"]


@pytest.fixture
def console(room, monkeypatch):
    monkeypatch.setattr(AgoraConsoleFacet, "_ensure_leader_drain", lambda *_: None)
    monkeypatch.setattr(AgoraConsoleFacet, "_codex_executable", lambda _: "fake-runner")
    monkeypatch.setattr(AgoraConsoleFacet, "_provider_data", lambda _: {
        "codex": {"current_model": "test-model", "current_effort": "high",
                  "catalog": [{"id": "test-model", "efforts": ["high"]}]},
    })
    root = room.facets["agora_console"] = AgoraConsoleFacet(room.hub)
    yield root.for_space("estudio")
    root.close()


def save_session(console, session_id, leader_id="codex"):
    with console._db() as db:
        db.execute("INSERT INTO leader_sessions(leader_id,session_id,updated_at) VALUES(?,?,0)",
                   (leader_id, session_id))


def queued_turns(console):
    with console._db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM leader_turns ORDER BY rowid")]


@pytest.mark.parametrize("review", [False, True])
def test_one_alert_per_outage_and_recovery(room, review):
    heartbeat(room)
    studio, monitor = room.studio, room.monitor
    studio.heartbeat({"agent": "director"})
    set_rank(studio, "director", "director", studio.person_id)
    studio.heartbeat({"agent": "other-member"})
    task = claim(studio)
    if review:
        studio.task_submit({"agent": "builder", "task_id": task["id"], "summary": "Ready for review"})

    room.now[0] += 1800
    monitor.check("estudio")
    assert status(monitor)["seconds_since_heartbeat"] == 1800
    assert not status(monitor)["overdue"]
    assert room.notify.unseen_count() == 0

    room.now[0] += 1
    monitor.check("estudio")
    thread_id = status(monitor)["outage_thread_id"]
    assert thread_id and status(monitor)["overdue"]
    thread = studio.thread({"thread_id": thread_id})
    assert thread["thread"]["status"] == "open"
    assert thread["messages"][0]["mentions"] == ["director"]
    [message] = [row for row in studio.inbox({"agent": "director", "peek": True})["messages"]
                 if row["thread_id"] == thread_id]
    assert message["body"].startswith("No se recibió el latido")
    [notice] = room.notify.history()
    assert not notice["seen"] and not notice["channels"]
    assert notice["url"].endswith(f"?space_id=estudio#agora-{thread_id}")

    room.now[0] += 600
    monitor.check("estudio")
    assert status(monitor)["outage_thread_id"] == thread_id
    assert room.notify.history() == [notice]

    heartbeat(room)
    monitor.check("estudio")
    assert status(monitor)["outage_thread_id"] is None
    assert status(monitor)["last_heartbeat_at"] == room.now[0]
    assert studio.thread({"thread_id": thread_id})["thread"]["status"] == "resolved"
    assert room.notify.unseen_count() == 0
    assert room.notify.get_notification(notice["id"])["seen"]

    room.now[0] += 1801
    monitor.check("estudio")
    assert status(monitor)["outage_thread_id"] not in (None, thread_id)
    assert len(room.notify.history()) == 2 and room.notify.unseen_count() == 1


def test_console_delivers_one_turn_per_message_to_the_established_leader(room, console):
    launcher = console._leader_identity("codex")
    for director in ("claude", "codex-investiga"):
        room.studio.heartbeat({"agent": director})
        set_rank(room.studio, director, "director", room.studio.person_id)
    with console._db() as db:
        db.execute("INSERT INTO workers(id,agent_id,parent_id,model,effort,task,state,launched_by,created_at,updated_at)"
                   " VALUES ('foreman','claude-capataz','claude','test-model','','Monitor','completed',?,0,0)",
                   (launcher,))
    save_session(console, "studio-leader")
    save_session(console, "unaddressed-leader", "cursor")
    other_space = room.facets["agora_console"]
    save_session(other_space, "ecosystem-leader")
    claim(room.studio)
    heartbeat(room)
    room.now[0] += 1801

    room.monitor.check("estudio")
    room.monitor.check("estudio")
    thread_id = status(room.monitor)["outage_thread_id"]
    thread = room.studio.thread({"thread_id": console.thread_id})
    [alert] = [row for row in thread["messages"] if f"#agora-{thread_id}" in row["body"]]
    assert alert["author"] == "system"
    assert alert["mentions"] == ["claude", "codex-investiga", launcher]
    assert "Capataz sin latido" in alert["body"]
    inbox = room.studio.inbox({"agent": launcher, "peek": True})["messages"]
    assert [row["id"] for row in inbox if row["for_you"] and "Capataz sin latido" in row["body"]] == [alert["id"]]
    assert room.studio.thread({"thread_id": thread_id})["thread"]["status"] == "open"
    assert len(room.notify.history()) == 1
    [turn] = queued_turns(console)
    assert (turn["leader_id"], turn["session_id"], turn["message_id"], turn["state"]) == (
        "codex", "studio-leader", str(alert["id"]), "queued")
    assert turn["prompt"].count(alert["body"]) == 1
    assert queued_turns(other_space) == []

    heartbeat(room)
    room.monitor.check("estudio")
    room.monitor.check("estudio")
    work = room.studio.thread({"thread_id": console.thread_id})
    alert_again, resolution = [row for row in work["messages"] if f"#agora-{thread_id}" in row["body"]]
    assert alert_again["id"] == alert["id"]
    assert resolution["mentions"] == alert["mentions"]
    assert resolution["body"].startswith("Se recibió un nuevo latido de claude-capataz.")
    first_turn, last_turn = queued_turns(console)
    assert first_turn == turn
    assert last_turn["state"] == "queued" and last_turn["session_id"] == "studio-leader"
    assert last_turn["message_id"] == str(resolution["id"]) and last_turn["prompt"].count(resolution["body"]) == 1
    assert room.studio.thread({"thread_id": thread_id})["thread"]["status"] == "resolved"
    assert room.notify.unseen_count() == 0


def test_console_mentions_the_leader_even_without_a_session_in_the_space(room, console):
    room.hub.config.language = "en"
    room.studio.heartbeat({"agent": "codex-investiga"})
    set_rank(room.studio, "codex-investiga", "director", room.studio.person_id)
    save_session(room.facets["agora_console"], "ecosystem-leader")
    heartbeat(room)
    room.now[0] += 1801
    room.monitor.check("estudio")
    assert status(room.monitor)["outage_thread_id"] is None
    assert queued_turns(console) == []

    claim(room.studio)
    room.monitor.check("estudio")
    heartbeat(room)
    room.monitor.check("estudio")
    thread = room.studio.thread({"thread_id": console.thread_id})
    alert, resolution = [row for row in thread["messages"] if "codex-investiga" in row["mentions"]]
    assert alert["body"].startswith("Foreman heartbeat overdue.")
    assert resolution["body"].startswith("A new heartbeat arrived from claude-capataz.")
    assert queued_turns(console) == []


def test_console_retries_the_same_message_after_an_interrupted_dispatch(room, console, monkeypatch):
    leader = console._leader_identity("codex")
    room.studio.heartbeat({"agent": leader})
    set_rank(room.studio, leader, "director", room.studio.person_id)
    save_session(console, "studio-leader")
    heartbeat(room)
    claim(room.studio)
    room.now[0] += 1801
    with monkeypatch.context() as interruption:
        def fail_after_queue(*_args):
            raise RuntimeError("Interrupted after committing the turn")

        interruption.setattr(console, "_record", fail_after_queue)
        with pytest.raises(RuntimeError, match="Interrupted after committing the turn"):
            room.monitor.check("estudio")
    [turn] = queued_turns(console)
    room.monitor.close()

    restarted = AgoraForemanFacet(room.hub)
    restarted.start()
    try:
        restarted.check("estudio")
        restarted.check("estudio")
        assert queued_turns(console) == [turn]
        thread = room.studio.thread({"thread_id": console.thread_id})
        assert [row["id"] for row in thread["messages"] if leader in row["mentions"]] == [int(turn["message_id"])]
        assert len(room.notify.history()) == 1
    finally:
        restarted.close()


def test_console_queued_notice_does_not_start_a_replacement_session(room, console, monkeypatch):
    leader = console._leader_identity("codex")
    room.studio.heartbeat({"agent": leader})
    set_rank(room.studio, leader, "director", room.studio.person_id)
    save_session(console, "studio-leader")
    heartbeat(room)
    claim(room.studio)
    room.now[0] += 1801
    room.monitor.check("estudio")
    with console._db() as db:
        db.execute("UPDATE leader_sessions SET session_id='replacement' WHERE leader_id='codex'")

    def unexpected_turn(_turn):
        pytest.fail("A heartbeat notice must stay with the addressed session.")

    monkeypatch.setattr(console, "_run_leader_turn", unexpected_turn)
    console._drain_leader("codex")
    [turn] = queued_turns(console)
    assert turn["state"] == "failed" and turn["session_id"] == "studio-leader"


def test_silence_without_work_in_the_space_does_not_alert(room):
    heartbeat(room)
    last = room.now[0]
    claim(room.agora)  # work in ecosystem only
    room.now[0] += 1801
    room.agora.heartbeat({"agent": "claude-capataz"})
    task = room.studio.task_add({"agent": "claude-capataz", "title": "Unclaimed local work"})["task"]
    room.monitor.check("estudio")
    assert status(room.monitor)["last_heartbeat_at"] == last
    assert status(room.monitor)["overdue"]
    assert status(room.monitor)["outage_thread_id"] is None
    assert room.monitor.status({})["foreman"] is None
    assert not room.notify.history()

    room.studio.task_claim({"agent": "builder", "task_id": task["id"]})
    room.monitor.check("estudio")
    assert status(room.monitor)["outage_thread_id"]
    assert len(room.notify.history()) == 1


def test_custom_threshold_and_worker_without_a_claimed_task(room):
    room.monitor.settings["estudio"] = {"agent": "relief", "threshold_minutes": 1}
    root = room.facets["agora_console"] = AgoraConsoleFacet(room.hub)
    try:
        console = root.for_space("estudio")
        room.studio.heartbeat({"agent": "relief"})
        with console._db() as db:
            db.execute("INSERT INTO workers(id, agent_id, parent_id, model, effort, task, state, created_at, updated_at)"
                       " VALUES ('qa', 'codex-worker', 'codex', 'local', '', 'QA', 'running', ?, ?)",
                       (room.now[0], room.now[0]))
        room.now[0] += 61
        room.monitor.check("estudio")
        result = status(room.monitor)
        assert result["agent_id"] == "relief"
        assert result["threshold_seconds"] == 60 and result["outage_thread_id"]
    finally:
        root.close()


def test_initial_grace_and_restart_between_inbox_send_and_ack(room):
    monitor = room.monitor
    claim(room.studio)
    monitor.check("estudio")
    assert status(monitor)["last_heartbeat_at"] is None
    assert room.notify.unseen_count() == 0
    room.now[0] += 1801
    monitor.check("estudio")
    thread_id = status(monitor)["outage_thread_id"]
    [notice] = room.notify.history()
    room.agora.db.execute("UPDATE agora_foreman_outages SET notification_id=NULL")
    monitor.close()

    restarted = AgoraForemanFacet(room.hub)
    restarted.start()
    try:
        restarted.check("estudio")
        assert status(restarted)["outage_thread_id"] == thread_id
        assert room.notify.history() == [notice]
        heartbeat(room)
        restarted.check("estudio")
        assert room.notify.unseen_count() == 0
        assert status(restarted)["outage_thread_id"] is None
    finally:
        restarted.close()
