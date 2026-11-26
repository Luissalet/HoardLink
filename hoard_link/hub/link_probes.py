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
import re
import sqlite3
import time

from ..atomic import read_json
from ..service_contracts import SERVICES
from . import contract, procs
from .link_inventory import COMPATIBILITY_OWNERS, NATIVE_COMPATIBILITY
from .placement import APPS, REMOTE_HEALTH_TTL_S, PlacementError
from .registry import App

RESPONDS = "responds"
FAILED = "not_responding"
STOPPED = "stopped"
UNCHECKED = "not_checked"

CATALOGUE_PATH = "/api/agent/tools"
HEALTH_PATH = "/api/health"
GET_PATHS = frozenset({CATALOGUE_PATH, HEALTH_PATH, "/api/hoard-copilot/capabilities"})
APP_READ_TOOLS = frozenset({("nightingale", "nightingale_modules")})
DEFAULT_EXCLUDED_APPS = frozenset({"baird"})
EXCLUDED_PORTS = frozenset({5232})
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
        if getattr(self.hub.config, "isolated", False) or getattr(self.hub.config, "isolated_catalog", False):
            return Observation(UNCHECKED, "Hub registry", "isolated_hub")
        if self.hub.automation_blocked(app_id) or self.hub.automation_blocked(owner_id):
            return Observation(STOPPED, "Hub registry", "manually_stopped")

        placement = self.hub.placement
        state = placement.state()
        managed = owner_id in APPS
        remote = managed and state["location"] == "sparks"
        if managed and (state.get("job") or {}).get("running"):
            return Observation(UNCHECKED, "placement.json", "placement_moving")
        app = self.hub.get(app_id)
        if app_id == "faustus":
            urls = self.hub.config.faustus_urls
            base = faustus_base or (urls[0] if urls else "")
            app = App("faustus", "Faustus", "", "", base, HEALTH_PATH, None)
        if app is None:
            return Observation(FAILED, "Hub registry", "app_missing")
        if app.port in EXCLUDED_PORTS:
            return Observation(UNCHECKED, "Hub registry", "endpoint_not_verified")
        if not remote and app_id != owner_id:
            owner = self.hub.get(owner_id)
            if owner is not None and owner.url:
                prefix = urlsplit(app.url).path
                app = replace(app, url=owner.url.rstrip("/") + prefix)
        if remote:
            cached = self._remote_health(state)
            if cached is None:
                return Observation(UNCHECKED, "placement health cache", "remote_state_unknown")
            if cached.get("ok") is False:
                return Observation(FAILED, "placement health cache", "remote_unreachable")
            row = (cached.get("apps") or {}).get(owner_id, {})
            if row.get("service") in ("inactive", "failed", "deactivating"):
                return Observation(STOPPED, "placement health cache", "no_running_service")
            if row.get("service") not in ("active", "reloading"):
                return Observation(UNCHECKED, "placement health cache", "remote_state_unknown")
            base = (state.get("urls") or {}).get(owner_id)
            if not base or (faustus_base and faustus_base.rstrip("/") != base.rstrip("/")):
                return Observation(UNCHECKED, "placement.json", "endpoint_not_verified")
            prefix = urlsplit(app.url).path if app_id != owner_id else ""
            app = replace(app, url=base.rstrip("/") + prefix)

        parsed = urlsplit(app.url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port in EXCLUDED_PORTS
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            return Observation(UNCHECKED, "Hub registry", "endpoint_not_verified")
        if not remote:
            if parsed.hostname not in LOOPBACK:
                return Observation(UNCHECKED, "Hub registry", "endpoint_not_verified")
            if self.listeners is None:
                return Observation(UNCHECKED, "Hub registry / OS listeners", "listener_state_unknown")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if port not in self.listeners:
                return Observation(STOPPED, "Hub registry / OS listeners", "no_listener")
        return app

    def _http(self, app, path, deadline, *, native_tool=None):
        if app.id in self.excluded or app.port in EXCLUDED_PORTS:
            return Observation(UNCHECKED, "link-check.json", "operator_excluded"), None
        if native_tool is not None:
            if (app.id, native_tool) not in APP_READ_TOOLS or path != "/api/agent/call":
                raise ValueError("App tool is outside the link checker allowlist")
            method = "POST"
            args = {"name": native_tool, "tool": native_tool, "arguments": {}}
        else:
            if path not in GET_PATHS:
                raise ValueError("GET path is outside the link checker allowlist")
            method, args = "GET", None
        call = f"{app.id} {method} {path}" + (f" ({native_tool})" if native_tool else "")
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            return Observation(FAILED, call, "probe_timeout"), None
        status, body = self._request(app, method, path, args, timeout=seconds)
        if status is None:
            return Observation(FAILED, call, "no_http_response"), None
        if not 200 <= status < 300:
            return Observation(FAILED, call, "http_error", {"http_status": status}), None
        return Observation(RESPONDS, call, "http_success", {"http_status": status}), body

    def catalogue(self, app_id, deadline, required=()):
        app = self._get_app(app_id, deadline)
        return app if isinstance(app, Observation) else self._catalogue(app, deadline, required)

    def _catalogue(self, app, deadline, required):
        module = NATIVE_COMPATIBILITY.get(app.id)
        prefix = f"/compat/{module}" if module and app.id != "scheherazade" else ""
        if prefix and not urlsplit(app.url).path.endswith(prefix):
            app = replace(app, url=app.url.rstrip("/") + prefix)
        obs, body = self._http(app, CATALOGUE_PATH, deadline)
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
        app = self._get_app(app_id, deadline)
        if isinstance(app, Observation):
            return app
        health = self._health(app, deadline)
        if health.state != RESPONDS:
            return health
        obs, body = self._http(app, "/api/agent/call", deadline, native_tool=tool)
        if obs.state != RESPONDS:
            return obs
        if isinstance(body, dict) and isinstance(body.get("modules"), list):
            return replace(obs, outcome="module_inventory", metrics={**obs.metrics, "modules": len(body["modules"])})
        return replace(obs, state=FAILED, outcome="unexpected_response")

    def _health(self, app, deadline):
        if app.health_path != HEALTH_PATH:
            return Observation(UNCHECKED, "Hub registry", "health_route_not_reviewed")
        obs, body = self._http(app, HEALTH_PATH, deadline)
        if obs.state != RESPONDS:
            return obs
        if (not isinstance(body, dict) or body.get("ok") is False
                or (app.expect_service and body.get("service") != app.expect_service)):
            return replace(obs, state=FAILED, outcome="unexpected_response")
        return replace(obs, scope="health")

    def health(self, app_id, deadline):
        app = self._get_app(app_id, deadline)
        return app if isinstance(app, Observation) else self._health(app, deadline)

    def compatibility(self, app_id, deadline):
        app = self._get_app(app_id, deadline)
        if isinstance(app, Observation):
            return app
        module = NATIVE_COMPATIBILITY.get(app_id)
        if module:
            declaration = app.family.get("absorbed_by")
            if (not isinstance(declaration, dict) or declaration.get("id") != COMPATIBILITY_OWNERS[app_id]
                    or declaration.get("module") != module):
                return Observation(FAILED, f"{app_id} compatibility manifest", "bridge_owner_missing")
            obs = self._catalogue(app, deadline, WRITER_COMPAT_TOOLS.get(app_id, ()))
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
        observation = self._catalogue(app, deadline, required)
        if observation.scope != "catalogue":
            return observation
        return replace(observation, called=f"{app_id} bridge.json + {observation.called}")

    def _remote_health(self, placement):
        cache = getattr(self.hub.placement, "_remote_health", None)
        checked = (cache or {}).get("checked")
        if not checked or checked < (placement.get("since") or 0) or time.time() - checked > REMOTE_HEALTH_TTL_S:
            return None
        return cache

    def placement(self):
        state = self.hub.placement.state()
        if (state.get("job") or {}).get("running"):
            return Observation(UNCHECKED, "placement.json", "placement_moving")
        if state["location"] != "sparks":
            return Observation(UNCHECKED, "placement.json", "not_placed_remotely")
        if not state.get("node"):
            return Observation(FAILED, "placement.json", "remote_node_missing")
        cached = self._remote_health(state)
        call = "placement.json + placement health cache"
        if cached is None:
            return Observation(UNCHECKED, call, "remote_state_unknown")
        if cached.get("ok") is False:
            return Observation(FAILED, call, "remote_unreachable")
        rows = cached.get("apps") or {}
        included = set(APPS) - self.excluded
        healthy = sum(1 for app in included if rows.get(app, {}).get("healthy") is True)
        metrics = {"services": len(included), "healthy_http": healthy, "seen_at": cached["checked"]}
        if healthy == len(included) and healthy:
            return Observation(RESPONDS, call, "recorded_health", metrics, "cached")
        if included and all(rows.get(app, {}).get("service") in ("inactive", "failed") for app in included):
            return Observation(STOPPED, call, "no_running_service", metrics, "cached")
        if healthy < len(included) and included <= rows.keys():
            return Observation(FAILED, call, "remote_unreachable", metrics, "cached")
        return Observation(UNCHECKED, call, "remote_state_unknown", metrics, "cached")

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
        gate = self._facet("mailgate")
        settings = gate.config()
        call = "Mail gateway cadence + last successful pass"
        if not settings.get("enabled"):
            return Observation(STOPPED, call, "mail_disabled")
        # Access the already-open store only. The property would initialise a
        # database and the ordinary status endpoint also loads account data.
        store = gate._store
        last_ok = store.get_state("last_ok_ts") if store is not None else None
        if not isinstance(last_ok, (int, float)) or last_ok <= 0:
            return Observation(UNCHECKED, call, "mail_no_pass")
        interval_s = 60 * settings["interval_min"]
        age = max(0, round(gate.clock() - last_ok))
        return Observation(RESPONDS if age <= interval_s else FAILED, call,
                           "mail_fresh" if age <= interval_s else "mail_stale",
                           {"last_success_at": last_ok, "age_s": age, "interval_s": interval_s}, "recorded_receipt")

    def fleet(self, deadline, reader=None):
        if reader:
            app = self._get_app(reader, deadline)
            if isinstance(app, Observation):
                return app
        call = "Fleet recorded watcher receipts"
        value = self._facet("fleet").cached
        if not value:
            return Observation(UNCHECKED, call, "fleet_no_snapshot", scope="cached")
        owners = ({reader} if reader else {"tantalus", "phileas", "hermes"}) - self.excluded
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
        return Observation(state, call, "fleet_receipts" if state != UNCHECKED else "fleet_no_receipt", metrics, "cached")

    def mail_library(self, deadline):
        from .drift import find_vendored
        canonical = Path(__file__).resolve().parents[1]
        py = (canonical / "fam_mail.py").read_bytes()
        js = (canonical.parent / "js" / "hoard-link.js").read_bytes()
        total = differing = 0
        for app in self.hub.apps:
            if time.monotonic() >= deadline:
                return Observation(FAILED, "Vendored mail library bytes", "probe_timeout")
            if app.id in self.excluded or app.id in COMPATIBILITY_OWNERS or app.port in EXCLUDED_PORTS or not app.folder:
                continue
            candidates = [(path / "fam_mail.py", py) for path in find_vendored(app.folder, canonical)]
            candidates.append((Path(app.folder) / "server" / "hoard-link.js", js))
            for path, source in candidates:
                if path.is_file() and not path.is_symlink():
                    total += 1
                    differing += path.read_bytes() != source
        state = FAILED if differing else RESPONDS if total else UNCHECKED
        return Observation(state, "Vendored mail library bytes", "vendor_copies", {"copies": total, "differing": differing}, "source")

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
        app = self._get_app("faustus", deadline, faustus_base=brain.target().get("base"))
        if isinstance(app, Observation):
            return app
        obs = self._health(app, deadline)
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
        app = self._get_app("faustus", deadline)
        if isinstance(app, Observation):
            return app
        obs, body = self._http(app, "/api/hoard-copilot/capabilities", deadline)
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
                                "running_candidates": sum(isinstance(state, App) for state in states),
                                "stopped_providers": sum(isinstance(state, Observation) and state.state == STOPPED
                                                         for state in states)})

    def prerequisites(self, apps, reason, deadline):
        app_ids = apps.split(",")
        call = f"{apps} GET {HEALTH_PATH}"
        rows = []
        for app_id in app_ids:
            row = self.health(app_id, deadline)
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
        return self.catalogue(owner, deadline, required)

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
            rows = [self.catalogue(app, deadline, required) for app, required in (
                ("funes", ("transcribe_file",)), ("borges", ("library_reindex",)),
            )]
            return self.combine(rows, f"funes, borges GET {CATALOGUE_PATH}", "prerequisites")
        probes = {
            "hub_catalog": lambda: self.hub_catalogue(("hub_link_check", "hub_event_stats")),
            "relay": lambda: self.relay(deadline),
            "family_catalogues": lambda: self.family_catalogues(deadline),
            "mail": self.mail,
            "mail_reader": lambda: self.fleet(deadline, arg),
            "mail_library": lambda: self.mail_library(deadline),
            "fleet": lambda: self.fleet(deadline),
            "events": self.events,
            "faustus_events": lambda: self.events("faustus"),
            "rules": self.rules,
            "brain": lambda: self.brain(deadline),
            "agenda": lambda: self.agenda(deadline),
            "spheres": self.spheres,
            "lanes": self.lanes,
            "coordination": self.coordination,
            "web": self.web,
            "placement": self.placement,
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
