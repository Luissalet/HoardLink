from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
from pathlib import Path

from hoard_link.hub import tools
from hoard_link.hub import portable_profiles
from hoard_link.hub.portable_profiles import export_profile, import_profile, preview_import
from hoard_link.hub.server import make_server
from .test_hub_and_server import _http


def _sample_profile(hub, tmp_path):
    hub.config.profiles = {
        "Creative": {
            "apps": ["launch"],
            "desktop": ["launch"],
            "commands": [{
                "name": "Renderer",
                "cmd": [sys.executable, "render.py"],
                "cwd": str(tmp_path),
                "health": "http://127.0.0.1:8888/health",
                "env": {"CUDA_VISIBLE_DEVICES": "2", "API_TOKEN": "never-export-this-value",
                        "AUTH_ENABLED": False, "COOKIE_SECURE": False},
            }],
        }
    }


def test_profile_package_round_trip_redacts_secrets_and_keeps_local_fields_separate(hub, tmp_path):
    _sample_profile(hub, tmp_path)
    before = copy.deepcopy(hub.config.profiles)
    package = export_profile(hub, "Creative")
    encoded = json.dumps(package)
    assert package["format"] == "hoardlink.launch-profile" and package["version"] == 1
    assert package["profile"]["apps"] == ["launch"]
    assert package["profile"]["desktop"] == ["launch"]
    assert package["dependencies"]["apps"] == [{
        "id": "launch", "name": "Launch's Hoard", "available_on_source": True}]
    assert package["profile"]["commands"][0]["cwd"] == str(tmp_path)
    assert package["profile"]["commands"][0]["health"].startswith("http://127.0.0.1")
    assert package["profile"]["commands"][0]["env"] == {
        "CUDA_VISIBLE_DEVICES": "2", "AUTH_ENABLED": "False", "COOKIE_SECURE": "False"}
    assert package["portability"]["redacted_environment"] == [{"command": "Renderer", "keys": ["API_TOKEN"]}]
    assert "never-export-this-value" not in encoded and "pid" not in encoded.lower()
    assert hub.config.profiles == before  # export never edits the source profile

    needs_review = preview_import(hub, package, name="Relocated")
    assert needs_review["can_import"]
    assert needs_review["machine_specific_fields"] == ["command 'Renderer'.cwd", "command 'Renderer'.health"]
    assert any("machine-specific" in item for item in needs_review["warnings"])
    assert needs_review["profile"]["commands"][0]["cmd"] == [sys.executable, "render.py"]
    overridden = preview_import(hub, package, name="With local command", command_overrides={
        "Renderer": {"cmd": [sys.executable, "render.py", "--local"]}})
    assert overridden["can_import"]
    assert overridden["profile"]["commands"][0]["cmd"] == [sys.executable, "render.py", "--local"]
    acknowledged = preview_import(hub, package, name="Relocated")
    assert acknowledged["can_import"]

    unresolved = copy.deepcopy(package)
    unresolved["profile"]["apps"] = ["portable-launch"]
    unresolved["profile"]["desktop"] = ["portable-launch"]
    preview = preview_import(hub, unresolved, name="Imported", command_overrides={"Renderer": {"cwd": None, "health": None}})
    assert not preview["can_import"]
    assert any("unresolved app dependency" in error for error in preview["errors"])
    assert preview["dependencies"][0]["source_id"] == "portable-launch"
    assert preview["machine_specific_fields"] == []  # command paths are independent of app ID mapping

    options = {"name": "Imported", "app_map": {"portable-launch": "launch"},
               "command_overrides": {"Renderer": {
                   "cwd": None, "health": None, "env": {"API_TOKEN": "local-secret"}}}}
    preview = preview_import(hub, unresolved, **options)
    assert preview["can_import"] and preview["missing_environment"] == []
    assert preview["profile"]["apps"] == ["launch"]
    assert preview["started"] is False
    result = import_profile(hub, unresolved, **options)
    assert result["ok"] and result["imported"] and result["started"] is False
    imported = hub.config.profiles["Imported"]
    assert imported["apps"] == ["launch"] and imported["desktop"] == ["launch"]
    assert imported["commands"][0]["env"] == {
        "CUDA_VISIBLE_DEVICES": "2", "AUTH_ENABLED": "False", "COOKIE_SECURE": "False",
        "API_TOKEN": "local-secret"}
    assert imported["commands"][0]["cwd"] is None and imported["commands"][0]["health"] is None
    from hoard_link.hub.config import HubConfig
    saved = HubConfig.load(env={"HOARD_HUB_DATA_DIR": hub.config.data_dir})
    assert saved.profiles["Imported"] == hub.config.profiles["Imported"]


def test_import_collision_and_invalid_package_are_reported_without_writes(hub, tmp_path, monkeypatch):
    _sample_profile(hub, tmp_path)
    package = export_profile(hub, "Creative")
    package_before = copy.deepcopy(hub.config.profiles)
    collision = preview_import(hub, package)
    assert not collision["can_import"] and collision["conflict"]["exists"]
    assert any("already exists" in error for error in collision["errors"])
    assert hub.config.profiles == package_before

    no_paths = copy.deepcopy(package)
    no_paths["profile"]["commands"][0]["cwd"] = None
    no_paths["profile"]["commands"][0]["health"] = None
    plan = preview_import(hub, no_paths, name="Creative", replace=True)
    assert plan["can_import"]  # stopped profile may be replaced explicitly

    hub.config.profiles["Slug Collision"] = {"commands": [{"name": "Renderer", "cmd": "python render.py"}]}
    slug_collision = preview_import(hub, no_paths, name="Slug-Collision")
    assert not slug_collision["can_import"]
    assert any("command IDs collide" in error for error in slug_collision["errors"])
    del hub.config.profiles["Slug Collision"]

    monkeypatch.setattr(hub, "profile_status", lambda _name: {"members": [{"state": "running"}]})
    running = preview_import(hub, no_paths, name="Creative", replace=True)
    assert not running["can_import"] and any("running profile" in error for error in running["errors"])

    malformed = preview_import(hub, {"format": "wrong", "version": 1})
    assert not malformed["can_import"]
    hub.config.profiles["Inline secret"] = {"commands": [{"name": "private", "cmd": "server --api-key embedded-secret"}]}
    try:
        export_profile(hub, "Inline secret")
        assert False, "export should refuse a recognizable inline credential"
    except ValueError as exc:
        assert "private" in str(exc) and "embedded-secret" not in str(exc)
    del hub.config.profiles["Inline secret"]
    assert hub.config.profiles["Creative"] == package_before["Creative"]


def test_http_and_mcp_profile_transfer_never_start_members(hub, tmp_path, monkeypatch):
    _sample_profile(hub, tmp_path)
    calls = []
    monkeypatch.setattr(hub, "profile_start", lambda *a, **kw: calls.append(("profile", a, kw)))
    monkeypatch.setattr(hub, "start", lambda *a, **kw: calls.append(("app", a, kw)))
    monkeypatch.setattr(hub.commands, "start", lambda *a, **kw: calls.append(("command", a, kw)))
    server = make_server(hub, port=hub.config.port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = hub.config.url
        status, package = _http(url + "/api/profiles/export", {"name": "Creative"})
        assert status == 200 and package["portability"]["runtime_state_included"] is False
        document = copy.deepcopy(package)
        document["profile"]["name"] = "Imported"
        document["profile"]["commands"][0]["cwd"] = None
        document["profile"]["commands"][0]["health"] = None
        # Credentials omitted by export can be supplied as local command overrides.
        options = {"document": document, "command_overrides": {"Renderer": {
            "env": {"API_TOKEN": "local-secret"}}}}
        status, preview = _http(url + "/api/profiles/import/preview", options)
        assert status == 200 and preview["can_import"] and preview["started"] is False
        status, imported = _http(url + "/api/profiles/import", options)
        assert status == 200 and imported["imported"] and imported["started"] is False
        assert hub.config.profiles["Imported"]["commands"][0]["env"]["API_TOKEN"] == "local-secret"

        names = {item["name"] for item in tools.catalogue()}
        assert {"hub_profile_export", "hub_profile_import_preview", "hub_profile_import"} <= names
        mcp = _http(url + "/api/agent/call", {"tool": "hub_profile_export", "arguments": {"name": "Imported"}},
                    headers={"Authorization": f"Bearer {hub.token}"})
        assert mcp[0] == 200 and mcp[1]["result"]["format"] == "hoardlink.launch-profile"
        mcp_import = _http(url + "/api/agent/call", {"tool": "hub_profile_import_preview", "arguments": {
            "document": mcp[1]["result"], "name": "Imported Copy",
            "command_overrides": {"Renderer": {"cwd": None, "health": None}}}},
            headers={"Authorization": f"Bearer {hub.token}"})
        assert mcp_import[0] == 200 and mcp_import[1]["result"]["can_import"]
        malformed_replace = _http(url + "/api/agent/call", {"tool": "hub_profile_import", "arguments": {
            "document": mcp[1]["result"], "name": "Must Not Import", "replace": "false"}},
            headers={"Authorization": f"Bearer {hub.token}"})
        assert malformed_replace[0] == 400
        assert malformed_replace[1]["result"]["ok"] is False
        assert "replace must be a boolean" in malformed_replace[1]["result"]["errors"][0]
        assert "Must Not Import" not in hub.config.profiles
        assert calls == []
    finally:
        server.shutdown()


def test_native_profile_names_and_machine_paths_import_without_starting(hub, tmp_path, monkeypatch):
    profile_name = "🎨 " + ("Native profile " * 8)
    hub.config.profiles = {profile_name: {"commands": [{
        "name": "Build", "cmd": [sys.executable, "build.py"], "cwd": str(tmp_path),
        "health": "http://127.0.0.1:9000/health", "env": {"AUTH_ENABLED": False}}]}}
    package = export_profile(hub, profile_name)
    assert package["profile"]["name"] == profile_name
    assert package["profile"]["commands"][0]["env"] == {"AUTH_ENABLED": "False"}
    assert package["portability"]["redacted_environment"] == []

    calls = []
    monkeypatch.setattr(hub, "profile_start", lambda *a, **kw: calls.append(("profile", a, kw)))
    monkeypatch.setattr(hub, "start", lambda *a, **kw: calls.append(("app", a, kw)))
    monkeypatch.setattr(hub.commands, "start", lambda *a, **kw: calls.append(("command", a, kw)))
    imported_name = "🖌️ " + ("Portable name " * 7)
    preview = preview_import(hub, package, name=imported_name)
    assert preview["can_import"]
    assert preview["machine_specific_fields"] == ["command 'Build'.cwd", "command 'Build'.health"]
    assert any("retained" in warning for warning in preview["warnings"])
    result = import_profile(hub, package, name=imported_name)
    assert result["ok"] and result["started"] is False
    assert hub.config.profiles[imported_name]["commands"][0]["env"]["AUTH_ENABLED"] == "False"
    assert calls == []


def test_failed_profile_save_restores_config_and_keeps_existing_file(hub, tmp_path, monkeypatch):
    _sample_profile(hub, tmp_path)
    package = export_profile(hub, "Creative")
    package["profile"]["name"] = "Imported"
    config_path = Path(hub.config.data_dir) / "hub.json"
    hub.config.save()
    original_bytes = config_path.read_bytes()
    original_profiles = copy.deepcopy(hub.config.profiles)

    def fail_save():
        raise OSError("simulated disk failure")

    monkeypatch.setattr(hub.config, "save", fail_save)
    result = import_profile(hub, package)
    assert result["ok"] is False
    assert "simulated disk failure" in result["error"]
    assert hub.config.profiles == original_profiles
    assert config_path.read_bytes() == original_bytes


def test_concurrent_imports_serialize_command_id_collision_validation(hub, tmp_path, monkeypatch):
    _sample_profile(hub, tmp_path)
    package = export_profile(hub, "Creative")
    save_entered = threading.Event()
    allow_save = threading.Event()
    second_plan_entered = threading.Event()
    real_save = hub.config.save
    real_plan = portable_profiles._plan

    def blocked_save():
        save_entered.set()
        assert allow_save.wait(5), "test did not release the first profile save"
        return real_save()

    def observed_plan(target_hub, document, **options):
        if options.get("name") == "Render-One":
            second_plan_entered.set()
        return real_plan(target_hub, document, **options)

    monkeypatch.setattr(hub.config, "save", blocked_save)
    monkeypatch.setattr(portable_profiles, "_plan", observed_plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(import_profile, hub, package, name="Render One")
        assert save_entered.wait(5), "first import never reached its save"
        second = pool.submit(import_profile, hub, package, name="Render-One")
        assert not second_plan_entered.wait(.2), "second validation ran before the first locked save completed"
        allow_save.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result["ok"] is True
    assert second_result["ok"] is False
    assert any("command IDs collide" in error for error in second_result["errors"])
    assert "Render One" in hub.config.profiles
    assert "Render-One" not in hub.config.profiles
