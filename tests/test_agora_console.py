from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

from hoard_link.hub.agora_console import AgoraConsoleFacet, THREAD_ID
from hoard_link.hub.facets import FACET_MODULES, Request
from hoard_link.hub.tools import call


class FakeAgora:
    def __init__(self):
        self.posts = []

    def agents(self):
        return {"ok": True, "agents": [{"id": x, "state": "idle"} for x in
                                         ("codex-investiga", "claude", "cursor")]}

    def post(self, args):
        self.posts.append(args)
        return {"ok": True, "message_id": 902, "thread": {"id": THREAD_ID}}

    def thread_recent(self, args):
        assert args == {"thread_id": THREAD_ID, "limit": 40}
        return {"ok": True, "thread": {"id": THREAD_ID}, "messages": []}

    def usage(self):
        return {"ok": True, "providers": [{"provider_id": "codex", "pools": [{"pool_id": "team"}]}]}


class FakeHub:
    def __init__(self, tmp_path: Path):
        self.config = SimpleNamespace(data_dir=str(tmp_path), token_file=str(tmp_path / "mcp-token"),
                                      url="http://127.0.0.1:62841",
                                      faustus_urls=["http://127.0.0.1:7000"])
        self.agora = FakeAgora()
        self.faustus_runtime = SimpleNamespace(installation=lambda: (tmp_path, "python-test"))

    def facet(self, facet_id):
        return SimpleNamespace(agora=self.agora) if facet_id == "agora" else None

    def get(self, app_id):
        return None

    def faustus_status(self):
        return {"reachable": True, "url": "http://127.0.0.1:7000",
                "body": {"service": "faustus"}}


def _request(method, path, body=None, query=None, caller="ui", agent=False):
    return Request(method=method, path=path, query=query or {}, body=body or {},
                   caller=lambda: caller, agent=lambda: agent)


def _facet(tmp_path):
    facet = AgoraConsoleFacet(FakeHub(tmp_path))
    facet._provider_cache = {
        "codex": {"state": "available", "detail": "real catalog", "catalog": [
            {"id": "codex-model-from-cli", "name": "Current CLI model", "efforts": ["low", "high"], "default_effort": "low"}]},
        "claude": {"state": "unverified", "detail": "no catalog", "catalog": []},
        "cursor": {"state": "unavailable", "detail": "no runner", "catalog": []},
        "faustus": {"state": "available", "detail": "health only", "catalog": []},
    }
    facet._provider_at = 10**9
    return facet


def test_capacity_reporter_starts_with_this_hub_configuration(tmp_path, monkeypatch):
    calls = []
    stopped = []
    monkeypatch.setattr("hoard_link.hub.agora_capacity.start_codex_capacity_reporter",
                        lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr("hoard_link.hub.agora_capacity.stop_codex_capacity_reporter",
                        lambda: stopped.append(True))
    monkeypatch.setenv("HOARD_HUB_URL", "http://127.0.0.1:8810")

    facet = _facet(tmp_path)
    facet.start()

    assert calls == [{
        "interval_seconds": 90,
        "reporter_kwargs": {
            "repo_root": Path(__file__).resolve().parents[1],
            "env": {
                "HOARD_HUB_URL": "http://127.0.0.1:62841",
                "HOARD_HUB_DATA_DIR": str(tmp_path),
                "HOARD_HUB_TOKEN_FILE": str(tmp_path / "mcp-token"),
            },
        },
    }]
    assert os.environ["HOARD_HUB_URL"] == "http://127.0.0.1:8810"

    facet.close()
    assert stopped == [True]


def _mock_faustus_controller(tmp_path, monkeypatch, *, active=False):
    controller_dir = tmp_path / "faustus-agora"
    controller_dir.mkdir(exist_ok=True)
    (controller_dir / "faustus_agora.py").write_text("# controller stub", encoding="utf-8")
    (controller_dir / "run.lock").touch(exist_ok=True)
    monkeypatch.setenv("FAUSTUS_AGORA_DIR", str(controller_dir))
    snapshot = {"session": "controller-owned-session", "local_turn_active": active}
    (controller_dir / "state.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(snapshot)))


def test_facet_order_routes_console_before_legacy_agora():
    assert FACET_MODULES.index("agora_console") < FACET_MODULES.index("agora")


def test_overview_uses_canonical_thread_and_persisted_preferences(tmp_path):
    facet = _facet(tmp_path)
    saved = facet.set_preferences({"leader_id": "codex", "model": "codex-model-from-cli"})
    assert saved["ok"] and saved["verified"] and saved["applies_to"] == "next_console_leader_turn_and_child_worker_default"

    response = facet.get(_request("GET", "/api/agora/console/overview"))

    assert response["thread_id"] == THREAD_ID
    assert response["leaders"][0]["model"] == "codex-model-from-cli"
    assert response["leaders"][0]["effort"] == "low"
    assert response["leaders"][2]["state"] == "unavailable"
    assert response["quota_pools"][0]["pools"][0]["pool_id"] == "team"
    assert response["messages"]["thread"]["id"] == THREAD_ID


def test_message_posts_attributed_canonical_message_and_queues_real_provider_turn(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    facet.set_preferences({"leader_id": "codex", "model": "codex-model-from-cli"})
    monkeypatch.setattr("hoard_link.hub.agora_console.shutil.which", lambda _: "codex-test.exe")
    monkeypatch.setattr(facet, "_ensure_leader_drain", lambda _: None)

    response = facet.post(_request("POST", "/api/agora/console/message", {
        "body": "Review this candidate when your next turn starts.", "recipients": ["codex", "faustus"],
        "actor": "codex-investiga"
    }))

    assert response["ok"]
    assert response["thread_id"] == THREAD_ID and response["message_id"] == 902
    assert response["author"] == "luis"
    assert response["dispatch"][0]["status"] == "queued"
    assert "independent Codex console session" in response["dispatch"][0]["detail"]
    assert response["dispatch"][0]["legacy_mention"]["status"] == "queued"
    assert response["dispatch"][1]["status"] == "queued"
    assert facet.hub.agora.posts == [{"agent": "luis", "_person": True, "thread_id": THREAD_ID,
                                     "body": "Review this candidate when your next turn starts.",
                                     "kind": "comment", "mentions": ["codex-investiga"]}]
    with facet._db() as db:
        turn = db.execute("SELECT state,model,effort,prompt FROM leader_turns").fetchone()
    assert turn["state"] == "queued" and turn["model"] == "codex-model-from-cli" and turn["effort"] == "low"
    assert "Recent canonical thread 32" in turn["prompt"]


def test_http_and_mcp_share_write_handlers_and_auth_boundary(tmp_path):
    facet = _facet(tmp_path)
    denied = facet.post(_request("POST", "/api/agora/console/preferences", {}, caller=None))
    assert denied["status"] == 401

    tools = {tool["name"]: tool for tool in facet.tools()}
    assert {"hub_agora_console_overview", "hub_agora_console_message", "hub_agora_console_preferences",
            "hub_agora_console_worker"} <= set(tools)
    assert tools["hub_agora_console_overview"]["annotations"]["readOnlyHint"] is True
    assert tools["hub_agora_console_message"]["annotations"]["readOnlyHint"] is False
    result = facet.handlers()["hub_agora_console_preferences"]({
        "leader_id": "codex", "model": "codex-model-from-cli", "effort": "low", "actor": "cursor-agora"})
    assert result["ok"] and result["verified"]
    assert facet._preferences()["codex"]["updated_by"] == "cursor-agora"


def test_worker_unavailable_is_recorded_without_fabricating_a_reply(tmp_path):
    facet = _facet(tmp_path)

    response = facet.worker({"leader_id": "claude", "model": "unknown", "effort": "high",
                             "task": "Summarize the open review thread."})

    assert response["ok"]
    assert response["worker"]["state"] == "unavailable"
    assert response["worker"]["result"] == ""
    assert "no catalog" in response["worker"]["detail"]
    assert response["worker"]["agent_id"].startswith("claude-worker-")
    assert facet.overview()["workers"][0]["id"] == response["worker"]["id"]


def test_worker_prompt_and_receipt_use_a_distinct_child_identity(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    facet.set_preferences({"leader_id": "codex", "model": "codex-model-from-cli", "effort": "low"})
    monkeypatch.setattr(facet, "_codex_executable", lambda: "codex-test.exe")
    commands = []
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.Popen", lambda command, **kwargs: (
        commands.append((command, kwargs)) or SimpleNamespace(pid=1234)))

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs
        def start(self):
            pass
    monkeypatch.setattr("hoard_link.hub.agora_console.threading.Thread", DeferredThread)

    response = facet.worker({"leader_id": "codex", "model": "codex-model-from-cli", "effort": "low",
                             "task": "Inspect one isolated candidate."})

    worker = response["worker"]
    command = commands[0][0]
    prompt = command[-1]
    assert worker["agent_id"] == f"codex-worker-{worker['id'][:8]}"
    assert worker["parent_id"] == "codex"
    assert f"--as {worker['agent_id']} hb" in prompt
    assert "create or claim an Agora task" in prompt
    assert "CRITERIOS.md" in prompt and "MEMORY.md" in prompt
    assert commands[0][1]["cwd"] == str(Path(__file__).resolve().parents[1])


def test_external_worker_registration_requires_real_receipt_and_tracks_lifecycle(tmp_path):
    facet = _facet(tmp_path)
    incomplete = facet.worker({"action": "register", "worker_id": "child-1", "leader_id": "codex"})
    assert not incomplete["ok"] and incomplete["status"] == 400

    registered = facet.worker({"action": "register", "worker_id": "child-1", "parent_id": "codex-console",
                               "leader_id": "codex", "model": "gpt-6-luna", "effort": "low",
                               "task": "Review an isolated candidate", "session_id": "actual-session-17",
                               "model_source": "runner metadata"})
    assert registered["ok"]
    worker = registered["worker"]
    assert worker["state"] == "running" and worker["session_id"] == "actual-session-17"
    assert worker["model_source"] == "runner metadata" and worker["result"] == ""

    heartbeat = facet.worker({"action": "heartbeat", "worker_id": "child-1"})["worker"]
    assert heartbeat["state"] == "running" and heartbeat["session_id"] == "actual-session-17"
    finished = facet.worker({"action": "finish", "worker_id": "child-1", "result": "Review completed."})["worker"]
    assert finished["state"] == "completed" and finished["result"] == "Review completed."


def test_cursor_catalog_is_discovered_from_installed_cli_model_output(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="Available models\nauto - Auto\ngpt-5.6-luna-low - GPT 5.6 Luna Low\nclaude-sonnet-5-high - Claude Sonnet High\ngpt-5.3-codex-fast - GPT Codex Fast\n"))

    provider = facet._cursor_status()

    assert provider["state"] == "available"
    assert provider["current_model"] == "auto" and provider["current_effort"] == "default"
    assert provider["catalog"] == [
        {"id": "auto", "name": "Auto", "efforts": ["default"],
         "default_effort": "default", "effort_in_model_id": False},
        {"id": "gpt-5.6-luna-low", "name": "GPT 5.6 Luna Low", "efforts": ["low"],
         "default_effort": "low", "effort_in_model_id": True},
        {"id": "claude-sonnet-5-high", "name": "Claude Sonnet High", "efforts": ["high"],
         "default_effort": "high", "effort_in_model_id": True},
        {"id": "gpt-5.3-codex-fast", "name": "GPT Codex Fast", "efforts": ["default"],
         "default_effort": "default", "effort_in_model_id": False},
    ]


def test_codex_runner_falls_back_to_the_installed_app_binary(tmp_path, monkeypatch):
    executable = tmp_path / "OpenAI" / "Codex" / "bin" / "installed-version" / "codex.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"installed CLI marker")
    monkeypatch.setattr("hoard_link.hub.agora_console.shutil.which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert AgoraConsoleFacet._codex_executable() == str(executable)


def test_faustus_catalog_only_exposes_the_active_sparks_provider(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "mcp-token").write_text("local-test-token", encoding="utf-8")
    replies = iter([
        (200, {"items": [
            {"endpoint_id": "pc-local", "endpoint_name": "127.0.0.1:11434", "models": ["qwen-local"]},
            {"endpoint_id": "spark-main", "endpoint_name": "Sparks · glm53", "models": ["glm-flash"]},
        ]}),
        (200, {"levels": ["low", "high"], "default": "high", "source": "verified template"}),
    ])
    seen = []
    def get(url, token, timeout):
        seen.append((url, token, timeout))
        return next(replies)
    monkeypatch.setattr("hoard_link.hub.contract._get", get)

    provider = facet._faustus_status()

    assert provider["state"] == "available"
    assert [row["id"] for row in provider["catalog"]] == ["spark-main::glm-flash"]
    assert provider["catalog"][0]["efforts"] == ["low", "high"]
    assert seen[0][0] == "http://127.0.0.1:7000/api/models?background=false"
    assert seen[0][1] == "local-test-token"

    facet._provider_cache["faustus"] = provider
    saved = facet.set_preferences({"leader_id": "faustus", "model": "spark-main::glm-flash", "effort": "low"})
    assert saved["verified"] and saved["applies_to"] == "queued_until_native_session_idle"
    assert "controller is not present" in saved["warning"]
    with facet._db() as db:
        pending = db.execute("SELECT session_id,model,effort,state FROM native_preferences").fetchone()
    assert pending["session_id"] == ""
    assert pending["model"] == "spark-main::glm-flash" and pending["effort"] == "low"
    assert pending["state"] == "pending"


def test_faustus_native_preference_uses_controller_and_api_confirmed_idle_binding(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    sid = "controller-owned-session"
    _mock_faustus_controller(tmp_path, monkeypatch)
    facet._provider_cache["faustus"] = {"state": "available", "detail": "verified catalog", "catalog": [
        {"id": "spark-main::glm-flash", "endpoint_id": "spark-main", "model_id": "glm-flash",
         "efforts": ["low", "high"], "default_effort": "high"}]}
    facet._provider_at = 10**9
    calls = []
    def request(method, path, fields=None):
        calls.append((method, path, fields))
        if path.startswith("/api/projects/resolve/session/"):
            return 200, {"project": {"id": "faustus-project"}}
        if path == f"/api/coordination/{sid}":
            return 200, {"binding": {"session_id": sid, "project_id": "faustus-project",
                                     "actor": "faustus", "active": True}}
        if path == "/api/chat/activity":
            return 200, {"running": [], "unverified": 0}
        if method == "PATCH":
            return 200, {"id": sid, "model": "glm-flash", "reasoning_effort": "low"}
        if sum(call[0] == "GET" for call in calls) == 1:
            return 200, {"session_id": sid, "reasoning_effort": "high", "available_levels": ["low", "high"]}
        return 200, {"session_id": sid, "reasoning_effort": "low", "available_levels": ["low", "high"]}
    monkeypatch.setattr(facet, "_faustus_session_request", request)

    saved = facet.set_preferences({"leader_id": "faustus", "model": "spark-main::glm-flash", "effort": "low"})

    assert saved["verified"] and saved["applies_to"] == "next_native_session_turn" and saved["warning"] is None
    patch = next(call for call in calls if call[0] == "PATCH")
    assert patch[1] == f"/api/session/{sid}"
    assert patch[2] == {"model": "glm-flash", "endpoint_id": "spark-main", "endpoint_url": "",
                        "reasoning_effort": "low"}
    with facet._db() as db:
        native = db.execute("SELECT session_id,model,effort,state FROM native_preferences").fetchone()
    assert tuple(native) == (sid, "spark-main::glm-flash", "low", "applied")


def test_faustus_native_preference_waits_when_controller_reports_active_turn(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    session_id = "controller-owned-session"
    _mock_faustus_controller(tmp_path, monkeypatch, active=True)
    facet._provider_cache["faustus"] = {"state": "available", "detail": "verified catalog", "catalog": [
        {"id": "spark-main::glm-flash", "endpoint_id": "spark-main", "model_id": "glm-flash",
         "efforts": ["low", "high"], "default_effort": "high"}]}
    facet._provider_at = 10**9
    calls = []
    def request(method, path, fields=None):
        calls.append((method, path, fields))
        return 503, {"error": "not expected while controller is active"}
    monkeypatch.setattr(facet, "_faustus_session_request", request)

    saved = facet.set_preferences({"leader_id": "faustus", "model": "spark-main::glm-flash", "effort": "low"})

    assert saved["applies_to"] == "queued_until_native_session_idle"
    assert "observed working" in saved["warning"]
    assert not calls
    with facet._db() as db:
        native = db.execute("SELECT session_id,state FROM native_preferences").fetchone()
    assert tuple(native) == (session_id, "pending")

    _mock_faustus_controller(tmp_path, monkeypatch, active=False)
    def idle_request(method, path, fields=None):
        calls.append((method, path, fields))
        if path.startswith("/api/projects/resolve/session/"):
            return 200, {"project": {"id": "faustus-project"}}
        if path == f"/api/coordination/{session_id}":
            return 200, {"binding": {"session_id": session_id, "project_id": "faustus-project",
                                     "actor": "faustus", "active": True}}
        if path == "/api/chat/activity":
            return 200, {"running": [], "unverified": 0}
        if method == "PATCH":
            return 200, {"id": session_id, "model": "glm-flash", "reasoning_effort": "low"}
        level = "high" if sum(call[0] == "GET" and "/reasoning-effort" in call[1] for call in calls) == 1 else "low"
        return 200, {"session_id": session_id, "reasoning_effort": level,
                     "available_levels": ["low", "high"]}
    monkeypatch.setattr(facet, "_faustus_session_request", idle_request)
    overview = facet.overview()
    assert overview["ok"]
    assert any(call[0] == "PATCH" for call in calls)
    with facet._db() as db:
        native = db.execute("SELECT state FROM native_preferences").fetchone()
    assert native["state"] == "applied"


def test_faustus_native_preference_rejects_foreign_coordination_binding(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    _mock_faustus_controller(tmp_path, monkeypatch)
    facet._provider_cache["faustus"] = {"state": "available", "catalog": [
        {"id": "spark-main::glm-flash", "endpoint_id": "spark-main", "model_id": "glm-flash",
         "efforts": ["low", "high"]}]}
    facet._provider_at = 10**9
    def request(method, path, fields=None):
        if path.startswith("/api/projects/resolve/session/"):
            return 200, {"project": {"id": "faustus-project"}}
        if path == "/api/coordination/controller-owned-session":
            return 200, {"binding": {"session_id": "controller-owned-session", "project_id": "faustus-project",
                                     "actor": "codex-investiga", "active": True}}
        raise AssertionError("an unverified binding must prevent any further session API request")
    monkeypatch.setattr(facet, "_faustus_session_request", request)

    saved = facet.set_preferences({"leader_id": "faustus", "model": "spark-main::glm-flash", "effort": "low"})

    assert saved["applies_to"] == "queued_until_native_session_idle"
    assert "actor/project binding" in saved["warning"]
    with facet._db() as db:
        native = db.execute("SELECT state FROM native_preferences").fetchone()
    assert native["state"] == "pending"


def test_faustus_session_patch_uses_configured_owner_token_and_form_contract(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    monkeypatch.setenv("FAUSTUS_OWNER_API_TOKEN", "configured-owner-token")
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return b'{"id":"session-1"}'
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())

    result = facet._faustus_session_request("PATCH", "/api/session/session-1", {
        "model": "glm-flash", "endpoint_id": "spark-main", "endpoint_url": "", "reasoning_effort": "low"})

    assert result == (200, {"id": "session-1"})
    request, timeout = requests[0]
    assert request.full_url == "http://127.0.0.1:7000/api/session/session-1"
    assert request.get_method() == "PATCH" and timeout == 8.0
    assert request.headers["Authorization"] == "Bearer configured-owner-token"
    assert parse_qs(request.data.decode("utf-8"), keep_blank_values=True) == {
        "model": ["glm-flash"], "endpoint_id": ["spark-main"], "endpoint_url": [""],
        "reasoning_effort": ["low"]}


def test_faustus_session_patch_does_not_forward_family_token(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    monkeypatch.delenv("FAUSTUS_OWNER_API_TOKEN", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "mcp-token").write_text("family-agent-token", encoding="utf-8")
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *args):
            return b'{"session_id":"session-1"}'
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())

    result = facet._faustus_session_request("GET", "/api/session/session-1/reasoning-effort")

    assert result == (200, {"session_id": "session-1"})
    assert "Authorization" not in requests[0].headers


def test_faustus_child_session_creation_uses_native_form_api(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *args):
            return b'{"id":"child-session"}'
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())

    status, data = facet._faustus_session_request("POST", "/api/session", {
        "name": "Agora worker faustus-worker-a1b2c3d4", "model": "glm-flash",
        "endpoint_id": "spark-main", "project_id": "faustus-project", "skip_validation": "true"})

    request, timeout = requests[0]
    assert (status, data) == (200, {"id": "child-session"})
    assert request.get_method() == "POST" and timeout == 8.0
    assert request.full_url.endswith("/api/session")
    assert parse_qs(request.data.decode("utf-8")) == {
        "name": ["Agora worker faustus-worker-a1b2c3d4"], "model": ["glm-flash"],
        "endpoint_id": ["spark-main"], "project_id": ["faustus-project"], "skip_validation": ["true"]}


def test_faustus_worker_starts_independent_native_project_session(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    monkeypatch.delenv("FAUSTUS_OWNER_API_TOKEN", raising=False)
    monkeypatch.setenv("FAUSTUS_API_TOKEN", "family-token-must-not-be-forwarded")
    _mock_faustus_controller(tmp_path, monkeypatch)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "faustus_run.py").write_text("# installed runner", encoding="utf-8")
    facet._provider_cache["faustus"] = {"state": "available", "detail": "real Sparks catalog", "catalog": [
        {"id": "spark-main::glm-flash", "model_id": "glm-flash", "endpoint_id": "spark-main",
         "efforts": ["low", "high"], "default_effort": "high"}]}
    calls = []
    def request(method, path, fields=None):
        calls.append((method, path, fields))
        if path.startswith("/api/projects/resolve/session/"):
            return 200, {"project": {"id": "faustus-project", "workspace": str(tmp_path / "workspace")}}
        if path == "/api/coordination/controller-owned-session":
            return 200, {"binding": {"session_id": "controller-owned-session", "project_id": "faustus-project",
                                     "actor": "faustus", "active": True}}
        if path == "/api/chat/activity":
            return 200, {"running": [], "unverified": 0}
        if method == "POST" and path == "/api/session":
            return 200, {"id": "new-child-session", "model": "glm-flash"}
        if path == "/api/projects/resolve/session/new-child-session":
            return 200, {"project": {"id": "faustus-project", "workspace": str(tmp_path / "workspace")}}
        if method == "PATCH" and path == "/api/session/new-child-session":
            return 200, {"id": "new-child-session", "model": "glm-flash", "reasoning_effort": "low"}
        if path == "/api/session/new-child-session/reasoning-effort":
            return 200, {"session_id": "new-child-session", "reasoning_effort": "low",
                         "available_levels": ["low", "high"]}
        raise AssertionError((method, path))
    monkeypatch.setattr(facet, "_faustus_session_request", request)
    commands = []
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.Popen", lambda command, **kwargs: (
        commands.append((command, kwargs)) or SimpleNamespace(pid=4321)))

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs
        def start(self):
            pass
    monkeypatch.setattr("hoard_link.hub.agora_console.threading.Thread", DeferredThread)

    response = facet.worker({"leader_id": "faustus", "model": "spark-main::glm-flash", "effort": "low",
                             "task": "Inspect this isolated project."})

    worker = response["worker"]
    command, options = commands[0]
    assert worker["state"] == "running" and worker["agent_id"].startswith("faustus-worker-")
    assert worker["session_id"] == "new-child-session" and worker["parent_id"] == "faustus"
    assert command[command.index("--session") + 1] == "new-child-session"
    assert command[command.index("--project-id") + 1] == "faustus-project"
    assert command[command.index("--agent-id") + 1] == worker["agent_id"]
    assert command[command.index("--model") + 1] == "glm-flash"
    assert command[command.index("--effort") + 1] == "low" and "--approve" in command
    assert options["cwd"] == str(tmp_path)
    assert options["env"].get("FAUSTUS_API_TOKEN") is None
    created = next(call for call in calls if call[0] == "POST")
    assert created[2]["project_id"] == "faustus-project" and created[2]["endpoint_id"] == "spark-main"
    patched_paths = [call[1] for call in calls if call[0] == "PATCH"]
    assert patched_paths == ["/api/session/new-child-session"]


def test_claude_auth_status_is_live_sanitized_and_does_not_invent_catalog(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    monkeypatch.setattr("hoard_link.hub.agora_console.shutil.which", lambda _: "claude-test.exe")
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='{"loggedIn":true,"email":"private@example.test"}'))

    provider = facet._claude_status()

    assert provider["state"] == "unverified"
    assert provider["auth_state"] == "reported_logged_in"
    assert provider["checked_at"].endswith("Z")
    assert provider["catalog"] == []
    assert "private@example.test" not in provider["detail"]


def test_provider_diagnostics_are_persisted_with_probe_timestamp(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    monkeypatch.setattr(facet, "_codex_catalog", lambda: {"state": "available", "detail": "catalog", "catalog": []})
    monkeypatch.setattr(facet, "_claude_status", lambda: {"state": "unverified", "auth_state": "unknown", "detail": "no safe catalog", "catalog": []})
    monkeypatch.setattr(facet, "_cursor_status", lambda: {"state": "unavailable", "detail": "not installed", "catalog": []})
    monkeypatch.setattr(facet, "_faustus_status", lambda: {"state": "unavailable", "detail": "not reachable", "catalog": []})

    providers = facet._provider_data(force=True)

    with facet._db() as db:
        rows = {row["provider_id"]: dict(row) for row in db.execute("SELECT * FROM provider_diagnostics")}
    assert set(rows) == {"codex", "claude", "cursor", "faustus"}
    assert rows["claude"]["auth_state"] == "unknown"
    assert rows["claude"]["checked_at"] == providers["claude"]["checked_at"]
    assert rows["faustus"]["checked_at"].endswith("Z")


def test_console_static_routes_index_and_rejects_escape(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text('<main>Agora</main><script src="./assets/app.js"></script>', encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    monkeypatch.setattr(facet, "_static_root", lambda: root)

    result = facet.get(_request("GET", "/agora"))
    assert result.status == 200
    assert result.body == b'<main>Agora</main><script src="/agora/assets/app.js"></script>'
    escaped = facet.get(_request("GET", "/agora/../secret.txt"))
    assert escaped["status"] == 404


def test_existing_agora_usage_route_is_delegated_to_legacy_facet(tmp_path):
    facet = _facet(tmp_path)
    assert facet.get(_request("GET", "/api/agora/console/usage")) is None


def test_mcp_dispatch_calls_the_same_console_handler_as_http(tmp_path):
    facet = _facet(tmp_path)
    facet.hub.facets = [facet]

    response = call(facet.hub, "hub_agora_console_preferences", {
        "leader_id": "codex", "model": "codex-model-from-cli", "effort": "high"})

    assert response["ok"] and response["verified"]
    assert facet._preferences()["codex"]["model"] == "codex-model-from-cli"


def test_mcp_message_identity_is_explicit_and_cannot_forge_luis(tmp_path):
    facet = _facet(tmp_path)
    result = facet.handlers()["hub_agora_console_message"]({"body": "hello", "recipients": ["claude"], "actor": "claude"})
    assert result["author"] == "claude"
    assert facet.hub.agora.posts[-1]["agent"] == "claude"

    rejected = facet.handlers()["hub_agora_console_message"]({"body": "hello", "actor": "luis"})
    assert not rejected["ok"] and rejected["status"] == 400


def test_authenticated_agent_identity_is_preserved_by_http_dispatch(tmp_path):
    facet = _facet(tmp_path)

    response = facet.post(_request("POST", "/api/agora/console/message", {
        "body": "Reply under my own identity.", "recipients": ["faustus"], "actor": "luis"},
        caller="codex-agora", agent=True))

    assert response["ok"] and response["author"] == "codex-agora"
    assert facet.hub.agora.posts[-1]["agent"] == "codex-agora"


def test_console_leader_posts_only_a_real_runner_result_with_its_own_identity(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    with facet._db() as db:
        db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at) VALUES('turn-1','codex','902','prompt','codex-model-from-cli','high','working',1,1)")

    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text("A real answer from the Codex CLI.", encoding="utf-8")
        log_path = tmp_path / "agora-console" / "leaders" / "codex" / "turn-1.jsonl"
        log_path.write_text('{"type":"thread.started","payload":{"thread_id":"provider-session-9"}}\n', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hoard_link.hub.agora_console.shutil.which", lambda _: "codex-test.exe")
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.run", runner)
    facet._run_leader_turn({"id": "turn-1", "leader_id": "codex", "model": "codex-model-from-cli",
                            "effort": "high", "prompt": "prompt"})

    with facet._db() as db:
        turn = db.execute("SELECT state,result,session_id FROM leader_turns WHERE id='turn-1'").fetchone()
        session = db.execute("SELECT session_id,model,effort,state FROM leader_sessions WHERE leader_id='codex'").fetchone()
    assert tuple(turn) == ("completed", "A real answer from the Codex CLI.", "provider-session-9")
    assert tuple(session) == ("provider-session-9", "codex-model-from-cli", "high", "idle")
    assert "--sandbox" in commands[0] and commands[0][commands[0].index("--sandbox") + 1] == "danger-full-access"
    assert 'approval_policy="never"' in commands[0] and "--dangerously-bypass-approvals-and-sandbox" not in commands[0]
    with facet._db() as db:
        metrics = db.execute("SELECT process_exit_code,outcome,tool_success_count,tool_failure_count FROM leader_turns WHERE id='turn-1'").fetchone()
    assert tuple(metrics) == (0, "unverified", 0, 0)
    assert facet.hub.agora.posts[-1] == {"agent": "codex-agora", "thread_id": THREAD_ID,
                                         "body": "A real answer from the Codex CLI.", "kind": "comment", "mentions": []}


def test_console_leader_resume_uses_supported_resume_options_and_keeps_session_cwd(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    with facet._db() as db:
        db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at) VALUES('turn-2','codex','903','new explicit prompt','codex-model-from-cli','low','working',2,2)")
        db.execute("INSERT INTO leader_sessions(leader_id,session_id,model,effort,state,updated_at) VALUES('codex','provider-session-9','codex-model-from-cli','high','working',1)")

    commands, run_kwargs = [], []
    def runner(command, **kwargs):
        commands.append(command)
        run_kwargs.append(kwargs)
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text("Reply from the resumed provider session.", encoding="utf-8")
        log_path = tmp_path / "agora-console" / "leaders" / "codex" / "turn-2.jsonl"
        log_path.write_text('{"type":"thread.started","payload":{"thread_id":"provider-session-9"}}\n', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hoard_link.hub.agora_console.shutil.which", lambda _: "codex-test.exe")
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.run", runner)
    facet._run_leader_turn({"id": "turn-2", "leader_id": "codex", "model": "codex-model-from-cli",
                            "effort": "low", "prompt": "new explicit prompt"})

    command = commands[0]
    resume = command.index("resume")
    assert command[resume:resume + 3] == ["resume", "--json", "--model"]
    assert command[resume + 3] == "codex-model-from-cli"
    assert command[resume + 4:resume + 6] == ["--config", 'model_reasoning_effort="low"']
    assert command[resume + 6:resume + 8] == ["--config", 'approval_policy="never"']
    assert command[resume + 8:resume + 10] == ["--config", 'sandbox_mode="danger-full-access"']
    assert command[resume + 10] == "provider-session-9"
    assert "--sandbox" not in command and "--cd" not in command
    assert run_kwargs[0]["cwd"] is None
    with facet._db() as db:
        turn = db.execute("SELECT state,result,session_id FROM leader_turns WHERE id='turn-2'").fetchone()
    assert tuple(turn) == ("completed", "Reply from the resumed provider session.", "provider-session-9")


def test_console_preserves_provider_text_and_reports_task_outcome_as_unverified(tmp_path, monkeypatch):
    facet = _facet(tmp_path)
    with facet._db() as db:
        db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at) VALUES('turn-incomplete','codex','902','prompt','codex-model-from-cli','high','working',1,1)")

    def runner(command, **kwargs):
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text("No pude completar la revisión: el entorno no tiene la herramienta.", encoding="utf-8")
        log_path = tmp_path / "agora-console" / "leaders" / "codex" / "turn-incomplete.jsonl"
        log_path.write_text('{"type":"thread.started","payload":{"thread_id":"session-10"}}\n', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hoard_link.hub.agora_console.shutil.which", lambda _: "codex-test.exe")
    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.run", runner)
    facet._run_leader_turn({"id": "turn-incomplete", "leader_id": "codex", "model": "codex-model-from-cli",
                            "effort": "high", "prompt": "prompt"})

    with facet._db() as db:
        turn = db.execute("SELECT state,result,detail,outcome,process_exit_code FROM leader_turns WHERE id='turn-incomplete'").fetchone()
    assert turn["state"] == "completed" and "No pude completar" in turn["result"]
    assert turn["outcome"] == "unverified" and turn["process_exit_code"] == 0
    assert "task outcome is unverified" in turn["detail"]
    assert facet.hub.agora.posts[-1]["body"] == turn["result"]


def test_runner_metrics_keep_tool_success_and_failure_counts_separate(tmp_path):
    log = tmp_path / "provider.jsonl"
    log.write_text("\n".join([
        '{"type":"item.completed","payload":{"item":{"type":"command_execution","exit_code":0}}}',
        '{"type":"item.completed","payload":{"item":{"type":"command_execution","exit_code":1}}}',
        '{"type":"turn.failed","payload":{"message":"provider returned an explicit error"}}',
    ]), encoding="utf-8")

    metrics = AgoraConsoleFacet._runner_metrics(log)

    assert metrics["tool_success_count"] == 1 and metrics["tool_failure_count"] == 1
    assert metrics["provider_error"] == "provider returned an explicit error"


def test_hub_restart_marks_ambiguous_leader_turn_unknown_without_replay(tmp_path):
    facet = _facet(tmp_path)
    with facet._db() as db:
        db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at) VALUES('ambiguous','cursor','902','prompt','cursor-model','high','working',1,1)")
        db.execute("INSERT INTO leader_sessions(leader_id,session_id,state,updated_at) VALUES('cursor','cursor-session','working',1)")

    facet._reconcile_workers()

    with facet._db() as db:
        turn = db.execute("SELECT state,detail FROM leader_turns WHERE id='ambiguous'").fetchone()
        session = db.execute("SELECT state FROM leader_sessions WHERE leader_id='cursor'").fetchone()
    assert turn["state"] == "unknown" and "not be retried automatically" in turn["detail"]
    assert session["state"] == "unknown"
    started = []
    facet._run_leader_turn = lambda turn: started.append(turn)
    facet._drain_leader("cursor")
    assert not started
    queued = facet.worker({"action": "reconcile", "leader_id": "cursor", "session_id": "cursor-session",
                           "confirm_idle": True})
    assert queued["ok"] and "remains unknown" in queued["detail"]
    # The explicit operator acknowledgement may release later queued work, but
    # it never changes the ambiguous turn's terminal record.
    with facet._db() as db:
        assert db.execute("SELECT state FROM leader_turns WHERE id='ambiguous'").fetchone()["state"] == "unknown"
