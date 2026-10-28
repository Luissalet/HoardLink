"""Pinned versions: a lock file of artifact hashes and tool versions, and a guard against sources changing mid-run.

``lock.json``::

    {"schema": 1, "artifacts": {"oracle-lib": {"path": "vendor/engine.js", "sha256": "..."}}, "tools": {"node": "v22.19.0"}}

``verify_lock`` fails on any hash or tool version that moved; ``snapshot``/``ensure_unchanged`` abort a run whose
sources (harness, oracle, probe, corpus builder) were edited while it was running, because its result would describe
a mixture of two versions.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


class LockError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tool_version(argv: list[str], cwd: Optional[Path] = None) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return "missing"
    return (r.stdout + r.stderr).strip().splitlines()[0] if (r.stdout + r.stderr).strip() else "unknown"


def make_lock(root: Path, artifacts: Mapping[str, str], tools: Mapping[str, list[str]]) -> dict[str, Any]:
    """Pin ``artifacts`` ({name: path relative to root}) by hash and ``tools`` ({name: version command}) by output."""
    return {"schema": 1,
            "artifacts": {n: {"path": p, "sha256": sha256_file(root / p)} for n, p in sorted(artifacts.items())},
            "tools": {n: tool_version(cmd, root) for n, cmd in sorted(tools.items())}}


def verify_lock(lock: Mapping[str, Any], root: Path, tools: Optional[Mapping[str, list[str]]] = None) -> dict[str, str]:
    """Raise :class:`LockError` listing everything that differs; return the artifact hashes that were checked."""
    if lock.get("schema") != 1:
        raise LockError("unsupported lock schema")
    problems, checked = [], {}
    for name, entry in (lock.get("artifacts") or {}).items():
        path = root / entry["path"]
        if not path.exists():
            problems.append(f"{name}: {entry['path']} is missing")
            continue
        actual = sha256_file(path)
        checked[name] = actual
        if actual != entry["sha256"]:
            problems.append(f"{name}: {entry['path']} changed (locked {entry['sha256'][:12]}, now {actual[:12]})")
    for name, locked in (lock.get("tools") or {}).items():
        if tools and name in tools:
            now = tool_version(tools[name], root)
            if now != locked:
                problems.append(f"tool {name}: locked {locked!r}, now {now!r}")
    if problems:
        raise LockError("version lock does not hold: " + "; ".join(problems) + " (re-pin deliberately with `parity lock`)")
    return checked


def snapshot(paths: Iterable[Path]) -> dict[str, str]:
    return {str(p): sha256_file(p) if p.exists() else "missing" for p in sorted(set(map(Path, paths)))}


def ensure_unchanged(before: Mapping[str, str]) -> None:
    after = snapshot(Path(p) for p in before)
    moved = sorted(p for p in before if before[p] != after[p])
    if moved:
        raise LockError("sources changed while the run was in progress: " + ", ".join(moved))
