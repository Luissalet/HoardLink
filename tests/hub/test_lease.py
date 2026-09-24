"""The GPU lease arbiter: fit, queue order, priority, reaping, persistence,
no double-booking — and the same over HTTP and the agent tools."""

from __future__ import annotations

import json
import threading
import time

import pytest

from hoard_link.gpu import GpuMemory
from hoard_link.hub import tools
from hoard_link.hub.lease import LeaseArbiter, LeaseError
from hoard_link.hub.server import make_server

from .test_hub_and_server import _http


class FakeGpus:
    def __init__(self, *gpus: tuple[int, int]):
        self.gpus = [GpuMemory(index=i, total_mb=total, used_mb=used) for i, (total, used) in enumerate(gpus)]
        self.calls = 0

    def set_used(self, index: int, used: int) -> None:
        g = self.gpus[index]
        self.gpus[index] = GpuMemory(index=g.index, total_mb=g.total_mb, used_mb=used)

    def __call__(self):
        self.calls += 1
        return list(self.gpus)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(gpus, tmp_path=None, clock=None, alive=None, headroom=0):
    return LeaseArbiter(str(tmp_path / "leases.json") if tmp_path else None, gpu_fn=gpus, headroom_mb=headroom,
                        now=clock or Clock(), alive=alive or (lambda pid, created: True), cache_s=0)


# ---- arbiter ------------------------------------------------------------------

def test_grant_when_it_fits_on_the_roomiest_gpu():
    arb = make(FakeGpus((24000, 20000), (24000, 4000)))
    r = arb.request(owner="scribe", purpose="whisper", vram_mb=6000)
    assert r["state"] == "granted" and r["gpu"] == 1 and r["position"] == 0
    st = arb.status()
    assert st["gpus"][1]["reserved_mb"] == 6000 and st["gpus"][1]["available_mb"] == 24000 - 4000 - 6000
    assert [l["owner"] for l in st["leases"]] == ["scribe"] and st["queue"] == []


def test_pinned_gpu_and_validation():
    arb = make(FakeGpus((24000, 0), (24000, 0)))
    assert arb.request(owner="a", vram_mb=1000, gpu=1)["gpu"] == 1
    assert arb.request(owner="a", vram_mb=1000, gpu="any")["state"] == "granted"
    with pytest.raises(LeaseError):
        arb.request(owner="a", vram_mb=1000, gpu=7)
    with pytest.raises(LeaseError):
        arb.request(owner="a", vram_mb=30000)          # can never fit anywhere
    with pytest.raises(LeaseError):
        arb.request(owner="a", vram_mb=-1)
    with pytest.raises(LeaseError):
        arb.request(owner="a", vram_mb="lots")


def test_reservations_are_not_double_booked():
    gpus = FakeGpus((24000, 4000))
    arb = make(gpus)
    a = arb.request(owner="a", vram_mb=12000)
    b = arb.request(owner="b", vram_mb=12000)            # nvidia-smi still says 20000 free: must queue
    assert a["state"] == "granted" and b["state"] == "queued" and b["position"] == 1
    # a's model loads: nvidia-smi now shows it. It must not be counted twice.
    gpus.set_used(0, 16000)
    assert arb.status()["gpus"][0]["available_mb"] == 24000 - 16000
    c = arb.request(owner="c", vram_mb=7000)
    assert c["state"] == "queued"                       # b is ahead and blocks this GPU (no starvation)
    gpus.set_used(0, 4000)                              # a unloads its model...
    arb.release(a["lease_id"])                          # ...and releases
    assert arb.get(b["lease_id"])["state"] == "granted"
    # b is reserved but not loaded yet: 24000 - (4000 + 12000) = 8000 left, enough for c
    assert arb.get(c["lease_id"])["state"] == "granted"
    g = arb.status()["gpus"][0]
    assert g["reserved_mb"] == 19000 and g["available_mb"] == 1000
    assert arb.request(owner="d", vram_mb=2000)["state"] == "queued"


def test_lease_less_memory_growth_is_respected():
    gpus = FakeGpus((24000, 2000))
    arb = make(gpus)
    arb.request(owner="a", vram_mb=6000)
    gpus.set_used(0, 20000)       # someone without a lease (a llama-server) took 18 GB
    assert arb.status()["gpus"][0]["available_mb"] == 4000
    assert arb.request(owner="b", vram_mb=5000)["state"] == "queued"


def test_queue_is_fifo_within_priority_and_priority_first():
    gpus = FakeGpus((10000, 0))
    arb = make(gpus)
    hold = arb.request(owner="hold", vram_mb=10000)
    low1 = arb.request(owner="low1", vram_mb=4000)
    low2 = arb.request(owner="low2", vram_mb=4000)
    high = arb.request(owner="high", vram_mb=4000, priority=5)
    assert [q["owner"] for q in arb.status()["queue"]] == ["high", "low1", "low2"]
    assert arb.get(high["lease_id"])["position"] == 1
    arb.release(hold["lease_id"])
    granted = {l["owner"] for l in arb.status()["leases"]}
    assert granted == {"high", "low1"}                  # 8000 of 10000; low2 waits
    assert arb.get(low2["lease_id"])["position"] == 1


def test_head_of_line_only_blocks_its_own_gpus():
    arb = make(FakeGpus((10000, 0), (10000, 0)))
    arb.request(owner="big0", vram_mb=9000, gpu=0)
    waiting = arb.request(owner="more0", vram_mb=5000, gpu=0)
    other = arb.request(owner="on1", vram_mb=5000, gpu=1)
    assert waiting["state"] == "queued" and other["state"] == "granted" and other["gpu"] == 1


def test_ttl_expiry_reaps_and_renew_extends():
    clock = Clock()
    arb = make(FakeGpus((10000, 0)), clock=clock)
    a = arb.request(owner="a", vram_mb=8000, ttl_s=60)
    b = arb.request(owner="b", vram_mb=8000, ttl_s=60)
    assert b["state"] == "queued"
    clock.t += 50
    assert arb.renew(a["lease_id"], ttl_s=60)["ok"]
    arb.renew(b["lease_id"])                              # keeps b in the queue
    clock.t += 50
    assert arb.get(a["lease_id"])["state"] == "granted"   # renewed at +50, lives to +110
    arb.renew(b["lease_id"])
    clock.t += 61
    assert arb.get(a["lease_id"])["ok"] is False           # expired
    assert arb.get(b["lease_id"])["state"] == "granted"    # and b took its place
    assert arb.status()["reaped"][-1]["reason"] == "expired"
    assert arb.renew("nope")["ok"] is False


def test_abandoned_queue_entry_leaves_the_queue():
    clock = Clock()
    arb = make(FakeGpus((10000, 0)), clock=clock)
    arb.request(owner="a", vram_mb=8000)
    b = arb.request(owner="b", vram_mb=8000)
    clock.t += 200                                       # b's owner never polled again
    assert arb.get(b["lease_id"])["ok"] is False
    assert arb.status()["queue"] == []


def test_dead_owner_pid_is_reaped_later():
    alive = {"ok": True}
    arb = make(FakeGpus((10000, 0)), alive=lambda pid, created: alive["ok"])
    a = arb.request(owner="app", vram_mb=8000, pid=4242)
    assert a["state"] == "granted"
    alive["ok"] = False                                   # the app crashed without releasing
    assert arb.get(a["lease_id"])["ok"] is False


def test_dead_owner_pid_is_reaped():
    dead = {4242}
    arb = make(FakeGpus((10000, 0)), alive=lambda pid, created: pid not in dead)
    a = arb.request(owner="crashy", vram_mb=8000, pid=4242)
    assert a["ok"] is False                               # owner not running: never enters the queue
    b = arb.request(owner="b", vram_mb=8000, pid=1)
    assert b["state"] == "granted"
    assert any(r["reason"].startswith("owner process 4242") for r in arb.status()["reaped"])


def test_real_pid_check_uses_this_process():
    from hoard_link.hub.lease import pid_alive, pid_create_time
    import os
    assert pid_alive(os.getpid(), pid_create_time(os.getpid()))
    pytest.importorskip("psutil")
    assert pid_alive(os.getpid(), 1.0) is False           # same pid, other process (recycled)


def test_persistence_survives_a_restart(tmp_path):
    clock = Clock()
    gpus = FakeGpus((10000, 1000))
    arb = make(gpus, tmp_path, clock)
    a = arb.request(owner="a", vram_mb=6000, ttl_s=100)
    old = arb.request(owner="old", vram_mb=100, ttl_s=10)
    b = arb.request(owner="b", vram_mb=6000, ttl_s=100)
    assert old["state"] == "granted" and b["state"] == "queued"
    saved = json.loads((tmp_path / "leases.json").read_text())
    assert {l["owner"] for l in saved["leases"]} == {"a", "b", "old"}
    clock.t += 20                                        # "old" expires while the hub is down
    again = make(gpus, tmp_path, clock)
    assert again.get(a["lease_id"])["state"] == "granted"
    assert again.get(b["lease_id"])["state"] == "queued"
    assert again.get(old["lease_id"])["ok"] is False
    # and the seq counter continues, so FIFO still holds
    c = again.request(owner="c", vram_mb=6000)
    assert [q["owner"] for q in again.status()["queue"]] == ["b", "c"]
    assert c["position"] == 2


def test_persistence_reaps_dead_pids_on_load(tmp_path):
    arb = make(FakeGpus((10000, 0)), tmp_path)
    a = arb.request(owner="a", vram_mb=1000, pid=999999)
    again = make(FakeGpus((10000, 0)), tmp_path, alive=lambda pid, created: False)
    assert again.get(a["lease_id"])["ok"] is False


def test_no_gpu_inventory_grants_without_check():
    arb = make(lambda: [])
    r = arb.request(owner="a", vram_mb=99999)
    assert r["state"] == "granted" and r["gpu"] is None and "no GPU inventory" in r["lease"]["note"]
    assert arb.status()["inventory"] is False


def test_headroom_is_kept_free():
    arb2 = make(FakeGpus((10000, 0)), headroom=1000)
    with pytest.raises(LeaseError):
        arb2.request(owner="a", vram_mb=9500)             # never fits with 1000 kept free
    assert arb2.request(owner="a", vram_mb=9000)["state"] == "granted"


def test_wait_long_polls_until_granted():
    arb = make(FakeGpus((10000, 0)))
    a = arb.request(owner="a", vram_mb=8000)
    b = arb.request(owner="b", vram_mb=8000)
    threading.Timer(0.3, lambda: arb.release(a["lease_id"])).start()
    t0 = time.monotonic()
    r = arb.request(lease_id=b["lease_id"], wait=True, wait_s=5)
    assert r["state"] == "granted" and time.monotonic() - t0 < 3


def test_wait_returns_queued_after_wait_s():
    arb = make(FakeGpus((10000, 0)))
    arb.request(owner="a", vram_mb=8000)
    t0 = time.monotonic()
    r = arb.request(owner="b", vram_mb=8000, wait=True, wait_s=0.5)
    assert r["state"] == "queued" and 0.4 < time.monotonic() - t0 < 3


def test_release_is_idempotent():
    arb = make(FakeGpus((10000, 0)))
    a = arb.request(owner="a", vram_mb=1)
    assert arb.release(a["lease_id"])["released"] is True
    assert arb.release(a["lease_id"])["released"] is False


# ---- hub, HTTP and tools ---------------------------------------------------------

@pytest.fixture
def gpu_hub(family, tmp_path):
    from hoard_link.hub.config import HubConfig
    from hoard_link.hub.core import Hub
    from .conftest import free_port
    gpus = FakeGpus((24000, 2000), (24000, 20000))
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "gdata"), roots=[str(family["root"])], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], lease_headroom_mb=0)
    hub = Hub(cfg, gpu_fn=gpus)
    server = make_server(hub, port=cfg.port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield hub, cfg.url, gpus
    server.shutdown()


def test_http_lease_endpoints(gpu_hub):
    hub, url, _ = gpu_hub
    status, a = _http(url + "/api/lease/request", {"owner": "scribe", "purpose": "whisper", "vram_mb": 20000})
    assert status == 200 and a["state"] == "granted" and a["gpu"] == 0
    status, b = _http(url + "/api/lease/request", {"owner": "daguerre", "vram_mb": 10000, "wait": True, "wait_s": 0.2})
    assert status == 200 and b["state"] == "queued" and b["position"] == 1
    status, one = _http(url + "/api/lease/" + b["lease_id"])
    assert status == 200 and one["state"] == "queued"
    status, st = _http(url + "/api/lease")
    assert status == 200 and len(st["gpus"]) == 2 and len(st["leases"]) == 1 and len(st["queue"]) == 1
    assert st["gpus"][0]["reserved_mb"] == 20000
    status, r = _http(url + "/api/lease/renew", {"lease_id": a["lease_id"], "ttl_s": 120})
    assert status == 200 and r["lease"]["ttl_s"] == 120
    # b waits on the lease it already has; a release elsewhere grants it
    threading.Timer(0.3, lambda: _http(url + "/api/lease/release", {"lease_id": a["lease_id"]})).start()
    status, b2 = _http(url + "/api/lease/request", {"lease_id": b["lease_id"], "wait": True, "wait_s": 5})
    assert status == 200 and b2["state"] == "granted" and b2["gpu"] == 0
    assert _http(url + "/api/lease/nope")[0] == 404
    assert _http(url + "/api/lease/renew", {"lease_id": "nope"})[0] == 404
    status, bad = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 999999})
    assert status == 400 and "never fit" in bad["error"]
    status, _ = _http(url + "/api/lease/request", {"vram_mb": 1}, headers={"Sec-Fetch-Site": "cross-site",
                                                                         "Sec-Fetch-Mode": "cors"})
    assert status == 403
    import os
    assert os.path.isfile(hub.config.leases_file)


def test_lease_tools(gpu_hub):
    hub, url, _ = gpu_hub
    names = {t["name"] for t in tools.catalogue()}
    assert {"hub_lease_status", "hub_lease_request", "hub_lease_release"} <= names
    ro = {t["name"]: t.get("annotations", {}).get("readOnlyHint") for t in tools.catalogue()}
    assert ro["hub_lease_status"] is True and not ro["hub_lease_request"] and not ro["hub_lease_release"]
    r = tools.call(hub, "hub_lease_request", {"vram_mb": 1000, "owner": "agent", "purpose": "test"})
    assert r["state"] == "granted" and "status" not in r
    st = tools.call(hub, "hub_lease_status", {})
    assert st["leases"][0]["owner"] == "agent"
    assert tools.call(hub, "hub_lease_release", {"lease_id": r["lease_id"]})["released"] is True
    assert tools.call(hub, "hub_lease_request", {"vram_mb": 10 ** 7})["ok"] is False
    # and over the bearer-protected agent route
    status, body = _http(url + "/api/agent/call", {"tool": "hub_lease_status", "arguments": {}},
                         headers={"Authorization": "Bearer " + hub.token})
    assert status == 200 and body["result"]["inventory"] is True


def test_lease_panel_is_in_the_ui(gpu_hub):
    import urllib.request
    _, url, _ = gpu_hub
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    html = opener.open(url + "/", timeout=5).read().decode()
    js = opener.open(url + "/ui/app.js", timeout=5).read().decode()
    assert 'id="gpu-panel"' in html and 'id="grid"' in html
    assert "/api/lease/release" in js and "/api/apps" in js
