"""The agent-facing tool catalogue: one source for ``/api/agent/tools``
and for the stdio MCP bridge, so the two never drift.

First line of every description ≤ 110 characters, with the words an
English *or* Spanish request would use — that is what a tool-retrieval
index sees. Read-only tools carry ``readOnlyHint``; the ones that start,
stop or close something do not.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .core import Hub

_PROFILE = {"type": "string", "description": "Profile name as listed by hub_profile_list."}
_APP_ID = {"type": "string", "description": "App id as listed by hub_list_apps (e.g. 'ledger', 'babel')."}


def catalogue() -> list[dict[str, Any]]:
    return [
        {
            "name": "hub_list_apps",
            "description": "List the local apps and whether each runs. Keywords: apps, list, which are open, estado, qué hay abierto.\n"
                           "Returns id, name, purpose, url, port, state (running|starting|foreign|down), pid, memory, windows, launchable.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_app_status",
            "description": "Status of one app: health, process on its port, open windows, last log lines. Keywords: estado, log.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID, "log_lines": {"type": "integer", "default": 40}},
                            "required": ["app"], "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_start_app",
            "description": "Start an app's local server and wait until it is ready. Keywords: start, launch, arrancar, iniciar, encender.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID, "wait": {"type": "boolean", "default": True}},
                            "required": ["app"], "additionalProperties": False},
        },
        {
            "name": "hub_stop_app",
            "description": "Stop an app's server and close its windows. Keywords: stop, kill, parar, cerrar, detener, apagar.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID}, "required": ["app"], "additionalProperties": False},
        },
        {
            "name": "hub_restart_app",
            "description": "Stop then start an app. Keywords: reiniciar, restart.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID}, "required": ["app"], "additionalProperties": False},
        },
        {
            "name": "hub_open_app",
            "description": "Open an app as a desktop window (default) or browser tab, starting it if needed. Keywords: abrir, ventana.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID,
                                                            "mode": {"type": "string", "enum": ["window", "browser"], "default": "window"},
                                                            "autostart": {"type": "boolean", "default": True}},
                            "required": ["app"], "additionalProperties": False},
        },
        {
            "name": "hub_close_windows",
            "description": "Close the desktop windows of an app; its server keeps running. Keywords: close window, cerrar ventana.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID}, "required": ["app"], "additionalProperties": False},
        },
        {
            "name": "hub_start_all",
            "description": "Start every launchable app that is not running. Keywords: start all, arrancar todo, encender todo.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "hub_stop_all",
            "description": "Stop every running app the hub manages. Keywords: parar todo, cerrar todo.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "hub_backends",
            "description": "Which local model server serves each capability (llm, vision, tts…) and free VRAM. Keywords: modelos, GPU.",
            "inputSchema": {"type": "object", "properties": {"force": {"type": "boolean", "default": False}}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_lease_status",
            "description": "GPU VRAM per GPU with active leases and the queue / VRAM libre, reservas y cola de la GPU.\n"
                           "Per GPU: total, used (nvidia-smi), reserved by granted leases, available. Then the granted "
                           "leases (owner, purpose, vram_mb, gpu, expires_in_s) and the queue in grant order.",
            "inputSchema": {"type": "object", "properties": {"force": {"type": "boolean", "default": False,
                                                                       "description": "Re-read nvidia-smi now."}},
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_lease_request",
            "description": "Reserve GPU memory before loading a model / reservar VRAM antes de cargar un modelo.\n"
                           "Returns lease_id and state granted (with the gpu to use) or queued (with position). "
                           "Release it with hub_lease_release when done; it expires after ttl_s otherwise.",
            "inputSchema": {"type": "object", "properties": {
                "vram_mb": {"type": "integer", "minimum": 0, "description": "MiB of VRAM needed."},
                "owner": {"type": "string", "description": "Who holds it (app id or agent name)."},
                "purpose": {"type": "string", "description": "What for, shown in the hub (e.g. 'whisper large-v3')."},
                "gpu": {"description": "GPU index, or 'any' (default).", "anyOf": [{"type": "integer"}, {"type": "string"}]},
                "priority": {"type": "integer", "default": 0, "description": "Higher is served first."},
                "ttl_s": {"type": "integer", "default": 1800, "description": "Seconds until it expires unless renewed."},
                "wait": {"type": "boolean", "default": False, "description": "Wait up to 25 s for a grant."},
            }, "required": ["vram_mb"], "additionalProperties": False},
        },
        {
            "name": "hub_lease_release",
            "description": "Release a GPU memory lease so the next one can load / liberar una reserva de VRAM de la GPU.",
            "inputSchema": {"type": "object", "properties": {"lease_id": {"type": "string"}},
                            "required": ["lease_id"], "additionalProperties": False},
        },
        {
            "name": "hub_profile_list",
            "description": "List app profiles and whether each is running. Keywords: profiles, perfiles, grupos de apps.\n"
                           "A profile is a named set of apps, external commands (e.g. a ComfyUI instance) and apps to "
                           "open as windows, started and stopped together. Returns each member's state.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_profile_start",
            "description": "Start a profile: its apps, commands and windows. Keywords: start profile, arrancar perfil.",
            "inputSchema": {"type": "object", "properties": {"name": _PROFILE}, "required": ["name"],
                            "additionalProperties": False},
        },
        {
            "name": "hub_profile_stop",
            "description": "Stop a profile's apps and the commands the hub started. Keywords: parar perfil, detener.",
            "inputSchema": {"type": "object", "properties": {"name": _PROFILE}, "required": ["name"],
                            "additionalProperties": False},
        },
        # -- the family layer: events, calls between apps, rules, jobs, backups, audit --
        {
            "name": "hub_events",
            "description": "Recent events from the app family (what happened, when, who). Keywords: eventos, qué pasó, historial.\n"
                           "Filter by type glob (scribe.*, agent.call, hub.backup.*), source (app id), since/until (epoch s), "
                           "text. Newest first unless since_id is given.",
            "inputSchema": {"type": "object", "properties": {
                "type": {"type": "string", "description": "Glob on the event type; | joins several."},
                "source": {"type": "string"}, "since_id": {"type": "integer"},
                "since": {"type": "number", "description": "Epoch seconds."}, "until": {"type": "number"},
                "text": {"type": "string", "description": "Substring in type or data."},
                "limit": {"type": "integer", "default": 50}}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_event_emit",
            "description": "Emit an event on the family bus so rules and other apps react. Keywords: emitir evento, avisar, notify.\n"
                           "type is dotted lower-case (e.g. 'digest.wanted'); data a small object with ids, never contents.",
            "inputSchema": {"type": "object", "properties": {"type": {"type": "string"}, "data": {"type": "object"}},
                            "required": ["type"], "additionalProperties": False},
        },
        {
            "name": "hub_event_stats",
            "description": "Counts of events by type and source, and the conventions. Keywords: estadísticas de eventos.",
            "inputSchema": {"type": "object", "properties": {"since": {"type": "number", "description": "Epoch seconds."}},
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_call_app",
            "description": "Call a tool of another app by id through the hub (it uses that app's token). Keywords: llamar, usar app.\n"
                           "hub_app_tools lists what an app offers. Prefer the app's own MCP tools when they are connected; "
                           "this is for apps that are not, and for rules.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID, "tool": {"type": "string"},
                                                            "arguments": {"type": "object"},
                                                            "timeout_s": {"type": "number", "default": 120}},
                            "required": ["app", "tool"], "additionalProperties": False},
        },
        {
            "name": "hub_app_tools",
            "description": "The tool catalogue of one app (names, descriptions, schemas). Keywords: qué sabe hacer, herramientas de.",
            "inputSchema": {"type": "object", "properties": {"app": _APP_ID}, "required": ["app"], "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_rules",
            "description": "List automation rules (when an event → then actions) and their last runs. Keywords: reglas, automatizaciones.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_rule_add",
            "description": "Add an automation: when an event matches, run actions. Keywords: crear regla, cuando pase X haz Y.\n"
                           "when: {type: glob, source?, where?: {\"data.key\": value}}. then: list of actions "
                           "{kind: tool, app, tool, args} | {kind: hub, tool, args} | {kind: event, type, data} | "
                           "{kind: start_app|stop_app|restart_app, app} | {kind: profile_start|profile_stop, name}. "
                           "Strings may use ${event.data.x}, ${event.type}, ${today}, ${now}.",
            "inputSchema": {"type": "object", "properties": {
                "name": {"type": "string"}, "when": {"type": "object"}, "then": {"type": "array", "items": {"type": "object"}},
                "cooldown_s": {"type": "number", "default": 5}, "enabled": {"type": "boolean", "default": True},
                "note": {"type": "string"}}, "required": ["when", "then"], "additionalProperties": False},
        },
        {
            "name": "hub_rule_update",
            "description": "Change or enable/disable a rule by id. Keywords: editar regla, desactivar regla, activar.",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                                                            "when": {"type": "object"}, "then": {"type": "array"},
                                                            "cooldown_s": {"type": "number"}, "enabled": {"type": "boolean"},
                                                            "note": {"type": "string"}},
                            "required": ["id"], "additionalProperties": False},
        },
        {
            "name": "hub_rule_remove",
            "description": "Delete a rule by id. Keywords: borrar regla, quitar automatización.",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
        },
        {
            "name": "hub_rule_run",
            "description": "Run a rule now against an event (a past event_id or a synthetic type+data). Keywords: probar regla.",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "event_id": {"type": "integer"},
                                                            "type": {"type": "string"}, "data": {"type": "object"}},
                            "required": ["id"], "additionalProperties": False},
        },
        {
            "name": "hub_jobs",
            "description": "List scheduled jobs (every 6h / at 04:00) with next and last runs. Keywords: tareas programadas, cron.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_job_add",
            "description": "Schedule actions: every '30m'/'6h'/'1d', or daily at 'HH:MM' (days optional). Keywords: programar, cada noche.\n"
                           "then: the same action list as rules (${today}, ${now} available).",
            "inputSchema": {"type": "object", "properties": {
                "name": {"type": "string"}, "every": {"type": "string"}, "at": {"type": "string"},
                "days": {"type": "array", "items": {"type": "string"}}, "then": {"type": "array", "items": {"type": "object"}},
                "enabled": {"type": "boolean", "default": True}, "catch_up": {"type": "boolean", "default": True},
                "note": {"type": "string"}}, "required": ["then"], "additionalProperties": False},
        },
        {
            "name": "hub_job_update",
            "description": "Change, enable or disable a scheduled job by id. Keywords: editar tarea programada.",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                                                            "every": {"type": "string"}, "at": {"type": "string"},
                                                            "days": {"type": "array"}, "then": {"type": "array"},
                                                            "enabled": {"type": "boolean"}, "catch_up": {"type": "boolean"},
                                                            "note": {"type": "string"}},
                            "required": ["id"], "additionalProperties": False},
        },
        {
            "name": "hub_job_remove",
            "description": "Delete a scheduled job by id. Keywords: borrar tarea programada.",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
        },
        {
            "name": "hub_job_run",
            "description": "Run a scheduled job right now. Keywords: ejecutar ahora, lanzar tarea.",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
        },
        {
            "name": "hub_backup_run",
            "description": "Back up every app's data folder (deduplicated snapshot). Keywords: copia de seguridad, backup, guardar todo.\n"
                           "apps: ids to include (default all + the hub). SQLite files are copied consistently even while in use.",
            "inputSchema": {"type": "object", "properties": {"apps": {"type": "array", "items": {"type": "string"}},
                                                            "label": {"type": "string"}}, "additionalProperties": False},
        },
        {
            "name": "hub_backup_status",
            "description": "Backup store: snapshots, sizes, last run, what each snapshot contains. Keywords: copias, backups, snapshots.",
            "inputSchema": {"type": "object", "properties": {"snapshot": {"type": "string", "description": "Detail of one snapshot."}},
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_backup_restore",
            "description": "Restore one app's data from a snapshot to a side folder, or in place when stopped. Keywords: restaurar.",
            "inputSchema": {"type": "object", "properties": {"snapshot": {"type": "string"}, "app": {"type": "string"},
                                                            "dest": {"type": "string"}, "in_place": {"type": "boolean", "default": False}},
                            "required": ["snapshot", "app"], "additionalProperties": False},
        },
        {
            "name": "hub_backup_verify",
            "description": "Re-hash every file a snapshot references (default: the latest). Keywords: verificar copia, comprobar backup.",
            "inputSchema": {"type": "object", "properties": {"snapshot": {"type": "string"}}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_backup_prune",
            "description": "Keep the last N snapshots and delete unreferenced files from the store. Keywords: limpiar copias antiguas.",
            "inputSchema": {"type": "object", "properties": {"keep": {"type": "integer", "default": 14}}, "additionalProperties": False},
        },
        {
            "name": "hub_family_audit",
            "description": "Audit the app family: contract, tokens, events, vendored library, data folders. Keywords: auditoría.\n"
                           "Returns a summary with recommendations and one line per app.",
            "inputSchema": {"type": "object", "properties": {"probe": {"type": "boolean", "default": True,
                                                                       "description": "Also query running apps (slower)."}},
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hub_rescan",
            "description": "Re-read the app folders for new or removed manifests. Keywords: rescan, refresh list, actualizar lista.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def handlers(hub: Hub) -> dict[str, Callable[[dict[str, Any]], Any]]:
    def list_apps(_: dict[str, Any]) -> Any:
        snap = hub.snapshot()
        return {"apps": [_compact(a) for a in snap["apps"]], "counts": snap["counts"], "faustus": snap["faustus"]}

    def app_status(args: dict[str, Any]) -> Any:
        app = hub.get(str(args.get("app", "")))
        if app is None:
            return {"ok": False, "error": f"unknown app: {args.get('app')}"}
        d = hub.app_status(app)
        d["log_tail"] = hub.log_tail(app.id, int(args.get("log_lines") or 40)).get("lines", [])
        return d

    def lease_request(a: dict[str, Any]) -> Any:
        return _drop_status(hub.leases.request(
            owner=str(a.get("owner") or "agent"), purpose=str(a.get("purpose") or ""), vram_mb=a.get("vram_mb", 0),
            gpu=a.get("gpu"), priority=a.get("priority", 0), ttl_s=a.get("ttl_s"), wait=bool(a.get("wait", False))))

    def events(a: dict[str, Any]) -> Any:
        evs = hub.events.query(since_id=int(a.get("since_id") or 0), type=a.get("type"), source=a.get("source"),
                               since_ts=a.get("since"), until_ts=a.get("until"), text=a.get("text"),
                               limit=int(a.get("limit") or 50))
        return {"ok": True, "last_id": hub.events.last_id, "count": len(evs), "events": evs}

    def event_emit(a: dict[str, Any]) -> Any:
        try:
            return {"ok": True, "event": hub.events.emit(str(a.get("type") or ""), a.get("data") or {}, source="hub")}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def call_app(a: dict[str, Any]) -> Any:
        try:
            timeout = float(a.get("timeout_s") or 120)
        except (TypeError, ValueError):
            timeout = 120.0
        return hub.call_app(str(a.get("app") or ""), str(a.get("tool") or ""), a.get("arguments") or {},
                            caller="hub-tool", timeout=max(1.0, min(timeout, 900.0)))

    def rule_run(a: dict[str, Any]) -> Any:
        rule = hub.rules.get(str(a.get("id") or ""))
        if rule is None:
            return {"ok": False, "error": f"unknown rule: {a.get('id')}"}
        ev = hub.events.get(int(a["event_id"])) if a.get("event_id") else None
        if ev is None:
            ev = {"id": None, "ts": time.time(), "type": str(a.get("type") or "manual.test"), "source": "hub",
                  "data": a.get("data") or {}}
        return hub.rules.run(rule, ev, manual=True)

    def backup_status(a: dict[str, Any]) -> Any:
        if a.get("snapshot"):
            m = hub.backups.load_snapshot(str(a["snapshot"]))
            if m is None:
                return {"ok": False, "error": "unknown snapshot"}
            return {"ok": True, "snapshot": {**m, "apps": {k: {"folder": v.get("folder"), "totals": v.get("totals"),
                                                              "skipped": (v.get("skipped") or [])[:20], "errors": v.get("errors")}
                                                          for k, v in (m.get("apps") or {}).items()}}}
        st = hub.backups.status()
        st["snapshots"] = hub.backups.list_snapshots()[-20:]
        st["sources"] = hub.backup_sources()
        return {"ok": True, **st}

    def audit_(a: dict[str, Any]) -> Any:
        rep = hub.family_audit(probe=bool(a.get("probe", True)))
        rep["apps"] = [{k: v for k, v in l.items() if k in ("id", "name", "state", "contract", "token_present", "events",
                                                            "vendored_hoard_link", "vendored_lags", "stack", "data_dir",
                                                            "data_dir_exists", "data_gitignored", "data_size", "tools")}
                       for l in rep["apps"]]
        return rep

    return {
        "hub_events": events,
        "hub_event_emit": event_emit,
        "hub_event_stats": lambda a: {"ok": True, **hub.events.stats(a.get("since"))},
        "hub_call_app": call_app,
        "hub_app_tools": lambda a: hub.app_tools(str(a.get("app") or "")),
        "hub_rules": lambda _: {"ok": True, "rules": hub.rules.list(), "history": hub.rules.history[-20:]},
        "hub_rule_add": lambda a: hub.rules.add(a),
        "hub_rule_update": lambda a: hub.rules.update(str(a.get("id") or ""), a),
        "hub_rule_remove": lambda a: hub.rules.remove(str(a.get("id") or "")),
        "hub_rule_run": rule_run,
        "hub_jobs": lambda _: {"ok": True, "jobs": hub.jobs.list(), "history": hub.jobs.history[-20:]},
        "hub_job_add": lambda a: hub.jobs.add(a),
        "hub_job_update": lambda a: hub.jobs.update(str(a.get("id") or ""), a),
        "hub_job_remove": lambda a: hub.jobs.remove(str(a.get("id") or "")),
        "hub_job_run": lambda a: hub.jobs.run_now(str(a.get("id") or "")),
        "hub_backup_run": lambda a: hub.backup_run(a.get("apps") or None, label=str(a.get("label") or "")),
        "hub_backup_status": backup_status,
        "hub_backup_restore": lambda a: hub.backup_restore(str(a.get("snapshot") or ""), str(a.get("app") or ""),
                                                           dest=a.get("dest"), in_place=bool(a.get("in_place", False))),
        "hub_backup_verify": lambda a: hub.backups.verify(a.get("snapshot")),
        "hub_backup_prune": lambda a: hub.backups.prune(int(a.get("keep") or 14)),
        "hub_family_audit": audit_,
        "hub_list_apps": list_apps,
        "hub_app_status": app_status,
        "hub_start_app": lambda a: hub.start(str(a.get("app", "")), wait=bool(a.get("wait", True))),
        "hub_stop_app": lambda a: hub.stop(str(a.get("app", ""))),
        "hub_restart_app": lambda a: hub.restart(str(a.get("app", ""))),
        "hub_open_app": lambda a: hub.open(str(a.get("app", "")), mode=str(a.get("mode") or "window"),
                                          autostart=bool(a.get("autostart", True))),
        "hub_close_windows": lambda a: hub.close_windows(str(a.get("app", ""))),
        "hub_start_all": lambda _: hub.start_all(),
        "hub_stop_all": lambda _: hub.stop_all(),
        "hub_backends": lambda a: hub.backends(force=bool(a.get("force", False))),
        "hub_rescan": lambda _: {"ok": True, "apps": [a.id for a in hub.rescan()]},
        "hub_profile_list": lambda _: _compact_profiles(hub.profiles_status()),
        "hub_profile_start": lambda a: hub.profile_start(str(a.get("name") or "")),
        "hub_profile_stop": lambda a: hub.profile_stop(str(a.get("name") or "")),
        "hub_lease_status": lambda a: hub.leases.status(force=bool(a.get("force", False))),
        "hub_lease_request": lease_request,
        "hub_lease_release": lambda a: _drop_status(hub.leases.release(str(a.get("lease_id") or ""))),
    }


def _compact_profiles(st: dict[str, Any]) -> dict[str, Any]:
    return {"profiles": [{"name": p["name"], "state": p["state"], "running": p["running"], "total": p["total"],
                          "members": [{k: m.get(k) for k in ("id", "kind", "name", "state")} for m in p["members"]],
                          "desktop": p["desktop"]} for p in st["profiles"]],
            "problems": st.get("problems", [])}


def _drop_status(res: dict[str, Any]) -> dict[str, Any]:
    res.pop("status", None)
    return res


def _compact(a: dict[str, Any]) -> dict[str, Any]:
    proc = a.get("process") or {}
    return {
        "id": a["id"], "name": a["name"], "purpose": a["purpose"], "url": a["url"], "port": a["port"],
        "state": a["state"], "pid": proc.get("pid"), "rss_mb": proc.get("rss_mb"), "uptime_s": proc.get("uptime_s"),
        "windows": len(a.get("windows") or []), "launchable": a["launchable"],
        "launch_reason": a["launch_reason"] if not a["launchable"] else "",
    }


def call(hub: Hub, name: str, arguments: dict[str, Any]) -> Any:
    fn = handlers(hub).get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    try:
        return fn(arguments or {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
