import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from hoard_link.hub import agora_faustus, agora_lessons, faustus_endpoint_key
from hoard_link.hub.agora import AgoraFacet
from hoard_link.hub.facets import Request
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


def test_studio_apprentice_name_can_be_reopened_but_not_used_in_parallel(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    name = agora_faustus.APPRENTICE
    monkeypatch.setattr(facet, "_taken_names", lambda: {name, "faustus-other"})
    assert facet._worker_name_problem("faustus", name)[0] == 409
    facet.space_id = "estudio"
    assert facet._worker_name_problem("faustus", name) == (0, "")
    with facet._db() as db:
        db.execute("INSERT INTO workers(id,parent_id,agent_id,model,effort,task,state,created_at,updated_at) "
                   "VALUES('worker','faustus',?,'model','','task','starting',1,1)", (name,))
    assert facet._worker_name_problem("faustus", name)[0] == 409
    with facet._db() as db:
        db.execute("UPDATE workers SET state='completed' WHERE id='worker'")
    assert facet._worker_name_problem("faustus", name) == (0, "")
    assert facet._worker_name_problem("faustus", "faustus-other")[0] == 409


def test_tutor_instinct_remembers_own_studio_message_and_returns_undo(tmp_path):
    facet = _facet(tmp_path)
    facet.space_id = "estudio"
    messages = {42: {"author": "tutor", "kind": "comment", "body": "State the view.", "thread_id": 18},
                43: {"author": "tutor", "kind": "comment", "body": "a" * 301, "thread_id": 18}}
    db = SimpleNamespace(one=lambda sql, params: messages.get(params[0]) if "FROM messages m" in sql else None)
    agora = SimpleNamespace(db=db, person_id="person", space_id="estudio")
    agora.for_space = lambda sid: agora
    facet.hub.agora = agora
    call = {"action": "remember", "message_id": 42, "trigger": "When checking a piece"}
    handler = facet.handlers_local()[agora_faustus.TUTOR_TOOL]
    assert handler({**call, "actor": "other"})["status"] == 403
    assert handler({**call, "actor": "tutor", "message_id": 99})["status"] == 404
    assert "300" in handler({**call, "actor": "tutor", "message_id": 43})["error"]
    saved = handler({**call, "actor": "tutor"})
    assert saved["ok"] and saved["agent"] == agora_faustus.APPRENTICE
    assert handler({**call, "actor": "tutor"})["undo"] == saved["undo"]
    lesson, = handler({"action": "list", "actor": "tutor"})["lessons"]
    assert (lesson["action"], lesson["author"], lesson["thread_id"]) == ("State the view.", "tutor", 18)
    assert handler({**saved["undo"]["arguments"], "actor": "other"})["status"] == 403
    assert handler(saved["undo"]["arguments"])["status"] == "undone"
    assert not agora_lessons.list_lessons(facet.hub.config.data_dir)["lessons"]


def test_shared_lesson_routes_withdraw_and_restore_a_legacy_record(tmp_path):
    facet = _facet(tmp_path)
    agora = SimpleNamespace(hub=facet.hub)
    folder = agora_faustus.worker_data(facet, agora_faustus.APPRENTICE) / "instincts" / "agora-tutor"
    folder.mkdir(parents=True)
    lesson = {"author": "tutor", "thread_id": 18, "message_id": 42, "trigger": "When checking",
              "action": "State the view.", "created": 20}
    (folder / "42.json").write_text(json.dumps(lesson), encoding="utf-8")

    def request(operation="", body=None, caller="hub"):
        method = "GET" if body is None else "POST"
        req = Request(method, agora_lessons.ROUTE + operation, {}, body or {}, lambda: caller,
                      lambda: caller == "hub")
        return (AgoraFacet.get if body is None else AgoraFacet.post)(agora, req)

    assert request(caller=None)["status"] == 401
    assert request()["lessons"] == [lesson]
    assert request("/withdraw", {"message_id": 42}, caller=None)["status"] == 401
    withdrawal = request("/withdraw", {"message_id": 42})
    assert withdrawal["undo"] and not request()["lessons"]
    assert request("/withdraw", {"message_id": 42}) == withdrawal
    assert request("/undo", {"token": withdrawal["undo"]})["ok"]
    assert request()["lessons"] == [lesson]
    assert request("/undo", {"token": withdrawal["undo"]})["status"] == 409


def test_resuming_apprentice_reactivates_the_exact_saved_binding(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    binding = {"id": "previous", "actor": agora_faustus.APPRENTICE, "active": False, "revision": 2,
               "space_id": "estudio", "connector_id": "agora", "operations": ["poll"], "thread_ids": [1],
               "resources": [], "repos": []}
    calls = []

    def request(method, path, fields=None, *, base_url=""):
        assert path == "/api/coordination/previous-chat" and base_url == "http://127.0.0.1:58200"
        calls.append((method, fields))
        return 200, {"binding": binding if method == "GET" else {**binding, "active": True}}

    monkeypatch.setattr(facet, "_faustus_session_request", request)
    agora_faustus.reactivate_apprentice_binding(facet, "http://127.0.0.1:58200", "previous-chat")
    assert calls == [("GET", None), ("PUT", {key: value for key, value in binding.items() if key != "revision"}
                                             | {"active": True, "expected_revision": 2})]


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
@pytest.mark.parametrize("key_status", ["open", "matched", "missing", "mismatched", "no-fingerprint"])
def test_pc_workspace_launches_headless_and_binds_own_identity(tmp_path, monkeypatch, connector_id, key_status):
    facet = _facet(tmp_path)
    key = "synthetic-key"
    facet.hub.placement = object()
    key_reads = []

    def read_key(root, python, endpoint_id, base_url, *, placement):
        key_reads.append((endpoint_id, base_url, placement))
        return {"missing": "", "mismatched": "old-key"}.get(key_status, key)

    monkeypatch.setattr(agora_faustus, "read_endpoint_key", read_key)
    facet.hub.agora.space_store = SimpleNamespace(get=lambda sid: {"workspace": str(tmp_path)})
    monkeypatch.setattr(agora_faustus, "service", lambda hub: {
        "remote": True, "reachable": True, "url": "https://spark.example"})
    runtime = {"url": "http://127.0.0.1:58200"}
    starts = []
    monkeypatch.setattr(agora_faustus, "start_pc",
                        lambda *args, **kwargs: starts.append((args, kwargs)) or runtime)
    calls = []

    def request(method, path, fields=None, *, base_url=""):
        calls.append((method, path, fields, base_url))
        if path == "/api/model-endpoints":
            fingerprint = None if key_status == "no-fingerprint" else hashlib.sha256(key.encode()).hexdigest()[:8]
            return 200, [
                {"id": "other", "is_enabled": True, "base_url": "http://model.example/v1", "has_key": True},
                {"id": "sparks", "is_enabled": True, "base_url": "http://model.example/v1",
                 "has_key": key_status != "open", "api_key_fingerprint": fingerprint},
            ]
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
    assert key_reads == ([] if key_status == "open" else [
        ("sparks", "http://model.example/v1", facet.hub.placement)])
    if key_status in ("missing", "mismatched", "no-fingerprint"):
        assert result is None and "Added Models" in error and "Modelos añadidos" in error
        assert key not in error and "old-key" not in error
        assert starts == [] and len(calls) == 1
        return
    assert not error and result["workspace"] == str(tmp_path)
    assert result["url"] == runtime["url"] and result["runtime"] is runtime
    args, kwargs = starts[0]
    assert args[-2:] == ("http://model.example/v1", "deepseek-v4.1-flash")
    assert kwargs == {"api_key": key if key_status == "matched" else ""}
    creation = next(row for row in calls if row[1] == "/api/session")
    assert creation[2]["endpoint_id"] == "headless"
    binding = next(row[2] for row in calls if row[1] == "/api/coordination/child")
    assert binding["actor"] == "faustus-qa" and binding["space_id"] == "ecosystem"
    assert binding["connector_id"] == connector_id
    assert calls[0][3] == "https://spark.example"


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("space", ["ecosystem", "estudio"])
def test_pc_runtime_installs_key_privately_and_preserves_it_on_resume(tmp_path, monkeypatch, resume, space):
    facet = _facet(tmp_path)
    facet.space_id = space
    key = "synthetic-key"
    launches = []
    requests = []
    process = SimpleNamespace(pid=1, poll=lambda: None)

    def popen(command, **kwargs):
        launches.append((command, kwargs["env"]))
        return process

    def request(method, path, fields=None, *, base_url=""):
        assert base_url.startswith("http://127.0.0.1:")
        requests.append((method, path, fields))
        if method == "PATCH":
            return 200, {"id": "headless", "is_enabled": True}
        return 200, [{"id": "agora", "status": "connected"}]

    monkeypatch.setattr(agora_faustus.subprocess, "Popen", popen)
    monkeypatch.setattr("hoard_link.hub.procs.proc_info", lambda pid: SimpleNamespace(created_at=1))
    monkeypatch.setattr("hoard_link.hub.contract._get", lambda *args: (200, {"service": "faustus"}))
    monkeypatch.setattr(facet, "_faustus_session_request", request)
    runtime = agora_faustus.start_pc(facet, "faustus-qa", tmp_path, "python", "http://models.example/v1",
                                     "model", api_key="" if resume else key, resume=resume)
    command, env = launches[0]
    assert env["FAUSTUS_AGORA_WORKER_SPACE"] == space
    assert ("--agora-studio" in command) == (space == "estudio")
    assert command[command.index("--endpoint-url") + 1] == ("" if resume else "http://models.example/v1")
    assert command[command.index("--api-key") + 1] == ""
    assert key not in repr(launches) and key not in repr(runtime)
    assert requests == [("GET", "/api/mcp/servers", None)] + (
        [] if resume else [("PATCH", "/api/model-endpoints/headless", {"api_key": key})])


def test_read_only_store_selects_exact_encrypted_endpoint(tmp_path):
    fernet = pytest.importorskip("cryptography.fernet").Fernet
    key_file = tmp_path / ".app_key"
    key_file.write_bytes(fernet.generate_key())
    box = fernet(key_file.read_bytes())
    database = tmp_path / "app.db"
    url = "http://models.example/v1"
    with sqlite3.connect(database) as store:
        store.execute("CREATE TABLE model_endpoints (id, base_url, is_enabled, api_key)")
        store.executemany("INSERT INTO model_endpoints VALUES (?, ?, ?, ?)", [
            ("other", url, 1, "enc:" + box.encrypt(b"other-key").decode()),
            ("selected", url, 1, "enc:" + box.encrypt(b"synthetic-key").decode()),
            ("legacy", url, 1, "unencrypted-key"),
            ("disabled", url, 0, "enc:" + box.encrypt(b"disabled-key").decode()),
        ])
    before = database.read_bytes()
    assert faustus_endpoint_key._read_key(database, key_file, "selected", url) == "synthetic-key"
    assert faustus_endpoint_key._read_key(database, key_file, "selected", url + "/changed") == ""
    for endpoint in ("missing", "legacy", "disabled"):
        assert faustus_endpoint_key._read_key(database, key_file, endpoint, url) == ""
    missing_key = tmp_path / "absent-key"
    assert faustus_endpoint_key._read_key(database, missing_key, "selected", url) == ""
    assert not missing_key.exists()
    assert database.read_bytes() == before


def test_key_reader_uses_placement_ssh_and_drops_failure_output(tmp_path):
    from hoard_link.hub.placement import PlacementError

    key = "synthetic-key"
    calls = []

    def ssh(command, *, input, timeout):
        calls.append(command)
        assert key not in command and key not in input
        assert "SELECT api_key FROM model_endpoints" in input
        if len(calls) == 1:
            return json.dumps(key)
        raise PlacementError(key)

    placement = SimpleNamespace(_ssh=ssh)
    url = "http://models.example/v1"
    assert faustus_endpoint_key.read_endpoint_key(tmp_path, "python", "selected", url, placement=placement) == key
    assert faustus_endpoint_key.read_endpoint_key(tmp_path, "python", "selected", url, placement=placement) == ""
    assert all(cmd.endswith(" - selected " + url) for cmd in calls)


def test_headless_endpoint_credential_patch_uses_json(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return nullcontext(SimpleNamespace(status=200, read=lambda limit: b'{"id":"headless","is_enabled":true}'))

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: SimpleNamespace(open=open_request))
    result = facet._faustus_session_request("PATCH", "/api/model-endpoints/headless", {"api_key": "synthetic-key"},
                                           base_url="http://127.0.0.1:58200")
    assert result == (200, {"id": "headless", "is_enabled": True})
    assert requests[0].headers["Content-type"] == "application/json"
    assert json.loads(requests[0].data) == {"api_key": "synthetic-key"}


def test_stop_targets_recorded_instance_even_after_placement_changes(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    facet._processes["worker"] = SimpleNamespace(pid=None)
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
    assert row["process_exit_code"] == 1 and "turno detenido" in row["detail"]


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
