"""Cooperative CPU/RAM/disk admission, alongside the existing GPU arbiter.

Does not throttle or kill native processes. Applications must acquire a claim
before starting heavy work and renew/release it. Live RAM availability is
checked as well as reservations. Existing GPU leases remain authoritative.
"""
from __future__ import annotations

import os
import json
import math
from pathlib import Path
import secrets
import threading
import time

from ..atomic import write_json_atomic
from .facets import Facet


def inventory():
    free = None
    try:
        import psutil
        free = int(psutil.virtual_memory().available / 1024 ** 2)
    except ImportError:
        if os.name == "nt":
            import ctypes
            class Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            *[(n, ctypes.c_ulonglong) for n in ("total", "available", "page_total", "page_free", "virtual_total", "virtual_free", "extended")]]
            memory = Memory()
            memory.length = ctypes.sizeof(memory)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                free = int(memory.available / 1024 ** 2)
    return {"cpu_slots": max(1, os.cpu_count() or 1), "ram_available_mb": free}


class ResourcePool:
    def __init__(self, path, *, inventory_fn=inventory, clock=time.time, config=None):
        self.path, self.inventory, self.clock = Path(path), inventory_fn, clock
        self.config = config or {}
        if not isinstance(self.config, dict):
            raise ValueError("resources configuration must be an object")
        for key, low, high in (("cpu_slots", 1, 1024), ("interactive_reserve", 0, 1024), ("io_slots", 1, 16)):
            if key in self.config and (type(self.config[key]) is not int or not low <= self.config[key] <= high):
                raise ValueError(f"{key} must be an integer in {low}..{high}")
        self.lock = threading.RLock()
        saved = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"schema": 1, "claims": {}}
        if saved.get("schema") != 1 or not isinstance(saved.get("claims"), dict):
            raise ValueError("invalid resource journal; preserve it for review")
        self.claims = saved["claims"]
        for key, claim in self.claims.items():
            if not isinstance(claim, dict) or not isinstance(claim.get("id"), str) or not isinstance(claim.get("owner"), str):
                raise ValueError("invalid resource claim; preserve the journal for review")
            if key != claim["owner"] + ":" + claim["id"] or claim.get("state") not in ("queued", "granted") or claim.get("mode") not in ("interactive", "background"):
                raise ValueError("invalid resource claim identity/state; preserve the journal for review")
            if any(type(claim.get(k)) is not int or not 0 <= claim[k] <= limit for k, limit in (("cpu", 1024), ("ram", 1048576), ("io", 16))):
                raise ValueError("invalid saved resource budget; preserve the journal for review")
            if any(type(claim.get(k)) not in (int, float) or not math.isfinite(claim[k]) for k in ("created_at", "expires_at")):
                raise ValueError("invalid saved resource time; preserve the journal for review")

    def _save(self):
        write_json_atomic(self.path, {"schema": 1, "claims": self.claims})

    def _tick(self):
        now = self.clock()
        self.claims = {k: v for k, v in self.claims.items() if v["expires_at"] > now}
        machine = self.inventory()
        cpu = int(self.config.get("cpu_slots", machine["cpu_slots"]))
        reserve = min(max(0, int(self.config.get("interactive_reserve", 1))), max(0, cpu - 1))
        io_limit = max(1, int(self.config.get("io_slots", 1)))
        used = {"cpu": 0, "ram": 0, "io": 0}
        for claim in self.claims.values():
            if claim["state"] == "granted":
                for key in used:
                    used[key] += claim[key]
        blocked = False
        queued = sorted((c for c in self.claims.values() if c["state"] == "queued"),
                        key=lambda c: (c["mode"] != "interactive", c["created_at"], c["id"]))
        for claim in queued:
            cap = cpu if claim["mode"] == "interactive" else cpu - reserve
            ram = machine.get("ram_available_mb")
            fits = (used["cpu"] + claim["cpu"] <= cap and used["io"] + claim["io"] <= io_limit and
                    (not claim["ram"] or (ram is not None and used["ram"] + claim["ram"] <= max(0, ram - 256))))
            if fits and not blocked:
                claim.update(state="granted", granted_at=now)
                for key in used:
                    used[key] += claim[key]
            else:
                blocked = True  # FIFO within priority; small work cannot starve a large queued request.
        self._save()
        return {"cpu_slots": cpu, "interactive_reserve": reserve, "io_slots": io_limit,
                "ram_available_mb": machine.get("ram_available_mb"), "reserved": used}

    def request(self, args, caller):
        with self.lock:
            mode = args.get("mode", "background")
            if mode not in ("interactive", "background"):
                raise ValueError("mode must be interactive or background")
            values = {key: args.get(name, 0) for key, name in (("cpu", "cpu_slots"), ("ram", "ram_mb"), ("io", "io_slots"))}
            if any(type(v) is not int or not 0 <= v <= limit for v, limit in zip(values.values(), (1024, 1048576, 16))) or not any(values.values()):
                raise ValueError("request nonnegative integer CPU/RAM/IO resources")
            ttl = args.get("ttl_s", 60)
            if type(ttl) is not int or not 5 <= ttl <= 300:
                raise ValueError("ttl_s must be 5..300 seconds")
            uid = args.get("request_id") or secrets.token_hex(12)
            if not isinstance(uid, str) or not 1 <= len(uid) <= 128:
                raise ValueError("invalid request_id")
            key = caller + ":" + uid
            prior = self.claims.get(key)
            if prior:
                if any(prior[k] != v for k, v in values.items()) or prior["mode"] != mode:
                    raise ValueError("request_id conflicts with its original resource request")
                prior["expires_at"] = self.clock() + ttl
            else:
                machine = self.inventory()
                cap = int(self.config.get("cpu_slots", machine["cpu_slots"]))
                reserve = min(int(self.config.get("interactive_reserve", 1)), max(0, cap - 1))
                if values["cpu"] > cap - (reserve if mode == "background" else 0) or values["io"] > int(self.config.get("io_slots", 1)):
                    raise ValueError("request exceeds resource pool capacity")
                self.claims[key] = {"id": uid, "owner": caller, **values, "mode": mode, "state": "queued",
                                    "created_at": self.clock(), "expires_at": self.clock() + ttl}
            self._tick()
            return {"ok": True, "claim": dict(self.claims[key]), "cooperative": True}

    def release(self, uid, caller):
        with self.lock:
            self.claims.pop(caller + ":" + str(uid), None)
            self._tick()
            return {"ok": True}

    def status(self):
        with self.lock:
            capacities = self._tick()
            return {"ok": True, **capacities, "claims": list(self.claims.values()), "cooperative": True,
                    "gpu": "use the existing GPU lease arbiter"}


class ResourcesFacet(Facet):
    id = "resources"

    def __init__(self, hub):
        super().__init__(hub)
        self.pool = ResourcePool(Path(hub.config.data_dir) / "resource-claims.json", config=getattr(hub.config, "resources", {}))

    def get(self, req):
        if req.path == "/api/resources":
            return self.pool.status()
        return None

    def post(self, req):
        if req.path.startswith("/api/resources/"):
            caller = req.caller()
            if not caller:
                return {"ok": False, "status": 401, "error": "authentication required"}
            try:
                if req.path == "/api/resources/request":
                    return self.pool.request(req.body, caller)
                if req.path == "/api/resources/release":
                    return self.pool.release(req.body.get("request_id"), caller)
            except ValueError as exc:
                return {"ok": False, "status": 400, "error": str(exc)}
        return None

    @classmethod
    def tools(cls):
        return [{"name": "hub_resource_status", "description": "Inspect cooperative CPU/RAM/disk reservations. Existing GPU leases are separate; native processes are never killed or throttled.",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True}}]

    def handlers(self):
        return {"hub_resource_status": lambda args: self.pool.status()}
