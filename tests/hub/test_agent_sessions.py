"""The «Sesiones de agente» facet against a real app (the shared router with its journal) and a fake one without a journal:
grouping by agent and session, undo with a dry run first, conflicts, the operator rule, tokens, the tool and the page script."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub.agent_sessions import group_sessions

from ._hub_fakes import FakeApp, http, make_hub, serve
from .conftest import free_port

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
from pydantic import BaseModel  # noqa: E402

APP_TOKEN = "n" * 40


class Add(BaseModel):
    text: str


class Edit(BaseModel):
    note_id: str
    text: str


class Empty(BaseModel):
    pass


class Notes:
    def __init__(self):
        self.notes: dict[str, str] = {}
        self.n = 0


def make_notes_app(data_dir: Path):
    from fastapi import FastAPI

    from hoard_link.agentkit import AppError, Tool, ann, call_tool, make_agent_router, tool_catalog

    ctx = Notes()

    def add(c, a):
        c.n += 1
        c.notes[f"n{c.n}"] = a.text
        return {"id": f"n{c.n}", "text": a.text}

    def add_undo(c, record):
        nid = next(i.split("=", 1)[1] for i in record["ids"] if i.startswith("id="))
        if c.notes.get(nid) != record["etag"]:
            raise AppError("conflict", "The note changed.")
        del c.notes[nid]
        return {"removed": nid}

    def edit(c, a):
        c.notes[a.note_id] = a.text
        return {"id": a.note_id, "text": a.text}

    tools_ = [Tool("note_list", "List.", Empty, ann(True), lambda c, a: {"notes": dict(c.notes)}),
              Tool("note_add", "Add.", Add, ann(False, draft_safe=True), add, undo=add_undo,
                   track=lambda a, r: {"objects": [f"note:{r['id']}"], "etag": r["text"]}),
              Tool("note_edit", "Edit.", Edit, ann(False), edit, track=lambda a, r: {"objects": [f"note:{a.note_id}"], "etag": r["text"]})]
    app = FastAPI()
    app.include_router(make_agent_router(tools_fn=lambda: tool_catalog(tools_), call_fn=lambda n, a: call_tool(tools_, ctx, n, a),
                                         token_fn=lambda: APP_TOKEN, instructions="Notes.", app_name="notes", reasons=True,
                                         data_dir=data_dir, tools=tools_, ctx_fn=lambda: ctx))
    return app, ctx


class Real:
    def __init__(self, data_dir: Path):
        import uvicorn

        data_dir.mkdir(parents=True, exist_ok=True)
        self.app, self.ctx = make_notes_app(data_dir)
        self.app_id = "notes"
        self.token = APP_TOKEN
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)

    def call(self, name, args, *, agent, session, reason="Because the test says so"):
        status, body = http(f"http://127.0.0.1:{self.port}/api/agent/call", {"name": name, "arguments": args, "reason": reason},
                            headers={"Authorization": f"Bearer {APP_TOKEN}", "X-Agent-Id": agent, "X-Agent-Session": session})
        assert status == 200, body
        return body

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def family(tmp_path):
    data_dir = tmp_path / "apps" / "Notes's Hoard" / "data"
    real = Real(data_dir)
    bare = FakeApp("plain", tools=[], handlers={})
    hub = make_hub(tmp_path, [real, bare], extra_apps=["ghost"])          # ghost: registered but not running
    server = serve(hub)
    auth = {"Authorization": f"Bearer {hub.token}"}
    yield hub, real, hub.config.url, auth
    server.shutdown()
    real.stop()
    bare.stop()


def test_group_sessions_is_pure():
    rows = [{"kind": "write", "id": "1", "ts": 10, "tool": "a", "agent": "x", "session": "s", "reason": "first", "ok": True, "undoable": True},
            {"kind": "write", "id": "2", "ts": 20, "tool": "a", "agent": "x", "session": "s", "reason": "first", "ok": False, "error": "boom"},
            {"kind": "write", "id": "3", "ts": 30, "tool": "b", "agent": "x", "session": "s", "reason": "second", "ok": True, "undone": True},
            {"kind": "undo", "id": "4", "ts": 40, "undoes": "3", "ok": True},
            {"kind": "write", "id": "5", "ts": 5, "tool": "a", "agent": "", "session": "", "ok": True}]
    groups = group_sessions("notes", "Notes", rows)
    assert [g["key"] for g in groups] == ["notes|x|s", "notes||"]                      # newest session first
    g = groups[0]
    assert (g["writes"], g["failed"], g["undone"], g["undoable"]) == (3, 1, 1, 1) and g["tools"] == {"a": 2, "b": 1}
    assert g["reasons"] == ["first", "second"] and g["can_undo"] is True and g["first_ts"] == 10 and g["last_ts"] == 30
    assert groups[1]["can_undo"] is False                                                # no session id: it cannot be undone together


def test_overview_groups_by_agent_and_session_and_lists_the_apps(family):
    hub, real, url, auth = family
    real.call("note_add", {"text": "one"}, agent="codex-sparks", session="run-1")
    real.call("note_add", {"text": "two"}, agent="codex-sparks", session="run-2")
    real.call("note_add", {"text": "three"}, agent="cursor", session="chat-9")
    code, res = http(url + "/api/agent-sessions?refresh=1", headers=auth)
    assert code == 200 and res["ok"] and res["adopted"] == 1
    states = {a["id"]: a["state"] for a in res["apps"]}
    assert states == {"notes": "ok", "plain": "not_adopted", "ghost": "down"}
    assert {(s["agent"], s["session"], s["writes"], s["undoable"]) for s in res["sessions"]} == \
        {("codex-sparks", "run-1", 1, 1), ("codex-sparks", "run-2", 1, 1), ("cursor", "chat-9", 1, 1)}
    by_agent = {a["agent"]: a for a in res["agents"]}
    assert len(by_agent["codex-sparks"]["sessions"]) == 2 and by_agent["cursor"]["writes"] == 1
    s = next(x for x in res["sessions"] if x["session"] == "run-1")
    assert s["app"] == "notes" and s["reasons"] == ["Because the test says so"] and s["entries"][0]["tool"] == "note_add" and s["can_undo"]


def test_session_detail(family):
    hub, real, url, auth = family
    real.call("note_add", {"text": "one"}, agent="a", session="s1")
    real.call("note_add", {"text": "other"}, agent="a", session="s2")
    code, res = http(url + "/api/agent-sessions/session?app=notes&session=s1", headers=auth)
    assert code == 200 and [e["session"] for e in res["entries"]] == ["s1"]
    assert http(url + "/api/agent-sessions/session?app=nope&session=s1", headers=auth)[0] == 404
    assert http(url + "/api/agent-sessions/session?app=ghost&session=s1", headers=auth)[0] == 503


def test_undo_asks_for_a_dry_run_then_does_it(family):
    hub, real, url, auth = family
    real.call("note_add", {"text": "one"}, agent="a", session="s1")
    real.call("note_add", {"text": "two"}, agent="a", session="s1")
    real.call("note_add", {"text": "keep"}, agent="b", session="s2")
    code, plan = http(url + "/api/agent-sessions/undo", {"app": "notes", "session": "s1", "agent": "a"}, headers=auth)       # dry run by default
    assert code == 200 and plan["dry_run"] is True and [u["tool"] for u in plan["would_undo"]] == ["note_add", "note_add"]
    assert len(real.ctx.notes) == 3
    code, res = http(url + "/api/agent-sessions/undo", {"app": "notes", "session": "s1", "agent": "a", "dry_run": False, "confirm": True,
                                                         "reason": "The person pressed undo"}, headers=auth)
    assert code == 200 and res["ok"] and res["complete"] and len(res["undone"]) == 2
    assert real.ctx.notes == {"n3": "keep"}
    code, over = http(url + "/api/agent-sessions?refresh=1", headers=auth)
    s1 = next(s for s in over["sessions"] if s["session"] == "s1")
    assert s1["undone"] == 2 and s1["undoable"] == 0 and s1["can_undo"] is False
    # without confirm=true the app refuses, and the hub says so
    code, res = http(url + "/api/agent-sessions/undo", {"app": "notes", "session": "s2", "dry_run": False}, headers=auth)
    assert code == 400 and res["ok"] is False and res["code"] == "confirm_required"
    assert real.ctx.notes == {"n3": "keep"}


def test_undo_reports_what_somebody_else_touched(family):
    hub, real, url, auth = family
    real.call("note_add", {"text": "mine"}, agent="a", session="s1")
    real.call("note_edit", {"note_id": "n1", "text": "theirs"}, agent="b", session="s2")
    code, plan = http(url + "/api/agent-sessions/undo", {"app": "notes", "session": "s1"}, headers=auth)
    assert code == 200 and plan["would_undo"] == [] and plan["complete"] is False
    assert plan["conflicts"][0]["with"]["agent"] == "b" and plan["conflicts"][0]["with"]["session"] == "s2"


def test_undo_errors(family):
    hub, real, url, auth = family
    assert http(url + "/api/agent-sessions/undo", {"app": "nope", "session": "s"}, headers=auth)[0] == 404
    assert http(url + "/api/agent-sessions/undo", {"app": "notes", "session": ""}, headers=auth)[0] == 400
    code, res = http(url + "/api/agent-sessions/undo", {"app": "notes", "session": "ghost-session"}, headers=auth)
    assert code == 404 and res["code"] == "session_not_found"
    assert http(url + "/api/agent-sessions/undo", {"app": "ghost", "session": "s"}, headers=auth)[0] == 503
    assert http(url + "/api/agent-sessions/undo", {"app": "plain", "session": "s"}, headers=auth)[0] == 404         # no journal there


def test_only_the_operator_reads_journals_and_undoes(family):
    hub, real, url, auth = family
    real.call("note_add", {"text": "one"}, agent="a", session="s1")
    assert http(url + "/api/agent-sessions")[0] == 403
    assert http(url + "/api/agent-sessions", headers={"Authorization": "Bearer nope"})[0] == 403
    assert http(url + "/api/agent-sessions/undo", {"app": "notes", "session": "s1", "dry_run": False, "confirm": True}, )[0] == 403
    # another app's own token is a family token but not the operator's
    plain_token = next(a for a in hub.apps if a.id == "plain")
    other = {"Authorization": "Bearer token-plain"}
    assert http(url + "/api/agent-sessions", headers=other)[0] == 403
    assert http(url + "/api/agent-sessions/tokens", {"app": "notes", "agent": "x"}, headers=other)[0] == 403
    assert len(real.ctx.notes) == 1


def test_mint_list_and_revoke_tokens_per_app(family):
    hub, real, url, auth = family
    code, minted = http(url + "/api/agent-sessions/tokens", {"app": "notes", "agent": "codex-sparks", "profile": "drafts", "label": "laptop"}, headers=auth)
    assert code == 200 and minted["token"].startswith("hat_") and minted["profile"] == "drafts"
    # the new token really limits the agent in the app
    call = lambda name, args, tok: http(f"http://127.0.0.1:{real.port}/api/agent/call",  # noqa: E731
                                        {"name": name, "arguments": args, "reason": "testing the profile"}, headers={"Authorization": f"Bearer {tok}"})
    assert call("note_add", {"text": "draft"}, minted["token"])[0] == 200
    assert call("note_edit", {"note_id": "n1", "text": "x"}, minted["token"])[0] == 403
    code, listed = http(url + "/api/agent-sessions/tokens", headers=auth)
    notes = next(a for a in listed["apps"] if a["app"] == "notes")
    assert [(t["agent"], t["profile"], t["label"]) for t in notes["tokens"]] == [("codex-sparks", "drafts", "laptop")]
    assert minted["token"] not in str(listed)
    assert next(a for a in listed["apps"] if a["app"] == "plain")["state"] == "not_adopted"
    assert http(url + "/api/agent-sessions/tokens", {"app": "notes", "agent": "x y", "profile": "all"}, headers=auth)[0] == 400
    assert http(url + "/api/agent-sessions/tokens", {"app": "notes", "agent": "x", "profile": "root"}, headers=auth)[0] == 400
    code, gone = http(url + "/api/agent-sessions/tokens/revoke", {"app": "notes", "id": minted["id"]}, headers=auth)
    assert code == 200 and gone["revoked"] == 1
    assert call("note_add", {"text": "again"}, minted["token"])[0] == 401
    assert http(url + "/api/agent-sessions/tokens/revoke", {"app": "notes", "id": minted["id"]}, headers=auth)[0] == 404


def test_the_agent_tool_is_read_only_and_filters(family):
    hub, real, url, auth = family
    real.call("note_add", {"text": "one"}, agent="a", session="s1")
    real.call("note_add", {"text": "two"}, agent="b", session="s2")
    spec = next(t for t in tools.all_tools() if t["name"] == "hub_agent_sessions")
    assert spec["annotations"]["readOnlyHint"] is True
    assert not any("undo" in t["name"] for t in tools.all_tools() if t["name"].startswith("hub_agent_session"))
    out = tools.call(hub, "hub_agent_sessions", {"agent": "b", "refresh": True})
    assert [s["session"] for s in out["sessions"]] == ["s2"]
    detail = tools.call(hub, "hub_agent_sessions", {"app": "notes", "session": "s1"})
    assert [e["tool"] for e in detail["entries"]] == ["note_add"]


def test_the_page_script_is_served_and_listed(family):
    hub, real, url, auth = family
    code, facets = http(url + "/api/facets")
    mine = next(f for f in facets["facets"] if f["id"] == "agent_sessions")
    assert mine["ui_scripts"] == ["agent_sessions.js"]
    import urllib.request
    with urllib.request.urlopen(url + "/ui/agent_sessions.js", timeout=10) as resp:
        text = resp.read().decode("utf-8")
    assert 'H.register("agent_sessions"' in text and "Deshacer sesión" in text and "Sesiones de agente" in text
    assert "innerHTML" not in text                                                    # every string goes in through textContent
    node = shutil.which("node")
    if node:
        path = Path(__file__).resolve().parents[2] / "hoard_link" / "hub" / "ui" / "agent_sessions.js"
        assert subprocess.run([node, "--check", str(path)], capture_output=True).returncode == 0
