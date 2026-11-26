"""Passive observations for the link checker.

GETs have a fixed path allowlist. A native tool may be called only through
its reviewed (app, tool) pair, with fixed arguments and no Hub app proxy.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit
import math
import re
import sqlite3
import time

from ..atomic import read_json
from ..service_contracts import SERVICES, service_owners
from . import contract, procs
from .link_inventory import COMPATIBILITY_OWNERS, NATIVE_COMPATIBILITY
from .placement import (
    APPS,
    HEALTH_EXCLUDED_APPS as DEFAULT_EXCLUDED_APPS,
    HEALTH_EXCLUDED_PORTS as EXCLUDED_PORTS,
    REMOTE_HEALTH_TTL_S,
    PlacementError,
    remote_health_targets,
)
from .registry import App

RESPONDS = "responds"
FAILED = "not_responding"
STOPPED = "stopped"
UNCHECKED = "not_checked"

CATALOGUE_PATH = "/api/agent/tools"
HEALTH_PATH = "/api/health"
GET_PATHS = frozenset({CATALOGUE_PATH, HEALTH_PATH, "/api/hoard-copilot/capabilities"})
HEALTH_APPS = frozenset({"faustus", "nightingale"})
APP_READ_TOOLS = frozenset({("nightingale", "nightingale_modules")})
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
SERVICE_SPECS = {key: (owner, tools) for key, owner, tools in SERVICES}
AGENDA_APPS = ("homehoard", "kafka", "ledger", "people", "hypatia", "mercator")
WRITER_COMPAT_TOOLS = {
    "borges": ("borges_modules",),
    "gutenberg": ("gutenberg_publications",),
    "scheherazade": ("wh_story_modules",),
}
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class Observation:
    state: str
    called: str
    outcome: str
    metrics: dict | None = None
    scope: str = "link"
    location: str | None = None


@dataclass(frozen=True)
class AppEndpoint:
    app: App
    location: str


def _request_app(app, method, path, args, *, timeout):
    if app.id == "faustus" and method == "GET":
        from ..config import LinkConfig
        token = LinkConfig.load(app="hoard-hub").faustus_token or ""
        return contract._get(app.url + path, token, timeout)
    return contract.app_request(app, method, path, args, timeout=timeout)


def _listener_ports() -> set[int] | None:
    """Read OS listener metadata. The Hub's general lookup can spawn a CLI."""
    ps = procs._psutil()
    if ps is None:
        return None
    try:
        return {
            row.laddr.port for row in ps.net_connections(kind="tcp")
            if row.status == ps.CONN_LISTEN and row.laddr and row.laddr.port not in EXCLUDED_PORTS
        }
    except (ps.Error, OSError):
        return None


def _tool_names(body) -> set[str] | None:
    rows = body.get("tools") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return None
    return {row["name"] for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)}


def read_only_without_arguments(tool):
    if not isinstance(tool, dict):
        return False
    annotations = tool.get("annotations")
    schema = tool.get("inputSchema")
    return (
        isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
        and isinstance(schema, dict) and schema.get("type") == "object"
        and schema.get("required", []) == []
    )


def _local_head(folder: Path) -> str | None:
    """Read an ordinary Git checkout or linked worktree without invoking Git."""
    try:
        git = folder / ".git"
        if git.is_file():
            prefix, _, target = git.read_text(encoding="utf-8").strip().partition(": ")
            if prefix != "gitdir" or not target:
                return None
            git = git.parent / target
        head = (git / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            if not ref.startswith("refs/") or ".." in Path(ref).parts or "\\" in ref or ":" in ref:
                return None
            common_file = git / "commondir"
            common = git / common_file.read_text(encoding="utf-8").strip() if common_file.is_file() else git
            ref_path = git / ref
            if not ref_path.is_file():
                ref_path = common / ref
            if ref_path.is_file():
                head = ref_path.read_text(encoding="ascii").strip()
            else:
                refs = (common / "packed-refs").read_text(encoding="ascii").splitlines()
                head = next((line.split(" ", 1)[0] for line in refs if line.endswith(" " + ref)), "")
        return head if SHA.fullmatch(head) else None
    except (OSError, ValueError):
        return None


class LinkProbes:
    def __init__(self, hub, excluded=(), *, request=None, listeners=None):
        self.hub = hub
        self.excluded = DEFAULT_EXCLUDED_APPS | frozenset(excluded)
        self._request = request if request is not None else _request_app
        self._listen = listeners if listeners is not None else _listener_ports
        self._placement_refresh_key = None
        self._placement_scoped_health = None

    @cached_property
    def listeners(self):
        return self._listen()

    @cached_property
    def hub_tools(self):
        from .tools import all_tools
        return {tool["name"]: tool for tool in all_tools()}

    def _get_app(self, app_id, deadline, *, faustus_base=None):
        """Check permission, the registry and a passive running-state source before any HTTP."""
        owner_id = COMPATIBILITY_OWNERS.get(app_id, app_id)
        if {app_id, owner_id} & self.excluded:
            return Observation(UNCHECKED, "link-check.json", "operator_excluded")
        if time.monotonic() >= deadline:
            return Observation(FAILED, "Hub registry", "probe_timeout")
        if self._isolated():
            return Observation(UNCHECKED, "Hub registry", "isolated_hub")
        placement = self.hub.placement
        state = placement.state()
        urls = state.get("urls") or {}
        app = self.hub.get(app_id) if app_id != "faustus" else None
        if app is not None:
            declared = app.family.get("absorbed_by")
            if isinstance(declared, dict) and isinstance(declared.get("id"), str):
                owner_id = COMPATIBILITY_OWNERS.get(app_id, declared["id"].casefold())
        if owner_id in self.excluded:
            return Observation(UNCHECKED, "link-check.json", "operator_excluded")

        managed = owner_id in APPS or owner_id in urls
        remote = managed and state["location"] == "sparks"
        location = "remote" if remote else "pc"
        if managed and (state.get("job") or {}).get("running"):
            return Observation(UNCHECKED, "placement.json", "placement_moving")
        if not remote and (self.hub.automation_blocked(app_id) or self.hub.automation_blocked(owner_id)):
            return Observation(STOPPED, "Hub registry", "manually_stopped", location=location)

        if app_id == "faustus":
            local_urls = self.hub.config.faustus_urls
            base = faustus_base or (local_urls[0] if local_urls else "")
            app = App("faustus", "Faustus", "", "", base, HEALTH_PATH, "faustus")
        if app is None:
            return Observation(FAILED, "Hub registry", "app_missing", location=location)
        if app.port in EXCLUDED_PORTS:
            return Observation(UNCHECKED, "Hub registry", "endpoint_not_verified", location=location)
        if not remote and app_id != owner_id:
            owner = self.hub.get(owner_id)
            if owner is not None and owner.url:
                prefix = urlsplit(app.url).path
                app = replace(app, url=owner.url.rstrip("/") + prefix)
        if remote:
            call = "placement health cache"
            cached = self._remote_health(state, deadline)
            if cached is None:
                return Observation(UNCHECKED, call, "remote_state_unknown", location=location)
            if cached.get("ok") is False:
                return Observation(FAILED, call, "remote_unreachable", location=location)
            row = (cached.get("apps") or {}).get(owner_id, {})
            if row.get("healthy") is not True:
                if row.get("service") in ("inactive", "failed", "deactivating"):
                    return Observation(FAILED, call, "no_running_service", location=location)
                if row.get("service") not in ("active", "reloading"):
                    return Observation(UNCHECKED, call, "remote_state_unknown", location=location)
            base = urls.get(owner_id)
            if not base or (faustus_base and faustus_base.rstrip("/") != base.rstrip("/")):
                return Observation(UNCHECKED, "placement.json", "endpoint_not_verified", location=location)
            if owner_id == "hermes":
                published = urlsplit(base)
                if published.path.rstrip("/") == "/products":
                    # Placement publishes the UI, while agent routes live at the origin.
                    base = published._replace(path="").geturl()
            prefix = urlsplit(app.url).path if app_id != owner_id else ""
            app = replace(app, url=base.rstrip("/") + prefix)

        parsed = urlsplit(app.url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port in EXCLUDED_PORTS
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            return Observation(UNCHECKED, "Hub registry", "endpoint_not_verified", location=location)
        if not remote:
            if parsed.hostname not in LOOPBACK:
                return Observation(UNCHECKED, "Hub registry", "endpoint_not_verified", location=location)
            if self.listeners is None:
                return Observation(UNCHECKED, "Hub registry / OS listeners", "listener_state_unknown", location=location)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if port not in self.listeners:
                return Observation(STOPPED, "Hub registry / OS listeners", "no_listener", location=location)
        return AppEndpoint(app, location)

    def _http(self, endpoint, path, deadline, *, native_tool=None):
        app, location = endpoint.app, endpoint.location
        if app.id in self.excluded or app.port in EXCLUDED_PORTS:
            return Observation(UNCHECKED, "link-check.json", "operator_excluded"), None
        if native_tool is not None:
            if (app.id, native_tool) not in APP_READ_TOOLS or path != "/api/agent/call":
                raise ValueError("App tool is outside the link checker allowlist")
            method = "POST"
            args = {"name": native_tool, "tool": native_tool, "arguments": {}}
        else:
            if path not in GET_PATHS or (path == "/api/hoard-copilot/capabilities" and app.id != "faustus"):
                raise ValueError("GET target is outside the link checker allowlist")
            if path == HEALTH_PATH and app.id not in HEALTH_APPS:
                return Observation(UNCHECKED, "Hub registry", "health_route_not_reviewed"), None
            method, args = "GET", None
        call = f"{app.id} {method} {path}" + (f" ({native_tool})" if native_tool else "")
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            return Observation(FAILED, call, "probe_timeout", location=location), None
        status, body = self._request(app, method, path, args, timeout=seconds)
        if status is None:
            return Observation(FAILED, call, "no_http_response", location=location), None
        if not 200 <= status < 300:
            return Observation(FAILED, call, "http_error", {"http_status": status}, location=location), None
        return Observation(RESPONDS, call, "http_success", {"http_status": status}, location=location), body

    def catalogue(self, app_id, deadline, required=()):
        endpoint = self._get_app(app_id, deadline)
        return endpoint if isinstance(endpoint, Observation) else self._catalogue(endpoint, deadline, required)

    def _catalogue(self, endpoint, deadline, required):
        app = endpoint.app
        module = NATIVE_COMPATIBILITY.get(app.id)
        prefix = f"/compat/{module}" if module and app.id != "scheherazade" else ""
        if prefix and not urlsplit(app.url).path.endswith(prefix):
            app = replace(app, url=app.url.rstrip("/") + prefix)
            endpoint = replace(endpoint, app=app)
        obs, body = self._http(endpoint, CATALOGUE_PATH, deadline)
        if prefix:
            obs = replace(obs, called=f"{app.id} GET {prefix}{CATALOGUE_PATH}")
        if obs.state != RESPONDS:
            return obs
        seen = _tool_names(body)
        if seen is None or (module and not seen):
            return replace(obs, state=FAILED, outcome="invalid_catalogue")
        if module and app.id not in WRITER_COMPAT_TOOLS and (not isinstance(body, dict) or body.get("module") != module):
            return replace(obs, state=FAILED, outcome="unexpected_response")
        matched = len(set(required) & seen)
        metrics = {**obs.metrics, "catalogue_tools": len(seen), "required_tools": len(required), "matched_tools": matched}
        if matched != len(required):
            return replace(obs, state=FAILED, outcome="missing_tools", metrics=metrics, scope="catalogue")
        return replace(obs, outcome="catalogue_matches", metrics=metrics, scope="catalogue")

    def native_tool(self, app_id, tool, deadline):
        if (app_id, tool) not in APP_READ_TOOLS:
            raise ValueError("App tool is outside the link checker allowlist")
        endpoint = self._get_app(app_id, deadline)
        if isinstance(endpoint, Observation):
            return endpoint
        health = self._health(endpoint, deadline)
        if health.state != RESPONDS:
            return health
        catalogue, body = self._http(endpoint, CATALOGUE_PATH, deadline)
        if catalogue.state != RESPONDS:
            return catalogue
        rows = body.get("tools") if isinstance(body, dict) else body
        if not isinstance(rows, list):
            return replace(catalogue, state=FAILED, outcome="invalid_catalogue")
        matches = [row for row in rows if isinstance(row, dict) and row.get("name") == tool]
        if len(matches) != 1 or not read_only_without_arguments(matches[0]):
            return replace(catalogue, state=UNCHECKED, outcome="no_health_probe")
        target = self._get_app(app_id, deadline)
        if isinstance(target, Observation):
            return target
        if (target.app.url, target.app.token_file, target.location) != (
                endpoint.app.url, endpoint.app.token_file, endpoint.location):
            return replace(catalogue, state=UNCHECKED, outcome="endpoint_not_verified")
        obs, body = self._http(target, "/api/agent/call", deadline, native_tool=tool)
        if obs.state != RESPONDS:
            return obs
        if isinstance(body, dict) and isinstance(body.get("modules"), list):
            return replace(obs, outcome="module_inventory", metrics={**obs.metrics, "modules": len(body["modules"])})
        return replace(obs, state=FAILED, outcome="unexpected_response")

    def _health(self, endpoint, deadline):
        app = endpoint.app
        if app.health_path != HEALTH_PATH:
            return Observation(UNCHECKED, "Hub registry", "health_route_not_reviewed")
        obs, body = self._http(endpoint, HEALTH_PATH, deadline)
        if obs.state != RESPONDS:
            return obs
        if (not isinstance(body, dict) or body.get("ok") is False
                or (app.expect_service and body.get("service") != app.expect_service)):
            return replace(obs, state=FAILED, outcome="unexpected_response")
        return replace(obs, scope="health")

    def health(self, app_id, deadline):
        endpoint = self._get_app(app_id, deadline)
        return endpoint if isinstance(endpoint, Observation) else self._health(endpoint, deadline)

    def compatibility(self, app_id, deadline):
        endpoint = self._get_app(app_id, deadline)
        if isinstance(endpoint, Observation):
            return endpoint
        app = endpoint.app
        module = NATIVE_COMPATIBILITY.get(app_id)
        if module:
            declaration = app.family.get("absorbed_by")
            if (not isinstance(declaration, dict) or declaration.get("id") != COMPATIBILITY_OWNERS[app_id]
                    or declaration.get("module") != module):
                return Observation(FAILED, f"{app_id} compatibility manifest", "bridge_owner_missing")
            obs = self._catalogue(endpoint, deadline, WRITER_COMPAT_TOOLS.get(app_id, ()))
            return replace(obs, called=f"{app_id} compatibility manifest + {obs.called}")
        if not app.manifest_path:
            return Observation(FAILED, f"{app_id} bridge.json", "bridge_owner_missing")
        bridge = read_json(Path(app.manifest_path).with_name("bridge.json"), {})
        if not isinstance(bridge, dict) or bridge.get("owner") != COMPATIBILITY_OWNERS[app_id]:
            return Observation(FAILED, f"{app_id} bridge.json", "bridge_owner_missing")
        rows = bridge.get("tools") or []
        required = _tool_names([row.get("schema") for row in rows if isinstance(row, dict)])
        if not required:
            return Observation(FAILED, f"{app_id} bridge.json", "bridge_tools_missing")
        observation = self._catalogue(endpoint, deadline, required)
        if observation.scope != "catalogue":
            return observation
        return replace(observation, called=f"{app_id} bridge.json + {observation.called}")

    def _placement_targets(self, state):
        targets = remote_health_targets(state.get("urls"))
        excluded = self.excluded | {
            alias for alias, owner in COMPATIBILITY_OWNERS.items() if owner in self.excluded
        }
        for app in self.hub.apps:
            if app.id in targets:
                parent = app.family.get("absorbed_by")
                owner = parent.get("id") if isinstance(parent, dict) else None
                if isinstance(owner, str) and owner.casefold() in self.excluded:
                    excluded |= {app.id}
        return {app: target for app, target in targets.items() if app not in excluded}

    def _remote_health(self, state, deadline):
        placement = self.hub.placement
        since = state.get("since") or 0
        if not state.get("node") or self._isolated():
            return None

        def fresh(cache):
            checked = (cache or {}).get("checked") or 0
            return bool(checked) and checked >= since and time.time() - checked <= REMOTE_HEALTH_TTL_S

        cache = getattr(placement, "_remote_health", None)
        placement_fields = ("location", "since", "node", "urls")
        state_key = tuple(state.get(field) for field in placement_fields)
        if not fresh(cache):
            targets = self._placement_targets(state)
            if targets.keys() != remote_health_targets(state.get("urls")).keys():
                # A selective read must neither contact excluded services nor
                # publish an incomplete snapshot to the ordinary placement view.
                key = (state_key, targets)
                previous = self._placement_scoped_health
                cache = previous[1] if previous and previous[0] == key else None
                remaining = deadline - time.monotonic() - 0.15
                if not fresh(cache) and remaining > 0:
                    cache = placement.read_remote_health(targets, timeout=remaining)
                    self._placement_scoped_health = (key, cache)
            else:
                refresh_key = (state_key, (cache or {}).get("checked"))
                if self._placement_refresh_key != refresh_key:
                    self._placement_refresh_key = refresh_key
                    cache = placement.remote_health(since=since)
                    while not fresh(cache) and getattr(placement, "_remote_refreshing", False):
                        remaining = deadline - time.monotonic() - 0.15
                        if remaining <= 0:
                            break
                        time.sleep(min(0.05, remaining))
                        cache = getattr(placement, "_remote_health", None)
        current = placement.state()
        if ((current.get("job") or {}).get("running")
                or tuple(current.get(field) for field in placement_fields) != state_key):
            return None
        return cache if fresh(cache) else None

    def _isolated(self):
        return getattr(self.hub.config, "isolated", False) or getattr(self.hub.config, "isolated_catalog", False)

    def placement(self, deadline):

        state = self.hub.placement.state()
        if (state.get("job") or {}).get("running"):
            return Observation(UNCHECKED, "placement.json", "placement_moving")
        if state["location"] != "sparks":
            return Observation(UNCHECKED, "placement.json", "not_placed_remotely")
        if not state.get("node"):
            return Observation(FAILED, "placement.json", "remote_node_missing")
        included = self._placement_targets(state).keys()
        if not included:
            return Observation(UNCHECKED, "link-check.json", "operator_excluded", location="remote")
        cached = self._remote_health(state, deadline)
        call = "placement.json + placement health cache"
        if cached is None:
            return Observation(UNCHECKED, call, "remote_state_unknown", location="remote")
        if cached.get("ok") is False:
            return Observation(FAILED, call, "remote_unreachable", location="remote")
        rows = cached.get("apps") or {}
        healthy = sum(1 for app in included if rows.get(app, {}).get("healthy") is True)
        metrics = {"services": len(included), "healthy_http": healthy, "seen_at": cached["checked"]}
        if healthy == len(included) and healthy:
            return Observation(RESPONDS, call, "recorded_health", metrics, "cached", "remote")
        if included and all(rows.get(app, {}).get("service") in ("inactive", "failed") for app in included):
            return Observation(FAILED, call, "no_running_service", metrics, "cached", "remote")
        if healthy < len(included) and included <= rows.keys():
            return Observation(FAILED, call, "remote_unreachable", metrics, "cached", "remote")
        return Observation(UNCHECKED, call, "remote_state_unknown", metrics, "cached", "remote")

    def spark_link(self):
        link = self.hub.placement.link.status()
        call = "placement link recorded status"
        if not link.get("wanted"):
            return Observation(UNCHECKED, call, "not_placed_remotely", scope="cached")
        return Observation(RESPONDS if link.get("up") else FAILED, call,
                           "tunnel_up" if link.get("up") else "tunnel_down", scope="cached")

    def deployment(self):
        state = self.hub.placement.state()
        deployed = state.get("deployed") or {}
        call = "placement.json + local Git HEAD"
        if state["location"] != "sparks" or (state.get("job") or {}).get("running"):
            return Observation(UNCHECKED, call, "deployment_unknown")
        try:
            paths = self.hub.placement.local_dirs()
        except PlacementError:
            return Observation(UNCHECKED, call, "deployment_unknown")
        candidates = set(APPS) - self.excluded
        compared = matches = 0
        for key in candidates:
            head = _local_head(paths[key]) if key in paths else None
            remote = str(deployed.get(key) or "").split("|", 1)[0]
            if head and SHA.fullmatch(remote):
                compared += 1
                matches += head == remote
        metrics = {"expected": len(candidates), "compared": compared, "matching": matches}
        if matches < compared:
            return Observation(FAILED, call, "deployment_differs", metrics, "recorded_revision")
        if compared < len(candidates) or not compared:
            return Observation(UNCHECKED, call, "deployment_unknown", metrics, "recorded_revision")
        return Observation(RESPONDS, call, "deployment_matches", metrics, "recorded_revision")



    def _facet(self, name):
        facet = self.hub.facet(name)
        if facet is None:
            raise LookupError("Facet unavailable")
        return facet

    def hub_catalogue(self, names, scope="declaration"):
        matched = len(set(names) & self.hub_tools.keys())
        return Observation(RESPONDS if matched == len(names) else FAILED, "Hub tool catalogue",
                           "catalogue_matches" if matched == len(names) else "missing_tools",
                           {"required_tools": len(names), "matched_tools": matched}, scope)

    def relay(self, deadline):
        relay = self._facet("mcp_relay")
        declared = described = 0
        for app in self.hub.apps:
            if time.monotonic() >= deadline:
                return Observation(FAILED, "MCP relay descriptions", "probe_timeout")
            if app.id in self.excluded or COMPATIBILITY_OWNERS.get(app.id) in self.excluded or app.port in EXCLUDED_PORTS:
                continue
            manifest = read_json(app.manifest_path, {}) if app.manifest_path else {}
            if not isinstance(manifest, dict) or not (manifest.get("mcp") or {}).get("command"):
                continue
            declared += 1
            try:
                # check=True can run SSH and the streamable endpoint can spawn a bridge.
                reply = relay.describe(app.id, check=False)
            except (OSError, ValueError, KeyError):
                continue
            described += bool(reply.get("mcp_path"))
        metrics = {"declared": declared, "described": described}
        if not declared:
            return Observation(UNCHECKED, "MCP relay descriptions", "no_declared_apps", metrics, "declaration")
        state = RESPONDS if described == declared else FAILED
        return Observation(state, "hub_mcp_apps (per app, check=false)", "relay_descriptions", metrics, "declaration")

    def mail(self):
        call = "Mail gateway cadence + last successful pass"
        if self._isolated():
            return Observation(UNCHECKED, call, "isolated_hub")
        gate = self._facet("mailgate")
        settings = gate.config()
        if not settings.get("enabled"):
            return Observation(STOPPED, call, "mail_disabled")
        last_ok = gate.last_successful_pass()
        interval_s = 60 * settings["interval_min"]
        if type(last_ok) not in (int, float) or not math.isfinite(last_ok) or last_ok <= 0:
            started_at = getattr(self.hub, "started_at", gate.clock())
            uptime = max(0, round(gate.clock() - started_at))
            metrics = {"uptime_s": uptime, "interval_s": interval_s}
            if interval_s and uptime > interval_s + gate.first_delay_s:
                return Observation(FAILED, call, "mail_first_pass_overdue", metrics, "gateway_state")
            return Observation(RESPONDS, call, "mail_since_start", metrics, "gateway_state")
        age = max(0, round(gate.clock() - last_ok))
        on_time = not interval_s or age <= interval_s
        outcome = "mail_on_demand" if not interval_s else "mail_fresh" if on_time else "mail_stale"
        return Observation(RESPONDS if on_time else FAILED, call, outcome,
                           {"last_success_at": last_ok, "age_s": age, "interval_s": interval_s}, "recorded_receipt")

    def gateway_reader(self, reader, deadline):
        from .mailgate import LINK_READERS

        if reader not in LINK_READERS:
            raise ValueError("Unknown gateway reader")
        call = "Mail gateway reader receipts"
        if reader in self.excluded:
            return Observation(UNCHECKED, "link-check.json", "operator_excluded")
        if self._isolated():
            return Observation(UNCHECKED, call, "isolated_hub")
        gate = self._facet("mailgate")
        receipt = getattr(gate, "link_readers", {}).get(reader)
        if receipt is not None:
            ts, ok, matched = receipt
            return Observation(RESPONDS if ok else FAILED, call, "mail_reader_scan" if ok else "mail_reader_scan_failed",
                               {"last_scan_at": ts, "messages_returned": matched}, "recorded_receipt")
        claim = gate.last_reader_claim(reader)
        if claim:
            return Observation(RESPONDS, call, "mail_reader_claim", {"last_claim_at": claim}, "recorded_receipt")
        return self.fleet(deadline, reader)

    def mail_caller(self):
        from ..mail_helper import INSTALLATION_FILE

        call = "Mail installation and caller metadata"
        if self._isolated():
            return Observation(UNCHECKED, call, "isolated_hub")
        gate = self._facet("mailgate")
        last_use = getattr(gate, "last_caller_at", None)
        root = gate.faustus_dir()
        data = root / "data" if root is not None else None
        installed = data is not None and (data / INSTALLATION_FILE).is_file()
        registered = data is not None and (data / "mail_callers.json").is_file()
        metrics = {"installation_exists": installed, "registry_exists": registered, "last_used_at": last_use}
        if last_use is not None:
            return Observation(RESPONDS, call, "mail_caller_used", metrics, "recorded_receipt")
        if installed or registered:
            return Observation(RESPONDS, call, "mail_caller_declared", metrics, "declaration")
        return Observation(UNCHECKED, call, "mail_caller_unknown", metrics, "declaration")

    def dojo_count(self, deadline):
        endpoint = self._get_app("faustus", deadline)
        if isinstance(endpoint, Observation):
            return endpoint
        receipt = getattr(self.hub, "dojo_list_receipt", None)
        call = "Dojo bridge recorded exam count"
        if receipt is None or receipt[0] != endpoint.app.url.rstrip("/"):
            return Observation(UNCHECKED, call, "dojo_no_receipt", scope="cached", location=endpoint.location)
        _, ts, exams = receipt
        return Observation(RESPONDS, call, "dojo_exam_count", {"exams": exams, "seen_at": ts},
                           "cached", endpoint.location)

    def quota_measurement(self):
        agora = self._facet("agora").agora
        call = "Agora usage measurement metadata"
        now = agora.clock()
        rows = agora.db.query(
            """SELECT p.provider_id, MAX(p.reported_at) AS reported_at,
                      MAX(CASE WHEN json_extract(w.value, '$.measurement') = 'measured'
                               THEN p.reported_at END) AS measured_at,
                      MAX(CASE WHEN json_extract(w.value, '$.measurement') = 'measured'
                                 AND COALESCE(unixepoch(json_extract(w.value, '$.reset_at')), ?) > ?
                               THEN p.reported_at END) AS usable_at
                 FROM agora_usage_pools p
                 LEFT JOIN json_each(CASE WHEN json_valid(p.windows_json) THEN p.windows_json ELSE '[]' END) w
                 GROUP BY p.provider_id""",
            (now + 1, now),
        )
        metrics = {}
        for row in rows:
            provider = row["provider_id"]
            if provider in agora._USAGE_PROVIDERS:
                metrics[f"{provider}_reported_at"] = row["reported_at"]
                metrics[f"{provider}_measured_at"] = row["measured_at"]
                metrics[f"{provider}_fresh"] = (row["usable_at"] is not None
                                                and now - row["usable_at"] <= agora._USAGE_STALE_S)
        fresh = any(value is True for value in metrics.values())
        outcome = "quota_measured" if fresh else "quota_stale_or_unknown" if metrics else "quota_no_receipt"
        return Observation(RESPONDS if fresh else UNCHECKED, call, outcome, metrics, "recorded_receipt")

    def fleet(self, deadline, reader=None, *, owner=None):
        location = None
        if reader:
            endpoint = self._get_app(reader, deadline)
            if isinstance(endpoint, Observation):
                return endpoint
            location = endpoint.location
        else:
            state = self.hub.placement.state()
            if state["location"] == "sparks" and not (state.get("job") or {}).get("running"):
                location = "remote"
        call = "Fleet recorded watcher receipts"
        value = self._facet("fleet").cached
        if not value:
            return Observation(UNCHECKED, call, "fleet_no_snapshot", scope="cached", location=location)
        link_owner = reader or owner
        owners = ({link_owner} if link_owner else {"tantalus", "phileas", "hermes"}) - self.excluded
        rows = [row for row in value.get("items", []) if row.get("kind") == "watcher" and row.get("owner") in owners]
        # These readers can have other watchers. A non-mail watcher is never
        # evidence of a mail read.
        if reader:
            rows = [row for row in rows if "mail" in str(row.get("id", "")).lower()]
        last = max((r.get("last_success_ts") or 0 for r in rows), default=0)
        failed = sum(r.get("state") in ("error", "silent") for r in rows)
        metrics = {"watchers": len(rows), "failing": failed, "seen_at": value.get("generated_ts"),
                   "last_success_at": last or None}
        state = FAILED if failed else RESPONDS if last else UNCHECKED
        return Observation(state, call, "fleet_receipts" if state != UNCHECKED else "fleet_no_receipt", metrics, "cached", location)

    def mail_library(self, deadline):
        from .drift import find_vendored
        from .link_vendor_history import published_blobs

        canonical = Path(__file__).resolve().parents[2]
        py = "hoard_link/fam_mail.py"
        js = "js/hoard-link.js"
        sources = {name: (canonical / name).read_bytes() for name in (py, js)}
        call = "Vendored mail library bytes and canonical Git history"
        histories = {}
        counts = {"copies": 0, "differing": 0, "outdated": 0, "unmatched": 0, "history_unavailable": 0}
        behind = {}
        for app in self.hub.apps:
            if app.id in self.excluded or app.id in COMPATIBILITY_OWNERS or app.port in EXCLUDED_PORTS or not app.folder:
                continue
            if time.monotonic() >= deadline:
                return Observation(UNCHECKED, call, "probe_timeout")
            candidates = [(path / "fam_mail.py", py) for path in find_vendored(app.folder, canonical / "hoard_link")]
            candidates.append((Path(app.folder) / "server" / "hoard-link.js", js))
            for path, name in candidates:
                if time.monotonic() >= deadline:
                    return Observation(UNCHECKED, call, "probe_timeout")
                if path.is_symlink() or not path.is_file():
                    continue
                counts["copies"] += 1
                content = path.read_bytes()
                if content == sources[name]:
                    continue
                counts["differing"] += 1
                if name not in histories:
                    histories[name] = published_blobs(canonical, name, sources[name], deadline)
                history = histories[name]
                if history is None:
                    counts["history_unavailable"] += 1
                    continue
                lag = history.versions.get(history.blob_id(content))
                if lag is not None:
                    counts["outdated"] += 1
                    behind[lag] = behind.get(lag, 0) + 1
                else:
                    counts["unmatched" if history.complete else "history_unavailable"] += 1
        metrics = {**counts, **{f"versions_behind_{lag}": count for lag, count in sorted(behind.items())}}
        if behind:
            metrics.update(min_versions_behind=min(behind), max_versions_behind=max(behind))
        if counts["unmatched"]:
            return Observation(FAILED, call, "vendor_copies_unmatched", metrics, "source")
        if counts["history_unavailable"]:
            return Observation(UNCHECKED, call, "vendor_history_unavailable", metrics, "source")
        if counts["outdated"]:
            return Observation(RESPONDS, call, "vendor_copies_behind", metrics, "source")
        return Observation(RESPONDS if counts["copies"] else UNCHECKED, call, "vendor_copies", metrics, "source")

    def events(self, source=None):
        events = self.hub.events
        # Select aggregate metadata only. EventLog.query() also reads the payload.
        with events._lock:
            if source is None:
                row = events._db.execute("SELECT COUNT(*), MAX(ts) FROM events").fetchone()
            else:
                row = events._db.execute("SELECT COUNT(*), MAX(ts) FROM events WHERE source = ?", (source,)).fetchone()
        call = "Hub event log (count and last timestamp)"
        if source:
            call += f" / source={source}"
        state = RESPONDS if row[0] or not source else UNCHECKED
        return Observation(state, call, "event_stats" if state == RESPONDS else "no_event_receipt",
                           {"events": row[0], "last_event_at": row[1]}, "recorded_receipt")

    def rules(self):
        rules = self.hub.rules
        with rules._lock:
            enabled = [r for r in rules.rules if r.get("enabled", True)]
            last = [r["last"] for r in enabled if r.get("last")]
            metrics = {"configured": len(rules.rules), "enabled": len(enabled), "with_outcome": len(last),
                       "last_failure_count": sum(item.get("ok") is False for item in last),
                       "last_run_at": max((item.get("ts", 0) for item in last), default=None)}
        return Observation(FAILED if metrics["last_failure_count"] else RESPONDS, "Hub rules (recorded outcomes)",
                           "rule_stats", metrics, "recorded_receipt")

    def brain(self, deadline):
        brain = self._facet("brain")
        endpoint = self._get_app("faustus", deadline, faustus_base=brain.target().get("base"))
        if isinstance(endpoint, Observation):
            return endpoint
        obs = self._health(endpoint, deadline)
        if obs.state != RESPONDS:
            return obs
        # The native Brain status enumerates the person's notes. Read only
        # the bridge's recorded outcomes alongside Faustus's service health.
        stats = brain.counts()
        counts = {key: stats.get(key, 0) for key in ("pending", "done", "partial", "failed")}
        state = FAILED if counts["failed"] or counts["partial"] else RESPONDS if counts["done"] else UNCHECKED
        return replace(obs, state=state, called=obs.called + " + Hub Brain queue counts",
                       outcome="brain_queue_status" if state != UNCHECKED else "brain_no_receipt",
                       metrics={**obs.metrics, **counts}, scope="health_and_queue")

    def copilot(self, deadline):
        if "faustus" in self.excluded:
            return Observation(UNCHECKED, "link-check.json", "operator_excluded")
        declared = sum(1 for a in self.hub.apps if a.id not in self.excluded
                       and COMPATIBILITY_OWNERS.get(a.id) not in self.excluded
                       and a.port not in EXCLUDED_PORTS and a.family.get("copilot"))
        endpoint = self._get_app("faustus", deadline)
        if isinstance(endpoint, Observation):
            return endpoint
        obs, body = self._http(endpoint, "/api/hoard-copilot/capabilities", deadline)
        if obs.state != RESPONDS:
            return obs
        if not isinstance(body, dict) or not body.get("contract_version"):
            return replace(obs, state=FAILED, outcome="unexpected_response")
        return replace(obs, outcome="copilot_capabilities", metrics={**obs.metrics, "app_declarations": declared},
                       scope="prerequisites")

    def spheres(self):
        facet = self._facet("spheres")
        return Observation(RESPONDS, "Hub sphere registry (count)", "sphere_count", {"spheres": len(facet.ids())})

    def lanes(self):
        lanes = self.hub.lanes
        # LaneArbiter.status() reaps leases and can save and emit notifications.
        with lanes._cond:
            now = lanes._now()
            metrics = {"unexpired_leases": sum(lease.expires_at > now for lease in lanes._leases.values()),
                       "waiters": len(lanes._waiters)}
        return Observation(RESPONDS, "Hub browser lane memory (counts)", "lane_counts", metrics)

    def coordination(self):
        config = self._facet("coordination").config()
        state = RESPONDS if config.get("authority") == "hub" and config.get("schema_version") == 1 else FAILED
        return Observation(state, "hub_coordination_config", "coordination_schema",
                           {"supported_schema": state == RESPONDS}, "declaration")

    def web(self):
        enabled = self._facet("web").settings().get("enabled") is True
        return Observation(RESPONDS if enabled else STOPPED, "Shared web service setting",
                           "enabled" if enabled else "disabled", scope="declaration")

    def backend_cache(self, kind):
        if kind == "text":
            _, cache = self.hub.link._status_cache
            row = (cache or {}).get("llm")
            available = row.get("available") if isinstance(row, dict) else None
        else:
            _, cache = self.hub._backends_cache
            row = ((cache or {}).get("capabilities") or {}).get("image")
            state = row.get("state") if isinstance(row, dict) else None
            available = state == "resolved" if state in ("resolved", "unavailable") else None
        call = f"Hub backend cache ({kind})"
        recorded = (cache or {}).get("checked_at")
        if (not isinstance(recorded, (int, float)) or time.time() - recorded > 120
                or not isinstance(available, bool)):
            return Observation(UNCHECKED, call, "backend_cache_unknown", scope="cached")
        return Observation(RESPONDS if available else FAILED, call, "backend_cached",
                           {"available": available, "seen_at": recorded}, "cached")

    def gpu_inventory(self):
        leases = self.hub.leases
        with leases._cond:
            state = leases._inventory_status
            count = len(leases._inv[1])
        status = {"ready": RESPONDS, "empty": STOPPED, "error": FAILED}.get(state, UNCHECKED)
        return Observation(status, "GPU lease inventory cache",
                           "gpu_inventory" if status != UNCHECKED else "gpu_inventory_unprobed",
                           {"gpu_count": count}, "cached")

    def skills(self, deadline):
        rows = self.hub.apps
        declared = 0
        for app in rows:
            if time.monotonic() >= deadline:
                return Observation(FAILED, "Skill manifest declarations", "probe_timeout")
            if app.id in self.excluded or COMPATIBILITY_OWNERS.get(app.id) in self.excluded or app.port in EXCLUDED_PORTS:
                continue
            manifest = read_json(app.manifest_path, {}) if app.manifest_path else {}
            if isinstance(manifest, dict) and (manifest.get("provides") or {}).get("skills"):
                declared += 1
        observation = self.hub_catalogue(("hub_skills_overview", "hub_skills_library", "hub_agent_flows"))
        return replace(observation, called="Hub tool catalogue + skill manifest declarations",
                       metrics={**observation.metrics, "apps_declaring_skills": declared})

    def foreman(self):
        facet = self._facet("agora_foreman")
        # The status implementation reads heartbeat metadata only. Never run
        # the monitor's check(), which can open threads and send notices.
        rows = [facet.status({"space_id": space}) for space in facet.settings]
        heartbeats = [row["foreman"] for row in rows if row.get("foreman")]
        overdue = sum(row.get("overdue") is True for row in heartbeats)
        observed = sum(row.get("last_heartbeat_at") is not None for row in heartbeats)
        metrics = {"configured": len(rows), "observed": observed, "overdue": overdue}
        state = FAILED if overdue else RESPONDS if observed else UNCHECKED
        return Observation(state, "Agora foreman heartbeat metadata",
                           "foreman_heartbeat" if state != UNCHECKED else "no_foreman_heartbeat",
                           metrics, "recorded_receipt")

    def console_orphans(self):
        from .agora_worker_processes import live_pids

        facet = self._facet("agora_console")
        call = "Agora console live orphan metadata (loaded spaces)"
        pids = live_pids()
        if not pids:
            return Observation(UNCHECKED, call, "listener_state_unknown")
        with facet._db_lock:
            views = [facet, *facet._space_views.values()]
            checked = orphans = legacy = 0
            for view in views:
                if not view._db_path.is_file():
                    continue
                uri = view._db_path.resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                    db.row_factory = sqlite3.Row
                    rows = db.execute(
                        "SELECT id,pid,state,log_path,process_started_at FROM workers WHERE pid IS NOT NULL AND log_path<>''"
                    )
                    for row in rows:
                        if row["pid"] not in pids:
                            continue
                        # Legacy identification reads a command line that can
                        # contain an instruction or private task text.
                        if row["process_started_at"] is None:
                            legacy += 1
                        else:
                            orphans += bool(view._orphan_process(row))
                    checked += 1
        metrics = {"spaces_checked": checked, "orphans": orphans, "legacy_unverified": legacy}
        if orphans:
            return Observation(FAILED, call, "console_orphans", metrics, "recorded_receipt")
        if not checked or legacy:
            reason = "legacy_workers" if legacy else "no_console_snapshot"
            return Observation(UNCHECKED, call, reason, metrics, "recorded_receipt")
        return Observation(RESPONDS, call, "console_orphans", metrics, "recorded_receipt")

    @staticmethod
    def combine(children, called, scope="group"):
        if not children:
            return Observation(UNCHECKED, called, "no_members", scope=scope)
        metrics = {state: sum(row.state == state for row in children)
                   for state in (RESPONDS, FAILED, STOPPED, UNCHECKED)}
        state = next((state for state in (FAILED, UNCHECKED, STOPPED, RESPONDS) if metrics[state]), UNCHECKED)
        return Observation(state, called, "group_counts", metrics, scope)

    def agenda(self, deadline):
        specs = ("hub_today", "hub_agenda", "hub_ics_link", "hub_search", "hub_refs")
        obs = self.hub_catalogue(specs, scope="prerequisites")
        providers = [
            app_id for app_id in AGENDA_APPS if app_id not in self.excluded
            if (app := self.hub.get(app_id)) is not None and app.family.get("agenda") is True
        ]
        states = [self._get_app(app_id, deadline) for app_id in providers]
        return replace(obs, called=obs.called + " + Hub registry agenda declarations and passive state",
                       metrics={**obs.metrics, "apps_declaring_agenda": len(providers),
                                "running_candidates": sum(isinstance(state, AppEndpoint) for state in states),
                                "stopped_providers": sum(isinstance(state, Observation) and state.state == STOPPED
                                                         for state in states)})

    def prerequisites(self, apps, reason, deadline):
        app_ids = apps.split(",")
        call = f"{apps} GET {CATALOGUE_PATH}"
        rows = []
        for app_id in app_ids:
            # Some health handlers run tool detection or contact another service.
            row = self.catalogue(app_id, deadline)
            rows.append(row)
            if row.state != RESPONDS:
                return replace(row, scope="prerequisites")
        return replace(self.combine(rows, call, "prerequisites"), state=UNCHECKED, outcome=reason)

    def family_catalogues(self, deadline):
        rows = []
        for app in self.hub.apps:
            if app.id in self.excluded or app.id in COMPATIBILITY_OWNERS or app.port in EXCLUDED_PORTS or not app.agent_contract:
                continue
            if time.monotonic() >= deadline:
                return Observation(FAILED, "Registered app catalogues", "probe_timeout")
            rows.append(self.catalogue(app.id, deadline))
        return self.combine(rows, f"Registered apps GET {CATALOGUE_PATH}", "catalogue")

    def service(self, key, deadline):
        owner, required = SERVICE_SPECS[key]
        if owner == "hub":
            obs = self.hub_catalogue(required)
            if obs.state == RESPONDS:
                gateway = self.mail()
                return replace(gateway, called=obs.called + " + " + gateway.called,
                               metrics={**obs.metrics, **(gateway.metrics or {})})
            return obs
        first = None
        seen = set()
        for candidate in service_owners(key):
            endpoint = self._get_app(candidate, deadline)
            if isinstance(endpoint, Observation):
                observation = endpoint
            else:
                candidate = endpoint.app.id
                identity = (candidate, endpoint.app.url)
                if identity in seen:
                    continue
                seen.add(identity)
                observation = self._catalogue(endpoint, deadline, required)
            if first is None or (first.outcome == "app_missing" and observation.outcome != "app_missing"):
                first = observation
            if observation.state == RESPONDS:
                outcome = "legacy_service_catalogue" if candidate != owner else observation.outcome
                if candidate == owner and key in ("stt", "tts"):
                    outcome = "shared_service_catalogue"
                return replace(observation, outcome=outcome)
        return first

    def run(self, probe, deadline, results):
        kind, _, arg = probe.partition(":")
        if kind == "no":
            return Observation(UNCHECKED, "", arg)
        if kind == "excluded":
            return Observation(UNCHECKED, "link-check.json", "operator_excluded")
        if kind == "bridge":
            return self.compatibility(arg, deadline)
        if kind == "service":
            return self.service(arg, deadline)
        if kind == "app_tool":
            app, tool = arg.split(":")
            return self.native_tool(app, tool, deadline)
        if kind == "prerequisites":
            apps, reason = arg.split(":")
            return self.prerequisites(apps, reason, deadline)
        if kind == "app_catalog":
            apps, _, tool = arg.partition(":")
            required = (tool,) if tool else ()
            children = [self.catalogue(app, deadline, required) for app in apps.split(",")]
            return children[0] if len(children) == 1 else self.combine(children, f"{apps} GET {CATALOGUE_PATH}", "catalogue")
        if kind in ("services", "compatibility"):
            prefix = "4.5." if kind == "services" else "1.17."
            return self.combine([row for key, row in results.items() if key.startswith(prefix)], "Child link checks")
        if kind == "media_owners":
            stt = self.service("stt", deadline)
            indexing = self.catalogue("borges", deadline, ("library_reindex",))
            observation = self.combine([stt, indexing], f"{stt.called} + {indexing.called}", "prerequisites")
            return replace(observation, metrics={**observation.metrics,
                                                 "stt_fallback": int(stt.outcome == "legacy_service_catalogue")})
        probes = {
            "hub_catalog": lambda: self.hub_catalogue(("hub_link_check", "hub_event_stats")),
            "relay": lambda: self.relay(deadline),
            "family_catalogues": lambda: self.family_catalogues(deadline),
            "mail": self.mail,
            "mail_reader": lambda: self.fleet(deadline, arg),
            "gateway_reader": lambda: self.gateway_reader(arg, deadline),
            "mail_caller": self.mail_caller,
            "dojo_count": lambda: self.dojo_count(deadline),
            "quota_measurement": self.quota_measurement,
            "mail_library": lambda: self.mail_library(deadline),
            "fleet": lambda: self.fleet(deadline, owner=arg or None),
            "events": self.events,
            "faustus_events": lambda: self.events("faustus"),
            "rules": self.rules,
            "brain": lambda: self.brain(deadline),
            "agenda": lambda: self.agenda(deadline),
            "spheres": self.spheres,
            "lanes": self.lanes,
            "coordination": self.coordination,
            "web": self.web,
            "placement": lambda: self.placement(deadline),
            "spark_link": self.spark_link,
            "deployment": self.deployment,
            "backend_cache": lambda: self.backend_cache(arg),
            "gpu_inventory": self.gpu_inventory,
            "faustus": lambda: self.health("faustus", deadline),
            "copilot": lambda: self.copilot(deadline),
            "skills": lambda: self.skills(deadline),
            "agora_catalog": lambda: self.hub_catalogue(("hub_agora_board", "hub_agora_tasks", "hub_agora_live")),
            "agora_foreman": self.foreman,
            "console_orphans": self.console_orphans,
        }
        return probes[kind]()
