"""Backups of every app's data folder, deduplicated, restorable, verified.

Twenty apps, twenty ``data/`` folders, twenty SQLite files and none of
them copied anywhere: that is what this fixes. A **snapshot** walks the
data folder of every app the hub knows (plus the hub's own settings),
stores each file once by content hash under ``<backups>/objects/`` and
writes a manifest ``<backups>/snapshots/<stamp>.json`` that maps paths to
hashes. Unchanged files cost nothing the second time; a snapshot of 2 GB
where 5 MB changed adds 5 MB.

SQLite databases are not copied as files (a copy taken mid-write is
corrupt): they are read through ``sqlite3``'s online backup API into a
temporary file first, so what is stored is a consistent database even
while the app writes. Their ``-wal`` / ``-shm`` sidecars are skipped.

What is skipped by default: ``logs/``, ``profiles/`` (browser profiles),
caches (``*cache*``, ``__pycache__``), temporary files, anything over
``max_file_mb`` (models and renders belong to their own backup), and
what ``backup.exclude`` in ``hub.json`` adds. Every skip is counted and
listed by reason so "why is X not in the backup?" is answered by the
manifest, not by guessing.

**Restore** writes a snapshot's files for one app to a folder: by default
``<app>/data.restored-<stamp>`` next to the live data, never over it; an
in-place restore is allowed only when the app is not running, and the
live folder is moved to ``data.before-restore-<stamp>`` first. **Verify**
re-hashes every object a snapshot references. **Prune** keeps the last N
snapshots and deletes objects nothing references any more.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

DEFAULT_EXCLUDE = ["logs", "logs/**", "profiles", "profiles/**", "*.log", "*.tmp", "*.part", "*-wal", "*-shm",
                   "*-journal", "__pycache__", "__pycache__/**", "*cache*", "*cache*/**", "tmp", "tmp/**",
                   "*.lock", "url", "commands.json", "leases.json"]
SQLITE_EXT = (".db", ".sqlite", ".sqlite3", ".db3")
DEFAULT_MAX_FILE_MB = 512
DEFAULT_KEEP = 14
CHUNK = 1 << 20


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")


def _excluded(rel: str, patterns: Iterable[str]) -> Optional[str]:
    rel_posix = rel.replace(os.sep, "/")
    parts = rel_posix.split("/")
    for pat in patterns:
        if fnmatch.fnmatchcase(rel_posix, pat) or fnmatch.fnmatchcase(parts[-1], pat):
            return pat
        # A folder pattern ("logs") excludes everything under it.
        if any(fnmatch.fnmatchcase(p, pat) for p in parts[:-1]):
            return pat
    return None


def _sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _is_sqlite(path: str) -> bool:
    if not path.lower().endswith(SQLITE_EXT):
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _sqlite_consistent_copy(path: str, dest: str) -> None:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


class BackupStore:
    def __init__(self, root: str, *, exclude: Optional[list[str]] = None, max_file_mb: float = DEFAULT_MAX_FILE_MB,
                 now: Callable[[], float] = time.time):
        self.root = os.path.abspath(root)
        self.objects_dir = os.path.join(self.root, "objects")
        self.snapshots_dir = os.path.join(self.root, "snapshots")
        self.exclude = list(DEFAULT_EXCLUDE) + [str(p) for p in (exclude or [])]
        self.max_file_bytes = int(float(max_file_mb) * 1024 * 1024)
        self._now = now
        self._lock = threading.Lock()
        self.busy: Optional[str] = None

    # -- objects ----------------------------------------------------------------
    def _object_path(self, sha: str) -> str:
        return os.path.join(self.objects_dir, sha[:2], sha)

    def has_object(self, sha: str) -> bool:
        return os.path.isfile(self._object_path(sha))

    def _store_file(self, path: str) -> tuple[str, int, bool]:
        """Hash ``path`` and copy it into the store unless present.
        Returns ``(sha, size, was_new)``."""
        sha, size = _sha256_file(path)
        dest = self._object_path(sha)
        if os.path.isfile(dest):
            return sha, size, False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        shutil.copyfile(path, tmp)
        os.replace(tmp, dest)
        return sha, size, True

    # -- snapshots ------------------------------------------------------------
    def list_snapshots(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not os.path.isdir(self.snapshots_dir):
            return out
        for name in sorted(os.listdir(self.snapshots_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.snapshots_dir, name), "r", encoding="utf-8") as fh:
                    m = json.load(fh)
            except (OSError, ValueError):
                continue
            out.append({"id": m.get("id", name[:-5]), "ts": m.get("ts"), "apps": sorted((m.get("apps") or {}).keys()),
                        "files": m.get("totals", {}).get("files", 0), "bytes": m.get("totals", {}).get("bytes", 0),
                        "new_bytes": m.get("totals", {}).get("new_bytes", 0), "skipped": m.get("totals", {}).get("skipped", 0),
                        "label": m.get("label", ""), "ok": m.get("ok", True)})
        return out

    def load_snapshot(self, snapshot_id: str) -> Optional[dict[str, Any]]:
        if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id:
            return None
        path = os.path.join(self.snapshots_dir, snapshot_id + ".json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def latest(self) -> Optional[str]:
        snaps = self.list_snapshots()
        return snaps[-1]["id"] if snaps else None

    def snapshot(self, sources: dict[str, str], *, label: str = "", progress: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        """Back up ``sources`` (app id -> data folder). Returns the manifest summary."""
        with self._lock:
            if self.busy:
                return {"ok": False, "error": f"a backup is already running: {self.busy}"}
            self.busy = "snapshot"
        try:
            return self._snapshot(sources, label=label, progress=progress)
        finally:
            with self._lock:
                self.busy = None

    def _snapshot(self, sources: dict[str, str], *, label: str, progress: Optional[Callable[[str], None]]) -> dict[str, Any]:
        ts = self._now()
        sid = _stamp(ts)
        os.makedirs(self.snapshots_dir, exist_ok=True)
        os.makedirs(self.objects_dir, exist_ok=True)
        manifest: dict[str, Any] = {"id": sid, "ts": ts, "label": label, "apps": {}, "totals": {}, "ok": True}
        t_files = t_bytes = t_new = t_new_bytes = t_skipped = 0
        errors: list[str] = []
        for app_id, folder in sorted(sources.items()):
            if progress:
                progress(app_id)
            entry: dict[str, Any] = {"folder": folder, "files": [], "skipped": [], "errors": []}
            if not folder or not os.path.isdir(folder):
                entry["missing"] = True
                manifest["apps"][app_id] = entry
                continue
            for root, dirs, names in os.walk(folder):
                # Never walk into the store itself when it lives under a source (the hub's own data/).
                if os.path.abspath(root) == self.root or os.path.abspath(root).startswith(self.root + os.sep):
                    dirs[:] = []
                    if os.path.abspath(root) == self.root:
                        entry["skipped"].append({"path": os.path.relpath(root, folder).replace(os.sep, "/") + "/", "why": "backup store"})
                    continue
                rel_root = os.path.relpath(root, folder)
                rel_root = "" if rel_root == "." else rel_root
                # Prune excluded folders early (browser profiles are huge).
                keep_dirs = []
                for d in sorted(dirs):
                    rel_d = os.path.join(rel_root, d) if rel_root else d
                    why = _excluded(rel_d, self.exclude)
                    if why:
                        entry["skipped"].append({"path": rel_d.replace(os.sep, "/") + "/", "why": why})
                    else:
                        keep_dirs.append(d)
                dirs[:] = keep_dirs
                for name in sorted(names):
                    rel = os.path.join(rel_root, name) if rel_root else name
                    full = os.path.join(root, name)
                    why = _excluded(rel, self.exclude)
                    if why:
                        entry["skipped"].append({"path": rel.replace(os.sep, "/"), "why": why}); continue
                    try:
                        st = os.stat(full)
                    except OSError as exc:
                        entry["errors"].append(f"{rel}: {exc}"); continue
                    if st.st_size > self.max_file_bytes:
                        entry["skipped"].append({"path": rel.replace(os.sep, "/"), "why": f"> {self.max_file_bytes // (1024*1024)} MB",
                                                 "bytes": st.st_size}); continue
                    try:
                        if _is_sqlite(full):
                            fd, tmp = tempfile.mkstemp(prefix="hoard-bk-", suffix=".db")
                            os.close(fd)
                            try:
                                _sqlite_consistent_copy(full, tmp)
                                sha, size, new = self._store_file(tmp)
                            finally:
                                try:
                                    os.remove(tmp)
                                except OSError:
                                    pass
                            kind = "sqlite"
                        else:
                            sha, size, new = self._store_file(full)
                            kind = "file"
                    except PermissionError:
                        # Held exclusively by the app (a DuckDB file, a Windows lock):
                        # not an error of the backup, a fact about the app. A rule
                        # "backup when the app stops" catches it later.
                        entry["skipped"].append({"path": rel.replace(os.sep, "/"), "why": "locked by the app (in use)",
                                                 "bytes": st.st_size}); continue
                    except Exception as exc:  # noqa: BLE001
                        entry["errors"].append(f"{rel}: {type(exc).__name__}: {exc}"); continue
                    entry["files"].append({"path": rel.replace(os.sep, "/"), "sha": sha, "bytes": size,
                                           "mtime": st.st_mtime, "kind": kind})
                    t_files += 1
                    t_bytes += size
                    if new:
                        t_new += 1
                        t_new_bytes += size
            entry["totals"] = {"files": len(entry["files"]), "bytes": sum(f["bytes"] for f in entry["files"]),
                               "skipped": len(entry["skipped"]), "errors": len(entry["errors"])}
            t_skipped += len(entry["skipped"])
            errors += [f"{app_id}: {e}" for e in entry["errors"]]
            manifest["apps"][app_id] = entry
        manifest["totals"] = {"apps": len(sources), "files": t_files, "bytes": t_bytes, "new_files": t_new,
                              "new_bytes": t_new_bytes, "skipped": t_skipped, "errors": len(errors),
                              "seconds": round(self._now() - ts, 2)}
        manifest["ok"] = not errors
        path = os.path.join(self.snapshots_dir, sid + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return {"ok": manifest["ok"], "snapshot": sid, "ts": ts, "label": label, "totals": manifest["totals"],
                "apps": {k: v.get("totals", {"missing": True}) for k, v in manifest["apps"].items()},
                "errors": errors[:20], "root": self.root}

    # -- restore --------------------------------------------------------------
    def restore(self, snapshot_id: str, app_id: str, *, dest: Optional[str] = None, in_place: bool = False,
                app_running: Optional[bool] = None) -> dict[str, Any]:
        m = self.load_snapshot(snapshot_id)
        if m is None:
            return {"ok": False, "error": f"unknown snapshot: {snapshot_id}"}
        entry = (m.get("apps") or {}).get(app_id)
        if not entry or entry.get("missing"):
            return {"ok": False, "error": f"snapshot {snapshot_id} has no data for {app_id}"}
        live = entry.get("folder") or ""
        stamp = _stamp(self._now())
        if in_place:
            if app_running:
                return {"ok": False, "error": f"{app_id} is running: stop it before restoring in place"}
            if not live:
                return {"ok": False, "error": "the snapshot does not record the live folder"}
            target = live
        else:
            target = dest or (os.path.join(os.path.dirname(live.rstrip("/\\")), os.path.basename(live.rstrip("/\\")) + f".restored-{stamp}")
                              if live else os.path.join(self.root, "restored", f"{app_id}-{stamp}"))
        target = os.path.abspath(target)
        if os.path.abspath(self.root) in (target, os.path.dirname(target)) and not dest:
            pass
        if target.startswith(self.objects_dir) or target.startswith(self.snapshots_dir):
            return {"ok": False, "error": "refusing to restore into the backup store itself"}
        moved_aside = None
        if in_place and os.path.isdir(target) and os.listdir(target):
            moved_aside = target.rstrip("/\\") + f".before-restore-{stamp}"
            os.rename(target, moved_aside)
        os.makedirs(target, exist_ok=True)
        if not in_place and os.listdir(target):
            return {"ok": False, "error": f"destination is not empty: {target}"}
        written, missing = 0, []
        for f in entry.get("files") or []:
            src = self._object_path(f["sha"])
            if not os.path.isfile(src):
                missing.append(f["path"]); continue
            out = os.path.join(target, f["path"].replace("/", os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copyfile(src, out)
            try:
                os.utime(out, (f.get("mtime") or self._now(), f.get("mtime") or self._now()))
            except OSError:
                pass
            written += 1
        return {"ok": not missing, "snapshot": snapshot_id, "app": app_id, "dest": target, "files": written,
                "missing": missing[:20], "in_place": in_place, "previous_data": moved_aside,
                "skipped_in_snapshot": len(entry.get("skipped") or [])}

    # -- verify / prune -------------------------------------------------------
    def verify(self, snapshot_id: Optional[str] = None) -> dict[str, Any]:
        sid = snapshot_id or self.latest()
        if not sid:
            return {"ok": False, "error": "no snapshots"}
        m = self.load_snapshot(sid)
        if m is None:
            return {"ok": False, "error": f"unknown snapshot: {sid}"}
        checked, bad, missing = 0, [], []
        seen: set[str] = set()
        for app_id, entry in (m.get("apps") or {}).items():
            for f in entry.get("files") or []:
                sha = f["sha"]
                if sha in seen:
                    continue
                seen.add(sha)
                path = self._object_path(sha)
                if not os.path.isfile(path):
                    missing.append(f"{app_id}/{f['path']}"); continue
                got, _ = _sha256_file(path)
                checked += 1
                if got != sha:
                    bad.append(f"{app_id}/{f['path']}")
        return {"ok": not bad and not missing, "snapshot": sid, "objects_checked": checked,
                "corrupt": bad[:20], "missing": missing[:20]}

    def prune(self, keep: int = DEFAULT_KEEP) -> dict[str, Any]:
        with self._lock:
            if self.busy:
                return {"ok": False, "error": f"busy: {self.busy}"}
            self.busy = "prune"
        try:
            snaps = self.list_snapshots()
            keep = max(1, int(keep))
            drop = snaps[:-keep] if len(snaps) > keep else []
            for s in drop:
                try:
                    os.remove(os.path.join(self.snapshots_dir, s["id"] + ".json"))
                except OSError:
                    pass
            referenced: set[str] = set()
            for s in self.list_snapshots():
                m = self.load_snapshot(s["id"]) or {}
                for entry in (m.get("apps") or {}).values():
                    for f in entry.get("files") or []:
                        referenced.add(f["sha"])
            removed, freed = 0, 0
            if os.path.isdir(self.objects_dir):
                for sub in os.listdir(self.objects_dir):
                    d = os.path.join(self.objects_dir, sub)
                    if not os.path.isdir(d):
                        continue
                    for name in os.listdir(d):
                        if name.endswith(".tmp") or name not in referenced:
                            p = os.path.join(d, name)
                            try:
                                freed += os.path.getsize(p)
                                os.remove(p)
                                removed += 1
                            except OSError:
                                pass
            return {"ok": True, "dropped_snapshots": [s["id"] for s in drop], "kept": min(len(snaps), keep),
                    "objects_removed": removed, "bytes_freed": freed}
        finally:
            with self._lock:
                self.busy = None

    def status(self) -> dict[str, Any]:
        snaps = self.list_snapshots()
        size, count = 0, 0
        if os.path.isdir(self.objects_dir):
            for root, _dirs, names in os.walk(self.objects_dir):
                for n in names:
                    try:
                        size += os.path.getsize(os.path.join(root, n)); count += 1
                    except OSError:
                        pass
        last = snaps[-1] if snaps else None
        return {"root": self.root, "snapshots": len(snaps), "objects": count, "store_bytes": size,
                "last": last, "busy": self.busy, "exclude": self.exclude, "max_file_mb": self.max_file_bytes // (1024 * 1024)}

    def diff(self, a: str, b: str, app_id: Optional[str] = None) -> dict[str, Any]:
        ma, mb = self.load_snapshot(a), self.load_snapshot(b)
        if ma is None or mb is None:
            return {"ok": False, "error": "unknown snapshot"}
        out: dict[str, Any] = {"ok": True, "a": a, "b": b, "apps": {}}
        apps = [app_id] if app_id else sorted(set(ma.get("apps", {})) | set(mb.get("apps", {})))
        for aid in apps:
            fa = {f["path"]: f["sha"] for f in (ma.get("apps", {}).get(aid) or {}).get("files") or []}
            fb = {f["path"]: f["sha"] for f in (mb.get("apps", {}).get(aid) or {}).get("files") or []}
            out["apps"][aid] = {"added": sorted(set(fb) - set(fa))[:50], "removed": sorted(set(fa) - set(fb))[:50],
                                "changed": sorted(p for p in set(fa) & set(fb) if fa[p] != fb[p])[:50]}
        return out
