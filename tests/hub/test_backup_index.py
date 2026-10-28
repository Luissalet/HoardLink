"""Snapshot summaries come from a persisted index, so a manifest is parsed once per version."""
import json
import os

from hoard_link.hub import backup
from hoard_link.hub.backup import BackupStore


def _manifest(path, sid, files):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"id": sid, "ts": 1.0, "apps": {"writer": {}}, "totals": {"files": files, "bytes": 10}}, fh)


def test_list_snapshots_parses_each_manifest_once(tmp_path, monkeypatch):
    store = BackupStore(str(tmp_path))
    os.makedirs(store.snapshots_dir)
    _manifest(os.path.join(store.snapshots_dir, "a.json"), "a", 1)
    assert [s["id"] for s in store.list_snapshots()] == ["a"]
    assert os.path.isfile(os.path.join(str(tmp_path), "snapshot-index.json"))

    calls = []
    real = backup.json.load
    monkeypatch.setattr(backup.json, "load", lambda fh: calls.append(1) or real(fh))
    fresh = BackupStore(str(tmp_path))  # a restarted hub reads the index from disk
    assert fresh.list_snapshots()[0]["files"] == 1
    assert len(calls) == 1  # the index itself, not the manifest

    _manifest(os.path.join(store.snapshots_dir, "b.json"), "b", 2)
    assert [s["id"] for s in fresh.list_snapshots()] == ["a", "b"]
    os.remove(os.path.join(store.snapshots_dir, "a.json"))
    assert [s["id"] for s in fresh.list_snapshots()] == ["b"]


def test_changed_manifest_is_reread(tmp_path):
    store = BackupStore(str(tmp_path))
    os.makedirs(store.snapshots_dir)
    path = os.path.join(store.snapshots_dir, "a.json")
    _manifest(path, "a", 1)
    assert store.list_snapshots()[0]["files"] == 1
    _manifest(path, "a", 22)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert store.list_snapshots()[0]["files"] == 22


def test_store_totals_survive_restart_and_recount_after_change(tmp_path):
    store = BackupStore(str(tmp_path), background=False)
    os.makedirs(os.path.join(store.objects_dir, "ab"))
    with open(os.path.join(store.objects_dir, "ab", "abc"), "wb") as fh:
        fh.write(b"12345")
    assert store._store_totals() == (5, 1)
    with open(os.path.join(store.objects_dir, "ab", "abd"), "wb") as fh:
        fh.write(b"123")
    assert store._store_totals() == (5, 1)  # fresh figures are reused, not recounted on every read
    again = BackupStore(str(tmp_path), background=False)
    assert again._store_totals() == (5, 1)  # read back from the index after a restart
    again._store_cache = (0.0, *again._store_cache[1:])  # what a run or prune leaves behind
    assert again._store_totals() == (8, 2)
