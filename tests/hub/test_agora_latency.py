"""Loopback latency budget with a private Hub, 20 workers and 3 console pollers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import http.client
import json
import socket
import threading
import time

import pytest

from hoard_link.hub.agora_console import AgoraConsoleFacet, LEADERS
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.registry import App
from hoard_link.hub.server import serve_in_thread


def _http(port, path, *, body=None, token=""):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=8)) as conn:
        conn.request("POST" if data is not None else "GET", path, data, headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw), len(raw)


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("condition did not become true")


@contextmanager
def _private_hub(root, monkeypatch):
    roots = root / "apps"
    roots.mkdir()
    data = root / "data"
    observers = root / "observers"
    observers.mkdir()
    data.mkdir()
    (data / "hub.json").write_text(json.dumps({
        "agent_watch": {"roots": {"codex": [str(observers)]}},
    }), encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    cfg = HubConfig.load(env={
        "HOARD_HUB_PORT": str(port), "HOARD_HUB_DATA_DIR": str(data),
        "HOARD_HUB_ROOTS": str(roots),
    })
    assert cfg.isolated
    hub = Hub(cfg)
    # The passive Hub omits process-capable facets. Attach just the console
    # with all provider/process observations doubled before construction.
    console_type = AgoraConsoleFacet
    monkeypatch.setattr(console_type, "_reconcile_workers", lambda self: None)
    monkeypatch.setattr(console_type, "_start_queued_leaders", lambda self: None)
    monkeypatch.setattr(console_type, "_apply_pending_faustus_preference", lambda self: None)
    monkeypatch.setattr(console_type, "_provider_data", lambda self, force=False: {
        leader: {"catalog": []} for leader, *_ in LEADERS})
    monkeypatch.setattr(console_type, "_faustus_current_settings", lambda self: {})
    monkeypatch.setattr(console_type, "leader_states", lambda self, **kwargs: {"states": {}})
    monkeypatch.setattr("hoard_link.hub.agora_console.codex_observation", lambda: {"ok": True})
    console = AgoraConsoleFacet(hub)
    agora = hub.facet("agora").agora
    agora.db.execute("INSERT INTO threads(id,title,kind,created_by,status,created,updated,space_id) "
                     "VALUES(?, 'Console', 'handoff', 'tester', 'open', 0, 0, 'ecosystem')", (console.thread_id,))
    hub.facets.insert(0, console)
    hub._facets_by_id[console.id] = console
    app = App("lookup", "Lookup", "", str(roots), "http://127.0.0.1:9", "/api/health", None)
    hub._apps = {app.id: app}
    try:
        yield hub, console
    finally:
        hub.close()


@contextmanager
def _serve(hub):
    server, thread = serve_in_thread(hub)
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_agora_pool_bounds_pending_calls_and_cancels_them_on_close():
    from hoard_link.hub.agora_http import AgoraBusy, AgoraWorkPool

    pool = AgoraWorkPool("test", workers=1, capacity=2)
    entered = threading.Event()
    release = threading.Event()

    def blocking():
        assert threading.current_thread().daemon
        entered.set()
        return release.wait(5)

    running = pool.submit(blocking)
    try:
        assert entered.wait(2)
        queued = pool.submit(lambda: pytest.fail("queued work ran on shutdown"))
        with pytest.raises(AgoraBusy):
            pool.submit(lambda: None)
        pool.close()
        assert queued.cancelled()
        with pytest.raises(AgoraBusy):
            pool.submit(lambda: None)
    finally:
        pool.close()
        release.set()
    assert running.result(timeout=2)


def test_health_and_directory_under_agora_saturation(tmp_path, monkeypatch):
    with _private_hub(tmp_path, monkeypatch) as (hub, console):
        agora = hub.facet("agora").agora
        agora.heartbeat({"agent": "seed", "doing": "seed"})
        real_heartbeat = agora.heartbeat
        real_overview = console.overview
        probe_started = threading.Event()
        work_saturated = threading.Event()
        release = threading.Event()
        count_lock = threading.Lock()
        entered = {"workers": 0, "leads": 0}
        original_snapshot = hub._snapshot_now

        def block(kind):
            with count_lock:
                entered[kind] += 1
                if entered["workers"] >= 8 and entered["leads"] == 3:
                    work_saturated.set()
            assert release.wait(10)

        def heartbeat(args):
            assert threading.current_thread().name.startswith("hoard-agora-request")
            block("workers")
            return real_heartbeat(args)

        def overview(*args, **kwargs):
            assert threading.current_thread().name.startswith("hoard-agora-overview")
            block("leads")
            return real_overview(*args, **kwargs)

        def slow_probe():
            probe_started.set()
            assert release.wait(10)
            sample = original_snapshot()
            sample["apps"][0]["state"] = "running"
            return sample

        monkeypatch.setattr(agora, "heartbeat", heartbeat)
        monkeypatch.setattr(console, "overview", overview)
        monkeypatch.setattr(hub, "_snapshot_now", slow_probe)
        with _serve(hub) as port, ThreadPoolExecutor(max_workers=23) as clients:
            pending = [
                clients.submit(_http, port, "/api/agora/heartbeat", body={"agent": f"worker-{i}"}, token=hub.token)
                for i in range(20)
            ]
            pending.extend(clients.submit(_http, port, "/api/agora/console/overview", token=hub.token)
                           for _ in range(3))
            try:
                assert probe_started.wait(5)
                assert work_saturated.wait(5), entered
                routes = ("/api/health", "/api/apps", "/api/apps/lookup", "/api/apps/lookup/status")
                timings = {path: [] for path in routes}
                # Simulate unrelated Hub work holding its catalog writer lock.
                with hub._lock:
                    for _ in range(8):
                        for path in routes:
                            started = time.perf_counter()
                            status, body, _ = _http(port, path)
                            timings[path].append(1000 * (time.perf_counter() - started))
                            assert status == 200
                            if path == "/api/apps":
                                assert body["apps"][0]["url"] == "http://127.0.0.1:9"
                            elif path.endswith(("lookup", "status")):
                                assert body["id"] == "lookup"
                                assert body["status_checked_at"] is None
                maxima = {path: round(max(samples), 2) for path, samples in timings.items()}
                print("Loopback per-route maximum (ms) while saturated:", maxima)
                assert max(maxima.values()) < 50, timings
                # While all three legacy overview slots are busy, the incremental
                # route can still accept a request and give its cursor/pending state.
                status, delta, _ = _http(port, "/api/agora/console/overview?view=live", token=hub.token)
                assert status == 200 and delta["pending"] and delta["revision"] is None
            finally:
                release.set()
            assert [future.result()[0] for future in pending] == [200] * 23
            assert entered == {"workers": 20, "leads": 3}

            def sampled_status():
                row = _http(port, "/api/apps/lookup/status")[1]
                return row if row["status_checked_at"] is not None else None

            assert _wait_until(sampled_status)["state"] == "running"
            release.clear()
            try:
                hub.invalidate_snapshot()
                old = _http(port, "/api/apps/lookup")[1]
                assert old["state"] == "running" and old["status_stale"]
                hub.get("lookup").url = "http://127.0.0.1:10"
                changed = _http(port, "/api/apps/lookup")[1]
                assert changed["url"] == "http://127.0.0.1:10"
                assert changed["status_checked_at"] is None
            finally:
                release.set()


def test_incremental_console_history_and_delta(tmp_path, monkeypatch):
    with _private_hub(tmp_path, monkeypatch) as (hub, console), _serve(hub) as port:
        with console._db() as db:
            for n in range(40):
                db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,result,"
                           "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (str(n), "codex", str(n), "", "test", "", "completed", "a" * 24000, n, n))
        legacy_status, legacy, legacy_bytes = _http(port, "/api/agora/console/overview", token=hub.token)
        assert legacy_status == 200 and len(legacy["console_turns"]) == 40 and legacy_bytes > 960_000
        assert _http(port, "/api/agora/console/overview?view=live")[0] == 401

        def read(query):
            status, payload, size = _http(port, "/api/agora/console/overview?" + query, token=hub.token)
            assert status == 200
            return payload, size

        def version(query, previous=None):
            current, size = read(query)
            if current["revision"] and current["revision"] != previous:
                return current, size

        initial, live_bytes = _wait_until(lambda: version("view=live"))
        assert initial["reset"] and initial["leaders"] and "console_turns" not in initial
        assert live_bytes < legacy_bytes / 10
        cursor = initial["revision"]
        unchanged, unchanged_bytes = read(f"view=live&since={cursor}")
        assert unchanged["revision"] == cursor and "workers" not in unchanged and unchanged_bytes < 256
        arguments = {"view": "live", "since": cursor}
        for path, body in (
            ("/api/agent/tools/hub_agora_console_overview", arguments),
            ("/api/agent/call", {"tool": "hub_agora_console_overview", "arguments": arguments}),
            ("/api/apps/hub/call", {"tool": "hub_agora_console_overview", "arguments": arguments}),
        ):
            status, result, _ = _http(port, path, body=body, token=hub.token)
            assert status == 200 and result["result"]["revision"] == cursor
            assert "workers" not in result["result"]
        with console._db() as db:
            db.execute("INSERT INTO leader_colors VALUES('codex', '#123456', 'tester', 0)")
        # The first request queues a refresh. Its immediate data may still be the old snapshot.
        read(f"view=live&since={cursor}&refresh=1")
        changed, _ = _wait_until(lambda: version(f"view=live&since={cursor}", cursor))
        assert not changed["reset"] and "workers" not in changed
        assert next(leader for leader in changed["leaders"] if leader["id"] == "codex")["color"] == "#123456"
        reset, _ = read("view=live&since=lost")
        assert reset["reset"] and reset["workers"] == []
        full, _ = _wait_until(lambda: version("view=full"))
        assert full["console_turns"] == legacy["console_turns"]
        print("Console bytes legacy/live/unchanged:", legacy_bytes, live_bytes, unchanged_bytes)


def test_task_read_does_not_wait_for_transcripts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    with _private_hub(tmp_path, monkeypatch) as (hub, _), _serve(hub) as port:
        facet = hub.facet("agora")
        task = facet.agora.task_add({"agent": "planner", "title": "Synthetic task"})["task"]
        release = threading.Event()
        scan_started = threading.Event()

        def read_logs():
            scan_started.set()
            assert release.wait(5)
            return facet._empty_watch()

        monkeypatch.setattr(facet, "_watch", SimpleNamespace(view=read_logs))
        facet.start()
        try:
            path = f"/api/agora/tasks/{task['id']}"
            status, body, _ = _http(port, path)
            assert status == 200 and body["task"]["id"] == task["id"]
            assert scan_started.wait(5)
            with ThreadPoolExecutor(max_workers=1) as clients:
                future = clients.submit(_http, port, path)
                try:
                    assert future.result(timeout=1)[0] == 200
                finally:
                    release.set()
        finally:
            release.set()
