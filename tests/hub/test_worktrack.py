"""Jobs across apps: the worktrack facet follows <app>.job.* events (canonical and legacy names), joins GPU leases,
marks stale jobs, tells the person about failures, persists to work.json and answers over HTTP and as a tool."""

from __future__ import annotations

import json
import os
import time

import pytest

from hoard_link.hub import tools
from hoard_link.hub.worktrack import WorkFacet

from ._hub_fakes import RecordingNotify, http, make_hub, serve


@pytest.fixture
def hub(tmp_path):
    h = make_hub(tmp_path, extra_apps=["pygmalion"])
    h._facets_by_id["notify"] = RecordingNotify()
    yield h
    h.close()


def work(hub) -> WorkFacet:
    w = hub.facet("worktrack")
    assert w is not None
    return w


def emit(hub, type_, data, source="pygmalion"):
    return hub.events.emit(type_, data, source=source)


def settle(hub):
    assert work(hub).wait_idle(5)


def test_a_job_through_its_whole_life(hub):
    w = work(hub)
    emit(hub, "pygmalion.job.queued", {"job_id": "j1", "title": "Train kettle LoRA", "kind": "train", "url": "http://x/j1"})
    settle(hub)
    [job] = w.active()
    assert (job["app"], job["job_id"], job["title"], job["kind"], job["status"]) == ("pygmalion", "j1", "Train kettle LoRA", "train", "queued")
    emit(hub, "pygmalion.job.started", {"job_id": "j1", "gpu": 0, "eta_s": 600})
    emit(hub, "pygmalion.job.progress", {"job_id": "j1", "progress": 0.25, "eta_s": 450})
    settle(hub)
    [job] = w.active()
    assert job["status"] == "running" and job["progress"] == 0.25 and job["gpu"] == 0 and job["eta_s"] == 450 and job["started_ts"]
    assert job["url"] == "http://x/j1" and job["stale"] is False
    emit(hub, "pygmalion.job.progress", {"job_id": "j1", "progress": 60})                  # a percentage is understood
    settle(hub)
    assert w.active()[0]["progress"] == 0.6
    emit(hub, "pygmalion.job.done", {"job_id": "j1"})
    settle(hub)
    assert w.active() == []
    [done] = w.recent()
    assert done["status"] == "done" and done["progress"] == 1.0 and done["finished_ts"] and done["eta_s"] is None
    assert not hub.events.query(type="work.failed") and hub.facet("notify").sent == []


def test_a_job_without_an_id_is_keyed_by_kind_and_title(hub):
    emit(hub, "pygmalion.job.started", {"title": "Export", "kind": "export"})
    emit(hub, "pygmalion.job.progress", {"title": "Export", "kind": "export", "progress": 0.5})
    settle(hub)
    [job] = work(hub).active()
    assert job["progress"] == 0.5 and job["job_id"] == "export:Export"


def test_legacy_names_are_followed_through_the_aliases(hub):
    w = work(hub)
    emit(hub, "pygmalion.job_queued", {"job_id": "a", "title": "Sprite"})
    emit(hub, "lumiere.render.done", {"job_id": "r1", "title": "Intro cut", "url": "http://l/1"}, source="lumiere")
    emit(hub, "links.media.done", {"job_id": "d1", "title": "Talk.mp4"}, source="links")
    emit(hub, "hypatia.teacher_job.no_model", {"job_id": "t1", "title": "Quiz"}, source="hypatia")
    emit(hub, "hub.backup.done", {"snapshot": "s1", "job_id": "b1"}, source="hub")
    settle(hub)
    assert [j["job_id"] for j in w.active()] == ["a"]
    done = {j["job_id"]: j for j in w.recent()}
    assert (done["r1"]["app"], done["r1"]["kind"], done["r1"]["status"]) == ("lumiere", "render", "done")
    assert (done["d1"]["app"], done["d1"]["kind"]) == ("links", "download")
    assert done["t1"]["status"] == "failed" and done["t1"]["kind"] == "teacher" and done["t1"]["error"]
    assert (done["b1"]["app"], done["b1"]["kind"], done["b1"]["status"]) == ("hub", "backup", "done")
    # a plain hub.job.ran from the scheduler is not a job
    emit(hub, "hub.job.ran", {"job": "x"}, source="hub")
    settle(hub)
    assert len(w.recent(50)) == 4


def test_failure_emits_work_failed_and_notifies_once_per_kind(hub):
    notify = hub.facet("notify")
    emit(hub, "pygmalion.job.started", {"job_id": "j9", "title": "Train", "kind": "train"})
    emit(hub, "pygmalion.job.failed", {"job_id": "j9", "error": "CUDA out of memory", "url": "http://x/j9"})
    settle(hub)
    [ev] = hub.events.query(type="work.failed")
    assert ev["data"] == {"app": "pygmalion", "job_id": "j9", "title": "Train", "error": "CUDA out of memory"} and ev["source"] == "hub"
    [n] = notify.sent
    assert n["priority"] == "high" and n["group"] == "job" and n["dedupe_key"] == "pygmalion:train:failed"
    assert "Train" in n["title"] and "pygmalion" in n["title"] and n["body"] == "CUDA out of memory" and n["url"] == "http://x/j9"
    [failed] = work(hub).recent(failed_only=True)
    assert failed["error"] == "CUDA out of memory" and failed["status"] == "failed"
    # no notify facet: still tracked, nothing raised
    hub._facets_by_id.pop("notify")
    emit(hub, "pygmalion.job.failed", {"job_id": "j10", "title": "Again", "error": "x"})
    settle(hub)
    assert len(work(hub).recent(failed_only=True)) == 2
    # a cancelled job is finished but not a failure
    emit(hub, "pygmalion.job.cancelled", {"job_id": "j11", "title": "Nope"})
    settle(hub)
    assert work(hub).recent(1)[0]["status"] == "cancelled" and len(hub.events.query(type="work.failed")) == 2


def test_gpu_leases_are_joined_by_owner(hub):
    w = work(hub)
    emit(hub, "pygmalion.job.started", {"job_id": "j1", "title": "Train"})
    emit(hub, "hub.lease.granted", {"lease_id": "L1", "owner": "pygmalion", "purpose": "lora", "gpu": 1, "vram_mb": 9000}, source="hub")
    settle(hub)
    [job] = w.active()
    assert job["gpu"] == 1 and job["lease"] == {"gpu": 1, "vram_mb": 9000, "purpose": "lora"}
    emit(hub, "hub.lease.released", {"lease_id": "L1", "owner": "pygmalion"}, source="hub")
    settle(hub)
    assert w.active()[0]["gpu"] is None and "lease" not in w.active()[0]
    # a queued job holds nothing yet
    emit(hub, "pygmalion.job.queued", {"job_id": "q1", "title": "Later"})
    settle(hub)
    emit(hub, "hub.lease.granted", {"lease_id": "L9", "owner": "pygmalion", "gpu": 3}, source="hub")
    settle(hub)
    queued = next(j for j in w.active() if j["job_id"] == "q1")
    assert queued["gpu"] is None and "lease" not in queued
    emit(hub, "hub.lease.released", {"lease_id": "L9", "owner": "pygmalion"}, source="hub")
    settle(hub)
    # an owner that is not the job's app is not joined; "app:detail" owners are
    emit(hub, "hub.lease.granted", {"lease_id": "L2", "owner": "someone-else", "gpu": 0}, source="hub")
    emit(hub, "hub.lease.granted", {"lease_id": "L3", "owner": "pygmalion:worker-2", "gpu": 2}, source="hub")
    settle(hub)
    assert w.active()[0]["gpu"] == 2


def test_stale_jobs_are_marked_and_left_out_of_active(hub):
    w = work(hub)
    emit(hub, "pygmalion.job.started", {"job_id": "old", "title": "Forgotten"})
    emit(hub, "pygmalion.job.started", {"job_id": "new", "title": "Fresh"})
    settle(hub)
    real = time.time()
    w._active["pygmalion:old"]["updated_ts"] = real - 7 * 3600
    assert [j["job_id"] for j in w.active()] == ["new"]
    both = {j["job_id"]: j for j in w.active(include_stale=True)}
    assert both["old"]["stale"] is True and both["new"]["stale"] is False
    assert w.stats()["stale"] == 1 and w.stats()["active"] == 1
    # a week without news archives it
    w._now = lambda: real + 8 * 86400
    assert w.active(include_stale=True) == [] and w.recent()[0]["status"] == "stale"


def test_persistence_to_work_json(hub):
    w = work(hub)
    emit(hub, "pygmalion.job.started", {"job_id": "keep", "title": "Running one"})
    emit(hub, "pygmalion.job.done", {"job_id": "fin", "title": "Finished one"})
    settle(hub)
    path = os.path.join(hub.config.data_dir, "work.json")
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(path):
        time.sleep(0.05)
    raw = json.loads(open(path, encoding="utf-8").read())
    assert [j["job_id"] for j in raw["active"]] == ["keep"] and [j["job_id"] for j in raw["finished"]] == ["fin"]
    again = WorkFacet(hub)
    assert [j["job_id"] for j in again.active()] == ["keep"] and again.recent()[0]["job_id"] == "fin"
    # only the last 500 finished are kept
    for i in range(510):
        w.handle_event({"id": i, "ts": time.time(), "type": "pygmalion.job.done", "data": {"job_id": f"n{i}", "title": "x"}})
    assert len(w.recent(1000)) == 500 and len(w._finished) == 500


def test_http_and_tool(hub):
    emit(hub, "pygmalion.job.started", {"job_id": "j1", "title": "Train", "kind": "train", "progress": 0.1})
    emit(hub, "lumiere.render.done", {"job_id": "r1", "title": "Cut"}, source="lumiere")
    emit(hub, "lumiere.render.failed", {"job_id": "r2", "title": "Bad cut", "error": "codec"}, source="lumiere")
    settle(hub)
    server = serve(hub)
    try:
        url = hub.config.url
        status, body = http(url + "/api/work?active=1")
        assert status == 200 and [j["job_id"] for j in body["active"]] == ["j1"] and "finished" not in body and body["counts"]["active"] == 1
        status, body = http(url + "/api/work")
        assert {j["job_id"] for j in body["finished"]} == {"r1", "r2"}
        status, body = http(url + "/api/work?failed=1&app=lumiere")
        assert [j["job_id"] for j in body["finished"]] == ["r2"]
        status, body = http(url + "/api/work?app=nobody")
        assert body["active"] == [] and body["finished"] == []
        status, body = http(url + "/api/work/stats")
        assert status == 200 and body["active"] == 1 and body["running"] == 1 and body["done_24h"] == 1 and body["failed_24h"] == 1
        assert body["by_app"]["lumiere"]["failed_24h"] == 1
    finally:
        server.shutdown()
    tool = WorkFacet.tools()[0]
    assert tool["name"] == "hub_work" and tool["annotations"]["readOnlyHint"] and len(tool["description"].splitlines()[0]) <= 110
    res = tools.call(hub, "hub_work", {})
    assert res["ok"] and [j["job_id"] for j in res["active"]] == ["j1"] and "finished" not in res
    res = tools.call(hub, "hub_work", {"active": False, "app": "lumiere"})
    assert {j["job_id"] for j in res["finished"]} == {"r1", "r2"} and res["active"] == []


def test_the_emitter_is_never_blocked(hub):
    w = work(hub)
    t0 = time.monotonic()
    for i in range(300):
        emit(hub, "pygmalion.job.progress", {"job_id": "burst", "progress": i / 300})
    assert time.monotonic() - t0 < 5
    settle(hub)
    assert w.active()[0]["progress"] > 0.9
