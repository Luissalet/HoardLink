"""The ``lease()`` client against a real hub server (fake GPUs), its
graceful fallback when no hub answers, and how it finds/starts the hub."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from hoard_link import Lease, LeaseError, LeaseTimeout, lease
from hoard_link import _hubclient
import importlib

# ``hoard_link.lease`` the attribute is the function; the module is in sys.modules.
lease_mod = importlib.import_module("hoard_link.lease")
from hoard_link.gpu import GpuMemory
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server

from .conftest import free_port
from .test_lease import FakeGpus


@pytest.fixture
def live_hub(tmp_path):
    gpus = FakeGpus((24000, 2000), (24000, 12000))
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(tmp_path / "none")], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], lease_headroom_mb=0)
    hub = Hub(cfg, gpu_fn=gpus)
    server = make_server(hub, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, cfg.url
    server.shutdown()


def test_sync_lease_granted_renewed_released(live_hub):
    hub, url = live_hub
    with lease(vram_mb=6000, purpose="whisper", owner="scribe", hub_url=url, ttl_s=6) as l:
        assert l.via == "hub" and l.state == "granted" and l.gpu == 0 and l.lease_id
        first = hub.leases.get(l.lease_id)["lease"]["expires_at"]
        time.sleep(2.6)                                   # renew every ttl/3 = 2 s
        assert hub.leases.get(l.lease_id)["lease"]["expires_at"] > first
        assert hub.leases.status()["leases"][0]["pid"]    # our pid is on it
    assert l.state == "released" and hub.leases.status()["leases"] == []


def test_sync_waits_in_the_queue_then_gets_it(live_hub):
    hub, url = live_hub
    big = hub.leases.request(owner="comfy", vram_mb=22000, gpu=0)
    threading.Timer(0.5, lambda: hub.leases.release(big["lease_id"])).start()
    with Lease(20000, "render", "daguerre", gpu=0, hub_url=url, timeout_s=10) as l:
        assert l.state == "granted" and l.gpu == 0


def test_sync_timeout_leaves_the_queue(live_hub):
    hub, url = live_hub
    hub.leases.request(owner="comfy", vram_mb=22000, gpu=0)
    with pytest.raises(LeaseTimeout):
        with lease(vram_mb=20000, gpu=0, hub_url=url, timeout_s=0.5):
            pass
    assert hub.leases.status()["queue"] == []


def test_sync_refusal_raises(live_hub):
    _, url = live_hub
    with pytest.raises(LeaseError):
        with lease(vram_mb=10 ** 7, hub_url=url):
            pass


@pytest.mark.asyncio
async def test_async_lease(live_hub):
    hub, url = live_hub
    async with lease(vram_mb=8000, purpose="clip", owner="daguerre", hub_url=url) as l:
        assert l.via == "hub" and l.state == "granted" and l.gpu == 0
        assert hub.leases.status()["gpus"][0]["reserved_mb"] == 8000
    assert hub.leases.status()["leases"] == []


@pytest.mark.asyncio
async def test_async_wait_and_cancel(live_hub):
    hub, url = live_hub
    hub.leases.request(owner="comfy", vram_mb=22000, gpu=0)
    task = asyncio.ensure_future(lease(vram_mb=20000, gpu=0, hub_url=url).aacquire())
    await asyncio.sleep(0.5)
    assert len(hub.leases.status()["queue"]) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(40):
        if not hub.leases.status()["queue"]:
            break
        await asyncio.sleep(0.05)
    assert hub.leases.status()["queue"] == []


def _fake_gpus():
    return [GpuMemory(index=0, total_mb=24000, used_mb=22000), GpuMemory(index=1, total_mb=24000, used_mb=4000)]


def test_fallback_without_hub_uses_local_check(monkeypatch, caplog):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", _fake_gpus)
    url = f"http://127.0.0.1:{free_port()}"
    with caplog.at_level("WARNING", logger="hoard_link.lease"):
        with lease(vram_mb=6000, purpose="fallback-test", hub_url=url) as l:
            assert l.via == "local" and l.state == "local" and l.gpu == 1 and l.lease_id is None
    assert "not reachable" in l.warning and "GPU 1 has 20000 MiB free" in l.warning
    assert any("not reachable" in r.message for r in caplog.records)


def test_fallback_when_nothing_fits_still_proceeds(monkeypatch):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", _fake_gpus)
    with lease(vram_mb=23000, hub_url=f"http://127.0.0.1:{free_port()}") as l:
        assert l.via == "local" and l.gpu is None and "proceeding anyway" in l.warning


@pytest.mark.asyncio
async def test_async_fallback(monkeypatch):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", lambda: [])
    async with lease(vram_mb=1, hub_url=f"http://127.0.0.1:{free_port()}") as l:
        assert l.via == "local" and "nvidia-smi" in l.warning


def test_fallback_when_hub_is_older_than_leases(monkeypatch, fake_app):
    """Something answers as the hub but has no /api/lease: local check."""
    monkeypatch.setattr(lease_mod, "gpu_free_mb", lambda: [])
    monkeypatch.setattr(_hubclient, "hub_up", lambda url, timeout=1.5: True)
    port, _ = fake_app
    with lease(vram_mb=1, hub_url=f"http://127.0.0.1:{port}") as l:
        assert l.via == "local"


def test_autostart_is_tried_then_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(lease_mod, "gpu_free_mb", lambda: [])
    monkeypatch.setattr(_hubclient, "ensure_hub", lambda url=None, wait_s=20: calls.append(url) or False)
    url = f"http://127.0.0.1:{free_port()}"
    with lease(vram_mb=1, hub_url=url, autostart=True) as l:
        assert l.via == "local"
    assert calls == [url]
    with lease(vram_mb=1, hub_url=url) as l:                 # HOARD_HUB_AUTOSTART=0 in the test env
        assert l.via == "local"
    assert calls == [url]


def test_ensure_hub_respects_the_switch_and_backs_off(monkeypatch):
    spawned = []
    monkeypatch.setattr(_hubclient, "hub_up", lambda url, timeout=1.5: False)
    monkeypatch.setattr(_hubclient, "start_headless", lambda url, repo=None: spawned.append(url) or True)
    url = f"http://127.0.0.1:{free_port()}"
    monkeypatch.setenv("HOARD_HUB_AUTOSTART", "0")
    assert _hubclient.ensure_hub(url) is False and spawned == []
    monkeypatch.setenv("HOARD_HUB_AUTOSTART", "1")
    assert _hubclient.ensure_hub(url, wait_s=0.2) is False and spawned == [url]
    assert _hubclient.ensure_hub(url, wait_s=0.2) is False and spawned == [url]   # backs off for a minute
    _hubclient._last_failed.clear()


def test_ensure_hub_waits_for_the_spawned_hub(monkeypatch):
    state = {"up": False}
    monkeypatch.setenv("HOARD_HUB_AUTOSTART", "1")
    monkeypatch.setattr(_hubclient, "hub_up", lambda url, timeout=1.5: state["up"])

    def spawn(url, repo=None):
        threading.Timer(0.3, lambda: state.update(up=True)).start()
        return True

    monkeypatch.setattr(_hubclient, "start_headless", spawn)
    assert _hubclient.ensure_hub(f"http://127.0.0.1:{free_port()}", wait_s=5) is True


def test_hub_url_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("HOARD_HUB_URL", raising=False)
    monkeypatch.delenv("HOARD_HUB_DATA_DIR", raising=False)
    assert _hubclient.hub_url("http://127.0.0.1:1/") == "http://127.0.0.1:1"
    (tmp_path / "url").write_text("http://127.0.0.1:8899", encoding="utf-8")
    monkeypatch.setenv("HOARD_HUB_DATA_DIR", str(tmp_path))
    assert _hubclient.hub_url() == "http://127.0.0.1:8899"
    monkeypatch.setenv("HOARD_HUB_URL", "http://127.0.0.1:8898/")
    assert _hubclient.hub_url() == "http://127.0.0.1:8898"
    assert _hubclient.hub_repo_dir() is not None          # this checkout carries hoard_link/hub


def test_mcp_bridge_uses_the_shared_autostart(monkeypatch):
    from hoard_link.hub import mcp
    seen = []
    monkeypatch.setattr(_hubclient, "ensure_hub", lambda url=None, wait_s=20: seen.append(url) or False)
    monkeypatch.setenv("HOARD_HUB_URL", "http://127.0.0.1:1")
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "hub_lease_status", "arguments": {}}})
    assert reply["result"]["isError"] is True and seen == ["http://127.0.0.1:1"]
