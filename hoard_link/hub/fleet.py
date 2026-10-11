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
APP_CONFIRM_S = 60.0       # an app is only silent after its health probe failed for this long (and twice)
APP_PUSH_DELAY_S = 60.0    # ...and a push for an app waits this long more, so Cassandra's own incident can cover it
APP_PUSH_GAP_S = 1800.0    # at most one fleet push per app per half hour (a flapping app is one burst)
CASSANDRA_WINDOW_S = 1800.0
STORM_LIMIT = 3            # more pushable transitions than this in one tick become one summary notification
WAITING_FRESH_S = 600.0    # a first-seen "waiting for you" receipt this recent is still worth a push
REFRESH_WAIT_S = 1.0       # most a request waits for a background refresh before it answers with the cache
MIN_RECONCILE_GAP_S = 3.0  # receipts arriving in a burst wake the background reconcile at most this often
KIND_NAMES = {"es": {"app": "App", "job": "Tarea del Hub", "rule": "Regla", "watcher": "Vigilante", "scheduled_task": "Tarea programada"},
              "en": {"app": "App", "job": "Hub job", "rule": "Rule", "watcher": "Watcher", "scheduled_task": "Scheduled task"}}
_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


def stamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def deadline(item: dict, baseline: float) -> float | None:
    """After two actual scheduled opportunities; weekend gaps are not missed runs."""
    slots = item.get("expected_slots")
    if isinstance(slots, list) and len(slots) == 2 and all(stamp(s) for s in slots) and slots[0] < slots[1]:
        resumed = stamp(item.get("resumed_ts"))
        # Slots computed from before the task was resumed would make a task paused for days "silent" the
        # moment it is switched on again: until its producer republishes slots from the resume, no deadline.
        if resumed and slots[1] <= resumed:
            return None
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
        "timestamp_basis", "success_observed_ts", "expected_slots", "schedule_basis", "cadence_unverified", "partial", "resumed_ts")}
    out["no_cadence"] = (item.get("kind") != "app" and not (item.get("every") or item.get("at") or item.get("expected_slots"))
                         and not item.get("cadence_unverified"))
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
        if health == "down" and not item.get("down_confirmed"):
            state, reason = "unknown", "app_down_unconfirmed"   # one failed probe is not an outage
        else:
            state = {"healthy": "healthy", "down": "silent", "foreign": "error"}.get(health, "unknown")
            reason = {"healthy": "health_confirmed", "down": "app_down", "foreign": "wrong_service"}.get(health, "health_unverified")
    elif error and (not success or error > success or (error == success and item.get("last_status") == "error")):
        state, reason = "error", "failure_recorded"
    elif item.get("partial"):
        state, reason = "error", "partial_result"
    elif item.get("cadence_unverified"):
        state, reason = "unknown", "cadence_unverified"
    else:
        # The clock starts at the latest of: the last success, the declaration and the last resume from a pause.
        baseline = max([v for v in (stamp(item.get('success_observed_ts')) or success, stamp(item.get("created_ts")),
                                    stamp(item.get("resumed_ts"))) if v] or [now])
        due = deadline(item, baseline)
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
    """Five groups for the person. Items with no declared cadence (rules, event tasks) cannot be certified
    on time, so unless they failed, wait for the person or are paused they sit apart in ``no_cadence``."""
    loose = [i for i in items if i.get("no_cadence") and i["state"] in ("healthy", "unknown") and not i.get("needs_user")]
    ids = {id(i) for i in loose}
    return {"today": [i for i in items if i["state"] == "healthy" and not i.get("needs_user") and id(i) not in ids],
            "waiting_for_you": [i for i in items if i.get("needs_user") and i["state"] != "paused"],
            "blocked": [i for i in items if i["state"] in ("silent", "error") and not i.get("needs_user")],
            "unknown": [i for i in items if i["state"] == "unknown" and not i.get("needs_user") and id(i) not in ids],
            "paused": [i for i in items if i["state"] == "paused"],
            "no_cadence": loose}


def fallback_label(item: dict, language: str) -> str:
    names = KIND_NAMES["en" if language == "en" else "es"]
    return f"{names.get(item.get('kind'), 'Item')} {str(item.get('id') or '').rsplit(':', 1)[-1]}".strip()


def is_paused(item: dict) -> bool:
    return not item.get("enabled", True) or bool(item.get("paused"))


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
        # Stale-while-revalidate: requests answer from the cache the background reconcile keeps fresh. Without a
        # background (tests, embedded use) a read reconciles inline as before.
        self.swr = bool(background)
        self.dirty_seq = self.clean_seq = 0   # dirty until a reconcile that STARTED after the change has finished
        self.version = 0
        self.cv = threading.Condition(self.lock)
        self.wake = threading.Event()
        self.oneshot = None
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
            item["resumed_ts"] = self.clock() if (old and old.get("enabled") is False and item.get("enabled", True)) else old.get("resumed_ts")
            self.data["definitions"][item["id"]] = item
            self.save(); self._dirty()
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
            if record.get("paused") and body["status"] != "paused":
                record["resumed_ts"] = ts   # the clock for "silent" restarts when the producer is switched back on
            record.update(receipt_ts=ts, paused=body["status"] == "paused", needs_user=body["status"] == "waiting")
            if body["status"] in ("ok", "error"): record["last_status"] = body["status"]
            if body["status"] == "ok": record["last_success_ts"] = ts
            if body["status"] == "error": record["last_error_ts"] = ts
            reason = body.get("user_reason")
            record["user_reason"] = reason if reason in ("login", "approval", "configuration", "other") else None
            code = body.get("error_code")
            record["error_code"] = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", code) else None
            self.save(); self._dirty()
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
            "expected_slots", "schedule_basis", "cadence_unverified", "resumed_ts"}
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

    def _cassandra_covers(self, app_id, now):
        """Cassandra already tells the person about an app outage (one notice per burst): an incident it opened
        for this app in the last half hour, open or closed, means Fleet stays quiet about the same outage."""
        try:
            rows = self.hub.events.query(type="cassandra.incident.opened", text=app_id, since_ts=now - CASSANDRA_WINDOW_S, limit=200)
        except Exception:
            return False
        return any(isinstance(r.get("data"), dict) and r["data"].get("app") == app_id for r in rows)

    def _app_push_covered(self, item, rec, now):
        last = stamp(rec.get("last_push_ts"))
        if last is not None and now - last < APP_PUSH_GAP_S:
            return True
        return self._cassandra_covers(str(item["id"]).split(":", 1)[-1], now)

    def _reconcile(self, *, notify=True):
        seen_seq = self.dirty_seq   # anything that arrives while this runs asks for another pass
        now = self.clock()
        lang = "en" if self.hub.config.language == "en" else "es"
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
                    "resumed_ts": job.get("resumed_ts"),
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
            changes, pushes = [], []
            seen = set()
            for source in sources[:MAX_ITEMS]:
                if source["id"] in seen:
                    errors["duplicate:" + source["id"]] = "conflicting_definition"
                    continue
                seen.add(source["id"])
                source["label"] = snippet(str(source.get("label") or ""), 120) or fallback_label(source, lang)
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
                # Resuming from a pause restarts the clock: the producer's own stamp, or the moment we saw it switch on.
                paused_now = is_paused(merged)
                if rec.get("was_paused") and not paused_now: rec["resumed_ts"] = now
                rec["was_paused"] = paused_now
                merged["resumed_ts"] = max(stamp(source.get("resumed_ts")) or 0, stamp(rec.get("resumed_ts")) or 0) or None
                if source.get("kind") == "app":
                    # One failed health probe (1.5 s) is not an outage: down needs two failures spanning APP_CONFIRM_S.
                    if source.get("health") == "down":
                        rec.setdefault("down_since", now)
                        rec["down_probes"] = int(rec.get("down_probes") or 0) + 1
                        merged["down_confirmed"] = rec["down_probes"] >= 2 and now - rec["down_since"] >= APP_CONFIRM_S
                    else:
                        rec.pop("down_since", None); rec.pop("down_probes", None)
                item = classify(merged, now)
                critical = item["state"] == "silent" or bool(item.get("needs_user") and item["state"] != "paused")
                signature = (item["state"], bool(item.get("needs_user")))
                first = rec.get("state") is None
                previous = (rec.get("state"), bool(rec.get("needs_user_state")))
                def push(generation, rec=rec, item=item):
                    rec["last_push_ts"] = now
                    pushes.append((copy.deepcopy(item), generation))
                if notify and signature != previous:
                    rec["generation"] = int(rec.get("generation") or 0) + 1
                    if not first:
                        changes.append(copy.deepcopy(item))
                        if critical and item.get("kind") == "app":
                            rec["push_due_ts"], rec["push_gen"] = now + APP_PUSH_DELAY_S, rec["generation"]
                        elif critical:
                            push(rec["generation"])
                    elif critical and item.get("needs_user") and stamp(rec.get("receipt_ts")) and now - stamp(rec.get("receipt_ts")) < WAITING_FRESH_S:
                        push(rec["generation"])   # a brand-new "waiting for you" receipt; everything else first seen is a baseline
                if notify and rec.get("push_due_ts"):
                    if not critical: rec.pop("push_due_ts", None)
                    elif now >= rec["push_due_ts"]:
                        rec.pop("push_due_ts", None)
                        if not self._app_push_covered(item, rec, now): push(int(rec.get("push_gen") or rec.get("generation") or 1))
                if notify: rec.update(state=item["state"], needs_user_state=bool(item.get("needs_user")))
                items.append(item)
            self.save()
            value = {"ok": True, "generated_ts": now, "items": items, "brief": brief(items),
                "counts": {state: sum(i["state"] == state for i in items) for state in STATES},
                "coverage": {"apps": "declared_and_autostart", "jobs": "hub_jobs", "rules": "last_outcome; cadence_only_when_declared", "external": "registered_receipts_and_declared_publishers",
                    "truncated": len(sources) > MAX_ITEMS, "errors": errors, "os_tasks": "declared_task_scheduler_metadata" if self.cfg.get('windows_tasks') else "registration_required",
                    "monitoring_enabled": self.cfg.get("enabled", True)}}
            self.cached = copy.deepcopy(value)
            self.version += 1
            self.clean_seq = max(self.clean_seq, seen_seq)
            self.cv.notify_all()
        for item in changes:
            try: self.hub.events.emit("hub.fleet.changed", {"id": item["id"], "state": item["state"], "needs_user": bool(item.get("needs_user"))}, source="hub")
            except Exception: pass  # monitoring cannot break the work it observes
        self._send(pushes, lang, now)
        return value

    def _send(self, pushes, lang, now):
        """Push the transitions into silent / waiting-for-you. A burst becomes one summary, not a storm."""
        try:
            channel = self.hub.facet("notify")
            if not pushes or not channel or not self.cfg.get("enabled", True) or not self.cfg.get("notifications", True): return
            es = lang == "es"
            if len(pushes) > STORM_LIMIT:
                names = [item["label"] or fallback_label(item, lang) for item, _ in pushes]
                body = "\n".join(names[:8]) + (("\n" + (f"y {len(names) - 8} más" if es else f"and {len(names) - 8} more")) if len(names) > 8 else "")
                channel.send(f"Flota: {len(names)} cosas necesitan tu atención" if es else f"Fleet: {len(names)} things need your attention",
                    body, app="hub", group="fleet", dedupe_key=f"fleet:summary:{int(now)}")
                return
            for item, generation in pushes:
                try:
                    channel.send("Flota: necesita tu atención" if es else "Fleet: needs your attention",
                        item["label"] or fallback_label(item, lang), app="hub", group="fleet", dedupe_key=f"fleet:{item['id']}:{generation}")
                except Exception: pass
        except Exception: pass

    def _dirty(self):
        """Something changed (a registration, a receipt): refresh in the background, never inside the request."""
        if self.swr:
            self.dirty_seq += 1
            self.wake_up()
        else:
            self.cached = None

    @property
    def dirty(self):
        return self.dirty_seq > self.clean_seq

    def wake_up(self):
        if self.thread and self.thread.is_alive():
            self.wake.set()
            return
        with self.lock:
            if self.stop_event.is_set() or (self.oneshot and self.oneshot.is_alive()): return
            self.oneshot = threading.Thread(target=self._refresh_once, daemon=True, name="hoard-fleet-refresh")
            self.oneshot.start()

    def _refresh_once(self):
        while not self.stop_event.is_set():
            try: self.reconcile(notify=False)   # a read never consumes a transition; only the background loop notifies
            except Exception: pass
            if not self.dirty: return
            self.stop_event.wait(MIN_RECONCILE_GAP_S)

    def _warming(self):
        return {"ok": True, "generated_ts": None, "items": [], "brief": brief([]), "counts": {state: 0 for state in STATES},
                "coverage": {"apps": "declared_and_autostart", "jobs": "hub_jobs", "rules": "last_outcome; cadence_only_when_declared",
                    "external": "registered_receipts_and_declared_publishers", "truncated": False, "errors": {"fleet": "warming_up"},
                    "os_tasks": "registration_required", "monitoring_enabled": self.cfg.get("enabled", True)},
                "refreshing": True}

    def status(self, *, refresh=False, wait=None):
        """The fleet view. ``wait`` is the most (seconds) a caller waits for a background refresh; 0 never waits.

        With a background thread this serves the last reconcile at once (a refresh, a receipt or an old cache only
        start a new pass in the background and the answer says ``refreshing``). Without one it reconciles inline."""
        if not self.swr:
            with self.lock:
                if not refresh and self.cached and self.clock() - self.cached["generated_ts"] < 60:
                    return copy.deepcopy(self.cached)
            return self.reconcile(notify=False)
        wait = REFRESH_WAIT_S if wait is None else wait
        looping = bool(self.thread and self.thread.is_alive())
        with self.cv:
            cached, version = self.cached, self.version
            old = cached is None or self.clock() - cached["generated_ts"] >= (90 if looping else 60)
            needs = cached is None or refresh or self.dirty or old
        if needs:
            self.wake_up()
            if wait > 0 and (cached is None or refresh or self.dirty):
                with self.cv: self.cv.wait_for(lambda: self.version > version, timeout=wait)
        with self.lock:
            fresh = self.cached
            if fresh is None: return self._warming()
            out = copy.deepcopy(fresh)
            out["refreshing"] = bool(needs and self.version == version)
        return out

    def start(self):
        if not self.background or not self.cfg.get("enabled", True): return
        def loop():
            if self.stop_event.wait(2): return
            while not self.stop_event.is_set():
                self.wake.clear()
                try: self.reconcile()
                except Exception: pass
                ended = time.monotonic()
                woke = self.wake.wait(60)
                if self.stop_event.is_set(): return
                # A burst of receipts is one pass, not one per receipt; the first one after a quiet spell is immediate.
                if woke: self.stop_event.wait(max(0.0, MIN_RECONCILE_GAP_S - (time.monotonic() - ended)))
        self.thread = threading.Thread(target=loop, daemon=True, name="hoard-fleet")
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
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
