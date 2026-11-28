from contextlib import ExitStack
import sys
from threading import Event, RLock
from types import SimpleNamespace

from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.registry import scan
from hoard_link.hub.server import serve_in_thread

from .conftest import free_port, write_manifest
from .test_hub_and_server import _http
from .test_lease import FakeGpus, make
from .test_resources import pool


def write_transition_manifest(root, port):
    return write_manifest(root / "voice", "cyrano", port,
                          launch={"kind": "process", "executable": sys.executable,
                                  "argv": ["-m", "boltagon_hoard"]},
                          extra={"x-family": {"canonical_id": "boltagon"}})


def test_transition_manifest_uses_existing_legacy_launch_override(tmp_path, monkeypatch):
    monkeypatch.delenv("BOLTAGON_TOKEN_FILE", raising=False)
    folder = write_transition_manifest(tmp_path, 45071)
    data = tmp_path / "voice-data"
    token = data / "custom-token"
    launch = {"executable": sys.executable, "cwd": str(folder),
              "argv": ["-m", "cyrano_hoard", "--data-dir", str(data)],
              "env": {"CYRANO_TOKEN_FILE": str(token)}}
    [app] = scan([str(tmp_path)], launch_overrides={"cyrano": launch})
    assert app.id == "boltagon" and app.launch_source == "override"
    assert app.launch.argv == launch["argv"]
    assert app.launch.env["CYRANO_TOKEN_FILE"] == str(token)
    assert app.token_file == str(token) and app.data_dir == str(data)

    preferred = {**launch, "argv": ["-m", "boltagon_hoard", "--data-dir", str(data)]}
    [app] = scan([str(tmp_path)], launch_overrides={"cyrano": launch, "boltagon": preferred})
    assert app.launch.argv == preferred["argv"]


def test_http_directory_includes_the_legacy_id_with_one_registered_app(tmp_path):
    apps = tmp_path / "apps"
    write_transition_manifest(apps, 45072)
    config = HubConfig.load(env={"HOARD_HUB_PORT": str(free_port()),
                                 "HOARD_HUB_DATA_DIR": str(tmp_path / "data"),
                                 "HOARD_HUB_ROOTS": str(apps)})
    hub = Hub(config)
    with ExitStack() as stack:
        stack.callback(hub.close)
        server, thread = serve_in_thread(hub)
        stack.callback(thread.join, 5)
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        status, data = _http(config.url + "/api/apps")
        assert status == 200
        rows = {app["id"]: app for app in data["apps"]}
        assert set(rows) == {"cyrano", "boltagon"}
        assert rows["cyrano"] == {**rows["boltagon"], "id": "cyrano", "alias_of": "boltagon"}
        assert data["counts"]["total"] == hub.app_count == 1


def test_profiles_and_manual_stops_honor_the_legacy_id(tmp_path):
    hub = object.__new__(Hub)
    hub._lock = RLock()
    hub._stopping_all = Event()
    hub._manual_stops = {"cyrano"}
    hub._apps = {"boltagon": SimpleNamespace(id="boltagon", name="Voice")}
    hub.config = SimpleNamespace(isolated=False, data_dir=str(tmp_path),
                                 read_profiles=lambda: {"work": {"apps": ["cyrano"]}})
    hub.commands = SimpleNamespace(status=lambda command: {})
    [profile] = hub.profiles_status({"boltagon": "running"})["profiles"]
    assert profile["state"] == "running"
    assert profile["members"][0]["id"] == "cyrano"

    assert hub.automation_blocked("cyrano") and hub.automation_blocked("boltagon")
    hub._manual_stop(["boltagon"], stopped=False)
    assert not hub.automation_blocked("cyrano")


def test_resource_claim_can_be_renewed_and_released_by_both_ids(tmp_path):
    request = {"request_id": "voice", "cpu_slots": 2}
    pool(tmp_path).request(request, "cyrano")
    restarted = pool(tmp_path)
    assert restarted.request(request, "boltagon")["claim"]["owner"] == "cyrano"
    assert restarted.status()["reserved"]["cpu"] == 2
    restarted.release("voice", "boltagon")
    assert restarted.status()["claims"] == []


def test_gpu_lease_shares_the_voice_service_across_ids():
    arbiter = make(FakeGpus((12000, 0)))
    startup = arbiter.request(owner="hub", vram_mb=2000)
    arbiter.bind_process(startup["lease_id"], 10, service="cyrano")
    work = arbiter.request(owner="cyrano", service="boltagon", vram_mb=1500)
    lease = arbiter.get(work["lease_id"])["lease"]
    assert lease["owner"] == "boltagon"
    assert lease["service_lease"] == startup["lease_id"]
    assert arbiter.status()["gpus"][0]["reserved_mb"] == 2000
