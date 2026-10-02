"""hoard_link.waiting: one cap for every tool that waits on a job."""

from __future__ import annotations

import pytest

from hoard_link import waiting
from tests.commons.jsrun import load_vectors, normalise, python_call, run_js

CASES = load_vectors("waiting")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{c['args']}")
def test_clamp_vectors_python(case):
    assert normalise(python_call(waiting, case)) == case["expect"]


def test_clamp_vectors_node():
    got = run_js("server.js", CASES)
    assert got == [c["expect"] for c in CASES]


def test_cap_is_under_the_mcp_client_limit():
    assert waiting.MAX_WAIT_S == 150 and waiting.MAX_WAIT_S < 180
    assert waiting.clamp_wait(float("nan")) == 0 and waiting.clamp_wait(float("inf")) == 150


class Clock:
    def __init__(self):
        self.t = 100.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_returns_as_soon_as_the_job_is_done():
    clock, states = Clock(), iter(["queued", "running", "running", "done"])
    job = waiting.wait_for(lambda: {"id": "j", "state": next(states), "result": 1}, 60, sleep=clock.sleep, clock=clock.now)
    assert job["state"] == "done" and job["result"] == 1 and "still_running" not in job
    assert job["waited_s"] == pytest.approx(0.75) and clock.sleeps == [0.25, 0.25, 0.25]


@pytest.mark.parametrize("state", waiting.DONE_STATES)
def test_every_end_state_stops_the_wait(state):
    clock = Clock()
    job = waiting.wait_for(lambda: {"state": state}, 10, sleep=clock.sleep, clock=clock.now)
    assert job["state"] == state and clock.sleeps == []


def test_gives_up_with_still_running_and_clamps():
    clock = Clock()
    job = waiting.wait_for(lambda: {"id": "j", "state": "running", "progress": 40}, 99999, sleep=clock.sleep, clock=clock.now)
    assert job["still_running"] is True and job["progress"] == 40 and job["waited_s"] == pytest.approx(150)
    assert sum(clock.sleeps) == pytest.approx(150)


def test_zero_wait_reads_once():
    calls = []
    job = waiting.wait_for(lambda: calls.append(1) or {"state": "running"}, 0, sleep=lambda s: pytest.fail("slept"))
    assert len(calls) == 1 and job["still_running"] is True and job["waited_s"] == 0


def test_does_not_mutate_what_get_job_returned():
    original = {"state": "done"}
    out = waiting.wait_for(lambda: original, 1)
    assert "waited_s" not in original and out is not original


def test_status_key_and_custom_done_states_and_missing():
    clock = Clock()
    assert waiting.wait_for(lambda: {"status": "ok"}, 5, done_states=("ok",), sleep=clock.sleep, clock=clock.now)["status"] == "ok"
    assert waiting.wait_for(lambda: None, 5)["state"] == "missing"


def test_exceptions_from_get_job_propagate():
    def boom():
        raise KeyError("gone")
    with pytest.raises(KeyError):
        waiting.wait_for(boom, 1)
