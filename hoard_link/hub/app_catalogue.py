"""Compact app tool discovery for workers with a declared app scope."""

import json
from typing import Any

NAME = "hub_app_tools"
SCOPE_ENV = "HOARD_WORKER_APP_SCOPE"
SCOPE_HEADER = "X-Hoard-Worker-App-Scope"
_SCOPE_ARG = "_worker_app_scope"


def _parse_scope(value: str) -> dict:
    try:
        scope = json.loads(value)
    except ValueError:
        return {}
    return scope if isinstance(scope, dict) else {}


def app_scope_header(scope: str, app: Any) -> str:
    declared = _parse_scope(scope)
    return json.dumps({app: declared[app]} if isinstance(app, str) and app in declared else {},
                      separators=(",", ":"))


def request_arguments(arguments: dict, scope_header: str | None) -> dict:
    """Keep runtime scope separate from model arguments."""
    args = {key: value for key, value in arguments.items() if key != _SCOPE_ARG}
    if scope_header is not None:
        args[_SCOPE_ARG] = _parse_scope(scope_header)
    return args


def _summary(spec: dict) -> dict:
    lines = str(spec.get("description") or "").strip().splitlines()
    schema = spec.get("inputSchema")
    required = schema.get("required") if isinstance(schema, dict) else None
    return {
        "name": spec["name"],
        "description": lines[0][:160].rstrip() if lines else "",
        "required": [name for name in required if isinstance(name, str)] if isinstance(required, list) else [],
    }


def list_tools(hub: Any, args: dict) -> dict:
    app = str(args.get("app") or "")
    tool = args.get("tool")
    if tool is not None and (not isinstance(tool, str) or not tool.strip()):
        return {"ok": False, "code": "invalid_app_tool",
                "error": "Use an exact tool ID. / Indica un identificador exacto."}

    scoped = _SCOPE_ARG in args
    allowed = None
    if scoped:
        scope = args[_SCOPE_ARG]
        allowed = scope.get(app) if isinstance(scope, dict) else None
        if not isinstance(allowed, list) or not allowed:
            return {"ok": False, "code": "worker_app_not_allowed",
                    "error": "App outside the worker scope. / Aplicación fuera del ámbito del trabajador."}
        if tool is not None and tool not in allowed:
            return {"ok": False, "code": "worker_app_tool_not_allowed",
                    "error": "Tool outside the worker scope. / Herramienta fuera del ámbito del trabajador."}

    catalogue = hub.app_tools(app)
    if not catalogue.get("ok") or (not scoped and tool is None and args.get("summary") is not True):
        return catalogue

    specs = [
        spec for spec in catalogue["tools"]
        if isinstance(spec, dict) and isinstance(spec.get("name"), str)
        and (allowed is None or spec["name"] in allowed)
        and (tool is None or spec["name"] == tool)
    ]
    if tool is not None and not specs:
        return {"ok": False, "code": "unknown_app_tool",
                "error": "Tool absent from the app catalogue. / La herramienta no figura en el catálogo."}
    entries = [_summary(spec) for spec in specs] if tool is None else specs[:1]
    return {
        "ok": True, "app": catalogue["app"], "contract": catalogue["contract"],
        "instructions": catalogue.get("instructions", ""), "tools": entries,
    }
