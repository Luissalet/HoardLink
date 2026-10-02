"""The hub installs its recommended rules when it starts (unless the person removed one), the new 0.7 rules do
what they say, and a rule aimed at an app that is not installed is skipped instead of failing."""

from __future__ import annotations

import json
import os

import pytest

from hoard_link.hub import actions
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.events import EventLog
from hoard_link.hub.rules import RuleEngine, example_rules, rule_matches, validate_rule

from ._hub_fakes import FakeApp, make_hub, wait_for
from .conftest import free_port

OLD_SIX = {"rule-backup-on-stop", "rule-watch-digest", "rule-watcher-alert-digest", "rule-shipment-digest",
           "rule-repo-issue-digest", "rule-restart-down"}


@pytest.fixture(autouse=True)
def _auto_on(monkeypatch):
    monkeypatch.delenv("HOARD_HUB_AUTO_RULES", raising=False)


def rule_ids(hub):
    return {r["id"] for r in hub.rules.list()}


def test_the_recommended_set_keeps_the_six_and_adds_the_new_ones():
    ex = example_rules()
    ids = [r["id"] for r in ex]
    assert sorted(ids[:6]) == sorted(OLD_SIX) and len(ids) == len(set(ids)) == 18
    for r in ex:
        assert not validate_rule(r), r["id"]
    new = [r for r in ex if r["id"] not in OLD_SIX and r["id"] != "rule-download-transcribe-index"]
    assert len(new) == 11 and all(r["id"].startswith("rule-") and r["note"] and r["cooldown_s"] == 5 for r in new)


def test_the_hub_installs_them_at_start_and_not_twice(tmp_path):
    hub = make_hub(tmp_path)
    try:
        assert rule_ids(hub) == {r["id"] for r in example_rules()}
        assert all(r["enabled"] for r in hub.rules.list() if r["id"] != "rule-download-transcribe-index")
        assert not hub.rules.get("rule-download-transcribe-index")["enabled"]
    finally:
        hub.close()
    hub2 = make_hub(tmp_path)                                    # a second start adds nothing and keeps what the person changed
    try:
        hub2.rules.update("rule-ledger-payment-failed", {"enabled": False, "cooldown_s": 99})
    finally:
        hub2.close()
    hub3 = make_hub(tmp_path)
    try:
        assert len(hub3.rules.list()) == 18
        r = hub3.rules.get("rule-ledger-payment-failed")
        assert r["enabled"] is False and r["cooldown_s"] == 99
    finally:
        hub3.close()


def test_a_removed_recommended_rule_stays_removed_until_asked_for(tmp_path):
    hub = make_hub(tmp_path)
    meta = os.path.join(hub.config.data_dir, "rules_meta.json")
    try:
        assert hub.rules.remove("rule-restart-down")["ok"] and hub.rules.remove("rule-ledger-subscription-new")["ok"]
        assert json.loads(open(meta, encoding="utf-8").read())["dismissed"] == ["rule-ledger-subscription-new", "rule-restart-down"]
        # a rule of the person's own is not remembered anywhere
        hub.rules.add({"id": "mine", "name": "m", "when": {"type": "x.y"}, "then": [{"kind": "event", "type": "z"}]})
        hub.rules.remove("mine")
        assert "mine" not in json.loads(open(meta, encoding="utf-8").read())["dismissed"]
    finally:
        hub.close()
    hub2 = make_hub(tmp_path)
    try:
        assert "rule-restart-down" not in rule_ids(hub2) and "rule-ledger-subscription-new" not in rule_ids(hub2) and len(hub2.rules.list()) == 16
        # "install the recommended rules" brings back what it installs and forgets the dismissal
        res = hub2.rules.install_examples()
        assert sorted(res["installed"]) == ["rule-ledger-subscription-new", "rule-restart-down"]
        assert json.loads(open(meta, encoding="utf-8").read())["dismissed"] == []
        assert len(hub2.rules.list()) == 18
    finally:
        hub2.close()
    hub3 = make_hub(tmp_path)
    try:
        assert len(hub3.rules.list()) == 18
    finally:
        hub3.close()


def test_the_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_AUTO_RULES", "0")
    hub = make_hub(tmp_path / "a")
    try:
        assert hub.rules.list() == []
        assert len(hub.rules.install_examples()["installed"]) == 18            # the button still works
    finally:
        hub.close()
    monkeypatch.delenv("HOARD_HUB_AUTO_RULES")
    (tmp_path / "b" / "hubdata").mkdir(parents=True)
    (tmp_path / "b" / "hubdata" / "rules_meta.json").write_text('{"auto_install": false}', encoding="utf-8")
    hub = make_hub(tmp_path / "b")
    try:
        assert hub.rules.list() == []
    finally:
        hub.close()
    # an engine with no file behind it has nothing to install into
    eng = RuleEngine(None, EventLog(None), lambda a, c, w: [])
    assert eng.list() == []
    eng.close()


# ---- the new rules ---------------------------------------------------------------------------------------

def ev(type_, data=None, source="x"):
    return {"id": 1, "type": type_, "source": source, "data": data or {}}


def test_new_rules_match_their_events_and_only_those():
    by = {r["id"]: r for r in example_rules()}
    assert rule_matches(by["rule-ledger-payment-failed"], ev("ledger.payment.failed"))
    assert rule_matches(by["rule-ledger-subscription-price"], ev("ledger.subscription.price"))
    assert rule_matches(by["rule-ledger-subscription-new"], ev("ledger.subscription.new"))
    assert rule_matches(by["rule-homehoard-maintenance-due"], ev("homehoard.maintenance.due"))
    inc = by["rule-cassandra-app-incident"]
    assert rule_matches(inc, ev("cassandra.incident.opened", {"service_kind": "app"}))
    assert not rule_matches(inc, ev("cassandra.incident.opened", {"service_kind": "external"}))
    assert rule_matches(by["rule-funes-minutes-people-deadlines"], ev("funes.minutes.ready"))
    assert rule_matches(by["rule-people-commitment-deadline"], ev("people.commitment.added"))
    pub = by["rule-pygmalion-publish-galton"]
    assert rule_matches(pub, ev("pygmalion.job.done", {"kind": "publish"})) and not rule_matches(pub, ev("pygmalion.job.done", {"kind": "train"}))
    # the legacy names reach the same rules once the event log has renamed them
    log = EventLog(None)
    done = log.emit("pygmalion.job_done", {"kind": "publish", "model": "m1"}, source="pygmalion")
    assert rule_matches(pub, done)
    render = by["rule-lumiere-render-draft"]
    assert rule_matches(render, log.emit("lumiere.render.done", {"ref": "hoard://lumiere/render/1"}, source="lumiere"))
    assert not rule_matches(render, log.emit("lumiere.render.failed", {}, source="lumiere"))
    assert rule_matches(by["rule-mercator-sales-ledger"], ev("mercator.sales.imported"))
    exp = by["rule-exports-to-vulcan"]
    assert rule_matches(exp, ev("plato.export.done")) and rule_matches(exp, ev("gepetto.export.done")) and not rule_matches(exp, ev("plato.export.failed"))
    assert not any("links.highlight.added" in json.dumps(r["when"]) for r in example_rules())     # nothing by default


def test_notify_rules_render_what_they_promise():
    by = {r["id"]: r for r in example_rules()}
    ctx = actions.base_context(event={"type": "ledger.payment.failed", "data": {"merchant": "Vodafone", "amount": 39.9, "currency": "EUR"}})
    step = by["rule-ledger-payment-failed"]["then"][0]
    assert step["kind"] == "hub" and step["tool"] == "hub_notify"
    args = actions.render(step["args"], ctx)
    assert args["title"] == "Pago fallido: Vodafone" and args["priority"] == "high" and args["body"].startswith("39.9")
    for rid, prio, group in (("rule-ledger-subscription-price", "normal", "subscription"), ("rule-homehoard-maintenance-due", "normal", "maintenance"),
                             ("rule-cassandra-app-incident", "high", "incident")):
        a = by[rid]["then"][0]["args"]
        assert by[rid]["then"][0]["tool"] == "hub_notify" and a["priority"] == prio and a["group"] == group
    digest = by["rule-ledger-subscription-new"]["then"][0]
    assert digest["kind"] == "event" and digest["type"] == "digest.item"


@pytest.fixture
def apps(tmp_path):
    names = {"kafka": ["deadlines_from_minutes", "deadline_add"], "people": ["people_from_minutes"], "galton": ["galton_run"],
             "mercator": ["post_draft_from_media"], "ledger": ["income_from_sales"], "vulcan": ["model_import_file"]}
    fakes = {n: FakeApp(n, handlers={t: (lambda a: {"ok": True}) for t in ts}) for n, ts in names.items()}
    hub = make_hub(tmp_path, list(fakes.values()))
    yield hub, fakes
    hub.close()
    for f in fakes.values():
        f.stop()


def fired(hub, rid, n=1):
    return wait_for(lambda: len([h for h in hub.rules.history if h["rule"] == rid]) >= n) and [h for h in hub.rules.history if h["rule"] == rid]


def test_tool_rules_call_the_apps_with_the_event_data(apps):
    hub, f = apps
    hub.events.emit("funes.minutes.ready", {"minutes_id": 41}, source="funes")
    [run] = fired(hub, "rule-funes-minutes-people-deadlines")
    assert run["ok"] and f["people"].calls[-1]["arguments"] == {"minutes_id": 41} and f["kafka"].calls[-1]["arguments"] == {"minutes_id": 41}
    assert f["people"].calls[-1]["caller"] == "rule:rule-funes-minutes-people-deadlines"
    hub.events.emit("people.commitment.added", {"title": "Send the deck", "due": "2026-10-09", "ref": "hoard://people/commitment/3"}, source="people")
    fired(hub, "rule-people-commitment-deadline")
    assert f["kafka"].calls[-1]["arguments"] == {"title": "Send the deck", "due": "2026-10-09", "source_ref": "hoard://people/commitment/3"}
    # the legacy event name reaches the rule through the alias; only a publish triggers it
    hub.events.emit("pygmalion.job.done", {"kind": "train", "model": "x"}, source="pygmalion")
    hub.events.emit("pygmalion.job_done", {"kind": "publish", "model": "kettle-v2"}, source="pygmalion")
    fired(hub, "rule-pygmalion-publish-galton")
    assert [c["arguments"] for c in f["galton"].calls] == [{"models": ["kettle-v2"]}]
    hub.events.emit("lumiere.render.done", {"ref": "hoard://lumiere/render/5", "title": "Teaser"}, source="lumiere")
    fired(hub, "rule-lumiere-render-draft")
    assert f["mercator"].calls[-1]["arguments"] == {"media_ref": "hoard://lumiere/render/5", "title": "Teaser"}
    hub.events.emit("mercator.sales.imported", {"batch": "2026-09"}, source="mercator")
    fired(hub, "rule-mercator-sales-ledger")
    assert f["ledger"].calls[-1]["arguments"] == {"batch": "2026-09"}
    hub.rules.update("rule-exports-to-vulcan", {"cooldown_s": 0})            # the default 5 s cooldown would swallow the second
    hub.events.emit("plato.export.done", {"path": "C:/x/a.stl", "ref": "hoard://plato/export/1"}, source="plato")
    hub.events.emit("gepetto.export.done", {"path": "C:/x/b.stl", "ref": "hoard://gepetto/export/2"}, source="gepetto")
    fired(hub, "rule-exports-to-vulcan", 1)
    wait_for(lambda: len(f["vulcan"].calls) == 2)
    assert {c["arguments"]["path"] for c in f["vulcan"].calls} == {"C:/x/a.stl", "C:/x/b.stl"}
    assert f["vulcan"].calls[0]["arguments"]["source_ref"].startswith("hoard://")


def test_a_rule_for_an_app_that_is_not_installed_is_skipped_not_failed(tmp_path):
    hub = make_hub(tmp_path)                                    # none of the target apps exists here
    try:
        rule = hub.rules.get("rule-funes-minutes-people-deadlines")
        summary = hub.rules.run(rule, ev("funes.minutes.ready", {"minutes_id": 1}, source="funes"))
        assert summary["ok"] is True and summary["skipped"] == 2
        assert all(r["skipped"] and r["ok"] and "not installed" in r["reason"] for r in summary["results"])
        assert hub.rules.get(rule["id"])["last"]["ok"] is True and hub.rules.get(rule["id"])["last"]["skipped"] == 2
        assert hub.events.query(type="hub.rule.ran")[0]["data"]["skipped"] == 2
        # a hub tool this hub does not have is skipped the same way; a real failure still is one
        res = actions.run_one(hub, {"kind": "hub", "tool": "hub_does_not_exist"}, {})
        assert res["ok"] and res["skipped"]
        res = actions.run_one(hub, {"kind": "hub", "tool": "hub_event_emit", "args": {}}, {})
        assert res["ok"] is False and "skipped" not in res
        assert actions.run_one(hub, {"kind": "start_app", "app": "nobody"}, {})["ok"] is False
        # an app that IS installed but down is an error, not a skip
        down = make_hub(tmp_path / "d", extra_apps=["kafka"])
        try:
            res = actions.run_one(down, {"kind": "tool", "app": "kafka", "tool": "deadline_add", "args": {}}, {}, timeout=2)
            assert res["ok"] is False and not res.get("skipped")
        finally:
            down.close()
    finally:
        hub.close()


def test_an_app_the_hub_just_stopped_is_neither_reported_nor_restarted(tmp_path):
    from hoard_link.hub.events import EventLog
    from hoard_link.hub.rules import RuleEngine, example_rules
    ran = []
    log = EventLog(None)
    eng = RuleEngine(str(tmp_path / "rules.json"), log, lambda acts, ctx, caller: ran.append(ctx["event"]["data"]) or [],
                     auto_install=False)
    try:
        for ex in example_rules():
            if ex["id"] in ("rule-restart-down", "rule-cassandra-app-incident"):
                assert eng.add(dict(ex))["ok"]
        log.emit("hub.app.stopped", {"app": "kafka"}, source="hub")
        log.emit("cassandra.incident.opened", {"app": "kafka", "to_state": "down", "service_kind": "app", "incident_id": "1"},
                 source="cassandra")
        log.emit("cassandra.incident.opened", {"app": "ledger", "to_state": "down", "service_kind": "app", "incident_id": "2"},
                 source="cassandra")
        import time
        deadline = time.time() + 5
        while time.time() < deadline and len(ran) < 2:
            time.sleep(0.05)
        time.sleep(0.2)
        assert sorted({d["app"] for d in ran}) == ["ledger"]          # kafka was stopped by the hub a moment ago
    finally:
        eng.close()


def test_a_newer_template_upgrades_an_installed_recommended_rule(tmp_path):
    from hoard_link.hub.events import EventLog
    from hoard_link.hub.rules import RuleEngine, example_rules
    old = next(dict(ex) for ex in example_rules() if ex["id"] == "rule-restart-down")
    old["when"] = {"type": "cassandra.incident.opened"}
    old.pop("rev", None)
    old["enabled"] = False
    eng = RuleEngine(str(tmp_path / "rules.json"), EventLog(None), lambda *a: [], auto_install=False)
    assert eng.add(old)["ok"]
    res = eng.install_recommended_on_start()
    assert "rule-restart-down" in res["upgraded"]
    cur = eng.get("rule-restart-down")
    assert cur["when"].get("unless_recent") and cur["rev"] == 2 and cur["enabled"] is False     # the person's switch stays
    assert eng.install_recommended_on_start()["upgraded"] == []
    eng.close()
