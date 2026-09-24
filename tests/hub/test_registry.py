from __future__ import annotations

import os
import json
import sys
from pathlib import Path

from hoard_link.hub.registry import read_manifest, resolve_placeholders, scan

from .conftest import write_manifest


def test_placeholder_rules():
    missing: set[str] = set()
    out = resolve_placeholders(
        "{BABEL_DIR}/.venv/{PYTHON}/{APP_URL}/{FAUSTUS_PYTHON}/{NOPE}",
        folder="/apps/babel", defaults={"PYTHON": "py-{BABEL_DIR}", "APP_URL": "http://x"},
        extra={"FAUSTUS_PYTHON": "/py"}, missing=missing,
    )
    assert out == "/apps/babel/.venv/py-/apps/babel/http://x//py/{NOPE}"
    assert missing == {"NOPE"}


def test_faustus_dir_is_not_the_app_folder():
    missing: set[str] = set()
    out = resolve_placeholders("{FAUSTUS_DIR}", folder="/apps/x", defaults={}, extra={}, missing=missing)
    assert out == "{FAUSTUS_DIR}" and missing == {"FAUSTUS_DIR"}
    out = resolve_placeholders("{FAUSTUS_DIR}", folder="/apps/x", defaults={}, extra={"FAUSTUS_DIR": "/f"}, missing=set())
    assert out == "/f"


def test_scan_reads_family_and_skips_junk(family):
    apps = scan([str(family["root"])])
    ids = [a.id for a in apps]
    assert ids == ["dead", "fake", "launch"]  # sorted by name; broken + not-an-app skipped
    fake = next(a for a in apps if a.id == "fake")
    assert fake.port == family["fake_port"]
    assert fake.health_url().endswith("/api/health")
    assert fake.icon_path and fake.icon_path.endswith("app-icon.png")
    assert not fake.launchable and "no process launch hint" in fake.launch_reason


def test_unresolved_and_missing_executable(family):
    apps = {a.id: a for a in scan([str(family["root"])])}
    dead = apps["dead"]
    assert not dead.launchable
    assert "MISSING_EXE" in dead.launch_reason
    launch = apps["launch"]
    assert launch.launchable, launch.launch_reason
    assert launch.launch.executable == sys.executable
    assert launch.launch.cwd == launch.folder


def test_duplicate_ids_keep_first(tmp_path: Path):
    write_manifest(tmp_path / "A", "same", 5001)
    write_manifest(tmp_path / "B", "same", 5002)
    apps = scan([str(tmp_path)])
    assert len(apps) == 1 and apps[0].port == 5001


def test_root_can_be_an_app_folder_itself(tmp_path: Path):
    write_manifest(tmp_path / "Solo", "solo", 5003)
    apps = scan([str(tmp_path / "Solo")])
    assert [a.id for a in apps] == ["solo"]


def test_shared_icon_folder(tmp_path: Path):
    folder = write_manifest(tmp_path / "apps" / "Ledger's Hoard", "ledger", 5180, name="Ledger's Hoard")
    (folder / "app-icon.png").unlink()
    icons = tmp_path / "Icons"
    icons.mkdir()
    (icons / "Ledgers hoard.png").write_bytes(b"x")
    app = read_manifest(folder / "faustus-plugin.json", icon_dirs=[str(icons)])
    assert app is not None and app.icon_path == str(icons / "Ledgers hoard.png")


def test_newer_schema_and_missing_app_block(tmp_path: Path):
    p = tmp_path / "x" / "faustus-plugin.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"schema": 2, "id": "x", "app": {"url_default": "http://127.0.0.1:1"}}))
    assert read_manifest(p) is None
    p.write_text(json.dumps({"schema": 1, "id": "x"}))
    assert read_manifest(p) is None


def test_exe_without_argv_is_a_window_app(tmp_path: Path):
    exe = tmp_path / "Thing.exe"
    exe.write_bytes(b"MZ")
    folder = write_manifest(tmp_path / "Thing", "thing", 5004, launch={
        "kind": "process", "executable": str(exe), "argv": [], "cwd": "{THING_DIR}",
    })
    app = read_manifest(folder / "faustus-plugin.json")
    assert app is not None and app.kind == "window-app" and app.launchable


def test_x_family_block_marks_an_app_without_the_agent_contract(tmp_path: Path):
    from hoard_link.hub import audit as auditmod
    from tests.hub.conftest import write_manifest

    plain = write_manifest(tmp_path / "plain", "plain", 1)
    legacy = write_manifest(tmp_path / "legacy", "legacy", 2, extra={"x-family": {"agent_contract": False, "stack": "python", "note": "REST only"}})
    a = read_manifest(plain / "faustus-plugin.json")
    b = read_manifest(legacy / "faustus-plugin.json")
    assert a.agent_contract is True and a.family == {} and a.to_dict()["agent_contract"] is True
    assert b.agent_contract is False and b.family["note"] == "REST only" and b.to_dict()["family"]["stack"] == "python"
    report = auditmod.audit([a, b], probe=False)
    s = report["summary"]
    assert s["no_contract"] == ["legacy"]
    assert "plain" in s["no_token"] and "legacy" not in s["no_token"]
    assert "plain" in s["not_vendoring"] and "legacy" not in s["not_vendoring"]
    line = next(l for l in report["apps"] if l["id"] == "legacy")
    assert line["agent_contract"] is False and line["stack"] == "python"


def test_token_file_may_live_under_an_environment_folder(tmp_path: Path, monkeypatch):
    from tests.hub.conftest import write_manifest

    monkeypatch.setenv("HOARD_TEST_PROFILE", str(tmp_path / "profile"))
    folder = write_manifest(tmp_path / "desk", "desk", 3, extra={"defaults": {"APP_URL": "http://127.0.0.1:3", "TOKEN_FILE": "%HOARD_TEST_PROFILE%/desk/token"}})
    app = read_manifest(folder / "faustus-plugin.json")
    assert app.token_file == os.path.normpath(str(tmp_path / "profile" / "desk" / "token"))
    assert app.data_dir == os.path.normpath(str(tmp_path / "profile" / "desk"))
    unknown = write_manifest(tmp_path / "unk", "unk", 4, extra={"defaults": {"APP_URL": "http://127.0.0.1:4", "TOKEN_FILE": "%NO_SUCH_VAR_HOARD%/token"}})
    assert read_manifest(unknown / "faustus-plugin.json").token_file == os.path.normpath(str(unknown / "data" / "mcp-token"))
