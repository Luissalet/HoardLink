from types import SimpleNamespace
import json
import threading

import pytest

from hoard_link.hub import agent_routes, contract, mcp
from hoard_link.hub.core import Hub
from hoard_link.service_contracts import SERVICE_TOOLS


@pytest.fixture
def voice_hub(monkeypatch):
    hub = object.__new__(Hub)
    hub._lock = threading.RLock()
    hub._apps = {app: SimpleNamespace(id=app, url="http://fixture.invalid", token_file="") for app in
                 ("boltagon", "funes", "prospero")}
    hub.facet = lambda key: None
    hub._safe_emit = lambda *args: None
    hub.start = lambda *args, **kwargs: pytest.fail("automatic start")
    hub.stop = lambda *args, **kwargs: pytest.fail("automatic stop")
    hub.automation_blocked = set().__contains__
    state = {"catalogue": {app: set(tools) for app, tools in (
        ("boltagon", {*SERVICE_TOOLS["stt"], *SERVICE_TOOLS["tts"], *SERVICE_TOOLS["dictation"]}),
        ("funes", SERVICE_TOOLS["stt"]), ("prospero", SERVICE_TOOLS["tts"]))}, "calls": [], "probes": []}

    def catalogue(app, **kwargs):
        state["probes"].append(app.id)
        advertised = state["catalogue"].get(app.id)
        return {"ok": advertised is not None, "tools": [{"name": tool} for tool in advertised or ()]}

    def call(app, tool, args, **kwargs):
        state["calls"].append((app.id, tool, args, kwargs.get("caller")))
        return {"ok": True, "app": app.id, "status": 200, "result": {
            "job_id": args.get("job_id") or "job123", "status": "done", "path": "sample.wav",
        }}

    monkeypatch.setattr(contract, "app_tools", catalogue)
    monkeypatch.setattr(contract, "call_app", call)
    return hub, state


@pytest.mark.parametrize("address", ["prospero", "boltagon", "tts"])
def test_agent_speaks_through_registered_owner_without_generation(voice_hub, address, monkeypatch):
    hub, state = voice_hub
    def request(method, path, body, **kwargs):
        assert (method, path) == ("POST", "/api/agent/call")
        reply, status = agent_routes.invoke(hub, body["tool"], body["arguments"])
        return status, reply

    monkeypatch.setattr(mcp, "_ensure_hub", lambda: True)
    monkeypatch.setattr(mcp, "_request", request)
    monkeypatch.setattr(mcp, "request_context", lambda: None)
    reply = mcp.handle({"id": 1, "method": "tools/call", "params": {
        "name": "hub_call_app", "arguments": {
            "app": address, "tool": "voice_tts", "arguments": {"text": "Veinte euros"},
        },
    }})["result"]
    assert not reply["isError"]
    result = json.loads(reply["content"][0]["text"])
    assert result["service_owner"] == "boltagon"
    assert state["probes"] == ["boltagon"]
    assert state["calls"] == [("boltagon", "voice_tts", {"text": "Veinte euros"}, "hub-tool")]


@pytest.mark.parametrize("problem", ["down", "tool_missing", "manually_stopped"])
def test_voice_falls_back_before_sending_work(voice_hub, problem):
    hub, state = voice_hub
    if problem == "down":
        del state["catalogue"]["boltagon"]
    elif problem == "tool_missing":
        state["catalogue"]["boltagon"].discard("voice_tts")
    else:
        hub.automation_blocked = {"boltagon"}.__contains__
    result = hub.call_app("tts", "voice_tts", {"text": "Hola"})
    assert result["ok"] and result["service_owner"] == "prospero"
    assert [call[0] for call in state["calls"]] == ["prospero"]
    assert ("boltagon" in state["probes"]) == (problem != "manually_stopped")


def test_old_and_new_transcription_jobs_follow_their_issuing_provider(voice_hub):
    hub, state = voice_hub
    new = hub.call_app("funes", "transcribe_file", {"path": "sample.wav"})
    handle = new["result"]["job_id"]
    assert handle != "job123" and new["service_owner"] == "boltagon"
    before = len(state["probes"])
    for tool in ("transcribe_status", "transcribe_cancel"):
        result = hub.call_app("funes", tool, {"job_id": handle})
        assert result["result"]["job_id"] == handle
        result = hub.call_app("funes", tool, {"job_id": "old-id"})
        assert result["result"]["job_id"] == "old-id"
    assert len(state["probes"]) == before
    assert [(app, tool, args["job_id"]) for app, tool, args, _ in state["calls"][1:]] == [
        ("boltagon", "transcribe_status", "job123"), ("funes", "transcribe_status", "old-id"),
        ("boltagon", "transcribe_cancel", "job123"), ("funes", "transcribe_cancel", "old-id"),
    ]


def test_voice_alias_and_provider_delegation(voice_hub):
    hub, state = voice_hub
    assert hub.get("cyrano").id == "boltagon"
    assert hub.call_app("cyrano", "dictation_understand", {})["app"] == "boltagon"
    assert hub.call_app("dictation", "dictation_understand", {})["service_owner"] == "boltagon"
    assert hub.call_app("funes", "transcribe_file", {"path": "sample.wav"}, caller="boltagon")["service_owner"] == "funes"
    assert hub.call_app("prospero", "voice_tts", {"text": "Hola"}, caller="cyrano")["service_owner"] == "prospero"
    assert state["probes"] == ["boltagon", "funes", "prospero"]
    hub.call_app("prospero", "voice_job", {"job_id": "an-audiobook"})
    assert state["calls"][-1][0:2] == ("prospero", "voice_job")


def test_an_ambiguous_work_reply_is_never_replayed(voice_hub, monkeypatch):
    hub, state = voice_hub

    def lost(app, *args, **kwargs):
        assert app.id == "boltagon"
        return {"ok": False, "status": None, "contract": "shared", "error": "response lost"}

    monkeypatch.setattr(contract, "call_app", lost)
    result = hub.call_app("funes", "transcribe_file", {"path": "sample.wav"})
    assert not result["ok"] and result["error"] == "response lost"
    assert result["status"] == 502 and result["submission_unknown"]
    assert state["probes"] == ["boltagon"]
