import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hoard_link.hub import integration_checks as checks
from hoard_link.hub import integration_git as git
from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.agora_ranks import set_rank
from hoard_link.hub.integration import INTEGRATION_TOKEN, IntegrationFacet, delivery_contract, submission_tests
from hoard_link.hub.production import ProductionFacet


LINES = "\n".join(f"line {i}" for i in range(60)) + "\n"


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
    write(repo, "tests/test_feature.py", """
import pytest

def test_known():
    assert False, "stable reproduction"

@pytest.mark.inference
def test_model():
    pytest.fail("The contract lane must never run this")
""")
    base = commit(repo, "Create baseline", "feature.py", "other.txt", "tests/test_feature.py")
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


def test_delivery_contract_requires_a_named_tip_and_tracked_test(delivery):
    repo, _, head, _ = delivery
    path = "tests/test_feature.py"
    assert submission_tests(f"Resumen\n\nTest-Paths: {path}, {path}\n") == [path]
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
        _, *arguments = shlex.split(command)
        completed = subprocess.run([sys.executable, *arguments], input=input, encoding="utf-8",
                                   capture_output=True, timeout=timeout)
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    hub = SimpleNamespace(placement=SimpleNamespace(_ssh=ssh))
    result = checks.suite(hub, repo, worktree, ["tests/test_feature.py"], tmp_path / "report.xml",
                          "contract", remote=str(repo), base=base, ref=head)
    assert result["counts"]["passed"] == 1
    assert list(result["failures"]) == ["tests.test_feature::test_known"]


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
        summary = f"Repository: {repo.name}\nTest-Paths: tests/test_feature.py"
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


def test_integrate_and_undo_exact_approved_delivery(delivery, tmp_path, monkeypatch):
    repo, base, head, branch = delivery
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
        agora.task_review({"agent": "reviewer", "task_id": task["id"], "verdict": "approve",
                           "expected_submission_revision": 1, "body": "Reviewed exact branch."})
        result = facet.run({"agent": "owner", "task_id": task["id"], "revision": 1, "merge": True,
                            "live_test": "tests/test_feature.py::test_new"})
        run = result["receipt"]["id"]
        deadline = time.monotonic() + 35
        while facet.active and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = facet.status({"id": run})["receipt"]
        assert receipt["comparison"]["known"]
        assert receipt["comparison"]["candidate"]["counts"]["passed"] == 1
        assert receipt["state"] == "integrated", receipt
        assert not branch.exists()
        assert git.git_text(repo, "rev-parse", "HEAD") == head
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


@pytest.mark.parametrize("declared", [True, False])
def test_transferred_delivery_compares_independent_conftests(delivery, tmp_path, monkeypatch, declared):
    repo, _, _, branch = delivery
    git.git(repo, "branch", "earlier-delivery", "delivery")
    write(repo, "tests/conftest.py", "MAIN_NAME = 'main'\n")
    write(repo, "editing_tests/conftest.py", "EDIT_NAME = 'editor'\n")
    write(repo, "tests/test_api.py", "from conftest import MAIN_NAME\ndef test_api(): assert MAIN_NAME == 'main'\n")
    write(repo, "editing_tests/test_api.py",
          "from conftest import EDIT_NAME\ndef test_api(): assert EDIT_NAME == 'editor'\n")
    base = commit(repo, "Add independent pytest roots", "tests/conftest.py", "editing_tests/conftest.py",
                  "tests/test_api.py", "editing_tests/test_api.py")
    git.git(branch, "-c", "user.name=Luissalet", "-c", "user.email=luissalet@users.noreply.github.com",
            "merge", "--no-edit", "main")
    write(branch, "api.py", "enabled = True\n")
    test_path = "editing_tests/test_api.py"
    if declared:
        write(branch, test_path, "from conftest import EDIT_NAME\ndef test_api(): assert EDIT_NAME.startswith('edit')\n")
    head = commit(branch, "Deliver API change", "api.py", test_path)
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                          facet=lambda name: SimpleNamespace(agora=agora))
    facet = IntegrationFacet(hub)
    monkeypatch.setattr(checks, "SUITE_LOCK", tmp_path / "local-slot")
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
        if not declared:
            with pytest.raises(git.IntegrationError, match="provide tracked pytest"):
                facet.inspect(args)
            with pytest.raises(git.IntegrationError, match="Invalid pytest path"):
                facet.inspect({**args, "test_paths": ["../test_api.py"]})
        plan = facet.inspect({**args, "test_paths": ["tests/test_api.py"]})["plan"]
        assert plan["test_paths"] == [test_path if declared else "tests/test_api.py"]
        assert plan["test_paths_source"] == ("submission" if declared else "integrator")
        assert plan["tests"]["contract"] == [test_path, "tests/test_api.py", "tests/test_feature.py"]
        run = facet.run({**args, "test_paths": ["tests/test_api.py"], "merge": True,
                         "live_test": f"{test_path}::test_api"})["receipt"]["id"]
        deadline = time.monotonic() + 35
        while facet.active and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = facet.status({"id": run})["receipt"]
        assert receipt["state"] == "integrated", receipt
        assert receipt["comparison"]["roots"]["editing_tests"]["candidate"]["counts"]["passed"] == 1
        assert receipt["comparison"]["roots"]["tests"]["main"]["counts"]["passed"] == 1
        assert len(receipt["comparison"]["roots"]["tests"]["known"]) == 1
        assert git.git_text(repo, "rev-parse", "HEAD") == head
        assert agora._task_row(task["id"])["status"] == "done"
        assert facet.undo({"id": run, "agent": "integrator"})["receipt"]["state"] == "undone"
        assert git.git_text(repo, "rev-parse", "HEAD") == base
    finally:
        facet.close()
        agora.close()
