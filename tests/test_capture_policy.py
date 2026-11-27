"""The screen-memory writer and the capture producer share one durable fence."""

from dataclasses import dataclass
from threading import Event, Thread
import sqlite3

import pytest

from hoard_link import capture_policy


MONITOR = "screen-test"
LAYOUT = "f" * 64


@dataclass
class Snapshot:
    revision: int
    private: bool
    excluded: bool
    audio_excluded: bool


@pytest.fixture(params=("DELETE", "WAL"))
def database(tmp_path, request):
    path = tmp_path / "argus-hoard.db"
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"PRAGMA journal_mode = {request.param}").fetchone()[0].upper() == request.param
        conn.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE exclusions (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                pattern TEXT NOT NULL, enabled INTEGER NOT NULL, created_at REAL NOT NULL);
            INSERT INTO settings VALUES ('private', 'false');
        """)
    return path


@pytest.fixture
def policy(database):
    capture_policy.provision(database, MONITOR, LAYOUT)
    return capture_policy.ScreenCapturePolicy(database, Snapshot)


def test_factory_is_off_and_requires_an_explicit_path(monkeypatch):
    monkeypatch.setenv("HOARD_CAPTURE_POLICY_DB", "relative.db")
    monkeypatch.delenv("HOARD_CAPTURE_POLICY_ENABLED", raising=False)
    assert capture_policy.create() is None
    monkeypatch.setenv("HOARD_CAPTURE_POLICY_ENABLED", "1")
    assert capture_policy.create() is None


def test_permit_checks_the_scope_revision_and_medium(policy):
    state = policy.read(MONITOR, LAYOUT)
    assert state == Snapshot(1, False, False, False)
    for args in [("other", LAYOUT, 1, "video"), (MONITOR, "0" * 64, 1, "video"),
                 (MONITOR, LAYOUT, 0, "audio"), (MONITOR, LAYOUT, 1, "other")]:
        with policy.lease(*args) as granted:
            assert granted is False
    with policy.lease(MONITOR, LAYOUT, 1, "audio") as granted:
        assert granted is True


@pytest.mark.parametrize(("statement", "field"), [
    ("INSERT INTO settings VALUES ('private', 'true') ON CONFLICT(key) DO UPDATE SET value=excluded.value", "private"),
    ("INSERT INTO exclusions VALUES (1, 'app', 'test.exe', 1, 0)", "excluded"),
])
def test_cutoff_waits_for_the_issued_acquisition(database, policy, statement, field):
    attempting = Event()
    confirmed = Event()
    acquisitions = []

    def change():
        with sqlite3.connect(database, timeout=4) as conn:
            attempting.set()
            conn.execute(statement)
        confirmed.set()

    before = policy.read(MONITOR, LAYOUT)
    cut = Thread(target=change)
    try:
        with policy.lease(MONITOR, LAYOUT, before.revision, "audio") as granted:
            assert granted is True
            cut.start()
            assert attempting.wait(3) and not confirmed.wait(0.05)
            acquisitions.append("audio")
    finally:
        if cut.ident is not None:
            cut.join(4)
    assert confirmed.is_set()
    state = policy.read(MONITOR, LAYOUT)
    assert getattr(state, field) and state.revision > before.revision
    for medium in ("video", "audio"):
        with policy.lease(MONITOR, LAYOUT, state.revision, medium) as granted:
            assert granted is False
    assert acquisitions == ["audio"]


def test_abandoned_permission_survives_reopening_and_denies_edits(database, policy):
    class Interrupted(BaseException):
        pass

    with pytest.raises(Interrupted):
        with policy.lease(MONITOR, LAYOUT, 1, "video") as granted:
            assert granted is True
            raise Interrupted()
    reopened = capture_policy.ScreenCapturePolicy(database, Snapshot)
    assert reopened.read(MONITOR, LAYOUT) is None
    with reopened.lease(MONITOR, LAYOUT, 1, "audio") as granted:
        assert granted is False
    with sqlite3.connect(database) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="Outstanding"):
            conn.execute("UPDATE settings SET value='true' WHERE key='private'")
        with pytest.raises(sqlite3.IntegrityError, match="Outstanding"):
            conn.execute("INSERT INTO exclusions VALUES (1, 'app', 'test.exe', 1, 0)")
    with pytest.raises(ValueError, match="outstanding"):
        capture_policy.remove(database)


def test_settled_error_releases_permit_and_removal_restores_policy_edits(database, policy):
    with pytest.raises(RuntimeError, match="settled"):
        with policy.lease(MONITOR, LAYOUT, 1, "video") as granted:
            assert granted is True
            raise RuntimeError("settled")
    assert policy.read(MONITOR, LAYOUT) == Snapshot(1, False, False, False)
    capture_policy.remove(database)
    assert policy.read(MONITOR, LAYOUT) is None
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE settings SET value='true' WHERE key='private'")


def test_provision_and_runtime_are_closed_on_unknown_or_incomplete_policy(database, policy):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE settings SET value='null' WHERE key='private'")
    with pytest.raises(ValueError, match="authoritative"):
        policy.read(MONITOR, LAYOUT)
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TRIGGER capture_policy_fence_settings_update")
    assert policy.read(MONITOR, LAYOUT) is None
