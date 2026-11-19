"""Agent command-line contract and old-Hub compatibility."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hoard_link.hub import cli

from .test_hub_and_server import served  # noqa: F401


def test_autodiscovery_status_tool_and_arguments_file(served, monkeypatch, tmp_path, capsys):
    hub, url = served
    for option in ("HOARD_HUB_URL", "HOARD_HUB_TOKEN_FILE"):
        monkeypatch.delenv(option, raising=False)
    monkeypatch.setenv("HOARD_HUB_DATA_DIR", hub.config.data_dir)
    hub.write_url_file()

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
    client = cli.Client()
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


@pytest.mark.skipif(os.name != "nt", reason="The batch launcher only runs on Windows")
def test_batch_launcher_preserves_failure_exit_code(served, monkeypatch, tmp_path):
    hub, _ = served
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
