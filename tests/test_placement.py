"""PC <-> Spark placement: the transfer helpers both ends share, the settings patch, and the Hub's "never on both" guard."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

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


TEST_NODE = {"name": "Studio node", "ssh": "studio-node", "host": "192.0.2.10"}


@pytest.mark.parametrize("refresh", [False, True], ids=["deploy", "refresh-after-interruption"])
def test_spark_services_restart_with_the_mail_installation(tmp_path, monkeypatch, refresh):
    from hoard_link.hub.placement import APPS, STACK, Placement
    from hoard_link.mail_helper import INSTALLATION_FILE

    stack = str(tmp_path / "remote").replace("\\", "/")
    monkeypatch.setattr("hoard_link.hub.placement.SPARK_STACK", stack)
    hermes_data = tmp_path / "remote" / "hermes" / "data"
    _write(hermes_data / "modules.json", '{"enabled": ["shipments", "products"]}')
    for module in ("shipments", "products"):
        _write(hermes_data / module / "mcp-token", f"synthetic-{module}-token")
    declaration = tmp_path / "remote" / "faustus" / "data" / INSTALLATION_FILE
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    monkeypatch.setattr(pr.Path, "home", staticmethod(lambda: tmp_path))
    reloaded = []
    monkeypatch.setattr(pr.subprocess, "run", lambda argv, **kw: reloaded.append(argv))
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path / "hub")), get=lambda _: None,
                          facet=lambda _: SimpleNamespace(list=lambda: [
                              {"id": "shared", "mail_accounts": ["*"], "apps": ["phileas", "tantalus"], "name": "Shared"},
                          ]), apps=[SimpleNamespace(id=id_, name=id_.title()) for id_ in ("hub", "phileas", "tantalus")])
    pl = Placement(hub)
    pl._update(node=TEST_NODE, location="sparks" if refresh else "pc")
    dirs = {app: tmp_path / app for app in APPS}
    restarted = {}
    code_sent = []

    def remote(command):
        operation, *args = command.split()
        if operation == "units":
            assert args == [STACK]
            return pr.units(stack)
        assert operation == "svc"
        action, *names = args
        if action == "restart":
            policy = json.loads(declaration.read_text(encoding="utf-8"))
            assert policy["hub_urls"] == [pr.LINK_URL]
            assert set(policy["offline"]) == {"phileas", "tantalus"}
            assert policy["spheres"] == [{"id": "shared", "mail_accounts": ["*"], "apps": ["phileas", "tantalus"]}]
            for name in names:
                unit = (unit_dir / f"{name}.service").read_text(encoding="utf-8")
                restarted[name] = dict(line.removeprefix("Environment=").split("=", 1)
                                       for line in unit.splitlines() if line.startswith("Environment="))
        else:
            assert action == "enable"

    monkeypatch.setattr(pl, "local_dirs", lambda: dirs)
    monkeypatch.setattr(pl, "_ssh_step", lambda *a, **kw: "8388608")
    monkeypatch.setattr(pl, "_put_text", lambda *a, **kw: None)
    monkeypatch.setattr(pl, "_remote", remote)
    monkeypatch.setattr(pl, "_upload_helper", lambda: None)

    def upload_mail_helper():
        assert declaration.is_file()
        code_sent.append("standalone")

    def deploy_code(app, *_args):
        if refresh:
            assert declaration.is_file()
        code_sent.append(app)

    monkeypatch.setattr(pl, "_upload_mail_helper", upload_mail_helper)
    monkeypatch.setattr(pl, "_ensure_node_runtime", lambda: None)
    monkeypatch.setattr(pl, "_deploy_code", deploy_code)
    monkeypatch.setattr(pl, "_ensure_runtime", lambda *a: None)
    monkeypatch.setattr(pl, "_pairs", lambda dirs: [])
    monkeypatch.setattr(pl, "_remote_patch", pr.patch)
    monkeypatch.setattr(pl, "_wait_remote_health", lambda: dict.fromkeys(APPS, True))
    monkeypatch.setattr(pl, "_tailnet_urls", lambda: {})

    if refresh:
        token = hermes_data / "products" / "mcp-token"
        token.unlink()
        with pytest.raises(ValueError, match="tantalus"):
            pl._refresh_sparks()
        assert restarted == {} and code_sent == []
        assert not declaration.exists()
        _write(token, "synthetic-products-token")

        pr.units(stack)
        reloaded.clear()
        for name in ("hermes", "faustus"):
            path = unit_dir / f"{name}.service"
            before = path.read_text(encoding="utf-8").splitlines(keepends=True)
            path.write_text("".join(line for line in before
                                    if not line.startswith(("Environment=HOARD_HUB_URL=", "Environment=HOARD_MAIL_REAL_"))),
                            encoding="utf-8")

        def interrupted():
            raise RuntimeError("simulated interruption")

        monkeypatch.setattr(pl, "_ensure_node_runtime", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            pl._refresh_sparks()
        assert restarted == {}
        assert declaration.exists()
        assert code_sent == ["standalone"]
        code_sent.clear()
        monkeypatch.setattr(pl, "_ensure_node_runtime", lambda: None)
        pl._refresh_sparks()
    else:
        monkeypatch.setattr(pl, "_stop_local", lambda: None)
        monkeypatch.setattr(pl, "_stop_local_whatsapp", lambda root: None)
        monkeypatch.setattr(pl, "_mail", lambda *a, **kw: None)
        pl._to_sparks()

    assert reloaded == [["systemctl", "--user", "daemon-reload"]]
    assert code_sent == (["standalone", *APPS] if refresh else [*APPS, "standalone"])
    assert set(restarted) == ({"faustus", "hermes"} if refresh else set(APPS))
    link = "http://127.0.0.1:8811"
    assert restarted["hermes"]["HOARD_HUB_URL"] == link
    assert restarted["hermes"]["HOARD_MAIL_REAL_HUB_URLS"] == link
    assert restarted["hermes"]["HOARD_MAIL_REAL_DATA_DIRS"] == f"{stack}/hermes/data"
    assert restarted["hermes"]["FAUSTUS_DIR"] == f"{stack}/faustus"
    assert restarted["faustus"]["HOARD_MAIL_REAL_HUB_URLS"] == link
    assert restarted["faustus"]["HOARD_MAIL_REAL_DATA_DIRS"] == f"{stack}/faustus/data"
    assert restarted["faustus"]["FAUSTUS_HUB_LINK"] == f"{stack}/hub-link.json"
    assert "HOARD_HUB_URL" not in restarted["faustus"]
    assert pl.state().get("deployed", {}) == {}


def test_empty_placement_does_not_invent_a_target(tmp_path):
    hub = _hub(tmp_path)
    try:
        pl = hub.placement
        status = pl.status()
        assert status["location"] == "pc" and status["node"] is None and status["nodes"] == []
        assert pl.state()["node"] is None and pl.spark_hosts() == set()
        assert pl.switch("pc") == {"ok": True, "already": True, "location": "pc"}
        assert pl.switch("sparks")["ok"] is False
        assert pl._thread is None and not pl.path.exists()
    finally:
        hub.close()


def test_installation_default_migrates_existing_state_once(tmp_path):
    from hoard_link.hub.placement import Placement

    data = tmp_path / "data"
    data.mkdir()
    state = data / "placement.json"
    existing = {"location": "pc", "job": {"running": False}, "deployed": {"faustus": "current"}}
    state.write_text(json.dumps(existing), encoding="utf-8")
    seed = data / "placement-default.json"
    seed.write_text(json.dumps(TEST_NODE), encoding="utf-8")
    hub = _hub(tmp_path)
    try:
        assert json.loads(state.read_text(encoding="utf-8")) == {**existing, "node": TEST_NODE}
        assert hub.placement.status()["nodes"] == [TEST_NODE["name"]]
        seed.write_text(json.dumps({"name": "Other node", "ssh": "other-node"}), encoding="utf-8")
        assert Placement(hub).state()["node"] == TEST_NODE
        assert json.loads(state.read_text(encoding="utf-8")) == {**existing, "node": TEST_NODE}
    finally:
        hub.close()


def test_missing_remote_node_keeps_local_start_blocked(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    try:
        pl = hub.placement
        pl._update(location="sparks", urls={"faustus": "https://studio-node.example.invalid/"})

        def unexpected_probe():
            pytest.fail("remote probe without a node")

        monkeypatch.setattr(pl, "remote_health", unexpected_probe)
        monkeypatch.setattr(pl, "_refresh_urls_later", unexpected_probe)
        status = pl.status()
        assert status["location"] == "sparks" and "remote" not in status
        assert pl.remote_app("faustus")["state"] == "down"
        assert pl.local_block("faustus")
        assert pl.switch("pc")["ok"] is False and pl._thread is None
    finally:
        hub.close()


def test_hub_refuses_local_starts_while_on_the_spark(tmp_path):
    hub = _hub(tmp_path)
    try:
        assert hub.placement.status()["location"] == "pc"
        assert hub.placement.local_block("faustus") is None
        hub.placement._refresh_remote_health = lambda: None                 # no SSH from the tests
        hub.placement._update(location="sparks", node=TEST_NODE, urls={"faustus": "https://studio-node.example.invalid"})
        assert TEST_NODE["name"] in hub.placement.local_block("hermes")
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


def test_switch_uses_configured_node_and_retains_live_source(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    pl = hub.placement
    target = {"name": "Other node", "ssh": "other-node", "host": "192.0.2.11"}
    monkeypatch.setattr(pl, "_configured_sparks", lambda: [target])
    seen = []
    monkeypatch.setattr(pl, "_run_switch", lambda to: seen.append((to, pl._node())))
    try:
        assert pl.status()["node"] is None and pl.status()["nodes"] == [target["name"]]
        assert not pl.switch("sparks")["ok"]
        assert not pl.switch("sparks", "unconfigured")["ok"]
        assert pl.switch("sparks", target["name"])["ok"]
        pl._thread.join(timeout=2)
        assert seen == [("sparks", target["ssh"])]
        assert pl.state()["node"] == target
        pl._update(node=TEST_NODE, deployed={"faustus": "old"}, location="sparks", job={"running": False})
        assert not pl.switch("sparks", target["name"])["ok"]
        assert pl._node() == TEST_NODE["ssh"]
        pl._update(location="pc")
        assert pl.switch("sparks", target["name"])["ok"]
        pl._thread.join(timeout=2)
        assert seen == [("sparks", target["ssh"])] * 2
        assert pl.state()["deployed"] == {}
        assert pl.status()["job"]["node"] == target["name"]
    finally:
        hub.close()


def test_unchanged_folders_skip_transfer_in_both_directions(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    pl = hub.placement
    local = tmp_path / "source"
    for name in ("one", "two", "three"):
        _write(local / name, name)
    monkeypatch.setattr(pl, "_remote", lambda _: pr.manifest(str(local), []))
    writes = []
    monkeypatch.setattr(pl, "_put_text", lambda path, text: writes.append(path))
    try:
        assert pl._push_dir(local, "~/source", [])["bytes"] == 0
        assert pl._pull_dir("source", local, "~/source", [], tmp_path / "trash")["bytes"] == 0
        assert writes == ["~/faustus-stack/excludes.json"] * 2
        assert not (tmp_path / "trash").exists()
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


# ---- the Hub shows them as running on the Spark, not as stopped here ---------------------------------------------------

def test_health_command_and_parse_cover_the_three_apps():
    from hoard_link.hub.placement import Placement, APPS
    cmd = Placement._health_command()
    for app in APPS:
        assert f"is-active {app}.service" in cmd
    parsed = Placement._parse_health("faustus active 200\nhermes active 000\nprometheus inactive\nnoise line\n")
    assert parsed["faustus"]["healthy"] is True
    assert parsed["hermes"] == {"service": "active", "http": "000", "healthy": False}
    assert parsed["prometheus"]["service"] == "inactive" and parsed["prometheus"]["healthy"] is False


def test_apps_on_the_spark_show_its_state_where_and_tailnet_address(tmp_path):
    hub = _hub(tmp_path)
    try:
        pl = hub.placement
        pl._refresh_remote_health = lambda: None
        assert pl.remote_app("hermes") is None                                   # on the PC: nothing changes
        pl._update(location="sparks", node=TEST_NODE, urls={"hermes": "https://studio-node.example.invalid:5221/products/",
                                                           "faustus": "https://studio-node.example.invalid"})
        assert pl.remote_app("hermes")["state"] == "starting"                    # first answer not in yet
        pl._remote_health = {"checked": time.time(), "ok": True, "error": "", "apps": {
            "faustus": {"service": "active", "http": "200", "healthy": True},
            "hermes": {"service": "active", "http": "200", "healthy": True},
            "prometheus": {"service": "inactive", "http": "000", "healthy": False}}}
        hermes = pl.remote_app("hermes")
        assert hermes["state"] == "running" and hermes["where"] == TEST_NODE["name"] and hermes["url"].endswith(":5221/products/")
        assert pl.remote_app("prometheus")["state"] == "down"
        assert pl.remote_app("comfyui") is None
        status = pl.status()
        assert status["remote"]["apps"]["faustus"]["healthy"] is True
        pl._remote_health = {"checked": time.time(), "ok": False, "error": "ssh: timeout", "apps": {}}
        assert pl.remote_app("faustus")["state"] == "down" and "timeout" in pl.remote_app("faustus")["detail"]
        pl._update(job={"running": True, "step": "x", "log": []})
        assert pl.remote_app("hermes") is None and "remote" not in pl.status()   # while moving, the move speaks
    finally:
        hub.close()


def test_opening_an_app_on_the_spark_opens_its_tailnet_address(tmp_path, monkeypatch):
    from hoard_link.hub import core
    hub = _hub(tmp_path)
    try:
        pl = hub.placement
        pl._refresh_remote_health = lambda: None
        url = "https://studio-node.example.invalid:5205/"
        pl._update(location="sparks", node=TEST_NODE, urls={"prometheus": url})
        pl._remote_health = {"checked": time.time(), "ok": True, "error": "", "apps": {
            "prometheus": {"service": "active", "http": "200", "healthy": True}}}
        from hoard_link.hub.registry import App
        with hub._lock:
            hub._apps["prometheus"] = App(id="prometheus", name="Prometheus's Hoard", purpose="", folder=str(tmp_path),
                                          url="http://127.0.0.1:5205/", health_path="/api/health", expect_service=None,
                                          launchable=True)
        st = hub.app_status(hub.get("prometheus"), {}, {})
        assert st["state"] == "running" and st["where"] == TEST_NODE["name"] and st["url"] == url
        assert st["launchable"] is False and st["stoppable"] is False and st["process"] is None
        seen = {}
        monkeypatch.setattr(core.desktop, "open_in_browser", lambda u: seen.setdefault("url", u) and {"ok": True})
        monkeypatch.setattr(core.desktop, "start_app", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")), raising=False)
        res = hub.open("prometheus", mode="browser")
        assert seen["url"] == url and res["where"] == TEST_NODE["name"]
    finally:
        hub.close()


def test_whatsapp_session_travels_with_faustus_but_not_its_process_record(tmp_path):
    from hoard_link.hub import placement as pl
    _write(tmp_path / "whatsapp" / "auth" / "creds.json", "{}")
    _write(tmp_path / "whatsapp" / "messages.jsonl", "{}")
    _write(tmp_path / "whatsapp" / "bridge.json", '{"pid": 1}')
    _write(tmp_path / "logs" / "a.log", "x")
    sub, excludes = pl.DATA["faustus"]
    found = pr.manifest(str(tmp_path), pl.COMMON_EXCLUDES + excludes)
    assert sorted(found) == ["whatsapp/auth/creds.json", "whatsapp/messages.jsonl"]


def test_bridge_pid_comes_from_faustus_record(tmp_path):
    from hoard_link.hub import placement as pl
    assert pl.Placement._bridge_pid(tmp_path) is None
    _write(tmp_path / "data" / "whatsapp" / "bridge.json", '{"pid": 4321, "port": 8790}')
    assert pl.Placement._bridge_pid(tmp_path) == 4321


def test_saving_the_state_waits_for_a_reader_that_holds_the_file(tmp_path, monkeypatch):
    from hoard_link.hub import placement as pl
    hub = _hub(tmp_path)
    try:
        real, calls = os.replace, []

        def busy_twice(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(5, "Access is denied")
            return real(src, dst)

        monkeypatch.setattr(pl.os, "replace", busy_twice)
        monkeypatch.setattr(pl.time, "sleep", lambda _s: None)
        hub.placement._update(urls={"faustus": "https://example.invalid"})
        assert len(calls) == 3 and hub.placement.state()["urls"] == {"faustus": "https://example.invalid"}
    finally:
        monkeypatch.undo()
        hub.close()
