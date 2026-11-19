from __future__ import annotations

import time
from types import SimpleNamespace

from hoard_link.hub import agora_worker_profiles as profiles
from hoard_link.hub.agora import Agora
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.facets import Request


def console_for(tmp_path):
    agora = Agora(tmp_path / "agora.db")
    agora.space_create({"id": "estudio", "title": "Estudio", "agent": "codex-builder"})
    config = SimpleNamespace(data_dir=str(tmp_path), token_file=str(tmp_path / "mcp-token"), url="http://127.0.0.1:9940")
    hub = SimpleNamespace(config=config, facet=lambda _: SimpleNamespace(agora=agora))
    return AgoraConsoleFacet(hub), agora


def request(method, *, args=None, caller="ui"):
    return Request(method, profiles.ROUTE, {"space_id": ["estudio"]}, args or {}, lambda: caller, lambda: caller == "hub")


def test_space_profile_is_readable_editable_and_scoped(tmp_path):
    console, agora = console_for(tmp_path)
    try:
        starter = console.get(request("GET"))
        assert "4006" in starter["profile"]["rules"]
        assert "cartógrafa" not in starter["profile"]["context"]
        assert starter["profile"]["auto_review"] is False
        assert set(starter["templates"]) == {"build", "probe", "review", "integrate"}
        assert starter["templates"]["review"]["es"]["label"] == "Revisar"
        assert starter["templates"]["review"]["en"]["label"] == "Review"
        args = {"space_id": "estudio", "expected_revision": 0, "profile": {
            "rules": "Español y café", "context": "A project", "folders": [" work", "work"],
            "repositories": ["repo"], "auto_review": False,
        }}
        assert console.post(request("POST", args={**args, "_person": True}, caller="hub"))["status"] == 403
        assert console.post(request("POST", args=args))["profile"]["revision"] == 1
        assert console.post(request("POST", args=args))["status"] == 409
        handler = console.handlers()[profiles.TOOL]
        current = handler({"space_id": "estudio"})["profile"]
        assert current["folders"] == ["work"] and current["rules"] == "Español y café"
        assert handler({})["profile"]["rules"] == ""
        created_again = AgoraConsoleFacet(console.hub)
        try:
            assert created_again.get(request("GET"))["profile"] == current
        finally:
            created_again.close()
    finally:
        console.close()
        agora.close()


def test_manual_runner_receives_rules_context_paths_and_selected_template(tmp_path, monkeypatch):
    root, agora = console_for(tmp_path)
    console = root.for_space("estudio")
    monkeypatch.setattr(console, "_provider_data", lambda: {"codex": {
        "catalog": [{"id": "small", "efforts": ["low"], "default_effort": "low"}],
    }})
    monkeypatch.setattr(console, "_codex_executable", lambda: "codex-test")
    monkeypatch.setattr(console, "_monitor_worker", lambda *args: None)
    calls = []

    def process(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr("hoard_link.hub.agora_console.subprocess.Popen", process)
    try:
        result = console.worker({"leader_id": "codex", "model": "small", "template": "probe", "task": "Tour the sample"})
        assert result["ok"] and result["undo"]["arguments"]["worker_id"] == result["worker"]["id"]
        prompt, env = calls[0][0][-1], calls[0][1]
        assert "Permanent space rules" in prompt and "Project context" in prompt and "Repositories" in prompt
        assert "Run the scenario" in prompt and "Task: Tour the sample" in prompt
        assert env["AGORA_SPACE"] == "estudio" and env["HOARD_HUB_URL"] == "http://127.0.0.1:9940"
        assert result["worker"]["created_at"] <= time.time()
        assert len(calls) == 1
        assert console.worker({"leader_id": "codex", "model": "small", "template": "undefined", "task": "test"})["status"] == 400
    finally:
        root.close()
        agora.close()
