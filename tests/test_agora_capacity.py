from __future__ import annotations

import json
import queue
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from hoard_link.hub import agora_capacity
from hoard_link.hub.agora import Agora


def test_rate_limit_windows_are_independent_unknown_and_expire_at_reset():
    account = {"type": "chatgpt", "email": "private@example.invalid"}
    report = agora_capacity.build_usage_report(account, {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 70, "windowDurationMins": 10080, "resetsAt": 2000},
                "secondary": None,
            },
            "codex_other": {
                "limitId": "codex_other",
                "primary": {"usedPercent": 12.5, "windowDurationMins": 60, "resetsAt": 500},
                "secondary": {"usedPercent": None, "windowDurationMins": 30, "resetsAt": 3000},
            },
        }
    }, now=1000)

    windows = {item["window_id"]: item for item in report["windows"]}
    assert len(windows) == 4
    assert windows["codex.primary"]["measurement"] == "measured"
    assert windows["codex.primary"]["used_percent"] == 70
    assert windows["codex.primary"]["remaining_percent"] == 30
    assert windows["codex.primary"]["reset_at"] == datetime.fromtimestamp(2000, timezone.utc).isoformat()
    assert windows["codex.secondary"]["measurement"] == "unknown"
    assert windows["codex.secondary"]["used_percent"] is None
    assert windows["codex_other.primary"]["measurement"] == "unknown"  # expired before 24h
    assert windows["codex_other.primary"]["remaining_percent"] is None
    assert windows["codex_other.primary"]["reset_at"] is None
    assert windows["codex_other.secondary"]["measurement"] == "unknown"
    assert windows["codex_other.secondary"]["reset_at"] == datetime.fromtimestamp(3000, timezone.utc).isoformat()
    assert all(item["source"] == agora_capacity.SOURCE for item in windows.values())
    assert "private@example.invalid" not in json.dumps(report)
    assert report["pool_id"].startswith("codex-")


def test_pool_hash_is_stable_but_separates_accounts_without_exporting_identity():
    first = agora_capacity.build_usage_report(
        {"type": "chatgpt", "email": "  Account@Example.invalid "},
        {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20, "resetsAt": 5000}}}, now=1000)
    same = agora_capacity.build_usage_report(
        {"type": "chatgpt", "email": "account@example.invalid"},
        {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20, "resetsAt": 5000}}}, now=1000)
    other = agora_capacity.build_usage_report(
        {"type": "chatgpt", "email": "other@example.invalid"},
        {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20, "resetsAt": 5000}}}, now=1000)

    assert first["pool_id"] == same["pool_id"]
    assert first["pool_id"] != other["pool_id"]
    assert first["pool_label"] == "Codex account"
    assert "account@example.invalid" not in json.dumps(first)
    assert "other@example.invalid" not in json.dumps(other)


def test_stdio_reader_uses_only_read_methods_and_returns_sanitized_dto():
    requests = []
    output: "queue.Queue[str | None]" = queue.Queue()

    class FakeStdout:
        def __iter__(self):
            while True:
                line = output.get()
                if line is None:
                    return
                yield line

    class FakeStdin:
        def write(self, text):
            for line in text.splitlines():
                request = json.loads(line)
                requests.append(request)
                if "id" not in request:
                    continue
                if request["id"] == 0:
                    result = {"userAgent": "Codex"}
                elif request["id"] == 1:
                    result = {"account": {"type": "chatgpt", "email": "private@example.invalid"}}
                else:
                    result = {"rateLimitsByLimitId": {"codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 70, "windowDurationMins": 10080, "resetsAt": 5000},
                        "secondary": None,
                    }}}
                output.put(json.dumps({"id": request["id"], "result": result}) + "\n")
            return len(text)

        def flush(self):
            pass

        def close(self):
            output.put(None)

    process = SimpleNamespace(stdin=FakeStdin(), stdout=FakeStdout(), returncode=0)
    process.wait = lambda timeout=None: 0

    def popen(args, **kwargs):
        assert args == ["codex.exe", "app-server", "--stdio"]
        assert kwargs["stderr"] is agora_capacity.subprocess.DEVNULL
        return process

    report = agora_capacity.collect_codex_usage_report(executable="codex.exe", now=1000, popen=popen)
    methods = [request["method"] for request in requests]
    assert methods == ["initialize", "initialized", "account/read", "account/rateLimits/read"]
    account_read = requests[2]
    assert account_read["params"] == {"refreshToken": False}
    assert report["windows"][0]["used_percent"] == 70
    assert "private@example.invalid" not in json.dumps(report)
    assert not any("login" in method or "logout" in method or "consume" in method for method in methods)


def test_report_uses_existing_cli_identity_and_never_passes_raw_identity(tmp_path):
    report = agora_capacity.build_usage_report(
        {"type": "chatgpt", "email": "private@example.invalid"},
        {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 25, "resetsAt": 5000}}}, now=1000)
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["body"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "reported_at": "2026-10-10T00:00:00+00:00"}))

    result = agora_capacity._report_via_agora_cli(
        report, repo_root=Path(__file__).resolve().parents[1], runner=runner)
    assert captured["command"][captured["command"].index("--as") + 1] == "codex-capacity"
    assert "private@example.invalid" not in captured["body"]
    assert result == {"ok": True, "provider_id": "codex", "window_count": 2,
                      "reported_at": "2026-10-10T00:00:00+00:00"}


def test_usage_store_expires_only_the_reset_window(tmp_path):
    now = 1_792_162_466.0
    store = Agora(tmp_path / "agora.db", clock=lambda: now)
    store.usage_report({
        "agent": "codex-capacity",
        "provider_id": "codex",
        "pool_id": "codex-test-pool",
        "pool_label": "Codex account",
        "source": agora_capacity.SOURCE,
        "windows": [
            {"window_id": "weekly.primary", "label": "Weekly", "measurement": "measured",
             "used_percent": 70, "remaining_percent": 30, "reset_at": "2026-10-09T00:00:00Z"},
            {"window_id": "weekly.secondary", "label": "Short", "measurement": "measured",
             "used_percent": 10, "remaining_percent": 90, "reset_at": "2026-10-20T00:00:00Z"},
        ],
    })

    result = store.usage()
    codex = next(item for item in result["providers"] if item["provider_id"] == "codex")
    windows = {item["window_id"]: item for item in codex["pools"][0]["windows"]}
    assert windows["weekly.primary"]["measurement"] == "unknown"
    assert windows["weekly.primary"]["used_percent"] is None
    assert windows["weekly.primary"]["remaining_percent"] is None
    assert windows["weekly.primary"]["reset_at"] is None
    assert windows["weekly.primary"]["stale"] is True
    assert windows["weekly.secondary"]["measurement"] == "measured"
    assert windows["weekly.secondary"]["remaining_percent"] == 90
    assert windows["weekly.secondary"]["stale"] is False
    store.close()
