"""Declared production handoffs, proxy checks and native module controls."""

from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from .dispatch_proxy import probe_catalog
from .facets import Facet


def _tool(name, description, properties=None, required=(), *, read=True):
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties or {},
                        "required": list(required), "additionalProperties": False},
        "annotations": {"readOnlyHint": read, "destructiveHint": False},
    }


def _chains(stations, source, target, limit):
    edges = defaultdict(list)
    inputs = defaultdict(set)
    for station in stations:
        for handoff in station["handoffs"]:
            for piece in handoff["produces"]:
                step = {"app": station["app"], "accepts": handoff["accepts"], "produces": piece,
                        "skill": handoff["skill"], "check": handoff["check"]}
                if piece in handoff.get("piece_checks", {}):
                    step["piece_check"] = handoff["piece_checks"][piece]
                edges[step["accepts"]].append(step)
                inputs[piece].add(step["accepts"])

    linked = {target}
    pending_types = deque([target])
    while pending_types:
        for piece in inputs[pending_types.popleft()] - linked:
            linked.add(piece)
            pending_types.append(piece)
    if source not in linked:
        return []
    paths = []
    pending = deque([(source, [], {source})])
    while pending and len(paths) < limit:
        piece, steps, seen = pending.popleft()
        if piece == target:
            paths.append(steps)
            continue
        for step in edges[piece]:
            produced = step["produces"]
            if produced in linked and produced not in seen:
                pending.append((produced, [*steps, step], seen | {produced}))
    return paths


class ProductionFacet(Facet):
    id = "production"

    def __init__(self, hub):
        super().__init__(hub)
        self._switch_locks = {}

    def catalog(self, args):
        from .piece_checks import descriptor

        app_filter = args.get("app")
        stations = []
        for app in sorted(self.hub.apps, key=lambda app: app.id):
            declaration = app.family.get("production")
            if (not isinstance(declaration, dict) or
                    (app_filter and app_filter != app.id)):
                continue
            raw_handoffs = declaration.get("handoffs")
            handoffs = [
                row for row in raw_handoffs
                if isinstance(row, dict) and isinstance(row.get("accepts"), str)
                and isinstance(row.get("produces"), list) and row["produces"]
                and all(isinstance(piece, str) for piece in row["produces"])
                and isinstance(row.get("skill"), str) and isinstance(row.get("check"), dict)
            ] if isinstance(raw_handoffs, list) else []
            if handoffs:
                stations.append({"app": app.id, "name": app.name, "handoffs": [
                    {**row, "piece_checks": {kind: check for kind in [row["accepts"], *row["produces"]]
                                              if (check := descriptor(kind)) is not None}}
                    for row in handoffs]})

        result = {"ok": True, "stations": stations}
        source, target = args.get("from_type"), args.get("to_type")
        if source or target:
            if not source or not target:
                return {"ok": False, "error": "Supply both from_type and to_type"}
            limit = max(1, min(20, int(args.get("limit", 5))))
            result.update(from_type=source, to_type=target,
                          chains=_chains(stations, source, target, limit))
        return result

    def proxy_check(self, args):
        inventory = self.hub.facet("capabilities")
        apps = sorted(inventory.inventory() if inventory else self.hub.apps, key=lambda app: app.id)
        if args.get("app"):
            apps = [app for app in apps if app.id == args["app"]]
            if not apps:
                return {"ok": False, "status": 404, "error": "Unknown app"}
        timeout = args.get("timeout_s", 8)
        if type(timeout) not in (int, float) or not 2 <= timeout <= 30:
            return {"ok": False, "status": 400, "error": "timeout_s must be between 2 and 30"}

        def probe(app):
            if self.hub.get(app.id) is None:
                return {"app": app.id, "reachable": False, "status": None,
                        "state": "no_proxy", "responding": False,
                        "contract": "stdio" if app.kind == "mcp-only" else "unknown",
                        "error": "No HTTP app proxy route"}
            return probe_catalog(app, timeout_s=timeout)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(probe, apps))
        return {"ok": True, "apps": results,
                "slow": [row["app"] for row in results if row["state"] == "slow"],
                "unreachable": [row["app"] for row in results if not row["reachable"] and row["state"] != "slow"]}

    def modules(self, args):
        app_id = args.get("app")
        app = self.hub.get(app_id)
        controls = app.family.get("module_controls") if app else None
        if not isinstance(controls, dict) or not controls:
            return {"ok": False, "error": f"No module controls declared for {app_id}"}
        group_filter = args.get("group")
        if group_filter and group_filter not in controls:
            return {"ok": False, "error": f"Unknown module group: {group_filter}"}
        modules, errors = [], []
        for group, control in controls.items():
            if group_filter and group != group_filter:
                continue
            if not isinstance(control, dict) or not control.get("list") or not control.get("set"):
                errors.append({"group": group, "error": "Invalid module controls"})
                continue
            result = self.hub.call_app(app_id, control["list"], {}, timeout=10)
            body = result.get("result")
            rows = body.get("modules") if isinstance(body, dict) else None
            if not result["ok"] or not isinstance(rows, list):
                errors.append({"group": group, "status": result.get("status"),
                               "error": result.get("error") or "Invalid module status"})
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = row.get(control.get("name_field", "module"))
                if isinstance(name, str) and isinstance(row.get("enabled"), bool):
                    modules.append({**row, "module": name, "group": group})
        return {"ok": not errors, "app": app_id, "modules": modules, "errors": errors}

    def module_set(self, args):
        app_id, name, group = (args.get(key) for key in ("app", "module", "group"))
        enabled = args.get("enabled")
        expected = args.get("expected_enabled")
        automatic = args.get("automatic")
        reason = args.get("reason")
        if (not isinstance(enabled, bool) or (expected is not None and not isinstance(expected, bool))
                or (automatic is not None and not isinstance(automatic, bool))
                or not isinstance(reason, str) or not reason.strip()):
            return {"ok": False, "error": "Supply a boolean enabled value and a reason"}

        app = self.hub.get(app_id)
        if app is None:
            return {"ok": False, "error": f"Unknown app: {app_id}"}
        with self._switch_locks.setdefault(app.id, Lock()):
            status = self.modules({"app": app_id, "group": group})
            if not status["ok"]:
                return status
            matches = [row for row in status["modules"] if row["module"] == name]
            if len(matches) != 1:
                return {"ok": False, "error": f"Choose one module and group for {name}"}
            before = matches[0]
            group = before["group"]
            previous = before["enabled"]
            if expected is not None and expected != previous:
                return {"ok": False, "error": "Module state changed", "module": before}
            if previous == enabled:
                return {"ok": True, "changed": False, "module": before}

            control = app.family["module_controls"][group]
            values = {"module": name, "enabled": enabled}
            previous_automatic = before.get("automatic")
            if control.get("automatic", True):
                if automatic is not None:
                    values["automatic"] = automatic
                elif isinstance(previous_automatic, bool):
                    values["automatic"] = previous_automatic
            result = self.hub.call_app(app_id, control["set"], values, reason=reason,
                                       caller="hub-tool")
            if not result["ok"]:
                return result
            native = result["result"]
            if isinstance(native, dict) and native.get("ok") is False:
                return {"ok": False, "error": native.get("error", "Module switch failed")}
            undo = {"app": app_id, "group": group, "module": name, "enabled": previous,
                    "expected_enabled": enabled, "reason": f"Restore module state: {name}"}
            if isinstance(previous_automatic, bool) and control.get("automatic", True):
                undo["automatic"] = previous_automatic
            return {"ok": True, "changed": True, "result": native,
                    "undo": {"tool": "hub_module_set", "arguments": undo}}

    @classmethod
    def tools(cls):
        string, boolean = {"type": "string", "minLength": 1}, {"type": "boolean"}
        return [
            _tool("hub_station_catalog", "List production handoffs and plan chains. Ver puestos y proponer cadenas.",
                  {"app": string, "from_type": string, "to_type": string,
                   "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}),
            _tool("hub_proxy_check", "Check which app catalogues respond and how fast / comprueba qué catálogos responden.",
                  {"app": {"type": "string"}, "timeout_s": {"type": "number", "minimum": 2, "maximum": 30}}),
            _tool("hub_modules", "List native module switches. Consultar módulos.",
                  {"app": string, "group": string}, ("app",)),
            _tool("hub_module_set", "Toggle a native module and return undo. Activar o desactivar un módulo y devolver cómo deshacerlo.",
                  {"app": string, "group": string, "module": string, "enabled": boolean,
                   "expected_enabled": boolean, "reason": string,
                   "automatic": {"type": "boolean", "description": "Restore the automatic setting from an undo request."}},
                  ("app", "module", "enabled", "reason"), read=False),
        ]

    def handlers(self):
        return {"hub_station_catalog": self.catalog, "hub_proxy_check": self.proxy_check,
                "hub_modules": self.modules, "hub_module_set": self.module_set}
