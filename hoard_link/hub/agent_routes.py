"""Named HTTP entry points for the Hub's existing agent tool contract."""

from . import tools
from .order_receipts import CALL, CONTEXT

PREFIX = "/api/agent/tools/"


def describe(path):
    name = path.removeprefix(PREFIX)
    return next((tool for tool in tools.all_tools() if tool["name"] == name), None)


def capture(hub, context, app, tool, arguments, invoke):
    facet = hub.facet("order_receipts") if context is not None else None
    if facet is None:
        return invoke(), {}
    try:
        scope = facet.context(context, app)
    except ValueError as exc:
        return {"ok": False, "status": 400, "error": str(exc)}, {}
    return facet.capture(scope, app, tool, arguments, invoke)


def invoke(hub, name, arguments):
    context = arguments.get(CONTEXT)
    meta = {}
    if context is not None and name != CALL:
        arguments = {key: value for key, value in arguments.items() if key != CONTEXT}
        native = name == "hub_call_app" and isinstance(arguments.get("arguments", {}), dict)
        app = str(arguments.get("app") or "") if native else ""
        tool = str(arguments.get("tool") or "") if native else name
        saved = arguments.get("arguments", {}) if native else arguments
        result, meta = capture(hub, context, app, tool, saved, lambda: tools.call(hub, name, arguments))
    else:
        result = tools.call(hub, name, {key: value for key, value in arguments.items() if key != CONTEXT})
    ok = not (isinstance(result, dict) and result.get("ok") is False)
    return {"ok": ok, "tool": name, "result": result, **meta}, 200 if ok else 400
