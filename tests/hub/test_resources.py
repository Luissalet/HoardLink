import json
import pytest
from hoard_link.hub.resources import ResourcePool


def pool(tmp_path, **kwargs):
    return ResourcePool(tmp_path / "claims.json", inventory_fn=lambda: {"cpu_slots": 4, "ram_available_mb": 4096}, **kwargs)


def test_invalid_configuration_and_incomplete_claims_are_preserved(tmp_path):
    with pytest.raises(ValueError, match="io_slots"):
        pool(tmp_path, config={"io_slots": 0})
    path = tmp_path / "claims.json"
    saved = json.dumps({"schema": 1, "claims": {"writer:a": {"id": "a", "owner": "writer"}}})
    path.write_text(saved, encoding="utf-8")
    with pytest.raises(ValueError, match="preserve"):
        pool(tmp_path)
    assert path.read_text(encoding="utf-8") == saved


def test_background_reserves_foreground_capacity_and_release_admits_queue(tmp_path):
    resources = pool(tmp_path)
    first = resources.request({"request_id": "render", "cpu_slots": 3}, "lumiere")
    assert first["claim"]["state"] == "granted"
    second = resources.request({"request_id": "index", "cpu_slots": 1}, "borges")
    assert second["claim"]["state"] == "queued"
    interactive = resources.request({"request_id": "chat", "cpu_slots": 1, "mode": "interactive"}, "faustus")
    assert interactive["claim"]["state"] == "granted"
    resources.release("render", "lumiere")
    assert resources.request({"request_id": "index", "cpu_slots": 1}, "borges")["claim"]["state"] == "granted"


def test_ram_and_io_are_reserved_and_owner_cannot_release_sibling(tmp_path):
    resources = pool(tmp_path)
    first = resources.request({"request_id": "a", "ram_mb": 3000, "io_slots": 1}, "kafka")
    assert first["claim"]["state"] == "granted"
    second = resources.request({"request_id": "b", "ram_mb": 1500, "io_slots": 1}, "atlas")
    assert second["claim"]["state"] == "queued"
    resources.release("a", "atlas")
    assert resources.request({"request_id": "b", "ram_mb": 1500, "io_slots": 1}, "atlas")["claim"]["state"] == "queued"
    resources.release("a", "kafka")
    assert resources.request({"request_id": "b", "ram_mb": 1500, "io_slots": 1}, "atlas")["claim"]["state"] == "granted"


def test_restart_preserves_claims_and_expiry_releases_them(tmp_path):
    now = [10.0]
    resources = pool(tmp_path, clock=lambda: now[0])
    resources.request({"request_id": "a", "cpu_slots": 3, "ttl_s": 10}, "lumiere")
    restarted = pool(tmp_path, clock=lambda: now[0])
    assert restarted.status()["reserved"]["cpu"] == 3
    now[0] = 21
    assert not restarted.status()["claims"]


def test_same_request_is_idempotent_but_conflicting_shape_is_refused(tmp_path):
    resources = pool(tmp_path)
    args = {"request_id": "a", "cpu_slots": 2}
    resources.request(args, "lumiere")
    resources.request(args, "lumiere")
    assert len(resources.status()["claims"]) == 1
    with pytest.raises(ValueError, match="conflicts"):
        resources.request({**args, "cpu_slots": 1}, "lumiere")


@pytest.mark.parametrize("args", [{"cpu_slots": True}, {"cpu_slots": 10}, {"cpu_slots": -1}, {"cpu_slots": 1, "ttl_s": 1}, {"cpu_slots": 1, "mode": "kill-native"}])
def test_impossible_or_malformed_requests_never_claim_resources(tmp_path, args):
    resources = pool(tmp_path)
    with pytest.raises(ValueError):
        resources.request(args, "lumiere")
    assert not resources.status()["claims"]


def test_unknown_ram_does_not_grant_memory_but_cpu_still_works(tmp_path):
    resources = ResourcePool(tmp_path / "claims.json", inventory_fn=lambda: {"cpu_slots": 4, "ram_available_mb": None})
    assert resources.request({"ram_mb": 100, "request_id": "ram"}, "atlas")["claim"]["state"] == "queued"
    resources.release("ram", "atlas")
    assert resources.request({"cpu_slots": 1}, "atlas")["claim"]["state"] == "granted"


def test_corrupt_journal_is_preserved_instead_of_ignoring_live_claims(tmp_path):
    path = tmp_path / "claims.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        pool(tmp_path)
    assert path.read_text() == "{broken"
