from __future__ import annotations

import httpx
import pytest

from hoard_link._comfy import ComfyClient
from hoard_link.types import OutputFile

API_WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
}

UI_WORKFLOW = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}


def make_comfy_client(handler) -> ComfyClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ComfyClient("http://127.0.0.1:8188", client=client)


@pytest.mark.asyncio
async def test_queue_wait_outputs_download_round_trip():
    history = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt" and request.method == "POST":
            history["abc123"] = {}  # not finished yet on first check
            return httpx.Response(200, json={"prompt_id": "abc123", "number": 1, "node_errors": {}})
        if path == "/history/abc123":
            if not history["abc123"]:
                history["abc123"] = {
                    "outputs": {
                        "9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}
                    }
                }
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"abc123": history["abc123"]})
        if path == "/view":
            return httpx.Response(200, content=b"png-bytes")
        return httpx.Response(404)

    client = make_comfy_client(handler)
    prompt_id = await client.queue(API_WORKFLOW, client_id="test-client")
    assert prompt_id == "abc123"

    entry = await client.wait(prompt_id, timeout_s=5.0, poll_interval_s=0)
    assert "outputs" in entry

    outs = await client.outputs(prompt_id)
    assert outs == [OutputFile(node_id="9", filename="out.png", subfolder="", type="output", kind="image")]

    data = await client.download(outs[0])
    assert data == b"png-bytes"


@pytest.mark.asyncio
async def test_queue_rejects_ui_format_workflow():
    async def handler(request):  # pragma: no cover - should never be called
        return httpx.Response(200, json={})

    client = make_comfy_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="API format"):
        await client.queue(UI_WORKFLOW, client_id="c")


@pytest.mark.asyncio
async def test_queue_rejects_malformed_workflow():
    client = make_comfy_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        await client.queue({"3": {"not_class_type": True}}, client_id="c")


@pytest.mark.asyncio
async def test_wait_times_out():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = make_comfy_client(handler)
    with pytest.raises(TimeoutError):
        await client.wait("neverdone", timeout_s=0.05, poll_interval_s=0.01)


@pytest.mark.asyncio
async def test_system_stats_and_object_info():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"vram": 1}})
        if request.url.path == "/object_info/CheckpointLoaderSimple":
            return httpx.Response(200, json={"CheckpointLoaderSimple": {}})
        return httpx.Response(404)

    client = make_comfy_client(handler)
    stats = await client.system_stats()
    assert stats == {"system": {"vram": 1}}
    info = await client.object_info("CheckpointLoaderSimple")
    assert info == {"CheckpointLoaderSimple": {}}


@pytest.mark.asyncio
async def test_upload_image_posts_multipart():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"name": "img.png", "subfolder": "", "type": "input"})

    client = make_comfy_client(handler)
    result = await client.upload_image(b"bytes", "img.png")
    assert result["name"] == "img.png"
    assert "multipart/form-data" in captured["content_type"]


@pytest.mark.asyncio
async def test_interrupt_and_free():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    client = make_comfy_client(handler)
    await client.interrupt()
    await client.free(unload_models=True, free_memory=True)
    assert calls == ["/interrupt", "/free"]
