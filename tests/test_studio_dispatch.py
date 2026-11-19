import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import urlopen

from hoard_link.hub import agora_auto_review, agora_capacity, agora_capacity_accounts
from hoard_link.hub import agora_worker_profiles, dispatch_proxy
from hoard_link.hub.core import Hub
from hoard_link.hub.instances import InstancesFacet
from hoard_link.hub.server import _HubHandler, _client_disconnected, serve_in_thread


def test_codex_account_primary_is_measured_even_when_only_extras_in_scoped_map():
    report = agora_capacity.build_usage_report({"id": "fixture-account"}, {
        "rateLimitsByLimitId": {"extra": {"limitId": "extra"}},
        "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 8, "resetsAt": 2000}},
    }, now=1000)
    windows = {row["window_id"]: row for row in report["windows"]}
    assert windows["codex.primary"]["remaining_percent"] == 92
    assert windows["codex.secondary"]["measurement"] == "unknown"


def test_claude_session_diagnostic_uses_no_model_and_tells_explicit_logout(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(agora_capacity_accounts.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(agora_capacity_accounts.subprocess, "run",
                        lambda argv, **_: commands.append(argv) or SimpleNamespace(
                            returncode=1, stdout='{"loggedIn":false}'))
    assert agora_capacity_accounts.probe_claude_sign_in(tmp_path) == "logged_out"
    assert commands == [["claude", "auth", "status"]]


def test_auto_review_ignores_interactive_preference_and_uses_economical_other_provider(monkeypatch):
    def quota(provider, remaining):
        return {"provider_id": provider, "pools": [{"windows": [{"measurement": "measured",
                                                                 "remaining_percent": remaining}]}]}

    console = SimpleNamespace(
        _agora=lambda: SimpleNamespace(usage=lambda: {"providers": [quota("claude", 10), quota("codex", 30)]}),
        _provider_data=lambda: {
            "codex": {"state": "available", "current_model": "gpt-premium-astra",
                      "catalog": [{"id": "gpt-premium-astra", "efforts": ["high"]}]},
            "claude": {"state": "available", "catalog": [{"id": "haiku", "efforts": ["high", "low"]}]},
        },
    )
    monkeypatch.setattr(agora_worker_profiles, "read", lambda _: {})
    monkeypatch.setattr(agora_auto_review, "codex_observation", lambda: {})
    assert agora_auto_review._choose(console, ["codex", "claude"], review=True) == ("claude", "haiku", "low")
    assert agora_auto_review._choose(console, ["codex"], review=True) is None


def test_proxy_reports_a_slow_catalogue_and_a_refused_connection(tmp_path):
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.05)
            body = b'{"tools": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    stopper = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    stopper.start()
    app = SimpleNamespace(id="lag", token_file=str(tmp_path / "token"),
                          url=f"http://127.0.0.1:{server.server_port}")
    try:
        result = dispatch_proxy.probe_catalog(app, slow_s=0.01, timeout_s=0.25)
        assert result["reachable"] and result["state"] == "slow"
    finally:
        server.shutdown()
        server.server_close()
        stopper.join()
    result = dispatch_proxy.probe_catalog(app, slow_s=0.01, timeout_s=2.0)
    assert result["state"] == "unreachable" and not result["reachable"]


def test_json_cancelled_poll_does_not_attempt_a_500(caplog):
    class ClosedSocket:
        def write(self, data):
            raise ConnectionAbortedError(10053, "browser disconnected")

    request = object.__new__(_HubHandler)
    request.close_connection = False
    request.command = "GET"
    request.wfile = ClosedSocket()
    headers = []
    request.send_response = headers.append
    request.send_header = lambda *_: None
    request.end_headers = lambda: None
    with caplog.at_level(logging.ERROR):
        request._json({"services": []})
    assert request.close_connection and headers == [200] and not caplog.records
    assert not _client_disconnected(FileNotFoundError())


def test_abandoned_keepalive_read_closes_connection():
    class ClosedSocket:
        def readline(self, size):
            raise ConnectionResetError(10054, "client closed the connection")

    request = object.__new__(_HubHandler)
    request.close_connection = False
    request.rfile = ClosedSocket()
    request.handle_one_request()
    assert request.close_connection


def test_app_status_alias_and_native_shelf_proxy(tmp_path):
    from hoard_link.hub.config import HubConfig

    roots = tmp_path / "apps"
    root = roots / "testapp"
    root.mkdir(parents=True)
    (root / "faustus-plugin.json").write_text(json.dumps({
        "id": "testapp", "name": "Test app", "app": {"url_default": "http://127.0.0.1:1"}
    }), encoding="utf-8")
    cfg = HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path / "hub"), "HOARD_HUB_ROOTS": str(roots)})
    hub = Hub(cfg)
    srv, thread = serve_in_thread(hub, port=0)
    try:
        base = f"http://127.0.0.1:{srv.server_port}/api/apps/testapp"
        with urlopen(base) as original, urlopen(base + "/status") as alias:
            assert json.load(original)["state"] == json.load(alias)["state"]
        found = hub.call_app("testapp", "skills_list")
        assert found["ok"] and found["contract"] == "shelf" and "skills" in found["result"]
        missing = hub.call_app("testapp", "skill_read", {"slug": "missing"})
        assert not missing["ok"] and missing["status"] == 404
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
        hub.close()


def test_isolated_instances_create_own_data_use_own_token_and_undo(monkeypatch, tmp_path):
    from hoard_link.hub import isolated_apps

    original = tmp_path / "main"
    worktree = tmp_path / "worktree"
    data = tmp_path / "hub"
    for path in (original, worktree, data):
        path.mkdir()
    (worktree / ".git").write_text("gitdir: test", encoding="utf-8")
    (worktree / "faustus-plugin.json").write_text(json.dumps({
        "id": "sample", "name": "Sample", "app": {"url_default": "http://127.0.0.1:12345",
            "launch_hint": {"kind": "process", "executable": "python", "argv": ["-V"], "cwd": "{SAMPLE_DIR}"}},
        "x-family": {"isolated_instance": {"data_env": "SAMPLE_DATA", "port_env": "SAMPLE_PORT"}},
    }), encoding="utf-8")
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(data), faustus_dir=None, url="http://127.0.0.1:8888"),
                          get=lambda _: SimpleNamespace(folder=str(original), token_file=str(original / "mcp-token")))
    children = []

    def launch(argv, *, env, **kwargs):
        from pathlib import Path
        folder = Path(env["SAMPLE_DATA"])
        (folder / "mcp-token").write_text("secondary-task-token", encoding="utf-8")
        child = SimpleNamespace(running=True, poll=lambda: None if child.running else 0)
        children.append(child)
        return child

    def stop(child):
        child.running = False
        return True

    monkeypatch.setattr(isolated_apps.proc, "popen", launch)
    monkeypatch.setattr(isolated_apps.proc, "kill_tree", stop)
    facet = InstancesFacet(hub)
    result = facet.isolated.manage({"action": "start", "app": "sample", "worktree": str(worktree)})
    assert result["ok"] and result["instance"]["state"] == "running"
    assert facet.owner("secondary-task-token") == "sample" and not list(original.iterdir())
    ended = facet.isolated.manage(result["undo"])
    assert ended["instance"]["state"] == "stopped" and facet.owner("secondary-task-token") is None
    assert facet.isolated.manage(ended["undo"])["instance"]["state"] == "running"
    facet.close()
    assert all(not child.running for child in children)
