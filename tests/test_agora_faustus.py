from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from hoard_link.hub import agora_faustus
from tests.test_agora_console import _facet


def test_service_follows_placement_without_probing_localhost(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    facet.hub.placement = SimpleNamespace(state=lambda: {
        "location": "sparks", "urls": {"faustus": "https://spark.example/"}})
    probes = []
    monkeypatch.setattr("hoard_link.hub.contract._get", lambda url, token, timeout: (
        probes.append(url) or (200, {"service": "faustus"})))
    facet.hub.faustus_status = lambda: pytest.fail("local service must not be probed")
    result = agora_faustus.service(facet.hub)
    assert result["reachable"] and result["remote"]
    assert result["url"] == "https://spark.example"
    assert probes == ["https://spark.example/api/health"]


@pytest.mark.parametrize("state", [
    {"location": "sparks", "job": {"running": True}},
    {"location": "sparks", "urls": {}},
])
def test_missing_or_moving_placement_does_not_use_stale_pc_service(tmp_path, state):
    facet = _facet(tmp_path)
    facet.hub.placement = SimpleNamespace(state=lambda: state)
    assert agora_faustus.service(facet.hub)["reachable"] is False


def test_pc_service_keeps_existing_health_contract(tmp_path):
    facet = _facet(tmp_path)
    assert agora_faustus.service(facet.hub) == facet.hub.faustus_status()


def test_placed_session_request_uses_spark_address(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    monkeypatch.setattr(agora_faustus, "service", lambda hub: {
        "remote": True, "reachable": True, "url": "https://spark.example", "body": {"service": "faustus"}})
    requests = []
    monkeypatch.setattr("hoard_link.hub.contract._get", lambda url, token, timeout: (
        requests.append(url) or (200, {"id": "session"})))
    assert facet._faustus_session_request("GET", "/api/projects")[0] == 200
    assert requests == ["https://spark.example/api/projects"]


def test_stop_request_sends_native_run_identity_and_work_scope(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        return nullcontext(SimpleNamespace(status=200, read=lambda limit: b'{"stopped":true}'))
    monkeypatch.setattr("urllib.request.build_opener", lambda *args:
                        SimpleNamespace(open=open_request))
    result = facet._faustus_session_request("POST", "/api/chat/stop/child", {"scope": "work"},
                                           base_url="http://127.0.0.1:58200", expected_run_id="run-1")
    assert result == (200, {"stopped": True})
    assert requests[0].headers["X-odysseus-run-id"] == "run-1"
    assert requests[0].headers["Content-type"] == "application/json"
    assert requests[0].data == b'{"scope": "work"}'


@pytest.mark.parametrize("connector_id", ["agora", "existing-hub-connector"])
def test_pc_workspace_launches_headless_and_binds_own_identity(tmp_path, monkeypatch, connector_id):
    facet = _facet(tmp_path)
    facet.hub.agora.space_store = SimpleNamespace(get=lambda sid: {"workspace": str(tmp_path)})
    monkeypatch.setattr(agora_faustus, "service", lambda hub: {
        "remote": True, "reachable": True, "url": "https://spark.example"})
    runtime = {"url": "http://127.0.0.1:58200"}
    starts = []
    monkeypatch.setattr(agora_faustus, "start_pc", lambda *args: starts.append(args) or runtime)
    calls = []

    def request(method, path, fields=None, *, base_url=""):
        calls.append((method, path, fields, base_url))
        if path == "/api/model-endpoints":
            return 200, [{"id": "sparks", "is_enabled": True, "base_url": "http://model.example/v1"}]
        assert base_url == runtime["url"]
        if path == "/api/projects":
            return (200, []) if method == "GET" else (200, {"id": "project"})
        if path == "/api/mcp/servers":
            return 200, [{"id": connector_id, "name": "Hoard Hub", "status": "connected",
                          "args": ["-m", "hoard_link.hub.mcp"]}]
        if path == "/api/session":
            return 200, {"id": "child", "model": "deepseek-v4.1-flash"}
        if path == "/api/session/child":
            return 200, {"reasoning_effort": "high"}
        if path == "/api/coordination/child":
            return 200, {"binding": {"actor": fields["actor"], "project_id": "project"}}
        pytest.fail(path)

    monkeypatch.setattr(facet, "_faustus_session_request", request)
    result, error = agora_faustus.prepare(facet, "faustus-qa", {
        "endpoint_id": "sparks", "model_id": "deepseek-v4.1-flash"}, "high")
    assert not error and result["workspace"] == str(tmp_path)
    assert result["url"] == runtime["url"] and result["runtime"] is runtime
    assert starts[0][-2:] == ("http://model.example/v1", "deepseek-v4.1-flash")
    creation = next(row for row in calls if row[1] == "/api/session")
    assert creation[2]["endpoint_id"] == "headless"
    binding = next(row[2] for row in calls if row[1] == "/api/coordination/child")
    assert binding["actor"] == "faustus-qa" and binding["space_id"] == "ecosystem"
    assert binding["connector_id"] == connector_id
    assert calls[0][3] == "https://spark.example"


def test_stop_targets_recorded_instance_even_after_placement_changes(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    facet._processes["worker"] = SimpleNamespace()
    with facet._db() as db:
        db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,session_id,faustus_url,created_at,updated_at) "
                   "VALUES('worker','faustus','model','','task','running','child','http://127.0.0.1:58200',1,1)")
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return (200, {"runs": {"child": "run-1"}}) if args[0] == "GET" else (200, {"stopped": True})
    monkeypatch.setattr(facet, "_faustus_session_request", request)
    result = facet.worker({"action": "stop", "worker_id": "worker"})
    assert result["ok"] and result["state"] == "stopping"
    assert calls == [(("GET", "/api/chat/activity"), {"base_url": "http://127.0.0.1:58200"}),
                     (("POST", "/api/chat/stop/child", {"scope": "work"}),
                      {"base_url": "http://127.0.0.1:58200", "expected_run_id": "run-1"})]
    assert facet._worker_rows()[0]["state"] == "stopping"


def test_recovered_worker_stop_settles_without_a_runner_monitor(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    with facet._db() as db:
        db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,session_id,faustus_url,created_at,updated_at) "
                   "VALUES('worker','faustus','model','','task','unknown','child','http://127.0.0.1:58200',1,1)")
    monkeypatch.setattr(facet, "_faustus_session_request", lambda *args, **kwargs:
                        (200, {"runs": {"child": "run-1"}}) if args[0] == "GET" else (200, {"stopped": True}))
    assert facet.worker({"action": "stop", "worker_id": "worker"})["state"] == "stopped"


def test_running_receipt_reports_real_tool_progress(tmp_path):
    facet = _facet(tmp_path)
    log = tmp_path / "worker.jsonl"
    log.write_text('{"type":"tool_output","tool":"agora_write","exit_code":0}\n', encoding="utf-8")
    with facet._db() as db:
        db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,log_path,created_at,updated_at) "
                   "VALUES('worker','faustus','model','','task','running',?,1,1)", (str(log),))
    row = facet._worker_rows()[0]
    assert row["tool_success_count"] == 1 and row["tool_failure_count"] == 0
    assert row["updated_at"] == log.stat().st_mtime


def test_cancelled_runner_reports_stopped_outcome(tmp_path):
    facet = _facet(tmp_path)
    log = tmp_path / "worker.jsonl"
    log.write_text('{"type":"run_summary","session":"child","stop":"cancelled","text":""}\n', encoding="utf-8")
    with facet._db() as db:
        db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,log_path,created_at,updated_at) "
                   "VALUES('worker','faustus','model','','task','stopping',?,1,1)", (str(log),))
    facet._monitor_worker("worker", SimpleNamespace(wait=lambda: 1), tmp_path / "result.txt")
    row = facet._worker_rows()[0]
    assert row["state"] == "stopped" and row["outcome"] == "cancelled"
    assert row["process_exit_code"] == 1 and "confirmed cancellation" in row["detail"]


def test_restart_during_cancellation_reports_unknown(tmp_path):
    facet = _facet(tmp_path)
    with facet._db() as db:
        db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,created_at,updated_at) "
                   "VALUES('worker','faustus','model','','task','stopping',1,1)")
    facet._reconcile_workers()
    assert facet._worker_rows()[0]["state"] == "unknown"


def test_unconfirmed_stop_does_not_claim_cancellation(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    with facet._db() as db:
        db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,session_id,faustus_url,created_at,updated_at) "
                   "VALUES('worker','faustus','model','','task','running','child','http://127.0.0.1:58200',1,1)")
    monkeypatch.setattr(facet, "_faustus_session_request", lambda *args, **kwargs:
                        (200, {"runs": {"child": "run-1"}}) if args[0] == "GET" else (200, {"stopped": False}))
    assert facet.worker({"action": "stop", "worker_id": "worker"})["ok"] is False
    assert facet._worker_rows()[0]["state"] == "running"
