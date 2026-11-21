"""Ágora delivery review: the diff of a submission, line comments delivered to the owner, the guided reading, the HTTP
routes and the MCP tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.agora_review import build_chapters, parse_patch

from ._hub_fakes import http, make_hub, serve, ui_headers


def _run(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _repo(root: Path) -> tuple[str, str]:
    """A repo with two delivered commits: one edits and adds, the second renames and deletes."""
    root.mkdir(parents=True, exist_ok=True)
    for args in (["init"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        _run(root, *args)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    (root / "src" / "gone.py").write_text("old\ncode\n", encoding="utf-8")
    (root / "src" / "old_name.py").write_text("print('same')\n" * 8, encoding="utf-8")
    _run(root, "add", ".")
    _run(root, "commit", "-m", "init")
    lines = [f"line {i}" for i in range(1, 21)]
    lines[4] = "line 5 changed"
    lines.insert(10, "inserted after ten")
    (root / "src" / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "src" / "new.py").write_text("def hello():\n    return 'ñ'\n", encoding="utf-8")
    _run(root, "add", ".")
    _run(root, "commit", "-m", "Edit app and add new")
    first = _run(root, "rev-parse", "HEAD")
    (root / "src" / "gone.py").unlink()
    _run(root, "mv", "src/old_name.py", "src/new_name.py")
    (root / "image.bin").write_bytes(bytes(range(256)) * 4)
    _run(root, "add", "-A")
    _run(root, "commit", "-m", "Rename, delete and add a binary")
    return first, _run(root, "rev-parse", "HEAD")


@pytest.fixture
def ag(tmp_path):
    repo = tmp_path / "Demo"
    first, second = _repo(repo)
    events = []
    a = Agora(tmp_path / "agora.db", emit=lambda t, d: events.append((t, d)),
              repo_lookup=lambda name: repo if name == "Demo" else None)
    a.events, a.first, a.second = events, first, second
    yield a
    a.close()


def _delivered(ag, commits=None, **extra):
    task = ag.task_add({"agent": "builder", "title": "Ship it", "repo": "Demo", "claim": True, "paths": ["src"],
                        **extra})["task"]
    out = ag.task_submit({"agent": "builder", "task_id": task["id"], "summary": "Done",
                          "commits": commits or [ag.first, ag.second]})
    return out["task"]


def test_patch_parser_numbers_every_line():
    text = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -3,3 +3,4 @@ def f():\n a\n-b\n+B\n"
            "+extra\n c\n\\ No newline at end of file\n"
            "diff --git a/o.py b/n.py\nsimilarity index 100%\nrename from o.py\nrename to n.py\n"
            "diff --git a/z.bin b/z.bin\nnew file mode 100644\nBinary files /dev/null and b/z.bin differ\n")
    files = parse_patch(text)
    assert [f["path"] for f in files] == ["x.py", "n.py", "z.bin"]
    x = files[0]
    assert (x["additions"], x["deletions"]) == (2, 1)
    lines = x["hunks"][0]["lines"]
    assert [(l["t"], l["old"], l["new"]) for l in lines] == [
        ("ctx", 3, 3), ("del", 4, None), ("add", None, 4), ("add", None, 5), ("ctx", 5, 6)]
    assert files[1]["status"] == "renamed" and files[1]["old_path"] == "o.py"
    assert files[2]["binary"] is True and files[2]["status"] == "added"


def test_diff_reads_each_commit_with_stats_and_filters(ag):
    task = _delivered(ag)
    out = ag.review_diff({"task_id": task["id"]})
    assert [c["short"] for c in out["commits"]] == [ag.first[:10], ag.second[:10]]
    first, second = out["commits"]
    assert first["subject"] == "Edit app and add new"
    assert {f["path"]: f["status"] for f in first["files"]} == {"src/app.py": "modified", "src/new.py": "added"}
    assert {f["path"]: f["status"] for f in second["files"]} == {
        "src/gone.py": "deleted", "src/new_name.py": "renamed", "image.bin": "added"}
    assert next(f for f in second["files"] if f["path"] == "image.bin")["binary"] is True
    app = next(f for f in first["files"] if f["path"] == "src/app.py")
    added = [l for h in app["hunks"] for l in h["lines"] if l["t"] == "add"]
    assert {l["text"] for l in added} == {"line 5 changed", "inserted after ten"}
    assert next(l for l in added if l["text"] == "inserted after ten")["new"] == 11
    # narrowing and counts only
    one = ag.review_diff({"task_id": task["id"], "commit": ag.second[:8], "path": "src/gone.py"})
    assert len(one["commits"]) == 1 and [f["path"] for f in one["commits"][0]["files"]] == ["src/gone.py"]
    brief = ag.review_diff({"task_id": task["id"], "summary_only": True})
    assert all(f["hunks"] == [] for c in brief["commits"] for f in c["files"])
    capped = ag.review_diff({"task_id": task["id"], "commit": ag.first, "path": "src/app.py", "max_lines": 2})
    assert sum(len(h["lines"]) for h in capped["commits"][0]["files"][0]["hunks"]) == 2
    with pytest.raises(AgoraError) as missing:
        ag.review_diff({"task_id": task["id"], "commit": "deadbee"})
    assert missing.value.status == 404


def test_diff_needs_commits_a_repo_and_a_readable_commit(ag):
    bare = ag.task_add({"agent": "builder", "title": "No commits", "claim": True})["task"]
    ag.task_submit({"agent": "builder", "task_id": bare["id"], "summary": "x"})
    with pytest.raises(AgoraError) as nothing:
        ag.review_diff({"task_id": bare["id"]})
    assert nothing.value.status == 409
    stray = _delivered(ag, commits=["0123456789abcdef0123456789abcdef01234567"])
    out = ag.review_diff({"task_id": stray["id"]})
    assert "error" in out["commits"][0] and out["commits"][0]["short"] == "0123456789"
    elsewhere = ag.task_add({"agent": "builder", "title": "Other", "repo": "Unknown", "claim": True})["task"]
    ag.task_submit({"agent": "builder", "task_id": elsewhere["id"], "summary": "x", "commits": [ag.first]})
    with pytest.raises(AgoraError) as unknown:
        ag.review_diff({"task_id": elsewhere["id"]})
    assert unknown.value.status == 404


def test_repo_less_delivery_infers_registered_commits_and_leaves_missing_visible(ag, tmp_path):
    ag.registered_repositories = lambda: [{"name": "Demo", "path": str(tmp_path / "Demo")}]
    missing = "0123456789abcdef0123456789abcdef01234567"
    task = _delivered(ag, commits=[ag.first, missing], repo="")
    out = ag.review_diff({"task_id": task["id"], "summary_only": True})
    assert out["repositories"] == ["Demo"]
    assert out["commits"][0]["repo"] == "Demo"
    assert out["commits"][0]["files"][0]["hunks"] == []
    assert out["commits"][1]["error"]
    unreadable = _delivered(ag, commits=[missing], repo="")
    with pytest.raises(AgoraError, match="--repo-commits") as exc:
        ag.review_diff({"task_id": unreadable["id"]})
    assert exc.value.status == 409
    assert f"agora update {unreadable['id']} --repo" in str(exc.value)


def test_repo_less_delivery_reads_the_matching_checkout_when_names_repeat(ag, tmp_path):
    demo = tmp_path / "Demo"
    other = tmp_path / "other" / "Demo"
    other.mkdir(parents=True)
    _run(other, "init")
    ag.registered_repositories = lambda: [{"name": "Demo", "path": root} for root in (other, demo)]
    ag.repo_lookup = lambda name: other
    task = _delivered(ag, commits=[ag.first], repo="")
    [commit] = ag.review_diff({"task_id": task["id"]})["commits"]
    assert commit["repo"] == "Demo" and commit["sha"] == ag.first and commit["files"]


def test_grouped_submit_keeps_other_repository_when_one_is_rebased(ag, tmp_path):
    demo = tmp_path / "Demo"
    other = tmp_path / "Other"
    other.mkdir()
    _run(other, "init")
    _run(other, "config", "user.email", "t@example.com")
    _run(other, "config", "user.name", "t")
    (other / "other.txt").write_text("from another repository\n", encoding="utf-8")
    _run(other, "add", ".")
    _run(other, "commit", "-m", "Other delivery")
    other_ref = _run(other, "rev-parse", "HEAD")
    ag.repo_lookup = {"Demo": demo, "Other": other}.get
    task = ag.task_add({"agent": "builder", "title": "Cross-repo", "repo": "Demo", "claim": True})["task"]
    args = {"agent": "builder", "task_id": task["id"]}
    task = ag.task_update({**args, "repositories": ["Demo", "Other", "demo"]})["task"]
    assert task["repo"] == "Demo" and task["repositories"] == ["Demo", "Other"]
    r1 = ag.task_submit({**args, "summary": "First", "commits_by_repo": {
        "Demo": [ag.second], "Other": [other_ref]}})["task"]
    _run(demo, "commit", "--amend", "-m", "Rewritten delivery")
    new_ref = _run(demo, "rev-parse", "HEAD")
    r2 = ag.task_submit({**args, "summary": "Second", "commits_by_repo": {"Demo": [new_ref]}})["task"]
    assert r1["submission_revision"] == 1 and r2["submission_revision"] == 2
    assert r2["commits"] == [new_ref, other_ref]
    assert r2["commits_by_repo"] == {"Demo": [new_ref], "Other": [other_ref]}
    diff = ag.review_diff(args)
    assert [(c["repo"], c["sha"]) for c in diff["commits"]] == [("Demo", new_ref), ("Other", other_ref)]
    assert diff["commits"][1]["files"][0]["hunks"][0]["lines"][0]["text"] == "from another repository"
    with pytest.raises(AgoraError, match="either commits"):
        ag.task_submit({**args, "summary": "Invalid", "commits": [new_ref], "commits_by_repo": {"Other": [other_ref]}})
    assert ag.task(args)["task"]["submission_revision"] == 2


def test_legacy_diff_infers_repositories_per_commit_and_reports_shared_hash(ag, tmp_path):
    demo = tmp_path / "Demo"
    other = tmp_path / "Other"
    _run(tmp_path, "clone", str(demo), str(other))
    _run(other, "config", "user.email", "t@example.com")
    _run(other, "config", "user.name", "t")
    _run(other, "commit", "--allow-empty", "-m", "Only in the second repository")
    other_ref = _run(other, "rev-parse", "HEAD")
    ag.repo_lookup = {"Demo": demo, "Other": other}.get
    ag.registered_repositories = lambda: [{"name": name, "path": path} for name, path in
                                          (("Demo", demo), ("Other", other))]
    task = _delivered(ag, commits=[ag.first, other_ref], repo="")
    commits = ag.review_diff({"task_id": task["id"]})["commits"]
    assert commits[0]["repository_candidates"] == ["Demo", "Other"]
    assert commits[0]["sha"] == ag.first and commits[0]["files"]
    assert commits[1]["repo"] == "Other"
    primary_task = _delivered(ag, commits=[ag.first, other_ref])
    legacy = ag.review_diff({"task_id": primary_task["id"]})["commits"]
    assert [(c["repo"], c["sha"]) for c in legacy] == [("Demo", ag.first), ("Other", other_ref)]
    ag.task_submit({"agent": "builder", "task_id": task["id"], "summary": "Named",
                    "commits_by_repo": {"Demo": [ag.first], "Other": [other_ref]}})
    assert ag.review_diff({"task_id": task["id"]})["repositories"] == ["Demo", "Other"]


def test_comments_reach_the_owner_as_one_message(ag):
    task = _delivered(ag)
    revision = task["submission_revision"]
    out = ag.review_comment_send({
        "agent": "reviewer", "task_id": task["id"], "expected_submission_revision": revision,
        "body": "Overall good.",
        "comments": [{"commit": ag.first[:8], "path": "src/app.py", "line": 11, "body": "Why here?"},
                     {"commit": ag.first, "path": "src/app.py", "line": 5, "side": "old", "end_line": 6,
                      "body": "The old text was clearer."}]})
    assert out["count"] == 2 and out["delivery"]["mentioned"] == ["builder"]
    thread = ag.task({"task_id": task["id"], "peek": True})
    msg = thread["messages"][-1]
    assert msg["author"] == "reviewer" and msg["mentions"] == ["builder"] and msg["id"] == out["message_id"]
    assert "src/app.py:11 (nuevo)" in msg["body"] and "> inserted after ten" in msg["body"] and "Why here?" in msg["body"]
    assert "src/app.py:5-6 (antes)" in msg["body"] and "Overall good." in msg["body"]
    inbox = ag.inbox({"agent": "builder", "peek": True})
    assert any(m["id"] == out["message_id"] for m in inbox["messages"])
    listed = ag.review_comments({"task_id": task["id"]})
    assert listed["count"] == 2 and [c["line"] for c in listed["comments"]] == [11, 5]
    assert listed["comments"][0]["commit"] == ag.first and listed["comments"][0]["code"] == "inserted after ten"
    assert not listed["comments"][0]["outdated"]
    assert any(e[0] == "agora.review.comments" and e[1]["count"] == 2 for e in ag.events)
    # the diff view carries them too, and a resubmission makes them outdated, not gone
    assert len(ag.review_diff({"task_id": task["id"]})["comments"]) == 2
    ag.task_submit({"agent": "builder", "task_id": task["id"], "summary": "Again", "commits": [ag.first, ag.second]})
    after = ag.review_comments({"task_id": task["id"]})
    assert all(c["outdated"] for c in after["comments"])
    assert ag.review_comments({"task_id": task["id"], "revision": revision + 1})["count"] == 0


def test_comments_are_checked_against_the_diff(ag):
    task = _delivered(ag)
    base = {"agent": "reviewer", "task_id": task["id"]}

    def send(**item):
        return ag.review_comment_send({**base, "comments": [{"commit": ag.first, "path": "src/app.py", "line": 11,
                                                             "body": "x", **item}]})

    with pytest.raises(AgoraError) as far:
        send(line=19)                       # a line the commit did not touch and has no context for
    assert far.value.status == 404
    with pytest.raises(AgoraError) as wrong_side:
        send(line=20, side="old")           # outside every hunk on the old side too
    assert wrong_side.value.status == 404
    with pytest.raises(AgoraError):
        send(path="src/nothing.py")
    with pytest.raises(AgoraError):
        send(commit="")                     # two commits: the commit has to be named
    with pytest.raises(AgoraError):
        send(body="  ")
    with pytest.raises(AgoraError):
        send(side="middle")
    with pytest.raises(AgoraError):
        send(end_line=3)
    with pytest.raises(AgoraError):
        ag.review_comment_send({**base, "comments": []})
    with pytest.raises(AgoraError) as old:
        ag.review_comment_send({**base, "expected_submission_revision": 9,
                                "comments": [{"commit": ag.first, "path": "src/app.py", "line": 11, "body": "x"}]})
    assert old.value.status == 409 and old.value.extra["error_code"] == "stale_submission"
    assert ag.review_comments({"task_id": task["id"]})["count"] == 0     # nothing was stored by the refused ones
    # a general note alone is fine and a single-commit delivery does not need the commit named
    only = _delivered(ag, commits=[ag.first])
    ag.review_comment_send({**base, "task_id": only["id"], "comments": [{"path": "src/new.py", "line": 2, "body": "ñ ok?"}]})
    assert ag.review_comment_send({**base, "task_id": only["id"], "body": "Just a note"})["count"] == 0


def test_the_person_comments_only_from_the_page(ag):
    task = _delivered(ag)
    item = {"commit": ag.first, "path": "src/app.py", "line": 11, "body": "from the page"}
    with pytest.raises(AgoraError) as refused:
        ag.review_comment_send({"agent": "person", "task_id": task["id"], "comments": [item]})
    assert refused.value.status == 403
    out = ag.review_comment_send({"agent": "person", "_person": True, "task_id": task["id"], "comments": [item]})
    assert out["delivery"]["mentioned"] == ["builder"]


def test_session_hook_is_reported_in_the_delivery(ag):
    ag.review_session_hook = lambda who: {"worker_id": "w1", "state": "running", "via": "inbox mention"} if who == "builder" else None
    task = _delivered(ag)
    out = ag.review_comment_send({"agent": "reviewer", "task_id": task["id"], "comments": [
        {"commit": ag.first, "path": "src/app.py", "line": 11, "body": "x"}]})
    assert out["delivery"]["session"]["worker_id"] == "w1"


GUIDE = {"summary": "Edits the app and tidies files.", "chapters": [
    {"title": "The app", "summary": "app.py gets a line.", "check": "Line 11", "files": ["src/app.py", "nope.py"]},
    {"title": "Empty", "summary": "no real files", "files": ["ghost.py"]}]}


def test_guide_orders_the_diff_into_chapters_and_keeps_them(ag):
    task = _delivered(ag)
    calls = []

    def chat(body):
        calls.append(body)
        return 200, {"ok": True, "json": GUIDE, "model": "fake-model"}

    ag.review_chat = chat
    assert ag.review_guide({"agent": "reviewer", "task_id": task["id"], "only_cached": True})["guide"] is None
    out = ag.review_guide({"agent": "reviewer", "task_id": task["id"]})
    guide = out["guide"]
    assert not out["cached"] and guide["model"] == "fake-model" and guide["summary"].startswith("Edits the app")
    titles = [c["title"] for c in guide["chapters"]]
    assert titles == ["The app", "Resto de cambios"]                      # invented files and empty chapters are gone
    assert guide["chapters"][0]["files"] == ["src/app.py"]
    rest = guide["chapters"][1]["files"]
    assert set(rest) == {"src/new.py", "src/gone.py", "src/new_name.py", "image.bin"}   # the whole diff is covered
    prompt = calls[0]["messages"][1]["content"]
    assert "Ship it" in prompt and "inserted after ten" in prompt and calls[0]["json"]["required"]
    again = ag.review_guide({"agent": "claude", "task_id": task["id"]})
    assert again["cached"] and len(calls) == 1
    assert ag.review_diff({"task_id": task["id"]})["has_guide"] is True
    ag.review_guide({"agent": "claude", "task_id": task["id"], "refresh": True})
    assert len(calls) == 2
    # a new submission is a new guide
    ag.task_submit({"agent": "builder", "task_id": task["id"], "summary": "More", "commits": [ag.first]})
    assert ag.review_guide({"agent": "claude", "task_id": task["id"], "only_cached": True})["guide"] is None


def test_guide_failures_are_honest(ag):
    task = _delivered(ag)
    with pytest.raises(AgoraError) as none:
        ag.review_guide({"agent": "reviewer", "task_id": task["id"]})
    assert none.value.status == 503
    ag.review_chat = lambda body: (503, {"ok": False, "error": "no_model", "detail": "Spark is down"})
    with pytest.raises(AgoraError) as down:
        ag.review_guide({"agent": "reviewer", "task_id": task["id"]})
    assert "Spark is down" in str(down.value)
    ag.review_chat = lambda body: (200, {"ok": True, "json": None, "text": "I refuse"})
    with pytest.raises(AgoraError):
        ag.review_guide({"agent": "reviewer", "task_id": task["id"]})
    assert ag.review_guide({"agent": "reviewer", "task_id": task["id"], "only_cached": True})["guide"] is None


def test_build_chapters_rules():
    summary, chapters = build_chapters({"summary": "S", "chapters": [
        {"title": "A", "summary": "x", "files": ["a", "a", "b"]}, "junk", {"title": "", "files": ["a"]}]}, ["a", "b", "c"])
    assert summary == "S" and [c["files"] for c in chapters] == [["a", "b"], ["c"]]
    with pytest.raises(AgoraError):
        build_chapters({"chapters": []}, ["a"])
    with pytest.raises(AgoraError):
        build_chapters("text", ["a"])


def test_http_routes_and_mcp_tools(tmp_path):
    repo = tmp_path / "Demo"
    first, _second = _repo(repo)
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"enabled": False}
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    facet = hub.facet("agora")
    facet.agora.repo_lookup = lambda name: repo if name == "Demo" else None
    page = ui_headers(hub)
    try:
        names = {t["name"] for t in tools.all_tools()}
        assert {"hub_agora_review_diff", "hub_agora_review_comments", "hub_agora_review_comment_send",
                "hub_agora_review_guide"} <= names
        task = tools.call(hub, "hub_agora_task_add", {"agent": "builder", "title": "Ship", "repo": "Demo",
                                                       "claim": True})["task"]
        tools.call(hub, "hub_agora_task_submit", {"agent": "builder", "task_id": task["id"], "summary": "Done",
                                                   "commits": [first]})
        status, body = http(base + f"/api/agora/review_diff?task_id={task['id']}")
        assert status == 200 and body["commits"][0]["files"][0]["hunks"], body
        status, body = http(base + "/api/agora/review_comment_send", {
            "task_id": task["id"], "comments": [{"path": "src/app.py", "line": 11, "body": "from the page"}]},
            headers=page)
        assert status == 200 and body["count"] == 1 and body["delivery"]["mentioned"] == ["builder"], body
        status, body = http(base + "/api/agora/review_comment_send", {
            "agent": "person", "task_id": task["id"], "comments": [{"path": "src/app.py", "line": 11, "body": "x"}]})
        assert status == 401
        status, body = http(base + f"/api/agora/review_comments?task_id={task['id']}")
        assert status == 200 and body["comments"][0]["author"] == "person"
        shown = tools.call(hub, "hub_agora_review_diff", {"task_id": task["id"], "summary_only": True})
        assert shown["commits"][0]["stats"]["files"] == 2
        sent = tools.call(hub, "hub_agora_review_comment_send", {
            "agent": "reviewer", "task_id": task["id"], "expected_submission_revision": 1,
            "comments": [{"path": "src/new.py", "line": 1, "body": "name?"}]})
        assert sent["ok"] and sent["count"] == 1, sent
        listed = tools.call(hub, "hub_agora_review_comments", {"task_id": task["id"]})
        assert listed["count"] == 2
        facet.agora.review_chat = lambda body: (200, {"ok": True, "json": GUIDE, "model": "m"})
        guided = tools.call(hub, "hub_agora_review_guide", {"agent": "reviewer", "task_id": task["id"]})
        assert guided["guide"]["chapters"][0]["title"] == "The app", guided
        status, body = http(base + "/api/agora/review_guide", {"task_id": task["id"], "only_cached": True},
                            headers=page)
        assert status == 200 and body["cached"] is True
    finally:
        server.shutdown()
        hub.close()
