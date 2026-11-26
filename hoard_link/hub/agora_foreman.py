"""Watch explicit, space-scoped Agora heartbeats and track one notice per outage."""

from __future__ import annotations

import json
import logging
import math
import threading
from typing import Any
from urllib.parse import quote

from ..coordination.spaces import SpaceError
from . import agora_ranks
from .facets import Facet

LOG = logging.getLogger(__name__)
HEARTBEAT = "agora.agent.heartbeat"
DEFAULT_MINUTES = 30
POLL_SECONDS = 10
SCHEMA = """
CREATE TABLE IF NOT EXISTS agora_foreman_heartbeats (
    space_id TEXT NOT NULL, agent TEXT NOT NULL, monitoring_since REAL NOT NULL,
    event_id INTEGER, heartbeat_at REAL, PRIMARY KEY (space_id, agent)
);
CREATE TABLE IF NOT EXISTS agora_foreman_outages (
    space_id TEXT PRIMARY KEY, agent TEXT NOT NULL, event_id INTEGER,
    thread_id INTEGER NOT NULL REFERENCES threads(id), notification_id INTEGER
);
CREATE TABLE IF NOT EXISTS agora_foreman_work_messages (
    thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE, phase TEXT NOT NULL,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, dispatched INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (thread_id, phase)
);
CREATE INDEX IF NOT EXISTS agora_foreman_pending ON agora_foreman_work_messages(message_id) WHERE dispatched=0;
"""


class AgoraForemanFacet(Facet):
    id = "agora_foreman"

    def __init__(self, hub: Any):
        super().__init__(hub)
        cfg = getattr(hub.config, "agora", {})
        foremen = cfg.get("foremen") if isinstance(cfg, dict) else None
        self.settings = {"estudio": {"agent": "claude-capataz"}}
        if isinstance(foremen, dict):
            self.settings.update({sid: row for sid, row in foremen.items() if isinstance(row, dict)})
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._unsubscribe = None
        self._thread = None

    def start(self) -> None:
        facet = self.hub.facet("agora")
        if facet is None or self._thread is not None:
            return
        self.agora = facet.agora
        self.agora.db.script(SCHEMA)
        self._unsubscribe = self.hub.events.subscribe(self._on_event)
        for event in self.hub.events.query(type=HEARTBEAT, source="hub", limit=2000):
            self._on_event(event)
        self._thread = threading.Thread(target=self._run, name="agora-foreman", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()

    def _settings(self, sid: str) -> tuple[str, float] | None:
        row = self.settings.get(sid)
        if row is None:
            return None
        try:
            agent = self.agora._agent(row.get("agent", "claude-capataz" if sid == "estudio" else None))
        except ValueError:
            return None
        try:
            minutes = float(row.get("threshold_minutes", DEFAULT_MINUTES))
        except (TypeError, ValueError):
            minutes = DEFAULT_MINUTES
        if not math.isfinite(minutes) or minutes <= 0 or not math.isfinite(minutes * 60):
            minutes = DEFAULT_MINUTES
        return agent, minutes * 60

    def _on_event(self, event: dict[str, Any]) -> None:
        if event["type"] != HEARTBEAT or event["source"] != "hub":
            return
        data = event["data"]
        sid = data.get("space_id")
        if not isinstance(sid, str):
            return
        settings = self._settings(sid)
        if settings is None or settings[0] != data.get("agent"):
            return
        try:
            self.agora.db.execute(
                "INSERT INTO agora_foreman_heartbeats(space_id,agent,monitoring_since,event_id,heartbeat_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(space_id,agent) DO UPDATE SET"
                " event_id=excluded.event_id, heartbeat_at=excluded.heartbeat_at"
                " WHERE event_id IS NULL OR event_id<excluded.event_id",
                (sid, settings[0], event["ts"], event["id"], event["ts"]))
        except Exception:
            LOG.exception("Could not record the foreman heartbeat in %s", sid)
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            for sid in self.settings:
                if self._stop.is_set():
                    break
                try:
                    self.check(sid)
                except SpaceError:
                    continue  # a configured space may be created later
                except Exception:
                    LOG.exception("Could not check the foreman heartbeat in %s", sid)
            self._wake.wait(POLL_SECONDS)

    def _heartbeat(self, sid: str, agent: str) -> Any:
        return self.agora.db.one("SELECT * FROM agora_foreman_heartbeats WHERE space_id=? AND agent=?", (sid, agent))

    def _outage(self, sid: str) -> Any:
        return self.agora.db.one("SELECT * FROM agora_foreman_outages WHERE space_id=?", (sid,))

    def _active_work(self, sid: str) -> bool:
        if self.agora.db.one("SELECT 1 FROM tasks WHERE space_id=? AND status IN ('claimed','review') LIMIT 1", (sid,)):
            return True
        console = self.hub.facet("agora_console")
        return console is not None and any(space == sid for _, space in console._running_worker_names())

    def check(self, sid: str) -> None:
        """Run one monitor pass for a space, independently of API or browser reads."""
        settings = self._settings(sid)
        if settings is None:
            return
        agora = self.agora.for_space(sid)
        agent, threshold = settings
        now = agora.clock()
        heartbeat = self._heartbeat(sid, agent)
        if heartbeat is None:
            agora.db.execute(
                "INSERT OR IGNORE INTO agora_foreman_heartbeats(space_id,agent,monitoring_since) VALUES (?,?,?)",
                (sid, agent, now))
            heartbeat = self._heartbeat(sid, agent)
        outage = self._outage(sid)
        if outage and (outage["agent"] != agent or outage["event_id"] != heartbeat["event_id"]):
            self._close_outage(agora, outage, replaced=outage["agent"] != agent)
            outage = None
        if outage is None:
            since = heartbeat["heartbeat_at"]
            if since is None:
                since = heartbeat["monitoring_since"]
            if now - since > threshold and self._active_work(sid):
                self._open_outage(agora, agent, heartbeat["event_id"])
                outage = self._outage(sid)
        if outage and outage["notification_id"] is None:
            self._notify(agora, outage)
        self._dispatch_work_messages(agora)

    def _console(self, agora: Any) -> Any:
        console = self.hub.facet("agora_console")
        return console.for_space(agora.space_id) if console is not None else None

    @staticmethod
    def _work_thread(agora: Any, console: Any) -> int | None:
        if console is None:
            return None
        thread = console.thread_id
        return thread if agora.db.one("SELECT 1 FROM threads WHERE id=? AND space_id=?",
                                      (thread, agora.space_id)) else None

    @staticmethod
    def _leaders(agora: Any, agent: str, console: Any) -> list[str]:
        leaders = [row["id"] for row in agora_ranks.ranks(agora)["members"]
                   if row["rank"] == "director" and row["id"] != agent]
        launcher = console.worker_launcher(agent) if console is not None else ""
        if launcher not in ("", "codex-console", agora.person_id, agent, *leaders):
            leaders.append(launcher)
        return leaders

    def _open_outage(self, agora: Any, agent: str, event_id: int | None) -> None:
        sid = agora.space_id
        console = self._console(agora)
        work_thread = self._work_thread(agora, console)
        leaders = self._leaders(agora, agent, console)
        with agora.db.tx():
            if self._outage(sid) or self._heartbeat(sid, agent)["event_id"] != event_id:
                return
            title, body = self._notice(agora, agent)
            now = agora.clock()
            thread = agora.db.insert("threads", {
                "space_id": sid, "title": title, "kind": "note", "status": "open",
                "created_by": "system", "created": now, "updated": now,
            })
            agora._msg(thread, "system", "proposal", body, leaders if work_thread is None else [])
            agora.db.insert("agora_foreman_outages", {
                "space_id": sid, "agent": agent, "event_id": event_id, "thread_id": thread,
            })
            message = self._write_work_message(agora, work_thread, thread, "overdue", f"{title}. {body}", leaders)
        agora._bump("agora.thread.opened", {"thread_id": thread, "kind": "note", "title": title, "by": "system"})
        self._publish_work_message(agora, work_thread, message)

    def _notice_url(self, agora: Any, thread: int) -> str:
        return f"{self.hub.config.url}/?space_id={quote(agora.space_id, safe='')}#agora-{thread}"

    def _write_work_message(self, agora: Any, work_thread: int | None, outage_thread: int,
                            phase: str, body: str, leaders: list[str]) -> int | None:
        if work_thread is None:
            return None
        message = agora._msg(work_thread, "system", "comment", f"{body} {self._notice_url(agora, outage_thread)}",
                             leaders)
        agora.db.insert("agora_foreman_work_messages", {
            "thread_id": outage_thread, "phase": phase, "message_id": message,
        })
        return message

    @staticmethod
    def _publish_work_message(agora: Any, thread: int | None, message: int | None) -> None:
        if message is not None:
            agora._bump("agora.thread.message", {
                "thread_id": thread, "message_id": message, "author": "system", "kind": "comment",
            })

    def _dispatch_work_messages(self, agora: Any) -> None:
        pending = agora.db.query(
            "SELECT w.message_id, m.body, m.mentions FROM agora_foreman_work_messages w "
            "JOIN threads t ON t.id=w.thread_id JOIN messages m ON m.id=w.message_id "
            "WHERE t.space_id=? AND w.dispatched=0 ORDER BY w.message_id", (agora.space_id,))
        if not pending or (console := self._console(agora)) is None:
            return
        for row in pending:
            console.deliver_to_existing_leaders(json.loads(row["mentions"]), row["message_id"], row["body"])
            agora.db.execute("UPDATE agora_foreman_work_messages SET dispatched=1 WHERE message_id=?",
                             (row["message_id"],))

    def _notice(self, agora: Any, agent: str) -> tuple[str, str]:
        space = agora.space_store.get(agora.space_id)["title"]
        if self.hub.facet("notify")._lang() == "es":
            return "Capataz sin latido", f"No se recibió el latido de {agent} en «{space}» mientras había trabajo en marcha."
        return "Foreman heartbeat overdue", f"No heartbeat arrived from {agent} in “{space}” while work was in progress."

    def _notification(self, outage: Any) -> int | None:
        if outage["notification_id"] is not None:
            return outage["notification_id"]
        # A committed inbox entry survives a restart between send and recording its id in Agora.
        history = self.hub.facet("notify").history(1, app="hub", group=self._group(outage))
        return history[0]["id"] if history else None

    @staticmethod
    def _group(outage: Any) -> str:
        return f"agora-foreman-{outage['thread_id']}"

    def _notify(self, agora: Any, outage: Any) -> None:
        notification_id = self._notification(outage)
        if notification_id is None:
            title, body = self._notice(agora, outage["agent"])
            link = self._notice_url(agora, outage["thread_id"])
            result = self.hub.facet("notify").send(
                title, body, url=link, group=self._group(outage), channels_override=[], now=agora.clock())
            notification_id = result["id"]
        agora.db.execute("UPDATE agora_foreman_outages SET notification_id=? WHERE thread_id=?",
                         (notification_id, outage["thread_id"]))

    def _close_outage(self, agora: Any, outage: Any, *, replaced: bool) -> None:
        thread = outage["thread_id"]
        opening = agora.db.one(
            "SELECT m.thread_id, m.mentions FROM agora_foreman_work_messages w JOIN messages m ON m.id=w.message_id "
            "WHERE w.thread_id=? AND w.phase='overdue'", (thread,))
        if opening:
            work_thread, leaders = opening["thread_id"], json.loads(opening["mentions"])
        else:
            console = self._console(agora)
            work_thread = self._work_thread(agora, console)
            leaders = self._leaders(agora, outage["agent"], console)
        notification_id = self._notification(outage)
        if notification_id is not None:
            self.hub.facet("notify").mark_read([notification_id])
        agent = outage["agent"]
        if self.hub.facet("notify")._lang() == "es":
            resolution = "Se ha cambiado el capataz que se vigila." if replaced else f"Se recibió un nuevo latido de {agent}."
        else:
            resolution = "The monitored foreman has changed." if replaced else f"A new heartbeat arrived from {agent}."
        with agora.db.tx():
            if not agora.db.one("SELECT 1 FROM agora_foreman_outages WHERE thread_id=?", (thread,)):
                return
            updated = agora.db.execute(
                "UPDATE threads SET status='resolved', resolution=?, resolved_by='system' WHERE id=? AND status<>'resolved'",
                (resolution, thread)).rowcount
            if updated:
                agora._msg(thread, "system", "resolution", resolution, leaders if work_thread is None else [])
            message = self._write_work_message(agora, work_thread, thread, "resolved", resolution, leaders)
            agora.db.execute("DELETE FROM agora_foreman_outages WHERE thread_id=?", (thread,))
        if updated:
            agora._bump("agora.thread.resolved", {"thread_id": thread, "by": "system"})
        self._publish_work_message(agora, work_thread, message)

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(self, "agora"):
            return {"ok": False, "status": 503, "error": "Agora heartbeat monitor unavailable"}
        try:
            sid = self.agora.space_store.get(args.get("space_id"))["id"]
        except SpaceError as exc:
            return {"ok": False, "status": exc.status, "error": str(exc)}
        out = {"ok": True, "space_id": sid, "foreman": None}
        settings = self._settings(sid)
        if settings is None:
            return out
        agent, threshold = settings
        with self.agora.db.lock:
            heartbeat = self._heartbeat(sid, agent)
            outage = self._outage(sid)
        now = self.agora.clock()
        last = heartbeat["heartbeat_at"] if heartbeat else None
        since = last if last is not None else (heartbeat["monitoring_since"] if heartbeat else now)
        out["foreman"] = {
            "agent_id": agent, "last_heartbeat_at": last, "checked_at": now,
            "seconds_since_heartbeat": None if last is None else max(0, int(now - last)),
            "threshold_seconds": threshold, "overdue": now - since > threshold,
            "outage_thread_id": outage["thread_id"] if outage else None,
        }
        return out

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [{
            "name": "hub_agora_foreman",
            "read": True,
            "description": "Read the space's foreman, last explicit heartbeat, age, threshold and open outage thread.",
            "inputSchema": {"type": "object", "properties": {"space_id": {"type": "string"}}},
        }]

    def handlers(self) -> dict[str, Any]:
        return {"hub_agora_foreman": self.status}
