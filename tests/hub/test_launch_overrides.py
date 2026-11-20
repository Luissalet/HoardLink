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
    folder = write_manifest(tmp_path / "Example", "example", 5301,
                            launch=_exe_hint("{EXAMPLE_DIR}/release/app.exe"))
    (folder / "scripts").mkdir()
    overrides = {"example": {"executable": sys.executable, "argv": ["{APP_DIR}/scripts/dev.mjs"],
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
    write_manifest(tmp_path / "Example", "example", 5302)
    [app] = scan([str(tmp_path)], launch_overrides={"example": {"executable": str(tmp_path / "nope.exe")}})
    assert not app.launchable
    assert "nope.exe" in app.launch_reason


def test_apps_without_an_override_keep_the_manifest(tmp_path: Path):
    write_manifest(tmp_path / "Other", "other", 5303)
    [app] = scan([str(tmp_path)], launch_overrides={"writer": {"executable": sys.executable}})
    assert app.launch_source == "manifest"


def test_writer_prefers_source_to_a_duplicate_and_legacy_override(tmp_path, monkeypatch):
    from hoard_link.hub import registry

    installed = write_manifest(tmp_path / "Installed", "writer", 5301, launch=_exe_hint("installed.exe"))
    source = write_manifest(tmp_path / "Source", "writer", 5302, launch={
        "executable": "node", "argv": ["scripts/dev-desktop.mjs", "--hub"], "opens_window": True,
        "port_argument": "--bridge-port", "readiness": {"url": "{APP_URL}/api/health"},
    })
    (source / "scripts").mkdir()
    (source / "scripts" / "dev-desktop.mjs").touch()
    monkeypatch.setattr(registry.shutil, "which", lambda command: command)
    monkeypatch.setenv("WH_USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("WH_AIBRIDGE_PORT", "5303")
    overrides = {"writer": {"executable": sys.executable}}
    [writer] = scan([str(installed), str(source)], launch_overrides=overrides)
    assert writer.launchable and writer.launch_source == "manifest"
    assert writer.folder == str(source) and writer.launch.argv == ["scripts/dev-desktop.mjs", "--hub"]
    assert writer.launch.port_argument == "--bridge-port"
    assert writer.launch.readiness_url == writer.health_url() == "http://127.0.0.1:5303/api/health"
    assert Path(writer.token_file) == tmp_path / "profile" / "aibridge" / "token"
    assert Path(writer.data_dir) == tmp_path / "profile" / "backups"
    [installed_only] = scan([str(installed)], launch_overrides=overrides)
    assert not installed_only.launchable and installed_only.launch is None


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

    # Taking the override out again restores the manifest's launch.
    path.write_text(json.dumps({}))
    hub.rescan()
    assert hub.get(app_id).launch_source == "manifest"
