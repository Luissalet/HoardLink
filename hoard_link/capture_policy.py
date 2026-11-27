"""Opt-in capture policy for a producer sharing the screen-memory database.

The database is the point that serializes policy edits and physical acquisitions.
The screen-memory process owns the policy; its ordinary SQLite writes participate
without a network poll or a separate copy of the exclusion settings.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4


_PRIVATE = {
    "insert": "NEW.key = 'private'",
    "update": "OLD.key = 'private' OR NEW.key = 'private'",
    "delete": "OLD.key = 'private'",
}
_OPERATIONS = tuple(_PRIVATE)
_POLICY_TABLES = {"settings": _PRIVATE, "exclusions": dict.fromkeys(_OPERATIONS, "1")}
_TRIGGERS = {
    f"capture_policy_{when}_{table}_{verb}"
    for when in ("fence", "revision")
    for table in _POLICY_TABLES
    for verb in _OPERATIONS
}
_WAIT_TIMEOUT = 0.5
_SETTLE_TIMEOUT = 2.0
_RECEIPT_POLL_INTERVAL = 0.01


def _scope(monitor: str, layout: str) -> None:
    if (not isinstance(monitor, str) or not 1 <= len(monitor) <= 512
            or not isinstance(layout, str) or not re.fullmatch(r"[a-f0-9]{64}", layout)):
        raise ValueError("A stable display and its current layout are required")


@contextmanager
def _transaction(db: Path, *, write: bool = False):
    if not db.is_absolute():
        raise ValueError("An explicit absolute policy database path is required")
    mode = "rw" if write else "ro"
    with closing(sqlite3.connect(f"{db.as_uri()}?mode={mode}", uri=True,
                                 timeout=_WAIT_TIMEOUT, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


def _private(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = 'private'").fetchone()
    value = json.loads(row[0]) if row else None
    if type(value) is not bool:
        raise ValueError("Private mode has no authoritative value")
    return value


def _install_fences(conn: sqlite3.Connection) -> None:
    for table, operations in _POLICY_TABLES.items():
        for verb, predicate in operations.items():
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS capture_policy_fence_{table}_{verb}
                BEFORE {verb} ON {table}
                WHEN ({predicate}) AND EXISTS (SELECT 1 FROM capture_policy_grant)
                BEGIN SELECT RAISE(ABORT, 'Outstanding screen acquisition'); END""")
            changed = " AND (OLD.value IS NOT NEW.value OR OLD.key IS NOT NEW.key)" if (
                table == "settings" and verb == "update") else ""
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS capture_policy_revision_{table}_{verb}
                AFTER {verb} ON {table} WHEN ({predicate}){changed}
                BEGIN UPDATE capture_policy_scope SET revision = revision + 1;
                END""")


def provision(db: Path, monitor: str, layout: str) -> None:
    """Explicitly register one display against an existing screen-memory DB."""
    _scope(monitor, layout)
    with _transaction(db, write=True) as conn:
        _private(conn)
        conn.execute("SELECT enabled FROM exclusions LIMIT 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS capture_policy_scope (
            slot INTEGER PRIMARY KEY CHECK (slot = 1), monitor TEXT NOT NULL,
            layout TEXT NOT NULL, revision INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS capture_policy_grant (
            slot INTEGER PRIMARY KEY CHECK (slot = 1), receipt TEXT NOT NULL,
            revision INTEGER NOT NULL, medium TEXT NOT NULL)""")
        if conn.execute("SELECT 1 FROM capture_policy_grant").fetchone():
            raise ValueError("An acquisition is still outstanding")
        _install_fences(conn)
        conn.execute("""INSERT INTO capture_policy_scope VALUES (1, ?, ?, 1)
            ON CONFLICT(slot) DO UPDATE SET monitor = excluded.monitor,
            layout = excluded.layout, revision = capture_policy_scope.revision + 1
            WHERE monitor <> excluded.monitor OR layout <> excluded.layout""", (monitor, layout))


def remove(db: Path) -> None:
    """Remove this integration once all issued permissions are settled."""
    with _transaction(db, write=True) as conn:
        if conn.execute("SELECT 1 FROM capture_policy_grant").fetchone():
            raise ValueError("An acquisition is still outstanding")
        for trigger in _TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE capture_policy_grant")
        conn.execute("DROP TABLE capture_policy_scope")


class ScreenCapturePolicy:
    def __init__(self, db: Path, snapshot):
        self.db = db
        self.snapshot = snapshot

    def _read(self, conn, monitor, layout):
        triggers = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'capture_policy_%'")}
        if not _TRIGGERS <= triggers:
            return None
        row = conn.execute("SELECT revision FROM capture_policy_scope WHERE monitor = ? AND layout = ?",
                           (monitor, layout)).fetchone()
        if not row:
            return None
        private = _private(conn)
        # A whole-display pull cannot fence a compositor visibility change.
        # Apply every enabled exclusion to both streams until visibility can be coordinated.
        excluded = conn.execute("SELECT 1 FROM exclusions WHERE enabled <> 0 LIMIT 1").fetchone() is not None
        return self.snapshot(row[0], private, excluded, False)

    @contextmanager
    def _settled_state(self, monitor: str, layout: str, *, write: bool = False):
        deadline = time.monotonic() + _WAIT_TIMEOUT
        while True:
            with _transaction(self.db, write=write) as conn:
                state = self._read(conn, monitor, layout)
                if state is None or not conn.execute("SELECT 1 FROM capture_policy_grant").fetchone():
                    yield conn, state
                    return
            # The selector and the worker can overlap. Release this transaction
            # so an active owner can finish, then read the committed policy again.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield None, None
                return
            time.sleep(min(remaining, _RECEIPT_POLL_INTERVAL))

    def read(self, monitor: str, layout: str):
        with self._settled_state(monitor, layout) as (_, state):
            return state

    @contextmanager
    def publication(self, monitor: str, layout: str, revision: int):
        """Serialize an in-process publication without issuing a native grant."""
        with self._settled_state(monitor, layout, write=True) as (_, state):
            yield bool(state is not None and state.revision == revision
                       and not (state.private or state.excluded or state.audio_excluded))

    @staticmethod
    def _release(conn, receipt: str) -> None:
        deadline = time.monotonic() + _SETTLE_TIMEOUT
        while True:
            try:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM capture_policy_grant WHERE receipt = ?", (receipt,))
                conn.commit()
                return
            except sqlite3.OperationalError as exc:
                conn.rollback()
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                time.sleep(_RECEIPT_POLL_INTERVAL)

    @contextmanager
    def lease(self, monitor: str, layout: str, revision: int, medium: str):
        if medium not in ("video", "audio") or type(revision) is not int:
            yield False
            return
        with self._settled_state(monitor, layout, write=True) as (conn, state):
            if state is None or state.revision != revision or state.private or state.excluded:
                if conn is not None:
                    conn.rollback()
                yield False
                return

            # The receipt survives a producer crash. Under normal operation
            # policy writers wait on the transaction until the call settles.
            receipt = uuid4().hex
            conn.execute("INSERT INTO capture_policy_grant VALUES (1, ?, ?, ?)", (receipt, revision, medium))
            conn.commit()
            settled = True  # A failed transaction before handoff cannot authorize a device.
            try:
                conn.execute("BEGIN IMMEDIATE")
                settled = False
                yield True
            except TimeoutError:
                # The caller could have lost contact while a helper can still act.
                # Keep the durable fence when there is no settlement signal.
                raise
            except Exception:
                # The producer reports an ordinary failure only after settling
                # the call (including a confirmed exit of its audio helper).
                settled = True
                raise
            else:
                settled = True
            finally:
                if settled:
                    self._release(conn, receipt)


def create():
    """Baird worker factory, inert unless explicitly enabled by its launcher."""
    if os.environ.get("HOARD_CAPTURE_POLICY_ENABLED") != "1":
        return None
    path = os.environ.get("HOARD_CAPTURE_POLICY_DB")
    if not path or not Path(path).is_absolute():
        return None
    from baird_hoard.monitor_privacy import PolicySnapshot

    return ScreenCapturePolicy(Path(path), PolicySnapshot)
