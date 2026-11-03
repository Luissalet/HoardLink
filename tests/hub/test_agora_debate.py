"""Ágora debates: frozen snapshot, findings with evidence, verify / challenge / reject rounds, consensus, close into
the decision log with tasks from the adopted findings, inbox wake-up, HTTP routes and MCP tools."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub.agora import Agora, AgoraError, MIGRATIONS
from hoard_link.hub.agora_debate import finding_status, guess_evidence_kind, normalize_evidence

from ._hub_fakes import http, make_hub, serve


def _git_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    for args in (["init"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def ag(tmp_path):
    events = []
    repo = tmp_path / "Demo"
    head = _git_repo(repo)
    a = Agora(tmp_path / "agora.db", emit=lambda t, d: events.append((t, d)),
              repo_lookup=lambda name: repo if name == "Demo" else None)
    a.events, a.head = events, head
    yield a
    a.close()


def _thread(ag, kind="debate"):
    return ag.thread_open({"agent": "claude", "title": "Cache or recompute?", "body": "Should the board cache "
                           "the observer view?", "kind": kind})["thread"]["id"]


def test_evidence_is_classified():
    assert guess_evidence_kind("src/app.py#L3-L5") == "file"
    assert guess_evidence_kind("hoard_link/hub/agora.py:120") == "file"
    assert guess_evidence_kind("bf3215f") == "commit"
    assert guess_evidence_kind("https://example.com/x") == "url"
    assert guess_evidence_kind("$ pytest -q") == "command"
    assert guess_evidence_kind("it is slower") == "note"
    ev = normalize_evidence(["$ pytest -q tests/x.py\n12 passed in 0.4s", {"kind": "url", "ref": "https://a.b"}])
    assert ev[0] == {"kind": "command", "ref": "pytest -q tests/x.py", "output": "12 passed in 0.4s"}
    assert ev[1] == {"kind": "url", "ref": "https://a.b"}
    with pytest.raises(AgoraError):
        normalize_evidence([{"kind": "rumour", "ref": "x"}])


def test_status_rules():
    assert finding_status("a", {}) == "proposed"
    assert finding_status("a", {"b": "verify"}) == "verified"
    assert finding_status("a", {"b": "verify", "c": "challenge"}) == "challenged"
    assert finding_status("a", {"b": "reject", "c": "reject", "d": "verify"}) == "rejected"
    assert finding_status("a", {"b": "reject", "c": "verify"}) == "challenged"
    assert finding_status("a", {"a": "reject", "b": "verify"}) == "rejected"          # withdrawn
    assert finding_status("a", {"b": "reject", "c": "reject", "luis": "verify"}) == "verified"   # Luis decides


def test_open_freezes_snapshot_and_invites(ag):
    tid = _thread(ag)
    r = ag.debate_open({"agent": "claude", "thread_id": tid, "participants": ["codex", "@luis"],
                        "files": ["src/app.py#L3-L5", "src/app.py"], "commits": ["abc1234"], "repo": "Demo"})
    d = r["debate"]
    assert d["participants"] == ["claude", "codex", "luis"] and d["status"] == "open"
    snap = d["snapshot"]
    assert snap["head"] == ag.head and snap["question"].startswith("Should the board")
    assert snap["files"][0]["text"] == "line 3\nline 4\nline 5" and snap["files"][0]["lines"] == [3, 5]
    assert snap["files"][1] == {"ref": "src/app.py", "path": "src/app.py", "head": ag.head}
    assert snap["commits"] == ["abc1234"] and len(snap["digest"]) == 64
    inbox = ag.inbox({"agent": "codex", "peek": True})
    assert [m["author"] for m in inbox["pending"]] == ["claude"] and "Debate #" in inbox["pending"][0]["body"]
    assert any(t == "agora.debate.opened" for t, _ in ag.events)
    assert ag.db.schema_version == len(MIGRATIONS)


def test_rounds_consensus_and_close_into_decision_log(ag):
    tid = _thread(ag)
    did = ag.debate_open({"agent": "claude", "thread_id": tid, "participants": ["codex"]})["debate"]["id"]
    f1 = ag.debate_finding({"agent": "claude", "debate_id": did, "claim": "The view is rebuilt on every read",
                            "evidence": ["src/app.py#L1-L2", "$ pytest -q\n3 passed"], "disposition": "adopt"})["finding"]
    f2 = ag.debate_finding({"agent": "codex", "debate_id": did, "claim": "A 5 s cache already exists",
                            "evidence": ["bf3215f"], "disposition": "drop"})["finding"]
    f3 = ag.debate_finding({"agent": "codex", "debate_id": did, "claim": "Measure first",
                            "disposition": "needs-luis"})["finding"]
    assert f1["status"] == "proposed" and f1["evidence"][1]["kind"] == "command" and f3["disposition"] == "needs_luis"
    with pytest.raises(AgoraError):
        ag.debate_verify({"agent": "claude", "finding_id": f1["id"]})               # not your own
    r = ag.debate_challenge({"agent": "codex", "finding_id": f1["id"], "note": "only when refresh=1"})
    assert r["finding"]["status"] == "challenged" and r["finding"]["challenged_by"] == ["codex"]
    pending = ag.inbox({"agent": "claude", "peek": True})["pending"]
    assert any("Cuestiona F" in m["body"] for m in pending)
    ag.debate_finding({"agent": "claude", "finding_id": f1["id"], "evidence": ["src/app.py:7"]})
    with pytest.raises(AgoraError) as e:
        ag.debate_finding({"agent": "codex", "finding_id": f1["id"], "claim": "mine now"})
    assert e.value.status == 403
    r = ag.debate_verify({"agent": "codex", "finding_id": f1["id"], "note": "reproduced"})
    assert r["finding"]["status"] == "verified" and len(r["finding"]["evidence"]) == 3
    ag.debate_reject({"agent": "claude", "finding_id": f2["id"], "note": "that cache is per process"})
    ag.debate_verify({"agent": "luis", "_person": True, "finding_id": f3["id"]})
    view = ag.debate_show({"debate_id": did})
    c = view["consensus"]
    assert c["by_status"]["verified"] == [f1["id"], f3["id"]] and c["by_status"]["rejected"] == [f2["id"]]
    assert c["adopted"] == [f1["id"]] and c["participants"]["codex"]["challenge"] == 0
    assert c["participants"]["codex"]["verify"] == 1 and "luis" in c["participants"]
    assert [e["seq"] for e in view["events"]] == list(range(1, len(view["events"]) + 1))
    closed = ag.debate_close({"agent": "claude", "debate_id": did, "outcome": "Cache the view for 5 s",
                              "dispositions": {str(f3["id"]): "adapt"}, "spawn_tasks": True})
    assert closed["decision_thread_id"] == tid and closed["debate"]["status"] == "closed"
    assert len(closed["tasks"]) == 2                                            # f1 adopt + f3 adapt, not f2
    task = ag.task({"task_id": closed["tasks"][0]})["task"]
    assert task["title"] == "The view is rebuilt on every read" and "debate-" + str(did) in task["labels"]
    assert {f["task_id"] for f in closed["findings"] if f["task_id"]} == set(closed["tasks"])
    decision = ag.decisions()["decisions"][0]
    assert decision["id"] == tid and "Cache the view for 5 s" in decision["resolution"]
    assert "F%d" % f1["id"] in decision["resolution"]
    with pytest.raises(AgoraError) as e:
        ag.debate_finding({"agent": "codex", "debate_id": did, "claim": "late"})
    assert e.value.status == 409
    assert ag.debate_list({"thread_id": tid})["debates"][0]["consensus"]["counts"]["verified"] == 2


def test_task_thread_close_opens_a_decision_thread(ag):
    task = ag.task_add({"agent": "codex", "title": "Pick the cache", "repo": "Demo", "body": "which cache?"})["task"]
    did = ag.debate_open({"agent": "codex", "task_id": task["id"], "participants": ["claude"]})["debate"]["id"]
    assert ag.debate_list({"task_id": task["id"]})["debates"][0]["id"] == did
    r = ag.debate_close({"agent": "codex", "debate_id": did, "outcome": "Nothing to change"})
    assert r["decision_thread_id"] != task["thread_id"] and r["tasks"] == []
    th = ag.thread({"thread_id": r["decision_thread_id"]})["thread"]
    assert th["kind"] == "decision" and th["status"] == "resolved" and th["task_id"] == task["id"]


def test_inbox_long_wait_wakes_on_a_finding(ag):
    tid = _thread(ag)
    did = ag.debate_open({"agent": "claude", "thread_id": tid, "participants": ["codex"]})["debate"]["id"]
    ag.inbox({"agent": "codex"})                                                 # read everything so far
    got = {}

    def wait():
        got["r"] = ag.inbox({"agent": "codex", "wait_s": 10})

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.3)
    ag.debate_finding({"agent": "claude", "debate_id": did, "claim": "Found it", "evidence": ["src/app.py:2"]})
    t.join(5)
    assert not t.is_alive() and got["r"]["counts"]["messages"] == 1
    assert "codex" in got["r"]["messages"][0]["mentions"]


def test_http_and_tools(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    try:
        names = {t["name"] for t in tools.all_tools()}
        assert {"hub_agora_debate_open", "hub_agora_debate_finding", "hub_agora_debate_challenge",
                "hub_agora_debate_verify", "hub_agora_debate_reject", "hub_agora_debate_close",
                "hub_agora_debate_show", "hub_agora_debate_list"} <= names
        tid = tools.call(hub, "hub_agora_thread_open", {"agent": "claude", "title": "T", "body": "q?"})["thread"]["id"]
        r = tools.call(hub, "hub_agora_debate_open", {"agent": "claude", "thread_id": tid, "participants": ["luis"]})
        assert r["ok"], r
        did = r["debate"]["id"]
        assert tools.call(hub, "hub_agora_debate_open", {"agent": "luis", "thread_id": tid, "_person": True})["ok"] is False
        f = tools.call(hub, "hub_agora_debate_finding", {"agent": "claude", "debate_id": did, "claim": "X",
                                                         "evidence": ["https://example.com"], "disposition": "adopt"})
        page = {"Sec-Fetch-Site": "same-origin"}
        status, body = http(base + "/api/agora/debate_verify", {"finding_id": f["finding"]["id"], "note": "ok"},
                            headers=page)
        assert status == 200 and body["finding"]["verified_by"] == ["luis"], body
        status, body = http(base + f"/api/agora/debates?thread_id={tid}&full=1")
        assert status == 200 and body["debates"][0]["consensus"]["counts"]["verified"] == 1
        status, body = http(base + f"/api/agora/debates/{did}")
        assert status == 200 and body["findings"][0]["status"] == "verified"
        status, body = http(base + "/api/agora/debate_close", {"debate_id": did, "outcome": "Adopt X",
                                                                "spawn_tasks": True}, headers=page)
        assert status == 200 and body["debate"]["closed_by"] == "luis" and len(body["tasks"]) == 1, body
        shown = tools.call(hub, "hub_agora_debate_show", {"debate_id": did})
        assert shown["debate"]["status"] == "closed" and shown["debate"]["decision_thread_id"] == tid
    finally:
        server.shutdown()
        hub.close()


def test_cli_debate_subcommands(ag, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agora_cli_debate",
                                                  Path(__file__).resolve().parents[2] / "scripts" / "agora.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    tid = _thread(ag)
    sent = []

    def fake(method, path, body=None, timeout=150.0):
        sent.append((method, path, body))
        op = path.split("?")[0].rsplit("/", 1)[-1]
        if method == "POST":
            fn = getattr(ag, op)
            return fn({**body, "_person": body.get("agent") == "luis"})
        if "/debates/" in path:
            return ag.debate_show({"debate_id": op})
        return ag.debate_list({"thread_id": tid, "full": "1"})

    monkeypatch.setattr(cli, "call", fake)
    assert cli.main(["--as", "claude", "debate", "open", str(tid), "--with", "codex", "--file", "src/app.py#L1-L2"]) == 0
    did = ag.debate_list({"thread_id": tid})["debates"][0]["id"]
    assert sent[-1][2]["participants"] == ["codex"] and sent[-1][2]["files"] == ["src/app.py#L1-L2"]
    assert cli.main(["--as", "claude", "debate", "finding", str(did), "--claim", "X holds",
                     "--evidence", "src/app.py:3", "--evidence", "$ pytest -q", "--disposition", "adopt"]) == 0
    fid = ag.debate_show({"debate_id": did})["findings"][0]["id"]
    assert cli.main(["--as", "codex", "debate", "challenge", f"F{fid}", "--note", "where is line 3 used?"]) == 0
    assert cli.main(["--as", "codex", "debate", "verify", str(fid), "--note", "ok now"]) == 0
    assert cli.main(["debate", "show", str(did)]) == 0
    out = capsys.readouterr().out
    assert f"F{fid} [verified · adopt] claude: X holds" in out and "verified: codex" in out
    assert cli.main(["--as", "claude", "debate", "close", str(did), "--outcome", "Adopt X", "--spawn-tasks",
                     "--disposition", f"F{fid}=adapt"]) == 0
    assert sent[-1][2]["dispositions"] == {str(fid): "adapt"}
    shown = ag.debate_show({"debate_id": did})
    assert shown["debate"]["status"] == "closed" and len(shown["debate"]["spawned_tasks"]) == 1
    assert cli.main(["debate", "list"]) == 0 and "resultado: Adopt X" in capsys.readouterr().out


@pytest.mark.parametrize("width", [1280, 390])
def test_findings_board_in_the_browser(tmp_path, width):
    browser_api = pytest.importorskip("playwright.sync_api")
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"enabled": False}
    server = serve(hub)
    ag = hub.facet("agora").agora
    tid = ag.thread_open({"agent": "claude", "title": "Cache?", "body": "cache or not"})["thread"]["id"]
    did = ag.debate_open({"agent": "claude", "thread_id": tid, "participants": ["luis"]})["debate"]["id"]
    f1 = ag.debate_finding({"agent": "claude", "debate_id": did, "claim": "Rebuilt on every read",
                            "evidence": ["src/a.py:3"], "disposition": "adopt"})["finding"]["id"]
    base = hub.config.url.rstrip("/")
    try:
        with browser_api.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Headless Chromium is not installed")
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/")
                page.evaluate("() => localStorage.setItem('hub.lang', 'en')")
                page.goto(base + f"/#agora-{tid}")
                card = page.locator(f'.ag-fnd[data-finding="{f1}"]')
                card.wait_for()
                assert page.locator(".ag-deb-col.proposed .ag-fnd").count() == 1
                card.locator('button[data-op="verify"]').click()
                page.locator(f'.ag-deb-col.verified .ag-fnd[data-finding="{f1}"]').wait_for()
                # Luis files a finding of his own from the board
                page.locator(f'button[data-toggle="deb-{did}-add"]').click()
                form = page.locator(".ag-deb-form").first
                form.locator("textarea").nth(0).fill("Measure it first")
                form.locator("textarea").nth(1).fill("https://example.com/profile")
                form.locator("button.primary").click()
                page.locator(".ag-deb-col.proposed .ag-fnd", has_text="Measure it first").wait_for()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                # a second debate opened from the page
                page.get_by_role("button", name="Open debate").click()
                opening = page.locator(".ag-deb-form", has=page.get_by_role("button", name="Start debate"))
                opening.locator("input").first.fill("Second question")
                opening.get_by_role("button", name="Start debate").click()
                page.locator(".ag-deb-title", has_text="Second question").wait_for()
                assert page.locator(".ag-deb").count() == 2
                # close: outcome into the decision log, one task from the verified adopt finding
                page.locator(f'button[data-toggle="deb-{did}-close"]').click()
                closing = page.locator(".ag-deb-form", has=page.get_by_role("button", name="Close and record decision"))
                closing.locator("textarea").fill("Cache it")
                closing.get_by_role("button", name="Close and record decision").click()
                page.locator(".ag-deb .ag-dec", has_text="Cache it").wait_for()
                assert errors == []
            finally:
                browser.close()
        shown = ag.debate_show({"debate_id": did})
        assert shown["debate"]["status"] == "closed" and shown["debate"]["closed_by"] == "luis"
        assert len(shown["debate"]["spawned_tasks"]) == 1
        assert [f["author"] for f in shown["findings"]] == ["claude", "luis"]
        assert ag.decisions()["decisions"][0]["id"] == tid
    finally:
        server.shutdown()
        hub.close()
