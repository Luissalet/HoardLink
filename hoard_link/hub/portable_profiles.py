"""Portable exchange for Hub launch profiles.

The package contains only profile configuration. It never contains runtime
state, process identifiers, command logs, or credential-like environment
values, and importing it only writes ``hub.json``.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .profiles import slug

FORMAT = "hoardlink.launch-profile"
VERSION = 1
_SECRET_ENV = re.compile(r"(?:PASS(?:WORD)?|TOKEN|API[_-]?KEY|SECRET|CREDENTIAL|AUTH|AUTHORIZATION|BEARER|COOKIE|PRIVATE[_-]?KEY)", re.I)
_NON_SECRET_ENV_FLAGS = {"AUTH_ENABLED", "COOKIE_SECURE"}
_SECRET_ARG = re.compile(r"(?i)(?:^|\s)--?(?:password|token|secret|api[-_]?key|client[-_]?secret|authorization)(?:=|\s+)([^\s\"']+)")
_ENV_REF = re.compile(r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*}?|%[A-Za-z_][A-Za-z0-9_]*%|\{[A-Za-z_][A-Za-z0-9_]*\})$")


def _as_command_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("commands must be an array")
    result = []
    for i, item in enumerate(value, 1):
        if not isinstance(item, dict) or not item.get("cmd"):
            raise ValueError(f"command #{i} needs a nonempty cmd")
        if item.get("name") is not None and not isinstance(item["name"], str):
            raise ValueError(f"command #{i} name must be a string")
        cmd = item["cmd"]
        if not isinstance(cmd, str) and not (isinstance(cmd, list) and cmd and all(isinstance(part, str) for part in cmd)):
            raise ValueError(f"command #{i} cmd must be a string or nonempty string array")
        env = item.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in env.items()):
            raise ValueError(f"command #{i} env must be an object of string keys and scalar values")
        if item.get("cwd") is not None and not isinstance(item["cwd"], str):
            raise ValueError(f"command #{i} cwd must be a string or null")
        if item.get("health") is not None and not isinstance(item["health"], str):
            raise ValueError(f"command #{i} health must be a string or null")
        result.append({
            "name": str(item.get("name") or f"command {i}"),
            "cmd": copy.deepcopy(cmd),
            "cwd": str(item["cwd"]) if item.get("cwd") is not None else None,
            "health": str(item["health"]) if item.get("health") is not None else None,
            "env": {str(k): str(v) for k, v in env.items()},
        })
    return result


def _is_secret_env_key(key: str) -> bool:
    # These are operational booleans, not credentials. Keep them portable.
    if key.upper() in _NON_SECRET_ENV_FLAGS:
        return False
    return bool(_SECRET_ENV.search(key))


def export_profile(hub, name: str) -> dict[str, Any]:
    """Return a JSON-ready portable profile; never consults live process status."""
    raw = hub.config.profiles.get(name)
    if not isinstance(raw, dict):
        raise ValueError(f"unknown profile: {name}")
    app_lists = {}
    for key in ("apps", "desktop"):
        value = raw.get(key, []) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(app_id, str) for app_id in value):
            raise ValueError(f"profile {name!r} has invalid {key}; expected app ID strings")
        app_lists[key] = list(value)
    commands = _as_command_list(raw.get("commands", []) or [])
    for command in commands:
        rendered = command["cmd"] if isinstance(command["cmd"], str) else " ".join(command["cmd"])
        match = _SECRET_ARG.search(rendered)
        if match and not _ENV_REF.fullmatch(match.group(1)):
            raise ValueError(f"command {command['name']!r} may contain an inline credential; replace it with an environment variable before export")
        if command["health"]:
            url = urlsplit(command["health"])
            if url.username or url.password or any(_SECRET_ENV.search(key) for key, _value in parse_qsl(url.query)):
                raise ValueError(f"command {command['name']!r} health URL may contain a credential; remove it before export")

    redacted = []
    for command in commands:
        omitted = sorted(key for key in command["env"] if _is_secret_env_key(key))
        if omitted:
            redacted.append({"command": command["name"], "keys": omitted})
            command["env"] = {key: value for key, value in command["env"].items() if key not in omitted}

    app_ids = list(dict.fromkeys(app_lists["apps"] + app_lists["desktop"]))
    dependencies = []
    for app_id in app_ids:
        app = hub.get(app_id)
        dependencies.append({"id": app_id, "name": app.name if app else None, "available_on_source": app is not None})

    return {
        "format": FORMAT,
        "version": VERSION,
        "profile": {"name": name, "apps": app_lists["apps"], "desktop": app_lists["desktop"], "commands": commands},
        "dependencies": {"apps": dependencies},
        "portability": {
            "machine_specific_fields": ["profile.commands[].cwd", "profile.commands[].health"],
            "redacted_environment": redacted,
            "runtime_state_included": False,
        },
    }


def _plan(hub, document: Any, *, name: str | None = None, app_map: Any = None,
          command_overrides: Any = None, replace: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if (not isinstance(document, dict) or document.get("format") != FORMAT or
            type(document.get("version")) is not int or document.get("version") != VERSION):
        return {"ok": False, "can_import": False, "errors": [f"expected {FORMAT} version {VERSION}"], "warnings": []}
    if set(document) - {"format", "version", "profile", "dependencies", "portability"}:
        return {"ok": False, "can_import": False, "errors": ["package contains unsupported top-level fields"], "warnings": []}
    if not isinstance(replace, bool):
        return {"ok": False, "can_import": False,
                "errors": ["replace must be a boolean"], "warnings": []}
    profile = document.get("profile")
    if not isinstance(profile, dict):
        return {"ok": False, "can_import": False, "errors": ["profile must be an object"], "warnings": []}
    source_name = profile.get("name")
    target_name = name if name is not None else source_name
    if not isinstance(target_name, str) or not target_name.strip():
        errors.append("profile name must be a nonempty string")
    if set(profile) - {"name", "apps", "desktop", "commands"}:
        errors.append("profile contains unsupported fields")
    portability = document.get("portability") or {}
    if not isinstance(portability, dict):
        errors.append("portability metadata must be an object")
        portability = {}

    apps, desktop = profile.get("apps", []), profile.get("desktop", [])
    if not isinstance(apps, list) or not all(isinstance(x, str) and x for x in apps):
        errors.append("profile.apps must be an array of nonempty app IDs")
        apps = []
    if not isinstance(desktop, list) or not all(isinstance(x, str) and x for x in desktop):
        errors.append("profile.desktop must be an array of nonempty app IDs")
        desktop = []
    if len(set(apps)) != len(apps) or len(set(desktop)) != len(desktop):
        errors.append("app and desktop lists cannot contain duplicate IDs")
    if app_map is None:
        app_map = {}
    if not isinstance(app_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in app_map.items()):
        errors.append("app_map must map source app ID strings to local app ID strings")
        app_map = {}
    if set(app_map) - set(apps + desktop):
        errors.append("app_map contains IDs that are not profile dependencies")
    resolved: dict[str, str] = {}
    dependencies = []
    for source_id in dict.fromkeys(apps + desktop):
        local_id = app_map.get(source_id, source_id)
        app = hub.get(local_id)
        dependencies.append({"source_id": source_id, "local_id": local_id, "available": app is not None,
                             "local_name": app.name if app else None})
        if app is None:
            errors.append(f"unresolved app dependency {source_id!r}; map it to a registered local app ID")
        else:
            resolved[source_id] = local_id
    mapped_apps = [resolved[x] for x in apps if x in resolved]
    mapped_desktop = [resolved[x] for x in desktop if x in resolved]
    if len(set(mapped_apps)) != len(mapped_apps) or len(set(mapped_desktop)) != len(mapped_desktop):
        errors.append("app mapping collapses multiple source IDs to duplicate local IDs")

    try:
        commands = _as_command_list(profile.get("commands", []) or [])
    except ValueError as exc:
        commands = []
        errors.append(str(exc))
    for command in commands:
        if any(_is_secret_env_key(key) for key in command["env"]):
            errors.append(f"package command {command['name']!r} contains a credential-like environment value; re-export to redact it")
    if command_overrides is None:
        command_overrides = {}
    if not isinstance(command_overrides, dict):
        errors.append("command_overrides must be an object keyed by command name")
        command_overrides = {}
    known_names = {command["name"] for command in commands}
    for unknown in set(command_overrides) - known_names:
        errors.append(f"command override refers to unknown command {unknown!r}")
    names = set()
    command_ids = set()
    redacted_rows = portability.get("redacted_environment", [])
    if not isinstance(redacted_rows, list):
        errors.append("portability.redacted_environment must be an array")
        redacted_rows = []
    redacted = {}
    for row in redacted_rows:
        if (not isinstance(row, dict) or not isinstance(row.get("command"), str) or
                not isinstance(row.get("keys"), list) or not all(isinstance(key, str) for key in row["keys"])):
            errors.append("each portability.redacted_environment item needs a command name and string key array")
            continue
        redacted[row["command"]] = set(row["keys"])
    missing_env = []
    machine_fields = []
    normalized_commands = []
    for command in commands:
        command_name = command["name"]
        if not command_name or command_name in names:
            errors.append(f"command names must be nonempty and unique: {command_name!r}")
        names.add(command_name)
        command_id = f"{slug(target_name)}--{slug(command_name)}" if isinstance(target_name, str) else ""
        if command_id in command_ids:
            errors.append(f"command names collide after Hub ID normalization: {command_name!r}")
        command_ids.add(command_id)
        override = command_overrides.get(command_name, {})
        if not isinstance(override, dict) or set(override) - {"cmd", "cwd", "health", "env"}:
            errors.append(f"command override for {command_name!r} may contain only cmd, cwd, health and env")
            override = {}
        env = dict(command.get("env", {}))
        for key, value in override.items():
            if key != "env":
                command[key] = copy.deepcopy(value)
        supplied_env = override.get("env", {})
        if not isinstance(supplied_env, dict):
            errors.append(f"command {command_name!r} env override must be an object")
            supplied_env = {}
        env.update(supplied_env)
        for key in ("cwd", "health"):
            if command.get(key) is not None and not isinstance(command[key], str):
                errors.append(f"command {command_name!r} {key} must be a string or null")
                command[key] = None
        if "cmd" in override:
            replacement_cmd = override["cmd"]
            if not isinstance(replacement_cmd, str) and not (isinstance(replacement_cmd, list) and replacement_cmd and all(isinstance(part, str) for part in replacement_cmd)):
                errors.append(f"command {command_name!r} cmd override must be a string or nonempty string array")
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in env.items()):
            errors.append(f"command {command_name!r} env override must contain scalar values")
            env = {}
        env = {str(k): str(v) for k, v in env.items()}
        for key in redacted.get(command_name, set()):
            if key not in env:
                missing_env.append({"command": command_name, "key": key})
        if command.get("cwd"):
            field = f"command {command_name!r}.cwd"
            machine_fields.append(field)
            try:
                cwd_exists = Path(command["cwd"]).exists()
            except (OSError, ValueError):
                cwd_exists = False
            if not cwd_exists:
                warnings.append(f"{field} does not exist on this computer")
        if command.get("health"):
            machine_fields.append(f"command {command_name!r}.health")
        cmd = command.get("cmd")
        if not cmd or (not isinstance(cmd, str) and not (isinstance(cmd, list) and cmd and all(isinstance(part, str) for part in cmd))):
            errors.append(f"command {command_name!r} needs a valid cmd")
        normalized_commands.append({"name": command_name, "cmd": copy.deepcopy(cmd),
                                    "cwd": command.get("cwd"), "health": command.get("health"), "env": env})
    if isinstance(target_name, str):
        incoming_ids = {f"{slug(target_name)}--{slug(command['name'])}" for command in normalized_commands}
        for other_name, other_spec in hub.config.profiles.items():
            if other_name == target_name or not isinstance(other_spec, dict):
                continue
            try:
                other_commands = _as_command_list(other_spec.get("commands", []) or [])
            except ValueError:
                continue
            duplicates = incoming_ids & {f"{slug(other_name)}--{slug(command['name'])}" for command in other_commands}
            if duplicates:
                errors.append(f"command IDs collide with existing profile {other_name!r}; choose a different name or command name")
    if machine_fields:
        warnings.append("machine-specific cwd/health values are retained; review or override them for this computer")
    if missing_env:
        warnings.append("credential-like environment values were omitted from the package; configure them locally after import")

    existing = target_name in hub.config.profiles if isinstance(target_name, str) else False
    if existing and not replace:
        errors.append(f"profile name {target_name!r} already exists; choose another name or set replace=true")
    if existing and replace:
        status = hub.profile_status(target_name)
        if any(member.get("state") in {"running", "starting"} for member in status.get("members", [])):
            errors.append(f"cannot replace running profile {target_name!r}; stop it first")
    summary = {"name": target_name, "apps": mapped_apps, "desktop": mapped_desktop,
               "commands": [{"name": c["name"], "cmd": copy.deepcopy(c["cmd"]),
                             "cwd": c.get("cwd"), "health": c.get("health"),
                             "env_keys": sorted(c["env"])} for c in normalized_commands]}
    return {"ok": not errors, "can_import": not errors,
            "errors": errors, "warnings": warnings, "profile": summary, "dependencies": dependencies,
            "machine_specific_fields": machine_fields, "missing_environment": missing_env,
            "conflict": {"exists": existing, "replace_requested": replace},
            "started": False, "runtime_state_included": False,
            "_normalized": {"apps": mapped_apps, "desktop": mapped_desktop, "commands": normalized_commands}}


def preview_import(hub, document: Any, **options) -> dict[str, Any]:
    """Validate/import-plan without writing config or checking/starting processes."""
    result = _plan(hub, document, **options)
    result.pop("_normalized", None)
    return result


def import_profile(hub, document: Any, **options) -> dict[str, Any]:
    """Persist the reviewed profile configuration; never starts apps or commands."""
    with hub._lock:
        # Plan against the exact profile map that this locked save will replace.
        plan = _plan(hub, document, **options)
        normalized = plan.pop("_normalized", None)
        if not plan["can_import"]:
            plan["ok"] = False
            plan["error"] = "profile import needs the reported dependency or collision issues resolved"
            return plan
        target = plan["profile"]["name"]
        old = copy.deepcopy(hub.config.profiles)
        new_profiles = copy.deepcopy(old)
        new_profiles[target] = normalized
        hub.config.profiles = new_profiles
        try:
            hub.config.save()
        except OSError as exc:
            hub.config.profiles = old
            return {**plan, "ok": False, "error": f"could not save imported profile: {exc}"}
    plan["ok"] = True
    plan["imported"] = True
    return plan
