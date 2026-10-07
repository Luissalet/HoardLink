import subprocess
import pytest
from hoard_link import gpu
from hoard_link.hub.lease import LeaseArbiter


def test_confirmed_empty_is_not_a_failed_probe(monkeypatch):
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
    result = gpu.probe_gpu_memory()
    assert result.status == "empty" and not result.error
    arbiter = LeaseArbiter(gpu_fn=lambda: result)
    assert arbiter.request(owner="cpu-host", vram_mb=100)["state"] == "granted"
    assert arbiter.status()["inventory_status"] == "empty"


@pytest.mark.parametrize("failure,kind", [(FileNotFoundError("missing"), "not_found"),
    (subprocess.TimeoutExpired("nvidia-smi", 2), "timeout"), (PermissionError("denied"), "unavailable")])
def test_probe_execution_failures_are_visible_and_do_not_grant_leases(monkeypatch, failure, kind):
    def run(*args, **kwargs): raise failure
    monkeypatch.setattr(gpu.subprocess, "run", run)
    result = gpu.probe_gpu_memory()
    assert result.status == "error" and result.error_type == kind
    arbiter = LeaseArbiter(gpu_fn=lambda: result)
    reply = arbiter.request(owner="model", vram_mb=8000)
    assert reply["state"] == "queued" and reply["gpu"] is None
    state = arbiter.status()
    assert state["inventory_status"] == "error" and state["inventory_error_type"] == kind
    assert not state["leases"] and "could not be verified" in state["queue"][0]["note"]


def test_nonzero_exit_preserves_reason(monkeypatch):
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 4, "", "GPU query failed"))
    result = gpu.probe_gpu_memory()
    assert result.error_type == "exit" and "exited 4" in result.error and "GPU query failed" in result.error


@pytest.mark.parametrize("text", ["driver message", "0, [N/A], [N/A]", "0, 100, 101", "0, 100, 1\n0, 100, 2"])
def test_unusable_or_inconsistent_inventory_cannot_admit_a_model(monkeypatch, text):
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, text, ""))
    result = gpu.probe_gpu_memory()
    assert result.status == "error" and result.error_type == "invalid_output"
    arbiter = LeaseArbiter(gpu_fn=lambda: result)
    assert arbiter.request(owner="model", vram_mb=10)["state"] == "queued"
    assert arbiter.status()["gpus"] == []


def test_probe_recovery_admits_only_one_fitting_model_and_reserves_memory():
    value = [gpu.GpuProbe(error="slow driver", error_type="timeout")]
    arbiter = LeaseArbiter(gpu_fn=lambda: value[0], cache_s=0, headroom_mb=1000)
    a = arbiter.request(owner="a", vram_mb=8000)
    b = arbiter.request(owner="b", vram_mb=8000)
    assert a["state"] == b["state"] == "queued"
    value[0] = gpu.GpuProbe((gpu.GpuMemory(0, 12000, 1000),))
    state = arbiter.status(force=True)
    assert state["inventory_status"] == "ready" and state["inventory_error"] is None
    assert [lease["owner"] for lease in state["leases"]] == ["a"]
    assert [lease["owner"] for lease in state["queue"]] == ["b"]
    assert state["gpus"][0]["reserved_mb"] == 8000
    assert not state["leases"][0]["note"]


def test_provider_exception_is_not_silently_converted_to_gpu_absence():
    def broken(): raise RuntimeError("driver unavailable")
    arbiter = LeaseArbiter(gpu_fn=broken)
    assert arbiter.request(owner="x", vram_mb=100)["state"] == "queued"
    assert arbiter.status()["inventory_error_type"] == "provider_error"


def test_last_inventory_does_not_authorize_new_grants_after_probe_failure():
    value = [gpu.GpuProbe((gpu.GpuMemory(0, 24000, 0),))]
    arbiter = LeaseArbiter(gpu_fn=lambda: value[0], cache_s=0)
    a = arbiter.request(owner="a", vram_mb=4000)
    value[0] = gpu.GpuProbe(error="timeout", error_type="timeout")
    b = arbiter.request(owner="b", vram_mb=4000)
    assert a["state"] == "granted" and b["state"] == "queued"
    state = arbiter.status(force=True)
    assert state["inventory_status"] == "error" and state["gpus"] == []
    assert [lease["owner"] for lease in state["leases"]] == ["a"]


def test_windows_system32_resolution_without_path(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu.sys, "platform", "win32")
    monkeypatch.setattr(gpu.shutil, "which", lambda name: None)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    binary = tmp_path / "System32" / "nvidia-smi.exe"
    binary.parent.mkdir(); binary.write_bytes(b"fixture, never executed")
    assert gpu._nvidia_smi() == str(binary)
