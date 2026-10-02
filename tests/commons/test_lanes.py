"""hoard_link.lanes: the lane scheduler (dedupe, periodic jobs, persisted last runs) and the persisted JobQueue."""

from __future__ import annotations

import json
import threading
import time

import pytest

from hoard_link.lanes import Job, JobCancelled, JobCtx, JobNotFound, JobQueue, LaneScheduler, WaitingForResources, jobs_schema
from hoard_link.sqlkit import Database


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ================================================================== LaneScheduler

def test_run_now_inline_without_started_lanes_and_errors_propagate():
    s = LaneScheduler({"io": 1})
    s.register(Job("ok", fn=lambda: {"n": 1}, lane="io"))
    s.register(Job("bad", fn=lambda: 1 / 0, lane="io"))
    assert s.run_now("ok") == {"n": 1}
    with pytest.raises(ZeroDivisionError):
        s.run_now("bad")
    with pytest.raises(ValueError):
        s.run_now("unknown")
    assert s.jobs_done == 0                                  # inline runs do not touch the lanes
    assert s.status()["jobs"]["bad"]["failures"] == 1 and "ZeroDivisionError" in s.status()["jobs"]["bad"]["last_error"]


def test_run_now_through_started_lanes():
    s = LaneScheduler({"io": 1}, tick_s=60)
    s.register(Job("work", fn=lambda: threading.current_thread().name, lane="io"))
    s.start()
    try:
        assert s.run_now("work", timeout=5).startswith("lanes-io-")
        assert s.status()["running"] is True and s.status()["jobs_done"] == 1
    finally:
        s.stop()
    assert s.status()["running"] is False


def test_run_now_error_becomes_runtime_error_and_calls_on_error():
    seen = []
    s = LaneScheduler({"io": 1}, on_error=lambda job, exc: seen.append((job.kind, type(exc).__name__)), tick_s=60)
    s.register(Job("boom", fn=lambda: int("x"), lane="io"))
    s.start()
    try:
        with pytest.raises(RuntimeError, match="ValueError"):
            s.run_now("boom", timeout=5)
        assert seen == [("boom", "ValueError")]
        s.register(Job("fine", fn=lambda: "still alive", lane="io"))
        assert s.run_now("fine", timeout=5) == "still alive"      # a failing job never stops its lane
    finally:
        s.stop()


def test_on_error_failing_is_ignored():
    s = LaneScheduler({"io": 1}, on_error=lambda *a: 1 / 0, tick_s=60)
    s.register(Job("boom", fn=lambda: 1 / 0, lane="io"))
    s.start()
    try:
        with pytest.raises(RuntimeError):
            s.run_now("boom", timeout=5)
    finally:
        s.stop()


def test_dedupe_by_key_and_already_queued_note():
    gate = threading.Event()
    s = LaneScheduler({"io": 1}, tick_s=60)
    s.register(Job("scan", fn=lambda: gate.wait(5) and "done", lane="io"))
    s.start()
    try:
        first = s.submit(Job("scan", fn=lambda: gate.wait(5) and "done", lane="io"))
        assert first is not None
        assert s.submit(Job("scan", fn=lambda: None, lane="io")) is None            # same key: no-op
        assert s.run_now("scan", timeout=1) == {"queued": True, "note": "already queued"}
        other = s.submit(Job("scan", key="scan:folder-b", fn=lambda: "b", lane="io"))   # another key is a different job
        assert other is not None
        gate.set()
        assert first.done.wait(5) and first.result == "done"
        assert other.done.wait(5) and other.result == "b"
        assert s.submit(Job("scan", fn=lambda: "again", lane="io")) is not None         # free again once it finished
    finally:
        gate.set()
        s.stop()


def test_run_now_timeout_returns_still_running():
    gate = threading.Event()
    s = LaneScheduler({"io": 1}, tick_s=60)
    s.register(Job("slow", fn=lambda: gate.wait(5), lane="io"))
    s.start()
    try:
        out = s.run_now("slow", timeout=0.1)
        assert out["queued"] is True and "still running" in out["note"]
    finally:
        gate.set()
        s.stop()


def test_lane_concurrency_follows_workers():
    live, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.15)
        with lock:
            live -= 1

    for workers, expected in ((1, 1), (3, 3)):
        live = peak = 0
        s = LaneScheduler({"io": workers}, tick_s=60)
        s.start()
        jobs = [s.submit(Job("w", key=f"w{i}", fn=work, lane="io")) for i in range(workers * 2)]
        assert all(j.done.wait(5) for j in jobs)
        s.stop()
        assert peak == expected


def test_lanes_do_not_block_each_other():
    gate = threading.Event()
    s = LaneScheduler({"slow": 1, "fast": 1}, tick_s=60)
    s.start()
    try:
        s.submit(Job("hold", fn=lambda: gate.wait(5), lane="slow"))
        quick = s.submit(Job("quick", fn=lambda: "q", lane="fast"))
        assert quick.done.wait(2) and quick.result == "q"
    finally:
        gate.set()
        s.stop()


def test_unknown_lane_and_missing_fn_are_rejected():
    s = LaneScheduler({"io": 1})
    with pytest.raises(ValueError):
        s.submit(Job("x", fn=lambda: 1, lane="nope"))
    with pytest.raises(ValueError):
        s.register(Job("x", fn=lambda: 1, lane="nope"))
    with pytest.raises(ValueError):
        s.submit(Job("x", lane="io"))
    with pytest.raises(ValueError):
        LaneScheduler({})


def test_periodic_due_logic_with_an_injected_clock():
    clock = Clock()
    s = LaneScheduler({"io": 1}, clock=clock)
    ran = []
    s.register(Job("every_min", fn=lambda: ran.append("m"), lane="io", every_s=60))
    s.register(Job("delayed", fn=lambda: ran.append("d"), lane="io", every_s=3600, first_delay_s=300))
    s.register(Job("manual_only", fn=lambda: ran.append("x"), lane="io"))
    assert s.enqueue_due() == 1                                  # never ran: due at once; "delayed" waits for its first delay
    assert not s.is_due(s._registered["delayed"]) and not s.is_due(s._registered["manual_only"])
    s._queues["io"].get_nowait()                                  # drop it so we can inspect later enqueues
    s._pending.clear()
    clock.advance(299)
    assert s.is_due(s._registered["delayed"]) is False
    clock.advance(2)
    assert s.is_due(s._registered["delayed"]) is True


def test_periodic_jobs_run_and_are_not_requeued_before_they_are_due():
    clock = Clock()
    s = LaneScheduler({"io": 1}, clock=clock)
    runs = []
    s.register(Job("tick", fn=lambda: runs.append(clock()), lane="io", every_s=60))
    # inline path: the registry plus run_now sets last, so it is not due again until 60 s later
    s.run_now("tick")
    assert s.is_due(s._registered["tick"]) is False
    clock.advance(59.9)
    assert s.enqueue_due() == 0
    clock.advance(0.2)
    assert s.enqueue_due() == 1


def test_enabled_predicate_and_dynamic_interval():
    clock = Clock()
    s = LaneScheduler({"io": 1}, clock=clock)
    on = {"v": False}
    interval = {"v": 600}
    s.register(Job("mail", fn=lambda: "m", lane="io", every_s=lambda: interval["v"], enabled=lambda: on["v"]))
    assert s.enqueue_due() == 0                                  # disabled
    on["v"] = True
    assert s.enqueue_due() == 1
    s._pending.clear()
    s.last["mail"] = clock()
    clock.advance(100)
    interval["v"] = 60                                           # the setting changed at runtime: due sooner
    assert s.enqueue_due() == 1
    s.register(Job("broken", fn=lambda: 1, lane="io", every_s=lambda: 1 / 0))
    s.register(Job("broken2", fn=lambda: 1, lane="io", every_s=60, enabled=lambda: 1 / 0))
    assert s.enqueue_due() == 0                                  # broken getters are skipped, never raised


def test_ticker_enqueues_due_jobs_and_respects_paused_and_enabled():
    ran = threading.Event()
    paused = {"v": True}
    s = LaneScheduler({"io": 1}, tick_s=0.05, paused=lambda: paused["v"])
    s.register(Job("auto", fn=ran.set, lane="io", every_s=3600))
    s.start()
    try:
        time.sleep(0.3)
        assert not ran.is_set() and s.last_tick_ts is not None          # paused: the tick runs, nothing is queued
        s.run_now("auto", timeout=5)                                   # but a person asking is never held back
        assert ran.is_set()
    finally:
        s.stop()

    ran2 = threading.Event()
    s2 = LaneScheduler({"io": 1}, tick_s=0.05)
    s2.register(Job("auto", fn=ran2.set, lane="io", every_s=3600))
    s2.enabled = False
    s2.start()
    try:
        time.sleep(0.25)
        assert not ran2.is_set()
        s2.enabled = True
        assert ran2.wait(3)
    finally:
        s2.stop()


def test_last_runs_persist_so_a_restart_does_not_run_everything(tmp_path):
    state = tmp_path / "sched" / "state.json"
    clock = Clock()
    s = LaneScheduler({"io": 1}, state_path=state, clock=clock)
    s.register(Job("a", fn=lambda: 1, lane="io", every_s=600))
    s.register(Job("b", fn=lambda: 2, lane="io", every_s=600))
    s.run_now("a")
    saved = json.loads(state.read_text())
    assert saved == {"last": {"a": clock.t}}

    clock.advance(120)                                            # the app restarts two minutes later
    s2 = LaneScheduler({"io": 1}, state_path=state, clock=clock)
    s2.register(Job("a", fn=lambda: 1, lane="io", every_s=600))
    s2.register(Job("b", fn=lambda: 2, lane="io", every_s=600, first_delay_s=30))
    assert s2.last == {"a": 1_000_000.0}
    assert s2.is_due(s2._registered["a"]) is False                # ran two minutes ago: not due
    assert s2.is_due(s2._registered["b"]) is False                # never ran: waits its first delay, measured from this start
    clock.advance(31)
    assert s2.is_due(s2._registered["b"]) is True
    clock.advance(500)
    assert s2.is_due(s2._registered["a"]) is True


def test_state_file_survives_corruption_and_future_timestamps(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json")
    assert LaneScheduler({"io": 1}, state_path=state).last == {}
    clock = Clock()
    state.write_text(json.dumps({"last": {"future": clock.t + 99999, "ok": clock.t - 5, "junk": "x", "flag": True}}))
    s = LaneScheduler({"io": 1}, state_path=state, clock=clock)
    assert s.last == {"future": clock.t, "ok": clock.t - 5}      # a clock that went back must not postpone a job forever


def test_unwritable_state_file_never_breaks_a_job(tmp_path, monkeypatch):
    from hoard_link import lanes
    monkeypatch.setattr(lanes.atomic, "write_json_atomic", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    s = LaneScheduler({"io": 1}, state_path=tmp_path / "s.json")
    s.register(Job("a", fn=lambda: "fine", lane="io"))
    assert s.run_now("a") == "fine"


def test_status_shape():
    s = LaneScheduler({"ingest": 2, "reminders": 1}, tick_s=60)
    s.register(Job("folders", fn=lambda: 1, lane="ingest", every_s=300))
    st = s.status()
    assert st["lanes"] == {"ingest": {"workers": 2, "queue": 0, "current": []}, "reminders": {"workers": 1, "queue": 0, "current": []}}
    assert st["jobs"]["folders"]["every_s"] == 300 and st["jobs"]["folders"]["last_ts"] is None
    assert st["enabled"] is True and st["running"] is False and st["paused"] is False
    gate = threading.Event()
    s.start()
    try:
        s.submit(Job("hold", fn=lambda: gate.wait(5), lane="ingest", reason="manual"))
        assert wait_until(lambda: s.status()["lanes"]["ingest"]["current"] != [])
        assert s.status()["lanes"]["ingest"]["current"] == [{"kind": "hold", "key": "hold", "reason": "manual"}]
    finally:
        gate.set()
        s.stop()


def test_start_is_idempotent_and_stop_returns_quickly():
    s = LaneScheduler({"io": 2}, tick_s=60)
    s.start()
    n = len(s._threads)
    s.start()
    assert len(s._threads) == n
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 3


# ================================================================== JobQueue

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "jobs.db")
    yield d
    d.close()


def test_schema_sql_is_reusable_in_a_migration(tmp_path):
    d = Database(tmp_path / "m.db", migrations=[jobs_schema("jobs")])
    JobQueue(d)                                              # IF NOT EXISTS: no conflict with the app's migration
    assert d.scalar("SELECT COUNT(*) FROM jobs") == 0
    d.close()
    with pytest.raises(ValueError):
        jobs_schema("bad name; --")
    with pytest.raises(ValueError):
        JobQueue(Database(":memory:"), table="x y")


def test_inline_queue_runs_a_job_to_done(db):
    events, done = [], []
    q = JobQueue(db, inline=True, on_event=lambda name, job: events.append(name), on_done=done.append)

    def work(ctx: JobCtx):
        ctx.progress(50, "half way", force=True)
        return {"echo": ctx.payload["x"], "attempt": ctx.attempt}

    q.register("echo", work)
    jid = q.submit("echo", {"x": "ñ"}, label="my job")
    job = q.get(jid)
    assert jid.startswith("job_") and job["state"] == "done" and job["result"] == {"echo": "ñ", "attempt": 1}
    assert job["progress"] == 100 and job["label"] == "my job" and job["payload"] == {"x": "ñ"} and job["error"] == ""
    assert job["started_ts"] and job["finished_ts"] and job["elapsed_s"] is not None
    assert events == ["queued", "started", "progress", "done"] and [d["state"] for d in done] == ["done"]


def test_a_handler_that_raises_ends_in_error_and_other_jobs_go_on(db):
    q = JobQueue(db, inline=True)
    q.register("bad", lambda ctx: 1 / 0)
    q.register("good", lambda ctx: None)
    bad, good = q.submit("bad"), q.submit("good")
    assert q.get(bad)["state"] == "error" and "ZeroDivisionError" in q.get(bad)["error"]
    assert q.get(good)["state"] == "done" and q.get(good)["result"] == {}


def test_unknown_kind_and_lane_and_missing_job(db):
    q = JobQueue(db, inline=True)
    with pytest.raises(ValueError):
        q.submit("nope")
    with pytest.raises(ValueError):
        q.register("x", lambda c: 1, lane="nope")
    with pytest.raises(ValueError):
        q.register("x", lambda c: 1, on_restart="sometimes")
    q.register("x", lambda c: 1)
    with pytest.raises(ValueError):
        q.submit("x", lane="nope")
    with pytest.raises(JobNotFound):
        q.get("job_missing")
    assert isinstance(JobNotFound("x"), KeyError)


def test_hooks_that_fail_never_reach_the_job(db):
    q = JobQueue(db, inline=True, on_event=lambda *a: 1 / 0, on_done=lambda *a: 1 / 0)
    q.register("x", lambda c: {"ok": True})
    jid = q.submit("x")
    assert q.get(jid)["state"] == "done"


def test_dedupe_key_returns_the_active_job(db):
    gate = threading.Event()
    q = JobQueue(db, {"work": 1})
    q.register("scan", lambda ctx: gate.wait(5) and {"ok": 1})
    q.start()
    try:
        a = q.submit("scan", dedupe_key="folder-1")
        b = q.submit("scan", dedupe_key="folder-1")
        c = q.submit("scan", dedupe_key="folder-2")
        assert a == b and a != c
        gate.set()
        assert q.wait(a, 5)["state"] == "done"
        d = q.submit("scan", dedupe_key="folder-1")             # finished jobs do not block a new one
        assert d != a
        q.wait(d, 5)
    finally:
        gate.set()
        q.stop()


def test_ids_sort_in_creation_order(db):
    q = JobQueue(db, inline=True)
    q.register("x", lambda c: None)
    made = [q.submit("x") for _ in range(50)]
    assert made == sorted(made) and [j["id"] for j in q.list(limit=50)] == made[::-1]


def test_threaded_queue_runs_jobs_in_lanes(db):
    q = JobQueue(db, {"work": 2, "render": 1})
    seen = []
    q.register("a", lambda ctx: seen.append(("a", threading.current_thread().name)) or {"k": 1})
    q.register("r", lambda ctx: seen.append(("r", threading.current_thread().name)) or {"k": 2}, lane="render")
    q.start()
    try:
        ja, jr = q.submit("a"), q.submit("r")
        assert q.wait(ja, 5)["state"] == "done" and q.wait(jr, 5)["state"] == "done"
        assert dict(seen)["a"].startswith("jobs-work-") and dict(seen)["r"] == "jobs-render-0"
    finally:
        q.stop()


def test_progress_is_throttled_and_clamped(db):
    q = JobQueue(db, inline=True)
    values = []

    def work(ctx):
        for i in range(0, 101, 10):
            ctx.progress(i, f"step {i}")
            values.append(q.get(ctx.id)["progress"])
        ctx.progress(-5, force=True)
        values.append(q.get(ctx.id)["progress"])
        ctx.progress(500, force=True)
        values.append(q.get(ctx.id)["progress"])

    q.register("p", work)
    q.submit("p")
    assert values[0] == 0 and values[1] == 0                      # throttled: the second write within 0.4 s was skipped
    assert values[-2] == 0 and values[-1] == 100


def test_cancel_a_queued_job(db):
    gate = threading.Event()
    q = JobQueue(db, {"work": 1})
    q.register("hold", lambda ctx: gate.wait(5))
    q.register("later", lambda ctx: {"ran": True})
    q.start()
    try:
        first = q.submit("hold")
        assert wait_until(lambda: q.get(first)["state"] == "running")
        second = q.submit("later")
        cancelled = q.cancel(second)
        assert cancelled["state"] == "cancelled" and cancelled["cancel_requested"] is True
        gate.set()
        assert q.wait(first, 5)["state"] == "done"
        time.sleep(0.2)
        assert q.get(second)["state"] == "cancelled"              # the worker skipped it
        assert q.cancel(first)["state"] == "done"                 # finished jobs are left alone
    finally:
        gate.set()
        q.stop()


def test_cancel_a_running_job_cooperatively(db):
    started = threading.Event()
    q = JobQueue(db, {"work": 1})

    def long(ctx):
        started.set()
        for _ in range(500):
            ctx.check()
            time.sleep(0.01)
        return {"finished": True}

    def long_progress(ctx):
        started.set()
        for i in range(500):
            ctx.progress(i / 5, force=True)                       # progress() raises too
            time.sleep(0.01)

    q.register("long", long)
    q.register("longp", long_progress)
    q.start()
    try:
        for kind in ("long", "longp"):
            started.clear()
            jid = q.submit(kind)
            assert started.wait(5)
            assert q.cancel(jid)["cancel_requested"] is True
            job = q.wait(jid, 5)
            assert job["state"] == "cancelled" and job["error"] == "Cancelled." and "finished" not in job["result"]
    finally:
        q.stop()


def test_shutdown_marks_running_jobs_interrupted(db):
    started = threading.Event()
    q = JobQueue(db, {"work": 1})

    def long(ctx):
        started.set()
        while True:
            ctx.check()
            time.sleep(0.01)

    q.register("long", long)
    q.start()
    jid = q.submit("long")
    assert started.wait(5)
    q.stop()
    job = q.get(jid)
    assert job["state"] == "interrupted" and "stopping" in job["error"]


def test_requeue_interrupted_on_start_per_policy(db):
    ran = []
    q = JobQueue(db, {"work": 1})
    q.register("idem", lambda ctx: ran.append(("idem", ctx.attempt)) or {"again": True}, on_restart="requeue")
    q.register("heavy", lambda ctx: ran.append(("heavy", ctx.attempt)))
    # simulate a crashed previous run: rows left in `running`, `waiting` and `queued`
    for jid, kind, state in (("job_a", "idem", "running"), ("job_b", "heavy", "running"), ("job_c", "heavy", "waiting"), ("job_d", "heavy", "queued")):
        db.execute("INSERT INTO jobs(id, kind, lane, state, created_ts, started_ts) VALUES (?, ?, 'work', ?, ?, 1)", (jid, kind, state, time.time()))
    assert q.requeue_interrupted() == 2
    assert q.get("job_a")["state"] == "queued" and q.get("job_b")["state"] == "interrupted"
    assert "stopped" in q.get("job_b")["error"] and q.get("job_b")["finished_ts"]
    assert q.get("job_c")["state"] == "queued"
    q2 = JobQueue(db, {"work": 1})
    q2.register("idem", lambda ctx: ran.append(("idem", ctx.attempt)) or {"again": True}, on_restart="requeue")
    q2.register("heavy", lambda ctx: ran.append(("heavy", ctx.attempt)))
    q2.start()                                                   # start() requeues, then runs everything that is queued
    try:
        assert wait_until(lambda: all(q2.get(j)["state"] == "done" for j in ("job_a", "job_c", "job_d")))
        assert q2.get("job_b")["state"] == "interrupted"
        assert ("idem", 1) in ran
    finally:
        q2.stop()


def test_start_returns_how_many_were_running(db):
    q = JobQueue(db, {"work": 1})
    db.execute("INSERT INTO jobs(id, kind, lane, state, created_ts) VALUES ('job_x', 'k', 'work', 'running', 1)")
    q.register("k", lambda ctx: None)
    assert q.start() == 1
    q.stop()


def test_waiting_for_resources_requeues_later_then_gives_up(db):
    calls = []
    clock = {"t": 1000.0}
    q = JobQueue(db, inline=True, clock=lambda: clock["t"], max_waiting_s=100)

    def needs_gpu(ctx):
        calls.append(ctx.attempt)
        if len(calls) < 3:
            raise WaitingForResources("only 2 GB of VRAM free", retry_in_s=0.05)
        return {"ran_on": "gpu"}

    q.register("render", needs_gpu)
    jid = q.submit("render")
    job = q.get(jid)
    assert job["state"] == "waiting" and job["message"] == "only 2 GB of VRAM free"
    assert wait_until(lambda: q.get(jid)["state"] == "done")
    assert calls == [1, 2, 3] and q.get(jid)["result"] == {"ran_on": "gpu"}

    calls.clear()
    q2 = JobQueue(db, inline=True, clock=lambda: clock["t"], max_waiting_s=100, table="jobs")
    q2.register("render", lambda ctx: (_ for _ in ()).throw(WaitingForResources("busy", retry_in_s=0.02)))
    jid = q2.submit("render")
    assert q2.get(jid)["state"] == "waiting"
    clock["t"] += 500                                            # it has been waiting for too long
    assert wait_until(lambda: q2.get(jid)["state"] == "error")
    assert "Gave up waiting for resources: busy" in q2.get(jid)["error"]


def test_cancel_a_waiting_job_and_stop_drops_timers(db):
    q = JobQueue(db, inline=True)
    q.register("w", lambda ctx: (_ for _ in ()).throw(WaitingForResources("later", retry_in_s=30)))
    jid = q.submit("w")
    assert q.get(jid)["state"] == "waiting"
    assert q.cancel(jid)["state"] == "cancelled"
    q.stop()
    assert q._timers == set()
    jid2 = JobQueue(db, inline=True)
    jid2.register("w", lambda ctx: (_ for _ in ()).throw(WaitingForResources("later", retry_in_s=30)))
    waiting = jid2.submit("w")
    jid2.stop()                                                   # a stopped queue leaves the job `waiting` for the next start
    assert jid2.get(waiting)["state"] == "waiting"
    q3 = JobQueue(db, inline=True)
    q3.register("w", lambda ctx: {"ran": True})
    q3.start()                                                    # inline start runs what was waiting
    assert q3.get(waiting)["state"] == "done"


def test_list_filters_and_purge(db):
    clock = {"t": 1000.0}
    q = JobQueue(db, inline=True, clock=lambda: clock["t"])
    q.register("a", lambda ctx: None)
    q.register("b", lambda ctx: 1 / 0)
    ids = [q.submit("a"), q.submit("b"), q.submit("a")]
    assert [j["id"] for j in q.list(kind="a")] == [ids[2], ids[0]]
    assert [j["id"] for j in q.list(state="error")] == [ids[1]]
    assert q.list(state="active") == []
    assert len(q.list(limit=2)) == 2
    clock["t"] += 10 * 86400
    fresh = q.submit("a")
    assert q.purge_finished(7 * 86400) == 3
    assert [j["id"] for j in q.list()] == [fresh]


def test_wait_uses_the_shared_helper_and_reports_still_running(db):
    gate = threading.Event()
    q = JobQueue(db, {"work": 1})
    q.register("slow", lambda ctx: gate.wait(5) and {"x": 1})
    q.start()
    try:
        jid = q.submit("slow")
        out = q.wait(jid, 0.3, poll=0.05)
        assert out["still_running"] is True and out["state"] in ("queued", "running") and out["waited_s"] >= 0.3
        gate.set()
        out = q.wait(jid, 5, poll=0.02)
        assert out["state"] == "done" and "still_running" not in out
    finally:
        gate.set()
        q.stop()


def test_jobs_survive_reopening_the_database(tmp_path):
    path = tmp_path / "persist.db"
    d = Database(path)
    q = JobQueue(d, inline=True)
    q.register("x", lambda ctx: {"v": [1, 2, "ñ"]})
    jid = q.submit("x", {"in": 1})
    d.close()
    d2 = Database(path)
    q2 = JobQueue(d2, inline=True)
    assert q2.get(jid)["result"] == {"v": [1, 2, "ñ"]} and q2.get(jid)["payload"] == {"in": 1}
    d2.close()


def test_handler_without_registration_after_restart_ends_in_error(db):
    q = JobQueue(db, inline=True)
    db.execute("INSERT INTO jobs(id, kind, lane, state, created_ts) VALUES ('job_old', 'removed_kind', 'work', 'queued', 1)")
    q.start()
    assert q.get("job_old")["state"] == "error" and "No handler" in q.get("job_old")["error"]
