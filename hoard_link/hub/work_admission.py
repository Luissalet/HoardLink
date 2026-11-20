"""Machine-wide admission for work that uses more than GPU memory."""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from .facets import Request
from .production import _tool

LIMITS = {"cpu": 2, "ram": 2, "suite": 1, "worker": 4}
PRIORITY = {"low": 0, "normal": 1, "high": 2}
TTL_SECONDS = 90
ROUTE = "/api/work-admission/"


class WorkQueue:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def _advance(self, now):
        for ticket, row in list(self._items.items()):
            if row["expires"] <= now:
                del self._items[ticket]
        used = dict.fromkeys(LIMITS, 0)
        waiting = []
        for row in self._items.values():
            if row["state"] == "running":
                for resource, units in row["resources"].items():
                    used[resource] += units
            else:
                waiting.append(row)
        waiting.sort(key=lambda row: (-PRIORITY[row["priority"]], row["created"]))
        blocked = set()
        for row in waiting:
            resources = row["resources"]
            if blocked.intersection(resources) or any(used[key] + units > LIMITS[key] for key, units in resources.items()):
                blocked.update(resources)
                continue
            for key, units in resources.items():
                used[key] += units
            row["state"] = "running"

    def manage(self, args):
        action = args.get("action")
        worker = args.get("agent")
        ticket = args.get("ticket")
        if not isinstance(worker, str) or not worker.strip() or len(worker) > 80:
            return {"ok": False, "status": 400, "error": "agent is required"}
        if action not in ("request", "poll", "release") or (action != "request" and not isinstance(ticket, str)):
            return {"ok": False, "status": 400, "error": "Use request, poll or release with a ticket"}
        key = args.get("key")
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 120):
            return {"ok": False, "status": 400, "error": "key must be a non-empty string of up to 120 characters"}
        with self._lock:
            now = self._clock()
            self._advance(now)
            if action == "request":
                resources = args.get("resources")
                priority = args.get("priority", "normal")
                if (not isinstance(resources, dict) or not resources or
                    any(key not in LIMITS or type(units) is not int or not 1 <= units <= LIMITS[key]
                        for key, units in resources.items()) or priority not in tuple(PRIORITY)):
                    return {"ok": False, "status": 400, "error": "Invalid resources or priority", "limits": LIMITS}
                existing = next(((ident, row) for ident, row in self._items.items()
                                 if key and row["key"] == key and row["agent"] == worker), None)
                if existing:
                    ticket, prior = existing
                    if prior["resources"] != resources or prior["priority"] != priority:
                        return {"ok": False, "status": 409, "error": "The work key already has a different request"}
                else:
                    ticket = secrets.token_urlsafe(18)
                    self._items[ticket] = {
                        "agent": worker, "key": key, "resources": dict(resources), "priority": priority,
                        "state": "queued", "created": now, "expires": now + TTL_SECONDS,
                    }
                self._advance(now)
            row = self._items.get(ticket)
            if row is None or row["agent"] != worker:
                return {"ok": False, "status": 404, "error": "Unknown or expired work ticket"}
            if action == "release":
                del self._items[ticket]
                self._advance(now)
                return {"ok": True, "state": "released", "ticket": ticket}
            row["expires"] = now + TTL_SECONDS
            return {"ok": True, "ticket": ticket, "state": row["state"], "expires_in_s": TTL_SECONDS,
                    "undo": {"action": "release", "agent": worker, "ticket": ticket}}


class WorkAdmission:
    def __init__(self, hub):
        self.hub = hub
        self.queue = WorkQueue()

    def _orders(self, operation, args):
        from . import order_budget
        from .agora import AgoraError, SpaceError

        try:
            agora = self.hub.facet("agora").agora
            if not (operation == "metrics" and args.get("all_spaces") in (True, "1")):
                agora = agora.for_space(args.get("space_id"))
            return getattr(order_budget, operation)(agora, args)
        except (AgoraError, SpaceError) as exc:
            return {"ok": False, "status": exc.status, "error": str(exc), **getattr(exc, "extra", {})}

    def _route(self, req: Request, actions):
        name = req.path.removeprefix(ROUTE)
        if name not in actions or not req.path.startswith(ROUTE):
            return None
        if req.caller() is None and not req.agent():
            return {"ok": False, "status": 401, "error": "A family bearer token or the Hub page is required"}
        return actions[name](dict(req.body) if req.method == "POST" else {key: values[0] for key, values in req.query.items()})

    def get(self, req):
        return self._route(req, {"metrics": lambda args: self._orders("metrics", args), "capability": self._capability})

    def post(self, req):
        return self._route(req, {
            "ticket": self.queue.manage,
            "budget": lambda args: self._orders("configure", args),
            "attempt": lambda args: self._orders("attempt", args),
            "capability": self._capability,
        })

    def _capability(self, args):
        from .worker_capabilities import candidates

        console = self.hub.facet("agora_console")
        if args.get("space_id"):
            console = console.for_space(args["space_id"])
        capability = args.get("capability")
        if capability not in ("reasoning_high", "vision_strict", "fast_local"):
            return {"ok": False, "status": 400, "error": "Unknown capability"}
        return {"ok": True, "candidates": candidates(console, capability)}

    @classmethod
    def tools(cls):
        agent = {"agent": {"type": "string"}}
        space = {"space_id": {"type": "string"}}
        order = {**space, **agent, "order_id": {"type": "integer"}}
        return [
            _tool("hub_work_ticket", "Request, renew or release a machine work turn / reserva un turno de carga.",
                  {**agent, "action": {"enum": ["request", "poll", "release"]}, "ticket": {"type": "string"},
                   "key": {"type": "string"},
                   "priority": {"enum": list(PRIORITY)}, "resources": {"type": "object"}},
                  ("agent", "action"), read=False),
            _tool("hub_order_budget", "Set limits for an order / fija los límites de una orden.",
                  {**order, "retries": {"type": "integer"}, "gpu_seconds": {"type": "integer"},
                   "returns": {"type": "integer"}}, ("agent", "order_id"), read=False),
            _tool("hub_order_attempt", "Reserve or report a station attempt / reserva o declara un intento.",
                  {**order, "task_id": {"type": "integer"}, "key": {"type": "string"},
                   "action": {"enum": ["begin", "pass", "fail"]}, "gpu_seconds": {"type": "integer"}},
                  ("agent", "order_id", "task_id", "key", "action"), read=False),
            _tool("hub_order_metrics", "First-pass decisions, returns, task times and station costs / decisiones a la primera, devoluciones, plazos y coste por puesto.",
                  {**space, "all_spaces": {"type": "boolean"}, "order_id": {"type": "integer", "minimum": 1}}),
            _tool("hub_worker_capability", "Find available runners by capability / busca ejecutores por capacidad.",
                  {**space, "capability": {"enum": ["reasoning_high", "vision_strict", "fast_local"]}}, ("capability",)),
        ]

    def handlers(self):
        return {
            "hub_work_ticket": self.queue.manage,
            "hub_order_budget": lambda args: self._orders("configure", args),
            "hub_order_attempt": lambda args: self._orders("attempt", args),
            "hub_order_metrics": lambda args: self._orders("metrics", args),
            "hub_worker_capability": self._capability,
        }
