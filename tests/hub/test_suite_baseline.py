from hoard_link.atomic import write_json_atomic
from hoard_link.hub.suite_baseline import SuiteBaseline, closure_report, focal_failures, import_registry, runner_failed


def test_imports_comparison_without_inventing_dates_and_preserves_first_observation(tmp_path, monkeypatch):
    source = tmp_path / "registry.json"
    comparison = tmp_path / "comparison.csv"
    monkeypatch.setenv("HOARD_HUB_SUITE_BASELINE", str(source))
    monkeypatch.setenv("HOARD_HUB_SUITE_COMPARISON", str(comparison))
    first = "2026-11-21T12:00:00+00:00"
    latest = "2026-11-22T12:00:00+00:00"
    repo = {"name": "Example", "commit": "a" * 40, "status": "failed",
            "results": {"tests": {"status": "failed", "started_at": first, "issues": [
                {"case": "tests.test_example.Check::test_red", "kind": "failure"},
                {"case": "tests.test_example::test_fixed", "kind": "failure"},
                {"case": "tests.test_example::test_skipped", "kind": "skipped"},
            ]}}}
    write_json_atomic(source, {"repositories": [repo]})
    comparison.write_text(
        "repo,test,local_sha,local_code,historical_commit,local_matched_node\n"
        f"Example,tests/test_example.py::Check::test_red,{'b' * 40},fail,aaaaaaa,\n"
        f"Example,tests/test_example.py::test_fixed,{'b' * 40},pass,aaaaaaa,\n",
        encoding="utf-8",
    )
    baseline = SuiteBaseline(tmp_path)
    row, = baseline.read("example")
    assert row["failures"] == [{"test": "tests/test_example.py::Check::test_red",
                               "commit": "b" * 40, "first_seen": first, "last_seen": None}]
    assert baseline.read("missing") == []
    repo["commit"] = "c" * 40
    repo["results"]["tests"]["started_at"] = latest
    repo["results"]["tests"]["issues"].pop(1)
    write_json_atomic(source, {"repositories": [repo]})
    row, = baseline.read()
    assert row["failures"] == [{"test": "tests.test_example.Check::test_red",
                               "commit": "c" * 40, "first_seen": first, "last_seen": latest}]
    issues = repo["results"]["tests"].pop("issues")
    write_json_atomic(source, {"repositories": [repo]})
    assert baseline.read()[0]["failures"] == []
    repo["results"]["tests"]["issues"] = issues
    write_json_atomic(source, {"repositories": [repo]})
    assert SuiteBaseline(tmp_path).read() == [row]
    source.unlink()
    assert SuiteBaseline(tmp_path).read() == [row]


def test_partial_snapshot_keeps_conclusive_reds_from_the_associated_comparison():
    current, older, compared = "a" * 40, "b" * 40, "c" * 40
    registry = {"repositories": [{"name": "Example", "commit": current, "status": "partial"}]}
    rows = [
        {"repo": "Example", "test": "tests/test_focal.py::test_now_green", "historical_commit": current,
         "local_sha": compared, "local_code": "pass"},
        {"repo": "Example", "test": "tests/test_focal.py::test_still_red", "historical_commit": older,
         "local_sha": compared, "local_code": "fail"},
    ]
    row = import_registry(registry, rows)["example"]
    assert row["measured"]
    assert list(row["failures"].values()) == [{"test": "tests/test_focal.py::test_still_red",
                                              "commit": compared, "first_seen": None, "last_seen": None}]
    registry["repositories"][0]["commit"] = "d" * 40
    assert import_registry(registry, rows)["example"]["failures"] == {}


def test_classifies_only_declared_failures_and_preserves_parameter_ids():
    baseline = {"failures": [{"test": "tests/test_file.py::Check::test_old[a/b]"}]}
    result = {"roots": {"tests": {}, "other": {}}, "failures": {
        "tests/tests.test_file.Check::test_old[a/b]": {"file": "tests/test_file.py"},
        "tests/tests.test_file::test_new": {"file": None},
        "other/other.test_file::test_elsewhere": {"file": "other/test_file.py"},
    }}
    cases = focal_failures(result, ["tests/test_file.py"], baseline)
    assert closure_report("Example", cases) == {"repo": "Example", "new": ["tests.test_file::test_new"],
                                               "known": ["tests.test_file.Check::test_old[a/b]"]}
    assert not runner_failed({"runner_error": "setup error", "case_errors": [
        "tests/tests.test_file.Check::test_old[a/b]"]}, cases)
    assert runner_failed({"runner_error": "setup error", "case_errors": ["tests/tests.test_file::test_new"]}, cases)
    assert runner_failed({"runner_error": "collection error"}, cases)
