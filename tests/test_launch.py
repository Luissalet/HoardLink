"""hoard_link.launch: finding, starting and stopping shared backends."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hoard_link import launch
from hoard_link.launch import Launcher, comfy_port_from_url


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "hoard-home"
    monkeypatch.setenv("HOARD_HOME", str(h))
    monkeypatch.delenv("COMFYUI_DIR", raising=False)
    return h


def _fake_comfy(root: Path, flags=("--disable-auto-launch", "--preview-method", "--cuda-device", "--output-directory",
                                    "--temp-directory", "--user-directory", "--database-url")) -> Path:
    folder = root / "ComfyUI"
    (folder / "comfy").mkdir(parents=True)
    (folder / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (folder / "comfy" / "cli_args.py").write_text("\n".join(f'parser.add_argument("{f}")' for f in flags), encoding="utf-8")
    py = folder / "venv" / ("Scripts" if launch.IS_WIN else "bin") / ("python.exe" if launch.IS_WIN else "python")
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    return folder


def test_comfy_port_from_url():
    assert comfy_port_from_url(None) == 8188
    assert comfy_port_from_url("http://127.0.0.1:8190") == 8190
    assert comfy_port_from_url("http://localhost") == 80
    assert comfy_port_from_url("http://192.168.1.4:8188") is None


def test_hoard_home_env(home):
    assert launch.hoard_home() == home
    assert Launcher().config_path == home / "backends.json"


def test_comfy_found_from_config_with_isolated_extra_port(home, tmp_path):
    folder = _fake_comfy(tmp_path)
    ln = Launcher(app="test")
    ln.set_config({"comfyui": {"dir": str(folder), "args": ["--lowvram"]}})
    main = ln.comfy_service(8188, gpu=2)
    assert main.problem is None and main.install == str(folder)
    assert main.argv[1:6] == ["main.py", "--listen", "127.0.0.1", "--port", "8188"]
    assert "--cuda-device" in main.argv and main.argv[main.argv.index("--cuda-device") + 1] == "2"
    assert "--output-directory" not in main.argv  # the default instance keeps ComfyUI's own folders
    assert main.argv[-1] == "--lowvram"
    assert main.env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    extra = ln.comfy_service(8191)
    assert "--cuda-device" not in extra.argv
    out = extra.argv[extra.argv.index("--output-directory") + 1]
    assert Path(out) == home / "backends" / "comfyui-8191" / "output"
    assert any(a.startswith("sqlite:///") and a.endswith("comfyui-8191/comfyui.db") for a in extra.argv)


def test_comfy_flags_only_when_the_install_knows_them(home, tmp_path):
    folder = _fake_comfy(tmp_path, flags=("--cuda-device",))
    ln = Launcher()
    ln.set_config({"comfyui": {"dir": str(folder)}})
    svc = ln.comfy_service(8189)
    assert "--database-url" not in svc.argv and "--preview-method" not in svc.argv


def test_comfy_offloads_over_ram_not_disk(home, tmp_path):
    # with fast disk, a model bigger than the free VRAM is re-read from disk
    # on every step; the launcher turns it off unless the config asks for it
    folder = _fake_comfy(tmp_path, flags=("--cuda-device", "--disable-fast-disk", "--fast-disk"))
    ln = Launcher()
    ln.set_config({"comfyui": {"dir": str(folder)}})
    assert ln.comfy_service(8189).argv.count("--disable-fast-disk") == 1
    ln.set_config({"comfyui": {"dir": str(folder), "args": ["--fast-disk"]}})
    assert "--disable-fast-disk" not in ln.comfy_service(8189).argv
    ln.set_config({"comfyui": {"dir": str(folder), "args": ["--disable-fast-disk"]}})
    assert ln.comfy_service(8189).argv.count("--disable-fast-disk") == 1
    old = _fake_comfy(tmp_path / "old", flags=("--cuda-device",))
    older = Launcher()
    older.set_config({"comfyui": {"dir": str(old), "args": []}})
    assert older.comfy_install()[0] == old
    assert "--disable-fast-disk" not in older.comfy_service(8189).argv


def test_comfy_found_through_env(home, tmp_path, monkeypatch):
    folder = _fake_comfy(tmp_path)
    monkeypatch.setenv("COMFYUI_DIR", str(folder))
    assert Launcher().comfy_install()[0] == folder


def test_missing_comfy_is_unavailable_not_an_error(home, tmp_path, monkeypatch):
    ln = Launcher()
    monkeypatch.setattr(Launcher, "comfy_candidates", lambda self: [tmp_path / "nope"])
    port = _free_port()
    status = ln.status(ln.comfy_service(port))
    assert status["state"] == "unavailable" and "not found" in status["problem"]
    res = ln.start(f"comfyui@{port}", gpu=0)
    assert res["ok"] is False and "not found" in res["error"]


def test_comfy_without_python_env(home, tmp_path):
    folder = tmp_path / "C2"
    (folder / "comfy").mkdir(parents=True)
    (folder / "main.py").write_text("", encoding="utf-8")
    ln = Launcher()
    ln.set_config({"comfyui": {"dir": str(folder)}})
    assert "no Python environment" in ln.comfy_service().problem


def test_set_config_merges_and_validates(home):
    ln = Launcher()
    ln.set_config({"comfyui": {"dir": "X:/C", "gpu": "3"}})
    ln.set_config({"comfyui": {"args": ["--lowvram"]}, "ollama": {"exe": "o.exe"}})
    cfg = json.loads((home / "backends.json").read_text(encoding="utf-8"))
    assert cfg["comfyui"] == {"dir": "X:/C", "gpu": 3, "args": ["--lowvram"]}
    ln.set_config({"comfyui": {"dir": None}, "ollama": {"exe": ""}})
    cfg = ln.config()
    assert "dir" not in cfg["comfyui"] and "ollama" not in cfg
    with pytest.raises(ValueError):
        ln.set_config({"comfyui": {"gpu": "fast"}})
    with pytest.raises(ValueError):
        ln.set_config({"commands": [{"id": "Bad Id", "argv": ["x"], "health": "http://127.0.0.1:1/"}]})
    with pytest.raises(ValueError):
        ln.set_config({"commands": [{"id": "x", "argv": ["x"], "health": "http://10.0.0.2:1/"}]})


def test_unknown_service(home):
    assert Launcher().start("nope")["ok"] is False
    with pytest.raises(KeyError):
        Launcher().get("comfyui@99999")


def _server_command(port: int) -> dict:
    return {"id": "web", "label": "Test server", "capabilities": ["llm"],
            "argv": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            "health": f"http://127.0.0.1:{port}/"}


def test_command_lifecycle(home, tmp_path):
    port = _free_port()
    ln = Launcher(app="prospero")
    cmd = _server_command(port)
    cmd["cwd"] = str(tmp_path)
    ln.set_config({"commands": [cmd]})
    before = {s["id"]: s for s in ln.statuses()}
    assert before["cmd:web"]["state"] == "down" and before["cmd:web"]["startable"]
    res = ln.start("cmd:web", wait_s=20)
    try:
        assert res["ok"] and res["ready"], res
        st = ln.status(ln.get("cmd:web"))
        assert st["state"] == "running" and st["started_by"] == "prospero" and st["stoppable"]
        again = ln.start("web")
        assert again["already"] and again["state"] == "running"
        # another app of the family sees it and may stop it
        other = Launcher(app="hub")
        assert other.status(other.get("cmd:web"))["started_by"] == "prospero"
        stopped = other.stop("web")
        assert stopped["ok"], stopped
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and ln.status(ln.get("cmd:web"))["state"] != "down":
            time.sleep(0.3)
        assert ln.status(ln.get("cmd:web"))["state"] == "down"
        assert ln.stop("web") == {"ok": True, "service": "cmd:web", "detail": "not running"}
        assert "started by prospero" in ln.log_path("cmd:web").read_text(encoding="utf-8")
    finally:
        ln.stop("web")


def test_stop_refuses_a_server_started_elsewhere(home, tmp_path):
    port = _free_port()
    ln = Launcher()
    ln.set_config({"commands": [_server_command(port)]})
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if launch.IS_WIN else 0
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=str(tmp_path),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        assert ln.wait_ready("cmd:web", 20)
        st = ln.status(ln.get("cmd:web"))
        assert st["state"] == "running" and not st["stoppable"] and st["started_by"] is None
        res = ln.stop("cmd:web")
        assert res["ok"] is False and "not started from the Hoard family" in res["error"]
        assert proc.poll() is None
        assert ln.start("cmd:web")["already"]
    finally:
        proc.kill()
        proc.wait(10)


def test_a_busy_server_is_running_not_down(home, monkeypatch):
    # a listening socket that never answers: ComfyUI in the middle of a heavy
    # render can leave its health page hanging for seconds
    ln = Launcher()
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]
        ln.set_config({"commands": [_server_command(port)]})
        monkeypatch.setattr(launch, "_http_status", lambda url, timeout=2.0: None)
        st = ln.status(ln.get("cmd:web"))
        assert st["state"] == "running" and st["busy"] is True
        assert ln.start("cmd:web")["already"]
    st = ln.status(ln.get("cmd:web"))
    assert st["state"] == "down" and st["busy"] is False


def test_a_command_that_dies_reports_its_log(home, tmp_path):
    port = _free_port()
    ln = Launcher()
    ln.set_config({"commands": [{"id": "boom", "argv": [sys.executable, "-c", "import sys; print('no model here'); sys.exit(3)"],
                                 "health": f"http://127.0.0.1:{port}/"}]})
    res = ln.start("boom", wait_s=15)
    assert res["ok"] is False and res["state"] == "exited" and "no model here" in res["error"]


def test_pick_gpu_prefers_free_memory_and_skips_busy(home, monkeypatch):
    monkeypatch.setattr(launch, "list_gpus", lambda: [
        {"index": 0, "name": "a", "free_mb": 1000, "total_mb": 12000},
        {"index": 1, "name": "b", "free_mb": 9000, "total_mb": 16000},
        {"index": 2, "name": "c", "free_mb": 8000, "total_mb": 16000},
    ])
    ln = Launcher()
    assert ln.pick_gpu() == 1
    assert ln.pick_gpu(exclude={1}) == 2
    assert ln.pick_gpu(exclude={0, 1, 2}) == 1  # everything busy: still the freest
    monkeypatch.setattr(launch, "list_gpus", lambda: [])
    assert ln.pick_gpu() is None


def test_state_survives_a_new_launcher_and_forgets_dead_pids(home):
    ln = Launcher()
    ln._save_state({"comfyui@8199": {"pid": 999_999_9, "created": 1.0, "by": "x", "gpu": 1}})
    ids = [s.id for s in Launcher().services()]
    assert "comfyui@8199" in ids  # still listed so it can be looked at
    assert Launcher()._owned("comfyui@8199") is None


def test_a_server_outlives_the_app_that_started_it(home, tmp_path):
    """Stopping an app kills its process tree; the server it started must
    not be part of that tree (it is shared by the family)."""
    import os

    port = _free_port()
    Launcher().set_config({"commands": [_server_command(port)]})
    code = ("import sys, time; from hoard_link.launch import Launcher; "
            "r = Launcher(app='parent').start('cmd:web', wait_s=20); print(r.get('ready'), flush=True); time.sleep(120)")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    try:
        assert parent.stdout.readline().strip() == "True"
        launch._kill_tree(parent.pid, grace_s=2)
        parent.wait(15)
        ln = Launcher()
        st = ln.status(ln.get("cmd:web"))
        assert st["state"] == "running" and st["started_by"] == "parent" and st["stoppable"], st
    finally:
        if parent.poll() is None:
            parent.kill()
        Launcher().stop("cmd:web")


def test_a_stop_script_stops_a_server_started_elsewhere(home, tmp_path):
    port = _free_port()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if launch.IS_WIN else 0
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=str(tmp_path),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    stopper = tmp_path / "stop.py"
    stopper.write_text(f"import os, signal\nos.kill({proc.pid}, signal.SIGTERM)\nprint('stopped it')\n", encoding="utf-8")
    cmd = _server_command(port)
    cmd["stop_argv"] = [sys.executable, str(stopper)]
    ln = Launcher()
    ln.set_config({"commands": [cmd]})
    try:
        assert ln.wait_ready("cmd:web", 20)
        st = ln.status(ln.get("cmd:web"))
        assert st["stoppable"] and st["started_by"] is None
        res = ln.stop("web")
        assert res["ok"] and res["via"] == "stop command" and "stopped it" in res["output"], res
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


def test_memory_maps_gpu_processes_to_services(home, monkeypatch):
    ln = Launcher()
    monkeypatch.setattr(launch, "_gpu_processes", lambda: (
        [{"index": 0, "name": "a", "used_mb": 11000, "free_mb": 1000, "total_mb": 12000},
         {"index": 1, "name": "b", "used_mb": 12000, "free_mb": 4000, "total_mb": 16000}],
        [{"pid": 50, "name": "llama-server.exe", "gpu": 0, "used_mb": None},
         {"pid": 50, "name": "llama-server.exe", "gpu": 1, "used_mb": None},
         {"pid": 60, "name": "python.exe", "gpu": 1, "used_mb": None},
         {"pid": 70, "name": "chrome.exe", "gpu": 0, "used_mb": None},
         {"pid": 80, "name": "GameLauncher.exe", "gpu": 0, "used_mb": None}]))
    monkeypatch.setattr(launch, "_listeners", lambda: {8081: 50, 8188: 60, 62234: 80})
    monkeypatch.setattr(Launcher, "statuses", lambda self, ports=None: [
        {"id": "comfyui@8188", "label": "ComfyUI :8188", "kind": "comfyui", "state": "running",
         "url": "http://127.0.0.1:8188", "pid": None, "stoppable": True, "started_by": "prospero"},
        {"id": "ollama", "label": "Ollama", "kind": "ollama", "state": "down", "url": "http://127.0.0.1:11434",
         "pid": None, "stoppable": False, "started_by": None}])
    monkeypatch.setattr(launch, "_what_is_loaded", lambda item: {"held_mb": 2000, "models": []})
    monkeypatch.setattr(launch, "host_stats", lambda: {"ram": {"total_mb": 1, "used_mb": 1, "free_mb": 0}})
    mem = launch.memory(ln)
    assert mem["host"]["ram"]["total_mb"] == 1
    by = {s["id"]: s for s in mem["services"]}
    assert by["comfyui@8188"]["gpus"] == [1] and by["comfyui@8188"]["held_mb"] == 2000
    assert by["pid:50"]["label"] == "llama-server.exe :8081" and sorted(by["pid:50"]["gpus"]) == [0, 1]
    assert "ollama" not in by
    g0 = mem["gpus"][0]
    assert "pid:80" not in by  # listens and uses the GPU, but serves no model
    assert g0["services"] == ["pid:50"] and g0["others"] == 2


def test_host_stats_reports_ram_and_cpu():
    st = launch.host_stats()
    assert st["ram"]["total_mb"] > 0 and 0 <= st["ram"]["used_mb"] <= st["ram"]["total_mb"]
    assert 0 <= launch.host_stats()["cpu_pct"] <= 100
