"""Agent command-line contract and old-Hub compatibility."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hoard_link.hub import cli

from ._hub_fakes import FakeApp, make_hub, serve
from .test_hub_and_server import _http, _observed_app


@pytest.fixture
def served(tmp_path):
    with ExitStack() as cleanup:
        app = FakeApp("fake", handlers={"echo": lambda arguments: arguments})
        cleanup.callback(app.stop)
        hub = make_hub(tmp_path, [app])
        cleanup.callback(hub.close)
        server = serve(hub)
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        yield hub, hub.config.url, app


def test_autodiscovery_status_tool_and_arguments_file(served, monkeypatch, tmp_path, capsys):
    hub, url, _ = served
    for option in ("HOARD_HUB_URL", "HOARD_HUB_TOKEN_FILE"):
        monkeypatch.delenv(option, raising=False)
    monkeypatch.setenv("HOARD_HUB_DATA_DIR", hub.config.data_dir)
    hub.write_url_file()

    # The directory serves a provisional state until its background probe completes.
    status, observed = _observed_app(url, "/api/apps/fake/status")
    assert status == 200 and observed["state"] == "running"
    assert cli.main(["--json", "state", "fake"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "running"

    arguments = tmp_path / "args with spaces.json"
    arguments.write_text('{"app":"fake","log_lines":2}', encoding="utf-8-sig")
    assert cli.main(["tool", "hub_app_status", "--args-file", str(arguments), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["id"] == "fake"

    assert cli.main(["chain", "brief", "3mf"]) == 0
    assert "No chain" in capsys.readouterr().out
    assert cli.main(["state", "absent", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == 404


def test_bad_argument_file_is_reported_before_contacting_hub(tmp_path, monkeypatch, capsys):
    args_file = tmp_path / "bad.json"
    args_file.write_text("[]", encoding="utf-8")

    def unexpected_client():
        raise AssertionError("invalid arguments must not trigger a request")

    monkeypatch.setattr(cli, "Client", unexpected_client)
    assert cli.main(["call", "fake", "write", "--args-file", str(args_file), "--json"]) == 1
    assert "JSON object" in json.loads(capsys.readouterr().out)["error"]


def test_legacy_route_fallback_never_retries_a_call_after_timeout(monkeypatch):
    client = cli.Client(hub_url="http://127.0.0.1:1")
    calls = []

    def old_hub(path, body, *, timeout):
        calls.append((path, body, timeout))
        if path.endswith("hub_start_app"):
            return 404, {"ok": False, "error": "not found"}
        return 200, {"ok": True}

    monkeypatch.setattr(client, "request", old_hub)
    assert client.tool("hub_start_app", {"app": "fake"}) == (200, {"ok": True})
    assert calls[-1][:2] == ("/api/agent/call", {"tool": "hub_start_app", "arguments": {"app": "fake"}})

    def timeout(path, body, **kwargs):
        assert path.endswith("hub_start_app")
        return None, {"ok": False}

    monkeypatch.setattr(client, "request", timeout)
    assert client.tool("hub_start_app", {"app": "fake"}) == (None, {"ok": False})


def test_isolated_target_ignores_inherited_connection(served, monkeypatch, tmp_path, capsys):
    hub, url, _ = served
    hub.write_url_file()
    inherited_token = tmp_path / "unrelated-token"
    inherited_token.write_text("unrelated", encoding="utf-8")
    monkeypatch.setenv("HOARD_HUB_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", str(inherited_token))
    monkeypatch.setenv("HOARD_HUB_DATA_DIR", hub.config.data_dir)
    tool = ["tool", "hub_list_apps", "--json"]

    assert cli.main(tool) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)["ok"] and result.err.strip() == f"Hub: {url}"

    monkeypatch.setenv("HOARD_HUB_DATA_DIR", str(tmp_path))
    assert cli.main(tool) == 1
    result = capsys.readouterr()
    assert "No Hub URL" in json.loads(result.out)["error"] and not result.err

    assert cli.main(["--data-dir", hub.config.data_dir, *tool]) == 0
    assert capsys.readouterr().err.strip() == f"Hub: {url}"
    assert cli.main([*tool, "--hub-url", url, "--token-file", hub.config.token_file]) == 0
    assert capsys.readouterr().err.strip() == f"Hub: {url}"
    assert not cli.Client(hub_url=url).token


@pytest.mark.parametrize("command, fallback", [
    (["call", "fake", "echo"], "hub"),
    (["tool", "hub_call_app"], "hub-tool"),
])
def test_cli_forwards_agent_identity(served, monkeypatch, tmp_path, capsys, command, fallback):
    hub, _, app = served
    hub.write_url_file()
    arguments = {"text": "unchanged"}
    args_file = tmp_path / "args.json"
    payload = arguments if fallback == "hub" else {"app": "fake", "tool": "echo", "arguments": arguments}
    args_file.write_text(json.dumps(payload), encoding="utf-8")
    cli_args = [*command, "--data-dir", hub.config.data_dir, "--args-file", str(args_file), "--json"]

    monkeypatch.setenv("HOARD_AGENT_ID", "  worker-1  ")
    monkeypatch.setenv("HOARD_AGENT_SESSION", "run-42")
    assert cli.main(cli_args) == 0, capsys.readouterr()
    assert app.calls[-1]["agent"] == "worker-1"
    assert app.calls[-1]["session"] == "run-42"
    assert app.calls[-1]["arguments"] == arguments

    monkeypatch.delenv("HOARD_AGENT_ID")
    monkeypatch.delenv("HOARD_AGENT_SESSION")
    assert cli.main(cli_args) == 0, capsys.readouterr()
    assert app.calls[-1]["agent"] == fallback
    assert "session" not in app.calls[-1]


def test_shared_route_forwards_caller_and_session(served):
    hub, url, app = served
    headers = {"Authorization": "Bearer " + hub.token}
    args = {"app": "fake", "tool": "echo", "arguments": {"text": "unchanged"}, "reason": "Test the proxy"}
    payload = {"name": "hub_call_app", "arguments": args, "caller": "worker-β", "session": "run-雪"}
    status, reply = _http(url + "/api/agent/call", payload, headers)
    assert status == 200 and reply["result"]["result"] == args["arguments"]
    assert app.calls[-1]["agent"] == payload["caller"]
    assert app.calls[-1]["session"] == payload["session"]
    assert app.calls[-1]["reason"] == args["reason"]

    headers.update({"X-Agent-Id": "header-worker", "X-Agent-Session": "header-run"})
    payload.update(agent="body-worker", _session="body-run")
    assert _http(url + "/api/agent/call", payload, headers)[0] == 200
    assert app.calls[-1]["agent"] == "header-worker"
    assert app.calls[-1]["session"] == "header-run"


@pytest.mark.skipif(os.name != "nt", reason="The batch launcher only runs on Windows")
def test_batch_launcher_preserves_failure_exit_code(served, monkeypatch, tmp_path):
    hub, _, _ = served
    for option in ("HOARD_HUB_URL", "HOARD_HUB_TOKEN_FILE"):
        monkeypatch.delenv(option, raising=False)
    monkeypatch.setenv("HOARD_HUB_DATA_DIR", hub.config.data_dir)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    hub.write_url_file()
    launcher = Path(__file__).resolve().parents[2] / "hub.cmd"
    invocation = subprocess.list2cmdline([str(launcher), "state", "absent", "--json"])
    completed = subprocess.run(f'cmd.exe /d /s /c "{invocation}"', cwd=tmp_path, capture_output=True, timeout=20)
    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["status"] == 404
    assert completed.stderr.decode("utf-8").strip() == f"Hub: {hub.config.url}"
