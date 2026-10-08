"""The observer through the hub: GET /api/agora/watch, the bind route, the hub_agora_watch tools, the board merge, and
the hub.json switches."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hoard_link.hub import tools

from ._hub_fakes import http, make_hub, serve
from ._transcripts import (clock, cursor_file, codex_file, cur_text, cur_user, cx_exec, cx_head, cx_start, roots)  # noqa: F401


def _hub(tmp_path, roots):
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"roots": {e: str(p) for e, p in roots.items()}}
    return hub


def test_facet_routes_tools_and_board_merge(tmp_path, roots):
    now = time.time()
    codex_file(roots["codex"], cx_head(now - 300) + [cx_start(now - 200), cx_exec(now - 190, "c", "agora.py --as codex-sparks hb"),
                                                      cx_exec(now - 30, "d", "pytest -q")], now - 30)
    cursor_file(roots["cursor"], [cur_user("sin agente"), cur_text("hola")], now - 40)
    hub = _hub(tmp_path, roots)
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    try:
        token = Path(hub.config.token_file).read_text(encoding="utf-8").strip()
        hdr = {"Authorization": "Bearer " + token}
        page = {"Sec-Fetch-Site": "same-origin"}
        status, w = http(base + "/api/agora/watch")
        assert status == 200 and w["ok"] and set(w) >= {"sessions", "agents", "questions", "unbound"}
        assert w["agents"]["codex-sparks"]["state"] == "tool" and w["agents"]["codex-sparks"]["tool"]["name"] == "exec"
        assert [s["engine"] for s in w["unbound"]] == ["cursor"] and w["questions"] == []
        status, only = http(base + "/api/agora/watch?agent=codex-sparks&refresh=1")
        assert [s["agent"] for s in only["sessions"]] == ["codex-sparks"] and only["unbound"] == []

        # the board carries the observation of every agent it knows, whether the agent announced itself or not
        http(base + "/api/agora/heartbeat", {"agent": "codex-sparks", "doing": "obs", "state": "idle"}, headers=hdr)
        http(base + "/api/agora/heartbeat", {"agent": "builder", "doing": "x"}, headers=hdr)
        status, board = http(base + "/api/agora/board")
        by = {a["id"]: a for a in board["agents"]}
        assert by["codex-sparks"]["observed"]["state"] == "tool" and by["codex-sparks"]["observed"]["tool"]["name"] == "exec"
        assert by["codex-sparks"]["observed"]["title"] == "Arregla el bucle del agente" and by["builder"]["observed"] is None
        assert board["watch"] == {"questions": 0, "unbound": 1, "sessions": 2} and board["observed_only"] == {}

        # binding: no token, no page -> refused; token or page -> done
        status, err = http(base + "/api/agora/watch/bind", {"session_key": "cursor:abc-123", "agent": "builder"})
        assert status == 401
        status, ok = http(base + "/api/agora/watch/bind", {"session_key": "cursor:abc-123", "agent": "builder"}, headers=hdr)
        assert status == 200 and ok["binding"] == "explicit"
        status, bad = http(base + "/api/agora/watch/bind", {"session_key": "cursor:nope", "agent": "builder"}, headers=page)
        assert status == 404 and not bad["ok"]
        status, bad = http(base + "/api/agora/watch/bind", {"session_key": "cursor:abc-123", "agent": "luis"}, headers=page)
        assert status == 400
        status, board = http(base + "/api/agora/board")
        assert {a["id"]: a for a in board["agents"]}["builder"]["observed"]["engine"] == "cursor"
        assert json.loads(Path(hub.config.data_dir, "agent_watch.json").read_text(encoding="utf-8"))["bindings"] == {
            "cursor:abc-123": "builder"}

        # the tools
        names = {t["name"]: t for t in tools.all_tools()}
        assert {"hub_agora_watch", "hub_agora_watch_bind"} <= set(names)
        assert names["hub_agora_watch"]["annotations"]["readOnlyHint"] is True
        assert names["hub_agora_watch_bind"]["annotations"]["readOnlyHint"] is False
        for n in ("hub_agora_watch", "hub_agora_watch_bind"):
            first = names[n]["description"].split("\n")[0]
            assert len(first) <= 110 and "/" in first, first
        r = tools.call(hub, "hub_agora_watch", {"agent": "builder"})
        assert r["ok"] and list(r["agents"]) == ["builder"]
        r = tools.call(hub, "hub_agora_watch_bind", {"session_key": "cursor:abc-123", "agent": ""})
        assert r["ok"] and r["agent"] is None
        assert tools.call(hub, "hub_agora_watch_bind", {"session_key": "cursor:abc-123", "agent": "BAD ID"})["ok"] is False
        r = tools.call(hub, "hub_agora_board", {})
        assert r["ok"] and all("observed" in a for a in r["agents"])
        assert hub.facet("agora").agora.board()["ok"] and "observed" not in hub.facet("agora").agora.board()["agents"][0]
    finally:
        server.shutdown()
        hub.close()


def test_facet_can_be_disabled_and_a_broken_root_never_breaks_the_board(tmp_path, roots):
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"enabled": False}
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    try:
        status, w = http(base + "/api/agora/watch")
        assert status == 404 and not w["ok"]
        hub.facet("agora").agora.heartbeat({"agent": "builder"})
        status, board = http(base + "/api/agora/board")
        assert status == 200 and "observed" not in board["agents"][0]
    finally:
        server.shutdown()
        hub.close()
    hub = make_hub(tmp_path / "second", [])
    hub.config.agent_watch = {"roots": {"codex": str(tmp_path / "missing"), "claude": [str(tmp_path / "file.txt")]}}
    (tmp_path / "file.txt").write_text("x")
    try:
        assert hub.facet("agora").agora.heartbeat({"agent": "builder"})["ok"]
        r = tools.call(hub, "hub_agora_board", {})
        assert r["ok"] and r["agents"][0]["observed"] is None
    finally:
        hub.close()
