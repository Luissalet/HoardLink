"""Accountable agents: identity headers, mandatory reasons, the write journal (masking, rotation), undo of a whole session
(order, dry run, conflicts), and the per-agent tokens with their profiles, through ``agentkit.make_agent_router``."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hoard_link import agent_journal, agentkit, family, tokens
from hoard_link.agent_journal import Journal, digest_args, mask_secrets, summarize_args
from hoard_link.agentkit import AppError, Tool, ann, call_tool, tool_catalog

pydantic = pytest.importorskip("pydantic")
fastapi = pytest.importorskip("fastapi")
from pydantic import BaseModel, Field  # noqa: E402

MAIN = "m" * 40


# ------------------------------------------------------------------------------------------------ a tiny app

class Notes:
    """The app's state: notes (text), folders (names). ``clock`` makes the 'version' of a note observable."""

    def __init__(self):
        self.notes: dict[str, dict] = {}
        self.folders: dict[str, str] = {"f1": "Inbox"}
        self.n = 0
        self.secrets: list = []

    def next_id(self):
        self.n += 1
        return f"n{self.n}"


def etag_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


class NoteAdd(BaseModel):
    text: str = Field(..., min_length=1)
    folder: str = ""


class NoteEdit(BaseModel):
    note_id: str
    text: str


class NoteRef(BaseModel):
    note_id: str


class FolderRename(BaseModel):
    folder_id: str
    name: str


class SecretSet(BaseModel):
    service: str
    password: str = ""
    note: str = ""


class NoteWithReason(BaseModel):
    note_id: str
    reason: str = ""


def add_run(ctx, a):
    nid = ctx.next_id()
    ctx.notes[nid] = {"text": a.text, "folder": a.folder}
    return {"id": nid, "text": a.text}


def add_track(args, result):
    path = f"folder:{args.folder}/note:{result['id']}" if args.folder else f"note:{result['id']}"
    return {"objects": [path], "etag": etag_of(result["text"])}


def add_undo(ctx, record):
    nid = next(i.split("=", 1)[1] for i in record["ids"] if i.startswith("id="))
    if nid not in ctx.notes:
        raise AppError("conflict", "The note is already gone.")
    if etag_of(ctx.notes[nid]["text"]) != record["etag"]:
        raise AppError("conflict", "The note was edited after the agent added it.")
    del ctx.notes[nid]
    return {"removed": nid}


def edit_run(ctx, a):
    if a.note_id not in ctx.notes:
        raise KeyError(f"no note {a.note_id}")
    ctx.notes[a.note_id]["text"] = a.text
    return {"id": a.note_id, "text": a.text}


def edit_capture(ctx, a):
    return {"text": ctx.notes[a.note_id]["text"]}


def edit_track(args, result):
    return {"objects": [f"note:{args.note_id}"], "etag": etag_of(result["text"])}


def edit_undo(ctx, record, dry_run=False):
    nid = record["objects"][0].split(":", 1)[1]
    if nid not in ctx.notes or etag_of(ctx.notes[nid]["text"]) != record["etag"]:
        raise AppError("conflict", "The note changed since the agent edited it.")
    if dry_run:
        return {"would_restore": record["before"]["text"]}
    ctx.notes[nid]["text"] = record["before"]["text"]
    return {"restored": record["before"]["text"]}


def delete_run(ctx, a):
    ctx.notes.pop(a.note_id, None)
    return {"deleted": a.note_id}


def list_run(ctx, a):
    return {"notes": {k: v["text"] for k, v in ctx.notes.items()}}


def rename_run(ctx, a):
    ctx.folders[a.folder_id] = a.name
    return {"id": a.folder_id}


def rename_track(args, result):
    return {"objects": [f"folder:{args.folder_id}"]}


def secret_run(ctx, a):
    ctx.secrets.append(a.service)
    return {"stored": True, "service_id": a.service}


def own_reason_run(ctx, a):
    return {"id": a.note_id, "reason_seen": a.reason}


class Empty(BaseModel):
    pass


TOOLS = [
    Tool("note_list", "List notes.", Empty, ann(True), list_run),
    Tool("note_add", "Add a note.", NoteAdd, ann(False, False, False, draft_safe=True), add_run, track=add_track, undo=add_undo),
    Tool("note_edit", "Edit a note.", NoteEdit, ann(False, False, True), edit_run, capture=edit_capture, track=edit_track, undo=edit_undo),
    Tool("note_delete", "Delete a note.", NoteRef, ann(False, True, True), delete_run),
    Tool("folder_rename", "Rename a folder.", FolderRename, ann(False, False, True), rename_run, track=rename_track),
    Tool("secret_set", "Store a secret.", SecretSet, ann(False, False, True, draft_safe=True), secret_run),
    Tool("note_flag", "Flag a note (its own reason field).", NoteWithReason, ann(False), own_reason_run),
]


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(family, "emit", lambda type, data=None, **kw: seen.append((type, dict(data or {}))) or True)
    return seen


def build(tmp_path, **overrides):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    ctx = Notes()
    tools = overrides.pop("tools", TOOLS)
    router_tools = overrides.pop("router_tools", tools)
    kwargs = dict(tools_fn=lambda: tool_catalog(tools), call_fn=lambda name, args: call_tool(tools, ctx, name, args),
                  token_fn=lambda: MAIN, instructions="Notes.", app_name="notes", reasons=True, data_dir=tmp_path, tools=router_tools,
                  ctx_fn=lambda: ctx)
    kwargs.update(overrides)
    app.include_router(agentkit.make_agent_router(**kwargs))
    client = TestClient(app, raise_server_exceptions=False)
    client.ctx = ctx
    return client


def call(client, name, arguments=None, *, token=MAIN, agent=None, session=None, reason="Because the person asked", body_extra=None, **headers):
    h = {"Authorization": f"Bearer {token}"}
    if agent is not None:
        h["X-Agent-Id"] = agent
    if session is not None:
        h["X-Agent-Session"] = session
    h.update(headers)
    body = {"name": name, "arguments": arguments or {}}
    if reason is not None:
        body["reason"] = reason
    body.update(body_extra or {})
    return client.post("/api/agent/call", json=body, headers=h)


def undo(client, session, *, token=MAIN, **body):
    return client.post("/api/agent/undo", json={"session": session, **body}, headers={"Authorization": f"Bearer {token}"})


def journal(client, token=MAIN, **params):
    return client.get("/api/agent/journal", params=params, headers={"Authorization": f"Bearer {token}"})


# ------------------------------------------------------------------------------------------------ identity

def test_headers_carry_the_agent_and_the_session(tmp_path, events):
    c = build(tmp_path)
    assert call(c, "note_add", {"text": "a"}, agent="codex-sparks", session="run-1").status_code == 200
    row = journal(c).json()["entries"][0]
    assert (row["agent"], row["session"], row["tool"], row["ok"]) == ("codex-sparks", "run-1", "note_add", True)
    assert row["app"] == "notes" and row["reason"] == "Because the person asked" and row["profile"] == "all"
    audit = [d for t, d in events if t == "agent.call"][-1]
    assert audit["caller"] == "codex-sparks" and audit["session"] == "run-1"


def test_body_fields_work_too_and_headers_win(tmp_path, events):
    c = build(tmp_path)
    call(c, "note_add", {"text": "a"}, body_extra={"caller": "cursor", "_session": "s-body"})
    call(c, "note_add", {"text": "b"}, agent="from-header", session="s-header", body_extra={"caller": "cursor", "_session": "s-body"})
    call(c, "note_add", {"text": "c"}, body_extra={"agent": "claude-ish", "session": "s-plain"})
    rows = journal(c).json()["entries"]
    assert [(r["agent"], r["session"]) for r in rows] == [("cursor", "s-body"), ("from-header", "s-header"), ("claude-ish", "s-plain")]


def test_identity_is_cleaned_and_bounded(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "a"}, agent="  spaced\x07 ", session="s" * 500)
    row = journal(c).json()["entries"][0]
    assert row["agent"] == "spaced" and len(row["session"]) == 120


def test_a_scoped_token_fixes_the_agent(tmp_path):
    c = build(tmp_path)
    minted = tokens.mint_agent_token(tmp_path, "agent-a", "all")
    assert call(c, "note_add", {"text": "x"}, token=minted["token"], agent="someone-else", session="s").status_code == 200
    row = journal(c).json()["entries"][0]
    assert row["agent"] == "agent-a" and row["token"] == minted["id"]


# ------------------------------------------------------------------------------------------------ reasons

def test_a_write_without_a_reason_is_refused(tmp_path):
    c = build(tmp_path)
    r = call(c, "note_add", {"text": "a"}, reason=None)
    assert r.status_code == 400 and r.json()["code"] == "reason_required" and "reason" in r.json()["hint"]
    assert c.ctx.notes == {}                                                      # nothing ran
    for bad in ("", "  ", "ab", "x" * 301):
        assert call(c, "note_add", {"text": "a"}, reason=bad).json()["code"] == "reason_required"
    assert journal(c).json()["entries"] == []                                     # refused calls are not writes


def test_reads_never_need_a_reason(tmp_path):
    c = build(tmp_path)
    assert call(c, "note_list", reason=None).status_code == 200


def test_the_reason_may_come_in_the_arguments_and_is_not_passed_on(tmp_path):
    c = build(tmp_path)
    r = call(c, "note_add", {"text": "a", "reason": "Arguments carry it"}, reason=None)
    assert r.status_code == 200
    assert journal(c).json()["entries"][0]["reason"] == "Arguments carry it"


def test_a_tool_with_its_own_reason_field_keeps_it(tmp_path):
    c = build(tmp_path)
    r = call(c, "note_flag", {"note_id": "n1", "reason": "my own field"}, reason=None)
    assert r.status_code == 200 and r.json()["reason_seen"] == "my own field"


def test_the_catalogue_asks_for_reason_on_writes_only(tmp_path):
    c = build(tmp_path)
    body = c.get("/api/agent/tools").json()
    by = {t["name"]: t for t in body["tools"]}
    assert body["reasons_required"] is True and "reason" in body["instructions"]
    assert "reason" in by["note_add"]["inputSchema"]["required"]
    assert by["note_add"]["inputSchema"]["properties"]["reason"]["minLength"] == 3
    assert "reason" not in by["note_list"]["inputSchema"].get("required", []) and "reason" not in by["note_list"]["inputSchema"].get("properties", {})
    assert by["note_add"]["annotations"]["draftSafeHint"] is True and "draftSafeHint" not in by["note_edit"]["annotations"]


def test_reasons_are_off_by_default(tmp_path):
    c = build(tmp_path, reasons=False)
    assert call(c, "note_add", {"text": "a"}, reason=None).status_code == 200
    assert "reasons_required" not in c.get("/api/agent/tools").json()
    assert "reason" not in c.get("/api/agent/tools").json()["tools"][1]["inputSchema"].get("properties", {})
    assert call(c, "note_add", {"text": "b"}, reason="given anyway").status_code == 200
    assert [r["reason"] for r in journal(c).json()["entries"]] == ["", "given anyway"]


def test_an_app_without_a_data_dir_behaves_as_before(tmp_path):
    c = build(tmp_path, data_dir=None, reasons=False, router_tools=None)
    assert call(c, "note_add", {"text": "a"}, reason=None).status_code == 200
    assert journal(c).status_code == 404 and journal(c).json()["code"] == "journal_unavailable"
    assert undo(c, "s", confirm=True).status_code == 404
    assert not (tmp_path / "agent_journal.jsonl").exists()


# ------------------------------------------------------------------------------------------------ journal

def test_the_journal_line(tmp_path, events):
    c = build(tmp_path)
    call(c, "note_add", {"text": "hello", "folder": "f1"}, agent="a1", session="s1")
    line = json.loads((tmp_path / "agent_journal.jsonl").read_text().splitlines()[0])
    assert line["kind"] == "write" and line["tool"] == "note_add" and line["ok"] is True and line["undoable"] is True
    assert line["ids"] == ["id=n1"] and line["objects"] == ["folder:f1/note:n1"] and line["etag"] == etag_of("hello")
    assert line["args_digest"].startswith("sha256:") and len(line["args_digest"]) == 23 and isinstance(line["ts"], float)
    assert json.loads(line["args_summary"]) == {"folder": "f1", "text": "hello"} and line["ms"] >= 0
    write = [d for t, d in events if t == "agent.write"][0]
    assert write["tool"] == "note_add" and write["agent"] == "a1" and write["session"] == "s1" and write["journal_id"] == line["id"]
    assert write["objects"] == ["folder:f1/note:n1"] and "args" not in write and "hello" not in json.dumps(write)


def test_a_failed_write_is_journalled_as_failed(tmp_path):
    c = build(tmp_path)
    r = call(c, "note_edit", {"note_id": "ghost", "text": "x"}, agent="a", session="s")
    assert r.status_code == 404
    row = journal(c).json()["entries"][0]
    assert row["ok"] is False and "ghost" in row["error"] and row["undoable"] is False
    r = call(c, "note_add", {"text": ""}, agent="a", session="s")                 # invalid arguments
    assert r.status_code == 400 and journal(c).json()["entries"][-1]["ok"] is False


def test_reads_are_not_journalled(tmp_path):
    c = build(tmp_path)
    call(c, "note_list", agent="a", session="s", reason=None)
    assert journal(c).json()["entries"] == []


def test_secrets_are_masked_in_the_summary_and_the_digest(tmp_path):
    c = build(tmp_path)
    call(c, "secret_set", {"service": "mail", "password": "hunter2-very-secret", "note": "see https://bob:pw123@example.com/x and Bearer abcdefghijklmnop"},
         agent="a", session="s")
    text = (tmp_path / "agent_journal.jsonl").read_text()
    for leaked in ("hunter2", "pw123", "abcdefghijklmnop"):
        assert leaked not in text
    row = journal(c).json()["entries"][0]
    assert json.loads(row["args_summary"])["password"] == "***"
    assert digest_args({"password": "one", "x": 1}) == digest_args({"password": "two", "x": 1}) != digest_args({"password": "one", "x": 2})


def test_masking_helpers():
    masked = mask_secrets({"api_key": "k", "Authorization": "Bearer abcdef123456", "nested": {"client_secret": "s", "ok": "fine"},
                           "items": [{"token": "t"}], "empty_password": "", "text": "password=hunter22 and sk-abcdefghijklmnopqr and ghp_" + "a" * 30})
    assert masked["api_key"] == "***" and masked["Authorization"] == "***" and masked["nested"] == {"client_secret": "***", "ok": "fine"}
    assert masked["items"] == [{"token": "***"}] and masked["empty_password"] == ""
    assert "hunter22" not in masked["text"] and "sk-abc" not in masked["text"] and "ghp_" not in masked["text"]
    assert mask_secrets({"author": "Ada", "tokens_id": "x"})["author"] == "Ada"
    long_hex = "a1" * 30
    assert mask_secrets({"v": f"id {long_hex}"})["v"] == "id ***"
    assert mask_secrets({"v": "01JABCDEFGHJKMNPQRSTVWXYZ0"})["v"] == "01JABCDEFGHJKMNPQRSTVWXYZ0"       # a ULID is an id, not a secret


def test_the_summary_is_short_and_the_ids_are_found():
    summary = summarize_args({"text": "x" * 5000, "items": list(range(100)), "deep": {"a": {"b": "y" * 500}}})
    assert len(summary) <= 300
    assert agent_journal.result_ids({"id": "d1", "slide_id": 7, "slide_ids": ["a", "b"], "title": "t", "flag_id": True}) == \
        ["id=d1", "slide_id=7", "slide_ids=a", "slide_ids=b"]
    assert agent_journal.default_objects({"deck_id": "D", "x": 1}, {"slide_id": "S"}) == ["deck:D", "slide:S"]


def test_object_paths_overlap_by_ancestry():
    ov = agent_journal.objects_overlap
    assert ov(["deck:A"], ["deck:A/slide:B"]) and ov(["deck:A/slide:B"], ["deck:A"]) and ov(["a"], ["x", "a"])
    assert not ov(["deck:A/slide:B"], ["deck:A/slide:C"]) and not ov(["deck:A"], ["deck:AB"]) and not ov([], ["a"])


def test_journal_queries(tmp_path):
    c = build(tmp_path)
    for i, (agent, session) in enumerate([("a1", "s1"), ("a1", "s2"), ("a2", "s3"), ("a1", "s1")]):
        call(c, "note_add", {"text": f"t{i}"}, agent=agent, session=session)
    assert len(journal(c).json()["entries"]) == 4
    assert [r["args_summary"] for r in journal(c, session="s1").json()["entries"]] == ['{"text":"t0"}', '{"text":"t3"}']
    assert len(journal(c, agent="a1").json()["entries"]) == 3
    assert len(journal(c, agent="a1", session="s2").json()["entries"]) == 1
    assert [r["args_summary"] for r in journal(c, limit=2).json()["entries"]] == ['{"text":"t2"}', '{"text":"t3"}']      # the last two
    rows = journal(c).json()["entries"]
    assert all("before" not in r for r in rows)
    assert journal(c).json()["undo_tools"] == ["note_add", "note_edit"]
    assert journal(c, token="wrong").status_code == 401
    assert c.get("/api/agent/journal").status_code == 401


def test_the_snapshot_is_served_only_on_request(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "a"}, session="s")
    call(c, "note_edit", {"note_id": "n1", "text": "b"}, session="s")
    rows = journal(c).json()["entries"]
    assert rows[1]["has_snapshot"] is True and "before" not in rows[1]
    assert journal(c, full=True).json()["entries"][1]["before"] == {"text": "a"}


def test_rotation_keeps_three_older_files(tmp_path):
    j = Journal(tmp_path, max_bytes=600, app="demo")
    for i in range(40):
        j.append({"kind": "write", "tool": "t", "session": "s", "agent": "a", "i": i, "pad": "x" * 60})
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["agent_journal.jsonl", "agent_journal.jsonl.1", "agent_journal.jsonl.2", "agent_journal.jsonl.3"]
    assert all(p.stat().st_size <= 800 for p in tmp_path.iterdir())
    rows = list(j.entries())
    assert [r["i"] for r in rows] == sorted(r["i"] for r in rows) and rows[-1]["i"] == 39 and len(rows) < 40      # oldest dropped, order kept
    assert j.query(limit=5)[-1]["i"] == 39 and all(r["app"] == "demo" for r in rows)


def test_a_corrupt_line_is_skipped(tmp_path):
    j = Journal(tmp_path)
    j.append({"kind": "write", "tool": "t", "n": 1})
    with open(j.path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    j.append({"kind": "write", "tool": "t", "n": 2})
    assert [r["n"] for r in j.entries()] == [1, 2]


def test_a_huge_snapshot_makes_the_write_not_undoable(tmp_path):
    j = Journal(tmp_path)
    row = j.append({"kind": "write", "tool": "t", "undoable": True, "before": {"blob": "x" * (300 * 1024)}})
    assert row["undoable"] is False and row["before"] is None and row["not_undoable_reason"] == "snapshot_too_large"


# ------------------------------------------------------------------------------------------------ undo

def test_undo_replays_in_reverse_order(tmp_path, events):
    c = build(tmp_path)
    call(c, "note_add", {"text": "v1"}, agent="a", session="s")                                  # n1
    call(c, "note_edit", {"note_id": "n1", "text": "v2"}, agent="a", session="s")
    call(c, "note_edit", {"note_id": "n1", "text": "v3"}, agent="a", session="s")
    r = undo(c, "s", confirm=True, reason="The person asked to roll back")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [u["tool"] for u in body["undone"]] == ["note_edit", "note_edit", "note_add"]          # newest first
    assert c.ctx.notes == {} and body["complete"] is True and body["counts"]["undone"] == 3
    undos = [x for x in journal(c, kind="undo").json()["entries"]]
    assert len(undos) == 3 and all(u["ok"] and u["session"] == "s" and u["agent"] == "a" and u["reason"] == "The person asked to roll back" for u in undos)
    assert [e for t, e in events if t == "agent.undo"][0]["undone"] == 3
    assert all(w["undone"] for w in journal(c, kind="write").json()["entries"])
    again = undo(c, "s", confirm=True, reason="Once more please").json()
    assert again["undone"] == [] and len(again["already_undone"]) == 3 and again["complete"] is True


def test_edit_restores_the_previous_text(tmp_path):
    c = build(tmp_path)
    c.ctx.notes["n9"] = {"text": "original", "folder": ""}
    call(c, "note_edit", {"note_id": "n9", "text": "changed"}, agent="a", session="s")
    assert undo(c, "s", confirm=True, reason="restore please").json()["undone"][0]["detail"] == {"restored": "original"}
    assert c.ctx.notes["n9"]["text"] == "original"


def test_dry_run_changes_nothing_and_records_nothing(tmp_path):
    c = build(tmp_path)
    c.ctx.notes["n9"] = {"text": "original", "folder": ""}
    call(c, "note_edit", {"note_id": "n9", "text": "changed"}, session="s")
    call(c, "note_add", {"text": "new"}, session="s")
    before = (tmp_path / "agent_journal.jsonl").read_text()
    r = undo(c, "s", dry_run=True).json()
    assert r["dry_run"] is True and r["undone"] == [] and [w["tool"] for w in r["would_undo"]] == ["note_add", "note_edit"]
    assert r["would_undo"][1]["detail"] == {"would_restore": "original"}                        # the handler's own dry run
    assert c.ctx.notes["n9"]["text"] == "changed" and (tmp_path / "agent_journal.jsonl").read_text() == before


def test_undo_needs_confirmation_and_a_known_session(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "a"}, session="s")
    r = undo(c, "s")
    assert r.status_code == 400 and r.json()["code"] == "confirm_required" and "dry_run" in r.json()["hint"]
    assert c.ctx.notes
    assert undo(c, "ghost", confirm=True, reason="no such session").status_code == 404
    assert undo(c, "ghost", confirm=True, reason="no such session").json()["code"] == "session_not_found"
    assert undo(c, "s", confirm=True).json()["code"] == "reason_required"                         # reasons are on: undo needs one too
    assert undo(c, "s", dry_run=True).status_code == 200                                         # a dry run does not
    assert undo(c, "s", token="wrong", confirm=True).status_code == 401


def test_other_sessions_are_never_touched(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "mine"}, agent="a", session="s1")
    call(c, "note_add", {"text": "theirs"}, agent="b", session="s2")
    undo(c, "s1", confirm=True, reason="roll back mine")
    assert list(c.ctx.notes) == ["n2"]
    assert undo(c, "s1", agent="b", confirm=True, reason="wrong agent").status_code == 404      # agent narrows the session


def test_a_later_write_by_another_session_is_a_conflict(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "v1"}, agent="a", session="s1")
    call(c, "note_edit", {"note_id": "n1", "text": "other agent's text"}, agent="b", session="s2")
    r = undo(c, "s1", confirm=True, reason="roll back s1").json()
    assert r["undone"] == [] and r["complete"] is False
    conflict = r["conflicts"][0]
    assert conflict["tool"] == "note_add" and conflict["reason"] == "later_write_by_other_session"
    assert conflict["with"]["agent"] == "b" and conflict["with"]["session"] == "s2" and conflict["with"]["tool"] == "note_edit"
    assert c.ctx.notes["n1"]["text"] == "other agent's text"                                    # untouched
    assert journal(c, kind="undo").json()["entries"] == []                                       # a conflict is not an undo
    # once the other session is itself undone, the first can go
    assert undo(c, "s2", confirm=True, reason="roll back s2").json()["undone"]
    assert undo(c, "s1", confirm=True, reason="roll back s1").json()["undone"][0]["tool"] == "note_add"


def test_a_conflict_in_one_write_does_not_stop_the_others(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "one"}, agent="a", session="s1")                              # n1
    call(c, "note_add", {"text": "two"}, agent="a", session="s1")                               # n2
    call(c, "note_edit", {"note_id": "n2", "text": "edited by b"}, agent="b", session="s2")
    r = undo(c, "s1", confirm=True, reason="roll back s1").json()
    assert [u["tool"] for u in r["undone"]] == ["note_add"] and len(r["conflicts"]) == 1
    assert list(c.ctx.notes) == ["n2"] and r["complete"] is False


def test_a_folder_rename_conflicts_with_a_note_added_inside_it(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "in folder", "folder": "f1"}, agent="a", session="s1")
    call(c, "folder_rename", {"folder_id": "f1", "name": "Work"}, agent="b", session="s2")
    r = undo(c, "s1", dry_run=True).json()
    assert r["would_undo"] == [] and r["conflicts"][0]["with"]["tool"] == "folder_rename"       # an ancestor path counts


def test_a_change_the_journal_never_saw_is_caught_by_the_handlers_version_check(tmp_path):
    c = build(tmp_path)
    call(c, "note_add", {"text": "v1"}, agent="a", session="s")
    c.ctx.notes["n1"]["text"] = "edited in the app's own UI"
    r = undo(c, "s", confirm=True, reason="roll back").json()
    assert r["undone"] == [] and r["conflicts"][0]["reason"] == "changed_since" and "edited" in r["conflicts"][0]["message"]
    assert "n1" in c.ctx.notes
    failed = journal(c, kind="undo").json()["entries"][0]
    assert failed["ok"] is False and failed["undoes"]


def test_writes_without_a_handler_are_reported_not_undoable(tmp_path):
    c = build(tmp_path)
    c.ctx.notes["n7"] = {"text": "t", "folder": ""}
    call(c, "note_delete", {"note_id": "n7"}, session="s")
    call(c, "folder_rename", {"folder_id": "f1", "name": "X"}, session="s")
    call(c, "note_add", {"text": "a"}, session="s")
    r = undo(c, "s", confirm=True, reason="roll back").json()
    assert [u["tool"] for u in r["undone"]] == ["note_add"]
    assert sorted((x["tool"], x["reason"]) for x in r["not_undoable"]) == [("folder_rename", "no_handler"), ("note_delete", "no_handler")]
    assert r["complete"] is False


def test_failed_writes_are_ignored_by_undo(tmp_path):
    c = build(tmp_path)
    call(c, "note_edit", {"note_id": "ghost", "text": "x"}, session="s")
    call(c, "note_add", {"text": "a"}, session="s")
    r = undo(c, "s", confirm=True, reason="roll back").json()
    assert len(r["undone"]) == 1 and len(r["ignored_failed"]) == 1 and r["complete"] is True


def test_a_failing_handler_is_reported_and_does_not_stop_the_rest(tmp_path):
    def broken(ctx, record):
        raise RuntimeError("disk on fire")

    tools = [TOOLS[0], Tool("note_add", "Add.", NoteAdd, ann(False), add_run, track=add_track, undo=broken), *TOOLS[2:]]
    c = build(tmp_path, tools=tools)
    assert call(c, "note_add", {"text": "a"}, session="s").status_code == 200
    call(c, "note_edit", {"note_id": "n1", "text": "b"}, session="s")
    r = undo(c, "s", confirm=True, reason="roll back").json()
    assert [u["tool"] for u in r["undone"]] == ["note_edit"]
    assert r["not_undoable"][0]["reason"] == "undo_failed" and "disk on fire" in r["not_undoable"][0]["message"]
    assert journal(c, kind="undo").json()["entries"][-1]["ok"] is False


def test_undo_survives_journal_rotation(tmp_path):
    c = build(tmp_path, journal_max_bytes=900)
    for i in range(6):
        call(c, "note_add", {"text": f"note {i}"}, agent="a", session="s")
    assert (tmp_path / "agent_journal.jsonl.1").exists()
    r = undo(c, "s", confirm=True, reason="roll back").json()
    assert len(r["undone"]) == 6 and c.ctx.notes == {}


def test_a_tool_whose_capture_fails_is_not_undoable(tmp_path):
    def failing_capture(ctx, a):
        raise RuntimeError("no snapshot")

    tools = [t if t.name != "note_edit" else Tool(t.name, t.description, t.input_model, t.annotations, t.run, capture=failing_capture,
                                                  track=t.track, undo=t.undo) for t in TOOLS]
    c = build(tmp_path, tools=tools)
    c.ctx.notes["n1"] = {"text": "a", "folder": ""}
    assert call(c, "note_edit", {"note_id": "n1", "text": "b"}, session="s").status_code == 200       # the write itself still happens
    assert journal(c).json()["entries"][0]["undoable"] is False
    assert undo(c, "s", dry_run=True).json()["not_undoable"][0]["reason"] == "capture_failed"


# ------------------------------------------------------------------------------------------------ tokens and profiles

def test_profiles_limit_what_a_token_may_call(tmp_path):
    c = build(tmp_path)
    ro = tokens.mint_agent_token(tmp_path, "reader", "read_only")["token"]
    dr = tokens.mint_agent_token(tmp_path, "drafter", "drafts")["token"]
    al = tokens.mint_agent_token(tmp_path, "boss", "all")["token"]
    c.ctx.notes["n9"] = {"text": "t", "folder": ""}
    # read_only: reads only
    assert call(c, "note_list", token=ro, reason=None).status_code == 200
    r = call(c, "note_add", {"text": "x"}, token=ro)
    assert r.status_code == 403 and r.json()["code"] == "profile_forbidden" and "read-only" in r.json()["hint"]
    # drafts: reads and draft_safe tools, nothing else
    assert call(c, "note_list", token=dr, reason=None).status_code == 200
    assert call(c, "note_add", {"text": "draft"}, token=dr, session="d").status_code == 200
    for blocked in (("note_edit", {"note_id": "n9", "text": "x"}), ("note_delete", {"note_id": "n9"}), ("folder_rename", {"folder_id": "f1", "name": "x"})):
        r = call(c, blocked[0], blocked[1], token=dr)
        assert r.status_code == 403 and r.json()["code"] == "profile_forbidden", blocked
    assert c.ctx.notes["n9"]["text"] == "t"
    # all: everything
    assert call(c, "note_edit", {"note_id": "n9", "text": "x"}, token=al).status_code == 200
    # an unknown tool is still a 404 for a scoped token
    assert call(c, "teleport", token=ro, reason=None).status_code == 404


def test_denied_calls_are_not_writes_but_are_audited(tmp_path, events):
    c = build(tmp_path)
    ro = tokens.mint_agent_token(tmp_path, "reader", "read_only")["token"]
    call(c, "note_add", {"text": "x"}, token=ro, session="s")
    assert journal(c).json()["entries"] == []
    audit = [d for t, d in events if t == "agent.call"][-1]
    assert audit["ok"] is False and "profile_forbidden" in audit["error"] and audit["caller"] == "reader"


def test_a_scoped_token_reads_only_its_own_journal_and_cannot_undo(tmp_path):
    c = build(tmp_path)
    a = tokens.mint_agent_token(tmp_path, "agent-a", "drafts")["token"]
    call(c, "note_add", {"text": "from a"}, token=a, session="sa")
    call(c, "note_add", {"text": "from main"}, agent="agent-b", session="sb")
    rows = journal(c, token=a).json()["entries"]
    assert [r["agent"] for r in rows] == ["agent-a"]
    assert [r["agent"] for r in journal(c, token=a, agent="agent-b").json()["entries"]] == ["agent-a"]   # cannot ask for the others
    r = undo(c, "sa", token=a, confirm=True, reason="trying my luck")
    assert r.status_code == 403 and r.json()["code"] == "profile_forbidden"
    full = tokens.mint_agent_token(tmp_path, "agent-c", "all")["token"]
    call(c, "note_add", {"text": "from c"}, token=full, session="sc")
    assert undo(c, "sa", token=full, confirm=True, reason="a full token of another agent").status_code == 404   # limited to its own agent
    assert undo(c, "sc", token=full, confirm=True, reason="my own session").json()["undone"]


def test_minting_and_revoking_take_effect_without_a_restart(tmp_path):
    c = build(tmp_path)
    minted = c.post("/api/agent/tokens", json={"agent": "codex-sparks", "profile": "drafts", "label": "laptop"},
                    headers={"Authorization": f"Bearer {MAIN}"}).json()
    assert minted["ok"] and minted["token"].startswith("hat_") and minted["profile"] == "drafts" and minted["id"]
    assert call(c, "note_list", token=minted["token"], reason=None).status_code == 200
    listed = c.get("/api/agent/tokens", headers={"Authorization": f"Bearer {MAIN}"}).json()
    assert listed["profiles"] == ["read_only", "drafts", "all"]
    assert listed["tokens"] == [{"id": minted["id"], "agent": "codex-sparks", "profile": "drafts", "label": "laptop", "created": listed["tokens"][0]["created"]}]
    assert minted["token"] not in json.dumps(listed)
    stored = json.loads((tmp_path / "agent_tokens.json").read_text())
    assert list(stored) == [hashlib.sha256(minted["token"].encode()).hexdigest()] and minted["token"] not in json.dumps(stored)
    gone = c.delete(f"/api/agent/tokens/{minted['id']}", headers={"Authorization": f"Bearer {MAIN}"})
    assert gone.status_code == 200 and gone.json()["revoked"] == 1
    assert call(c, "note_list", token=minted["token"], reason=None).status_code == 401
    assert c.delete(f"/api/agent/tokens/{minted['id']}", headers={"Authorization": f"Bearer {MAIN}"}).status_code == 404


def test_token_admin_is_for_the_main_token_only(tmp_path):
    c = build(tmp_path)
    boss = tokens.mint_agent_token(tmp_path, "boss", "all")["token"]
    for method, url, body in (("get", "/api/agent/tokens", None), ("post", "/api/agent/tokens", {"agent": "x", "profile": "all"}),
                              ("delete", "/api/agent/tokens/abcdefgh1234", None)):
        r = getattr(c, method)(url, headers={"Authorization": f"Bearer {boss}"}, **({"json": body} if body else {}))
        assert r.status_code == 403, (method, r.text)
        r = getattr(c, method)(url, **({"json": body} if body else {}))
        assert r.status_code == 401
    bad = c.post("/api/agent/tokens", json={"agent": "x", "profile": "root"}, headers={"Authorization": f"Bearer {MAIN}"})
    assert bad.status_code == 400 and "profile" in bad.json()["error"]


def test_token_library_and_cli(tmp_path):
    minted = tokens.mint_agent_token(tmp_path, "cursor", "read_only", label="ide")
    assert tokens.lookup_agent_token(tmp_path, minted["token"]) == {"id": minted["id"], "agent": "cursor", "profile": "read_only", "label": "ide"}
    assert tokens.lookup_agent_token(tmp_path, "nope") is None and tokens.lookup_agent_token(tmp_path, "") is None
    with pytest.raises(ValueError):
        tokens.mint_agent_token(tmp_path, "bad agent!", "all")
    with pytest.raises(ValueError):
        tokens.mint_agent_token(tmp_path, "ok", "admin")
    root = str(Path(__file__).resolve().parents[2])

    def cli(*args):
        out = subprocess.run([sys.executable, "-m", "hoard_link.tokens", *args], capture_output=True, text=True, cwd=root)
        return out.returncode, out.stdout, out.stderr

    code, out, _ = cli("mint", "--app-data-dir", str(tmp_path), "--agent", "codex-sparks", "--profile", "drafts")
    cli_token = json.loads(out)
    assert code == 0 and cli_token["token"].startswith("hat_") and cli_token["profile"] == "drafts"
    assert tokens.lookup_agent_token(tmp_path, cli_token["token"])["agent"] == "codex-sparks"
    code, out, _ = cli("list", "--app-data-dir", str(tmp_path))
    listed = json.loads(out)
    assert code == 0 and {r["agent"] for r in listed} == {"cursor", "codex-sparks"} and "token" not in out
    code, out, _ = cli("revoke", "--app-data-dir", str(tmp_path), "--agent", "cursor")
    assert code == 0 and json.loads(out) == {"revoked": 1}
    assert tokens.lookup_agent_token(tmp_path, minted["token"]) is None
    code, _, err = cli("revoke", "--app-data-dir", str(tmp_path), "--id", "abc")
    assert code == 2 and "8 characters" in err
    code, _, err = cli("mint", "--app-data-dir", str(tmp_path), "--agent", "x y", "--profile", "all")
    assert code == 2 and "agent" in err
    assert (tmp_path / "agent_tokens.json").stat().st_mode & 0o077 == 0 or sys.platform == "win32"


def test_ann_draft_safe_is_only_present_when_set():
    assert "draftSafeHint" not in ann() and ann(draft_safe=True)["draftSafeHint"] is True
    assert ann(True) == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
