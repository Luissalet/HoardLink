import json
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hoard_link.atomic import write_json_atomic
from hoard_link.hub import integration_checks as checks
from hoard_link.hub import integration_git as git
from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.agora_ranks import set_rank
from hoard_link.hub.integration import INTEGRATION_TOKEN, IntegrationFacet, delivery_contract, submission_tests
from hoard_link.hub.integration_runtime import IntegrationRuntime, pending_reloads
from hoard_link.hub.production import ProductionFacet


LINES = "\n".join(f"line {i}" for i in range(60)) + "\n"
RUN_WAIT_SECONDS = 180


def write(repo, name, text):
    file = repo / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")


def commit(repo, message, *files):
    git.git(repo, "add", "--", *files)
    git.git(repo, "commit", "-m", message)
    return git.git_text(repo, "rev-parse", "HEAD")


@pytest.fixture
def delivery(tmp_path):
    repo = tmp_path / "Repo with spaces"
    repo.mkdir()
    git.git(repo, "init", "-b", "main")
    git.git(repo, "config", "user.name", "Luissalet")
    git.git(repo, "config", "user.email", "luissalet@users.noreply.github.com")
    write(repo, "feature.py", LINES)
    write(repo, "other.txt", "unrelated\n")
    write(repo, ".gitignore", "__pycache__/\n")
    write(repo, "tests/test_station.py", "def test_station(): assert True\n")
    write(repo, "tests/test_feature.py", """
from pathlib import Path
import pytest

def test_known(tmp_path):
    pending = (value for value in ())
    assert False, f"{pending!r} did not open {tmp_path / 'record.json'} from {Path.cwd()}"

@pytest.mark.inference
def test_model():
    pytest.fail("The contract lane must never run this")
""")
    base = commit(repo, "Create baseline", ".gitignore", "feature.py", "other.txt",
                  "tests/test_feature.py", "tests/test_station.py")
    branch = tmp_path / "delivery"
    git.git(repo, "worktree", "add", "-b", "delivery", str(branch), base)
    write(branch, "feature.py", LINES.replace("line 2\n", "feature\n"))
    with (branch / "tests/test_feature.py").open("a", encoding="utf-8") as f:
        f.write("\ndef test_new():\n    assert True\n")
    head = commit(branch, "Deliver feature", "feature.py", "tests/test_feature.py")
    return repo, base, head, branch


def dirty_checkout(repo):
    staged = LINES.replace("line 20\n", "foreign staged\n")
    working = staged.replace("line 37\n", "foreign unstaged\n")
    write(repo, "feature.py", staged)
    write(repo, "other.txt", "foreign outside\n")
    git.git(repo, "add", "--", "feature.py", "other.txt")
    write(repo, "feature.py", working)
    write(repo, "draft.txt", "untracked\n")
    return staged, working


def test_move_preserves_both_layers_and_unrelated_paths(delivery, tmp_path):
    repo, base, head, _ = delivery
    staged, working = dirty_checkout(repo)
    events = []
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    original = git.foreign(repo, {"feature.py"})
    git.move_checkout(repo, base, head, scratch, lambda state, data: events.append((state, data)))
    assert [event[0] for event in events] == ["prepared_checkout", "moved_checkout", "restored_checkout"]
    assert events[0][1]["parked"] == ["feature.py"]
    assert git.git_text(repo, "show", ":feature.py") == staged.replace("line 2\n", "feature\n").strip()
    assert (repo / "feature.py").read_text() == working.replace("line 2\n", "feature\n")
    assert git.git_text(repo, "show", ":other.txt") == "foreign outside"
    assert (repo / "draft.txt").read_text() == "untracked\n"
    assert (scratch / "parked-index.patch").read_bytes() == original[1]
    assert (scratch / "parked-worktree.patch").read_bytes() == original[2]
    git.move_checkout(repo, head, base, scratch, lambda *_: None)
    assert (repo / "feature.py").read_text() == working
    assert git.git_text(repo, "show", ":feature.py") == staged.strip()


def test_binary_patch_round_trip_preserves_index_and_worktree(delivery, tmp_path):
    repo, _, _, _ = delivery
    asset = repo / "asset.bin"
    asset.write_bytes(b"\x00\x81original\r\n")
    base = commit(repo, "Add binary asset", asset.name)
    staged, unstaged = b"\x00\xd1index\r\n", b"\x00\xffworking\n"
    asset.write_bytes(staged)
    git.git(repo, "add", "--", asset.name)
    asset.write_bytes(unstaged)
    parked = [git.patch(repo, [asset.name], staged=True), git.patch(repo, [asset.name])]
    for index, data in enumerate(parked):
        assert b"GIT binary patch\n" in data
        journal = tmp_path / f"layer-{index}.patch"
        journal.write_bytes(data)
        parked[index] = journal.read_bytes()
    with git.worktree(repo, tmp_path / "replay", base) as replay:
        restored = git.replay(replay, *parked, [asset.name])
    with git.worktree(repo, tmp_path / "restore", base) as restore:
        git.apply_layers(restore, *restored)
        assert git.git(restore, "show", ":" + asset.name).stdout == staged
        assert (restore / asset.name).read_bytes() == unstaged


def test_unreplayable_change_leaves_the_destination_exact(delivery, tmp_path):
    repo, base, head, _ = delivery
    original = LINES.replace("line 2\n", "foreign edit\n")
    write(repo, "feature.py", original)
    with pytest.raises(git.IntegrationError, match="Foreign changes conflict"):
        git.move_checkout(repo, base, head, tmp_path, lambda *_: None)
    assert git.git_text(repo, "rev-parse", "HEAD") == base
    assert (repo / "feature.py").read_text() == original
    assert git.git(repo, "diff", "--cached").stdout == b""


def test_ignored_untracked_file_in_the_way_is_never_replaced(delivery, tmp_path):
    repo, base, _, branch = delivery
    write(branch, "private.tmp", "candidate\n")
    head = commit(branch, "Add candidate data", "private.tmp")
    write(repo, ".git/info/exclude", "*.tmp\n")
    write(repo, "private.tmp", "owner data\n")
    with pytest.raises(git.IntegrationError, match="private.tmp"):
        git.move_checkout(repo, base, head, tmp_path, lambda *_: None)
    assert (repo / "private.tmp").read_text() == "owner data\n"
    assert git.git_text(repo, "rev-parse", "HEAD") == base


def test_unrelated_untracked_sibling_does_not_block_new_file(delivery, tmp_path):
    repo, base, _, branch = delivery
    write(repo, "new/private.tmp", "owner data\n")
    write(branch, "new/feature.txt", "delivery\n")
    head = commit(branch, "Add file", "new/feature.txt")
    git.move_checkout(repo, base, head, tmp_path, lambda *_: None)
    assert (repo / "new/private.tmp").read_text() == "owner data\n"
    assert (repo / "new/feature.txt").read_text() == "delivery\n"


def test_delivery_contract_requires_a_named_tip_and_tracked_test(delivery):
    repo, _, head, _ = delivery
    path = "tests/test_feature.py"
    assert submission_tests(f"Resumen\n\nTest-Paths: {path}, {path}\n") == [path]
    assert submission_tests(f"Test-Paths: {path} tests/with spaces/test_second.py, {path}\n") == [
        path, "tests/with spaces/test_second.py"]
    assert delivery_contract(repo, "delivery", [head], [path]) == head
    with pytest.raises(git.IntegrationError, match="branch"):
        delivery_contract(repo, None, [head], [path])
    with pytest.raises(git.IntegrationError, match="absent"):
        delivery_contract(repo, "delivery", [head], ["tests/test_missing.py"])
    with pytest.raises(git.IntegrationError, match="Invalid"):
        submission_tests("Test-Paths: ../test_unsafe.py")


def test_selects_mapped_contracts_and_separates_inference():
    available = {"tests/test_feature.py", "tests/test_side.py", "tests/test_model.py"}
    config = {"paths": {"app/*": ["tests/test_side.py", "tests/test_model.py"]},
              "inference": ["tests/test_model.py"]}
    assert checks.tests_for({"app/feature.py"}, available, [], config) == {
        "contract": ["tests/test_feature.py", "tests/test_side.py"],
        "inference": ["tests/test_model.py"],
    }


def test_large_focal_selection_is_not_rejected_or_truncated():
    files = {f"tests/test_feature_{i:02}.py" for i in range(45)}
    assert checks.tests_for({"feature.py"}, files, [], {})["contract"] == sorted(files)


@pytest.mark.parametrize("value", [0, -1, True, 5.5, "600", None])
def test_contract_timeout_must_be_a_positive_integer(value):
    assert checks.contract_timeout({}) == 300
    with pytest.raises(git.IntegrationError, match="contract_timeout_seconds"):
        checks.contract_timeout({"contract_timeout_seconds": value})


def test_runtime_keeps_the_revision_loaded_at_startup(delivery):
    repo, base, head, branch = delivery
    running = IntegrationRuntime(repo)
    assert running.status() == {"loaded_commit": base, "main_commit": base, "stale": False}
    assert IntegrationRuntime(branch).status() == {"loaded_commit": head, "main_commit": base, "stale": False}
    merge = git.merge_candidate(repo, head)
    assert running.status() == {"loaded_commit": base, "main_commit": merge, "stale": True}
    assert IntegrationRuntime(repo).status()["stale"] is False


def test_receipt_lists_reload_actions(delivery):
    repo, base, _, branch = delivery
    files = ("hoard_link/hub/server.py", "shell/main.cjs")
    for name in files:
        write(branch, name, "changed\n")
    head = commit(branch, "Update running surfaces", *files)
    apps = [{"app": "window", "state": "deferred"}, {"app": "service", "state": "reloaded"}]
    assert [row["action"] for row in pending_reloads(repo, base, head,
        {"destination": "apps/agora/dist", "native": {"entry": "console.exe"}}, apps)] == [
            "reload_app", "restart_hub_when_idle", "reopen_hub_window", "reopen_native_console"]
    assert pending_reloads(repo, base, head, {"destination": "static/studio", "sha256": "build"}, [])[-1] == {
        "target": "studio", "action": "verify_spark_deployment", "sha256": "build"}


def test_test_environment_excludes_parent_hub_settings(monkeypatch):
    for key in ("AGORA_SPACE", "AGORA_AS", "HOARD_HUB_DATA_DIR", "HOARD_HUB_TOKEN_FILE", "PYTEST_ADDOPTS"):
        monkeypatch.setenv(key, "parent")
    env = checks.test_env()
    assert all(value != "parent" for value in env.values())
    assert env["HOARD_HUB_AUTOSTART"] == "0"


def test_compare_requires_the_same_failure_and_signature():
    known = {"signature": "same"}
    updated = {"signature": "different"}
    data = checks.compare({"failures": {"a": known, "b": known}}, {"failures": {"a": known, "b": updated, "c": known}})
    assert data["known"] == {"a": known}
    assert data["new"] == {"b": updated, "c": known}

def test_compare_flags_a_test_root_that_stops_running():
    def row(passed):
        return {"counts": {"passed": passed, "failed": 0, "skipped": 0}, "failures": {}}

    main = checks.combine({"editing_tests": row(10), "tests": row(93)})
    lost = checks.compare(main, checks.combine({"editing_tests": row(0), "tests": row(95)}))
    assert list(lost["new"]) == ["editing_tests::<no tests ran>"]
    fresh = checks.compare(checks.combine({"editing_tests": row(0), "tests": row(93)}),
                           checks.combine({"editing_tests": row(4), "tests": row(93)}))
    assert fresh["new"] == {}


def test_spark_request_executed_with_json_environment_and_git_diff(delivery, tmp_path):
    repo, base, head, worktree = delivery

    def ssh(command, *, input, timeout):
        assert json.loads(input)["timeout"] == 480
        assert timeout == 610
        _, *arguments = shlex.split(command)
        completed = subprocess.run([sys.executable, *arguments], input=input, encoding="utf-8",
                                   capture_output=True, timeout=timeout)
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    hub = SimpleNamespace(placement=SimpleNamespace(_ssh=ssh))
    result = checks.suite(hub, repo, worktree, ["tests/test_feature.py"], tmp_path / "report.xml",
                          "contract", remote=str(repo), base=base, ref=head, timeout=480)
    assert result["counts"]["passed"] == 1
    known = result["failures"]["tests.test_feature::test_known"]["message"]
    assert "at <address>" in known and "<pytest-temp>" in known and "<checkout>" in known


def test_catalogue_plans_the_hub_integration_handoff(tmp_path):
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)), apps=[])
    integration = IntegrationFacet(hub)
    hub.facet = lambda name: integration if name == "integration" else None
    try:
        result = ProductionFacet(hub).catalog({"from_type": "code_branch", "to_type": "integrated_code"})
        assert result["chains"][0][0]["app"] == "hoardhub"
        assert set(result["chains"][0][0]["check"]) == {"en", "es"}
    finally:
        integration.close()


def test_code_delivery_returns_incomplete_submission_before_review(delivery, tmp_path):
    repo, _, head, _ = delivery
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    try:
        task = agora.task_add({"agent": "worker", "title": "Code delivery", "labels": ["integration"]})["task"]
        args = {"agent": "worker", "task_id": task["id"]}
        agora.task_claim(args)
        summary = f"Repository: {repo.name}\nTest-Paths: tests/test_station.py"
        with pytest.raises(AgoraError, match="branch"):
            agora.task_submit({**args, "summary": summary, "commits": [head]})
        assert agora._task_row(task["id"])["submission_revision"] == 0
        result = agora.task_submit({**args, "summary": summary, "commits": [head], "branch": "delivery"})
        assert result["task"]["repo"] == repo.name
        assert result["task"]["status"] == "review"
    finally:
        agora.close()


def test_integration_finishes_approved_task_without_handover(delivery, tmp_path):
    _, _, head, _ = delivery
    agora = Agora(tmp_path / "agora.db")
    try:
        task = agora.task_add({"agent": "worker", "title": "Code delivery"})["task"]
        args = {"task_id": task["id"]}
        agora.task_claim({**args, "agent": "worker"})
        agora.task_submit({**args, "agent": "worker", "summary": "Delivery", "commits": [head]})
        agora.task_review({**args, "agent": "reviewer", "verdict": "approve",
                           "expected_submission_revision": 1, "body": "Verified."})
        with agora.db.tx():
            set_rank(agora, "worker", "support", "fixture")
            set_rank(agora, "integrator", "lead", "fixture")
        with pytest.raises(AgoraError, match="belongs"):
            agora.task_done({**args, "agent": "integrator", "_integration": "forged"})
        verified = {**args, "agent": "integrator", "_integration": INTEGRATION_TOKEN}
        with pytest.raises(AgoraError, match="current approved revision"):
            agora.task_done({**verified, "_integration_revision": 2})
        result = agora.task_done({**verified, "_integration_revision": 1})
        assert result["task"]["owner"] == "worker"
        assert result["task"]["status"] == "done"
    finally:
        agora.close()


def test_receipt_corrects_historic_fast_forward_and_can_undo(delivery, tmp_path):
    repo, base, head, _ = delivery
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    facet = IntegrationFacet(SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                                             facet=lambda name: SimpleNamespace(agora=agora)))
    try:
        task = agora.task_add({"agent": "owner", "title": "Two repositories", "claim": True})["task"]
        args = {"agent": "owner", "task_id": task["id"]}
        agora.task_submit({**args, "summary": "Delivery", "commits": ["other-head", head]})
        agora.task_review({**args, "agent": "reviewer", "verdict": "approve", "expected_submission_revision": 1,
                           "body": "Reviewed."})
        run = "a" * 32
        result = f"Integration {run}\nFirst repo own-merge\n{repo.name} {head}"
        closed = agora.task_done({**args, "commits": ["own-merge", head], "result": result,
                                  "force": True, "reason": "Merged the approved delivery"})["task"]
        path = facet._path(run)
        path.parent.mkdir(parents=True)
        raw = {"id": run, "task_id": task["id"], "agent": "owner", "space_id": agora.space_id,
               "state": "integrated", "revision": 1, "repositories": [
                   {"plan": {"repo_ref": "First repo"}, "merge_commit": "own-merge"},
                   {"plan": {"repo_ref": repo.name, "base": base, "head": head, "target": "main"},
                    "merge_commit": head, "baseline": {"commit": head}}],
               "close_result": {key: closed[key] for key in ("commits", "result", "closed_at")}}
        write_json_atomic(path, raw)
        merge = git.merge_candidate(repo, head)
        options = {"id": run, "agent": "verifier", "repo": repo.name, "commit": merge,
                   "reason": "Checked both parents and the mainline tree."}
        with pytest.raises(AgoraError, match="reason is required"):
            facet.reconcile({**options, "reason": " "})
        with pytest.raises(git.IntegrationError, match="replacement"):
            facet.reconcile({**options, "commit": head})
        corrected = facet.reconcile(options)
        rcpt = facet.status({"id": run})["receipt"]
        assert corrected["receipt"] == rcpt
        assert rcpt["repositories"][1]["merge_commit"] == rcpt["repositories"][1]["baseline"]["commit"] == merge
        assert rcpt["close_result"]["commits"] == ["own-merge", merge]
        assert rcpt["correction"]["agent"] == "verifier" and rcpt["correction"]["reason"] == options["reason"]
        updated = agora._task_row(task["id"])
        assert updated["commits"] == ["own-merge", merge]
        assert updated["reviewed_commits"] == ["other-head", head]
        assert updated["result"] == agora._thread_row(task["thread_id"])["resolution"] == result.replace(head, merge)
        thread = agora.thread({"thread_id": task["thread_id"]})
        assert thread["thread"]["resolved_by"] == "owner"
        assert thread["messages"][-1]["author"] == "verifier"
        assert options["reason"] in thread["messages"][-1]["body"]
        assert facet.reconcile({**options, "agent": "peer"}) == {"ok": True, "receipt": rcpt}
        with pytest.raises(AgoraError, match="belongs"):
            facet.reconcile({**corrected["undo"]["arguments"], "agent": "peer"})
        facet.reconcile(corrected["undo"]["arguments"])
        assert facet.status({"id": run})["receipt"] == raw
        assert agora._task_row(task["id"])["commits"] == ["own-merge", head]
        assert git.git_text(repo, "rev-parse", "HEAD") == merge
    finally:
        facet.close()
        agora.close()


def test_done_checks_manual_merge_parents_tree_and_mainline(delivery, tmp_path):
    repo, base, head, _ = delivery
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    try:
        task = agora.task_add({"agent": "owner", "title": "Manual merge", "repo": repo.name,
                               "paths": ["feature.py"], "claim": True})["task"]
        previous_locks = agora.locks()["locks"]
        opts = {"agent": "owner", "task_id": task["id"]}
        agora.task_submit({**opts, "branch": "delivery", "summary": "Review the branch", "commits": [head[:12]]})
        agora.task_review({**opts, "agent": "reviewer", "verdict": "approve", "expected_submission_revision": 1,
                           "body": "Reviewed."})
        with agora.db.tx():
            set_rank(agora, "integrator", "support", "fixture")
        verify = {**opts, "agent": "integrator", "result": "Checked the published merge and tree."}
        with git.worktree(repo, tmp_path / "manual", base) as work:
            unofficial = git.merge_candidate(work, head)
            with pytest.raises(AgoraError, match="not reviewed"):
                agora.task_done({**opts, "commits": [unofficial]})
            with pytest.raises(AgoraError, match="belongs"):
                agora.task_done({**verify, "commits": [unofficial], "force": True, "reason": "Override"})
        git.git(repo, "merge", "--no-ff", "--no-commit", head)
        write(repo, "unreviewed.txt", "Foreign content")
        contaminated = commit(repo, "Merge with extra changes", "unreviewed.txt")
        with pytest.raises(AgoraError, match="not reviewed"):
            agora.task_done({**opts, "commits": [contaminated]})
        git.git(repo, "reset", "--keep", base)
        merge = git.merge_candidate(repo, head)
        with pytest.raises(AgoraError, match="reason is required"):
            agora.task_done({**verify, "commits": [merge], "result": ""})
        response = agora.task_done({**verify, "commits": [merge]})
        result = response["task"]
        assert result["commits"] == [merge] and result["review_state"] == "equivalent"
        assert result["reviewed_commits"] == [head[:12]] and result["owner"] == "owner"
        assert result["reviewer"] == "reviewer" and result["result"] == verify["result"]
        assert agora._thread_row(task["thread_id"])["resolved_by"] == "integrator"
        assert not agora.locks()["locks"]
        undo = response["undo"]["arguments"]
        with pytest.raises(AgoraError, match="no longer matches"):
            agora.task_done({**undo, "agent": "peer"})
        lock = previous_locks[0]["resource"]
        agora.lock({"agent": "peer", "resources": [lock]})
        with pytest.raises(AgoraError, match="now in use"):
            agora.task_done(undo)
        assert agora._task_row(task["id"])["status"] == "done"
        agora.unlock({"agent": "peer", "resources": [lock]})
        restored = agora.task_done(undo)["task"]
        assert restored["status"] == "approved" and restored["commits"] == [head[:12]]
        assert restored["reviewer"] == "reviewer" and restored["result"] == task["result"]
        assert agora.locks()["locks"] == previous_locks
        assert git.git_text(repo, "rev-parse", "HEAD") == merge
        with pytest.raises(AgoraError, match="already undone"):
            agora.task_done(undo)
    finally:
        agora.close()


def test_integrate_and_undo_exact_approved_delivery(delivery, tmp_path, monkeypatch):
    repo, base, head, branch = delivery
    write(repo, ".git/info/exclude", "dist/\nnode_modules/\n*.tsbuildinfo\n")
    write(branch, "tests/__pycache__/test_feature.cpython-312.pyc", "regenerable")
    write(branch, "frontend/dist/assets/main.js", "compiled output")
    write(branch, "frontend/node_modules/package/index.js", "installed dependency")
    write(branch, "frontend/tsconfig.tsbuildinfo", "build metadata")
    staged, working = dirty_checkout(repo)
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                          facet=lambda name: SimpleNamespace(agora=agora))
    facet = IntegrationFacet(hub)
    monkeypatch.setattr(checks, "SUITE_LOCK", tmp_path / "local-slot")
    try:
        task = agora.task_add({"agent": "owner", "title": "Integrate feature", "repo": repo.name})["task"]
        agora.task_claim({"agent": "owner", "task_id": task["id"]})
        agora.task_submit({"agent": "owner", "task_id": task["id"], "branch": "delivery", "commits": [head],
                           "summary": "Test-Paths: tests/test_feature.py"})
        args = {"agent": "owner", "task_id": task["id"], "revision": 1, "merge": True,
                "live_test": "tests/test_feature.py::test_new"}
        with pytest.raises(AgoraError, match="requires approval"):
            facet.run(args)
        assert agora._task_row(task["id"])["status"] == "review"
        assert branch.exists()
        assert git.git_text(repo, "rev-parse", "HEAD") == base
        agora.task_review({"agent": "reviewer", "task_id": task["id"], "verdict": "approve",
                           "expected_submission_revision": 1, "body": "Reviewed exact branch."})
        result = facet.run(args)
        run = result["receipt"]["id"]
        deadline = time.monotonic() + RUN_WAIT_SECONDS
        while facet.active and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = facet.status({"id": run})["receipt"]
        assert len(receipt["comparison"]["known"]) == 1
        assert receipt["comparison"]["new"] == {}
        assert receipt["comparison"]["candidate"]["counts"]["passed"] == 1
        assert receipt["state"] == "integrated", receipt
        assert not branch.exists()
        merge = receipt["merge_commit"]
        assert git.git_text(repo, "rev-list", "--parents", "-n", "1", "HEAD").split() == [merge, base, head]
        assert agora._task_row(task["id"])["status"] == "done"
        assert facet.undo({"id": run, "agent": "owner"})["receipt"]["state"] == "undone"
        assert branch.exists()
        assert git.git_text(repo, "rev-parse", "HEAD") == base
        assert (repo / "feature.py").read_text() == working
        assert git.git_text(repo, "show", ":feature.py") == staged.strip()
        assert agora._task_row(task["id"])["status"] == "approved"
    finally:
        facet.close()
        agora.close()


def test_same_repository_deliveries_wait_and_replan_on_the_new_main(delivery, tmp_path, monkeypatch):
    from threading import Event

    repo, base, first_head, _ = delivery
    second = tmp_path / "next"
    git.git(repo, "worktree", "add", "-b", "next", str(second), base)
    write(second, "second.py", "VALUE = 2\n")
    second_head = commit(second, "Add second feature", "second.py")
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    launch = SimpleNamespace(cwd=str(repo))
    app = SimpleNamespace(id="fixture", kind="app", launch=launch)
    desktop = SimpleNamespace(id="desktop", kind="window-app", launch=launch)
    browser = SimpleNamespace(id="browser", kind="app", launch=launch)
    reloaded = []

    def restart(app_id, *, automatic):
        assert app_id == app.id and automatic
        reloaded.append(git.git_text(repo, "rev-parse", "HEAD"))
        return {"ok": True, "ready": True, "pid": len(reloaded)}

    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)), apps=[app, desktop, browser],
                          app_status=lambda app: {"state": "running", "stoppable": True,
                                                  "windows": [{"pid": 4}] if app.id == "browser" else []}, restart=restart,
                          automation_blocked=lambda app_id: False, facet=lambda name: SimpleNamespace(agora=agora))
    facet = IntegrationFacet(hub)
    monkeypatch.setattr(checks, "SUITE_LOCK", tmp_path / "local-slot")
    first_started, release = Event(), Event()
    original = facet._run_checks

    def checks_with_pause(*args, **kwargs):
        if not first_started.is_set():
            first_started.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(facet, "_run_checks", checks_with_pause)

    def submit(branch, head):
        task = agora.task_add({"agent": "owner", "title": branch, "repo": repo.name})["task"]
        agora.task_claim({"agent": "owner", "task_id": task["id"]})
        args = {"agent": "owner", "task_id": task["id"]}
        agora.task_submit({**args, "branch": branch, "commits": [head], "summary": "Test-Paths: tests/test_station.py"})
        agora.task_review({"agent": "reviewer", "task_id": task["id"], "verdict": "approve",
                           "expected_submission_revision": 1, "body": "Reviewed exact branch."})
        return {**args, "revision": 1, "merge": True}

    try:
        first = submit("delivery", first_head)
        next_args = submit("next", second_head)
        case = "tests/test_station.py::test_station"
        assert facet.inspect(next_args)["missing_live_tests"] == [repo.name]
        next_args["live_tests"] = {repo.name: case}
        assert facet.inspect(next_args)["plan"]["live_test"] == case
        resource = f"merge:{repo.name}"
        assert agora.lock({"agent": "manual", "resources": [resource]})["ok"]
        first_id = facet.run({**first, "live_test": case})["receipt"]["id"]
        deadline = time.monotonic() + 10
        while facet.status({"id": first_id})["receipt"]["state"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert facet.status({"id": first_id})["receipt"]["state"] == "waiting_for_merge_lock"
        extra = "tests/test_still_known.py"
        write(repo, extra, "def test_known_on_main(): assert False\n")
        advanced = commit(repo, "Advance main under manual lock", extra)
        write_json_atomic(facet._baseline(repo), {"local": {"commit": advanced, "failures": {
            "previous-test": {"file": extra.replace("/", "\\"), "signature": "old"},
            "missing-test": {"file": "deleted.py"}
        }}})
        agora.unlock({"agent": "manual", "resources": [resource]})
        assert first_started.wait(10)
        second_id = facet.run(next_args)["receipt"]["id"]
        assert facet.status({"id": second_id})["receipt"]["state"] == "queued"
        release.set()
        deadline = time.monotonic() + RUN_WAIT_SECONDS
        while facet.active and time.monotonic() < deadline:
            time.sleep(0.05)
        one, two = (facet.status({"id": run})["receipt"] for run in (first_id, second_id))
        assert one["state"] == "integrated", one
        assert two["state"] == "integrated", two
        assert one["plan"]["base"] == advanced
        assert one["plan"]["baseline_files"] == [extra]
        assert {row["file"] for row in one["baseline"]["failures"].values()} == {extra, "tests/test_feature.py"}
        assert two["plan"]["base"] == one["merge_commit"]
        assert two["plan"]["baseline_files"] == [extra, "tests/test_feature.py"]
        assert two["app_reload"][0]["state"] == "reloaded"
        assert [(row["app"], row["state"], row["reason_code"]) for row in two["app_reload"][1:]] == [
            (name, "deferred", "desktop_in_use") for name in ("desktop", "browser")]
        assert [row["target"] for row in two["reload_pending"]] == ["desktop", "browser"]
        assert reloaded == [one["merge_commit"], two["merge_commit"]]
        undo = facet.undo({"id": second_id, "agent": "owner"})["receipt"]
        assert undo["undo_app_reload"][0]["state"] == "reloaded"
        assert undo["undo_app_reload"][1:] == two["app_reload"][1:]
        assert reloaded[-1] == one["merge_commit"]
    finally:
        release.set()
        facet.close()
        agora.close()


@pytest.mark.parametrize("regression", [False, True])
def test_multi_repository_delivery_has_one_receipt_and_undo(delivery, tmp_path, monkeypatch, regression):
    first, base, head, tree = delivery
    second, second_tree = tmp_path / "Second Repo", tmp_path / "second-delivery"
    second.mkdir()
    git.git(second, "init", "-b", "main")
    git.git(second, "config", "user.name", "Luissalet")
    git.git(second, "config", "user.email", "luissalet@users.noreply.github.com")
    file = "tests/test_other.py"
    write(second, file, "def test_ready(): assert True\n")
    second_base = commit(second, "Create other baseline", file)
    git.git(second, "worktree", "add", "-b", "delivery", str(second_tree))
    write(second_tree, file, f"def test_ready(): assert {not regression}\n\ndef test_other(): assert True\n")
    second_head = commit(second_tree, "Deliver another feature", file)
    repos = {repo.name: repo for repo in (first, second)}
    agora = Agora(tmp_path / "agora.db", repo_lookup=repos.get)
    agora.registered_repositories = lambda: [{"name": name, "path": str(repo)} for name, repo in repos.items()]
    facet = IntegrationFacet(SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                                             facet=lambda name: SimpleNamespace(agora=agora)))
    monkeypatch.setattr(checks, "SUITE_LOCK", tmp_path / "local-slot")
    try:
        task = agora.task_add({"agent": "owner", "title": "Integrate two repos", "labels": ["integration"]})["task"]
        args = {"agent": "owner", "task_id": task["id"]}
        agora.task_claim(args)
        paths = f"tests/test_feature.py, tests/test_station.py, {file}"
        summary = f"Test-Paths: {paths}\nLive-Test: {first.name}/tests/test_feature.py::test_new"
        with pytest.raises(AgoraError, match="tests/test_missing.py"):
            agora.task_submit({**args, "branch": "delivery", "commits": [head[:12], second_head],
                               "summary": summary + "\nTest-Paths: tests/test_missing.py"})
        agora.task_submit({**args, "branch": "delivery", "commits": [head[:12], second_head], "summary": summary})
        agora.task_review({**args, "agent": "reviewer", "verdict": "approve",
                           "expected_submission_revision": 1, "body": "Reviewed both branches."})
        args["revision"] = 1
        selected = facet.inspect({**args, "repo": second.name})
        assert selected["plan"]["commits"] == [second_head]
        assert set(selected["repositories"]) == set(repos)
        assert selected["missing_live_tests"] == [second.name]
        assert selected["post_merge_checks"][second.name] == [file]
        plans = facet.inspect(args)["plans"]
        assert [p["head"] for p in plans] == [head, second_head]
        assert plans[0]["test_paths"] == ["tests/test_feature.py", "tests/test_station.py"]
        assert "tests/test_station.py" in plans[0]["tests"]["contract"]
        assert [p["live_test"] for p in plans] == ["tests/test_feature.py::test_new", None]
        run = facet.run({**args, "merge": True})["receipt"]["id"]
        deadline = time.monotonic() + RUN_WAIT_SECONDS
        while facet.active and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = facet.status({"id": run})["receipt"]
        assert len(receipt["repositories"]) == 2
        if regression:
            assert receipt["state"] == "regressions", receipt
            assert receipt["repositories"][0]["state"] == "rolled_back"
        else:
            assert receipt["state"] == "integrated", receipt
            assert receipt["repositories"][1]["live"]["counts"]["passed"] == 2
            merges = [row["merge_commit"] for row in receipt["repositories"]]
            assert git.git_text(first, "rev-list", "--parents", "-n", "1", "HEAD").split() == [merges[0], base, head]
            assert git.git_text(second, "rev-list", "--parents", "-n", "1", "HEAD").split() == [
                merges[1], second_base, second_head]
            assert agora._task_row(task["id"])["commits"] == merges
            assert not tree.exists() and not second_tree.exists()
            assert facet.undo({"id": run, "agent": "owner"})["receipt"]["state"] == "undone"
        assert tree.exists() and second_tree.exists()
        assert git.git_text(first, "rev-parse", "HEAD") == base
        assert git.git_text(second, "rev-parse", "HEAD") == second_base
        assert agora._task_row(task["id"])["status"] == "approved"
    finally:
        facet.close()
        agora.close()


def test_integration_infers_repo_batches_tests_and_reports_retained_delivery(delivery, tmp_path, monkeypatch):
    repo, base, _, branch = delivery
    write(repo, "main.txt", "main advanced\n")
    main = commit(repo, "Advance main", "main.txt")
    paths = [f"tests/test_feature_{n:02}.py" for n in range(41)]
    for name in paths:
        write(branch, name, "def test_delivery(): assert True\n")
    head = commit(branch, "Add delivery checks", *paths)
    write(repo, ".git/info/exclude", "*.keep\n")
    write(branch, "private.keep", "preserve\n")
    write(branch, "tests/__pycache__/test_feature.cpython-312.pyc", "retained with the rest")
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    agora.registered_repositories = lambda: [{"name": repo.name, "path": str(repo)}]
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                          facet=lambda name: SimpleNamespace(agora=agora))
    facet = IntegrationFacet(hub)
    monkeypatch.setattr(checks, "SUITE_LOCK", tmp_path / "local-slot")
    try:
        task = agora.task_add({"agent": "owner", "title": "Integrate feature"})["task"]
        args = {"agent": "owner", "task_id": task["id"]}
        agora.task_claim(args)
        agora.task_submit({**args, "branch": "delivery", "commits": [head],
                           "summary": "Test-Paths: " + " ".join(paths)})
        agora.task_review({**args, "agent": "reviewer", "verdict": "approve",
                           "expected_submission_revision": 1, "body": "Reviewed exact branch."})
        plan = facet.inspect({**args, "revision": 1})["plan"]
        assert plan["repo_ref"] == repo.name and plan["base"] == main
        assert len(plan["tests"]["contract"]) == 42
        assert facet.inspect({**args, "revision": 1})["missing_live_tests"] == [repo.name]
        run = facet.run({**args, "revision": 1, "merge": True,
                          "live_test": f"{paths[-1]}::test_delivery"})["receipt"]["id"]
        deadline = time.monotonic() + RUN_WAIT_SECONDS
        while facet.active and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = facet.status({"id": run})["receipt"]
        assert receipt["state"] == "integrated", receipt
        assert receipt["comparison"]["candidate"]["counts"]["passed"] == 42
        assert len(receipt["comparison"]["candidate"]["roots"]) == 2
        assert any(event["state"] == "delivery_retained" for event in receipt["events"])
        assert facet._summary(receipt)["cleanup"]["retained"]
        assert git.git_text(repo, "log", "-1", "--format=%s") == "Merge delivery: Add delivery checks"
        assert (branch / "private.keep").read_text().strip() == "preserve"
        assert (branch / "tests/__pycache__/test_feature.cpython-312.pyc").is_file()
        assert facet.undo({**args, "id": run})["receipt"]["state"] == "undone"
        assert git.git_text(repo, "rev-parse", "HEAD") == main
        assert git.ancestor(repo, base, main)
    finally:
        facet.close()
        agora.close()


@pytest.mark.parametrize("declared", [True, False])
def test_transferred_delivery_compares_independent_conftests(delivery, tmp_path, monkeypatch, declared):
    repo, _, _, branch = delivery
    git.git(repo, "branch", "earlier-delivery", "delivery")
    write(repo, "tests/conftest.py", "MAIN_NAME = 'main'\n")
    write(repo, "editing_tests/conftest.py", "EDIT_NAME = 'editor'\n")
    write(repo, "tests/test_api.py", "from conftest import MAIN_NAME\ndef test_api(): assert MAIN_NAME == 'main'\n")
    write(repo, checks.CONFIG, '{"contract_timeout_seconds": 600}')
    write(repo, "editing_tests/test_api.py",
          "from conftest import EDIT_NAME\ndef test_api(): assert EDIT_NAME == 'editor'\n")
    base = commit(repo, "Add independent pytest roots", "tests/conftest.py", "editing_tests/conftest.py",
                  "tests/test_api.py", "editing_tests/test_api.py", checks.CONFIG)
    git.git(branch, "-c", "user.name=Luissalet", "-c", "user.email=luissalet@users.noreply.github.com",
            "merge", "--no-edit", "main")
    write(branch, "api.py", "enabled = True\n")
    write(branch, checks.CONFIG, '{"contract_timeout_seconds": 1}')
    test_path = "editing_tests/test_api.py"
    if declared:
        write(branch, test_path, "from conftest import EDIT_NAME\ndef test_api(): assert EDIT_NAME.startswith('edit')\n")
    head = commit(branch, "Deliver API change", "api.py", test_path, checks.CONFIG)
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                          facet=lambda name: SimpleNamespace(agora=agora))
    facet = IntegrationFacet(hub)
    monkeypatch.setattr(checks, "SUITE_LOCK", tmp_path / "local-slot")
    run_local = checks.local_tests
    timeouts = []

    def record_timeout(repo, work, files, report, lane, basetemp, *, timeout):
        timeouts.append((lane, timeout))
        return run_local(repo, work, files, report, lane, basetemp, timeout=timeout)

    monkeypatch.setattr(checks, "local_tests", record_timeout)
    try:
        task = agora.task_add({"agent": "author", "title": "API delivery", "repo": repo.name})["task"]
        args = {"task_id": task["id"]}
        agora.task_claim({**args, "agent": "author"})
        ago = git.git_text(repo, "rev-parse", "earlier-delivery")
        agora.task_submit({**args, "agent": "author", "branch": "earlier-delivery", "commits": [ago],
                           "summary": "Test-Paths: tests/test_feature.py"})
        agora.task_submit({**args, "agent": "author", "branch": "delivery", "commits": [head],
                           "summary": f"Test-Paths: {test_path}" if declared else "Historic delivery"})
        agora.task_review({**args, "agent": "reviewer", "verdict": "approve",
                           "expected_submission_revision": 2, "body": "Reviewed current delivery."})
        agora.heartbeat({"agent": "integrator"})
        agora.handover({"agent": "author", "from_agent": "author", "to_agent": "integrator",
                        "tasks": [task["id"]], "reason": "Hand over the approved revision."})
        args.update(agent="integrator", revision=2)
        assert facet.inspect(args)["plan"]["tests"]["contract"]
        with pytest.raises(git.IntegrationError, match="Invalid pytest path"):
            facet.inspect({**args, "test_paths": ["../test_api.py"]})
        plan = facet.inspect({**args, "test_paths": ["tests/test_api.py"]})["plan"]
        assert plan["contract_timeout_seconds"] == 600
        assert plan["test_paths"] == ([test_path, "tests/test_api.py"] if declared else ["tests/test_api.py"])
        assert plan["test_paths_source"] == ("submission_and_integrator" if declared else "integrator")
        assert plan["tests"]["contract"] == [test_path, "tests/test_api.py", "tests/test_feature.py"]
        run = facet.run({**args, "test_paths": ["tests/test_api.py"], "merge": True,
                         "live_test": f"{test_path}::test_api"})["receipt"]["id"]
        deadline = time.monotonic() + RUN_WAIT_SECONDS
        while facet.active and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = facet.status({"id": run})["receipt"]
        assert receipt["state"] == "integrated", receipt
        assert receipt["comparison"]["roots"]["editing_tests"]["candidate"]["counts"]["passed"] == 1
        assert receipt["comparison"]["roots"]["tests"]["main"]["counts"]["passed"] == 1
        assert timeouts == [("contract", 600)] * 4 + [("live", 600)]
        assert len(receipt["comparison"]["roots"]["tests"]["known"]) == 1
        assert git.git_text(repo, "rev-list", "--parents", "-n", "1", "HEAD").split() == [
            receipt["merge_commit"], base, head]
        assert agora._task_row(task["id"])["status"] == "done"
        assert facet.undo({"id": run, "agent": "integrator"})["receipt"]["state"] == "undone"
        assert git.git_text(repo, "rev-parse", "HEAD") == base
    finally:
        facet.close()
        agora.close()
