"""Resolve explicit manifest routes and the closed voice compatibility routes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

_SEGMENT = re.compile(r"[a-z0-9_-]{1,64}")
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_PARAMETER = "{job_id}"
_KEY = "stream_routes"
_COMPATIBILITY_SERVICE = {"voice": "tts", "dictation": "dictation"}


def _parts(value):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("invalid stream route")
    parts = tuple(value.split("/"))
    if any(part != _PARAMETER and not _SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("invalid stream segment")
    if parts.count(_PARAMETER) > 1:
        raise ValueError("duplicate stream parameter")
    return parts


@dataclass(frozen=True)
class Route:
    parts: tuple[str, ...]
    method: str
    upstream: tuple[str, ...]
    body: str
    response: str
    cancel: tuple[str, ...] | None

    def match(self, parts):
        if len(parts) != len(self.parts):
            return False
        return all(_JOB_ID.fullmatch(actual) if expected == _PARAMETER else expected == actual
                   for expected, actual in zip(self.parts, parts))

    def target(self, parts):
        job_id = parts[self.parts.index(_PARAMETER)] if _PARAMETER in self.parts else ""
        return "/" + "/".join(job_id if part == _PARAMETER else part for part in self.upstream)


def _route(value):
    if not isinstance(value, dict):
        raise ValueError("invalid stream route descriptor")
    method = value.get("method")
    body = value.get("body", "")
    response = value.get("response", "json")
    if (method not in ("GET", "POST", "DELETE") or body not in ("", "json", "audio")
            or (method == "POST") != bool(body) or response not in ("json", "ndjson")):
        raise ValueError("invalid stream method, body or response")
    parts = _parts(value.get("route"))
    path = value.get("path")
    if not isinstance(path, str) or not path.startswith("/api/"):
        raise ValueError("invalid stream target")
    upstream = _parts(path[1:])
    cancel = _parts(value["cancel"]) if "cancel" in value else None
    if (_PARAMETER in parts) != (_PARAMETER in upstream):
        raise ValueError("unbound stream parameter")
    if response == "ndjson" and method != "GET":
        raise ValueError("NDJSON endpoints must use GET")
    if cancel is not None and (response != "ndjson" or _PARAMETER not in parts or _PARAMETER not in cancel):
        raise ValueError("invalid stream cancellation")
    return Route(parts, method, upstream, body, response, cancel)


@lru_cache(maxsize=1)
def _voice_manifest():
    return json.loads(files("hoard_link").joinpath("_data/boltagon-stream-manifest.json").read_text(encoding="utf-8"))


def resolve(app, path, *, voice_services=()):
    """Return a declared route or one granted to the registered service owner."""
    try:
        compatibility = _KEY not in app.family
        if compatibility:
            if not voice_services:
                return None
            entries = _voice_manifest()["x-family"][_KEY]
        else:
            entries = app.family[_KEY]
        if not isinstance(entries, list) or len(entries) > 32:
            return None
        parts = _parts(path)
        if _PARAMETER in parts:
            return None
        routes = [_route(entry) for entry in entries]
        if compatibility:
            routes = [route for route in routes if _COMPATIBILITY_SERVICE.get(route.parts[0]) in voice_services]
        matches = [route for route in routes if route.match(parts)]
        if len(matches) != 1:
            return None
        route = matches[0]
        cancel_target = None
        if route.cancel is not None:
            choices = [other for other in routes if other.parts == route.cancel and other.method == "DELETE"]
            if len(choices) != 1:
                return None
            job_id = parts[route.parts.index(_PARAMETER)]
            cancel_parts = tuple(job_id if part == _PARAMETER else part for part in route.cancel)
            cancel_target = choices[0].target(cancel_parts)
        return route, route.target(parts), cancel_target
    except (OSError, ValueError):
        # Invalid manifests fail closed and are repaired with a normal rescan.
        return None
