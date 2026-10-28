"""Selective execution: a harness reruns only when a file it depends on changed, and a state file remembers the last result.

``depends_on`` is a list of globs relative to the harness root (``**`` crosses folders). The *selection key* of a
harness is a hash of those files' contents, the harness file itself, the lock file and the environment, so an edit to
the probe, the oracle, the corpus builder or a pinned artifact invalidates exactly the harnesses that read it.
``changed_harnesses`` is the pull-request view: which harnesses a list of changed paths can affect, without running
anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

from ..atomic import write_json_atomic
from .lock import sha256_file


def glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    p = pattern.replace("\\", "/")
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif p.startswith("**", i):
            out.append(".*"); i += 2
        elif p[i] == "*":
            out.append("[^/]*"); i += 1
        elif p[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(p[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def expand(root: Path, patterns: Iterable[str]) -> list[Path]:
    """Existing files matching the globs, sorted, without folders."""
    found: set[Path] = set()
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            found.update(p for p in root.glob(pat) if p.is_file())
        elif (root / pat).is_file():
            found.add(root / pat)
    return sorted(found)


def selection_key(files: Iterable[Path], extra: Iterable[str] = ()) -> str:
    h = hashlib.sha256()
    for p in sorted(set(files)):
        h.update(f"{p.as_posix()}\0{sha256_file(p) if p.exists() else 'missing'}\n".encode())
    for e in extra:
        h.update(e.encode() + b"\n")
    return h.hexdigest()


def affected(root: Path, patterns: Iterable[str], changed: Iterable[str | Path]) -> bool:
    """True when any changed path (absolute, or relative to ``root``) matches one of the harness' globs."""
    regexes = [glob_regex(p) for p in patterns]
    for c in changed:
        path = Path(c)
        rel = path.relative_to(root).as_posix() if path.is_absolute() and root in path.parents else path.as_posix()
        if any(rx.match(rel) for rx in regexes):
            return True
    return False


def git_changed(root: Path, base: str) -> list[Path]:
    """Files changed since ``base`` (merge-base with HEAD) plus uncommitted ones, as absolute paths."""
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout
    top = Path(git("rev-parse", "--show-toplevel").strip())
    names = set(git("diff", "--name-only", base + "...HEAD").splitlines()) | set(git("diff", "--name-only", "HEAD").splitlines())
    names |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(top / n for n in names if n)


def changed_harnesses(harnesses: Iterable[Any], changed: Iterable[str | Path]) -> list[Any]:
    changed = list(changed)
    return [h for h in harnesses if affected(h.root, [*h.depends_on, *h.oracle_depends_on], changed)]


class State:
    """``state.json``: per harness the selection key of its last passing run and how many times in a row it passed."""

    def __init__(self, directory: Path):
        self.path = directory / "state.json"
        self.data: dict[str, Any] = {"schema": 1, "harnesses": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                pass

    def get(self, name: str) -> dict[str, Any]:
        return self.data["harnesses"].get(name, {})

    def record(self, name: str, key: str, status: str, at: str) -> dict[str, Any]:
        prev = self.get(name)
        passes = (prev.get("passes", 0) + 1 if prev.get("key") == key else 1) if status == "pass" else 0
        entry = {"key": key, "status": status, "passes": passes, "at": at}
        self.data["harnesses"][name] = entry
        write_json_atomic(self.path, self.data)
        return entry

    def needs_run(self, name: str, key: str, stable: int = 1) -> Optional[str]:
        """Why the harness must run, or None when its last pass stands."""
        prev = self.get(name)
        if not prev:
            return "never ran"
        if prev.get("status") != "pass":
            return f"last result was {prev.get('status')}"
        if prev.get("key") != key:
            return "a dependency changed"
        if prev.get("passes", 0) < stable:
            return f"{prev.get('passes', 0)} of {stable} required passes"
        return None
