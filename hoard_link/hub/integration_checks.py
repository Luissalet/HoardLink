"""Focal test selection, paired baseline runs and optional Spark execution."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .integration_git import IntegrationError, diff_bytes, git, test_path


SUITE_LOCK = Path(tempfile.gettempdir()) / "hoard-integration-test-slot.json"
MARKERS = "not (inference or live_model or gpu)"
CONFIG = ".hoard-integration.json"
TIMEOUT = 300
EXCLUDED_ENV = ("AGORA_", "HOARD_HUB_", "PYTEST_", "GIT_DIR", "GIT_WORK_TREE",
                "GIT_INDEX_FILE", "GIT_COMMON_DIR", "PYTHONPATH", "VIRTUAL_ENV")


def config_at(repo, ref):
    raw = git(repo, "show", f"{ref}:{CONFIG}", check=False)
    if raw.returncode:
        return {}
    config = json.loads(raw.stdout)
    if not isinstance(config, dict):
        raise IntegrationError(f"{CONFIG} must contain an object")
    return config


def tests_for(delta, available, declared, config):
    tests = {file for file in available if PurePosixPath(file).match("test_*.py")}
    selected = tests.intersection(delta) | set(declared)
    for name in delta:
        path = PurePosixPath(name)
        key = path.stem
        for pattern, items in config.get("paths", {}).items():
            if fnmatchcase(name, pattern):
                selected.update(test_path(item) for item in items)
        if key not in ("__init__", "conftest") and not path.match("test_*.py"):
            selected.update(test for test in tests if
                            (stem := PurePosixPath(test).stem.removeprefix("test_")) == key
                            or stem.startswith(key + "_"))
    if missing := selected - available:
        raise IntegrationError("Configured tests are missing: " + ", ".join(sorted(missing)))
    inference = {name for name in selected if any(
        fnmatchcase(name, pattern) for pattern in config.get("inference", []))}
    contract = sorted(selected - inference)
    if not contract:
        raise IntegrationError("No affected contract test was selected")
    if len(selected) > 40:
        raise IntegrationError("More than 40 test files match. Narrow the repository path map.")
    return {"contract": contract, "inference": sorted(inference)}


def test_roots(files, tracked):
    roots = {}
    for file in files:
        path = PurePosixPath(file)
        fallback = path.parts[0] if len(path.parts) > 1 else "."
        roots[file] = next((str(parent) for parent in path.parents
                            if parent != PurePosixPath(".") and str(parent / "conftest.py") in tracked),
                           fallback)
    return roots


def combine(results):
    counts = {key: sum(row["counts"][key] for row in results.values())
              for key in ("passed", "failed", "skipped")}
    failures = {(f"{root}/{key}" if len(results) > 1 else key): info
                for root, row in results.items() for key, info in row["failures"].items()}
    result = {"counts": counts, "failures": failures, "roots": results}
    errors = [f"{root}: {row['runner_error']}" for root, row in results.items() if row.get("runner_error")]
    if errors:
        result["runner_error"] = "\n".join(errors)
    return result


def test_env(data=None):
    """Start tests from an isolated context, even in a worker launched by Agora."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(EXCLUDED_ENV)}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", HOARD_HUB_AUTOSTART="0")
    if data:
        env.update(HOARD_HUB_DATA_DIR=str(data), HOARD_HUB_TOKEN_FILE=str(data / "mcp-token"),
                   HOARD_HUB_URL="http://127.0.0.1:1")
    return env


def arguments(files, report, lane):
    markers = "inference or live_model or gpu" if lane == "marked_inference" else MARKERS
    options = [] if lane == "inference" else ["-m", markers]
    return ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1", "--junitxml", str(report),
            *options, "--", *files]


def report_result(exit_code, report, output, *, empty_ok=False):
    if exit_code == 5 and empty_ok:
        return {"counts": {"passed": 0, "failed": 0, "skipped": 0}, "failures": {}}
    if not report or exit_code not in (0, 1, 2):
        raise IntegrationError(f"Test runner exited {exit_code}: {output[-3500:]}")
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    failures = {}
    errors = False
    for case in ET.fromstring(report).iter("testcase"):
        key = case.get("classname", "") + "::" + case.get("name", "")
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
            errors = errors or failure is not None
        if failure is not None:
            counts["failed"] += 1
            # Pytest's message contains the assertion and exception. Tracebacks include
            # checkout paths and line numbers, which can differ between the two trees.
            details = failure.get("message", "").strip()
            failures[key] = {"signature": hashlib.sha256(details.encode()).hexdigest(), "file": case.get("file"),
                             "message": details[:500]}
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    result = {"counts": counts, "failures": failures}
    if exit_code == 2 or errors:
        result["runner_error"] = output[-3500:]
    return result


def local_tests(repo, work, files, report, lane):
    python = next((repo / folder / path for folder in (".venv", "venv")
                   for path in ("Scripts/python.exe", "bin/python")
                   if (repo / folder / path).is_file()), Path(sys.executable))
    return subprocess.run(
        [str(python), *arguments(files, report, lane)], cwd=work, env=test_env(report.with_suffix(".data")),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# The Spark needs an existing checkout with the same base and an installed pytest
# environment. Only the proposed Git diff travels. No branch/ref is published.
REMOTE_RUNNER = """
import base64, json, os, pathlib, subprocess, sys, tempfile
p = json.load(sys.stdin)
repo = pathlib.Path(p["repo"]).expanduser().resolve()
env = {k: v for k, v in os.environ.items() if not k.upper().startswith(tuple(p["excluded"]))}
env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", HOARD_HUB_AUTOSTART="0")
with tempfile.TemporaryDirectory(prefix="hoard-tests-") as temp:
    work = pathlib.Path(temp) / "checkout"
    report = pathlib.Path(temp) / "report.xml"
    data = pathlib.Path(temp) / "data"
    env.update(HOARD_HUB_DATA_DIR=str(data), HOARD_HUB_TOKEN_FILE=str(data / "mcp-token"),
               HOARD_HUB_URL="http://127.0.0.1:1")
    subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
                    "worktree", "add", "--detach", str(work), p["base"]],
                   check=True, capture_output=True, env=env, timeout=60)
    try:
        patch = base64.b64decode(p["patch"])
        if patch:
            subprocess.run(["git", "apply", "-"], cwd=work, input=patch, check=True,
                           capture_output=True, env=env, timeout=60)
        python = next((str(repo / v / "bin/python") for v in (".venv", "venv")
                       if (repo / v / "bin/python").is_file()), sys.executable)
        args = [str(report) if a == "REPORT" else a for a in p["args"]]
        r = subprocess.run([python, *args], cwd=work, env=env, capture_output=True, timeout=p["timeout"])
        print(json.dumps({"exit": r.returncode, "report": report.read_text(encoding="utf-8") if report.exists() else "",
                          "output": (r.stdout + r.stderr).decode("utf-8", "replace")[-3500:]}))
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(work)],
                       check=True, capture_output=True, env=env, timeout=60)
"""


def suite(hub, repo, work, files, report, lane, *, remote, base, ref, empty_ok=False):
    if not files:
        return {"counts": {"passed": 0, "failed": 0, "skipped": 0}, "failures": {}}
    if remote:
        delta = diff_bytes(repo, base, ref) if base != ref else b""
        payload = {"repo": remote, "base": base, "patch": base64.b64encode(delta).decode("ascii"),
                   "args": arguments(files, "REPORT", lane), "timeout": TIMEOUT,
                   "excluded": EXCLUDED_ENV}
        result = json.loads(hub.placement._ssh("python3 -c " + shlex.quote(REMOTE_RUNNER),
                                                input=json.dumps(payload), timeout=TIMEOUT + 130))
        return report_result(result["exit"], result["report"], result["output"], empty_ok=empty_ok)
    result = local_tests(repo, work, files, report, lane)
    raw = report.read_bytes() if report.exists() else b""
    return report_result(result.returncode, raw, result.stdout.decode("utf-8", "replace"), empty_ok=empty_ok)


def compare(base, candidate):
    known, new = {}, {}
    for key, info in candidate["failures"].items():
        bucket = known if base["failures"].get(key, {}).get("signature") == info["signature"] else new
        bucket[key] = info
    result = {"main": base, "candidate": candidate, "known": known, "new": new}
    if "roots" in base and "roots" in candidate:
        result["roots"] = {root: compare(row, candidate["roots"][root])
                           for root, row in base["roots"].items()}
        for root, row in base["roots"].items():
            ran = lambda data: data["counts"]["passed"] + data["counts"]["failed"]
            if ran(row) and not ran(candidate["roots"].get(root, {"counts": {"passed": 0, "failed": 0}})):
                # A root that ran contract tests on main and collected none in the candidate lost its contract.
                new[f"{root}::<no tests ran>"] = {"signature": "root-lost", "file": root,
                                                  "message": f"{ran(row)} contract tests ran on main and none in the candidate"}
    return result
