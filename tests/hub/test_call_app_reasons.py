"""The hub forwards a reason and the agent identity when it calls an app's shared route (accountable agents)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from hoard_link.hub import contract
from hoard_link.hub.core import default_call_reason


class _Recorder(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def log_message(self, *a: Any) -> None:  # noqa: D401 - quiet
        pass

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        _Recorder.seen.append({"path": self.path, "body": body, "agent": self.headers.get("X-Agent-Id"),
                               "session": self.headers.get("X-Agent-Session")})
        reason = body.get("reason") or (body.get("arguments") or {}).get("reason")
        if not reason:
            out, code = {"error": "This tool changes data, so the call needs a reason.", "code": "reason_required"}, 400
        else:
            out, code = {"ok": True, "reason": reason}, 200
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def app(tmp_path):
    _Recorder.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    token = tmp_path / "tok"
    token.write_text("t", encoding="utf-8")
    yield SimpleNamespace(id="demo", url=f"http://127.0.0.1:{srv.server_address[1]}", token_file=str(token))
    srv.shutdown()


def test_reason_agent_and_session_are_forwarded(app):
    res = contract.call_app(app, "note_add", {"text": "x"}, caller="faustus", reason="Save the meeting note",
                            agent="claude", session="s-42", timeout=5)
    assert res["ok"] is True
    seen = _Recorder.seen[-1]
    assert seen["body"]["reason"] == "Save the meeting note"
    assert seen["body"]["agent"] == "claude" and seen["agent"] == "claude"
    assert seen["body"]["session"] == "s-42" and seen["session"] == "s-42"


def test_without_reason_the_app_refusal_reaches_the_caller(app):
    res = contract.call_app(app, "note_add", {"text": "x"}, caller="hub-tool", timeout=5)
    assert res["ok"] is False and res["status"] == 400
    assert "reason" in res["error"]
    assert "reason" not in _Recorder.seen[-1]["body"]


def test_identity_is_cleaned(app):
    contract.call_app(app, "note_add", {}, reason="ok then", agent="bad\nagent" + "x" * 200, timeout=5)
    seen = _Recorder.seen[-1]
    assert "\n" not in seen["agent"] and len(seen["agent"]) <= 80


@pytest.mark.parametrize("caller,expected", [
    ("hub", "Automatic step run by the Hub"),
    ("rule:morning", "Automatic step of the Hub rule morning"),
    ("job:backup", "Automatic step of the Hub job backup"),
    ("faustus", "Requested by faustus through the Hub"),
])
def test_default_reasons(caller, expected):
    assert default_call_reason(caller) == expected
    assert 3 <= len(default_call_reason(caller)) <= 300


def test_hub_gives_default_reason_but_not_to_agents(app, monkeypatch):
    from hoard_link.hub import core
    hub = SimpleNamespace(get=lambda _id: app, _safe_emit=lambda *a, **k: None)
    res = core.Hub.call_app(hub, "demo", "note_add", {"text": "x"}, caller="rule:daily", timeout=5)
    assert res["ok"] is True and _Recorder.seen[-1]["body"]["reason"] == "Automatic step of the Hub rule daily"
    assert _Recorder.seen[-1]["agent"] == "rule:daily"
    res = core.Hub.call_app(hub, "demo", "note_add", {"text": "x"}, caller="hub-tool", timeout=5)
    assert res["ok"] is False and res["status"] == 400
    res = core.Hub.call_app(hub, "demo", "note_add", {"text": "x", "reason": "From the arguments"}, caller="hub-tool", timeout=5)
    assert res["ok"] is True and _Recorder.seen[-1]["body"]["reason"] == "From the arguments"


def test_default_reason_names_the_agent_behind_a_hub_token(app):
    from hoard_link.hub import core
    hub = SimpleNamespace(get=lambda _id: app, _safe_emit=lambda *a, **k: None)
    res = core.Hub.call_app(hub, "demo", "note_add", {"text": "x"}, caller="hub", agent="claude-live", session="s1", timeout=5)
    assert res["ok"] is True
    assert _Recorder.seen[-1]["body"]["reason"] == "Requested by claude-live through the Hub"
    assert _Recorder.seen[-1]["agent"] == "claude-live" and _Recorder.seen[-1]["session"] == "s1"
