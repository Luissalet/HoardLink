"""Program discovery and reversible manual entries."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from types import SimpleNamespace

from hoard_link.hub import local_programs as programs
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub

from ._hub_fakes import ui_headers
from .test_hub_and_server import _http, served  # noqa: F401


def test_inventory_reads_portable_installs_and_environment_metadata(tmp_path, monkeypatch):
    tools = tmp_path / "Development" / "Tools"
    early = tools / "Godot-4.9" / "Godot4.9_console.exe"
    latest = tools / "Godot-4.12" / "Godot4.12_console.exe"
    comfy = tools / "ComfyUI" / "main.py"
    app = tmp_path / "Development" / "IA" / "Hoards" / "Family" / "Render App"
    venv = app / ".render"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    comfy_python = comfy.parent / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    for path in (early, latest, comfy, python, comfy_python):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (comfy.parent / "comfy").mkdir()
    (comfy.parent / "comfyui_version.py").write_text('__version__ = "12.34"\n', encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("version = 3.12.3\n", encoding="utf-8")
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "hoard-home"))
    monkeypatch.delenv("COMFYUI_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    hub = Hub(HubConfig(roots=[str(app.parent)], data_dir=str(tmp_path / "data"), jobs_enabled=False))
    hub._apps = {"render": SimpleNamespace(id="render", name="Render App", folder=str(app), launch=None)}
    monkeypatch.setattr(programs.shutil, "which", lambda _: None)
    monkeypatch.setattr(programs, "_version", lambda path: "fixture version" if path else None)
    monkeypatch.setattr(programs.bins, "find", lambda *_, **__: SimpleNamespace(path=None, version=""))
    by_name = {row["name"]: row for row in programs.discover(hub)}
    assert by_name["Godot"]["path"] == str(latest)
    assert "--headless" in by_name["Godot"]["headless"]
    assert by_name["ComfyUI"]["path"] == str(comfy)
    assert by_name["ComfyUI"]["version"] == "12.34"
    assert by_name["ComfyUI"]["interpreter"] == str(comfy_python)
    service = hub.launcher.comfy_service(port=64111)
    assert service.problem is None
    assert service.cwd == str(comfy.parent)
    assert service.argv[0] == str(comfy_python)
    assert by_name["Python · Render App · .render"]["path"] == str(python)
    assert by_name["Python · Render App · .render"]["version"] == "3.12.3"


def test_manual_registration_survives_reload_and_concurrent_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(programs, "discover", lambda *_, **__: [])
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)))
    first, second = programs.LocalPrograms(hub), programs.LocalPrograms(hub)
    args = {"name": "Fixture", "path": str(tmp_path / "fixture"), "headless": "fixture --headless"}
    with ThreadPoolExecutor(2) as pool:
        changes = list(pool.map(lambda facet: facet.register(args), [first, second]))
    assert sum(change["changed"] for change in changes) == 1
    assert changes[0]["undo"] == changes[1]["undo"]
    assert first.list_programs({})["programs"] == [changes[0]["program"]]

    undo = changes[0]["undo"]
    assert second.handlers()[undo["tool"]](undo["arguments"])["changed"]
    assert first.list_programs({})["programs"] == []
    next_add = second.register(args)
    assert next_add["program"]["id"] != changes[0]["program"]["id"]
    assert not first.undo(undo["arguments"])["changed"]
    assert second.list_programs({})["programs"] == [next_add["program"]]


def test_http_registry_uses_the_same_manual_entries_as_named_tools(served, tmp_path, monkeypatch):
    monkeypatch.setattr(programs, "discover", lambda *_, **__: [])
    hub, url = served
    payload = {"name": "Local Test", "path": str(Path(tmp_path) / "tool"), "headless": "tool --batch"}
    assert _http(url + "/api/programs/register", payload)[0] == 401
    assert _http(url + "/api/programs", None)[1] == {"ok": True, "programs": []}
    page_headers = ui_headers(hub)
    status, added = _http(url + "/api/programs/register", payload, page_headers)
    assert status == 200 and added["changed"]
    agent_headers = {"Authorization": f"Bearer {hub.token}"}
    status, listed = _http(url + "/api/agent/tools/hub_programs", {}, agent_headers)
    assert status == 200 and listed["result"]["programs"] == [added["program"]]
    assert _http(url + "/api/programs/undo", added["undo"]["arguments"], page_headers)[1]["changed"]
    assert not _http(url + "/api/programs", None)[1]["programs"]


def test_vulcan_blender_selection_persists_and_has_conditional_undo(served, tmp_path, monkeypatch):
    monkeypatch.setattr(programs, "discover", lambda *_, **__: [])
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    hub, url = served
    headers = {"Authorization": f"Bearer {hub.token}"}

    def set_renderer(arguments):
        return _http(url + "/api/agent/tools/hub_vulcan_blender_set", arguments, headers)[1]["result"]

    executables = {}
    for version in ("4.3", "4.5"):
        path = tmp_path / "Blender Foundation" / f"Blender {version}" / "blender.exe"
        path.parent.mkdir(parents=True)
        path.touch()
        executables[version] = str(path)
    assert not set_renderer({"version": "../4.3"})["ok"]
    assert not set_renderer({"version": "3.6"})["ok"]
    first = set_renderer({"version": "4.3"})
    assert first["ok"] and first["path"] == executables["4.3"]
    restored_facet = programs.LocalPrograms(hub)
    assert restored_facet.vulcan_blender_env() == {
        "VULCAN_BLENDER": executables["4.3"], "VULCAN_BLENDER_VERSION": "4.3",
    }
    row = _http(url + "/api/programs", None)[1]["programs"][0]
    assert (row["id"], row["source"], row["version"], row["path"]) == (
        "vulcan-blender", "configured", "4.3", executables["4.3"],
    )
    second = set_renderer({"version": "4.5"})
    assert not set_renderer(first["undo"]["arguments"])["ok"]
    assert set_renderer(second["undo"]["arguments"])["version"] == "4.3"
    assert set_renderer(first["undo"]["arguments"])["version"] is None
    assert restored_facet.vulcan_blender_env() == {}
    assert not _http(url + "/api/programs", None)[1]["programs"]
