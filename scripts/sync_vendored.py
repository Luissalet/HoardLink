#!/usr/bin/env python3
"""Refresh the copy of ``hoard_link/`` (and ``hoard-link.js``) every app vendors.

The library is vendored, not pip-installed, so each app runs with whatever
copy it has — and twenty apps drift twenty ways. This script makes the
copies equal to this repository's ``hoard_link/`` again:

    python scripts/sync_vendored.py                  # every sibling app that vendors it
    python scripts/sync_vendored.py --apps funes scribe
    python scripts/sync_vendored.py --install borges argus   # vendor it where it is missing
    python scripts/sync_vendored.py --dry-run

A Python app vendors the package at ``<app>/<package>/hoard_link/``; a
Node app vendors ``<app>/server/hoard-link.js``. ``--install`` puts the
package next to the app's ``__main__.py`` (or ``main.py``) when no copy
exists yet. ``__pycache__`` is never copied, and nothing outside the
vendored folder is touched. Roots default to the folder above this
repository; ``--roots`` overrides it.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC_PY = REPO / "hoard_link"
SRC_JS = REPO / "js" / "hoard-link.js"
SKIP = {"node_modules", "venv", ".venv", "dist", "static", ".git", "__pycache__", "data", "frontend", "client", "tests", "docs"}


def find_vendored(app: Path, max_depth: int = 3) -> list[Path]:
    out: list[Path] = []
    base = len(app.parts)
    for root, dirs, names in os.walk(app):
        depth = len(Path(root).parts) - base
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")] if depth < max_depth else []
        if Path(root).name == "hoard_link" and "__init__.py" in names and Path(root) != SRC_PY:
            out.append(Path(root))
            dirs[:] = []
    return out


def package_dir(app: Path) -> Path | None:
    for cand in sorted(app.iterdir()):
        if cand.is_dir() and cand.name not in SKIP and not cand.name.startswith(".") and (
                (cand / "__main__.py").is_file() or (cand / "main.py").is_file() or (cand / "api.py").is_file()):
            return cand
    return None


KEEP_IN_DST = {"VENDORED.txt", "LICENSE"}
#: The hub runs from this repository only; apps never need its subpackage.
SKIP_IN_SRC = {"hub"}


def _version() -> str:
    import re
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (SRC_PY / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "?"


def copy_tree(src: Path, dst: Path, dry: bool) -> tuple[int, int]:
    """Copy src over dst; returns (changed, removed)."""
    changed = removed = 0
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__" and not (Path(root) == src and d in SKIP_IN_SRC)]
        rel = Path(root).relative_to(src)
        for n in names:
            if n.endswith((".pyc", ".pyo")):
                continue
            s, d = Path(root) / n, dst / rel / n
            if d.is_file() and filecmp.cmp(s, d, shallow=False):
                continue
            changed += 1
            if not dry:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
    # files in dst that no longer exist in src (a vendored hub/ subpackage goes too)
    if dst.is_dir():
        for root, dirs, names in os.walk(dst, topdown=False):
            rel = Path(root).relative_to(dst)
            for n in names:
                if n.endswith((".pyc", ".pyo")) or (rel == Path(".") and n in KEEP_IN_DST):
                    continue
                if not (src / rel / n).exists() or (rel.parts and rel.parts[0] in SKIP_IN_SRC):
                    removed += 1
                    if not dry:
                        (Path(root) / n).unlink()
            if Path(root) != dst and not dry:
                try:
                    if not any(Path(root).iterdir()):
                        Path(root).rmdir()
                except OSError:
                    pass
    if not dry:
        note = dst / "VENDORED.txt"
        text = (f"Vendored from HoardLink (https://github.com/Luissalet/HoardLink), version {_version()}\n\n"
                "Byte-identical copy of the upstream hoard_link/ package (every .py file except the\n"
                "hub/ subpackage, which only the hub itself runs) plus its LICENSE. Do not edit these\n"
                "files; run scripts/sync_vendored.py in the HoardLink repository to update every copy.\n")
        if not note.is_file() or note.read_text(encoding="utf-8") != text:
            note.write_text(text, encoding="utf-8")
        lic = REPO / "LICENSE"
        if lic.is_file() and not (dst / "LICENSE").is_file():
            shutil.copy2(lic, dst / "LICENSE")
    return changed, removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", nargs="*", default=[str(REPO.parent)])
    ap.add_argument("--apps", nargs="*", help="only these app ids (folder names or manifest ids)")
    ap.add_argument("--install", nargs="*", help="vendor the package into these apps where missing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    wanted = {a.lower() for a in (args.apps or [])}
    install = {a.lower() for a in (args.install or [])}
    total = 0
    for root in args.roots:
        for folder in sorted(Path(root).iterdir()):
            if not folder.is_dir() or folder.resolve() == REPO.resolve():
                continue
            manifest = folder / "faustus-plugin.json"
            app_id = folder.name.lower()
            if manifest.is_file():
                try:
                    import json
                    app_id = str(json.loads(manifest.read_text(encoding="utf-8-sig")).get("id") or app_id).lower()
                except Exception:  # noqa: BLE001
                    pass
            keys = {app_id, folder.name.lower(), folder.name.lower().replace("'s hoard", "").replace(" hoard", "").strip()}
            if wanted and not (keys & wanted):
                continue
            targets = find_vendored(folder)
            js = folder / "server" / "hoard-link.js"
            if not targets and keys & install:
                pkg = package_dir(folder)
                if pkg is None:
                    print(f"  {folder.name}: no package folder found to vendor into")
                    continue
                targets = [pkg / "hoard_link"]
                print(f"  {folder.name}: installing at {targets[0].relative_to(folder)}")
            elif not targets and not js.is_file() and (folder / "server").is_dir() and keys & install:
                if not args.dry_run:
                    shutil.copy2(SRC_JS, js)
                print(f"  {folder.name}: installed server/hoard-link.js")
                total += 1
                continue
            for t in targets:
                ch, rm = copy_tree(SRC_PY, t, args.dry_run)
                total += ch + rm
                print(f"  {folder.name}: {t.relative_to(folder)}  {ch} file(s) updated, {rm} removed" + (" [dry run]" if args.dry_run else ""))
            if js.is_file():
                if filecmp.cmp(SRC_JS, js, shallow=False):
                    print(f"  {folder.name}: server/hoard-link.js up to date")
                else:
                    if not args.dry_run:
                        shutil.copy2(SRC_JS, js)
                    total += 1
                    print(f"  {folder.name}: server/hoard-link.js updated" + (" [dry run]" if args.dry_run else ""))
    print(f"{total} change(s)" + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
