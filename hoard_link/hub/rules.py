"""Automations: *when* an event lands, *then* run actions.

A rule is ``{id, name, when, then, enabled, cooldown_s, note}`` kept in
``<data>/rules.json``. ``when`` is ``{"type": "scribe.transcript.*",
"source": "scribe", "where": {"data.kind": "meeting"}}`` — ``type`` is a
glob (``|`` joins several), ``source`` optional, ``where`` optional exact
matches on dotted paths of the event (a list value means "any of"). ``then``
is the action list of ``actions.py``, with ``${event.data.x}`` available.

The engine listens to the event log and runs matching rules on one
worker thread, in order, so a burst of events never spawns a burst of
threads and two rules never race on the same app. Guards against loops:
a rule never fires on ``hub.rule.*`` events, nor on an event that one of
its own actions emitted (``data._via_rule``), and ``cooldown_s`` (default
5) caps how often each rule can fire. Every run is recorded as a
``hub.rule.ran`` event and in the rule's ``last`` field, so "why did that
happen at 03:12?" has an answer.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Callable, Optional

from . import actions
from .events import EventLog, matches

DEFAULT_COOLDOWN_S = 5.0
HISTORY = 100


def _get(event: dict[str, Any], path: str) -> Any:
    cur: Any = event
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def rule_matches(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    when = rule.get("when") or {}
    if not matches(str(when.get("type") or "*"), event.get("type", "")):
        return False
    src = when.get("source")
    if src and str(src) != event.get("source"):
        return False
    where = when.get("where") or {}
    if isinstance(where, dict):
        for path, wanted in where.items():
            have = _get(event, path if path.startswith(("data.", "type", "source")) else "data." + path)
            if isinstance(wanted, list):
                if have not in wanted and str(have) not in [str(w) for w in wanted]:
                    return False
            elif have != wanted and str(have) != str(wanted):
                return False
    return True


def validate_rule(rule: Any) -> list[str]:
    if not isinstance(rule, dict):
        return ["rule must be an object"]
    problems: list[str] = []
    when = rule.get("when")
    if not isinstance(when, dict) or not when.get("type"):
        problems.append("'when' needs a type pattern (e.g. 'scribe.*')")
    problems += actions.validate(rule.get("then"))
    return problems


class RuleEngine:
    def __init__(self, path: Optional[str], events: EventLog, runner: Callable[[list[dict[str, Any]], dict[str, Any], str], list[dict[str, Any]]],
                 *, now: Callable[[], float] = time.time):
        self.path = path
        self.events = events
        self._run_actions = runner
        self._now = now
        self._lock = threading.RLock()
        self.rules: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self._last_fire: dict[str, float] = {}
        self._queue: "queue.Queue[tuple[dict[str, Any], dict[str, Any]]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._load()
        self._off = events.subscribe(self._on_event)

    # -- persistence ----------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            raw = json.loads(open(self.path, "r", encoding="utf-8-sig").read())
        except (OSError, ValueError):
            raw = []
        rules = raw.get("rules") if isinstance(raw, dict) else raw
        with self._lock:
            self.rules = [r for r in (rules or []) if isinstance(r, dict) and r.get("id")]

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"rules": self.rules}, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, self.path)

    # -- CRUD -------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.rules]

    def get(self, rule_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for r in self.rules:
                if r.get("id") == rule_id:
                    return dict(r)
        return None

    def add(self, rule: dict[str, Any]) -> dict[str, Any]:
        problems = validate_rule(rule)
        if problems:
            return {"ok": False, "error": "; ".join(problems)}
        with self._lock:
            ids = {r["id"] for r in self.rules}
            rid = str(rule.get("id") or "").strip() or actions.new_id("rule", ids)
            if rid in ids:
                return {"ok": False, "error": f"rule id already exists: {rid}"}
            rec = {"id": rid, "name": str(rule.get("name") or rid), "when": rule["when"], "then": rule["then"],
                   "enabled": bool(rule.get("enabled", True)), "cooldown_s": float(rule.get("cooldown_s", DEFAULT_COOLDOWN_S)),
                   "note": str(rule.get("note") or ""), "created_ts": self._now(), "runs": 0, "last": None}
            self.rules.append(rec)
            self._save()
        return {"ok": True, "rule": dict(rec)}

    def update(self, rule_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for r in self.rules:
                if r.get("id") == rule_id:
                    cand = {**r, **{k: v for k, v in patch.items() if k in ("name", "when", "then", "enabled", "cooldown_s", "note")}}
                    problems = validate_rule(cand)
                    if problems:
                        return {"ok": False, "error": "; ".join(problems)}
                    r.update(cand)
                    self._save()
                    return {"ok": True, "rule": dict(r)}
        return {"ok": False, "error": f"unknown rule: {rule_id}"}

    def install_examples(self) -> dict[str, Any]:
        """Add every recommended rule not yet present (by id). Idempotent; existing rules are untouched."""
        installed, present = [], []
        for ex in example_rules():
            if self.get(ex["id"]) is not None:
                present.append(ex["id"])
                continue
            res = self.add(dict(ex))
            if res.get("ok"):
                installed.append(ex["id"])
        return {"ok": True, "installed": installed, "already_present": present, "rules": self.list()}

    def remove(self, rule_id: str) -> dict[str, Any]:
        with self._lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if r.get("id") != rule_id]
            if len(self.rules) == before:
                return {"ok": False, "error": f"unknown rule: {rule_id}"}
            self._save()
        return {"ok": True, "removed": rule_id}

    # -- matching -----------------------------------------------------------
    def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype.startswith("hub.rule."):
            return
        via = (event.get("data") or {}).get("_via_rule") if isinstance(event.get("data"), dict) else None
        with self._lock:
            due = []
            for r in self.rules:
                if not r.get("enabled", True) or r.get("id") == via or not rule_matches(r, event):
                    continue
                cooldown = float(r.get("cooldown_s", DEFAULT_COOLDOWN_S) or 0)
                last = self._last_fire.get(r["id"], -1e12)
                if self._now() - last < cooldown:
                    r["skipped"] = int(r.get("skipped", 0)) + 1
                    continue
                self._last_fire[r["id"]] = self._now()
                due.append(dict(r))
        for r in due:
            self._queue.put((r, event))
        if due:
            self._ensure_worker()

    def test(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Which rules would fire for ``event`` (no side effects)."""
        with self._lock:
            return [{"id": r["id"], "name": r.get("name"), "enabled": r.get("enabled", True)}
                    for r in self.rules if rule_matches(r, event)]

    # -- running ------------------------------------------------------------
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="hoard-hub-rules", daemon=True)
                self._worker.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rule, event = self._queue.get(timeout=30.0)
            except queue.Empty:
                return
            self.run(rule, event, manual=False)

    def run(self, rule: dict[str, Any], event: dict[str, Any], *, manual: bool = True) -> dict[str, Any]:
        """Run ``rule`` for ``event`` now (the worker calls this; so does
        the "run" button / tool with a synthetic or a past event)."""
        ctx = actions.base_context(event=event, rule={"id": rule.get("id"), "name": rule.get("name")})
        t0 = time.monotonic()
        results = self._run_actions(list(rule.get("then") or []), ctx, "rule:" + str(rule.get("id")))
        ms = int((time.monotonic() - t0) * 1000)
        ok = all(r.get("ok") for r in results)
        summary = {"rule": rule.get("id"), "name": rule.get("name"), "event_id": event.get("id"), "event_type": event.get("type"),
                   "ok": ok, "ms": ms, "ts": self._now(), "manual": manual, "results": actions.compact_results(results)}
        with self._lock:
            for r in self.rules:
                if r.get("id") == rule.get("id"):
                    r["runs"] = int(r.get("runs", 0)) + 1
                    r["last"] = {k: summary[k] for k in ("ok", "ms", "ts", "event_id", "event_type")}
                    if not ok:
                        r["last"]["error"] = "; ".join(str(x.get("error")) for x in results if x.get("error"))[:300]
            self.history.append(summary)
            del self.history[:-HISTORY]
            self._save()
        try:
            self.events.emit("hub.rule.ran", {k: v for k, v in summary.items() if k != "ts"}, source="hub")
        except Exception:  # noqa: BLE001
            pass
        return summary

    def close(self) -> None:
        self._stop.set()
        try:
            self._off()
        except Exception:  # noqa: BLE001
            pass


def example_rules() -> list[dict[str, Any]]:
    """The recommended rules: shown in the UI as templates and installed together by
    :meth:`RuleStore.install_examples` (the "Install the recommended rules" button,
    ``hub_rule_install_defaults``). Each has a stable ``id`` so installing twice adds nothing."""
    return [
        {"id": "rule-transcript-cards", "name": "Transcript → flashcard drafts", "when": {"type": "scribe.transcript.done"},
         "then": [{"kind": "tool", "app": "hypatia", "tool": "cards_suggest",
                   "args": {"session_id": "${event.data.session_id}", "limit": 8}}],
         "note": "Every finished Scribe session becomes Hypatia drafts to accept or discard."},
        {"id": "rule-backup-on-stop", "name": "Backup when an app stops", "when": {"type": "hub.app.stopped"},
         "then": [{"kind": "hub", "tool": "hub_backup_run", "args": {"apps": ["${event.data.app}"]}}],
         "cooldown_s": 60,
         "note": "A consistent copy of that app's data, taken while it is not writing."},
        {"id": "rule-watch-digest", "name": "New watched item → digest note", "when": {"type": "links.watch.new"},
         "then": [{"kind": "event", "type": "digest.item", "data": {"title": "${event.data.title}", "url": "${event.data.url}",
                                                                    "watch": "${event.data.watch}"}}],
         "cooldown_s": 0,
         "note": "Every new feed/release/page-change entry lands as a digest.item event for the daily recap skill."},
        {"id": "rule-restart-down", "name": "Service down → try a restart",
         "when": {"type": "cassandra.incident.opened", "where": {"data.to_state": "down", "data.service_kind": "app"}},
         "then": [{"kind": "start_app", "app": "${event.data.app}"}], "cooldown_s": 300,
         "note": "Cassandra reports a hub-managed app down: the hub starts it again, at most once every five minutes "
                 "(external services such as a model server carry service_kind 'external' and are left alone)."},
    ]
