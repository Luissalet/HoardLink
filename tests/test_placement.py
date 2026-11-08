"""PC <-> Spark placement: the transfer helpers both ends share, the settings patch, and the Hub's "never on both" guard."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
import time

from hoard_link.hub import placement_remote as pr


def _write(path, text, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_manifest_skips_excluded_folders_and_patterns(tmp_path):
    _write(tmp_path / "app.db", "x")
    _write(tmp_path / "logs" / "a.log", "x")
    _write(tmp_path / "native" / "products" / "data" / "browser-profile" / "Default" / "Cookies", "x")
    _write(tmp_path / "native" / "products" / "data" / "tantalus.db", "x")
    _write(tmp_path / "half.placement-tmp", "x")
    found = pr.manifest(str(tmp_path), ["logs", "*/browser-profile", "*.placement-tmp"])
    assert sorted(found) == ["app.db", "native/products/data/tantalus.db"]


def test_diff_tolerates_two_seconds_and_lists_deletions():
    source = {"a": [1, 10_000_000_000], "b": [2, 10_000_000_000], "c": [3, 10_000_000_000]}
    target = {"a": [1, 11_500_000_000], "b": [2, 13_000_000_000], "old": [1, 1]}
    changed, deleted = pr.diff(source, target)
    assert changed == ["b", "c"] and deleted == ["old"]


def test_stream_round_trip_keeps_mtimes_and_moves_deleted_files_to_trash(tmp_path):
    src, dst, trash = tmp_path / "src", tmp_path / "dst", tmp_path / "trash"
    _write(src / "db" / "app.db", "new contents", mtime=1_700_000_000)
    _write(src / "notes.txt", "hello", mtime=1_700_000_100)
    _write(dst / "stale.db-wal", "old wal")
    changed, deleted = pr.diff(pr.manifest(str(src), []), pr.manifest(str(dst), []))
    buf = io.BytesIO()
    assert pr.pack_to(str(src), changed, buf) == 2
    buf.seek(0)
    res = pr.apply_from(str(dst), buf, deleted, str(trash))
    assert res == {"extracted": 2, "removed": 1, "skipped": 0, "skipped_files": []}
    assert (dst / "db" / "app.db").read_text(encoding="utf-8") == "new contents"
    assert int((dst / "db" / "app.db").stat().st_mtime) == 1_700_000_000
    assert not (dst / "stale.db-wal").exists() and (trash / "stale.db-wal").read_text(encoding="utf-8") == "old wal"
    assert pr.diff(pr.manifest(str(src), []), pr.manifest(str(dst), [])) == ([], [])


def test_apply_refuses_paths_outside_the_folder(tmp_path):
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|") as tar:
        data = b"evil"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    pr.apply_from(str(tmp_path / "inside"), buf, ["../outside.txt"])
    assert not (tmp_path / "escape.txt").exists()


def test_patch_sets_inserts_and_removes_settings(tmp_path):
    db = tmp_path / "tantalus.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO settings VALUES ('notify.email.faustus_dir', 'E:/Development/IA/Faustus')")
    con.commit()
    con.close()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"recipes_dir": "C:\\recipes", "nodes": []}), encoding="utf-8")
    done = pr.patch({"sqlite": [{"db": str(db), "table": "settings", "key": "notify.email.faustus_dir", "value": "/home/x/faustus"},
                                {"db": str(db), "table": "settings", "key": "mail.faustus_dir", "value": "/home/x/faustus"}],
                     "json": [{"file": str(settings), "path": ["recipes_dir"], "value": "/home/x/recipes"}]})
    assert len(done) == 3
    con = sqlite3.connect(db)
    assert dict(con.execute("SELECT key, value FROM settings").fetchall()) == {
        "notify.email.faustus_dir": "/home/x/faustus", "mail.faustus_dir": "/home/x/faustus"}
    con.close()
    assert json.loads(settings.read_text(encoding="utf-8"))["recipes_dir"] == "/home/x/recipes"
    pr.patch({"sqlite": [{"db": str(db), "table": "settings", "key": "mail.faustus_dir", "value": None}],
              "json": [{"file": str(settings), "path": ["recipes_dir"], "value": None}]})
    con = sqlite3.connect(db)
    assert [r[0] for r in con.execute("SELECT key FROM settings")] == ["notify.email.faustus_dir"]
    con.close()
    assert "recipes_dir" not in json.loads(settings.read_text(encoding="utf-8"))


def test_units_carry_the_faustus_folder_for_hermes(tmp_path, monkeypatch):
    monkeypatch.setattr(pr.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: None)
    assert pr.units(str(tmp_path / "stack")) == ["faustus", "hermes", "prometheus"]
    assert pr.units(str(tmp_path / "stack")) == []                   # unchanged units: nothing to restart
    hermes = (tmp_path / ".config" / "systemd" / "user" / "hermes.service").read_text(encoding="utf-8")
    assert f"Environment=FAUSTUS_DIR={tmp_path / 'stack'}/faustus" in hermes
    assert "Environment=HERMES_ALLOWED_HOSTS=*.ts.net" in hermes
    prom = (tmp_path / ".config" / "systemd" / "user" / "prometheus.service").read_text(encoding="utf-8")
    assert f"Environment=PROMETHEUS_SSH_DIR={tmp_path / 'stack'}/prometheus-ssh" in prom
    assert "ExecStart=" in hermes and "-m hermes_hoard --no-browser" in hermes and "MemoryMax=2G" in hermes


# ---- the Hub never starts them here while they live on the Spark --------------------------------------------------------

def _hub(tmp_path):
    from hoard_link.hub.config import HubConfig
    from hoard_link.hub.core import Hub
    return Hub(HubConfig(port=0, data_dir=str(tmp_path / "data"), roots=[], icon_dirs=[], jobs_enabled=False,
                         faustus_urls=["http://127.0.0.1:1"]))


def test_hub_refuses_local_starts_while_on_the_spark(tmp_path):
    hub = _hub(tmp_path)
    try:
        assert hub.placement.status()["location"] == "pc"
        assert hub.placement.local_block("faustus") is None
        hub.placement._update(location="sparks", urls={"faustus": "https://spark3.example.ts.net"})   # links known: no lookup
        assert "Spark3" in hub.placement.local_block("hermes")
        assert hub.placement.local_block("comfyui") is None            # other apps are not part of the stack
        res = hub.faustus_start()
        assert res["ok"] is False and res["placement"] is True
        # the switch itself (its own thread) may start them: that is how they come back
        seen = {}
        t = threading.Thread(target=lambda: seen.update(block=hub.placement.local_block("faustus")), name="placement-switch")
        t.start(); t.join()
        assert seen["block"] is None
        assert hub.snapshot()["placement"]["location"] == "sparks"
    finally:
        hub.close()


def test_switch_refuses_unknown_targets_and_a_second_move(tmp_path):
    hub = _hub(tmp_path)
    try:
        assert hub.placement.switch("moon")["ok"] is False
        hub.placement._update(job={"running": True, "step": "datos de faustus", "log": []})
        res = hub.placement.switch("sparks")
        assert res["ok"] is False and "en marcha" in res["error"]
        assert "trasladando" in hub.placement.local_block("prometheus")
    finally:
        hub.close()


def test_a_switch_cut_short_by_a_hub_restart_is_reported_not_left_running(tmp_path):
    from hoard_link.hub.placement import Placement
    hub = _hub(tmp_path)
    try:
        hub.placement._update(job={"running": True, "step": "datos de faustus", "log": []})
        again = Placement(hub)
        job = again.state()["job"]
        assert job["running"] is False and job["ok"] is False and "a mitad" in job["error"]
    finally:
        hub.close()


def test_patches_point_hermes_and_prometheus_at_the_spark_and_back(tmp_path):
    from pathlib import Path
    from hoard_link.hub.placement import Placement, SPARK_STACK
    dirs = {"faustus": Path("E:/F"), "hermes": Path("E:/H"), "prometheus": Path("E:/P")}
    saved = {"hermes": {"data/native/products/data/tantalus.db|notify.email.faustus_dir": "E:/Development/IA/Faustus",
                        "data/native/shipments/data/phileas.db|mail.faustus_dir": None},
             "prometheus": {"recipes_dir": "C:\\recipes"}}
    there = Placement._patches(dirs, "sparks", saved)
    assert {p["key"]: p["value"] for p in there["sqlite"]} == {
        "notify.email.faustus_dir": f"{SPARK_STACK}/faustus", "mail.faustus_dir": f"{SPARK_STACK}/faustus",
        "notify.email.enabled": "1", "notify.email.min_severity": "medium"}      # parcels by e-mail: no toast or hub there
    assert there["json"][0]["value"] == f"{SPARK_STACK}/recipes"
    back = Placement._patches(dirs, "pc", saved)
    assert [p["value"] for p in back["sqlite"]] == ["E:/Development/IA/Faustus", None]   # keys never saved are left alone
    assert back["json"] == [{"file": str(Path("E:/P") / "data/settings.json"), "path": ["recipes_dir"], "value": "C:\\recipes"}]


def test_big_files_travel_as_changed_blocks_only(tmp_path):
    bs = 1024
    old = bytes(range(256)) * 16                      # 4 blocks of 1 KiB
    new = bytearray(old)
    new[1500] ^= 0xFF                                   # one byte in block 1
    new += b"tail" * 300                                # and 1200 more bytes: blocks 4 and 5
    src, dst = tmp_path / "src.db", tmp_path / "dst.db"
    src.write_bytes(bytes(new))
    dst.write_bytes(old)
    idx = pr.changed_blocks(pr.block_hashes(str(src), bs), pr.block_hashes(str(dst), bs))
    assert idx == [1, 4, 5]
    buf = io.BytesIO()
    sent = pr.send_blocks(str(src), idx, buf, bs)
    assert sent == 1024 + 1024 + 176
    buf.seek(0)
    pr.receive_blocks(str(dst), buf, len(new), 1_700_000_000_000_000_000)
    assert dst.read_bytes() == bytes(new) and dst.stat().st_mtime_ns == 1_700_000_000_000_000_000
    # a shrunk file is cut to size, and a stream that ends early leaves the old file untouched
    smaller = bytes(new[:2000])
    src.write_bytes(smaller)
    buf = io.BytesIO()
    pr.send_blocks(str(src), [], buf, bs)
    buf.seek(0)
    pr.receive_blocks(str(dst), buf, len(smaller), 1_700_000_000_000_000_000)
    assert dst.read_bytes() == smaller
    import pytest
    with pytest.raises(EOFError):
        pr.receive_blocks(str(dst), io.BytesIO(b"\x00" * 5), 10, 0)
    assert dst.read_bytes() == smaller and not (tmp_path / "dst.db.placement-tmp").exists()
