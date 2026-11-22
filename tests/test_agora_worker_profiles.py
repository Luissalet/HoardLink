from __future__ import annotations

import json
import time
from types import SimpleNamespace

from hoard_link.hub import agora_worker_profiles as profiles
from hoard_link.hub.agora import Agora
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.facets import Request


def console_for(tmp_path):
    agora = Agora(tmp_path / "agora.db")
    agora.space_create({"id": "estudio", "title": "Estudio", "agent": "codex-builder"})
    config = SimpleNamespace(data_dir=str(tmp_path), token_file=str(tmp_path / "mcp-token"), url="http://127.0.0.1:9940",
                             person_profile=agora.config.person_profile)
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
        assert set(starter["templates"]) == {"build", "probe", "check", "review", "integrate"}
        assert starter["templates"]["check"]["es"]["label"] == "Comprobar piezas"
        assert starter["templates"]["check"]["en"]["label"] == "Check pieces"
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

        view = agora.for_space("estudio")
        order = view.order_create({"agent": "producer", "title": "Check a piece"})["order"]["id"]
        path = tmp_path / "piece.txt"
        preview = tmp_path / "detail.png"

        def register(content):
            path.write_text(content, encoding="utf-8")
            preview.write_bytes(content.encode())
            return view.piece_add({"agent": "producer", "order_id": order, "kind": "text",
                                   "title": "Candidate", "path": str(path), "previews": [str(preview)]})["piece"]

        old = register("old")
        current = register("current")
        first_path = tmp_path / "first.txt"
        first_path.write_text("standalone", encoding="utf-8")
        first = view.piece_add({"agent": "producer", "order_id": order, "kind": "text",
                                "title": "First version", "path": str(first_path)})["piece"]
        task_id = view.plan_set({"agent": "producer", "order_id": order, "jobs": [
            {"key": "check", "title": "Compare", "inputs": [current["id"], first["id"]]},
        ]})["keys"]["check"]
        args = {"leader_id": "codex", "model": "small", "template": "check", "task": "Compare changes"}
        assert console.worker({**args, "task_id": task_id})["ok"]
        prompt = calls[1][0][-1]
        payload = prompt.split("revision_comparisons:\n", 1)[1].split("\n\nTask:", 1)[0]
        assert json.loads(payload) == view.job({"task_id": task_id})["job"]["revision_comparisons"]
        assert "sin referencia anterior" in payload
        proposal = console.worker({"action": "propose_files", "task_id": task_id})["file_scope"]
        assert set(proposal["read_files"]) == {
            old["stored"], *old["previews_stored"], current["stored"], *current["previews_stored"], first["stored"],
        }
        assert console.worker(args)["ok"]
        assert "Sin referencia anterior aportada por la consola." in calls[2][0][-1]
    finally:
        root.close()
        agora.close()
