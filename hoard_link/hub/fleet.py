"""Cadence-aware family monitoring. Published receipts, not private databases or log bodies.

Health observations and confirmed outcomes remain separate. Monitoring never starts,
stops, retries or approves the work it observes.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..atomic import write_json_atomic
from . import autostart, procs, contract
from .agent_watch import snippet
from .facets import Facet, Reply
from .jobs import next_run, parse_at, parse_days, parse_every
from .registry import App
from .fleet_windows import read_tasks, metadata as windows_metadata

STATES = ("healthy", "silent", "error", "paused", "unknown")
MAX_ITEMS = 1000
_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


def stamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def deadline(item: dict, baseline: float) -> float | None:
    """After two actual scheduled opportunities; weekend gaps are not missed runs."""
    slots = item.get("expected_slots")
    if isinstance(slots, list) and len(slots) == 2 and all(stamp(s) for s in slots) and slots[0] < slots[1]:
        return float(slots[1])
    interval = parse_every(item.get("every"))
    if interval and interval >= 60:
        return baseline + interval * 2
    if parse_at(item.get("at")):
        first = next_run({**item, "last_run_ts": baseline}, baseline + .001)
        return next_run({**item, "last_run_ts": first}, first + .001) if first else None
    return None


def classify(item: dict, now: float) -> dict:
    out = {k: copy.deepcopy(item.get(k)) for k in ("id", "label", "kind", "owner", "every", "at", "days",
        "created_ts", "last_success_ts", "last_error_ts", "active", "observed_ts", "needs_user", "user_reason", "error_code",
        "timestamp_basis", "success_observed_ts", "expected_slots", "schedule_basis", "cadence_unverified", "partial")}
    success, error = stamp(item.get("last_success_ts")), stamp(item.get("last_error_ts"))
    if not item.get("enabled", True) or item.get("paused"):
        state, reason = "paused", "explicit_pause"
    elif item.get("clock_invalid"):
        state, reason = "unknown", "future_timestamp"
    elif item.get("unavailable"):
        state, reason = "unknown", "source_unavailable"
    elif any(ts and ts > now + 5 for ts in (success, error, stamp(item.get("created_ts")))):
        state, reason = "unknown", "future_timestamp"
    elif item.get("kind") == "app":
        health = item.get("health")
        state = {"healthy": "healthy", "down": "silent", "foreign": "error"}.get(health, "unknown")
        reason = {"healthy": "health_confirmed", "down": "app_down", "foreign": "wrong_service"}.get(health, "health_unverified")
    elif error and (not success or error > success or (error == success and item.get("last_status") == "error")):
        state, reason = "error", "failure_recorded"
    elif item.get("partial"):
        state, reason = "error", "partial_result"
    elif item.get("cadence_unverified"):
        state, reason = "unknown", "cadence_unverified"
    else:
        due = deadline(item, stamp(item.get('success_observed_ts')) or success or stamp(item.get("created_ts")) or now)
        out["silent_after_ts"] = due
        if due and now > due:
            state, reason = "silent", "two_expected_runs_missed"
        elif success:
            state, reason = "healthy", "success_within_schedule" if due else "last_result_only"
        else:
            state, reason = "unknown", "no_confirmed_success"
    out.update(state=state, reason=reason)
    return out


def brief(items: list[dict]) -> dict:
    return {"today": [i for i in items if i["state"] == "healthy" and not i.get("needs_user")],
            "waiting_for_you": [i for i in items if i.get("needs_user") and i["state"] != "paused"],
            "blocked": [i for i in items if i["state"] in ("silent", "error") and not i.get("needs_user")],
            "unknown": [i for i in items if i["state"] == "unknown" and not i.get("needs_user")],
            "paused": [i for i in items if i["state"] == "paused"]}


class FleetFacet(Facet):
    id = "fleet"

    def __init__(self, hub, *, clock=time.time, health_fn=None, background=True):
        super().__init__(hub)
        self.clock, self.health = clock, health_fn or procs.health
        self.background = background
        self.path = Path(hub.config.data_dir) / "fleet.json"
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.cached = None
        self.cfg = dict(getattr(hub.config, "fleet", {}) or {})
        self.data = {"schema": 1, "definitions": {}, "observations": {}, "providers": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(self.data, dict) or self.data.get("schema") != 1
                or any(not isinstance(self.data.get(k), dict) for k in ("definitions", "observations"))):
                raise ValueError("Invalid fleet journal; preserve it for review")
            for table in ("definitions", "observations"):
                if any(not isinstance(key, str) or not _ID.fullmatch(key) or not isinstance(row, dict)
                       for key, row in self.data[table].items()):
                    raise ValueError("Invalid fleet journal records; preserve them for review")
        self.data.setdefault("providers", {})
        if not isinstance(self.data["providers"], dict) or any(not isinstance(value, dict) or not isinstance(value.get("items"), list) for value in self.data["providers"].values()):
            raise ValueError("Invalid fleet provider cache; preserve it for review")

    def save(self):
        write_json_atomic(self.path, self.data)

    def register(self, body):
        if not isinstance(body, dict) or set(body) - {"id", "label", "owner", "kind", "every", "at", "days", "enabled"}:
            return {"ok": False, "status": 400, "error": "Invalid expectation fields"}
        item = dict(body)
        if not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]):
            return {"ok": False, "status": 400, "error": "Invalid expectation id"}
        if item["id"].startswith(("app:", "job:", "rule:")):
            return {"ok": False, "status": 400, "error": "app:, job: and rule: identify native Hub observations"}
        if item.get("kind") not in ("watcher", "scheduled_task", "rule") or not _ID.fullmatch(str(item.get("owner") or "")):
            return {"ok": False, "status": 400, "error": "Expected watcher, scheduled_task or rule and an owner"}
        every, at = parse_every(item.get("every")), parse_at(item.get("at"))
        if (bool(every) == bool(at) or (every and every < 60)
            or ("every" in item and not every) or ("at" in item and not at)
            or (item.get("days") and not parse_days(item["days"]))):
            return {"ok": False, "status": 400, "error": "Supply one cadence: every (at least 1m) or at with valid days"}
        if "enabled" in item and type(item["enabled"]) is not bool:
            return {"ok": False, "status": 400, "error": "enabled must be boolean"}
        item["label"] = snippet(str(item.get("label") or item["id"]), 120)
        with self.lock:
            old = self.data["definitions"].get(item["id"], {})
            if not old and len(self.data["definitions"]) >= MAX_ITEMS:
                return {"ok": False, "status": 400, "error": "Fleet expectation capacity reached"}
            if old and item["owner"] != old.get("owner"):
                return {"ok": False, "status": 409, "error": "Expectation owner cannot be changed"}
            item["created_ts"] = old.get("created_ts") or self.clock()
            self.data["definitions"][item["id"]] = item
            self.save(); self.cached = None
        return {"ok": True, "expectation": copy.deepcopy(item)}

    def report(self, body, *, caller=None):
        if not isinstance(body, dict) or set(body) - {"id", "status", "ts", "error_code", "user_reason"}:
            return {"ok": False, "status": 400, "error": "Invalid receipt fields"}
        if not isinstance(body.get("id"), str) or not _ID.fullmatch(body["id"]):
            return {"ok": False, "status": 400, "error": "Invalid receipt id"}
        now = self.clock()
        ts = stamp(body.get("ts", now))
        if not ts or ts > now + 5 or body.get("status") not in ("ok", "error", "paused", "waiting"):
            return {"ok": False, "status": 400, "error": "Invalid receipt status or timestamp"}
        with self.lock:
            item = self.data["definitions"].get(body.get("id"))
            if not item:
                return {"ok": False, "status": 404, "error": "Register the expectation first"}
            if caller and caller != item["owner"]:
                return {"ok": False, "status": 403, "error": "Receipt belongs to another app"}
            record = self.data["observations"].setdefault(item["id"], {})
            if ts < (record.get("receipt_ts") or 0):
                return {"ok": True, "ignored": "older_receipt", "id": item["id"]}
            record.update(receipt_ts=ts, paused=body["status"] == "paused", needs_user=body["status"] == "waiting")
            if body["status"] in ("ok", "error"): record["last_status"] = body["status"]
            if body["status"] == "ok": record["last_success_ts"] = ts
            if body["status"] == "error": record["last_error_ts"] = ts
            reason = body.get("user_reason")
            record["user_reason"] = reason if reason in ("login", "approval", "configuration", "other") else None
            code = body.get("error_code")
            record["error_code"] = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", code) else None
            self.save(); self.cached = None
        return {"ok": True, "id": item["id"]}

    def app_items(self, now):
        ids = set(self.cfg.get("expected_apps") or [])
        if self.cfg.get("autostart", True):
            startup = autostart.status()
            profile = self.hub.profiles().get(startup.get("profile")) if startup.get("installed") else None
            if profile: ids.update(profile.members)
        ids.update(str(app.id) for app in getattr(self.hub, "apps", []) if app.family.get("fleet") is True)
        if self.cfg.get('faustus'): ids.add('faustus')
        items = []
        apps = []
        for app_id in sorted(ids):
            app = self.provider_apps().get(app_id) or self.hub.get(app_id)
            item = {"id": "app:" + app_id, "label": snippet(app.name, 120) if app else app_id,
                    "kind": "app", "owner": "hub", "created_ts": self.hub.started_at,
                    "paused": self.hub.automation_blocked(app_id), "observed_ts": now}
            if not app: item["unavailable"] = True
            elif not item["paused"]: apps.append((item, app))
            items.append(item)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [(item, pool.submit(self.health, app)) for item, app in apps]
            for item, future in futures:
                try: item["health"] = future.result().state
                except Exception: item["unavailable"] = True
        return items

    def reconcile(self, *, notify=True):
        with self.refresh_lock:
            return self._reconcile(notify=notify)

    def provider_apps(self):
        apps = {app.id: app for app in getattr(self.hub, 'apps', [])}
        if self.cfg.get('faustus') and getattr(self.hub.config, 'faustus_urls', None):
            apps['faustus'] = App(id='faustus', name='Faustus', purpose='Published task monitoring', folder='',
                url=self.hub.config.faustus_urls[0], health_path='/api/health', expect_service='faustus',
                token_file=self.hub.config.token_file, family={'fleet': True})
        return apps

    def provider_items(self, now, errors):
        out = []
        wanted = set(self.cfg.get("providers") or [])
        apps = self.provider_apps()
        wanted.update(str(app.id) for app in apps.values() if app.family.get("fleet") is True)
        allowed = {"id", "owner", "kind", "label", "created_ts", "every", "at", "days", "enabled", "paused",
            "last_success_ts", "last_error_ts", "last_status", "needs_user", "user_reason", "partial", "error_code",
            "expected_slots", "schedule_basis", "cadence_unverified"}
        def fetch(app_id):
            app = apps.get(app_id) or self.hub.get(app_id)
            if not app: return app_id, None
            if self.hub.automation_blocked(app_id): return app_id, None
            try:
                status, data = contract._get(app.url + "/api/family/fleet", contract.read_token(app.token_file), 2.0)
                if status == 200 and isinstance(data, dict) and data.get("ok") and data.get("owner") == app_id and isinstance(data.get("items"), list):
                    rows = []
                    for raw in data["items"][:MAX_ITEMS]:
                        if (not isinstance(raw, dict) or raw.get("owner") != app_id or not isinstance(raw.get("id"), str)
                            or not raw["id"].startswith(app_id + ":") or not _ID.fullmatch(raw["id"])
                            or raw.get("kind") not in ("watcher", "scheduled_task", "rule")):
                            return app_id, None
                        rows.append({k: copy.deepcopy(v) for k, v in raw.items() if k in allowed})
                    return app_id, {"items": rows, "observed_ts": now,
                        "truncated": len(data["items"]) > MAX_ITEMS or bool((data.get("coverage") or {}).get("truncated"))}
            except Exception: pass
            return app_id, None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fetch, sorted(wanted)))
        with self.lock:
            for app_id, fresh in results:
                if fresh is not None:
                    self.data["providers"][app_id] = fresh
                    if fresh["truncated"]: errors["provider:" + app_id] = "truncated"
                elif not self.hub.automation_blocked(app_id): errors["provider:" + app_id] = "source_unavailable"
                cached = self.data["providers"].get(app_id, {})
                for row in cached.get("items", []):
                    item = {**copy.deepcopy(row), "observed_ts": cached.get("observed_ts"), "unavailable": fresh is None, "_published": True}
                    if self.hub.automation_blocked(app_id): item["paused"] = True
                    out.append(item)
        return out

    def _reconcile(self, *, notify=True):
        now = self.clock()
        sources, errors = [], {}
        try: sources.extend(self.app_items(now))
        except Exception: errors["apps"] = "source_unavailable"
        try:
            for job in self.hub.jobs.list():
                last = job.get("last") or {}
                sources.append({"id": "job:" + job["id"], "label": snippet(job.get("name") or job["id"], 120),
                    "kind": "job", "owner": "hub", "created_ts": job.get("created_ts"),
                    "enabled": job.get("enabled", True) and self.hub.config.jobs_enabled,
                    "every": job.get("every"), "at": job.get("at"), "days": job.get("days"),
                    "active": job.get("running", False), "observed_ts": now,
                    "last_status": "ok" if last.get("ok") is True else "error" if last.get("ok") is False else None,
                    **({"last_success_ts": last.get("ts")} if last.get("ok") is True else {}),
                    **({"last_error_ts": last.get("ts"), "error_code": "job_failed"} if last.get("ok") is False else {})})
        except Exception: errors["jobs"] = "source_unavailable"
        try:
            for rule in self.hub.rules.list():
                last = rule.get("last") or {}
                cadence = (self.cfg.get("rule_cadences") or {}).get(rule["id"], {})
                sources.append({"id": "rule:" + rule["id"], "label": snippet(rule.get("name") or rule["id"], 120),
                    "kind": "rule", "owner": "hub", "created_ts": rule.get("created_ts"),
                    "enabled": rule.get("enabled", True), "observed_ts": now,
                    **{k: cadence.get(k) for k in ("every", "at", "days")},
                    "last_status": "ok" if last.get("ok") is True else "error" if last.get("ok") is False else None,
                    **({"last_success_ts": last.get("ts")} if last.get("ok") is True else {}),
                    **({"last_error_ts": last.get("ts"), "error_code": "rule_failed"} if last.get("ok") is False else {})})
        except Exception: errors["rules"] = "source_unavailable"
        sources.extend(self.provider_items(now, errors))
        if self.cfg.get('windows_tasks'):
            windows = read_tasks(self.cfg['windows_tasks'])
            if not windows.get('ok'): errors['windows'] = windows.get('error') or 'source_unavailable'
            else:
                sources.extend(windows_metadata(row, now) for row in windows['items'])
                missing=set(self.cfg['windows_tasks'])- {row.get('name') for row in windows['items']} - {str(row.get('path') or '')+str(row.get('name') or '') for row in windows['items']}
                if missing: errors['windows_missing'] = 'declared_task_not_found'
        with self.lock:
            sources.extend(copy.deepcopy(list(self.data["definitions"].values())))
            items = []
            transitions = []
            seen = set()
            for source in sources[:MAX_ITEMS]:
                if source["id"] in seen:
                    errors["duplicate:" + source["id"]] = "conflicting_definition"
                    continue
                seen.add(source["id"])
                source["label"] = snippet(str(source.get("label") or source["id"]), 120)
                rec = self.data["observations"].setdefault(source["id"], {})
                rec.setdefault('first_seen_ts', now)
                if not stamp(source.get('created_ts')): source['created_ts'] = rec['first_seen_ts']
                if source['id'].startswith('windows:task:'):
                    completed_start = stamp(source.get('last_success_ts'))
                    if completed_start and completed_start <= now + 5 and rec.get('completed_start_ts') != completed_start:
                        rec.update(completed_start_ts=completed_start, completed_observed_ts=now)
                    source['success_observed_ts'] = rec.get('completed_observed_ts')
                for key in ("last_success_ts", "last_error_ts"):
                    value = stamp(source.get(key))
                    if value and value <= now + 5: rec[key] = max(value, rec.get(key) or 0)
                    if value and value > now + 5: source["clock_invalid"] = True
                merged = {**source, **{k: rec.get(k) for k in ("last_success_ts", "last_error_ts", "last_status", "paused", "needs_user", "user_reason", "error_code") if k in rec}}
                # Native pause/enable state wins over an older registered receipt.
                if source.get("_published") or source["id"].startswith(("app:", "job:", "rule:")):
                    merged["paused"] = source.get("paused", False)
                    for key in ("needs_user", "user_reason", "last_status"):
                        if key in source: merged[key] = source[key]
                item = classify(merged, now)
                signature = (item["state"], bool(item.get("needs_user")))
                previous = (rec.get("state"), bool(rec.get("needs_user_state")))
                if notify and signature != previous:
                    rec["generation"] = int(rec.get("generation") or 0) + 1
                    transitions.append((copy.deepcopy(item), rec["generation"]))
                if notify: rec.update(state=item["state"], needs_user_state=bool(item.get("needs_user")))
                items.append(item)
            self.save()
            value = {"ok": True, "generated_ts": now, "items": items, "brief": brief(items),
                "counts": {state: sum(i["state"] == state for i in items) for state in STATES},
                "coverage": {"apps": "declared_and_autostart", "jobs": "hub_jobs", "rules": "last_outcome; cadence_only_when_declared", "external": "registered_receipts_and_declared_publishers",
                    "truncated": len(sources) > MAX_ITEMS, "errors": errors, "os_tasks": "declared_task_scheduler_metadata" if self.cfg.get('windows_tasks') else "registration_required",
                    "monitoring_enabled": self.cfg.get("enabled", True)}}
            self.cached = copy.deepcopy(value)
        for item, generation in transitions:
            try:
                self.hub.events.emit("hub.fleet.changed", {"id": item["id"], "state": item["state"], "needs_user": bool(item.get("needs_user"))}, source="hub")
                channel = self.hub.facet("notify")
                critical = item["state"] == "silent" or (item.get("needs_user") and item["state"] != "paused")
                if critical and self.cfg.get("enabled", True) and self.cfg.get("notifications", True) and channel:
                    es = self.hub.config.language != "en"
                    channel.send("Flota: necesita tu atención" if es else "Fleet: needs your attention",
                        item["label"], app="hub", group="fleet", dedupe_key=f"fleet:{item['id']}:{generation}")
            except Exception: pass  # monitoring cannot break the work it observes
        return value

    def status(self, *, refresh=False):
        with self.lock:
            if not refresh and self.cached and self.clock() - self.cached["generated_ts"] < 60:
                return copy.deepcopy(self.cached)
        return self.reconcile(notify=False)

    def start(self):
        if not self.background or not self.cfg.get("enabled", True): return
        def loop():
            while not self.stop_event.wait(60):
                try: self.reconcile()
                except Exception: pass
        self.thread = threading.Thread(target=loop, daemon=True, name="hoard-fleet")
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=2)

    def get(self, req):
        if req.path == "/api/fleet":
            if req.caller() is None:
                return Reply({'ok': False, 'error': 'A family bearer token or the Hub page is required'}, status=401)
            return self.status(refresh=req.q_bool("refresh"))

    def post(self, req):
        if req.path == "/api/fleet/expectations":
            if not req.agent(): return Reply({"ok": False, "error": "Hub token required"}, status=401)
            return self.register(req.body)
        if req.path == "/api/fleet/receipt":
            caller = req.caller()
            if not req.agent() and not caller: return Reply({"ok": False, "error": "App token required"}, status=401)
            return self.report(req.body, caller=None if req.agent() else caller)

    @classmethod
    def tools(cls):
        expectation = {"type": "object", "required": ["id", "owner", "kind"], "properties": {
            "id": {"type": "string"}, "owner": {"type": "string"}, "kind": {"type": "string", "enum": ["watcher", "scheduled_task", "rule"]},
            "label": {"type": "string"}, "every": {"type": "string"}, "at": {"type": "string"},
            "days": {"type": "array", "items": {"type": "string"}}, "enabled": {"type": "boolean"}}}
        receipt = {"type": "object", "required": ["id", "status"], "properties": {"id": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "error", "paused", "waiting"]}, "ts": {"type": "number"},
            "error_code": {"type": "string"}, "user_reason": {"type": "string", "enum": ["login", "approval", "configuration", "other"]}}}
        return [{"name": name, "description": desc + "\nSinónimos: flota, parado en silencio, estado de vigilantes, esperando por ti",
            "inputSchema": schema, "annotations": {"readOnlyHint": readonly, "destructiveHint": False, "idempotentHint": readonly}}
            for name, desc, schema, readonly in [
                ("hub_fleet_status", "Read confirmed outcomes and cadence-aware fleet states, brief and coverage. Does not start or stop work.", {"type": "object", "properties": {"refresh": {"type": "boolean"}}}, True),
                ("hub_fleet_register", "Declare an external watcher, scheduled task or rule's expected cadence and owner; no work is launched.", expectation, False),
                ("hub_fleet_report", "Report a minimal confirmed outcome for a registered expectation; older receipts are ignored with a notice.", receipt, False)]]

    def handlers(self):
        return {"hub_fleet_status": lambda a: self.status(refresh=bool(a.get("refresh"))),
                "hub_fleet_register": self.register, "hub_fleet_report": self.report}
