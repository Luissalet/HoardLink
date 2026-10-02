"""Canonical job events: legacy names are stored under their canonical type (original kept in _orig_type) and
everything written against the old name — rules, queries, tails — keeps working."""

from __future__ import annotations

import pytest

from hoard_link.hub.events import EVENT_ALIASES, EventLog, canonical_type, canonicalize, iter_since, register_alias
from hoard_link.hub.rules import rule_matches

from ._hub_fakes import make_hub, wait_for

SPEC = [
    ("pygmalion.job_queued", "pygmalion.job.queued", {}),
    ("pygmalion.job_done", "pygmalion.job.done", {}),
    ("hypatia.teacher_job.done", "hypatia.job.done", {}),
    ("hypatia.teacher_job.no_model", "hypatia.job.failed", {}),
    ("galton.run.done", "galton.job.done", {}),
    ("lumiere.render.done", "lumiere.job.done", {"kind": "render"}),
    ("lumiere.render.failed", "lumiere.job.failed", {"kind": "render"}),
    ("lumiere.job.failed", "lumiere.job.failed", {}),
    ("links.media.done", "links.job.done", {"kind": "download"}),
    ("links.media.failed", "links.job.failed", {}),
    ("midas.backtest.finished", "midas.job.done", {}),
    ("vitruvius.render.done", "vitruvius.job.done", {}),
    ("vitruvius.assay.done", "vitruvius.job.done", {}),
]


def test_hub_backup_events_keep_their_name_and_read_as_jobs():
    # the hub's own UI and rules listen to hub.backup.*: the log keeps the name, readers may map it
    assert canonical_type("hub.backup.done") == "hub.backup.done"
    typ, data = canonicalize("hub.backup.done", {"snapshot": "s"}, soft=True)
    assert typ == "hub.job.done" and data["kind"] == "backup"
    assert canonicalize("hub.backup.done", {}) == ("hub.backup.done", {})


@pytest.mark.parametrize("legacy, canonical, defaults", SPEC)
def test_the_table(legacy, canonical, defaults):
    assert canonical_type(legacy) == canonical
    assert canonical_type(canonical) == canonical
    typ, data = canonicalize(legacy, {"job_id": "j"})
    assert typ == canonical and data["job_id"] == "j"
    for k, v in defaults.items():
        assert data[k] == v
    if legacy != canonical:
        assert data["_orig_type"] == legacy
    else:
        assert "_orig_type" not in data


def test_non_aliased_types_pass_through():
    assert canonical_type("links.watch.new") == "links.watch.new"
    assert canonical_type("Links.Watch.New") == "links.watch.new"
    assert canonicalize("links.watch.new", {"a": 1}) == ("links.watch.new", {"a": 1})
    assert canonicalize("agent.call", None) == ("agent.call", {})


def test_emit_stores_the_canonical_type_and_keeps_the_original(tmp_path):
    log = EventLog(str(tmp_path / "e.db"))
    ev = log.emit("lumiere.render.done", {"job_id": "r1", "title": "Cut"}, source="lumiere")
    assert ev["type"] == "lumiere.job.done" and ev["data"] == {"job_id": "r1", "title": "Cut", "kind": "render", "_orig_type": "lumiere.render.done"}
    # an event that already says its kind keeps it
    ev2 = log.emit("lumiere.render.done", {"job_id": "r2", "kind": "export"}, source="lumiere")
    assert ev2["data"]["kind"] == "export"
    ev3 = log.emit("lumiere.job.done", {"job_id": "r3"}, source="lumiere")                  # already canonical
    assert ev3["data"] == {"job_id": "r3"}
    stored = log.get(ev["id"])
    assert stored["type"] == "lumiere.job.done" and stored["data"]["_orig_type"] == "lumiere.render.done"
    # the log can be asked in either language
    assert {e["id"] for e in log.query(type="lumiere.render.done")} == {ev["id"], ev2["id"]}
    assert {e["id"] for e in log.query(type="lumiere.job.done")} == {ev["id"], ev2["id"], ev3["id"]}
    assert {e["id"] for e in log.query(type="lumiere.render.*")} == {ev["id"], ev2["id"]}
    assert {e["id"] for e in log.query(type="lumiere.job.*")} == {ev["id"], ev2["id"], ev3["id"]}
    assert {e["id"] for e in iter_since(log, 0, ["lumiere.render.done"])} == {ev["id"], ev2["id"]}
    # and the subscribers see the canonical event
    seen = []
    off = log.subscribe(seen.append)
    log.emit("galton.run.done", {"job_id": "g"}, source="galton")
    off()
    assert seen[0]["type"] == "galton.job.done" and seen[0]["data"]["_orig_type"] == "galton.run.done"


def test_rules_written_for_the_old_name_still_match():
    event = {"id": 1, "type": "lumiere.job.done", "source": "lumiere",
             "data": {"kind": "render", "_orig_type": "lumiere.render.done", "job_id": "r1"}}
    assert rule_matches({"when": {"type": "lumiere.render.done"}}, event)                  # the old name
    assert rule_matches({"when": {"type": "lumiere.job.done"}}, event)                     # the canonical one
    assert rule_matches({"when": {"type": "lumiere.render.*"}}, event) and rule_matches({"when": {"type": "lumiere.*"}}, event)
    assert rule_matches({"when": {"type": "lumiere.render.done", "source": "lumiere", "where": {"data.kind": "render"}}}, event)
    assert not rule_matches({"when": {"type": "lumiere.render.failed"}}, event)
    assert not rule_matches({"when": {"type": "lumiere.render.done", "source": "links"}}, event)
    assert not rule_matches({"when": {"type": "lumiere.render.done", "where": {"data.kind": "export"}}}, event)
    plain = {"id": 2, "type": "links.watch.new", "source": "links", "data": {}}
    assert rule_matches({"when": {"type": "links.watch.new"}}, plain) and not rule_matches({"when": {"type": "lumiere.render.done"}}, plain)


def test_the_table_is_extendable(tmp_path):
    assert "kafka.scan.finished" not in EVENT_ALIASES
    try:
        register_alias("kafka.scan.finished", "kafka.job.done", kind="scan")
        log = EventLog(None)
        ev = log.emit("kafka.scan.finished", {"job_id": "s"}, source="kafka")
        assert ev["type"] == "kafka.job.done" and ev["data"]["kind"] == "scan" and ev["data"]["_orig_type"] == "kafka.scan.finished"
    finally:
        EVENT_ALIASES.pop("kafka.scan.finished", None)


def test_an_old_rule_fires_in_a_running_hub_on_the_renamed_event(tmp_path):
    hub = make_hub(tmp_path)
    try:
        for rid, when in (("old", "lumiere.render.done"), ("new", "lumiere.job.done")):
            res = hub.rules.add({"id": rid, "name": rid, "when": {"type": when},
                                 "then": [{"kind": "event", "type": f"fired.{rid}", "data": {"snap": "${event.data.snapshot}"}}], "cooldown_s": 0})
            assert res["ok"], res
        hub.events.emit("lumiere.render.done", {"snapshot": "s1"}, source="lumiere")
        assert wait_for(lambda: hub.events.query(type="fired.old") and hub.events.query(type="fired.new"))
        assert hub.events.query(type="fired.old")[0]["data"]["snap"] == "s1"
        # stored under the canonical name, with the alias defaults
        assert hub.events.query(type="lumiere.job.done")[0]["data"]["kind"] == "render"
    finally:
        hub.close()
