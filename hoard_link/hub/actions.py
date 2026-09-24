"""What a rule or a job *does*: a small list of actions, run in order.

Action shapes (JSON objects, ``kind`` picks one):

* ``{"kind": "tool", "app": "hypatia", "tool": "cards_suggest", "args": {...}}``
  — call a tool of an app through the family contract (``family.call_app``);
* ``{"kind": "hub", "tool": "hub_backup_run", "args": {...}}`` — one of the
  hub's own tools;
* ``{"kind": "event", "type": "digest.wanted", "data": {...}}`` — emit an event;
* ``{"kind": "start_app" | "stop_app" | "restart_app", "app": "scribe"}``;
* ``{"kind": "profile_start" | "profile_stop", "name": "video"}``.

Any string inside ``args`` / ``data`` may carry ``${path}`` placeholders
filled from the run's context: ``${event.type}``, ``${event.source}``,
``${event.data.session_id}``, ``${now}`` (epoch seconds), ``${today}``
(``YYYY-MM-DD``), ``${rule.id}`` / ``${job.id}``. A string that is exactly
one placeholder keeps the value's type (an object stays an object); a
missing path becomes ``""``. That is the whole templating language, on
purpose: a rule is a reflex, not a program — anything smarter belongs in
the assistant, which can be the target of a rule too.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Optional

from . import contract as family

_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z0-9_.\-]+)\}")
KINDS = ("tool", "hub", "event", "start_app", "stop_app", "restart_app", "profile_start", "profile_stop")


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def render(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole:
            found = _lookup(ctx, whole.group(1))
            return "" if found is None else found
        return _PLACEHOLDER.sub(lambda m: _to_text(_lookup(ctx, m.group(1))), value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def _to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def base_context(**extra: Any) -> dict[str, Any]:
    now = time.time()
    return {"now": int(now), "today": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
            "time": datetime.fromtimestamp(now).strftime("%H:%M"), **extra}


def validate(actions: Any) -> list[str]:
    """Problems with a list of actions (empty list = fine)."""
    problems: list[str] = []
    if not isinstance(actions, list) or not actions:
        return ["'then' must be a non-empty list of actions"]
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            problems.append(f"action {i}: not an object"); continue
        kind = str(a.get("kind") or "")
        if kind not in KINDS:
            problems.append(f"action {i}: unknown kind {kind!r} (one of {', '.join(KINDS)})"); continue
        if kind == "tool" and not (a.get("app") and a.get("tool")):
            problems.append(f"action {i}: 'tool' needs app and tool")
        if kind == "hub" and not a.get("tool"):
            problems.append(f"action {i}: 'hub' needs tool")
        if kind == "event" and not a.get("type"):
            problems.append(f"action {i}: 'event' needs type")
        if kind in ("start_app", "stop_app", "restart_app") and not a.get("app"):
            problems.append(f"action {i}: '{kind}' needs app")
        if kind in ("profile_start", "profile_stop") and not a.get("name"):
            problems.append(f"action {i}: '{kind}' needs name")
        if "args" in a and not isinstance(a["args"], dict):
            problems.append(f"action {i}: args must be an object")
    return problems


def run_one(hub: Any, action: dict[str, Any], ctx: dict[str, Any], *, caller: str = "hub",
            timeout: float = 120.0) -> dict[str, Any]:
    """Run a single action. Never raises; the result says what happened."""
    from . import tools as hub_tools  # late: tools imports core imports this module's siblings
    kind = str(action.get("kind") or "")
    t0 = time.monotonic()
    try:
        if kind == "tool":
            app = hub.get(str(action.get("app") or ""))
            if app is None:
                return {"kind": kind, "ok": False, "error": f"unknown app: {action.get('app')}"}
            args = render(action.get("args") or {}, ctx)
            res = family.call_app(app, str(action.get("tool")), args, timeout=timeout, caller=caller)
            return {"kind": kind, "app": app.id, "tool": action.get("tool"), **{k: v for k, v in res.items() if k != "app"}}
        if kind == "hub":
            args = render(action.get("args") or {}, ctx)
            res = hub_tools.call(hub, str(action.get("tool")), args if isinstance(args, dict) else {})
            ok = not (isinstance(res, dict) and res.get("ok") is False)
            return {"kind": kind, "tool": action.get("tool"), "ok": ok, "result": res}
        if kind == "event":
            data = render(action.get("data") or {}, ctx)
            if not isinstance(data, dict):
                data = {"value": data}
            via = ctx.get("rule", {}).get("id") if isinstance(ctx.get("rule"), dict) else None
            if via:
                data = {**data, "_via_rule": via}
            ev = hub.events.emit(str(action.get("type")), data, source=str(action.get("source") or "hub"))
            return {"kind": kind, "ok": True, "event_id": ev["id"], "type": ev["type"]}
        if kind in ("start_app", "stop_app", "restart_app"):
            fn = {"start_app": hub.start, "stop_app": hub.stop, "restart_app": hub.restart}[kind]
            res = fn(str(action.get("app") or ""))
            return {"kind": kind, "app": action.get("app"), "ok": bool(res.get("ok")), "result": res}
        if kind in ("profile_start", "profile_stop"):
            fn = hub.profile_start if kind == "profile_start" else hub.profile_stop
            res = fn(str(action.get("name") or ""))
            return {"kind": kind, "name": action.get("name"), "ok": bool(res.get("ok")), "result": res}
        return {"kind": kind, "ok": False, "error": f"unknown action kind: {kind}"}
    except Exception as exc:  # noqa: BLE001
        return {"kind": kind, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        pass


def run_all(hub: Any, actions: list[dict[str, Any]], ctx: dict[str, Any], *, caller: str = "hub",
            stop_on_error: bool = False, timeout: float = 120.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, action in enumerate(actions):
        t0 = time.monotonic()
        res = run_one(hub, action, ctx, caller=caller, timeout=timeout)
        res.setdefault("ms", int((time.monotonic() - t0) * 1000))
        res["index"] = i
        # Later actions can use earlier results: ${results.0.result.id}
        ctx.setdefault("results", []).append(res)
        out.append(res)
        if stop_on_error and not res.get("ok"):
            break
    return out


def compact_results(results: list[dict[str, Any]], max_chars: int = 400) -> list[dict[str, Any]]:
    """The results, trimmed for an event payload or a history line."""
    import json
    out = []
    for r in results:
        item = {k: r.get(k) for k in ("index", "kind", "app", "tool", "name", "type", "ok", "ms", "error", "event_id") if k in r}
        if "result" in r:
            text = json.dumps(r["result"], ensure_ascii=False, default=str)
            item["result"] = text if len(text) <= max_chars else text[:max_chars] + "…"
        out.append(item)
    return out


def describe(action: dict[str, Any]) -> str:
    kind = action.get("kind")
    if kind == "tool":
        return f"{action.get('app')}.{action.get('tool')}"
    if kind == "hub":
        return f"hub.{action.get('tool')}"
    if kind == "event":
        return f"emit {action.get('type')}"
    if kind in ("start_app", "stop_app", "restart_app"):
        return f"{kind} {action.get('app')}"
    if kind in ("profile_start", "profile_stop"):
        return f"{kind} {action.get('name')}"
    return str(kind)


def new_id(prefix: str, existing: Optional[set[str]] = None) -> str:
    import secrets
    while True:
        cand = f"{prefix}-{secrets.token_hex(3)}"
        if not existing or cand not in existing:
            return cand
