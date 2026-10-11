"""LiveFeed: cached within the TTL, fresh after it, stale when the source fails, single flight, disk survival."""

from __future__ import annotations

import threading
import time

import pytest

from hoard_link.livefeed import LiveFeed, LiveFeedError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_cached_then_fresh_then_stale(tmp_path):
    clock, calls = Clock(), []

    def fetch():
        calls.append(clock.now)
        return {"n": len(calls)}

    feed = LiveFeed("demo", ttl_s=60, disk_dir=tmp_path, clock=clock, sleep=lambda s: None)
    first = feed.get("k", fetch)
    assert first.freshness == "fresh" and first.data == {"n": 1} and first.age_s == 0
    clock.now += 30
    cached = feed.get("k", fetch)
    assert cached.freshness == "cached" and cached.age_s == 30 and len(calls) == 1
    clock.now += 40
    assert feed.get("k", fetch).data == {"n": 2}

    def broken():
        raise TimeoutError("source down")

    clock.now += 100
    stale = feed.get("k", broken)
    assert stale.freshness == "stale" and stale.data == {"n": 2} and stale.age_s == 100
    assert "TimeoutError: source down" in stale.error and stale.attempts == 3
    assert stale.meta()["freshness"] == "stale" and "error" in stale.meta()


def test_retries_with_growing_pauses_then_succeeds():
    pauses, tries = [], []

    def flaky():
        tries.append(1)
        if len(tries) < 3:
            raise ConnectionError("blip")
        return 7

    got = LiveFeed("demo", retries=2, backoff_s=0.5, sleep=pauses.append).get("k", flaky)
    assert got.data == 7 and got.attempts == 3 and pauses == [0.5, 1.0]


def test_no_answer_to_fall_back_on_raises(tmp_path):
    feed = LiveFeed("demo", retries=0, disk_dir=tmp_path)
    with pytest.raises(LiveFeedError, match="demo: ValueError: nope"):
        feed.get("k", lambda: (_ for _ in ()).throw(ValueError("nope")))


def test_too_old_to_serve_raises():
    clock = Clock()
    feed = LiveFeed("demo", ttl_s=10, max_stale_s=100, retries=0, clock=clock)
    feed.get("k", lambda: 1)
    clock.now += 500
    with pytest.raises(LiveFeedError, match="too old"):
        feed.get("k", lambda: 1 / 0)


def test_disk_copy_serves_a_new_process(tmp_path):
    clock = Clock()
    LiveFeed("demo", ttl_s=10, disk_dir=tmp_path, clock=clock).get("quakes", lambda: [1, 2, 3])
    clock.now += 3600
    again = LiveFeed("demo", ttl_s=10, retries=0, disk_dir=tmp_path, clock=clock).get("quakes", lambda: 1 / 0)
    assert again.freshness == "stale" and again.data == [1, 2, 3] and again.age_s == 3600


def test_single_flight_per_key():
    started, calls = threading.Event(), []

    def slow():
        calls.append(1)
        started.set()
        time.sleep(0.2)
        return "x"

    feed = LiveFeed("demo", ttl_s=60)
    out = []
    threads = [threading.Thread(target=lambda: out.append(feed.get("k", slow))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and sorted(r.freshness for r in out) == ["cached"] * 4 + ["fresh"]


def test_force_and_forget(tmp_path):
    n = []
    feed = LiveFeed("demo", ttl_s=60, disk_dir=tmp_path)
    feed.get("k", lambda: n.append(1) or len(n))
    assert feed.get("k", lambda: n.append(1) or len(n), force=True).data == 2
    feed.forget("k")
    assert not list((tmp_path / "demo").glob("*.json"))
