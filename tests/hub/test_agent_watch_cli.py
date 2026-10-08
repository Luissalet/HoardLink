"""``agora.py watch`` and ``watch-bind``: the observed state per agent, pending questions and unassigned sessions."""

from __future__ import annotations

import importlib.util
from pathlib import Path

def _cli():
    spec = importlib.util.spec_from_file_location("agora_cli_watch", Path(__file__).resolve().parents[2] / "scripts" / "agora.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_prints_observed_state_and_binds(monkeypatch, capsys):
    cli = _cli()
    now = 1_800_000_000.0
    board = {"ok": True, "agents": [
        {"id": "codex-sparks", "state": "idle", "last_seen": now - 7 * 3600, "stale": True, "doing": "x",
         "observed": {"state": "tool", "since": now - 120, "tool": {"name": "exec", "since": now - 120, "input": "pytest"},
                      "title": "Arregla el bucle", "engine": "codex", "binding": "inferred", "questions": []}},
        {"id": "builder", "state": "working", "last_seen": now - 60, "stale": False, "doing": "y", "observed": None}]}
    watch_ = {"ok": True, "agents": {"codex-sparks": board["agents"][0]["observed"]}, "questions": [
        {"agent": "codex-sparks", "engine": "codex", "kind": "approval", "what": "git push", "since": now - 600,
         "confidence": "explicit", "session_key": "codex:1", "title": "t"}],
        "unbound": [{"key": "cursor:abc", "engine": "cursor", "state": "working", "title": "sin agente", "last_activity": now - 30,
                     "since": now - 30, "tool": None}], "sessions": []}
    calls = []

    def fake_call(method, path, body=None, timeout=150.0):
        calls.append((method, path, body))
        return board if path.startswith("/api/agora/board") else (watch_ if path.startswith("/api/agora/watch?") or path == "/api/agora/watch" else {"ok": True, "session_key": "cursor:abc", "agent": "builder", "binding": "explicit"})

    monkeypatch.setattr(cli, "call", fake_call)
    monkeypatch.setattr(cli.time, "time", lambda: now)
    assert cli.main(["watch"]) == 0
    out = capsys.readouterr().out
    assert "codex-sparks" in out and "sin señales" in out and "ejecutando exec" in out and "hace 2 min" in out
    assert "Arregla el bucle" in out and "git push" in out and "cursor:abc" in out and "builder" in out
    assert cli.main(["watch-bind", "cursor:abc", "builder"]) == 0
    assert calls[-1] == ("POST", "/api/agora/watch/bind", {"session_key": "cursor:abc", "agent": "builder"})
    assert "cursor:abc" in capsys.readouterr().out
    assert cli.main(["watch-bind", "cursor:abc", "-"]) == 0
    assert calls[-1][2] == {"session_key": "cursor:abc", "agent": ""}
