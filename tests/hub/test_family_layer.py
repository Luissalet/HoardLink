"""The family layer of the hub (0.4): events, rules, jobs, the proxy that
calls apps with their own token, backups, the audit — in memory, over
HTTP, through the agent tools and from the app-side client."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub.actions import render, validate as validate_actions
from hoard_link.hub.backup import BackupStore
from hoard_link.hub.events import EventLog, matches
from hoard_link.hub.jobs import Scheduler, is_due, next_run, parse_every, validate_job
from hoard_link.hub.rules import RuleEngine, rule_matches
from hoard_link.hub.server import make_server

from .conftest import free_port, write_manifest
from .test_hub_and_server import _http


# ---- a sibling app on the shared contract ------------------------------------------

class _ContractApp(BaseHTTPRequestHandler):
    """Answers /api/health, /api/agent/tools and /api/agent/call (token
    from its own data/mcp-token), like the real apps."""
    service = "contract-hoard"
    token = "app-token-123"
    calls: list[dict] = []

    def log_message(self, *a):  # noqa: D102
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/agent/tools"):
            return self._json({"tools": [{"name": "echo", "description": "Echo back. Keywords: eco.",
                                          "inputSchema": {"type": "object"}}]})
        return self._json({"ok": True, "service": self.service, "hoard_link": {"version": "0.4.0", "events": True}})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        auth = self.headers.get("Authorization", "")
        if self.path == "/api/agent/call":
            if auth != "Bearer " + self.token:
                return self._json({"error": "Invalid MCP token."}, 401)
            type(self).calls.append(body)
            if body.get("name") == "boom":
                return self._json({"error": "it broke"}, 400)
            if body.get("name") not in ("echo",):
                return self._json({"error": f"unknown tool: {body.get('name')}"}, 404)
            return self._json({"echoed": body.get("arguments"), "caller": body.get("caller")})
        return self._json({"error": "not found"}, 404)


@pytest.fixture
def contract_app(tmp_path):
    port = free_port()
    _ContractApp.calls = []
    server = HTTPServer(("127.0.0.1", port), _ContractApp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield port
    finally:
        server.shutdown()


@pytest.fixture
def fhub(family, contract_app, tmp_path):
    """A hub whose family includes the contract app (with its token file)."""
    from hoard_link.hub.config import HubConfig
    from hoard_link.hub.core import Hub
    folder = write_manifest(family["root"] / "Contract's Hoard", "contract", contract_app, service="contract-hoard")
    (folder / "data").mkdir()
    (folder / "data" / "mcp-token").write_text(_ContractApp.token, encoding="utf-8")
    (folder / ".gitignore").write_text("data/\n", encoding="utf-8")
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(family["root"])], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False)
    h = Hub(cfg)
    yield h
    h.close()


@pytest.fixture
def fserved(fhub):
    server = make_server(fhub, port=fhub.config.port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield fhub, fhub.config.url
    server.shutdown()


# ---- events ------------------------------------------------------------------------

def test_event_log_emit_query_follow(tmp_path):
    log = EventLog(str(tmp_path / "e.db"))
    a = log.emit("scribe.transcript.done", {"session_id": 7}, source="scribe")
    b = log.emit("agent.call", {"tool": "x"}, source="links")
    assert a["id"] == 1 and b["id"] == 2 and log.last_id == 2
    assert [e["id"] for e in log.query()] == [2, 1]
    assert [e["id"] for e in log.query(type="scribe.*")] == [1]
    assert [e["id"] for e in log.query(source="links")] == [2]
    assert [e["id"] for e in log.query(since_id=1)] == [2]
    assert log.query(text="session_id")[0]["id"] == 1
    assert log.get(1)["data"] == {"session_id": 7}
    got = []
    t = threading.Thread(target=lambda: got.extend(log.follow(2, timeout=5)))
    t.start()
    time.sleep(0.1)
    log.emit("x.y", {}, source="hub")
    t.join(5)
    assert [e["type"] for e in got] == ["x.y"]
    assert log.follow(log.last_id, timeout=0.05) == []
    st = log.stats()
    assert st["total"] == 3 and {r["source"] for r in st["by_source"]} == {"scribe", "links", "hub"}
    # persists
    again = EventLog(str(tmp_path / "e.db"))
    assert again.last_id == 3 and again.count() == 3


def test_event_validation_and_prune(tmp_path):
    log = EventLog(str(tmp_path / "e.db"), keep=5)
    with pytest.raises(ValueError):
        log.emit("", {})
    with pytest.raises(ValueError):
        log.emit("big", {"blob": "x" * 20000})
    with pytest.raises(ValueError):
        log.emit("t", ["not", "an", "object"])  # type: ignore[arg-type]
    assert log.emit("Weird Type!", {})["type"] == "weird_type"
    for i in range(20):
        log.emit("t", {"i": i})
    assert log.prune() > 0 and log.count() == 5
    assert matches("a.*|b.c", "a.x.y") and matches("a.*|b.c", "b.c") and not matches("a.*", "b.c") and matches("*", "z")


# ---- rules / actions --------------------------------------------------------------

def test_render_and_validate_actions():
    ctx = {"event": {"type": "s.t", "data": {"id": 5, "obj": {"a": 1}}}, "today": "2026-01-02"}
    assert render("${event.data.id}", ctx) == 5
    assert render("id=${event.data.id} on ${today} ${missing}", ctx) == "id=5 on 2026-01-02 "
    assert render({"x": ["${event.data.obj}"]}, ctx) == {"x": [{"a": 1}]}
    assert validate_actions([]) and validate_actions([{"kind": "tool", "app": "a"}]) and not validate_actions(
        [{"kind": "tool", "app": "a", "tool": "b"}, {"kind": "event", "type": "x"}, {"kind": "hub", "tool": "hub_events"}])


def test_rule_matching():
    r = {"when": {"type": "scribe.*", "source": "scribe", "where": {"kind": ["meeting", "call"], "data.n": 3}}}
    ev = {"type": "scribe.transcript.done", "source": "scribe", "data": {"kind": "meeting", "n": 3}}
    assert rule_matches(r, ev)
    assert not rule_matches(r, {**ev, "source": "argus"})
    assert not rule_matches(r, {**ev, "data": {"kind": "song", "n": 3}})
    assert not rule_matches(r, {**ev, "data": {"kind": "call", "n": "4"}})
    assert rule_matches({"when": {"type": "*"}}, ev)


def test_rules_run_actions_with_cooldown_and_loop_guard(fhub):
    hub = fhub
    res = hub.rules.add({"name": "echo it", "when": {"type": "scribe.transcript.done"},
                         "then": [{"kind": "tool", "app": "contract", "tool": "echo", "args": {"sid": "${event.data.session_id}"}},
                                  {"kind": "event", "type": "chain.next", "data": {"from": "${event.id}"}}],
                         "cooldown_s": 5})
    assert res["ok"], res
    rid = res["rule"]["id"]
    assert hub.rules.add({"when": {}, "then": []})["ok"] is False
    # A rule on its own chained event must not loop back on itself.
    res2 = hub.rules.add({"name": "loop", "when": {"type": "chain.next"}, "then": [{"kind": "event", "type": "chain.next", "data": {}}], "cooldown_s": 0})
    assert res2["ok"]
    hub.events.emit("scribe.transcript.done", {"session_id": 42}, source="scribe")
    deadline = time.time() + 5
    while time.time() < deadline and not any(h["rule"] == rid for h in hub.rules.history):
        time.sleep(0.05)
    run = next(h for h in hub.rules.history if h["rule"] == rid)
    assert run["ok"] and run["results"][0]["ok"] and _ContractApp.calls[-1]["arguments"] == {"sid": 42}
    assert _ContractApp.calls[-1]["caller"] == "rule:" + rid
    time.sleep(0.5)
    chained = hub.events.query(type="chain.next")
    assert chained and chained[-1]["data"]["_via_rule"] == rid
    # The loop rule fired once for the chained event and not for its own emission.
    time.sleep(0.5)
    loop_runs = [h for h in hub.rules.history if h["rule"] == res2["rule"]["id"]]
    assert len(loop_runs) == 1
    assert hub.events.query(type="hub.rule.ran")
    # cooldown: a second event right away is skipped
    hub.events.emit("scribe.transcript.done", {"session_id": 43}, source="scribe")
    time.sleep(0.3)
    assert hub.rules.get(rid)["runs"] == 1 and hub.rules.get(rid).get("skipped", 0) >= 1
    # manual run with a synthetic event, disable, remove
    out = hub.rules.run(hub.rules.get(rid), {"id": None, "type": "manual", "source": "test", "data": {"session_id": 1}})
    assert out["ok"] and hub.rules.get(rid)["runs"] == 2
    assert hub.rules.update(rid, {"enabled": False})["rule"]["enabled"] is False
    assert hub.rules.test({"type": "scribe.transcript.done", "source": "scribe", "data": {}})[0]["id"] == rid
    assert hub.rules.remove(rid)["ok"] and hub.rules.get(rid) is None
    # persisted
    reloaded = RuleEngine(hub.config.rules_file, hub.events, lambda a, c, w: [])
    assert [r["name"] for r in reloaded.list()] == ["loop"]
    reloaded.close()


# ---- jobs --------------------------------------------------------------------------

def test_job_parsing_and_due():
    assert parse_every("6h") == 21600 and parse_every("30m") == 1800 and parse_every("2d") == 172800 and parse_every("x") is None
    assert validate_job({"then": [{"kind": "event", "type": "x"}]}) and not validate_job({"every": "1h", "then": [{"kind": "event", "type": "x"}]})
    assert validate_job({"every": "10s", "then": [{"kind": "event", "type": "x"}]})
    assert validate_job({"at": "25:00", "then": [{"kind": "event", "type": "x"}]})
    now = time.time()
    job = {"every": "1h", "created_ts": now - 3601, "enabled": True, "then": []}
    assert is_due(job, now) and not is_due({**job, "enabled": False}, now)
    assert next_run({**job, "last_run_ts": now}, now) == pytest.approx(now + 3600)
    # daily at a time already past today, not yet run today -> due (catch-up)
    from datetime import datetime, timedelta
    past = (datetime.fromtimestamp(now) - timedelta(minutes=5)).strftime("%H:%M")
    daily = {"at": past, "enabled": True, "then": [], "created_ts": now - 86400 * 2}
    assert is_due(daily, now)
    assert not is_due({**daily, "last_run_ts": now - 60}, now)
    assert not is_due({**daily, "catch_up": False}, now)
    future = (datetime.fromtimestamp(now) + timedelta(minutes=5)).strftime("%H:%M")
    assert not is_due({**daily, "at": future}, now)
    assert next_run({**daily, "at": future}, now) > now


def test_scheduler_runs_due_jobs(fhub):
    hub = fhub
    res = hub.jobs.add({"name": "ping", "every": "1m", "then": [{"kind": "event", "type": "job.ping", "data": {"d": "${today}"}}]})
    assert res["ok"], res
    jid = res["job"]["id"]
    assert hub.jobs.add({"then": [{"kind": "event", "type": "x"}]})["ok"] is False
    assert hub.jobs.tick() == []  # not due yet
    with_clock = Scheduler(hub.config.jobs_file, lambda a, c, w: hub_actions(hub, a, c, w), events=hub.events,
                           now=lambda: time.time() + 120)
    ran = with_clock.tick()
    assert len(ran) == 1 and ran[0]["ok"] and ran[0]["job"] == jid
    assert hub.events.query(type="job.ping")[0]["data"]["d"]
    assert hub.events.query(type="hub.job.ran")
    assert with_clock.get(jid)["runs"] == 1 and with_clock.get(jid)["next_run_ts"] > time.time() + 100
    out = with_clock.run_now(jid)
    assert out["ok"] and with_clock.get(jid)["runs"] == 2
    assert with_clock.update(jid, {"at": "04:00"})["job"]["every"] is None
    assert with_clock.remove(jid)["ok"] and with_clock.list() == []


def hub_actions(hub, acts, ctx, who):
    from hoard_link.hub import actions
    return actions.run_all(hub, acts, ctx, caller=who)


# ---- proxy / server -----------------------------------------------------------------

def test_call_app_proxy_and_events_over_http(fserved):
    hub, url = fserved
    hub_tok = {"Authorization": "Bearer " + hub.token}
    app_tok = {"Authorization": "Bearer " + _ContractApp.token}
    # tools of an app
    status, body = _http(url + "/api/apps/contract/tools")
    assert status == 200 and body["contract"] == "shared" and body["tools"][0]["name"] == "echo"
    # calling needs a family token
    status, body = _http(url + "/api/apps/contract/call", {"tool": "echo", "arguments": {"a": 1}})
    assert status == 401
    status, body = _http(url + "/api/apps/contract/call", {"tool": "echo", "arguments": {"a": 1}}, headers=hub_tok)
    assert status == 200 and body["ok"] and body["result"]["echoed"] == {"a": 1} and body["result"]["caller"] == "hub"
    status, body = _http(url + "/api/apps/contract/call", {"tool": "echo", "arguments": {}}, headers=app_tok)
    assert status == 200 and body["result"]["caller"] == "contract"
    status, body = _http(url + "/api/apps/contract/call", {"tool": "boom"}, headers=hub_tok)
    assert status == 400 and body["ok"] is False and "broke" in body["error"]
    status, body = _http(url + "/api/apps/contract/call", {"tool": "nope"}, headers=hub_tok)
    assert status == 404 and "unknown tool" in body["error"]
    status, body = _http(url + "/api/apps/dead/call", {"tool": "x"}, headers=hub_tok)
    assert status == 502 and body["ok"] is False
    # every proxied call is an event
    status, body = _http(url + "/api/events?type=hub.call")
    assert status == 200 and len(body["events"]) == 5
    # apps post events with their own token; the source is forced to themselves
    status, body = _http(url + "/api/events", {"type": "contract.thing.done", "data": {"x": 1}, "source": "someone-else"}, headers=app_tok)
    assert status == 200 and body["event"]["source"] == "contract"
    status, body = _http(url + "/api/events", {"type": "x", "data": {}})
    assert status == 401
    status, body = _http(url + "/api/events", {"type": "ui.test", "data": {}}, headers={"Sec-Fetch-Site": "same-origin"})
    assert status == 200 and body["event"]["source"] == "ui"
    status, body = _http(url + "/api/events", {"type": "", "data": {}}, headers=hub_tok)
    assert status == 400
    status, body = _http(url + "/api/events/stats")
    assert status == 200 and body["total"] >= 6 and body["conventions"]
    # SSE: one event after subscribing
    import urllib.request
    req = urllib.request.Request(url + f"/api/events/stream?since_id={hub.events.last_id}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = opener.open(req, timeout=10)
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    threading.Timer(0.2, lambda: hub.events.emit("sse.ping", {"n": 1})).start()
    lines = []
    while True:
        line = resp.readline().decode()
        lines.append(line)
        if line.startswith("data:"):
            break
    assert any(l.startswith("event: sse.ping") for l in lines) and json.loads(lines[-1][5:])["data"] == {"n": 1}
    resp.close()


def test_rules_jobs_backups_over_http_and_tools(fserved, tmp_path):
    hub, url = fserved
    tok = {"Authorization": "Bearer " + hub.token}
    # rules CRUD
    status, body = _http(url + "/api/rules", {"name": "r", "when": {"type": "a.b"}, "then": [{"kind": "event", "type": "c.d", "data": {}}]})
    assert status == 200 and body["ok"]
    rid = body["rule"]["id"]
    status, body = _http(url + "/api/rules")
    assert status == 200 and body["rules"][0]["id"] == rid and body["examples"]
    status, body = _http(url + f"/api/rules/{rid}/test", {"type": "a.b"})
    assert body["matches"][0]["id"] == rid
    status, body = _http(url + f"/api/rules/{rid}/run", {"type": "a.b", "data": {}})
    assert status == 200 and body["ok"] and body["manual"]
    status, body = _http(url + f"/api/rules/{rid}/update", {"enabled": False})
    assert body["rule"]["enabled"] is False
    status, body = _http(url + "/api/rules/nope/remove", {})
    assert status == 404
    status, body = _http(url + f"/api/rules/{rid}/remove", {})
    assert body["ok"]
    # jobs CRUD via tools
    res = tools.call(hub, "hub_job_add", {"name": "j", "at": "03:00", "then": [{"kind": "hub", "tool": "hub_event_stats", "args": {}}]})
    assert res["ok"], res
    jid = res["job"]["id"]
    res = tools.call(hub, "hub_job_run", {"id": jid})
    assert res["ok"] and res["results"][0]["ok"]
    status, body = _http(url + "/api/jobs")
    assert body["jobs"][0]["runs"] == 1 and body["jobs"][0]["next_run_ts"]
    assert tools.call(hub, "hub_job_remove", {"id": jid})["ok"]
    # events tools
    assert tools.call(hub, "hub_event_emit", {"type": "tool.test", "data": {"k": 1}})["ok"]
    res = tools.call(hub, "hub_events", {"type": "tool.*"})
    assert res["count"] == 1 and res["events"][0]["data"] == {"k": 1}
    assert tools.call(hub, "hub_event_emit", {"type": ""})["ok"] is False
    # proxy tool
    res = tools.call(hub, "hub_call_app", {"app": "contract", "tool": "echo", "arguments": {"z": 2}})
    assert res["ok"] and res["result"]["echoed"] == {"z": 2}
    assert tools.call(hub, "hub_app_tools", {"app": "contract"})["tools"][0]["name"] == "echo"
    # backups over http
    contract_dir = Path(hub.get("contract").data_dir)
    (contract_dir / "notes.txt").write_text("hello", encoding="utf-8")
    status, body = _http(url + "/api/backups/run", {"apps": ["contract"], "label": "t"})
    assert status == 200 and body["ok"] and body["totals"]["files"] == 2  # notes + mcp-token
    sid = body["snapshot"]
    status, body = _http(url + "/api/backups")
    assert body["snapshots"][-1]["id"] == sid and "contract" in body["sources"] and "hub" in body["sources"]
    status, body = _http(url + f"/api/backups/{sid}")
    assert status == 200 and body["snapshot"]["apps"]["contract"]["totals"]["files"] == 2
    status, body = _http(url + "/api/backups/verify", {"snapshot": sid})
    assert body["ok"] and body["objects_checked"] == 2
    status, body = _http(url + "/api/backups/restore", {"snapshot": sid, "app": "contract", "dest": str(tmp_path / "out")})
    assert status == 200 and body["ok"] and (tmp_path / "out" / "notes.txt").read_text() == "hello"
    status, body = _http(url + "/api/backups/restore", {"snapshot": sid, "app": "contract", "in_place": True})
    assert status == 409 and "running" in body["error"]
    status, body = _http(url + "/api/backups/restore", {"snapshot": "nope", "app": "contract"})
    assert status == 409
    assert hub.events.query(type="hub.backup.done") and hub.events.query(type="hub.backup.restored")
    # audit
    status, body = _http(url + "/api/audit")
    assert status == 200
    line = next(a for a in body["apps"] if a["id"] == "contract")
    assert line["contract"] == "shared" and line["token_present"] and line["events"] and line["data_gitignored"] is True
    assert "contract" in body["summary"]["shared_contract"] and body["summary"]["library_version"] == "0.4.0"
    res = tools.call(hub, "hub_family_audit", {"probe": False})
    assert res["ok"] and all("stack" in a and "tools" not in a for a in res["apps"])
    # catalogue and mcp bridge list the new tools
    names = {t["name"] for t in tools.catalogue()}
    assert {"hub_events", "hub_rule_add", "hub_job_add", "hub_backup_run", "hub_call_app", "hub_family_audit"} <= names


# ---- backups (store) ---------------------------------------------------------------

def test_backup_store_dedupe_sqlite_restore_prune(tmp_path):
    src = tmp_path / "app" / "data"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("aaa", encoding="utf-8")
    (src / "logs").mkdir()
    (src / "logs" / "x.log").write_text("noise", encoding="utf-8")
    (src / "big.bin").write_bytes(b"\0" * 65536)
    db = sqlite3.connect(str(src / "app.db"))
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES (1)")
    db.commit()  # keep open: a live database
    clock = [1_700_000_000.0]
    store = BackupStore(str(tmp_path / "bk"), max_file_mb=0.02, now=lambda: clock[0])
    res = store.snapshot({"app": str(src), "ghost": str(tmp_path / "nope")}, label="first")
    assert res["ok"], res
    assert res["totals"]["files"] == 2 and res["totals"]["new_files"] == 2 and res["apps"]["ghost"] == {"missing": True}
    m = store.load_snapshot(res["snapshot"])
    kinds = {f["path"]: f["kind"] for f in m["apps"]["app"]["files"]}
    assert kinds == {"a.txt": "file", "app.db": "sqlite"}
    skipped = {s["path"]: s["why"] for s in m["apps"]["app"]["skipped"]}
    assert "logs/" in skipped and skipped["big.bin"].startswith(">")
    # the sqlite copy is a consistent database, not a raw file copy
    sha = next(f["sha"] for f in m["apps"]["app"]["files"] if f["path"] == "app.db")
    copy = sqlite3.connect(store._object_path(sha))
    assert copy.execute("SELECT x FROM t").fetchall() == [(1,)]
    copy.close()
    # second snapshot: nothing new stored
    clock[0] += 60
    res2 = store.snapshot({"app": str(src)})
    assert res2["totals"]["new_files"] == 0 and res2["totals"]["files"] == 2
    (src / "a.txt").write_text("bbb", encoding="utf-8")
    clock[0] += 60
    res3 = store.snapshot({"app": str(src)})
    assert res3["totals"]["new_files"] == 1
    d = store.diff(res["snapshot"], res3["snapshot"])
    assert d["apps"]["app"]["changed"] == ["a.txt"]
    assert store.verify()["ok"] and store.verify(res["snapshot"])["objects_checked"] == 2
    # restore to a side folder, then in place (app not running)
    out = store.restore(res["snapshot"], "app")
    assert out["ok"] and out["dest"].endswith("data.restored-" + time.strftime("%Y%m%d", time.localtime(clock[0])) + "-" + time.strftime("%H%M%S", time.localtime(clock[0])))
    assert (Path(out["dest"]) / "a.txt").read_text() == "aaa"
    assert store.restore(res["snapshot"], "app", dest=out["dest"])["ok"] is False  # not empty
    assert store.restore(res["snapshot"], "app", in_place=True, app_running=True)["ok"] is False
    db.close()
    clock[0] += 1
    inplace = store.restore(res["snapshot"], "app", in_place=True, app_running=False)
    assert inplace["ok"] and (src / "a.txt").read_text() == "aaa" and Path(inplace["previous_data"]).is_dir()
    assert store.restore("nope", "app")["ok"] is False and store.restore(res["snapshot"], "ghost")["ok"] is False
    # prune keeps the last one and drops the unreferenced 'bbb' object? (a.txt=bbb is in snapshot 3 = kept)
    pr = store.prune(keep=1)
    assert pr["dropped_snapshots"] == [res["snapshot"], res2["snapshot"]] and pr["objects_removed"] == 1  # 'aaa'
    assert store.status()["snapshots"] == 1 and store.verify()["ok"]
    # corrupt an object -> verify says so
    m3 = store.load_snapshot(res3["snapshot"])
    sha3 = next(f["sha"] for f in m3["apps"]["app"]["files"] if f["path"] == "a.txt")
    Path(store._object_path(sha3)).write_bytes(b"zzz")
    assert store.verify()["corrupt"] == ["app/a.txt"]


# ---- the app-side client -------------------------------------------------------------

from pydantic import BaseModel as _BaseModel  # noqa: E402


class _Args(_BaseModel):
    """Look something up."""
    q: str
    limit: int = 5


def test_family_client_emit_and_call(fserved, tmp_path, monkeypatch):
    hub, url = fserved
    from hoard_link import family
    monkeypatch.delenv("HOARD_HUB_URL", raising=False)
    app_dir = Path(hub.get("contract").folder)
    st = family.configure("contract", str(app_dir / "data"), hub=url)
    assert st["app"] == "contract" and st["token_file"].endswith("mcp-token")
    assert family.emit("contract.did.thing", {"id": 9}, block=True)
    ev = hub.events.query(type="contract.did.thing")[0]
    assert ev["source"] == "contract" and ev["data"] == {"id": 9}
    family.record_call("echo", True, 12, caller="test")
    deadline = time.time() + 3
    while time.time() < deadline and not hub.events.query(type="agent.call"):
        time.sleep(0.05)
    assert hub.events.query(type="agent.call")[0]["data"]["tool"] == "echo"
    res = family.call("contract", "echo", {"q": 1})
    assert res["ok"] and res["result"]["echoed"] == {"q": 1} and res["result"]["caller"] == "contract"
    assert family.call("dead", "x")["ok"] is False
    hb = family.health_block()
    assert hb["events"] is True and hb["app"] == "contract" and hb["version"] == "0.4.0"
    # without a token the hub refuses, and the client says so
    family.configure("contract", None, token_file=str(tmp_path / "missing"), hub=url)
    assert family.emit("x.y", {}, block=True) is False and "401" in family.status()["last_error"] or family.status()["dropped"] >= 1
    assert "token" in family.call("contract", "echo")["error"]
    # disabled: nothing goes out
    family.configure("contract", str(app_dir / "data"), hub=url, enabled=False)
    assert family.emit("x.y", {}, block=True) is False and family.health_block()["events"] is False


def test_install_fastapi_adds_shared_contract(tmp_path, fserved):
    hub, url = fserved
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hoard_link import family
    app = FastAPI()

    @app.post("/api/agent/docs_search")
    def docs_search(args: _Args):
        """Search the docs. Keywords: buscar."""
        return {"hits": [args.q] * args.limit}

    @app.post("/api/agent/docs_index_folder")
    def docs_index(args: _Args):
        if args.q == "bad":
            raise ValueError("no such folder")
        return {"indexed": args.q}

    @app.get("/api/health")
    def health():
        return {"service": "test-hoard", "hoard_link": family.health_block()}

    # The SPA catch-alls a built frontend adds, registered BEFORE the family routes.
    @app.api_route("/api/{rest:path}", methods=["GET", "POST"])
    def api_not_found(rest: str):
        from fastapi.responses import JSONResponse as _JR
        return _JR({"error": "not_found"}, status_code=404)

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        return {"index": True}

    info = family.install_fastapi(app, "testapp", str(tmp_path / "data"), instructions="Be nice.")
    family.configure("testapp", str(tmp_path / "data"), hub=url)
    assert info["contract"] and (tmp_path / "data" / "mcp-token").is_file()
    token = (tmp_path / "data" / "mcp-token").read_text().strip()
    c = TestClient(app)
    cat = c.get("/api/agent/tools").json()
    assert cat["instructions"] == "Be nice." and [t["name"] for t in cat["tools"]] == ["docs_index_folder", "docs_search"]
    ds = next(t for t in cat["tools"] if t["name"] == "docs_search")
    assert ds["description"].startswith("Search the docs") and ds["inputSchema"]["properties"]["q"]["type"] == "string"
    assert ds["annotations"]["readOnlyHint"] is True
    assert next(t for t in cat["tools"] if t["name"] == "docs_index_folder")["annotations"]["readOnlyHint"] is False
    assert c.post("/api/agent/call", json={"name": "docs_search", "arguments": {"q": "x"}}).status_code == 401
    h = {"Authorization": "Bearer " + token}
    r = c.post("/api/agent/call", json={"name": "docs_search", "arguments": {"q": "x", "limit": 2}}, headers=h)
    assert r.status_code == 200 and r.json() == {"hits": ["x", "x"]}
    r = c.post("/api/agent/call", json={"name": "docs_search", "arguments": {"limit": "no"}}, headers=h)
    assert r.status_code == 400 and r.json()["ok"] is False
    r = c.post("/api/agent/call", json={"name": "docs_index_folder", "arguments": {"q": "bad"}}, headers=h)
    assert r.status_code == 400 and "no such folder" in r.json()["error"]
    r = c.post("/api/agent/call", json={"name": "zzz"}, headers=h)
    assert r.status_code == 404 and r.json()["tools"] == ["docs_index_folder", "docs_search"]
    # the old per-tool route still works and is recorded by the middleware
    r = c.post("/api/agent/docs_search", json={"q": "y"})
    assert r.status_code == 200
    assert c.get("/api/health").json()["hoard_link"]["events"] is True
    deadline = time.time() + 3
    while time.time() < deadline and len(hub.events.query(type="agent.call", source="testapp")) < 4:
        time.sleep(0.05)
    calls = hub.events.query(type="agent.call", source="testapp", newest_first=False)
    assert [ (e["data"]["tool"], e["data"]["ok"]) for e in calls ] == [("docs_search", True), ("docs_search", False), ("docs_index_folder", False), ("docs_search", True)]
    # the hub's proxy reaches it through the shared route with its token
    folder = write_manifest(Path(hub.config.roots[0]) / "Test's Hoard", "testapp", 1, service="test-hoard")
    (folder / "data").mkdir(exist_ok=True)
    (folder / "data" / "mcp-token").write_text(token)
    hub.rescan()
    assert hub.get("testapp").token_file.endswith("mcp-token")
