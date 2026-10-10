from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hoard_link.hub.agora import Agora, AgoraFacet
from hoard_link.hub.facets import Request


CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agora.py"
CLI_SPEC = importlib.util.spec_from_file_location("agora_cli_under_test", CLI_PATH)
CLI = importlib.util.module_from_spec(CLI_SPEC)
assert CLI_SPEC and CLI_SPEC.loader
CLI_SPEC.loader.exec_module(CLI)


NOW = 1_792_162_466.0


def _report(agent: str, pool: str, *, provider: str = "codex", windows=None):
    return {
        "agent": agent,
        "provider_id": provider,
        "pool_id": pool,
        "pool_label": pool,
        "source": "native_api",
        "windows": windows or [
            {"window_id": "weekly", "label": "Weekly", "measurement": "measured",
             "used_percent": 65, "remaining_percent": 35,
             "reset_at": "2033-01-01T00:00:00Z"},
            {"window_id": "daily", "label": "Daily", "measurement": "unknown",
             "used_percent": None, "remaining_percent": None, "reset_at": None},
        ],
    }


def test_usage_groups_agents_by_pool_and_keeps_distinct_windows(tmp_path):
    clock = [NOW]
    store = Agora(tmp_path / "agora.db", clock=lambda: clock[0])
    store.usage_report(_report("codex-bucle", "pool-a"))
    store.usage_report(_report("codex-cap", "pool-a"))
    store.usage_report(_report("codex-other", "pool-b"))

    response = store.usage()
    assert {item["provider_id"] for item in response["providers"]} == {"claude", "codex", "cursor", "faustus"}
    codex = next(item for item in response["providers"] if item["provider_id"] == "codex")
    assert len(codex["pools"]) == 2
    shared = next(item for item in codex["pools"] if item["pool_id"] == "pool-a")
    assert shared["agent_ids"] == ["codex-bucle", "codex-cap"]
    assert len(shared["windows"]) == 2
    weekly, daily = shared["windows"]
    assert (weekly["used_percent"], weekly["remaining_percent"], weekly["measurement"]) == (65.0, 35.0, "measured")
    assert (daily["used_percent"], daily["remaining_percent"], daily["measurement"]) == (None, None, "unknown")
    assert shared["stale"] is False
    store.close()


def test_stale_snapshot_is_unknown_not_zero_and_survives_reopen(tmp_path):
    clock = [NOW]
    path = tmp_path / "agora.db"
    store = Agora(path, clock=lambda: clock[0])
    store.usage_report(_report("claude", "shared-claude", provider="claude"))
    store.close()

    clock[0] += 24 * 3600 + 1
    store = Agora(path, clock=lambda: clock[0])
    result = store.usage()
    claude = next(item for item in result["providers"] if item["provider_id"] == "claude")
    pool = claude["pools"][0]
    assert pool["stale"] is True
    assert all(window["measurement"] == "unknown" for window in pool["windows"])
    assert all(window["used_percent"] is None and window["remaining_percent"] is None for window in pool["windows"])
    assert all(window["reset_at"] is None for window in pool["windows"])
    store.close()


def test_usage_rejects_raw_account_identifiers_and_fabricated_unknown_percentages(tmp_path):
    store = Agora(tmp_path / "agora.db", clock=lambda: NOW)
    with pytest.raises(ValueError, match="opaque label"):
        store.usage_report(_report("codex-cap", "user@example.com"))
    with pytest.raises(ValueError, match="must not include percentages"):
        store.usage_report(_report("faustus", "local-glm", provider="faustus", windows=[
            {"window_id": "subscription", "measurement": "unknown", "used_percent": 0, "remaining_percent": 100}
        ]))
    store.close()


def test_usage_read_and_report_are_available_through_http_and_mcp(tmp_path):
    store = Agora(tmp_path / "agora.db", clock=lambda: NOW)
    facet = AgoraFacet.__new__(AgoraFacet)
    facet.agora = store
    get = Request("GET", "/api/agora/console/usage", {}, {}, caller=lambda: "ui", agent=lambda: False)
    assert facet.get(get)["ok"] is True

    body = _report("codex-cap", "pool-http")
    post = Request("POST", "/api/agora/usage/report", {}, body, caller=lambda: "hub", agent=lambda: True)
    assert facet.post(post)["ok"] is True
    handlers = facet.handlers()
    assert handlers["hub_agora_usage"]({})["ok"] is True
    assert handlers["hub_agora_usage_report"](_report("codex-helper", "pool-mcp"))["ok"] is True
    names = {tool["name"] for tool in facet.tools()}
    assert {"hub_agora_usage", "hub_agora_usage_report"} <= names
    store.close()


def test_cli_usage_commands_use_normal_http_routes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(CLI, "call", lambda method, path, body=None: calls.append((method, path, body)) or {"ok": True})
    data = tmp_path / "quota.json"
    data.write_text('{"provider_id":"codex","pool_id":"pool-cli","pool_label":"shared","source":"native_api","windows":[]}',
                    encoding="utf-8")
    assert CLI.main(["--as", "codex-cap", "--json", "usage-report", "--data-file", str(data)]) == 0
    assert calls[-1][0:2] == ("POST", "/api/agora/usage/report")
    assert calls[-1][2]["agent"] == "codex-cap"
    assert CLI.main(["--json", "usage"]) == 0
    assert calls[-1][0:2] == ("GET", "/api/agora/console/usage")
