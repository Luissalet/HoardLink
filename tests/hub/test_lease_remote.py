import os

from hoard_link.hub.lease_remote import RemoteLeaseArbiter
from hoard_link.hub.server import serve_in_thread

from .test_lease import FakeGpus, make


def test_remote_requests_and_service_bind_share_the_primary_queue(hub):
    primary = hub.leases = make(FakeGpus((16000, 0)))
    server, thread = serve_in_thread(hub, port=0)
    remote = RemoteLeaseArbiter(62501, f"http://127.0.0.1:{server.server_port}")
    try:
        first = primary.request(owner="primary", gpu=0, vram_mb=14000)
        job = remote.request(owner="studio", service="comfyui:draft", gpu=0, vram_mb=10000)
        assert job["state"] == "queued"
        assert primary.status()["queue"][0]["lease_id"] == job["lease_id"]
        primary.release(first["lease_id"])
        job = remote.request(lease_id=job["lease_id"])
        assert job["state"] == "granted"
        startup = remote.reserve_service_for_work(job["lease_id"], "comfyui:draft", 512, 0)
        remote.bind_process(startup["lease_id"], os.getpid(), service="comfyui:draft")
        assert primary.get(startup["lease_id"])["lease"]["purpose"] == "hub@62501/comfyui:draft"
        assert remote.get(startup["lease_id"])["lease"]["purpose"] == "comfyui:draft"
        assert primary.status()["gpus"][0]["reserved_mb"] == 10000
        remote.release(job["lease_id"])
        assert primary.status()["gpus"][0]["reserved_mb"] == 512
        remote.release(startup["lease_id"])
        assert not primary.status()["leases"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_remote_fails_when_the_machine_arbiter_is_unavailable(monkeypatch):
    monkeypatch.setattr("hoard_link.hub.lease_remote.fetch", lambda *_a, **_k: (None, None))
    remote = RemoteLeaseArbiter(62501)
    rejected = remote.request(gpu=0, vram_mb=2000)
    assert rejected["ok"] is False and rejected["status"] == 503
    assert remote.status()["inventory"] is False
