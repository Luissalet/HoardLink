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
