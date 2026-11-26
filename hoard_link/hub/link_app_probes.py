"""Registry-backed app catalogue and passive HTTP proxy checks."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import time

from ..atomic import read_json
from . import contract
from .link_inventory import COMPATIBILITY_OWNERS, Link
from .link_probes import FAILED, RESPONDS, UNCHECKED, Observation, read_only_without_arguments


# These existing tools report switches in the owning host without loading an
# engine. The served schema must still advertise readOnlyHint at run time.
MODULE_HEALTH_TOOLS = {
    "daguerre": "visual_modules",
    "prospero": "studio_modules",
    "vitruvius": "software_modules",
}
TOOL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def app_links(apps):
    """Expand the existing catalogue and proxy inventory links by registry ID."""
    for app in sorted(apps, key=lambda row: row.id):
        app_id = app.id
        if app_id.casefold() == "baird":
            continue
        absorbed = app.family.get("absorbed_by")
        owner = absorbed.get("id") if isinstance(absorbed, dict) else None
        excluded = isinstance(owner, str) and owner.casefold() == "baird"
        catalogue = "excluded:baird" if excluded else f"registered_catalogue:{app_id}"
        health = "excluded:baird" if excluded else f"registered_health:{app_id}"
        yield Link(f"1.2.app.{app_id}", "Hub", app_id, "hub_app_tools", catalogue, "P", "1.2")
        yield Link(f"1.3.app.{app_id}", "Hub", app_id, "Hub HTTP tool proxy", health, "C", "1.3")


def _tool_name(value):
    return isinstance(value, str) and TOOL_NAME.fullmatch(value) is not None


def _settings(app):
    value = app.family.get("link_check", {})
    return value if isinstance(value, dict) else {}


def _declared_tools(app):
    """Return the published names and whether they define the entire catalogue."""
    settings = _settings(app)
    if "tools" in settings:
        rows = settings["tools"]
        if not isinstance(rows, list) or not all(_tool_name(name) for name in rows) or len(rows) != len(set(rows)):
            return None, True
        return set(rows), True

    owner = COMPATIBILITY_OWNERS.get(app.id)
    if owner and app.manifest_path:
        bridge = read_json(Path(app.manifest_path).with_name("bridge.json"), None)
        if isinstance(bridge, dict) and bridge.get("owner") == owner:
            rows = bridge.get("tools")
            if not isinstance(rows, list):
                return None, True
            names = [row.get("schema", {}).get("name")
                     if isinstance(row, dict) and isinstance(row.get("schema"), dict) else None
                     for row in rows]
            if not all(_tool_name(name) for name in names) or len(names) != len(set(names)):
                return None, True
            return set(names), True

    controls = app.family.get("module_controls")
    names = {
        control[key]
        for control in (controls.values() if isinstance(controls, dict) else ())
        if isinstance(control, dict)
        for key in ("list", "set")
        if _tool_name(control.get(key))
    }
    return names, False


def _catalogue(app, deadline, scope):
    seconds = deadline - time.monotonic()
    called = "hub_app_tools"
    if seconds <= 0:
        return Observation(FAILED, called, "probe_timeout", scope=scope), {}
    response = contract.app_tools(app, timeout=seconds)
    status = response.get("status")
    metrics = {"http_status": status}
    if response.get("ok") is not True:
        if status == 200:
            outcome = "invalid_catalogue"
        elif status is None:
            outcome = "probe_timeout" if response.get("transport") == "timeout" else "no_http_response"
        else:
            outcome = "http_error"
        return Observation(FAILED, called, outcome, metrics, scope), {}

    rows = response["tools"]
    tools = {}
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if not _tool_name(name) or name in tools:
            return Observation(FAILED, called, "invalid_catalogue", scope=scope), {}
        tools[name] = row
    return Observation(RESPONDS, called, "http_success",
                       {"http_status": 200, "served_tools": len(tools)}, scope), tools


def _compare(app, deadline):
    observation, served = _catalogue(app, deadline, "manifest_catalogue")
    if observation.state != RESPONDS:
        return observation
    names, complete = _declared_tools(app)
    if names is None:
        return replace(observation, state=FAILED, outcome="invalid_tool_declaration")
    if not names and not complete:
        return replace(observation, state=UNCHECKED, outcome="no_tool_declaration")
    missing = names - served.keys()
    extra = served.keys() - names
    metrics = {**observation.metrics, "declared_tools": len(names), "missing_tools": len(missing)}
    if complete:
        metrics["unexpected_tools"] = len(extra)
    if missing or (complete and extra):
        return replace(observation, state=FAILED, outcome="tool_catalogue_drift", metrics=metrics)
    if not complete:
        return replace(observation, state=UNCHECKED, outcome="partial_tool_declaration", metrics=metrics)
    return replace(observation, outcome="tool_catalogue_matches", metrics=metrics)


def _health(probes, app, deadline):
    scope = "app_proxy"
    called = "Hub HTTP tool proxy"
    settings = _settings(app)
    name = settings.get("health_tool", MODULE_HEALTH_TOOLS.get(app.id))
    if not _tool_name(name) or name.casefold().startswith("baird_"):
        return Observation(UNCHECKED, called, "no_health_probe", scope=scope)

    catalogue, tools = _catalogue(app, deadline, scope)
    if catalogue.state != RESPONDS:
        return catalogue
    if not read_only_without_arguments(tools.get(name)):
        return Observation(UNCHECKED, called, "no_health_probe", scope=scope)

    called = f"{called} ({name})"
    # Recheck manual stops and the selected endpoint before sending the POST.
    target = probes._get_app(app.id, deadline)
    if isinstance(target, Observation):
        return replace(target, scope=scope)
    if (target.app.url, target.app.token_file) != (app.url, app.token_file):
        return Observation(UNCHECKED, called, "endpoint_not_verified", scope=scope)
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        return Observation(FAILED, called, "probe_timeout", scope=scope)
    response = contract.call_app(target.app, name, {}, caller="hub-link-check", timeout=seconds, shared_only=True)
    status = response.get("status")
    metrics = {"http_status": status}
    if response.get("ok") is not True:
        outcome = "http_error"
        if status is None:
            outcome = "no_http_response"
        elif 200 <= status < 300:
            outcome = "unexpected_response"
        return Observation(FAILED, called, outcome, metrics, scope)
    result = response.get("result")
    healthy = isinstance(result, dict) and result.get("ok", True) is True and not result.get("error")
    if healthy:
        if name == MODULE_HEALTH_TOOLS.get(app.id):
            healthy = isinstance(result.get("modules"), list)
        else:
            healthy = result.get("ok") is True
    return Observation(RESPONDS if healthy else FAILED, called,
                       "app_proxy_health" if healthy else "unexpected_response", metrics, scope)


def probe_registered_app(probes, probe, deadline):
    kind, _, app_id = probe.partition(":")
    health = kind == "registered_health"
    scope = "app_proxy" if health else "manifest_catalogue"
    app = probes._get_app(app_id, deadline)
    if isinstance(app, Observation):
        return replace(app, scope=scope)
    if not app.app.agent_contract:
        return Observation(UNCHECKED, "Hub registry", "no_health_probe" if health else "no_http_tool_contract",
                           scope=scope, location=app.location)
    outcome = _health(probes, app.app, deadline) if health else _compare(app.app, deadline)
    return replace(outcome, location=outcome.location or app.location)
