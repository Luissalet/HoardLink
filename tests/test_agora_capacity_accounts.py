"""Claude and Cursor quota readers for the Agora (agora_capacity_accounts). No network, no real accounts:
fake credential files, a fake Cursor state database and a fake HTTP opener."""
from __future__ import annotations

import base64
import io
import json
import sqlite3
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard_link.hub import agora_capacity, agora_capacity_accounts as acc
from hoard_link.hub.agora import Agora

NOW = 1_791_634_000.0  # 2026-10-10
CLAUDE_TOKEN = "sk-ant-oat01-secret-token"


def _claude_files(tmp_path: Path, *, expires_ms: float, scopes=("user:inference", "user:profile")) -> tuple[Path, Path]:
    config = tmp_path / ".claude"
    config.mkdir()
    (config / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": CLAUDE_TOKEN, "refreshToken": "sk-ant-ort01-refresh", "expiresAt": expires_ms,
        "scopes": list(scopes), "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}), encoding="utf-8")
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": "11111111-2222-3333-4444-555555555555", "emailAddress": "luis@example.invalid"}}), encoding="utf-8")
    return config, tmp_path


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(payload, seen: list, status: int = 200):
    def open_(request, timeout):
        seen.append({"url": request.full_url, "headers": dict(request.header_items())})
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(b"{}"))
        return _Response(json.dumps(payload).encode())
    return open_


CLAUDE_USAGE = {
    "five_hour": {"utilization": 37.0, "resets_at": "2026-10-10T16:00:00+00:00"},
    "seven_day": {"utilization": 62.5, "resets_at": "2026-10-14T08:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 12, "resets_at": "2026-10-14T08:00:00+00:00"},
    "extra_usage": {"is_enabled": False},
}


def test_claude_reads_session_and_weekly_windows_without_leaking_identity(tmp_path):
    config, home = _claude_files(tmp_path, expires_ms=(NOW + 3600) * 1000)
    seen: list = []
    report = acc.collect_claude_usage_report(config_dir=config, home=home, now=NOW, opener=_opener(CLAUDE_USAGE, seen))
    assert seen[0]["url"] == acc.CLAUDE_USAGE_URL
    headers = {k.lower(): v for k, v in seen[0]["headers"].items()}
    assert headers["authorization"] == f"Bearer {CLAUDE_TOKEN}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    windows = {w["window_id"]: w for w in report["windows"]}
    assert windows["claude.session"]["used_percent"] == 37.0 and windows["claude.session"]["remaining_percent"] == 63.0
    assert windows["claude.week"]["label"] == "Semana · todos los modelos"
    assert windows["claude.week-sonnet"]["used_percent"] == 12
    assert "claude.week-opus" not in windows, "a null window is not invented"
    assert report["pool_label"] == "Claude Max 20x"
    dumped = json.dumps(report)
    assert CLAUDE_TOKEN not in dumped and "luis@example.invalid" not in dumped and "11111111" not in dumped


def test_claude_limits_array_fills_scoped_weeks(tmp_path):
    session = {"identity": "acct", "plan": "max", "tier": ""}
    usage = {"five_hour": None, "seven_day": None, "limits": [
        {"kind": "session", "percent": 5, "resets_at": "2026-10-10T18:00:00Z"},
        {"kind": "weekly_all", "percent": 40, "resets_at": "2026-10-15T00:00:00Z"},
        {"kind": "weekly_scoped", "percent": 80, "resets_at": "2026-10-15T00:00:00Z",
         "scope": {"model": {"display_name": "Opus 5.5"}}}]}
    report = acc.build_claude_report(usage, session, now=NOW)
    labels = {w["window_id"]: w["label"] for w in report["windows"]}
    assert labels == {"claude.session": "Sesión (5 h)", "claude.week": "Semana · todos los modelos",
                      "claude.week-opus-5-5": "Semana · Opus 5.5"}


def test_claude_expired_token_is_reported_as_unknown_with_the_reason(tmp_path):
    config, home = _claude_files(tmp_path, expires_ms=(NOW - 10) * 1000)
    calls: list = []
    result = acc.collect_and_report_account_usage(
        "claude", reporter=lambda report, **kw: calls.append((report, kw)) or {"ok": True},
        collector_kwargs={"config_dir": config, "home": home, "now": NOW})
    assert result["reason"] == "claude_token_expired" and result["measured"] is False
    report, kwargs = calls[0]
    assert kwargs["agent_id"] == "claude-capacity"
    assert report["windows"] == [{"window_id": "claude.session", "label": report["windows"][0]["label"],
                                  "measurement": "unknown", "used_percent": None, "remaining_percent": None,
                                  "reset_at": None, "source": acc.CLAUDE_SOURCE}]
    assert "claude /login" in report["windows"][0]["label"]
    assert CLAUDE_TOKEN not in json.dumps(report)


def test_claude_never_rewrites_credentials(tmp_path):
    config, home = _claude_files(tmp_path, expires_ms=(NOW + 3600) * 1000)
    before = (config / ".credentials.json").read_bytes()
    acc.collect_claude_usage_report(config_dir=config, home=home, now=NOW, opener=_opener(CLAUDE_USAGE, []))
    with pytest.raises(acc.AccountCapacityError) as err:
        acc.collect_claude_usage_report(config_dir=config, home=home, now=NOW, opener=_opener({}, [], status=401))
    assert err.value.code == "claude_token_expired"
    assert (config / ".credentials.json").read_bytes() == before


def test_claude_without_profile_scope_says_so(tmp_path):
    config, home = _claude_files(tmp_path, expires_ms=(NOW + 3600) * 1000, scopes=("user:inference",))
    with pytest.raises(acc.AccountCapacityError) as err:
        acc.read_claude_session(config_dir=config, home=home, now=NOW)
    assert err.value.code == "claude_scope_missing"


def _cursor_db(tmp_path: Path, *, exp: float, sub: str = "google-oauth2|user_01ABC") -> tuple[Path, str]:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub, "exp": exp}).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJIUzI1NiJ9.{payload}.signature"
    path = tmp_path / "state.vscdb"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    db.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (token,))
    db.commit()
    db.close()
    return path, token


CURSOR_SUMMARY = {
    "billingCycleStart": "2026-09-15T09:40:36.000Z", "billingCycleEnd": "2026-10-15T09:40:36.000Z",
    "membershipType": "pro", "isUnlimited": False,
    "individualUsage": {"plan": {"enabled": True, "used": 2000, "limit": 2000, "remaining": 0,
                                 "autoPercentUsed": 78.5, "apiPercentUsed": 100, "totalPercentUsed": 80.06},
                        "onDemand": {"enabled": False, "used": 0, "limit": None}},
}


def test_cursor_reports_both_included_quotas_like_the_dashboard(tmp_path):
    path, token = _cursor_db(tmp_path, exp=NOW + 86400)
    seen: list = []
    report = acc.collect_cursor_usage_report(db_path=path, now=NOW, opener=_opener(CURSOR_SUMMARY, seen))
    cookie = {k.lower(): v for k, v in seen[0]["headers"].items()}["cookie"]
    assert cookie == f"WorkosCursorSessionToken=user_01ABC%3A%3A{token}"
    windows = {w["window_id"]: w for w in report["windows"]}
    assert set(windows) == {"cursor.models", "cursor.other"}
    assert windows["cursor.models"]["used_percent"] == 78.5 and windows["cursor.models"]["remaining_percent"] == 21.5
    assert windows["cursor.other"]["used_percent"] == 100 and windows["cursor.other"]["remaining_percent"] == 0
    assert windows["cursor.models"]["reset_at"] == "2026-10-15T09:40:36+00:00"
    assert report["pool_label"] == "Cursor Pro"
    assert token not in json.dumps(report) and "user_01ABC" not in json.dumps(report)


def test_cursor_on_demand_and_plan_fallback(tmp_path):
    summary = json.loads(json.dumps(CURSOR_SUMMARY))
    plan = summary["individualUsage"]["plan"]
    del plan["autoPercentUsed"], plan["apiPercentUsed"]
    plan["used"] = 500
    summary["individualUsage"]["onDemand"] = {"enabled": True, "used": 5, "limit": 20}
    report = acc.build_cursor_report(summary, {"user_id": "u"}, now=NOW)
    windows = {w["window_id"]: w["used_percent"] for w in report["windows"]}
    assert windows == {"cursor.plan": 25.0, "cursor.on-demand": 25.0}


def test_cursor_database_is_opened_read_only_and_expired_session_is_explained(tmp_path):
    path, _ = _cursor_db(tmp_path, exp=NOW - 5)
    before = path.read_bytes()
    with pytest.raises(acc.AccountCapacityError) as err:
        acc.read_cursor_session(db_path=path, now=NOW)
    assert err.value.code == "cursor_token_expired" and "Cursor" in err.value.label
    assert path.read_bytes() == before
    with pytest.raises(acc.AccountCapacityError) as missing:
        acc.read_cursor_session(db_path=tmp_path / "nope.vscdb", now=NOW)
    assert missing.value.code == "cursor_app_missing"


def test_network_failures_publish_nothing(tmp_path):
    path, _ = _cursor_db(tmp_path, exp=NOW + 86400)

    def offline(request, timeout):
        raise urllib.error.URLError("down")

    calls: list = []
    result = acc.collect_and_report_account_usage(
        "cursor", reporter=lambda report, **kw: calls.append(report) or {"ok": True},
        collector_kwargs={"db_path": path, "now": NOW, "opener": offline})
    assert result == {"ok": False, "provider_id": "cursor", "reason": "network_unavailable"}
    assert calls == [], "a transient failure keeps the last real reading instead of erasing it"


def test_reports_are_accepted_by_the_agora_store(tmp_path):
    store = Agora(tmp_path / "agora.db", clock=lambda: NOW)
    claude = acc.build_claude_report(CLAUDE_USAGE, {"identity": "acct", "plan": "max", "tier": "default_claude_max_20x"}, now=NOW)
    cursor = acc.build_cursor_report(CURSOR_SUMMARY, {"user_id": "u"}, now=NOW)
    for report in (claude, cursor):
        store.usage_report({"agent": f"{report['provider_id']}-capacity", **report})
    providers = {p["provider_id"]: p for p in store.usage()["providers"]}
    cursor_windows = {w["window_id"]: w for w in providers["cursor"]["pools"][0]["windows"]}
    assert cursor_windows["cursor.other"]["remaining_percent"] == 0
    assert providers["claude"]["pools"][0]["pool_label"] == "Claude Max 20x"
    (tmp_path / "home").mkdir()
    _, home = _claude_files(tmp_path / "home", expires_ms=0)
    unknown = acc.claude_unavailable_report(acc.AccountCapacityError("claude_token_expired", "caducada"), home=home)
    store.usage_report({"agent": "claude-capacity", **unknown})
    pools = {p["provider_id"]: p for p in store.usage()["providers"]}["claude"]["pools"]
    assert any(w["measurement"] == "unknown" and w["label"] == "caducada" for pool in pools for w in pool["windows"])
    store.close()


def test_cli_publication_uses_the_provider_capacity_identity(tmp_path):
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "reported_at": "x"}))

    report = acc.build_cursor_report(CURSOR_SUMMARY, {"user_id": "u"}, now=NOW)
    result = agora_capacity._report_via_agora_cli(report, repo_root=Path(__file__).resolve().parents[1],
                                                 runner=runner, agent_id="cursor-capacity")
    assert captured["command"][captured["command"].index("--as") + 1] == "cursor-capacity"
    assert result["provider_id"] == "cursor" and result["window_count"] == 2


def test_reporter_thread_polls_both_providers(monkeypatch):
    done = threading.Event()
    seen = []

    def fake(provider, *, reporter_kwargs):
        seen.append(provider)
        if len(seen) >= 2:
            done.set()
        return {"ok": True}

    monkeypatch.setattr(acc, "collect_and_report_account_usage", fake)
    acc.start_account_capacity_reporter(interval_seconds=60, reporter_kwargs={"env": {}})
    assert done.wait(2)
    acc.stop_account_capacity_reporter()
    assert seen[:2] == ["claude", "cursor"]
