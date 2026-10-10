from datetime import datetime, timedelta
from types import SimpleNamespace
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hoard_link.hub.fleet import FleetFacet, classify, deadline


def expectation(**patch):
    return {"id": "qa:watcher", "label": "QA watcher", "kind": "watcher", "owner": "qa", "every": "1m", "created_ts": 100, **patch}


def registration(**patch):
    item = expectation(**patch)
    item.pop("created_ts", None)
    return item


@pytest.mark.parametrize("now,state", [(229, "healthy"), (230, "healthy"), (230.001, "silent")])
def test_strict_two_cadence_boundary(now, state):
    assert classify(expectation(last_success_ts=110), now)["state"] == state


def test_schedule_skips_weekend_and_waits_for_two_real_occurrences():
    friday = datetime(2026, 10, 9, 8, 45).timestamp()
    item = expectation(every=None, at="08:45", days=["weekdays"], last_success_ts=friday)
    tuesday = datetime(2026, 10, 13, 8, 45).timestamp()
    assert deadline(item, friday) == tuesday
    assert classify(item, friday + 2 * 86400 + 3600)["state"] == "healthy"
    assert classify(item, tuesday + .1)["state"] == "silent"


def test_unknown_clocks_pause_and_errors_do_not_turn_attempts_into_successes():
    assert classify(expectation(), 120)["state"] == "unknown"
    assert classify(expectation(), 221)["state"] == "silent"
    assert classify(expectation(last_run_ts=219), 221)["state"] == "silent"
    assert classify(expectation(last_error_ts=120, paused=True), 300)["state"] == "paused"
    assert classify(expectation(last_error_ts=120), 130)["state"] == "error"
    assert classify(expectation(last_success_ts=140, last_error_ts=120), 150)["state"] == "healthy"
    assert classify(expectation(last_success_ts=500), 150)["reason"] == "future_timestamp"
    assert classify(expectation(unavailable=True), 300)["state"] == "unknown"
    assert classify(expectation(last_error_ts=120, cadence_unverified=True), 130)['state'] == 'error'
    assert classify(expectation(last_success_ts=120, partial=True), 130)['reason'] == 'partial_result'
    assert classify(expectation(last_success_ts=120, cadence_unverified=True), 130)['state'] == 'unknown'


def test_windows_lower_bound_does_not_invent_a_finish_time_or_warn_early(tmp_path, monkeypatch):
    import hoard_link.hub.fleet as fleet
    hub = FakeHub(tmp_path)
    hub.config.fleet['windows_tasks'] = ['QA task']
    row = {'path':'\\', 'name':'QA task', 'state':'Ready', 'result':0,
           'last_start':'1970-01-01T00:00:10+00:00',
           'triggers':[{'kind':'MSFT_TaskTimeTrigger','interval':'PT1M'}]}
    monkeypatch.setattr(fleet, 'read_tasks', lambda _: {'ok':True,'items':[row]})
    now = [150]
    facet = FleetFacet(hub, clock=lambda:now[0], background=False)
    first = facet.status()['items'][0]
    assert first['last_success_ts'] == 10 and first['success_observed_ts'] == 150
    assert first['state'] == 'healthy' and first['silent_after_ts'] == 270
    now[0] = 269
    restarted = FleetFacet(hub, clock=lambda:now[0], background=False)
    assert restarted.status()['items'][0]['success_observed_ts'] == 150
    now[0] = 270.1
    assert restarted.status(refresh=True)['items'][0]['state'] == 'silent'


def test_fleet_read_requires_existing_family_authority_before_observing(tmp_path):
    from hoard_link.hub.facets import Request
    facet = FleetFacet(FakeHub(tmp_path), background=False)
    request = Request('GET', '/api/fleet', {}, {}, caller=lambda: None, agent=lambda: False)
    assert facet.get(request).status == 401
    assert not facet.data['observations'] and not facet.path.exists()
    request.caller = lambda: 'hub'
    assert facet.get(request)['ok']


class FakeHub:
    def __init__(self, tmp_path):
        self.config = SimpleNamespace(data_dir=str(tmp_path), jobs_enabled=True, language="es", fleet={"autostart": False})
        self.started_at = 100
        self.rows = []
        self.jobs = SimpleNamespace(list=lambda: self.rows)
        self.rules = SimpleNamespace(list=lambda: [])
        self.notices, self.emitted = [], []
        self.events = SimpleNamespace(emit=lambda *args, **kwargs: self.emitted.append((args, kwargs)))
        self.notifier = SimpleNamespace(send=lambda *args, **kwargs: self.notices.append((args, kwargs)))

    def get(self, app_id): return SimpleNamespace(id=app_id, name=app_id)
    def profiles(self): return {}
    def automation_blocked(self, app_id): return False
    def facet(self, name): return self.notifier if name == "notify" else None


def test_reports_transitions_restart_and_reads_do_not_send_or_consume_notifications(tmp_path):
    hub = FakeHub(tmp_path)
    clock = [100]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    assert facet.register(registration()) ["ok"]
    assert facet.report({"id": "qa:watcher", "status": "ok", "ts": 100}, caller="qa")["ok"]
    facet.reconcile()
    clock[0] = 221
    assert facet.status(refresh=True)["counts"]["silent"] == 1
    assert not hub.notices
    facet.reconcile()
    assert len(hub.notices) == 1
    facet.reconcile()
    assert len(hub.notices) == 1
    restarted = FleetFacet(hub, clock=lambda: clock[0], background=False)
    restarted.reconcile()
    assert len(hub.notices) == 1
    assert restarted.report({"id": "qa:watcher", "status": "waiting", "user_reason": "login"}, caller="qa")["ok"]
    result = restarted.reconcile()
    assert result["brief"]["waiting_for_you"][0]["user_reason"] == "login"
    assert not result["brief"]["blocked"]
    assert len(hub.notices) == 2
    restarted.reconcile()
    assert len(hub.notices) == 2
    restarted.report({"id": "qa:watcher", "status": "ok"}, caller="qa")
    restarted.reconcile()
    clock[0] = 342
    restarted.reconcile()
    assert len(hub.notices) == 3
    assert hub.notices[0][1]["dedupe_key"] != hub.notices[2][1]["dedupe_key"]


def test_receipt_owner_stale_order_same_timestamp_and_no_secret_bodies(tmp_path):
    hub = FakeHub(tmp_path)
    facet = FleetFacet(hub, clock=lambda: 150, background=False)
    facet.register(registration())
    assert facet.report({"id": "qa:watcher", "status": "ok"}, caller="other")["status"] == 403
    facet.report({"id": "qa:watcher", "status": "ok", "ts": 130}, caller="qa")
    facet.report({"id": "qa:watcher", "status": "error", "ts": 130}, caller="qa")
    assert facet.status(refresh=True)["items"][0]["state"] == "error"
    facet.report({"id": "qa:watcher", "status": "ok", "ts": 130}, caller="qa")
    assert facet.status(refresh=True)["items"][0]["state"] == "healthy"
    assert facet.report({"id": "qa:watcher", "status": "error", "ts": 120})["ignored"] == "older_receipt"
    assert facet.report({"id": "qa:watcher", "status": "error", "ts": 200})["status"] == 400
    assert facet.report({"id": "qa:watcher", "status": "error", "password": "QA private marker"})["status"] == 400
    assert "QA private marker" not in facet.path.read_text()


def test_native_jobs_use_completed_result_and_preserve_previous_success(tmp_path):
    hub = FakeHub(tmp_path)
    clock = [150]
    hub.rows = [{"id": "qa", "name": "QA", "every": "1m", "created_ts": 100, "last_run_ts": 145, "last": {"ok": True, "ts": 130}}]
    facet = FleetFacet(hub, clock=lambda: clock[0], background=False)
    assert facet.status()["items"][0]["last_success_ts"] == 130
    hub.rows[0]["last"] = {"ok": False, "ts": 140}
    row = facet.status(refresh=True)["items"][0]
    assert row["last_success_ts"] == 130 and row["state"] == "error"
    hub.rows[0]["enabled"] = False
    assert facet.status(refresh=True)["items"][0]["state"] == "paused"
    assert facet.register(registration(id="job:qa"))["status"] == 400


def test_source_failure_retains_other_sources_without_claiming_all_healthy(tmp_path):
    hub = FakeHub(tmp_path)
    facet = FleetFacet(hub, clock=lambda: 150, background=False)
    facet.register(registration())
    facet.report({"id": "qa:watcher", "status": "ok"})
    def fail(): raise OSError("QA source unavailable")
    hub.jobs.list = fail
    result = facet.status()
    assert result["items"][0]["state"] == "healthy"
    assert result["coverage"]["errors"] == {"jobs": "source_unavailable"}


def test_corrupt_journal_is_preserved(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text('{"schema":999,"private_marker":"keep"}')
    with pytest.raises(ValueError): FleetFacet(FakeHub(tmp_path), background=False)
    assert path.read_text() == '{"schema":999,"private_marker":"keep"}'


def test_published_metadata_token_filtering_cached_failure_and_manual_pause(tmp_path):
    hub = FakeHub(tmp_path)
    token = tmp_path / "app-token"; token.write_text("qa-fleet-token")
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "owner": "qa", "private_extra": "SECRET QA", "items": [
                expectation(id="qa:published", last_success_ts=130, last_status="ok", private_extra="SECRET QA")]}).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    app = SimpleNamespace(id="qa", name="QA app", family={"fleet": True}, url=f"http://127.0.0.1:{server.server_port}", token_file=str(token))
    hub.apps = [app]; hub.get = lambda app_id: app
    facet = FleetFacet(hub, clock=lambda: 150, health_fn=lambda app: SimpleNamespace(state="healthy"), background=False)
    try:
        result = facet.status()
        assert calls == [("/api/family/fleet", "Bearer qa-fleet-token")]
        assert result["counts"]["healthy"] == 2
        assert "SECRET QA" not in str(result) and "SECRET QA" not in facet.path.read_text()
    finally: server.shutdown(); server.server_close(); thread.join(timeout=2)
    missing = facet.status(refresh=True)
    assert missing["coverage"]["errors"]["provider:qa"] == "source_unavailable"
    row = next(r for r in missing["items"] if r["id"] == "qa:published")
    assert row["state"] == "unknown" and row["last_success_ts"] == 130
    hub.automation_blocked = lambda app_id: True
    paused = facet.status(refresh=True)
    assert paused["counts"]["paused"] == 2
    assert not paused["brief"]["blocked"] and not paused["brief"]["waiting_for_you"]
    assert not hub.notices
