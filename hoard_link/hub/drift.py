"""Drift between an app's copies of shared files and the canonical ones.

One implementation, used by the scripts that *fix* drift
(``scripts/sync_vendored.py``, ``scripts/sync_theme.py``) and by the hub's
Repos facet (``repos.py``) that only *reports* it, so the two can never
disagree about what "out of date" means.

Standard library only and no imports from the rest of the package: the
scripts load this file by path, without importing ``hoard_link`` (which
needs ``httpx``).

* **vendored library**: every app vendors ``hoard_link/`` (every ``.py``
  file except the hub's own subpackage, plus ``VENDORED.txt`` and
  ``LICENSE``, which belong to the copy) and Node apps vendor
  ``server/hoard-link.js``. :func:`plan_tree` says which files of a copy
  differ from the source, are missing, or no longer exist upstream.
* **theme**: ``hoard-theme.css`` is copied wherever an app's build picks
  CSS up; :func:`theme_copies_in` finds the copies, :func:`digest` compares.
"""

from __future__ import annotations

import filecmp
import hashlib
import os
from pathlib import Path
from typing import Iterator, Optional

#: Folders never searched for a vendored copy.
VENDOR_SKIP = {"node_modules", "venv", ".venv", "dist", "static", ".git", "__pycache__", "data", "frontend", "client",
               "tests", "docs"}
#: The hub runs from the HoardLink repository only; apps never need its subpackage.
SKIP_IN_SRC = {"hub"}
#: Files of a vendored copy that belong to the copy, not to the upstream package.
KEEP_IN_DST = {"VENDORED.txt", "LICENSE"}
#: Folders never searched for ``hoard-theme.css`` copies (build output and the like).
THEME_SKIP = {"node_modules", "dist", "build", ".git", ".venv", "venv", "__pycache__", "data", "release", "dist-electron",
              "assets"}
THEME_NAME = "hoard-theme.css"


def find_vendored(app: Path | str, src_pkg: Optional[Path | str] = None, max_depth: int = 3) -> list[Path]:
    """Every ``hoard_link/`` folder an app vendors (a folder of that name holding an ``__init__.py`` or a
    ``VENDORED.txt``), at most ``max_depth`` levels down; ``src_pkg`` (the canonical package) is not a copy."""
    app = Path(app)
    src = Path(src_pkg).resolve() if src_pkg else None
    out: list[Path] = []
    base = len(app.parts)
    for root, dirs, names in os.walk(app):
        depth = len(Path(root).parts) - base
        dirs[:] = [d for d in dirs if d not in VENDOR_SKIP and not d.startswith(".")] if depth < max_depth else []
        if Path(root).name == "hoard_link" and ("__init__.py" in names or "VENDORED.txt" in names):
            if src is not None and Path(root).resolve() == src:
                dirs[:] = []
                continue
            out.append(Path(root))
            dirs[:] = []
    return out


def _is_noise(name: str) -> bool:
    return name.endswith((".pyc", ".pyo"))


def plan_tree(src: Path | str, dst: Path | str) -> tuple[list[str], list[str]]:
    """What copying ``src`` over ``dst`` would do: ``(changed, removed)`` as ``/``-separated paths relative to
    the copy. ``changed`` is a file that is missing from ``dst`` or differs from ``src``; ``removed`` is a file
    of ``dst`` that ``src`` no longer has (or that sits in the never-vendored subpackage). ``__pycache__``,
    compiled files and the copy's own ``VENDORED.txt``/``LICENSE`` are ignored."""
    src, dst = Path(src), Path(dst)
    changed: list[str] = []
    removed: list[str] = []
    for root, dirs, names in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not (Path(root) == src and d in SKIP_IN_SRC))
        rel = Path(root).relative_to(src)
        for n in sorted(names):
            if _is_noise(n):
                continue
            s, d = Path(root) / n, dst / rel / n
            if d.is_file() and filecmp.cmp(s, d, shallow=False):
                continue
            changed.append((rel / n).as_posix())
    if dst.is_dir():
        for root, dirs, names in os.walk(dst):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            rel = Path(root).relative_to(dst)
            for n in sorted(names):
                if _is_noise(n) or (rel == Path(".") and n in KEEP_IN_DST):
                    continue
                if not (src / rel / n).exists() or (rel.parts and rel.parts[0] in SKIP_IN_SRC):
                    removed.append((rel / n).as_posix())
    return changed, removed


def vendored_js_stale(app: Path | str, src_js: Path | str) -> Optional[bool]:
    """``None`` when the app has no ``server/hoard-link.js``; else whether it differs from the canonical one."""
    js = Path(app) / "server" / "hoard-link.js"
    if not js.is_file() or not Path(src_js).is_file():
        return None
    return not filecmp.cmp(src_js, js, shallow=False)


def digest(path: Path | str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def theme_copies_in(folder: Path | str) -> Iterator[str]:
    """Every ``hoard-theme.css`` under one app folder (build folders skipped)."""
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in THEME_SKIP]
        if THEME_NAME in filenames:
            yield os.path.join(dirpath, THEME_NAME)


def theme_stale(copies: list[str], canon: Path | str) -> list[str]:
    """The copies whose bytes differ from the canonical theme (the canonical file itself never counts)."""
    if not Path(canon).is_file():
        return []
    want = digest(canon)
    canon_real = os.path.realpath(canon)
    return [c for c in copies if os.path.realpath(c) != canon_real and digest(c) != want]
