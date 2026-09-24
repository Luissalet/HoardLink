"""The agent-facing tool catalogue: one source for ``/api/agent/tools``
and for the stdio MCP bridge, so the two never drift.

First line of every description ≤ 110 characters, with the words an
English *or* Spanish request would use — that is what a tool-retrieval
index sees. Read-only tools carry ``readOnlyHint``; the ones that start,
stop or close something do not.
"""

from __future__ import annotations

from typing import Any, Callable

from .core import Hub

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

    return {
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
        "hub_lease_status": lambda a: hub.leases.status(force=bool(a.get("force", False))),
        "hub_lease_request": lease_request,
        "hub_lease_release": lambda a: _drop_status(hub.leases.release(str(a.get("lease_id") or ""))),
    }


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
