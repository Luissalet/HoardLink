import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.suite_baseline import junit


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "suite_baseline.py"


def git(cwd, *args):
    return subprocess.check_output(["git", "-C", cwd, *args], text=True).strip()


def create_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    (path / "test_example.py").write_text(
        "import os\n"
        "import pytest\n"
        "def test_pass(): assert os.environ['BASELINE_TEST_SETTING'] == 'from registry'\n"
        "def test_fail(): assert False, 'baseline example'\n"
        "@pytest.mark.skip(reason='example of a skip')\n"
        "def test_skip(): pass\n",
        encoding="utf-8",
    )
    git(path, "add", "test_example.py")
    git(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Initial")
    return git(path, "rev-parse", "HEAD")


def invoke(path, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, path, *args], capture_output=True, text=True, check=False,
    )


def pytest_batch(name, selector):
    return {
        "name": name,
        "command": [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--junitxml={report}", "--basetemp={artifacts}/tmp", "-k", selector,
        ],
    }


def test_pytest_junit_includes_passed_subtests_in_its_suite_total(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites name="pytest tests"><testsuite tests="5" failures="1" errors="0" skipped="1">'
        '<testcase name="with_subtests"/><testcase name="failed"><failure/></testcase>'
        '<testcase name="skipped"><skipped/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )

    counts, issues = junit(report)

    assert counts == {"tests": 5, "passed": 3, "failed": 1, "errors": 0, "skipped": 1}
    assert [issue["case"] for issue in issues] == ["failed"]


def test_node_junit_counts_nested_tests_and_ignores_expected_todo_failures(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testcase name="standalone"/><testcase name="later">'
        '<skipped type="todo"/><failure message="expected"/></testcase>'
        '<testsuite name="parent"><testcase name="child"><failure message="actual"/></testcase>'
        '</testsuite><!-- tests 4 --><!-- suites 0 --><!-- pass 1 --><!-- fail 2 -->'
        '<!-- cancelled 0 --><!-- skipped 0 --><!-- todo 1 --></testsuites>',
        encoding="utf-8",
    )

    counts, issues = junit(report)

    assert counts == {"tests": 4, "passed": 1, "failed": 2, "errors": 0, "skipped": 1}
    assert [(issue["case"], issue["message"]) for issue in issues] == [("child", "actual")]


@pytest.mark.parametrize("report_xml", [
    '<testsuites><testsuite name="empty" tests="0"/></testsuites>',
    '<testsuites><testcase name="runner"/><!-- tests 0 --><!-- suites 0 --><!-- pass 0 -->'
    '<!-- fail 0 --><!-- cancelled 0 --><!-- skipped 0 --><!-- todo 0 --></testsuites>',
])
def test_empty_report_makes_a_successful_process_incomplete(tmp_path, report_xml):
    repo = tmp_path / "example"
    commit = create_repo(repo)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"repositories": [{
        "name": "Example", "cwd": str(repo), "ref": commit,
        "batches": [{"name": "empty", "command": [
            sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
            "{report}", report_xml,
        ]}],
    }]}), encoding="utf-8")

    result = invoke(path)

    assert result.returncode == 1, result.stderr
    entry = json.loads(path.read_text(encoding="utf-8"))["repositories"][0]
    assert entry["status"] == "incomplete"
    assert entry["results"]["empty"]["exit_code"] == 0
    assert "no test cases" in entry["results"]["empty"]["problem"]


def test_reports_exact_command_results_and_revision_across_batches(tmp_path):
    repo = tmp_path / "worktree with spaces"
    commit = create_repo(repo)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"repositories": [{
        "name": "Example", "cwd": str(repo), "ref": commit,
        "env": {"BASELINE_TEST_SETTING": "from registry", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        "batches": [pytest_batch("first", "not test_fail"), pytest_batch("second", "test_fail")],
    }]}), encoding="utf-8")

    result = invoke(path, "--repo", "Example", "--batch", "first")
    assert result.returncode == 0, result.stderr
    first = json.loads(path.read_text(encoding="utf-8"))["repositories"][0]
    assert first["status"] == "partial"
    assert first["counts"] == {"tests": 2, "passed": 1, "failed": 0, "errors": 0, "skipped": 1}
    assert first["commit"] == commit

    result = invoke(path, "--repo", "Example", "--resume")
    assert result.returncode == 1, result.stderr
    registry = json.loads(path.read_text(encoding="utf-8"))
    entry = registry["repositories"][0]
    assert entry["status"] == "failed"
    assert entry["counts"] == {"tests": 3, "passed": 1, "failed": 1, "errors": 0, "skipped": 1}
    assert entry["results"]["first"] == first["results"]["first"]
    second = entry["results"]["second"]
    assert second["exit_code"] == 1
    assert second["seconds"] > 0
    assert second["cwd"] == str(repo.resolve())
    assert f"--junitxml={second['report']}" in second["command"]
    assert Path(second["report"]).is_file()
    assert second["issues"][0]["case"] == "test_example::test_fail"
    assert "test_example::test_fail" in path.with_suffix(".md").read_text(encoding="utf-8")

    result = invoke(path, "--repo", "Example", "--batch", "first")
    assert result.returncode == 0, result.stderr
    registry = json.loads(path.read_text(encoding="utf-8"))
    entry = registry["repositories"][0]
    assert entry["status"] == "failed"
    assert entry["results"]["second"] == second

    entry["env"]["BASELINE_TEST_SETTING"] = "changed"
    path.write_text(json.dumps(registry), encoding="utf-8")
    result = invoke(path, "--repo", "Example", "--batch", "second")
    assert result.returncode == 1, result.stderr
    entry = json.loads(path.read_text(encoding="utf-8"))["repositories"][0]
    assert entry["status"] == "partial"
    assert set(entry["results"]) == {"second"}


def test_missing_report_and_wrong_revision_do_not_yield_a_pass(tmp_path):
    repo = tmp_path / "example"
    commit = create_repo(repo)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"repositories": [
        {
            "name": "Wrong revision", "cwd": str(repo), "ref": "no-such-ref",
            "batches": [pytest_batch("first", "test_pass")],
        },
        {
            "name": "No report", "cwd": str(repo), "ref": commit,
            "batches": [{
                "name": "first", "command": [sys.executable, "-c", "print('35 passed')", "{report}"],
            }],
        },
    ]}), encoding="utf-8")
    result = invoke(path)
    assert result.returncode == 1, result.stderr
    wrong, missing = json.loads(path.read_text(encoding="utf-8"))["repositories"]
    assert wrong["status"] == "incomplete"
    assert not wrong["results"]
    assert missing["status"] == "incomplete"
    assert missing["counts"] is None
    assert missing["results"]["first"]["exit_code"] == 0
    assert "No usable JUnit report" in missing["results"]["first"]["problem"]
