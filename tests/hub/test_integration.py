import time
from types import SimpleNamespace

import pytest

from hoard_link.hub import integration_checks as checks
from hoard_link.hub import integration_git as git
from hoard_link.hub.agora import Agora
from hoard_link.hub.integration import IntegrationFacet, delivery_contract, submission_tests


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
    git.move_checkout(repo, base, head, scratch, lambda state, data: events.append((state, data)))
    assert [event[0] for event in events] == ["prepared_checkout", "moved_checkout", "restored_checkout"]
    assert events[0][1]["parked"] == ["feature.py"]
    assert git.git_text(repo, "show", ":feature.py") == staged.replace("line 2\n", "feature\n").strip()
    assert (repo / "feature.py").read_text() == working.replace("line 2\n", "feature\n")
    assert git.git_text(repo, "show", ":other.txt") == "foreign outside"
    assert (repo / "draft.txt").read_text() == "untracked\n"
    git.move_checkout(repo, head, base, scratch, lambda *_: None)
    assert (repo / "feature.py").read_text() == working
    assert git.git_text(repo, "show", ":feature.py") == staged.strip()


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
