"""Oracle cache: the reference side is slow and does not change when only the probe does.

An oracle run is identified by its command line, the hash of the input file, the lock and the files the oracle reads
(``oracle_depends_on``). When all of them are the same as in a stored run, the stored output is copied back instead of
running the oracle again; the copy is verified against the hash recorded at the time. The probe is never cached.
A failed or empty run is never stored."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Optional

from .lock import sha256_file


class OracleCache:
    def __init__(self, directory: Path):
        self.dir = directory

    @staticmethod
    def key(argv: list[str], inputs_sha: str, lock_sha: str, deps_key: str) -> str:
        return hashlib.sha256(json.dumps([argv, inputs_sha, lock_sha, deps_key], ensure_ascii=False).encode()).hexdigest()

    def get(self, key: str, dest: Path) -> bool:
        folder = self.dir / key
        meta_path, out = folder / "meta.json", folder / "output.jsonl"
        if not (meta_path.exists() and out.exists()):
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            return False
        if out.stat().st_size == 0 or sha256_file(out) != meta.get("sha256"):
            shutil.rmtree(folder, ignore_errors=True)       # a damaged entry is dropped, never replayed
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, dest)
        return True

    def put(self, key: str, output: Path) -> Optional[str]:
        if not output.exists() or output.stat().st_size == 0:
            return None
        folder = self.dir / key
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output, folder / "output.jsonl")
        sha = sha256_file(output)
        (folder / "meta.json").write_text(json.dumps({"sha256": sha, "bytes": output.stat().st_size}), encoding="utf-8")
        return sha
