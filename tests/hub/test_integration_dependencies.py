import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import venv
import zipfile
from types import SimpleNamespace

import pytest

from hoard_link.hub import integration_checks as checks
from hoard_link.hub import integration_dependencies as dependencies
from hoard_link.hub import integration_git as git
from hoard_link.hub.agora import Agora
from hoard_link.hub.integration import IntegrationFacet


def run(args, cwd):
    result = subprocess.run(args, cwd=cwd, env=checks.test_env(), capture_output=True, timeout=90)
    assert not result.returncode, (args, result.stdout, result.stderr)
    return result.stdout.decode("utf-8")


def write(repo, name, content):
    file = repo / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")


def commit(repo, message, *files):
    git.git(repo, "add", "--", *files)
    git.git(repo, "commit", "-m", message)
    return git.git_text(repo, "rev-parse", "HEAD")


def npm_package(path, version):
    manifest = json.dumps({"name": "fixture-dependency", "version": version, "main": "index.js"})
    with tarfile.open(path, "w:gz") as tar:
        for name, body in (("package.json", manifest), ("index.js", f"module.exports = {version!r};")):
            data = body.encode()
            member = tarfile.TarInfo(f"package/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))


def python_wheel(folder, version):
    info = f"fixture_dependency-{version}.dist-info"
    with zipfile.ZipFile(folder / f"fixture_dependency-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr("fixture_dependency.py", f"VERSION = {version!r}\n")
        wheel.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: fixture-dependency\nVersion: {version}\n")
        wheel.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr(f"{info}/RECORD", "")


def make_delivery(root):
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm or not shutil.which("node"):
        pytest.skip("The dependency integration tests need Node and npm")
    repo = root / "Dependency fixture"
    repo.mkdir()
    git.git(repo, "init", "-b", "main")
    git.git(repo, "config", "user.name", "Luissalet")
    git.git(repo, "config", "user.email", "luissalet@users.noreply.github.com")
    write(repo, ".gitignore", ".venv/\nnode_modules/\n__pycache__/\n")
    write(repo, "tests/test_contract.py", "def test_contract():\n    assert True\n")
    write(repo, "tests/test_live.py", textwrap.dedent(f"""\
        import os
        import subprocess
        from importlib.metadata import version
        from pathlib import Path

        def test_updates_are_available():
            expected = "2.0.0" if Path(os.environ["HOARD_HUB_DATA_DIR"]).name.startswith("live-") else "1.0.0"
            assert version("fixture-dependency") == expected
            installed = subprocess.check_output(
                ["node", "-p", "require('fixture-dependency')"], cwd={str(repo)!r}, text=True)
            assert installed.strip() == expected
        """))
    wheels = root / "wheels"
    wheels.mkdir()
    for version in ("1.0.0", "2.0.0"):
        npm_package(repo / f"dependency-{version}.tgz", version)
        python_wheel(wheels, version)
    write(repo, "package.json", json.dumps({"private": True,
          "dependencies": {"fixture-dependency": "file:dependency-1.0.0.tgz"}}))
    write(repo, "requirements.txt", f'--no-index\n--find-links "{wheels.as_posix()}"\n-r requirements/locked.txt\n')
    write(repo, "requirements/locked.txt", "fixture-dependency==1.0.0\n")
    write(repo, "pyproject.toml", '[project]\ndependencies = ["fixture-dependency>=1",'
                                ' "different-platform; sys_platform == \'never\'"]\n')
    run([npm, "install", "--offline", "--no-audit", "--no-fund"], repo)
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(repo / ".venv")
    python = str(checks.app_python(repo))
    run([python, "-m", "pip", "install", "-r", "requirements.txt"], repo)
    base = commit(repo, "Add baseline dependency versions", ".gitignore", "tests", "package.json",
                  "package-lock.json", "dependency-1.0.0.tgz", "dependency-2.0.0.tgz",
                  "requirements.txt", "requirements/locked.txt", "pyproject.toml")
    branch = root / "delivery"
    git.git(repo, "worktree", "add", "-b", "delivery", str(branch), base)
    write(branch, "package.json", json.dumps({"private": True,
          "dependencies": {"fixture-dependency": "file:dependency-2.0.0.tgz"}}))
    run([npm, "install", "--package-lock-only", "--offline", "--no-audit", "--no-fund"], branch)
    write(branch, "requirements/locked.txt", "fixture-dependency==2.0.0\n")
    head = commit(branch, "Advance both dependency specifications",
                  "package.json", "package-lock.json", "requirements/locked.txt")
    return repo, base, head


def wait(facet, request):
    run_id = facet.run(request)["receipt"]["id"]
    with facet.queue_changed:
        assert facet.queue_changed.wait_for(lambda: not facet.active, timeout=240)
    return facet.status({"id": run_id})["receipt"]


def test_integration_reports_and_explicitly_updates_both_stale_environments(tmp_path, monkeypatch):
    repo, base, head = make_delivery(tmp_path)
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name == repo.name else None)
    app = SimpleNamespace(id="fixture", launch=SimpleNamespace(cwd=str(repo)))
    running = True
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                          facet=lambda name: SimpleNamespace(agora=agora), apps=[app],
                          app_status=lambda app: {"state": "down", "process": running})
    facet = IntegrationFacet(hub)
    monkeypatch.setattr(checks, "SUITE_LOCK", tmp_path / "slot")
    try:
        task = agora.task_add({"agent": "owner", "title": "Update dependencies", "repo": repo.name})["task"]
        args = {"agent": "owner", "task_id": task["id"], "revision": 1}
        agora.task_claim(args)
        agora.task_submit({**args, "branch": "delivery", "commits": [head],
                           "summary": "Test-Paths: tests/test_contract.py"})
        agora.task_review({**args, "agent": "reviewer", "verdict": "approve",
                           "expected_submission_revision": 1, "body": "Reviewed the fixture."})

        with pytest.raises(git.IntegrationError, match="requires an approved merge"):
            facet.run({**args, "install_dependencies": True})
        read_only = wait(facet, {**args, "merge": True})
        assert read_only["state"] == "integrated", read_only
        assert [row["status"] for row in read_only["dependencies"]] == ["stale", "stale"]
        assert read_only["dependencies"][0]["changed"] == ["package-lock.json", "package.json"]
        assert read_only["dependencies"][1]["changed"] == ["requirements/locked.txt"]
        assert "install --no-save" in read_only["dependencies"][0]["commands"][0]
        assert "-m pip install -r requirements.txt" in read_only["dependencies"][1]["commands"][0]
        assert all("install" not in row for row in read_only["dependencies"])
        drift = dependencies.probe(repo, repo, read_only["merge_commit"], read_only["merge_commit"])
        assert all(row["status"] == "stale" and not row["changed"] for row in drift)
        dependencies.install(repo, drift, tmp_path, facet._dependency_apps(repo),
                             lambda: pytest.fail("A running app must defer installation"), lambda: None)
        assert all(row["install"] == {"state": "deferred", "reason": "app_running", "apps": ["fixture"]}
                   for row in drift)
        assert facet.undo({"id": read_only["id"], "agent": "owner"})["receipt"]["state"] == "undone"

        running = False
        updated = wait(facet, {**args, "merge": True, "install_dependencies": True,
                               "live_test": "tests/test_live.py::test_updates_are_available"})
        assert updated["state"] == "integrated", updated
        events = [event["state"] for event in updated["events"]]
        assert events.index("moved_checkout") < events.index("installing_dependencies") < events.index("checking_live_case")
        assert updated["live"]["counts"]["passed"] == 1
        for row in updated["dependencies"]:
            action = row["install"]
            assert action["state"] == "installed", row
            assert any(change["before"] == "1.0.0" and change["after"] == "2.0.0" for change in action["changes"])
        assert not git.git(repo, "diff", "--name-only", "HEAD").stdout
        assert agora._task_row(task["id"])["status"] == "done"
        undone = facet.undo({"id": updated["id"], "agent": "owner"})["receipt"]
        assert undone["state"] == "undone"
        assert [row["status"] for row in undone["undo_dependencies"]] == ["stale", "stale"]
        assert git.git_text(repo, "rev-parse", "HEAD") == base
        assert agora._task_row(task["id"])["status"] == "approved"
    finally:
        facet.close()
        agora.close()


def test_preexisting_python_lock_drift_and_missing_pip_are_reported(tmp_path):
    git.git(tmp_path, "init", "-b", "main")
    git.git(tmp_path, "config", "user.name", "Luissalet")
    git.git(tmp_path, "config", "user.email", "luissalet@users.noreply.github.com")
    write(tmp_path, "requirements-lock.txt", "fixture-dependency==2.0.0\n")
    commit_id = commit(tmp_path, "Pin a dependency", "requirements-lock.txt")
    missing, = dependencies.probe(tmp_path, tmp_path, commit_id, commit_id)
    assert missing["reason"] == "missing_venv"
    assert "-m venv .venv" in missing["commands"][0]
    venv.EnvBuilder(with_pip=False).create(tmp_path / ".venv")
    no_pip, = dependencies.probe(tmp_path, tmp_path, commit_id, commit_id)
    assert no_pip["changed"] == []
    assert no_pip["reason"] == "missing_pip"
    assert "-m ensurepip --upgrade" in no_pip["commands"][0]
    run([str(checks.app_python(tmp_path)), "-m", "ensurepip"], tmp_path)
    stale, = dependencies.probe(tmp_path, tmp_path, commit_id, commit_id)
    assert stale["status"] == "stale"
    assert stale["changed"] == []
    assert stale["issues"] == [{"package": "fixture-dependency", "expected": "fixture-dependency==2.0.0",
                                "installed": None}]
