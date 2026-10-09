"""Agent sessions: what the agents wrote across the family, grouped by agent and session, and the way to undo one.

Every app that adopted the accountable-agents part of ``hoard_link.agentkit`` keeps a write journal
(``<data>/agent_journal.jsonl``) and serves it at ``GET /api/agent/journal``; it also undoes a whole session at
``POST /api/agent/undo`` and manages extra tokens with a profile at ``/api/agent/tokens``. The hub already knows every app's
URL and token, so this facet is the one place that

* reads those journals (``overview``): one row per ``(app, agent, session)`` with the writes, the reasons given, the tools
  used and how much of it can still be undone, and the apps that have not adopted the journal yet;
* undoes a session on the person's say (``undo``): the page always asks for a dry run first, shows what would be undone and
  what cannot (a later write by someone else, no handler), and only then repeats the call with ``confirm``;
* mints and revokes the tokens of an app per agent and profile (``tokens``), through the app's own admin route.

Only the hub's page (or its bearer token) may undo a session or manage tokens: the answers of other apps are never trusted
to start one. The journals are the source of truth; the ``agent.write`` / ``agent.undo`` events the apps also emit are hints.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import quote

from . import contract
from .facets import Facet, Request

CACHE_S = 3.0
PER_APP_LIMIT = 400
DEFAULT_REASON = "Deshacer sesión desde el Hub"


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row.get("id"), "ts": row.get("ts"), "tool": row.get("tool"), "reason": row.get("reason") or "",
            "summary": row.get("args_summary") or "", "ok": bool(row.get("ok", True)), "undone": bool(row.get("undone")),
            "undoable": bool(row.get("undoable")), "error": row.get("error") or "", "objects": list(row.get("objects") or [])[:4]}


def group_sessions(app_id: str, app_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One dict per ``(agent, session)`` of the journal lines ``rows`` (oldest first, as the app serves them)."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    undo_lines = 0
    for row in rows:
        if row.get("kind", "write") != "write":
            undo_lines += 1
            continue
        key = (str(row.get("agent") or ""), str(row.get("session") or ""))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"key": f"{app_id}|{key[0]}|{key[1]}", "app": app_id, "app_name": app_name, "agent": key[0],
                               "session": key[1], "first_ts": row.get("ts"), "last_ts": row.get("ts"), "writes": 0, "failed": 0,
                               "tools": {}, "reasons": [], "undoable": 0, "undone": 0, "entries": []}
        g["last_ts"] = row.get("ts") or g["last_ts"]
        g["writes"] += 1
        if not row.get("ok", True):
            g["failed"] += 1
        g["tools"][str(row.get("tool"))] = g["tools"].get(str(row.get("tool")), 0) + 1
        reason = str(row.get("reason") or "")
        if reason and reason not in g["reasons"]:
            g["reasons"].append(reason)
        if row.get("undone"):
            g["undone"] += 1
        elif row.get("ok", True) and row.get("undoable"):
            g["undoable"] += 1
        g["entries"].append(_brief(row))
    out = []
    for g in groups.values():
        g["reasons"] = g["reasons"][-3:]
        g["entries"] = g["entries"][-12:]
        g["can_undo"] = bool(g["session"]) and g["undoable"] > 0
        out.append(g)
    return sorted(out, key=lambda g: g["last_ts"] or 0, reverse=True)


class AgentSessionsFacet(Facet):
    id = "agent_sessions"
    ui_scripts = ("agent_sessions.js",)

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.Lock()
        self._cache: Optional[dict[str, Any]] = None
        self._at = 0.0

    # ------------------------------------------------------------------ reading
    def _journal(self, app: Any, limit: int, session: str = "", agent: str = "") -> dict[str, Any]:
        path = f"/api/agent/journal?limit={int(limit)}"
        if session:
            path += "&session=" + quote(session, safe="")
        if agent:
            path += "&agent=" + quote(agent, safe="")
        status, body = contract.app_request(app, "GET", path, timeout=2.5)
        row: dict[str, Any] = {"id": app.id, "name": app.name, "url": app.url}
        if status is None:
            return {**row, "state": "down", "entries": []}
        if status == 200 and isinstance(body, dict) and isinstance(body.get("entries"), list):
            return {**row, "state": "ok", "entries": body["entries"], "reasons_required": bool(body.get("reasons_required")),
                    "undo_tools": body.get("undo_tools") or []}
        if status == 401:
            return {**row, "state": "unauthorized", "entries": []}
        return {**row, "state": "not_adopted", "entries": []}

    def overview(self, refresh: bool = False, limit: int = PER_APP_LIMIT) -> dict[str, Any]:
        with self._lock:
            if not refresh and self._cache is not None and time.monotonic() - self._at < CACHE_S:
                return self._cache
        apps = list(self.hub.apps)
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(apps)))) as pool:
            fetched = list(pool.map(lambda a: self._journal(a, limit), apps)) if apps else []
        sessions: list[dict[str, Any]] = []
        summary = []
        for item in fetched:
            sessions.extend(group_sessions(item["id"], item["name"], item["entries"]))
            summary.append({"id": item["id"], "name": item["name"], "state": item["state"], "entries": len(item["entries"]),
                            "reasons_required": item.get("reasons_required", False), "undo_tools": item.get("undo_tools", [])})
        sessions.sort(key=lambda g: g["last_ts"] or 0, reverse=True)
        agents: dict[str, dict[str, Any]] = {}
        for s in sessions:
            a = agents.setdefault(s["agent"], {"agent": s["agent"], "sessions": [], "writes": 0, "last_ts": 0, "undoable": 0})
            a["sessions"].append(s)
            a["writes"] += s["writes"]
            a["undoable"] += s["undoable"]
            a["last_ts"] = max(a["last_ts"], s["last_ts"] or 0)
        result = {"ok": True, "checked_at": time.time(), "apps": sorted(summary, key=lambda a: (a["state"] != "ok", a["name"].lower())),
                  "agents": sorted(agents.values(), key=lambda a: a["last_ts"], reverse=True), "sessions": sessions,
                  "adopted": sum(1 for a in summary if a["state"] == "ok")}
        with self._lock:
            self._cache, self._at = result, time.monotonic()
        return result

    def session(self, app_id: str, session: str, agent: str = "") -> dict[str, Any]:
        """Every journal line of one session of one app, oldest first."""
        app = self.hub.get(app_id)
        if app is None:
            return {"ok": False, "status": 404, "error": f"unknown app {app_id!r}"}
        item = self._journal(app, 1000, session=session, agent=agent)
        if item["state"] != "ok":
            return {"ok": False, "status": 503 if item["state"] == "down" else 404, "error": f"{app.name}: {item['state']}"}
        return {"ok": True, "app": app_id, "session": session, "entries": item["entries"]}

    # ------------------------------------------------------------------ acting
    def undo(self, body: dict[str, Any]) -> dict[str, Any]:
        app = self.hub.get(str(body.get("app") or ""))
        session = str(body.get("session") or "").strip()
        if app is None:
            return {"ok": False, "status": 404, "error": "unknown app"}
        if not session:
            return {"ok": False, "status": 400, "error": "a session id is needed (writes without one cannot be undone together)"}
        dry_run = bool(body.get("dry_run", True))
        payload: dict[str, Any] = {"session": session, "dry_run": dry_run, "confirm": bool(body.get("confirm")) and not dry_run,
                                   "reason": str(body.get("reason") or DEFAULT_REASON)[:300]}
        if body.get("agent"):
            payload["agent"] = str(body["agent"])
        status, answer = contract.app_request(app, "POST", "/api/agent/undo", payload, timeout=60.0)
        with self._lock:
            self._cache = None
        if status is None:
            return {"ok": False, "status": 503, "error": f"{app.name} is not reachable"}
        if not isinstance(answer, dict):
            answer = {"error": f"unexpected answer (HTTP {status})"}
        if not 200 <= status < 300:
            return {"ok": False, "status": status, "error": answer.get("error") or f"HTTP {status}", "code": answer.get("code"),
                    "hint": answer.get("hint")}
        return {**answer, "ok": True}

    def tokens(self, app_id: Optional[str] = None) -> dict[str, Any]:
        apps = [a for a in self.hub.apps if not app_id or a.id == app_id]
        rows = []
        for app in apps:
            status, body = contract.app_request(app, "GET", "/api/agent/tokens", timeout=2.5)
            if status == 200 and isinstance(body, dict) and isinstance(body.get("tokens"), list):
                rows.append({"app": app.id, "name": app.name, "tokens": body.get("tokens") or [], "profiles": body.get("profiles") or []})
            else:
                rows.append({"app": app.id, "name": app.name, "tokens": [], "profiles": [], "state": "down" if status is None else "not_adopted"})
        return {"ok": True, "apps": rows}

    def mint(self, body: dict[str, Any]) -> dict[str, Any]:
        app = self.hub.get(str(body.get("app") or ""))
        if app is None:
            return {"ok": False, "status": 404, "error": "unknown app"}
        status, answer = contract.app_request(app, "POST", "/api/agent/tokens", {
            "agent": str(body.get("agent") or ""), "profile": str(body.get("profile") or "drafts"), "label": str(body.get("label") or "")}, timeout=5.0)
        if status is None:
            return {"ok": False, "status": 503, "error": f"{app.name} is not reachable"}
        if status != 200 or not isinstance(answer, dict):
            return {"ok": False, "status": status or 502, "error": (answer or {}).get("error") if isinstance(answer, dict) else f"HTTP {status}"}
        return {**answer, "ok": True}

    def revoke(self, body: dict[str, Any]) -> dict[str, Any]:
        app = self.hub.get(str(body.get("app") or ""))
        token_id = str(body.get("id") or "").strip()
        if app is None or not token_id:
            return {"ok": False, "status": 400, "error": "app and token id are needed"}
        status, answer = contract.app_request(app, "DELETE", "/api/agent/tokens/" + quote(token_id, safe=""), timeout=5.0)
        if status is None:
            return {"ok": False, "status": 503, "error": f"{app.name} is not reachable"}
        if status != 200 or not isinstance(answer, dict):
            return {"ok": False, "status": status, "error": (answer or {}).get("error") if isinstance(answer, dict) else f"HTTP {status}"}
        return {**answer, "ok": True}

    # ------------------------------------------------------------------ http
    @staticmethod
    def _operator(req: Request) -> bool:
        return req.caller() in ("hub", "ui") or req.agent()

    def get(self, req: Request) -> Any:
        if not req.path.startswith("/api/agent-sessions"):
            return None
        if not self._operator(req):
            return {"ok": False, "status": 403, "error": "only the Hub operator may read the agent journals"}
        if req.path == "/api/agent-sessions":
            return self.overview(req.q_bool("refresh"), req.q_int("limit", PER_APP_LIMIT) or PER_APP_LIMIT)
        if req.path == "/api/agent-sessions/session":
            return self.session(str(req.q("app", "")), str(req.q("session", "")), str(req.q("agent", "")))
        if req.path == "/api/agent-sessions/tokens":
            return self.tokens(req.q("app"))
        return None

    def post(self, req: Request) -> Any:
        if not req.path.startswith("/api/agent-sessions/"):
            return None
        if not self._operator(req):
            return {"ok": False, "status": 403, "error": "only the Hub operator may undo sessions or manage agent tokens"}
        if req.path == "/api/agent-sessions/undo":
            return self.undo(req.body)
        if req.path == "/api/agent-sessions/tokens":
            return self.mint(req.body)
        if req.path == "/api/agent-sessions/tokens/revoke":
            return self.revoke(req.body)
        return None

    # ------------------------------------------------------------------ tools
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [{"name": "hub_agent_sessions",
                 "description": "What the agents wrote in the family apps that keep a write journal: one row per app, agent and session "
                                "(writes, reasons, tools, how many can still be undone). Pass app and session to read every line of one "
                                "session. Read only: undoing a session is done from the Hub page.",
                 "inputSchema": {"type": "object", "properties": {
                     "app": {"type": "string"}, "session": {"type": "string"}, "agent": {"type": "string"},
                     "refresh": {"type": "boolean", "default": False}}, "additionalProperties": False},
                 "annotations": {"readOnlyHint": True}}]

    def handlers(self) -> dict[str, Any]:
        def run(args: dict[str, Any]) -> dict[str, Any]:
            if args.get("app") and args.get("session"):
                return self.session(str(args["app"]), str(args["session"]), str(args.get("agent") or ""))
            result = self.overview(bool(args.get("refresh")))
            agent, app = str(args.get("agent") or ""), str(args.get("app") or "")
            if agent or app:
                result = {**result, "sessions": [s for s in result["sessions"] if (not agent or s["agent"] == agent) and (not app or s["app"] == app)]}
            return result
        return {"hub_agent_sessions": run}
