"""hoard_link.ids: ULIDs that stay ordered and unique when the clock does not move."""

from __future__ import annotations

import threading
import time

import pytest

from hoard_link import ids
from tests.commons.jsrun import load_vectors, normalise, python_call, run_js

CASES = load_vectors("ids")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{str(c['args'])[:40]}")
def test_vectors_python(case):
    assert normalise(python_call(ids, case)) == case["expect"]


def test_vectors_node():
    got = run_js("server.js", CASES)
    bad = [(c["args"], g, c["expect"]) for c, g in zip(CASES, got) if g != c["expect"]]
    assert not bad, bad


def test_format():
    u = ids.new_ulid()
    assert len(u) == 26 and ids.is_ulid(u) and u == u.upper()
    assert abs(ids.id_time(u) - time.time()) < 5


def test_explicit_time_is_exact():
    u = ids.new_ulid(1_700_000_000.123)
    assert ids.id_time(u) == pytest.approx(1_700_000_000.123, abs=0.001)
    assert u[:10] == ids.new_ulid(1_700_000_000.123)[:10]
    assert ids.new_ulid(0)[:10] == "0" * 10


def test_monotonic_when_the_clock_does_not_advance(monkeypatch):
    monkeypatch.setattr(ids.time, "time", lambda: 1_700_000_000.0)
    made = [ids.new_ulid() for _ in range(2000)]
    assert made == sorted(made) and len(set(made)) == 2000
    assert len({u[:10] for u in made}) <= 2          # all in the same millisecond (or rolled once)


def test_monotonic_when_the_clock_goes_back(monkeypatch):
    clock = {"t": 1_700_000_100.0}
    monkeypatch.setattr(ids.time, "time", lambda: clock["t"])
    a = ids.new_ulid()
    clock["t"] -= 50                                  # NTP step backwards
    b = ids.new_ulid()
    clock["t"] += 200
    c = ids.new_ulid()
    assert a < b < c


def test_counter_overflow_moves_to_next_millisecond(monkeypatch):
    monkeypatch.setattr(ids.time, "time", lambda: 1_700_000_000.0)
    first = ids.new_ulid()
    ids._last_rand = ids._RAND_MAX                    # the next one would overflow 80 bits
    second = ids.new_ulid()
    assert second > first and second[:10] != first[:10]


def test_unique_and_ordered_across_threads():
    out, lock = [], threading.Lock()

    def go():
        local = [ids.new_ulid() for _ in range(500)]
        with lock:
            out.append(local)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flat = [u for chunk in out for u in chunk]
    assert len(set(flat)) == len(flat) == 4000
    for chunk in out:                                  # each thread saw increasing ids
        assert chunk == sorted(chunk)


def test_new_id_and_short_id():
    a = ids.new_id("job")
    assert a.startswith("job_") and ids.is_ulid(a[4:]) and ids.id_time(a) is not None
    assert ids.new_id("x", sep="-").startswith("x-")
    assert ids.is_ulid(ids.new_id())
    assert ids.new_id("a") < ids.new_id("a")
    for n in (1, 7, 8, 16):
        s = ids.short_id(n)
        assert len(s) == n and all(c in "0123456789abcdef" for c in s)
    assert ids.short_id() != ids.short_id()
