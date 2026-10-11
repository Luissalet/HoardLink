"""Fleet: what is NOT a silent stop (resumes, one failed probe), who pushes (once per burst, never a storm)
and that a request never waits for a reconcile."""
from types import SimpleNamespace
import threading
import time

import pytest

from hoard_link.hub.fleet import FleetFacet, brief, classify, deadline


class Hub:
    def __init__(self, tmp_path, apps=(), language="es"):
        self.config = SimpleNamespace(data_dir=str(tmp_path), jobs_enabled=True, language=language,
                                      fleet={"autostart": False, "expected_apps": list(apps)})
        self.started_at = 0
        self.rows, self.rule_rows, self.cassandra = [], [], []
        self.jobs = SimpleNamespace(list=lambda: self.rows)
        self.rules = SimpleNamespace(list=lambda: self.rule_rows)
        self.notices, self.emitted = [], []
        self.events = SimpleNamespace(emit=lambda *a, **k: self.emitted.append((a, k)),
                                      query=lambda **k: list(self.cassandra))
        self.notifier = SimpleNamespace(send=lambda *a, **k: self.notices.append((a, k)))

    def get(self, app_id): return SimpleNamespace(id=app_id, name=app_id.title())
    def profiles(self): return {}
    def automation_blocked(self, app_id): return False
    def facet(self, name): return self.notifier if name == "notify" else None


def job(job_id="qa", **patch):
    return {"id": job_id, "name": "QA " + job_id, "every": "1m", "created_ts": 100, "last": {"ok": True, "ts": 110}, **patch}


def item(**patch):
    return {"id": "qa:x", "label": "X", "kind": "watcher", "owner": "qa", "every": "1m", "created_ts": 100, **patch}


# ---------------------------------------------------------------- P2-3a: resuming restarts the clock
def test_resume_baseline_is_the_latest_of_success_creation_and_resume():
    task = item(last_success_ts=110, resumed_ts=100000)
    assert classify(task, 100000 + 119)["state"] == "healthy"
    assert classify(task, 100000 + 121)["state"] == "silent"
    assert classify({**task, "resumed_ts": None}, 100000)["state"] == "silent"   # without the resume it would already be silent


def test_slots_computed_before_the_resume_are_not_a_deadline():
    task = item(every=None, expected_slots=[200, 300], last_success_ts=110, resumed_ts=1000)
    assert deadline(task, 1000) is None
    assert classify(task, 1005)["state"] == "healthy"
    # slots after the resume still count
    assert classify({**task, "expected_slots": [1100, 1200]}, 1250)["state"] == "silent"


def test_hub_job_switched_back_on_after_a_long_pause_is_not_silent(tmp_path):
    hub, clock = Hub(tmp_path), [200]
    hub.rows = [job(enabled=False)]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    assert facet.status(refresh=True)["items"][0]["state"] == "paused"
    clock[0] = 100000
    hub.rows[0]["enabled"] = True
    row = facet.status(refresh=True)["items"][0]
    assert row["state"] == "healthy" and row["resumed_ts"] == 100000
    clock[0] = 100000 + 121
    assert facet.status(refresh=True)["items"][0]["state"] == "silent"


def test_job_update_stamps_the_resume(tmp_path):
    from hoard_link.hub.jobs import Scheduler
    store = Scheduler(str(tmp_path / "jobs.json"), runner=lambda *a: [], now=lambda: 5000.0)
    added = store.add({"name": "QA", "every": "1h", "then": [{"kind": "event", "type": "qa.ping"}], "enabled": False})
    assert added["ok"], added
    assert "resumed_ts" not in added["job"]
    store.update(added["job"]["id"], {"name": "QA again"})
    assert "resumed_ts" not in store.get(added["job"]["id"])
    store.update(added["job"]["id"], {"enabled": True})
    assert store.get(added["job"]["id"])["resumed_ts"] == 5000.0


def test_registered_producer_reporting_again_after_a_pause_restarts_the_clock(tmp_path):
    hub, clock = Hub(tmp_path), [100]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    facet.register({"id": "qa:w", "owner": "qa", "kind": "watcher", "every": "1m"})
    facet.report({"id": "qa:w", "status": "paused", "ts": 101})
    clock[0] = 50000
    facet.report({"id": "qa:w", "status": "ok", "ts": 50000})
    clock[0] = 50000 + 119
    assert facet.status(refresh=True)["items"][0]["state"] == "healthy"
    # the registration itself switched on again
    facet.register({"id": "qa:w", "owner": "qa", "kind": "watcher", "every": "1m", "enabled": False})
    clock[0] = 90000
    facet.register({"id": "qa:w", "owner": "qa", "kind": "watcher", "every": "1m", "enabled": True})
    assert facet.status(refresh=True)["items"][0]["state"] == "healthy"


# ---------------------------------------------------------------- P2-3d: one failed probe is not an outage
def test_app_down_needs_two_failed_probes_over_a_minute(tmp_path):
    hub, clock, health = Hub(tmp_path, apps=["qa"]), [1000], ["healthy"]
    facet = FleetFacet(hub, clock=lambda: clock[0], health_fn=lambda app: SimpleNamespace(state=health[0]), background=False)
    def state(at, h):
        clock[0], health[0] = at, h
        row = facet.reconcile()["items"][0]
        return row["state"], row["reason"]
    assert state(1000, "healthy")[0] == "healthy"
    assert state(1010, "down") == ("unknown", "app_down_unconfirmed")      # a single 1.5 s probe failure
    assert state(1040, "down")[0] == "unknown"                            # second failure, but not a minute yet
    assert state(1069, "down")[0] == "unknown"
    assert state(1071, "down") == ("silent", "app_down")
    assert state(1100, "healthy")[0] == "healthy"
    assert state(1110, "down")[0] == "unknown"                            # the counter restarted
    assert not hub.notices


def test_app_outage_is_pushed_once_after_it_is_sustained(tmp_path):
    hub, clock, health = Hub(tmp_path, apps=["qa"]), [0], ["healthy"]
    facet = FleetFacet(hub, clock=lambda: clock[0], health_fn=lambda app: SimpleNamespace(state=health[0]), background=False)
    for at, h in [(0, "healthy"), (60, "down"), (125, "down"), (150, "down"), (186, "down"), (250, "down"), (310, "down")]:
        clock[0], health[0] = at, h
        facet.reconcile()
    assert len(hub.notices) == 1
    assert hub.notices[0][1]["dedupe_key"].startswith("fleet:app:qa:")


def test_flapping_app_is_one_push_for_the_whole_burst(tmp_path):
    hub, clock, health = Hub(tmp_path, apps=["qa"]), [0], ["healthy"]
    facet = FleetFacet(hub, clock=lambda: clock[0], health_fn=lambda app: SimpleNamespace(state=health[0]), background=False)
    timeline = [(0, "healthy"), (60, "down"), (125, "down"), (190, "down"),      # outage 1 -> its push
                (250, "healthy"), (310, "down"), (375, "down"), (440, "down"),   # outage 2, 4 minutes later
                (500, "healthy"), (560, "down"), (625, "down"), (690, "down")]   # outage 3
    for at, h in timeline:
        clock[0], health[0] = at, h
        facet.reconcile()
    assert len(hub.notices) == 1
    clock[0], health[0] = 190 + 1801, "healthy"
    facet.reconcile()
    for at in (2060, 2125, 2190, 2250):
        clock[0], health[0] = at, "down"
        facet.reconcile()
    assert len(hub.notices) == 2   # a new burst, half an hour later, is news again


def test_cassandra_incident_covers_the_app_so_fleet_does_not_push(tmp_path):
    hub, clock, health = Hub(tmp_path, apps=["qa"]), [0], ["healthy"]
    hub.cassandra = [{"type": "cassandra.incident.opened", "data": {"app": "qa", "burst_id": "b1"}}]
    facet = FleetFacet(hub, clock=lambda: clock[0], health_fn=lambda app: SimpleNamespace(state=health[0]), background=False)
    for at, h in [(0, "healthy"), (60, "down"), (125, "down"), (190, "down"), (250, "down")]:
        clock[0], health[0] = at, h
        result = facet.reconcile()
    assert result["items"][0]["state"] == "silent"          # still shown in Home and the brief
    assert result["brief"]["blocked"][0]["id"] == "app:qa"
    assert not hub.notices
    hub.cassandra = [{"type": "cassandra.incident.opened", "data": {"app": "other"}}]   # an incident of another app is no cover
    facet.data["observations"]["app:qa"].pop("last_push_ts", None)
    clock[0], health[0] = 300, "healthy"; facet.reconcile()
    for at in (360, 425, 490, 550):
        clock[0], health[0] = at, "down"
        facet.reconcile()
    assert len(hub.notices) == 1


# ---------------------------------------------------------------- P2-6: no storms
def test_first_observation_is_a_baseline_not_a_push(tmp_path):
    hub = Hub(tmp_path)
    hub.rows = [job(f"old{i}", last={"ok": True, "ts": 110}) for i in range(10)]
    facet = FleetFacet(hub, clock=lambda: 100000, background=False)
    result = facet.reconcile()
    assert result["counts"]["silent"] == 10
    assert not hub.notices and not hub.emitted
    facet.reconcile()
    assert not hub.notices


def test_waiting_receipt_seen_for_the_first_time_still_pushes_when_fresh(tmp_path):
    hub, clock = Hub(tmp_path), [1000]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    facet.register({"id": "qa:w", "owner": "qa", "kind": "watcher", "every": "1h"})
    facet.report({"id": "qa:w", "status": "waiting", "user_reason": "login", "ts": 1000})
    facet.reconcile()
    assert len(hub.notices) == 1
    (tmp_path / "b").mkdir()
    stale = Hub(tmp_path / "b")
    other = FleetFacet(stale, clock=lambda: clock[0], background=False)
    other.register({"id": "qa:w", "owner": "qa", "kind": "watcher", "every": "1h"})
    other.report({"id": "qa:w", "status": "waiting", "ts": 1000})
    clock[0] = 1000 + 3600
    other.reconcile()
    assert not stale.notices


def test_many_transitions_in_one_tick_are_one_summary(tmp_path):
    hub, clock = Hub(tmp_path), [200]
    hub.rows = [job(f"j{i}", last={"ok": True, "ts": 190}) for i in range(5)]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    facet.reconcile()                      # baseline: all healthy
    clock[0] = 200 + 200                   # five jobs go silent in the same tick
    facet.reconcile()
    assert len(hub.notices) == 1
    (title, body), kwargs = hub.notices[0][0][:2], hub.notices[0][1]
    assert title == "Flota: 5 cosas necesitan tu atención"
    assert body.splitlines() == [f"QA j{i}" for i in range(5)]
    assert kwargs["dedupe_key"].startswith("fleet:summary:")
    assert len(hub.emitted) == 5           # the bus still gets every change


def test_a_few_transitions_stay_individual_and_english_wording(tmp_path):
    hub, clock = Hub(tmp_path, language="en"), [200]
    hub.rows = [job(f"j{i}", last={"ok": True, "ts": 190}) for i in range(3)]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    facet.reconcile()
    clock[0] = 400
    facet.reconcile()
    assert [n[0][0] for n in hub.notices] == ["Fleet: needs your attention"] * 3
    (tmp_path / "e").mkdir()
    big = Hub(tmp_path / "e", language="en")
    big.rows = [job(f"k{i}", last={"ok": True, "ts": 190}) for i in range(12)]
    bf = FleetFacet(big, clock=lambda: clock[0], background=False)
    clock[0] = 200; bf.reconcile(); clock[0] = 400; bf.reconcile()
    assert big.notices[0][0][0] == "Fleet: 12 things need your attention"
    assert big.notices[0][0][1].splitlines()[-1] == "and 4 more"


# ---------------------------------------------------------------- P3-1: a label that is only a secret
def test_label_that_masks_to_nothing_falls_back_to_the_id(tmp_path):
    hub, clock = Hub(tmp_path), [200]
    hub.rows = [job("abc123", name="password=hunter2", last={"ok": True, "ts": 190})]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    assert facet.reconcile()["items"][0]["label"] == "Tarea del Hub abc123"
    clock[0] = 400
    facet.reconcile()
    assert hub.notices[0][0][:2] == ("Flota: necesita tu atención", "Tarea del Hub abc123")
    assert "hunter2" not in str(hub.notices)


# ---------------------------------------------------------------- P3-2: no cadence, no verdict
def test_rules_without_cadence_leave_the_brief_but_not_the_view(tmp_path):
    hub = Hub(tmp_path)
    hub.rule_rows = [{"id": "r-ok", "name": "Rule ok", "created_ts": 5, "last": {"ok": True, "ts": 50}},
                     {"id": "r-never", "name": "Rule never", "created_ts": 5},
                     {"id": "r-bad", "name": "Rule bad", "created_ts": 5, "last": {"ok": False, "ts": 50}},
                     {"id": "r-off", "name": "Rule off", "created_ts": 5, "enabled": False}]
    result = FleetFacet(hub, clock=lambda: 100, background=False).reconcile()
    names = lambda key: sorted(i["label"] for i in result["brief"][key])
    assert names("no_cadence") == ["Rule never", "Rule ok"]
    assert "Rule ok" not in names("today") and "Rule never" not in names("unknown")
    assert names("blocked") == ["Rule bad"] and names("paused") == ["Rule off"]
    assert len(result["items"]) == 4 and result["counts"]["healthy"] == 1


def test_declared_cadence_rule_stays_in_the_brief(tmp_path):
    hub = Hub(tmp_path)
    hub.config.fleet["rule_cadences"] = {"r1": {"every": "2h"}}
    hub.rule_rows = [{"id": "r1", "name": "Rule", "created_ts": 5, "last": {"ok": True, "ts": 50}}]
    result = FleetFacet(hub, clock=lambda: 100, background=False).reconcile()
    assert [i["id"] for i in result["brief"]["today"]] == ["rule:r1"] and not result["brief"]["no_cadence"]
    assert brief([])["no_cadence"] == []


# ---------------------------------------------------------------- P2-5: a request never reconciles
class Gate:
    """A health probe that blocks until released: proves who waits for it."""
    def __init__(self): self.event, self.calls = threading.Event(), 0
    def __call__(self, app):
        self.calls += 1
        self.event.wait(10)
        return SimpleNamespace(state="healthy")


def wait_for(predicate, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if predicate(): return True
        time.sleep(.01)
    return False


def test_requests_serve_the_cache_while_a_slow_reconcile_runs_in_the_background(tmp_path):
    hub, gate = Hub(tmp_path, apps=["qa"]), Gate()
    facet = FleetFacet(hub, health_fn=gate, background=True)   # SWR on, but no loop started: refreshes are one-shots
    started = time.time()
    cold = facet.status(wait=0)
    assert time.time() - started < .5 and cold["refreshing"] and cold["items"] == [] and cold["coverage"]["errors"] == {"fleet": "warming_up"}
    gate.event.set()
    assert wait_for(lambda: facet.version >= 1)
    gate.event.clear()
    warm = facet.status(wait=0)
    assert warm["items"][0]["id"] == "app:qa" and not warm["refreshing"]
    # a receipt marks the view dirty and refreshes in the background: the request still answers at once
    assert facet.register({"id": "qa:w", "owner": "qa", "kind": "watcher", "every": "1h"})["ok"]
    started = time.time()
    dirty = facet.status()                                      # may wait ~1 s at most, the probe is blocked
    assert time.time() - started < 2.5 and dirty["refreshing"]
    assert all(i["id"] != "qa:w" for i in dirty["items"])
    gate.event.set()
    assert wait_for(lambda: facet.version >= 2)
    after = facet.status(wait=0)
    assert {i["id"] for i in after["items"]} == {"app:qa", "qa:w"}
    facet.close()


def test_refresh_flag_returns_the_cache_with_refreshing_true_instead_of_blocking(tmp_path):
    hub, gate = Hub(tmp_path, apps=["qa"]), Gate()
    gate.event.set()
    facet = FleetFacet(hub, health_fn=gate, background=True)
    facet.status(wait=2)
    assert facet.version >= 1
    gate.event.clear()
    started = time.time()
    forced = facet.status(refresh=True)
    assert time.time() - started < 2.5 and forced["refreshing"] and forced["items"]
    gate.event.set()
    facet.close()


def test_today_never_reconciles_inline(tmp_path):
    from hoard_link.hub.today import TodayFacet
    hub, gate = Hub(tmp_path, apps=["qa"]), Gate()
    facet = FleetFacet(hub, health_fn=gate, background=True)
    started = time.time()
    got = facet.status(wait=0)
    assert time.time() - started < .5 and got["refreshing"]
    gate.event.set()
    facet.close()
    assert TodayFacet._fleet_count({"items": [1, 2, 3], "brief": {"today": [1, 2], "waiting_for_you": [1], "blocked": [1, 2],
                                                                  "unknown": [], "paused": [1], "no_cadence": [1, 2, 3]}}) == 4
    assert TodayFacet._fleet_count(None) == 0 and TodayFacet._fleet_count({"ok": True}) == 0


def test_without_a_background_the_old_inline_semantics_remain(tmp_path):
    hub = Hub(tmp_path)
    hub.rows = [job()]
    facet = FleetFacet(hub, clock=lambda: 150, background=False)
    assert facet.status()["items"][0]["id"] == "job:qa" and "refreshing" not in facet.status(refresh=True)
