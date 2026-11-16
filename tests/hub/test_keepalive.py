"""The keep-alive list: apps that start with the serving Hub and again when they fall."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hoard_link.hub import keepalive, procs, tools
from hoard_link.hub.keepalive import KeepAliveFacet


class StubHub:
    def __init__(self, data_dir, apps):
        self.config = SimpleNamespace(data_dir=str(data_dir))
        self._apps = {a.id: a for a in apps}
        self.placement = SimpleNamespace(local_block=lambda app_id: self.elsewhere.get(app_id))
        self.elsewhere: dict[str, str] = {}
        self.manual: set[str] = set()
        self.started: list[str] = []
        self.events: list[tuple[str, dict]] = []
        self.start_result = {"ok": True, "pid": 123}

    def get(self, app_id):
        return self._apps.get(app_id)

    def automation_blocked(self, app_id):
        return app_id in self.manual

    def _manual_stop(self, ids, stopped=True):
        (self.manual.update if stopped else self.manual.difference_update)(ids)

    def start(self, app_id, wait=True, *, automatic=False):
        assert automatic and not wait
        self.started.append(app_id)
        return dict(self.start_result, app=app_id)

    def _safe_emit(self, type, data):
        self.events.append((type, data))


def app(app_id, auto_start=None, launchable=True):
    family = {} if auto_start is None else {"auto_start": auto_start}
    return SimpleNamespace(id=app_id, name=app_id.title() + "'s Hoard", family=family, launchable=launchable)


@pytest.fixture
def health(monkeypatch):
    states: dict[str, str] = {}
    monkeypatch.setattr(procs, "health", lambda a: SimpleNamespace(state=states.get(a.id, "down")))
    return states


def make(tmp_path, *extra):
    hub = StubHub(tmp_path, [app("darwin"), app("links"), app("talos"), app("kafka"), app("baird", auto_start=False), *extra])
    return hub, KeepAliveFacet(hub)


def test_default_list_without_file(tmp_path):
    _, facet = make(tmp_path)
    assert facet.apps() == ["darwin", "links", "talos", "kafka"]


def test_boot_starts_down_apps_and_clears_previous_manual_stops(tmp_path, health):
    hub, facet = make(tmp_path)
    hub.manual.update({"darwin", "people"})          # stopped in an earlier Hub session
    health.update(links="healthy", talos="foreign")
    facet.check(boot=True)
    assert hub.started == ["darwin", "kafka"]          # links is up; talos's port answers as another program
    assert "darwin" not in hub.manual and "people" in hub.manual
    assert [e[0] for e in hub.events] == ["hub.keepalive.started"] * 2


def test_manual_stop_is_respected_while_the_hub_runs(tmp_path, health):
    hub, facet = make(tmp_path)
    health.update(links="healthy", talos="healthy", kafka="healthy")
    hub.manual.add("darwin")
    facet.check()
    assert hub.started == []


def test_failed_starts_back_off_and_healthy_resets(tmp_path, health, monkeypatch):
    hub, facet = make(tmp_path)
    health.update(links="healthy", talos="healthy", kafka="healthy")
    now = [1000.0]
    monkeypatch.setattr(keepalive.time, "time", lambda: now[0])
    facet.check()
    assert hub.started == ["darwin"]
    now[0] += 30                                        # before the next try: left alone
    facet.check()
    assert hub.started == ["darwin"]
    now[0] += 31
    facet.check()
    assert hub.started == ["darwin", "darwin"]
    assert facet.status(probe=False)["apps"][0]["next_try_ts"] == now[0] + 120
    health["darwin"] = "healthy"
    facet.check()
    row = facet.status(probe=False)["apps"][0]
    assert row["attempts"] == 0 and row["next_try_ts"] is None


def test_placement_elsewhere_is_left_alone(tmp_path, health):
    hub, facet = make(tmp_path)
    health.update(links="healthy", talos="healthy", kafka="healthy")
    hub.elsewhere["darwin"] = "on a Spark"
    facet.check()
    assert hub.started == []


def test_set_persists_and_refuses_manual_only_apps(tmp_path, health):
    hub, facet = make(tmp_path, app("people"), app("tool", launchable=False))
    assert facet.set("people", True)["ok"]
    assert facet.set("darwin", False)["ok"]
    saved = json.loads((tmp_path / "keep-alive.json").read_text(encoding="utf-8"))
    assert saved == {"apps": ["links", "talos", "kafka", "people"]}
    assert KeepAliveFacet(hub).apps() == saved["apps"]
    for refused in ("baird", "tool", "nope"):
        res = facet.set(refused, True)
        assert not res["ok"] and res["error"]
    assert "baird" not in facet.apps()


def test_never_list_is_dropped_from_a_hand_edited_file(tmp_path):
    (tmp_path / "keep-alive.json").write_text(json.dumps({"apps": ["baird", "darwin", "darwin"]}), encoding="utf-8")
    _, facet = make(tmp_path)
    assert facet.apps() == ["darwin"]


def test_watch_only_runs_when_served_and_env_turns_it_off(tmp_path, monkeypatch):
    _, facet = make(tmp_path)
    assert not facet.serving                            # building the facet starts nothing
    monkeypatch.setenv("HOARD_HUB_KEEP_ALIVE", "0")
    assert facet.serve() is False and not facet.serving


def test_http_and_mcp_surface(tmp_path, health):
    hub, facet = make(tmp_path)
    req = SimpleNamespace(path="/api/keep-alive", q_bool=lambda k, d=False: False, body={"app": "kafka", "keep": False})
    assert [r["id"] for r in facet.get(req)["apps"]] == ["darwin", "links", "talos", "kafka"]
    assert facet.post(req)["apps"] == ["darwin", "links", "talos"]
    names = {t["name"] for t in tools.all_tools()}
    assert {"hub_keep_alive_status", "hub_keep_alive_set"} <= names
    assert facet.handlers()["hub_keep_alive_set"]({"app": "kafka"})["apps"][-1] == "kafka"
