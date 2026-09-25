"""Per-machine launch overrides (hub.json) and executable candidate lists in launch hints."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from hoard_link.hub.config import HubConfig
from hoard_link.hub.registry import read_manifest, scan

from .conftest import write_manifest


def _exe_hint(executable):
    return {"kind": "process", "executable": executable, "argv": [], "cwd": "{X_DIR}"}


def test_override_replaces_the_manifest_launch(tmp_path: Path):
    folder = write_manifest(tmp_path / "Writer", "writer", 5301,
                            launch=_exe_hint("{WRITER_DIR}/release/app.exe"))
    (folder / "scripts").mkdir()
    overrides = {"writer": {"executable": sys.executable, "argv": ["{APP_DIR}/scripts/dev.mjs"],
                            "cwd": "{APP_DIR}", "env": {"PORT": "{APP_URL}"}}}
    [app] = scan([str(tmp_path)], launch_overrides=overrides)
    assert app.launch_source == "override"
    assert app.launchable, app.launch_reason
    assert app.launch.executable == sys.executable
    assert app.launch.argv == [str(folder) + "/scripts/dev.mjs"]
    assert app.launch.cwd == str(folder)
    assert app.launch.env == {"PORT": "http://127.0.0.1:5301"}
    assert app.launch.readiness_url == app.health_url()
    assert app.kind == "app"
    assert app.to_dict()["launch_source"] == "override"


def test_override_with_a_missing_executable_says_why(tmp_path: Path):
    write_manifest(tmp_path / "Writer", "writer", 5302)
    [app] = scan([str(tmp_path)], launch_overrides={"writer": {"executable": str(tmp_path / "nope.exe")}})
    assert not app.launchable
    assert "nope.exe" in app.launch_reason


def test_apps_without_an_override_keep_the_manifest(tmp_path: Path):
    write_manifest(tmp_path / "Other", "other", 5303)
    [app] = scan([str(tmp_path)], launch_overrides={"writer": {"executable": sys.executable}})
    assert app.launch_source == "manifest"


def test_executable_candidates_first_existing_wins(tmp_path: Path, monkeypatch):
    installed = tmp_path / "Programs" / "app.exe"
    installed.parent.mkdir()
    installed.write_bytes(b"")
    monkeypatch.setenv("HOARD_TEST_PROGRAMS", str(tmp_path / "Programs"))
    folder = write_manifest(tmp_path / "apps" / "Writer", "writer", 5304,
                            launch=_exe_hint(["%HOARD_TEST_PROGRAMS%/app.exe", "{WRITER_DIR}/release/app.exe"]))
    app = read_manifest(folder / "faustus-plugin.json")
    assert app.launchable, app.launch_reason
    assert Path(app.launch.executable) == installed
    installed.unlink()
    app = read_manifest(folder / "faustus-plugin.json")
    assert not app.launchable  # neither exists: the first candidate names the problem
    assert "HOARD_TEST_PROGRAMS" not in app.launch_reason and "Programs" in app.launch_reason


def test_config_reads_launch_overrides(tmp_path: Path):
    (tmp_path / "hub.json").write_text(json.dumps({"launch_overrides": {"writer": {"executable": "node"}}}))
    cfg = HubConfig.load(tmp_path / "hub.json", env={})
    assert cfg.launch_overrides == {"writer": {"executable": "node"}}


def test_rescan_picks_up_an_edited_hub_json(hub):
    """Editing launch_overrides in hub.json and rescanning is enough: no hub restart."""
    app_id = "launch"
    assert hub.get(app_id).launch_source == "manifest"
    path = Path(hub.config.data_dir) / "hub.json"
    path.write_text(json.dumps({"launch_overrides": {app_id: {"executable": sys.executable, "argv": ["-V"]}}}))
    hub.rescan()
    app = hub.get(app_id)
    assert app.launch_source == "override" and app.launch.argv == ["-V"]
