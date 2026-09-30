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
