"""GPU lists in leases ("any of GPUs 2 or 3") and protected GPUs: the arbiter, hub REST,
the agent tool, hub.json, and the lease() client with and without a hub."""

from __future__ import annotations

import importlib
import json
import threading

import pytest

from hoard_link import lease
from hoard_link.gpu import GpuMemory
from hoard_link.hub import tools
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.lease import LeaseArbiter, LeaseError, parse_gpu_request
from hoard_link.hub.server import make_server

from .conftest import free_port
from .test_hub_and_server import _http
from .test_lease import Clock, FakeGpus

lease_mod = importlib.import_module("hoard_link.lease")


def four(*used, total=24000):
    """A FakeGpus with one GPU per ``used`` value."""
    return FakeGpus(*[(total, u) for u in used])


def make(gpus, protected=(), headroom=0):
    return LeaseArbiter(None, gpu_fn=gpus, headroom_mb=headroom, now=Clock(), alive=lambda pid, created: True,
                        cache_s=0, protected_gpus=protected)


# ---- parsing -------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, "any"), ("any", "any"), ("AUTO", "any"), ("", "any"), ([], "any"),
    (2, 2), ("2", 2), (2.0, 2),
    ([2, 3], [2, 3]), ([3, 2, 3], [3, 2]), ("2,3", [2, 3]), (" 2 , 3 ", [2, 3]), ("3;2", [3, 2]), ((1, "2"), [1, 2]),
    ([2], [2]),
])
def test_parse_gpu_request(raw, expected):
    assert parse_gpu_request(raw) == expected


@pytest.mark.parametrize("raw", ["x", [1, "x"], [-1], True, "1,two", {"a": 1}, -3])
def test_parse_gpu_request_rejects_junk(raw):
    with pytest.raises(LeaseError):
        parse_gpu_request(raw)


# ---- arbiter ---------------------------------------------------------------------------

def test_list_places_on_the_roomiest_listed_gpu():
    arb = make(four(1000, 1000, 15000, 4000))                       # GPU 0 and 1 are emptier but not listed
    r = arb.request(owner="a", vram_mb=6000, gpu=[2, 3])
    assert r["state"] == "granted" and r["gpu"] == 3
    assert r["lease"]["gpu_request"] == [2, 3]                      # the request is shown as asked
    r2 = arb.request(owner="b", vram_mb=6000, gpu="2,3")
    assert r2["state"] == "granted" and r2["gpu"] == 3             # 24000-4000-6000 = 14000 > GPU 2's 9000
    r3 = arb.request(owner="c", vram_mb=6000, gpu=[2, 3])
    assert r3["gpu"] == 2                                           # GPU 3 has 8000 left now, GPU 2 has 9000
    assert arb.status()["leases"][0]["gpu_request"] == [2, 3]


def test_list_falls_back_to_the_other_gpu_when_one_is_full_and_queues_when_both_are():
    arb = make(four(0, 0, 22000, 20000))
    a = arb.request(owner="a", vram_mb=3000, gpu=[2, 3])
    assert a["state"] == "granted" and a["gpu"] == 3
    b = arb.request(owner="b", vram_mb=3000, gpu=[2, 3])            # GPU 3 now has 1000 left, GPU 2 2000
    assert b["state"] == "queued" and b["position"] == 1
    assert arb.request(owner="c", vram_mb=3000, gpu=0)["gpu"] == 0   # a request for another GPU still goes ahead
    arb.release(a["lease_id"])
    assert arb.get(b["lease_id"])["state"] == "granted" and arb.get(b["lease_id"])["gpu"] == 3


def test_queued_list_request_does_not_block_other_gpus():
    arb = make(four(0, 0, 23000, 23000))
    arb.request(owner="a", vram_mb=500, gpu=[2, 3])
    waiting = arb.request(owner="b", vram_mb=5000, gpu=[2, 3])
    assert waiting["state"] == "queued"
    assert arb.request(owner="c", vram_mb=5000, gpu=[0, 1])["state"] == "granted"
    assert arb.request(owner="d", vram_mb=100, gpu=[2, 3])["state"] == "queued"   # behind b, which holds 2 and 3 back


def test_list_validation():
    arb = make(four(0, 0, 0, 0))
    for bad in ([2, 9], "9", [0, "x"], [-1], "a,b"):
        with pytest.raises(LeaseError):
            arb.request(owner="a", vram_mb=1, gpu=bad)
    assert arb.request(owner="a", vram_mb=1, gpu=[1])["gpu"] == 1


def test_never_fits_uses_the_largest_gpu_in_the_list():
    gpus = FakeGpus((8000, 0), (8000, 0), (24000, 0), (12000, 0))
    arb = make(gpus)
    assert arb.request(owner="a", vram_mb=20000, gpu=[2, 3])["gpu"] == 2        # fits GPU 2 only
    with pytest.raises(LeaseError, match="never fit"):
        arb.request(owner="a", vram_mb=13000, gpu=[0, 3])                        # largest listed has 12000
    with pytest.raises(LeaseError, match="never fit"):
        arb.request(owner="a", vram_mb=9000, gpu="0,1")
    assert arb.request(owner="a", vram_mb=9000, gpu="0,3")["gpu"] == 3


def test_persisted_list_request_survives_a_restart(tmp_path):
    arb = LeaseArbiter(str(tmp_path / "leases.json"), gpu_fn=four(0, 0, 22000, 22000), headroom_mb=0,
                       now=Clock(), alive=lambda pid, created: True, cache_s=0)
    q = arb.request(owner="a", vram_mb=5000, gpu=[2, 3])
    assert q["state"] == "queued"
    again = LeaseArbiter(str(tmp_path / "leases.json"), gpu_fn=four(0, 0, 0, 0), headroom_mb=0,
                         now=Clock(), alive=lambda pid, created: True, cache_s=0)
    got = again.get(q["lease_id"])
    assert got["lease"]["gpu_request"] == [2, 3] and got["state"] == "granted" and got["gpu"] in (2, 3)


# ---- protected GPUs --------------------------------------------------------------------------

def test_any_never_gets_a_protected_gpu():
    gpus = FakeGpus((24000, 0), (24000, 0), (24000, 10000), (24000, 12000))
    arb = make(gpus, protected=[0, 1])
    r = arb.request(owner="a", vram_mb=4000)                                     # "any"
    assert r["gpu"] == 2
    assert arb.request(owner="a", vram_mb=4000, gpu="any")["gpu"] in (2, 3)
    assert arb.request(owner="a", vram_mb=4000, gpu=[0, 2, 3])["gpu"] == 0        # named explicitly: allowed
    assert arb.request(owner="a", vram_mb=4000, gpu=1)["gpu"] == 1


def test_any_queues_rather_than_spill_onto_a_protected_gpu():
    gpus = FakeGpus((24000, 0), (24000, 0), (24000, 22000), (24000, 22000))
    arb = make(gpus, protected=[0, 1])
    q = arb.request(owner="a", vram_mb=6000)
    assert q["state"] == "queued"
    st = arb.status()
    assert st["protected_gpus"] == [0, 1] and [g["protected"] for g in st["gpus"]] == [True, True, False, False]
    gpus.set_used(2, 1000)
    assert arb.get(q["lease_id"])["gpu"] == 2


def test_never_fits_for_any_ignores_protected_gpus():
    gpus = FakeGpus((48000, 0), (48000, 0), (12000, 0), (12000, 0))
    arb = make(gpus, protected=[0, 1])
    with pytest.raises(LeaseError, match="never fit"):
        arb.request(owner="a", vram_mb=20000)                                    # only 12000-GPUs are eligible for "any"
    assert arb.request(owner="a", vram_mb=20000, gpu=0)["gpu"] == 0
    assert arb.request(owner="a", vram_mb=20000, gpu=[0, 3])["gpu"] == 0


def test_all_gpus_protected_refuses_any_with_a_clear_error():
    arb = make(FakeGpus((24000, 0), (24000, 0)), protected=[0, 1])
    with pytest.raises(LeaseError, match="protected"):
        arb.request(owner="a", vram_mb=1)
    assert arb.request(owner="a", vram_mb=1, gpu=1)["gpu"] == 1


def test_nothing_is_protected_by_default():
    arb = make(FakeGpus((24000, 0), (24000, 5000)))
    assert arb.protected_gpus == [] and arb.status()["protected_gpus"] == []
    assert arb.request(owner="a", vram_mb=1000)["gpu"] == 0
    assert HubConfig().protected_gpus == [] and HubConfig().lease == {}


def test_protected_setting_accepts_lists_strings_and_ignores_junk():
    assert make(FakeGpus((1000, 0)), protected="0, 1").protected_gpus == [0, 1]
    assert make(FakeGpus((1000, 0)), protected=[1, "x", None, 1, True, -2]).protected_gpus == [1]
    assert make(FakeGpus((1000, 0)), protected=None).protected_gpus == []


def test_hub_json_lease_protected_gpus(tmp_path):
    (tmp_path / "hub.json").write_text(json.dumps({"lease": {"protected_gpus": [0, 1]}}), encoding="utf-8")
    cfg = HubConfig.load(tmp_path / "hub.json", env={"HOARD_HUB_DATA_DIR": str(tmp_path / "data")})
    assert cfg.protected_gpus == [0, 1]
    (tmp_path / "hub.json").write_text(json.dumps({"lease": "nope"}), encoding="utf-8")
    assert HubConfig.load(tmp_path / "hub.json", env={"HOARD_HUB_DATA_DIR": str(tmp_path / "data")}).protected_gpus == []
    path = HubConfig(data_dir=str(tmp_path / "d2"), lease={"protected_gpus": [0]}).save()
    assert json.loads(open(path, encoding="utf-8").read())["lease"] == {"protected_gpus": [0]}


# ---- hub REST and agent tools ----------------------------------------------------------------

@pytest.fixture
def hub4(family, tmp_path):
    gpus = FakeGpus((24000, 9000), (24000, 9000), (24000, 14000), (24000, 6000))
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "gdata"), roots=[str(family["root"])], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], lease_headroom_mb=0, lease={"protected_gpus": [0, 1]})
    hub = Hub(cfg, gpu_fn=gpus)
    server = make_server(hub, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, cfg.url
    server.shutdown()


def test_rest_accepts_a_list_and_a_comma_string(hub4):
    hub, url = hub4
    status, a = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 4000, "gpu": [2, 3]})
    assert status == 200 and a["gpu"] == 3 and a["lease"]["gpu_request"] == [2, 3]
    status, b = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 4000, "gpu": "2,3"})
    assert status == 200 and b["lease"]["gpu_request"] == [2, 3]
    status, bad = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 4000, "gpu": [2, 12]})
    assert status == 400 and "no GPU with index 12" in bad["error"]
    status, bad = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 4000, "gpu": "two"})
    assert status == 400
    status, st = _http(url + "/api/lease")
    assert st["protected_gpus"] == [0, 1] and st["gpus"][0]["protected"] is True and st["gpus"][3]["protected"] is False


def test_rest_any_skips_protected_and_explicit_gets_them(hub4):
    _, url = hub4
    status, a = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 3000})                 # no gpu key at all
    assert a["gpu"] == 3
    status, b = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 3000, "gpu": 1})
    assert b["gpu"] == 1
    status, c = _http(url + "/api/lease/request", {"owner": "x", "vram_mb": 3000, "gpu": [0, 1]})
    assert c["gpu"] == 0                                                                           # roomiest of the two


def test_tool_schema_and_calls(hub4):
    hub, url = hub4
    schema = next(t for t in tools.catalogue() if t["name"] == "hub_lease_request")["inputSchema"]["properties"]["gpu"]
    kinds = [alt.get("type") for alt in schema["anyOf"]]
    assert "integer" in kinds and "array" in kinds and "string" in kinds
    assert next(alt for alt in schema["anyOf"] if alt["type"] == "array")["items"] == {"type": "integer"}
    r = tools.call(hub, "hub_lease_request", {"vram_mb": 2000, "owner": "agent", "gpu": [2, 3]})
    assert r["state"] == "granted" and r["gpu"] == 3 and r["lease"]["gpu_request"] == [2, 3]
    r = tools.call(hub, "hub_lease_request", {"vram_mb": 2000, "owner": "agent", "gpu": "any"})
    assert r["gpu"] in (2, 3)
    r = tools.call(hub, "hub_lease_request", {"vram_mb": 2000, "owner": "agent", "gpu": "2,3"})
    assert r["state"] == "granted"
    assert tools.call(hub, "hub_lease_request", {"vram_mb": 2000, "gpu": [7]})["ok"] is False
    st = tools.call(hub, "hub_lease_status", {})
    assert st["protected_gpus"] == [0, 1]
    desc = next(t for t in tools.catalogue() if t["name"] == "hub_lease_status")["description"]
    assert "protected" in desc


# ---- client ---------------------------------------------------------------------------------

def test_client_sends_the_list_and_gets_a_listed_gpu(hub4):
    hub, url = hub4
    with lease(vram_mb=3000, purpose="render", owner="daguerre", gpu=[2, 3], hub_url=url) as l:
        assert l.via == "hub" and l.gpu == 3
        assert hub.leases.status()["leases"][0]["gpu_request"] == [2, 3]
    with lease(vram_mb=3000, owner="daguerre", gpu="2,3", hub_url=url) as l:
        assert l.gpu in (2, 3)
    with lease(vram_mb=3000, owner="daguerre", hub_url=url) as l:                # any: never the protected ones
        assert l.gpu in (2, 3)


@pytest.mark.asyncio
async def test_async_client_with_a_list(hub4):
    hub, url = hub4
    async with lease(vram_mb=3000, owner="daguerre", gpu=[0, 2], hub_url=url) as l:
        assert l.via == "hub" and l.gpu == 0                                     # GPU 0 has 15000 free, GPU 2 10000


def _fake4():
    return [GpuMemory(index=0, total_mb=24000, used_mb=1000), GpuMemory(index=1, total_mb=24000, used_mb=2000),
            GpuMemory(index=2, total_mb=24000, used_mb=20000), GpuMemory(index=3, total_mb=24000, used_mb=10000)]


@pytest.mark.parametrize("request_,expected", [
    ([2, 3], 3), ("2,3", 3), ([0, 1], 0), ([1, 2], 1), (2, 2), (None, 0), ("any", 0), ([3], 3),
])
def test_local_fallback_picks_the_roomiest_gpu_of_the_list(monkeypatch, request_, expected):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", _fake4)
    with lease(vram_mb=3000, gpu=request_, hub_url=f"http://127.0.0.1:{free_port()}") as l:
        assert l.via == "local" and l.gpu == expected


def test_local_fallback_prefers_a_gpu_that_fits_and_proceeds_when_none_does(monkeypatch):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", _fake4)
    url = f"http://127.0.0.1:{free_port()}"
    with lease(vram_mb=12000, gpu=[2, 3], hub_url=url) as l:                    # only GPU 3 has 12000 free... it has 14000
        assert l.gpu == 3
    with lease(vram_mb=15000, gpu=[2, 3], hub_url=url) as l:                    # neither fits: best-effort roomiest
        assert l.gpu == 3 and "proceeding anyway" in l.warning
    with lease(vram_mb=15000, gpu=2, hub_url=url) as l:                         # one named GPU: as asked
        assert l.gpu == 2


def test_local_fallback_ignores_unlisted_gpus_and_missing_inventory(monkeypatch):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", lambda: [GpuMemory(index=0, total_mb=24000, used_mb=0)])
    url = f"http://127.0.0.1:{free_port()}"
    with lease(vram_mb=100, gpu=[2, 3], hub_url=url) as l:                      # none of the listed GPUs exists here
        assert l.gpu is None
    monkeypatch.setattr(lease_mod, "gpu_free_mb", lambda: [])
    with lease(vram_mb=100, gpu=[2, 3], hub_url=url) as l:
        assert l.gpu is None and "nvidia-smi" in l.warning
    with lease(vram_mb=100, gpu=3, hub_url=url) as l:
        assert l.gpu == 3


@pytest.mark.asyncio
async def test_async_local_fallback_with_a_list(monkeypatch):
    monkeypatch.setattr(lease_mod, "gpu_free_mb", _fake4)
    async with lease(vram_mb=3000, gpu=[2, 3], hub_url=f"http://127.0.0.1:{free_port()}") as l:
        assert l.via == "local" and l.gpu == 3
