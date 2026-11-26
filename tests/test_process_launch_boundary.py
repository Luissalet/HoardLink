"""Static guard for process creation, including the Python sent to helper processes."""

import ast
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard_link import _process

SOURCE = Path(__file__).resolve().parents[1] / "hoard_link"
USER_OPEN = {
    ("net.py", "open_in_browser", "os.startfile", "url"),
    ("net.py", "open_in_browser", "webbrowser.open", "url"),
    ("hub/desktop.py", "open_folder", "os.startfile", "path"),
}
PENDING = {
    # fixed after #3782 lands
    ("hub/procs.py", "_listening_pids_cli", "subprocess.run", "netstat"),
    ("hub/procs.py", "_listening_pids_cli", "subprocess.run", "ss"),
    # The existing start_app launch supplies CREATE_NO_WINDOW via _detached_kwargs.
    # fixed after #3782 lands
    ("hub/procs.py", "start_app", "subprocess.Popen", "cmd"),
}
EMBEDDED = {"launch.py": "_SPAWNER", "hub/integration_checks.py": "REMOTE_RUNNER"}
LAUNCHERS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "os.system", "os.popen", "os.startfile", "webbrowser.open", "webbrowser.open_new", "webbrowser.open_new_tab",
    "_winapi.CreateProcess", "pty.spawn",
}
LAUNCH_PREFIXES = ("asyncio.create_subprocess_", "asyncio.subprocess.create_subprocess_",
                   "os.exec", "os.spawn", "os.posix_spawn")
PROCESS_MODULES = {"subprocess", "asyncio", "os", "webbrowser", "_winapi", "pty"}
BOUNDARY_MODULES = {"_process", "hoard_link._process"}
IMPORTERS = {"__import__", "builtins.__import__", "importlib.import_module"}
GETTERS = {"getattr", "builtins.getattr"}


def _standard_name(name):
    for module in BOUNDARY_MODULES:
        if name.startswith(module + ".subprocess"):
            return name.removeprefix(module + ".")
    return name


def _is_standard_launcher(name):
    return name in LAUNCHERS or any(name.startswith(prefix) and "." not in name[len(prefix):]
                                    for prefix in LAUNCH_PREFIXES)


def _is_launcher(name):
    return (_is_standard_launcher(name)
            or name.endswith((".subprocess_exec", ".subprocess_shell")))


def _direct_launches(source, path):
    tree = ast.parse(source, filename=path)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    annotations = set()
    aliases = {}
    imports = {}
    assignments = []

    def bind(identifier, names):
        if not names:
            return False
        bound = aliases.setdefault(identifier, set())
        added = names - bound
        bound.update(added)
        return bool(added)

    for node in ast.walk(tree):
        for value in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if value is not None:
                annotations.update(ast.walk(value))
        if isinstance(node, ast.Import):
            for item in node.names:
                root = item.name.split(".")[0]
                bind(item.asname or root, {item.name if item.asname else root})
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and module not in ("", "_process"):
                continue
            imported = set()
            for item in node.names:
                name = _standard_name(f"{module}.{item.name}" if module else item.name)
                imported.add(name)
                if item.name != "*":
                    bind(item.asname or item.name, {name})
            imports[node] = imported
        elif isinstance(node, ast.Assign):
            assignments.extend((target, node.value) for target in node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            assignments.append((node.target, node.value))

    def qualified(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, {node.id})
        if isinstance(node, ast.Attribute):
            return {_standard_name(f"{base}.{node.attr}") for base in qualified(node.value)}
        if isinstance(node, ast.NamedExpr):
            return qualified(node.value)
        if isinstance(node, ast.Call):
            func = qualified(node.func)
            if func & GETTERS and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                return {_standard_name(f"{base}.{node.args[1].value}") for base in qualified(node.args[0])}
            imported = dynamic_import(node, func)
            if imported:
                return {imported}
        return set()

    def dynamic_import(node, func):
        if not (func & IMPORTERS):
            return None
        module = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "name"), None)
        if isinstance(module, ast.Constant) and isinstance(module.value, str):
            return module.value
        return None

    # Only track names that can reach a process launcher. This also bounds
    # the fixed point for ordinary assignments such as value = value.field.
    tracked_modules = PROCESS_MODULES | BOUNDARY_MODULES | {
        "asyncio.subprocess", "hoard_link", "importlib", "builtins",
    }
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if isinstance(target, ast.Name):
                names = {name for name in qualified(value)
                         if name in tracked_modules or name in IMPORTERS or name in GETTERS
                         or _is_standard_launcher(name)}
                changed |= bind(target.id, names)

    for node in ast.walk(tree):
        if node in annotations:
            continue
        names = imports[node] if node in imports else qualified(node)
        names = {name for name in names if _is_launcher(name) or (
            name.endswith(".*") and name[:-2] in tracked_modules)}
        if isinstance(node, ast.Call):
            imported = dynamic_import(node, qualified(node.func))
            if imported and (imported.split(".")[0] in PROCESS_MODULES or imported in BOUNDARY_MODULES):
                names.add(f"dynamic import of {imported}")
        for name in sorted(names):
            parent = parents.get(node)
            command = ""
            if isinstance(parent, ast.Call) and parent.func is node and parent.args:
                argv = parent.args[0]
                if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
                    argv = argv.elts[0]
                command = argv.value if isinstance(argv, ast.Constant) else ast.unparse(argv)
            while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = parents.get(parent)
            yield (path, parent.name if parent else "", name, command), node.lineno


def test_process_launches_use_the_shared_boundary():
    permitted = USER_OPEN | PENDING
    violations = []
    for file in sorted(SOURCE.rglob("*.py")):
        path = file.relative_to(SOURCE).as_posix()
        if path == "_process.py":
            continue  # The only implementation that calls the standard-library launchers.
        source = file.read_text(encoding="utf-8-sig")
        launches = list(_direct_launches(source, path))
        if path in EMBEDDED:
            assignment = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                              and any(isinstance(t, ast.Name) and t.id == EMBEDDED[path] for t in node.targets))
            launches.extend(_direct_launches(ast.literal_eval(assignment.value), f"{path}:{EMBEDDED[path]}"))
        for site, line in launches:
            if site in permitted:
                permitted.remove(site)
                continue
            name = site[2]
            violations.append(f"{site[0]}:{line}: {name}")
    assert not violations, "Launch through hoard_link.proc or hoard_link._process:\n" + "\n".join(violations)
    assert not permitted, f"Remove obsolete process exceptions: {sorted(permitted)}"


@pytest.mark.parametrize(("snippet", "name"), [
    ("import subprocess as sp\nsp.check_output(['git'])", "subprocess.check_output"),
    ("from subprocess import run as launch\nlaunch(['git'])", "subprocess.run"),
    ("import subprocess\nrunner = subprocess.Popen", "subprocess.Popen"),
    ("import subprocess\ngetattr(subprocess, 'run')(['git'])", "subprocess.run"),
    ("import asyncio\nasyncio.create_subprocess_exec('git')", "asyncio.create_subprocess_exec"),
    ("from os import system\nsystem('git')", "os.system"),
    ("from subprocess import *\nrun(['git'])", "subprocess.*"),
    ("from os import *\nsystem('git')", "os.*"),
    ("from asyncio import *\ncreate_subprocess_exec('git')", "asyncio.*"),
    ("from asyncio.subprocess import *", "asyncio.subprocess.*"),
    ("import subprocess\nS = subprocess\nT = S\nT.run(['git'])", "subprocess.run"),
    ("import os\nO: object = os\nO.system('git')", "os.system"),
    ("import asyncio\n(A := asyncio).create_subprocess_exec('git')", "asyncio.create_subprocess_exec"),
    ("__import__('subprocess').run(['git'])", "subprocess.run"),
    ("import importlib\nimportlib.import_module('subprocess')", "dynamic import of subprocess"),
    ("from importlib import import_module as load\nI = load\nI(name='os')", "dynamic import of os"),
    ("from hoard_link import _process\n_process.subprocess.run(['git'])", "subprocess.run"),
    ("from ._process import subprocess as sp\nsp.Popen(['git'])", "subprocess.Popen"),
    ("import hoard_link._process as p\ngetattr(p, 'subprocess').call(['git'])", "subprocess.call"),
])
def test_static_guard_detects_direct_and_aliased_launches(snippet, name):
    assert name in {site[2] for site, _ in _direct_launches(snippet, "injected.py")}


@pytest.mark.parametrize(("wrapper", "launcher"), [("run_hidden", "run"), ("popen_hidden", "Popen")])
@pytest.mark.parametrize("windows", [False, True])
def test_boundary_preserves_options_and_prevents_console_flags(wrapper, launcher, windows, monkeypatch):
    calls = []
    monkeypatch.setattr(_process, "IS_WIN", windows)
    monkeypatch.setattr(subprocess, launcher, lambda argv, **kw: calls.append((argv, kw)))
    priority = 0x4000
    conflicting = 0x08 | 0x10
    flags = priority | conflicting if windows else 0
    getattr(_process, wrapper)(["helper"], cwd="work", creationflags=flags, stdin=subprocess.DEVNULL)
    expected = priority | _process.CREATE_NO_WINDOW if windows else 0
    assert calls == [(["helper"], {"cwd": "work", "creationflags": expected, "stdin": subprocess.DEVNULL})]


def test_boundary_runs_an_innocuous_child():
    result = _process.run_hidden([sys.executable, "-c", "print('child ready')"], capture_output=True,
                                 text=True, timeout=10, check=True)
    assert result.stdout.strip() == "child ready"


def test_detached_helper_runs_a_short_lived_child(tmp_path):
    from hoard_link import launch, proc

    log = tmp_path / "child.log"
    pid = launch.spawn_orphan([sys.executable, "-c", "print('detached child ready')"], str(tmp_path),
                              dict(os.environ), log)
    try:
        deadline = time.monotonic() + 10
        while proc.pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not proc.pid_alive(pid)
        assert log.read_text(encoding="utf-8").strip() == "detached child ready"
    finally:
        if proc.pid_alive(pid):
            proc.kill_tree(pid, grace_s=0)


def test_placement_upload_includes_the_standalone_process_helper(tmp_path, monkeypatch):
    from hoard_link.hub.placement import Placement

    placement = Placement(SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path))))
    uploaded = []
    monkeypatch.setattr(placement, "_ssh_step", lambda *args, **kwargs: "")

    def write(remote, source):
        local = tmp_path / Path(remote).name
        local.write_text(source, encoding="utf-8")
        uploaded.append(local)

    monkeypatch.setattr(placement, "_put_text", write)
    placement._upload_helper()
    assert [file.name for file in uploaded] == ["placement_process.py", "placement_remote.py"]
    result = _process.run_hidden([sys.executable, str(uploaded[1]), "manifest", str(tmp_path / "empty")],
                                 capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == "{}"
