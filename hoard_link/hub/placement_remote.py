"""Helper for moving Faustus, Hermes and Prometheus between this PC and a Spark (see ``placement.py``).

Standard library only: the Hub uploads this file to the Spark and runs it with the system ``python3``; the PC side
imports the same functions, so both ends compute file manifests, pack and apply changes, and patch settings the same way.
Tars travel as streams over SSH (``-`` = stdin / stdout), so nothing is staged on either disk.

    python3 placement_remote.py manifest <dir> <excludes.json>            -> {"rel/path": [size, mtime_ns]} on stdout
    python3 placement_remote.py pack <dir> <files.json> <out.tar|->       -> tar of the listed files (mtimes kept)
    python3 placement_remote.py apply <dir> <in.tar|-> <deleted.json|->   -> extract and remove the deleted ones
    python3 placement_remote.py hashes <file>                             -> blake2b of each 4 MB block, as a JSON list
    python3 placement_remote.py readblocks <file> <indices.json>          -> those blocks, framed, on stdout
    python3 placement_remote.py writeblocks <file> <size> <mtime_ns>      -> patch the file with framed blocks from stdin
    python3 placement_remote.py patch <patch.json>                        -> set values in SQLite settings / JSON files
    python3 placement_remote.py units <stack_dir>                         -> write the systemd user units
    python3 placement_remote.py svc <start|stop|restart|enable|disable|status> [names...]
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import IO, Optional

SERVICES = ("faustus", "hermes", "prometheus")


# ====================================================================================== manifests and transfers
def excluded(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    for pattern in patterns:
        p = pattern.rstrip("/")
        if rel == p or rel.startswith(p + "/") or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def manifest(root: str, patterns: list[str]) -> dict[str, list[int]]:
    base = Path(os.path.expanduser(root))
    out: dict[str, list[int]] = {}
    if not base.is_dir():
        return out
    for folder, dirs, files in os.walk(base):
        rel_dir = Path(folder).relative_to(base).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirs[:] = [d for d in dirs if not excluded(rel_dir + d, patterns)]
        for name in files:
            rel = rel_dir + name
            if excluded(rel, patterns):
                continue
            try:
                st = os.stat(os.path.join(folder, name))
            except OSError:
                continue
            out[rel] = [st.st_size, st.st_mtime_ns]
    return out


def diff(source: dict[str, list[int]], target: dict[str, list[int]]) -> tuple[list[str], list[str]]:
    """(changed, deleted): files to copy from source, files to remove from target. A file counts as unchanged when its
    size matches and its modification time is within two seconds (FAT/NTFS/ext4 keep different precisions)."""
    changed = [rel for rel, (size, mtime) in source.items()
               if rel not in target or target[rel][0] != size or abs(target[rel][1] - mtime) > 2_000_000_000]
    deleted = [rel for rel in target if rel not in source]
    return sorted(changed), sorted(deleted)


def pack_to(root: str, files: list[str], fileobj: IO[bytes]) -> int:
    """Write a streamed tar of ``files`` (relative to ``root``) into ``fileobj``."""
    root = os.path.expanduser(root)
    count = 0
    with tarfile.open(fileobj=fileobj, mode="w|") as tar:
        for rel in files:
            path = os.path.join(root, rel)
            try:
                with open(path, "rb") as fh:          # opened first: a file locked or gone since the manifest is skipped whole
                    info = tar.gettarinfo(arcname=rel, fileobj=fh)
                    if not info.isfile():
                        continue
                    tar.addfile(info, fh)
                count += 1
            except (FileNotFoundError, PermissionError, IsADirectoryError):
                continue                               # a temp file another process was replacing: it is not state
    return count


def pack(root: str, files: list[str], out: str) -> int:
    if out == "-":
        n = pack_to(root, files, sys.stdout.buffer)
        sys.stdout.buffer.flush()
        return n
    with open(out, "wb") as fh:
        return pack_to(root, files, fh)


def apply_from(root: str, fileobj: Optional[IO[bytes]], deleted: list[str], trash: Optional[str] = None) -> dict[str, int]:
    """Extract a streamed tar into ``root`` (each file written aside and renamed, mtimes kept), then remove ``deleted``
    — moved into ``trash`` when given, so a bad transfer never loses anything for good."""
    base = Path(os.path.expanduser(root))
    base.mkdir(parents=True, exist_ok=True)
    resolved = base.resolve()
    extracted = 0
    skipped: list[str] = []
    if fileobj is not None:
        with tarfile.open(fileobj=fileobj, mode="r|") as tar:
            for member in tar:
                target = (base / member.name).resolve()
                if not member.isfile() or (resolved not in target.parents):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                tmp = target.with_name(target.name + ".placement-tmp")
                with open(tmp, "wb") as fh:
                    while chunk := src.read(1 << 20):
                        fh.write(chunk)
                try:
                    os.replace(tmp, target)
                except PermissionError:           # a file this account may not replace (locked, or an ACL that shuts it out)
                    tmp.unlink(missing_ok=True)
                    skipped.append(member.name)
                    continue
                os.utime(target, ns=(int(member.mtime) * 1_000_000_000, int(member.mtime) * 1_000_000_000))
                extracted += 1
    removed = 0
    for rel in deleted:
        path = (base / rel).resolve()
        if resolved in path.parents and path.is_file():
            try:
                if trash:
                    dest = Path(trash) / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dest))
                else:
                    path.unlink()
            except PermissionError:
                skipped.append(rel)
                continue
            removed += 1
    return {"extracted": extracted, "removed": removed, "skipped": len(skipped), "skipped_files": skipped[:20]}


# ====================================================================================== big files: only the blocks that changed
# A database of gigabytes changes a few pages between two moves; sending it whole each time would take minutes. Both ends hash
# fixed blocks, and only the blocks that differ travel, framed as [offset u64][length u32][bytes] and ended by END.
BLOCK = 4 << 20
BIG_FILE = 16 << 20
END = (1 << 64) - 1
_FRAME = struct.Struct(">QI")


def block_hashes(path: str, bs: int = BLOCK) -> list[str]:
    out = []
    try:
        with open(os.path.expanduser(path), "rb") as fh:
            while chunk := fh.read(bs):
                out.append(hashlib.blake2b(chunk, digest_size=16).hexdigest())
    except FileNotFoundError:
        pass
    return out


def changed_blocks(source: list[str], target: list[str]) -> list[int]:
    return [i for i, h in enumerate(source) if i >= len(target) or target[i] != h]


def send_blocks(path: str, indices: list[int], out: IO[bytes], bs: int = BLOCK) -> int:
    sent = 0
    with open(os.path.expanduser(path), "rb") as fh:
        for i in indices:
            fh.seek(i * bs)
            data = fh.read(bs)
            out.write(_FRAME.pack(i * bs, len(data)))
            out.write(data)
            sent += len(data)
    out.write(_FRAME.pack(END, 0))
    out.flush()
    return sent


def _read_exact(stream: IO[bytes], n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise EOFError("block stream ended early")
        buf += chunk
    return bytes(buf)


def receive_blocks(path: str, stream: IO[bytes], size: int, mtime_ns: int) -> int:
    """Patch ``path`` with the framed blocks read from ``stream`` (on a copy, renamed over it at the end), cut it to ``size``
    and give it ``mtime_ns``. Returns the bytes written."""
    path = os.path.expanduser(path)
    tmp = path + ".placement-tmp"
    if os.path.isfile(path):
        shutil.copyfile(path, tmp)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(tmp, "wb").close()
    written = 0
    try:
        with open(tmp, "r+b") as fh:
            while True:
                offset, length = _FRAME.unpack(_read_exact(stream, _FRAME.size))
                if offset == END:
                    break
                fh.seek(offset)
                fh.write(_read_exact(stream, length))
                written += length
            fh.truncate(size)
        os.utime(tmp, ns=(mtime_ns, mtime_ns))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return written


def apply(root: str, tar_path: str, deleted: list[str], trash: Optional[str] = None) -> dict[str, int]:
    if tar_path == "-":
        return apply_from(root, sys.stdin.buffer, deleted, trash)
    if tar_path and tar_path != "none" and os.path.isfile(tar_path):
        with open(tar_path, "rb") as fh:
            return apply_from(root, fh, deleted, trash)
    return apply_from(root, None, deleted, trash)


# ====================================================================================== settings that hold machine paths
def patch(spec: dict) -> list[str]:
    """``{"sqlite": [{"db", "table", "key", "value"}], "json": [{"file", "path": [...], "value"}]}`` -> what changed.
    A ``value`` of None removes the key (back to "not set")."""
    done = []
    for item in spec.get("sqlite") or []:
        db = os.path.expanduser(item["db"])
        if not os.path.isfile(db):
            continue
        con = sqlite3.connect(db, timeout=30)
        try:
            table = item.get("table", "settings")
            if not table.replace("_", "").isalnum():
                continue
            if item["value"] is None:
                con.execute(f"DELETE FROM {table} WHERE key = ?", (item["key"],))
            else:
                con.execute(f"INSERT INTO {table} (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (item["key"], item["value"]))
            con.commit()
            done.append(f"{os.path.basename(db)}:{item['key']}")
        finally:
            con.close()
    for item in spec.get("json") or []:
        path = os.path.expanduser(item["file"])
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        node = data
        for key in item["path"][:-1]:
            node = node.setdefault(key, {}) if isinstance(node, dict) else node[int(key)]
        last = item["path"][-1]
        if isinstance(node, list):
            node[int(last)] = item["value"]
        elif item["value"] is None:
            node.pop(last, None)
        else:
            node[last] = item["value"]
        tmp = path + ".placement-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        done.append(f"{os.path.basename(path)}:{'/'.join(map(str, item['path']))}")
    return done


# ====================================================================================== systemd user units (Spark side)
UNIT = """[Unit]
Description={desc} (Hoard Hub placement)
After=network-online.target

[Service]
WorkingDirectory={cwd}
ExecStart={cwd}/venv/bin/python {args}
Environment=PYTHONUNBUFFERED=1
{env}Restart=on-failure
RestartSec=10
TimeoutStopSec=30
MemoryMax={memory}
OOMScoreAdjust=800

[Install]
WantedBy=default.target
"""

# name -> (description, folder, arguments, memory cap, extra environment)
# The apps answer loopback only unless told otherwise; on the Spark they are published on the tailnet (tailscale serve),
# whose names end in .ts.net and only reach the person's own devices.
TAILNET = "*.ts.net"
UNITS = {
    "faustus": ("Faustus", "faustus", "server_runtime.py serve --port 7000 --token placement", "6G", {}),
    "hermes": ("Hermes's Hoard", "hermes", "-m hermes_hoard --no-browser", "2G",
               {"FAUSTUS_DIR": "{stack}/faustus", "HERMES_ALLOWED_HOSTS": TAILNET, "TANTALUS_ALLOWED_HOSTS": TAILNET,
                "PHILEAS_ALLOWED_HOSTS": TAILNET}),
    "prometheus": ("Prometheus's Hoard", "prometheus", "-m prometheus_hoard --no-browser", "1G",
                   {"PROMETHEUS_ALLOWED_HOSTS": TAILNET, "PROMETHEUS_SSH_DIR": "{stack}/prometheus-ssh"}),
}


def units(stack: str) -> list[str]:
    """Write the three unit files; returns the services whose unit changed (they need a restart to take it)."""
    stack = os.path.expanduser(stack)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, (desc, folder, args, memory, env) in UNITS.items():
        env_lines = "".join(f"Environment={k}={v.format(stack=stack)}\n" for k, v in env.items())
        text = UNIT.format(desc=desc, cwd=f"{stack}/{folder}", args=args, memory=memory, env=env_lines)
        path = unit_dir / f"{name}.service"
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed.append(name)
    if changed:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return changed


def svc(action: str, names: list[str]) -> dict[str, str]:
    names = [n for n in (names or SERVICES) if n in SERVICES]
    if action == "status":
        out = {}
        for n in names:
            r = subprocess.run(["systemctl", "--user", "is-active", f"{n}.service"], capture_output=True, text=True)
            out[n] = r.stdout.strip() or r.stderr.strip()
        return out
    if action not in ("start", "stop", "restart", "enable", "disable"):
        return {"rc": "2", "error": f"unknown action {action}"}
    r = subprocess.run(["systemctl", "--user", action, *[f"{n}.service" for n in names]], capture_output=True, text=True)
    return {"rc": str(r.returncode), "error": r.stderr.strip()[-400:]}


def _json_arg(path: str) -> list:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path and path != "-" else []


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "manifest":
        print(json.dumps(manifest(argv[2], _json_arg(argv[3]) if len(argv) > 3 else [])))
    elif cmd == "pack":
        n = pack(argv[2], _json_arg(argv[3]), argv[4])
        if argv[4] != "-":
            print(json.dumps({"packed": n}))
    elif cmd == "apply":
        print(json.dumps(apply(argv[2], argv[3], _json_arg(argv[4]) if len(argv) > 4 else [])))
    elif cmd == "hashes":
        print(json.dumps(block_hashes(argv[2])))
    elif cmd == "readblocks":
        send_blocks(argv[2], _json_arg(argv[3]), sys.stdout.buffer)
    elif cmd == "writeblocks":
        print(json.dumps({"written": receive_blocks(argv[2], sys.stdin.buffer, int(argv[3]), int(argv[4]))}))
    elif cmd == "patch":
        print(json.dumps(patch(json.loads(Path(argv[2]).read_text(encoding="utf-8")))))
    elif cmd == "units":
        print(json.dumps(units(argv[2])))
    elif cmd == "svc":
        print(json.dumps(svc(argv[2], argv[3:])))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
