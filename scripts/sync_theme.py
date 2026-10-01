"""Copy the canonical hoard_link/ui/hoard-theme.css over every vendored copy.

Each app keeps its copy wherever its build picks CSS up (client/src,
frontend/src, static/…); this script finds every file called
``hoard-theme.css`` under the app folders the hub knows (or the roots given)
and makes it byte-identical to the canonical one. Build folders are skipped.

    python scripts/sync_theme.py                 # roots from data/hub.json or the parent folder
    python scripts/sync_theme.py --check         # exit 1 if any copy differs
    python scripts/sync_theme.py --root "D:/Proyectos independientes"
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANON = os.path.join(HERE, "hoard_link", "ui", "hoard-theme.css")
SKIP = {"node_modules", "dist", "build", ".git", ".venv", "venv", "__pycache__", "data", "release", "dist-electron",
        "assets"}


def copies(root: str):
    for top in sorted(os.listdir(root)):
        folder = os.path.join(root, top)
        if not os.path.isdir(folder) or os.path.abspath(folder) == HERE:
            continue
        if not os.path.isfile(os.path.join(folder, "faustus-plugin.json")):
            continue
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d not in SKIP]
            if "hoard-theme.css" in filenames:
                yield os.path.join(dirpath, "hoard-theme.css")


def digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    want = digest(CANON)
    with open(CANON, "rb") as fh:
        body = fh.read()
    stale = 0
    for path in copies(args.root):
        same = digest(path) == want
        if same:
            print(f"ok      {path}")
            continue
        stale += 1
        if args.check:
            print(f"STALE   {path}")
        else:
            with open(path, "wb") as fh:
                fh.write(body)
            print(f"updated {path}")
    return 1 if (args.check and stale) else 0


if __name__ == "__main__":
    sys.exit(main())
