"""Compare a committed dependency specification with the registered checkout's environment."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

from .._process import run_hidden
from . import integration_git as git
from .integration_checks import app_python, test_env


NPM_ARGS = ("install", "--no-save", "--no-audit", "--no-fund")
TIMEOUT = 900

# Use the app's interpreter for both installed metadata and platform markers.
PIP_PROBE = """
import importlib.metadata as metadata
import json
import sys
from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.utils import canonicalize_name

installed = {}
for dist in metadata.distributions():
    if name := dist.metadata["Name"]:
        installed.setdefault(canonicalize_name(name), dist)
issues = []
unverified = []
for text, constraint in json.load(sys.stdin):
    try:
        req = Requirement(text)
    except InvalidRequirement:
        unverified.append(text)
        continue
    if req.marker and not req.marker.evaluate({"extra": ""}):
        continue
    dist = installed.get(canonicalize_name(req.name))
    actual = dist.version if dist else None
    if constraint and actual is None:
        continue
    if actual is None or not req.specifier.contains(actual, prereleases=True):
        issues.append({"package": req.name, "expected": text, "installed": actual})
    elif req.url:
        origin = json.loads(dist.read_text("direct_url.json") or "{}").get("url")
        if origin != req.url:
            issues.append({"package": req.name, "expected": text, "installed": actual, "source": origin})
print(json.dumps({"installed": {name: dist.version for name, dist in installed.items()},
                  "issues": issues, "unverified": unverified}))
"""


def _safe_path(root, path):
    """Do not read or install through linked paths into another checkout."""
    for item in (path, *path.parents):
        if item == root:
            return True
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), "st_file_attributes", 0)
                                & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return False
    return False


def _command(args):
    if os.name != "nt":
        return shlex.join(args)
    # Present a PowerShell command and quote requirement comparators, markers and paths.
    def quote(arg):
        return "'" + arg.replace("'", "''") + "'" if re.search(r"[^\w./:\\-]", arg) else arg

    return "& " + " ".join(map(quote, args))


def _run(args, cwd, *, timeout, data=None, output=None):
    return run_hidden(args, cwd=cwd, input=data, env=test_env(), timeout=timeout,
                          stdout=output if output is not None else subprocess.PIPE, stderr=subprocess.STDOUT,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _json_file(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _npm_packages(lock):
    if "packages" in lock:
        return lock["packages"]
    packages = {}

    def visit(dependencies, prefix=""):
        for name, entry in dependencies.items():
            path = f"{prefix}node_modules/{name}"
            packages[path] = entry
            visit(entry.get("dependencies", {}), path + "/")

    visit(lock.get("dependencies", {}))
    return packages


def _npm_probe(repo, source, lock_name, delta):
    lock = PurePosixPath(lock_name)
    directory = lock.parent
    manifest = (directory / "package.json").as_posix()
    cwd = repo / directory
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    command = [npm or "npm", *NPM_ARGS]
    files = [manifest, lock_name]
    row = _row("npm", cwd, files, delta, [command])
    if not all(_safe_path(repo, path) for path in (cwd / "node_modules", *(repo / name for name in files))):
        return {**row, "status": "unverified", "reason": "linked_environment"}
    hidden = cwd / "node_modules" / ".package-lock.json"
    if not _safe_path(repo, hidden) or not _safe_path(source, source / lock_name):
        return {**row, "status": "unverified", "reason": "linked_environment"}
    try:
        lock_data = _json_file(source / lock_name)
        wanted = _npm_packages(lock_data)
        current = _npm_packages(_json_file(hidden)) if hidden.is_file() else {}
        issues = []
        if "" in wanted and _safe_path(source, source / manifest) and (source / manifest).is_file():
            declared = _json_file(source / manifest)
            for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                if declared.get(field, {}) != wanted[""].get(field, {}):
                    issues.append({"package": manifest, "field": field, "reason": "lock_specification_changed"})
        for name, entry in sorted(wanted.items()):
            if not name.startswith("node_modules/"):
                continue
            relative = PurePosixPath(name)
            if ".." in relative.parts or "\\" in name or ":" in name:
                raise ValueError("Invalid package path in package-lock.json")
            actual = current.get(name)
            if actual is None and entry.get("optional"):
                continue
            package = cwd / relative / "package.json"
            # Workspace entries describe links in the lock. Ordinary packages also need to exist on disk.
            fields = ("version", "resolved", "integrity", "link")
            matches = actual is not None and all(entry.get(key) == actual.get(key) for key in fields)
            if matches and not entry.get("link"):
                matches = (_safe_path(repo, package) and package.is_file()
                           and _json_file(package).get("version") == entry.get("version"))
            if not matches:
                issues.append({"package": name, "expected": entry.get("version") or entry.get("resolved"),
                               "installed": actual.get("version") if actual else None})
        extra = sorted(name for name in current.keys() - wanted.keys() if name.startswith("node_modules/"))
        issues.extend({"package": name, "expected": None, "installed": current[name].get("version")}
                      for name in extra)
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        return {**row, "status": "unverified", "reason": "invalid_lock", "detail": str(exc)}
    return {**row, "status": "stale" if issues or not hidden.is_file() else "current", "issues": issues,
            "installed": {name: entry.get("version") or entry.get("resolved")
                          for name, entry in current.items() if name.startswith("node_modules/")}}


def _requirements(source, primary, tracked, files, declarations, constraint=False):
    if primary in files:
        return
    file = source / primary
    if primary not in tracked or not _safe_path(source, file):
        raise ValueError(f"Requirement include is not a tracked local file: {primary}")
    files.add(primary)
    text = re.sub(r"\\\r?\n", " ", file.read_text(encoding="utf-8-sig"))
    for line in text.splitlines():
        line = re.split(r"(^|\s)#", line, maxsplit=1)[0].strip()
        if not line:
            continue
        include = re.fullmatch(r"(-r|--requirement|-c|--constraint)(?:[=\s]+)(.+)", line)
        if include:
            path = PurePosixPath(primary).parent / include[2].strip("\"'").replace("\\", "/")
            _requirements(source, path.as_posix(), tracked, files, declarations,
                          constraint or include[1] in ("-c", "--constraint"))
        elif not line.startswith("-") or line.startswith(("-e ", "--editable ")):
            declarations.append((re.split(r"\s+--hash[=\s]", line, maxsplit=1)[0], constraint))


def _python_probe(repo, source, tracked, delta):
    primary = next((name for name in ("requirements-lock.txt", "requirements.txt") if name in tracked), None)
    project = "pyproject.toml"
    files = {project} & tracked
    declarations = []
    python = app_python(repo)
    target = python or repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    install = [str(target), "-m", "pip", "install"]
    if primary:
        install.extend(("-r", primary))
    try:
        if primary:
            _requirements(source, primary, tracked, files, declarations)
        if project in files:
            if not _safe_path(source, source / project):
                raise ValueError("pyproject.toml is linked")
            metadata = tomllib.loads((source / project).read_text(encoding="utf-8-sig")).get("project", {})
            names = metadata.get("dependencies", [])
            declared = {item for item, constraint in declarations if not constraint}
            declarations.extend((item, False) for item in names)
            install.extend(dict.fromkeys(item for item in names if item not in declared))
    except (OSError, ValueError, TypeError) as exc:
        row = _row("python", repo, files, delta, [])
        return {**row, "status": "unverified", "reason": "unreadable_requirements", "detail": str(exc)}
    if not primary and not declarations:
        return None
    row = _row("python", repo, files, delta, [install])
    if python is None:
        provision = [sys.executable, "-m", "venv", ".venv"]
        return {**_row("python", repo, files, delta, [provision, install]),
                "status": "stale", "reason": "missing_venv"}
    # On POSIX the conventional bin/python inside an independent venv may be a symlink to its base interpreter.
    config = python.parent.parent / "pyvenv.cfg"
    if not _safe_path(repo, python.parent) or not _safe_path(repo, config) or not config.is_file():
        return {**row, "status": "unverified", "reason": "linked_environment"}
    try:
        pip = _run([str(python), "-m", "pip", "--version"], repo, timeout=30)
        if pip.returncode:
            bootstrap = [str(python), "-m", "ensurepip", "--upgrade"]
            return {**_row("python", repo, files, delta, [bootstrap, install]),
                    "status": "stale", "reason": "missing_pip"}
        output = _run([str(python), "-c", PIP_PROBE], repo, timeout=30,
                      data=json.dumps(declarations).encode("utf-8"))
        if output.returncode:
            raise ValueError(output.stdout.decode("utf-8", "replace")[-1500:])
        result = json.loads(output.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {**row, "status": "unverified", "reason": "pip_probe_failed", "detail": str(exc)}
    return {**row, **result, "status": "stale" if result["issues"] else
            "unverified" if result["unverified"] else "current"}


def _row(manager, cwd, files, delta, commands):
    return {"manager": manager, "cwd": str(cwd), "files": sorted(files), "changed": sorted(set(files) & delta),
            "commands": [_command(args) for args in commands], "argv": commands}


def probe(repo, source, base, ref):
    tracked = git.tracked(source, ref)
    delta = git.changed(source, base, ref) if base != ref else set()
    rows = [_npm_probe(repo, source, path, delta) for path in sorted(tracked)
            if PurePosixPath(path).name == "package-lock.json"]
    python = _python_probe(repo, source, tracked, delta)
    if python:
        rows.append(python)
    return rows


def public(rows):
    return [{key: value for key, value in row.items() if key != "installed"} for row in rows]


def install(repo, rows, folder, blocked, refresh, record):
    dirty = set(git.paths(git.git(repo, "diff", "--name-only", "-z", "HEAD").stdout))
    for index, row in enumerate(rows):
        if row["status"] != "stale":
            continue
        action = row["install"] = {"state": "deferred"}
        if blocked or dirty.intersection(row["files"]) or row.get("reason") == "missing_venv":
            action["reason"] = ("app_running" if blocked else "local_manifest_edits"
                                if dirty.intersection(row["files"]) else "missing_venv")
            if blocked:
                action["apps"] = blocked
            record()
            continue
        log = folder / f"dependencies-{index}.log"
        action.update(state="running", log=str(log))
        record()
        with log.open("wb") as output:
            try:
                for command in row["argv"]:
                    result = _run(command, Path(row["cwd"]), timeout=TIMEOUT, output=output)
                    if result.returncode:
                        action.update(state="failed", exit_code=result.returncode)
                        break
            except (OSError, subprocess.TimeoutExpired) as exc:
                action.update(state="failed", error=str(exc))
        after = refresh()[index]
        before_versions, after_versions = row.get("installed", {}), after.get("installed", {})
        action["changes"] = [{"package": name, "before": before_versions.get(name), "after": after_versions.get(name)}
                             for name in sorted(before_versions.keys() | after_versions.keys())
                             if before_versions.get(name) != after_versions.get(name)]
        action["after"] = public([after])[0]
        if action["state"] == "running":
            action["state"] = "installed" if after["status"] == "current" else "unverified"
        record()
