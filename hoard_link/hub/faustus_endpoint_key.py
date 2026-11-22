"""Read one encrypted credential on the host that owns the Faustus catalog."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys


def _read_key(database: Path, key_file: Path, endpoint_id: str, base_url: str) -> str:
    # Use the owning installation's cryptography dependency and existing key.
    # Opening the store here must never run app startup or generate a key.
    from cryptography.fernet import Fernet, InvalidToken

    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as store:
            row = store.execute(
                "SELECT api_key FROM model_endpoints WHERE id=? AND base_url=? AND is_enabled=1",
                (endpoint_id, base_url),
            ).fetchone()
        encrypted = row[0] if row else None
        if isinstance(encrypted, str) and encrypted.startswith("enc:"):
            return Fernet(key_file.read_bytes()).decrypt(encrypted[4:].encode("ascii")).decode("utf-8").strip()
    except (OSError, ValueError, InvalidToken, sqlite3.Error):
        pass
    return ""


def read_endpoint_key(root: Path, python: str, endpoint_id: str, base_url: str, *, placement=None) -> str:
    """Run the read-only helper on the catalog host and capture its private result."""
    try:
        source = Path(__file__).read_text(encoding="utf-8")
        if placement is None:
            process = subprocess.run(
                [str(python), "-B", "-X", "utf8", "-", endpoint_id, base_url], cwd=root,
                input=source, capture_output=True, encoding="utf-8", timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.returncode != 0:
                return ""
            output = process.stdout
        else:
            from .placement import STACK

            command = shlex.join(["venv/bin/python", "-B", "-X", "utf8", "-", endpoint_id, base_url])
            output = placement._ssh(f"cd {STACK}/faustus && {command}", input=source, timeout=15)
        key = json.loads(output)
        return key if isinstance(key, str) else ""
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        # SSH failures may include captured output. Keep diagnostics out of
        # worker receipts and logs even when the remote command fails.
        return ""


if __name__ == "__main__":
    from src.constants import APP_DB, APP_KEY_FILE

    sys.stdout.write(json.dumps(_read_key(Path(APP_DB), Path(APP_KEY_FILE), *sys.argv[1:])))
