from __future__ import annotations

import asyncio
import json
import subprocess
import threading

import httpx
import pytest

from hoard_link import gpu
from hoard_link._comfy import ComfyClient
from hoard_link.errors import BackendError
from tests.conftest import Router, make_link


def test_nvidia_smi_gets_no_window_flag_and_utf8_on_windows(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, stdout="0, 24576, 20000\n1, 36864, 32000\n", stderr="")

    monkeypatch.setattr(gpu.sys, "platform", "win32")
    monkeypatch.setattr(gpu.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(gpu.subprocess, "run", fake_run)
    monkeypatch.setattr(gpu.shutil, "which", lambda name: None)
    out = gpu.gpu_free_mb()
    assert seen["creationflags"] == 0x08000000
    assert seen["encoding"] == "utf-8"
    assert seen["stdin"] is subprocess.DEVNULL
    assert [g.free_mb for g in out] == [4576, 4864]


def test_nvidia_smi_na_values_are_skipped(monkeypatch):
    monkeypatch.setattr(
        gpu.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="0, [N/A], [N/A]\n1, 100, 40\n", stderr=""),
    )
    assert [(g.index, g.free_mb) for g in gpu.gpu_free_mb()] == [(1, 60)]


@pytest.mark.asyncio
async def test_gpu_query_runs_off_the_event_loop_and_reports_best_gpu(monkeypatch):
    threads = []

    def fake_gpu_free_mb():
        threads.append(threading.current_thread())
        return [gpu.GpuMemory(0, 24576, 23000), gpu.GpuMemory(1, 36864, 30000)]

    import hoard_link.link as link_mod

    monkeypatch.setattr(link_mod, "gpu_free_mb", fake_gpu_free_mb)
    router = Router().get(8188, "/system_stats", httpx.Response(200, json={"devices": []}))
    res = await make_link(router).resolve("image")
    assert threads and threads[0] is not threading.main_thread()
    assert res.details["vram_free_mb"] == 6864
    assert len(res.details["gpus"]) == 2


def comfy(handler) -> ComfyClient:
    return ComfyClient("http://127.0.0.1:8188/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow, match",
    [
        ({"last_node_id": 9, "nodes": [{"id": 1}], "extra": {}}, "UI export"),
        ({"links": [], "version": 0.4}, "UI export"),
        ([{"class_type": "X", "inputs": {}}], "API-format"),
        ({"prompt": {"1": {"class_type": "X", "inputs": {}}}}, "API-format"),
        ({}, "API-format"),
    ],
)
async def test_queue_rejects_non_api_workflows_before_any_request(workflow, match):
    def never(request):
        raise AssertionError("must not reach the server")

    with pytest.raises(ValueError, match=match):
        await comfy(never).queue(workflow, "cid")


@pytest.mark.asyncio
async def test_queue_400_keeps_node_errors_in_backend_error():
    body = {"error": {"type": "prompt_outputs_failed_validation"}, "node_errors": {"3": {"errors": ["bad ckpt"]}}}
    client = comfy(lambda r: httpx.Response(400, json=body))
    with pytest.raises(BackendError) as exc:
        await client.queue({"3": {"class_type": "KSampler", "inputs": {}}}, "cid")
    assert exc.value.status == 400 and "bad ckpt" in exc.value.body_excerpt


@pytest.mark.asyncio
async def test_wait_raises_on_failed_execution():
    history = {
        "p1": {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_error", {"node_id": "3", "node_type": "KSampler", "exception_message": "CUDA out of memory"}]],
            },
            "outputs": {},
        }
    }
    client = comfy(lambda r: httpx.Response(200, json=history))
    with pytest.raises(BackendError, match="CUDA out of memory"):
        await client.wait("p1", timeout_s=1, poll_interval_s=0)
