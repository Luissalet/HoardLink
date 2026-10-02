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
Node app vendors ``<app>/server/hoard-link.js`` and the Node commons
(``js/hoard-commons/``) at ``<app>/server/hoard-commons/``. ``--install`` puts the
package next to the app's ``__main__.py`` (or ``main.py``) when no copy
exists yet. ``__pycache__`` is never copied, and nothing outside the
vendored folder is touched. Roots default to the folder above this
repository; ``--roots`` overrides it.
"""

from __future__ import annotations

import argparse
import filecmp
import importlib.util
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC_PY = REPO / "hoard_link"
SRC_JS = REPO / "js" / "hoard-link.js"
SRC_JS_COMMONS = REPO / "js" / "hoard-commons"      # the Node commons, vendored as server/hoard-commons/


def _load_drift():
    """The comparison logic lives in hoard_link/hub/drift.py, shared with the hub's Repos facet (which only
    reports what this script fixes). Loaded by path: it is standard library only and the package import
    would need httpx."""
    spec = importlib.util.spec_from_file_location("hoard_link_drift", SRC_PY / "hub" / "drift.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


drift = _load_drift()
SKIP = drift.VENDOR_SKIP


def find_vendored(app: Path, max_depth: int = 3) -> list[Path]:
    return drift.find_vendored(app, SRC_PY, max_depth)


def package_dir(app: Path) -> Path | None:
    for cand in sorted(app.iterdir()):
        if cand.is_dir() and cand.name not in SKIP and not cand.name.startswith(".") and (
                (cand / "__main__.py").is_file() or (cand / "main.py").is_file() or (cand / "api.py").is_file()):
            return cand
    return None


KEEP_IN_DST = drift.KEEP_IN_DST
#: The hub runs from this repository only; apps never need its subpackage.
SKIP_IN_SRC = drift.SKIP_IN_SRC


def _version() -> str:
    import re
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (SRC_PY / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "?"


def copy_tree(src: Path, dst: Path, dry: bool) -> tuple[int, int]:
    """Copy src over dst; returns (changed, removed)."""
    changed, removed = drift.plan_tree(src, dst)
    if not dry:
        for rel in changed:
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, d)
        for rel in removed:  # files that no longer exist upstream (a vendored hub/ subpackage goes too)
            (dst / rel).unlink()
        # empty folders left behind
        for root, _dirs, _names in os.walk(dst, topdown=False):
            if Path(root) != dst:
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
    return len(changed), len(removed)


def _sync_js_commons(folder: Path, dry: bool) -> int:
    """Copy ``js/hoard-commons/`` over ``<app>/server/hoard-commons/`` (Node apps only); returns the changes."""
    if not SRC_JS_COMMONS.is_dir():
        return 0
    if not dry and not (folder / "server" / "hoard-link.js").is_file():
        return 0
    dst = folder / "server" / "hoard-commons"
    changed, removed = drift.plan_tree(SRC_JS_COMMONS, dst)
    if not dry:
        for rel in changed:
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SRC_JS_COMMONS / rel, d)
        for rel in removed:
            (dst / rel).unlink()
    n = len(changed) + len(removed)
    print(f"  {folder.name}: server/hoard-commons  {len(changed)} file(s) updated, {len(removed)} removed"
          + (" [dry run]" if dry else ""))
    return n


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
            if folder.name.startswith(("_", ".")):  # _archivo, _tmp, .cache: archived or scratch, never an app
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
                total += 1 + _sync_js_commons(folder, args.dry_run)
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
                total += _sync_js_commons(folder, args.dry_run)
    print(f"{total} change(s)" + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
