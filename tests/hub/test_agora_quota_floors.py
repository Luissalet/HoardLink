"""Weekly floors through the Agora store and the console's local HTTP and MCP handlers."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from hoard_link.hub.agora import Agora, MIGRATIONS
from hoard_link.hub import agora_auto_review, agora_capacity
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.config import HubConfig
from hoard_link.hub.facets import Request
from hoard_link.sqlkit import Database


NOW = 1_795_626_000.0
STUDIO = "estudio"
FLOOR_ROUTE = "/api/agora/console/quota_floors"
WORKER_ROUTE = "/api/agora/console/worker"


@pytest.fixture
def store(tmp_path):
    tick = iter(range(10000))
    config = HubConfig(data_dir=tmp_path, roots=[], language="es")
    agora = Agora(tmp_path / "agora.db", clock=lambda: NOW + next(tick), config=config)
    for sid in (STUDIO, "other"):
        agora.space_create({"agent": "builder", "id": sid, "title": sid})
    agora.quota_foreman = lambda sid: "test-lead" if sid == STUDIO else None
    yield agora
    agora.close()


@pytest.fixture
def console(store, monkeypatch):
    hub = SimpleNamespace(config=store.config, facet=lambda key: SimpleNamespace(agora=store) if key == "agora" else None)
    console = AgoraConsoleFacet(hub)
    monkeypatch.setattr(AgoraConsoleFacet, "_provider_data", lambda self: {})
    monkeypatch.setattr(AgoraConsoleFacet, "_codex_executable", lambda self: None)
    yield console
    console.close()


def _window(window_id, remaining, *, duration=None, measurement="measured", reset_at=None):
    return {"window_id": window_id, "used_percent": None if remaining is None else 100 - remaining,
            "remaining_percent": remaining, "measurement": measurement, "reset_at": reset_at,
            "window_duration_mins": duration}


def _report(store, provider, *windows, pool="account"):
    return store.usage_report({"agent": f"{provider}-capacity", "provider_id": provider, "pool_id": pool,
                               "pool_label": provider, "source": "fixture", "windows": list(windows)})


def _read(console, sid=STUDIO):
    return console.handlers()["hub_agora_quota_floors"]({"space_id": sid})


def _set(console, floors, *, actor="test-lead", rev=1, sid=STUDIO):
    return console.handlers()["hub_agora_quota_floors_set"](
        {"space_id": sid, "actor": actor, "expected_revision": rev, "floors": floors})


def _post(console, path, body, *, caller="ui", agent=False):
    return console.post(Request("POST", path, {}, {"space_id": STUDIO, **body}, lambda: caller, lambda: agent))


def test_migration_seeds_only_existing_studio(tmp_path):
    path = tmp_path / "old.db"
    with Database(path, migrations=MIGRATIONS[:-1]) as old:
        old.migrate()
        for sid in (STUDIO, "other"):
            old.insert("coordination_spaces", {"id": sid, "title": sid, "created_by": "seed", "created": 0, "updated": 0})
    agora = Agora(path, config=HubConfig(data_dir=tmp_path, roots=[]))
    try:
        rows = agora.db.query("SELECT space_id, revision FROM agora_quota_floors")
        assert [(row["space_id"], row["revision"]) for row in rows] == [(STUDIO, 1)]
        assert agora.for_space(STUDIO).board()["quota_floors"]["floors"] == {"claude": 45, "codex": 55}
    finally:
        agora.close()


def test_space_settings_validate_permissions_revision_and_undo(console):
    assert _read(console)["floors"] == {"claude": 45, "codex": 55}
    assert _read(console, "other")["floors"] == {}
    assert _set(console, {"claude": 50}, actor="builder")["status"] == 403
    for invalid in ({"claude": True}, {"claude": 101}, {"codex": -1}, {"cursor": 90}, {"codex": "50"}):
        assert _set(console, invalid)["status"] == 400
    changed = _post(console, FLOOR_ROUTE, {"expected_revision": 1, "floors": {"claude": 50.5}})
    assert changed["ok"] and _read(console)["floors"] == {"claude": 50.5}
    undo = changed["undo"]
    restored = console.handlers()[undo["tool"]]({**undo["arguments"], "actor": "test-lead"})
    assert restored["ok"] and _read(console)["floors"] == {"claude": 45, "codex": 55}
    assert console.handlers()[undo["tool"]]({**undo["arguments"], "actor": "test-lead"})["code"] == "stale_quota_floors"
    assert _read(console, "other")["floors"] == {}


def test_codex_floor_uses_explicit_account_wide_week_and_keeps_observation_time(store, console):
    _report(store, "codex", _window("codex.primary", 8, duration=300),
            _window("codex.secondary", 12),
            _window("codex-extra.secondary", 70, duration=10080))
    provider = _read(console)["providers"]["codex"]
    assert provider["weekly"]["remaining_percent"] is None and provider["reported_at"]
    report = agora_capacity.build_usage_report({"id": "synthetic-account"}, {"rateLimits": {
        "primary": {"usedPercent": 92, "windowDurationMins": 300},
        "secondary": {"usedPercent": 27.5, "windowDurationMins": 10080},
    }}, now=NOW)
    result = store.usage_report({"agent": "codex-capacity", **report})
    expected = {"remaining_percent": 72.5, "reported_at": result["reported_at"], "window_id": "codex.secondary"}
    assert _read(console)["providers"]["codex"]["weekly"] == expected
    assert store.for_space(STUDIO).board()["quota_floors"]["providers"]["codex"]["weekly"] == expected
    assert _read(console, "other")["providers"]["codex"]["weekly"] == expected


def test_claude_floor_ignores_session_and_model_specific_windows(store, console):
    _report(store, "claude", _window("claude.session", 9), _window("claude.week-opus", 1),
            _window("claude.week", 80))
    assert _read(console)["providers"]["claude"]["weekly"]["remaining_percent"] == 80
    assert store.for_space(STUDIO).channel_read({"peek": True})["messages"] == []


def test_floor_uses_local_reporter_and_stale_reading_is_unmeasured(store, console):
    first = _report(store, "claude", _window("claude.week", 70))
    store.usage_report({"agent": "other-meter", "provider_id": "claude", "pool_id": "other-account",
                        "pool_label": "Other account", "source": "fixture",
                        "windows": [_window("claude.week", 20)]})
    assert _read(console)["providers"]["claude"]["weekly"]["remaining_percent"] == 70
    assert store.for_space(STUDIO).channel_read({"peek": True})["messages"] == []
    store.clock = lambda: NOW + 25 * 3600
    weekly = _read(console)["providers"]["claude"]["weekly"]
    assert weekly["remaining_percent"] is None and weekly["reported_at"] == first["reported_at"]


def test_crossing_posts_to_general_and_mentions_directing_lead_once_until_recovery(store, console):
    space = store.for_space(STUDIO)
    store.for_space("other").heartbeat({"agent": "other-director"})
    store.db.execute("INSERT INTO agora_ranks VALUES(?,?,?,?,?)",
                     ("other", "other-director", "director", "system", NOW))
    assert _set(console, {"claude": 30}, sid="other", actor="other-director", rev=0)["ok"]

    for remaining in (69, 45, 44, 43, None, 42):
        measurement = "unknown" if remaining is None else "measured"
        _report(store, "claude", _window("claude.week", remaining, measurement=measurement))
    messages = space.channel_read({"peek": True})["messages"]
    notices = [message for message in messages if message["kind"] == "comment"]
    assert len(notices) == 1 and "44 % de cuota semanal libre" in notices[0]["body"]
    assert notices[0]["mentions"] == ["test-lead"]
    inbox = space.inbox({"agent": "test-lead", "peek": True})["messages"]
    assert any(message["id"] == notices[0]["id"] and message["for_you"] for message in inbox)
    assert store.for_space("other").channel_read({"peek": True})["messages"] == []

    _report(store, "claude", _window("claude.week", 45))
    _report(store, "claude", _window("claude.week", 20))
    assert sum(message["kind"] == "comment" for message in space.channel_read({"peek": True})["messages"]) == 2
    other = store.for_space("other").channel_read({"peek": True})["messages"]
    assert [message["mentions"] for message in other if message["kind"] == "comment"] == [["other-director"]]


@pytest.mark.parametrize("remaining,measurement,reset", [(None, "unknown", None), (11, "declared", None),
    (11, "measured", datetime.fromtimestamp(NOW - 1, timezone.utc).isoformat()), (55, "measured", None)])
def test_unmeasured_and_at_floor_do_not_block_launch(store, console, remaining, measurement, reset):
    _report(store, "codex", _window("codex.secondary", remaining, duration=10080,
                                    measurement=measurement, reset_at=reset))
    data = _read(console)["providers"]["codex"]["weekly"]
    assert data["remaining_percent"] == (55 if remaining == 55 else None)
    result = _post(console, WORKER_ROUTE, {"leader_id": "codex", "task": "Synthetic assignment"})
    assert result["ok"] and result["worker"]["state"] == "unavailable"
    assert store.for_space(STUDIO).channel_read({"peek": True})["messages"] == []


def test_launch_refuses_before_reserving_worker_and_accepts_explicit_lead_confirmation(store, console):
    _report(store, "codex", _window("codex.secondary", 11, duration=10080))
    request = {"leader_id": "codex", "task": "Synthetic assignment", "lang": "en"}
    refusal = _post(console, WORKER_ROUTE, request)
    assert refusal["status"] == 409 and refusal["code"] == "quota_below_floor"
    assert "11%" in refusal["error"] and "55%" in refusal["error"]
    assert console.for_space(STUDIO)._worker_rows() == []
    confirmation = {"provider_id": "codex", "floor_percent": 55}
    assert refusal["confirm_quota_floor"] == confirmation
    assert _post(console, WORKER_ROUTE, {**request, "actor": "test-lead", "confirm_quota_floor": confirmation},
                 caller="builder", agent=True)["status"] == 403
    call = console.handlers()["hub_agora_console_worker"]
    args = {"space_id": STUDIO, **request, "confirm_quota_floor": confirmation}
    assert call({**args, "actor": "builder"})["status"] == 403
    assert call({**args, "actor": "test-lead", "confirm_quota_floor": True})["status"] == 409
    assert call({**args, "actor": "test-lead", "confirm_quota_floor": {**confirmation, "floor_percent": 40}})["status"] == 409
    started = call({**args, "actor": "test-lead"})
    assert started["ok"] and started["worker"]["state"] == "unavailable"
    assert len(console.for_space(STUDIO)._worker_rows()) == 1
    assert call({**args, "space_id": "other", "actor": "builder"})["ok"]


def test_automatic_launch_waits_for_floor_and_can_choose_another_provider(store, console, monkeypatch):
    scoped = console.for_space(STUDIO)
    monkeypatch.setattr(agora_auto_review, "codex_observation", lambda: None)
    monkeypatch.setattr(scoped, "_provider_data", lambda: {
        "codex": {"state": "available", "catalog": [{"id": "codex-fixture"}]},
        "claude": {"state": "available", "catalog": [{"id": "claude-fixture"}]},
    })
    _report(store, "codex", _window("codex.secondary", 30, duration=10080))
    assert agora_auto_review._choose(scoped, ["codex", "claude"]) is None
    _report(store, "claude", _window("claude.week", 60))
    assert agora_auto_review._choose(scoped, ["codex", "claude"])[0] == "claude"
    _report(store, "codex", _window("codex.secondary", 55, duration=10080))
    assert agora_auto_review._choose(scoped, ["codex", "claude"])[0] == "codex"
