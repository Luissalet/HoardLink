"""hoard_link.sqlkit: the shared Database (migrations, re-entrant tx, the lock-leak regression, settings, backup)."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from hoard_link import sqlkit
from hoard_link.sqlkit import Database

MIGS = [
    "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]');\n"
    "CREATE INDEX items_name ON items(name);",
    "ALTER TABLE items ADD COLUMN qty INTEGER NOT NULL DEFAULT 0;",
]


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db", migrations=MIGS)
    yield d
    d.close()


# ------------------------------------------------------------------ opening, pragmas, queries

def test_pragmas(db):
    assert db.scalar("PRAGMA journal_mode") == "wal"
    assert db.scalar("PRAGMA foreign_keys") == 1
    assert db.scalar("PRAGMA busy_timeout") == 15000
    assert db.scalar("PRAGMA synchronous") == 1                       # NORMAL


def test_custom_pragmas_and_on_open(tmp_path):
    seen = []
    d = Database(tmp_path / "x.db", wal=False, journal_mode="DELETE", foreign_keys=False, synchronous="FULL", busy_timeout_ms=1234,
                 on_open=lambda c: (seen.append(c), c.execute("PRAGMA secure_delete = ON")))
    assert d.scalar("PRAGMA journal_mode") == "delete" and d.scalar("PRAGMA foreign_keys") == 0
    assert d.scalar("PRAGMA synchronous") == 2 and d.scalar("PRAGMA busy_timeout") == 1234
    assert d.scalar("PRAGMA secure_delete") == 1 and len(seen) == 1
    d.close()
    with pytest.raises(ValueError):
        Database(tmp_path / "y.db", synchronous="NORMAL; DROP TABLE x")
    with pytest.raises(ValueError):
        Database(tmp_path / "z.db", journal_mode="WAL; --")


def test_query_one_scalar_execute_insert(db):
    rid = db.insert("items", {"name": "a", "tags": sqlkit.dumps(["x", "ñ"])})
    assert rid == 1
    db.execute("INSERT INTO items(name, qty) VALUES (?, ?)", ("b", 5))
    db.execute("INSERT INTO items(name, qty) VALUES (:n, :q)", {"n": "c", "q": 7})
    assert [r["name"] for r in db.query("SELECT * FROM items ORDER BY id")] == ["a", "b", "c"]
    assert db.one("SELECT * FROM items WHERE name = ?", ("b",))["qty"] == 5
    assert db.one("SELECT * FROM items WHERE name = ?", ("zz",)) is None
    assert db.scalar("SELECT COUNT(*) FROM items") == 3
    assert db.scalar("SELECT qty FROM items WHERE name = 'zz'", default=-1) == -1
    assert db.scalar("SELECT NULL", default="d") == "d"
    assert sqlkit.loads(db.one("SELECT tags FROM items WHERE id = 1")["tags"]) == ["x", "ñ"]


def test_insert_validates_identifiers_and_conflicts(db):
    with pytest.raises(ValueError):
        db.insert("items; DROP TABLE items", {"name": "x"})
    with pytest.raises(ValueError):
        db.insert("items", {"name) VALUES ('x'); --": "x"})
    with pytest.raises(ValueError):
        db.insert("items", {})
    db.insert("items", {"id": 1, "name": "a"})
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("items", {"id": 1, "name": "b"})
    db.insert("items", {"id": 1, "name": "ignored"}, on_conflict="ignore")
    assert db.one("SELECT name FROM items WHERE id = 1")["name"] == "a"
    db.insert("items", {"id": 1, "name": "replaced"}, on_conflict="replace")
    assert db.one("SELECT name FROM items WHERE id = 1")["name"] == "replaced"
    with pytest.raises(ValueError):
        db.insert("items", {"name": "x"}, on_conflict="abort; --")


def test_executemany_is_all_or_nothing(db):
    db.executemany("INSERT INTO items(name) VALUES (?)", [("a",), ("b",)])
    assert db.scalar("SELECT COUNT(*) FROM items") == 2
    with pytest.raises(sqlite3.IntegrityError):
        db.executemany("INSERT INTO items(id, name) VALUES (?, ?)", [(10, "x"), (10, "dup")])
    assert db.scalar("SELECT COUNT(*) FROM items") == 2


def test_foreign_keys_are_enforced(tmp_path):
    d = Database(tmp_path / "fk.db", migrations=["CREATE TABLE p (id INTEGER PRIMARY KEY); CREATE TABLE c (p_id INTEGER REFERENCES p(id));"])
    with pytest.raises(sqlite3.IntegrityError):
        d.execute("INSERT INTO c(p_id) VALUES (99)")
    d.close()


def test_helpers():
    assert sqlkit.dumps({"b": "ñ", "a": [1]}) == '{"b":"ñ","a":[1]}'
    assert sqlkit.loads('{"a": 1}') == {"a": 1}
    assert sqlkit.loads(None, "d") == "d" and sqlkit.loads("", []) == [] and sqlkit.loads("{bad", {}) == {}
    assert sqlkit.loads(b'[1]') == [1]
    assert sqlkit.row_dict(None) is None
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT 1 AS a, 'x' AS b").fetchone()
    assert sqlkit.row_dict(row) == {"a": 1, "b": "x"} and sqlkit.row_dicts([row]) == [{"a": 1, "b": "x"}]


def test_check_fts5(db):
    assert sqlkit.check_fts5(db) in (True, False)
    assert sqlkit.check_fts5(db.conn) == sqlkit.check_fts5(db)
    assert db.scalar("SELECT COUNT(*) FROM sqlite_temp_master") == 0   # the probe cleans up after itself


# ------------------------------------------------------------------ migrations

def test_migrations_apply_in_order_and_record_versions(tmp_path):
    d = Database(tmp_path / "m.db", migrations=MIGS)
    assert d.schema_version == 2
    assert [r["version"] for r in d.query("SELECT version FROM schema_version ORDER BY version")] == [1, 2]
    assert d.scalar("SELECT applied_at FROM schema_version WHERE version = 1").endswith("Z")
    d.close()
    d = Database(tmp_path / "m.db", migrations=MIGS + ["CREATE TABLE later (x)"])      # reopening applies only the new one
    assert d.schema_version == 3 and d.scalar("SELECT COUNT(*) FROM schema_version") == 3
    d.close()


def test_script_with_triggers_and_semicolons_in_strings(tmp_path):
    sql = """
    CREATE TABLE a (id INTEGER PRIMARY KEY, note TEXT);
    CREATE TABLE log (msg TEXT);
    -- a comment; with a semicolon
    CREATE TRIGGER a_ins AFTER INSERT ON a BEGIN
      INSERT INTO log(msg) VALUES ('inserted; ' || NEW.id);
      INSERT INTO log(msg) VALUES ('second');
    END;
    INSERT INTO a(note) VALUES ('x; y');
    """
    d = Database(tmp_path / "s.db", migrations=[sql])
    assert d.scalar("SELECT note FROM a") == "x; y"
    assert [r["msg"] for r in d.query("SELECT msg FROM log ORDER BY rowid")] == ["inserted; 1", "second"]
    d.close()


def test_split_statements():
    assert sqlkit.split_statements("A;B;") == ["A;", "B;"]
    assert sqlkit.split_statements("SELECT 1") == ["SELECT 1"]
    assert sqlkit.split_statements("-- only a comment\n") == []
    assert sqlkit.split_statements("") == []


def test_failed_migration_rolls_back_everything_including_ddl(tmp_path):
    bad = ["CREATE TABLE ok (x);", "CREATE TABLE half (x); INSERT INTO nope VALUES (1);"]
    with pytest.raises(sqlite3.OperationalError):
        Database(tmp_path / "f.db", migrations=bad)
    raw = sqlite3.connect(tmp_path / "f.db")
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" in tables and "half" not in tables                      # migration 1 stayed, 2 left no partial DDL
    assert raw.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    raw.close()
    fixed = ["CREATE TABLE ok (x);", "CREATE TABLE half (x);"]
    d = Database(tmp_path / "f.db", migrations=fixed)                    # and the retry succeeds
    assert d.schema_version == 2
    d.close()


def test_callable_migrations_run_inside_the_transaction(tmp_path):
    calls = []

    def add_cols(conn):
        calls.append(conn.in_transaction)
        conn.execute("CREATE TABLE t (a)")
        conn.execute("INSERT INTO t VALUES (1)")

    def boom(conn):
        conn.execute("INSERT INTO t VALUES (2)")
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError):
        Database(tmp_path / "c.db", migrations=[add_cols, boom])
    raw = sqlite3.connect(tmp_path / "c.db")
    assert calls == [True] and raw.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    raw.close()


def test_existing_database_with_old_schema_version_tables(tmp_path):
    # the three shapes used by the old copies: plain, with applied_at NOT NULL + primary key
    for name, ddl in (("plain", "CREATE TABLE schema_version (version INTEGER NOT NULL)"),
                      ("links", "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")):
        p = tmp_path / f"{name}.db"
        raw = sqlite3.connect(p)
        raw.execute(ddl)
        raw.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]')")
        raw.execute("INSERT INTO schema_version(version) VALUES (1)" if name == "plain" else "INSERT INTO schema_version VALUES (1, 'x')")
        raw.commit()
        raw.close()
        d = Database(p, migrations=MIGS)                                # only migration 2 runs
        assert d.schema_version == 2 and d.scalar("SELECT COUNT(*) FROM pragma_table_info('items') WHERE name = 'qty'") == 1
        d.close()


def test_newer_database_than_the_build_only_warns(tmp_path, caplog):
    d = Database(tmp_path / "n.db", migrations=MIGS)
    d.close()
    with caplog.at_level("WARNING", logger="hoard_link.sqlkit"):
        d = Database(tmp_path / "n.db", migrations=MIGS[:1])
    assert d.schema_version == 2 and "schema version" in caplog.text
    d.close()


def test_two_processes_starting_together_apply_a_migration_once(tmp_path):
    p = tmp_path / "race.db"
    results, barrier = [], threading.Barrier(4)

    def go():
        barrier.wait()
        d = Database(p, migrations=["CREATE TABLE once (x); INSERT INTO once VALUES (1);"])
        results.append(d.scalar("SELECT COUNT(*) FROM once"))
        d.close()

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [1, 1, 1, 1]


# ------------------------------------------------------------------ transactions

def test_tx_commits_and_rolls_back(db):
    with db.tx() as conn:
        conn.execute("INSERT INTO items(name) VALUES ('kept')")
    with pytest.raises(RuntimeError):
        with db.tx():
            db.execute("INSERT INTO items(name) VALUES ('lost')")
            raise RuntimeError("boom")
    assert [r["name"] for r in db.query("SELECT name FROM items")] == ["kept"]
    assert not db.in_transaction and not db.conn.in_transaction


def test_tx_is_reentrant_and_only_the_outermost_commits(db):
    with db.tx():
        db.execute("INSERT INTO items(name) VALUES ('outer')")
        with db.tx():
            db.execute("INSERT INTO items(name) VALUES ('inner')")
            with db.tx():
                db.execute("INSERT INTO items(name) VALUES ('innermost')")
        assert db.conn.in_transaction and db.in_transaction
    assert db.scalar("SELECT COUNT(*) FROM items") == 3 and not db.conn.in_transaction


def test_failure_in_an_outer_tx_discards_inner_work(db):
    with pytest.raises(RuntimeError):
        with db.tx():
            with db.tx():
                db.execute("INSERT INTO items(name) VALUES ('inner')")
            raise RuntimeError("outer fails")
    assert db.scalar("SELECT COUNT(*) FROM items") == 0


def test_inner_failure_that_is_caught_rolls_back_only_the_inner_part(db):
    with db.tx():
        db.execute("INSERT INTO items(name) VALUES ('outer')")
        try:
            with db.tx():
                db.execute("INSERT INTO items(name) VALUES ('inner')")
                raise ValueError("inner fails")
        except ValueError:
            pass
        db.execute("INSERT INTO items(name) VALUES ('after')")
    assert [r["name"] for r in db.query("SELECT name FROM items ORDER BY id")] == ["outer", "after"]


def test_transaction_alias_and_executemany_inside_tx(db):
    with db.transaction():
        db.executemany("INSERT INTO items(name) VALUES (?)", [("a",), ("b",)])
        with pytest.raises(sqlite3.IntegrityError):
            db.executemany("INSERT INTO items(id, name) VALUES (?, ?)", [(50, "x"), (50, "y")])
    assert db.scalar("SELECT COUNT(*) FROM items") == 2


def test_tx_without_immediate(db):
    with db.tx(immediate=False):
        db.execute("INSERT INTO items(name) VALUES ('x')")
    assert db.scalar("SELECT COUNT(*) FROM items") == 1


def test_other_threads_wait_for_the_transaction(db):
    entered, release, order = threading.Event(), threading.Event(), []

    def holder():
        with db.tx():
            db.execute("INSERT INTO items(name) VALUES ('held')")
            entered.set()
            release.wait(5)
            order.append("commit")

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(5)
    reader = threading.Thread(target=lambda: order.append(f"read:{db.scalar('SELECT COUNT(*) FROM items')}"))
    reader.start()
    time.sleep(0.2)
    assert order == []                         # the reader is blocked while the transaction is open
    release.set()
    t.join()
    reader.join()
    assert order == ["commit", "read:1"]


def test_lock_leak_regression_failed_begin_releases_the_lock(tmp_path):
    """The 16 old copies took the lock and then ran BEGIN IMMEDIATE: if BEGIN raised, the lock stayed held and every
    other thread hung forever."""
    path = tmp_path / "locked.db"
    d = Database(path, migrations=MIGS, busy_timeout_ms=50)
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")           # another process holds the write lock
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with d.tx():
                pytest.fail("the body must not run when BEGIN fails")
        assert time.monotonic() - t0 < 5
        assert d._depth == 0
        # the lock was released: another thread can use the database (reads work while the writer holds its lock)
        result = {}

        def reader():
            result["n"] = d.scalar("SELECT COUNT(*) FROM items")
        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "another thread hung: the lock leaked"
        assert result == {"n": 0}
        # and a thread can acquire the RLock outright
        got = []
        def grab():
            ok = d.lock.acquire(timeout=2)
            got.append(ok)
            if ok:
                d.lock.release()
        t2 = threading.Thread(target=grab)
        t2.start()
        t2.join()
        assert got == [True]
    finally:
        other.execute("ROLLBACK")
        other.close()
    with d.tx():                               # once the other writer is gone, transactions work again
        d.execute("INSERT INTO items(name) VALUES ('ok')")
    assert d.scalar("SELECT COUNT(*) FROM items") == 1
    d.close()


def test_busy_timeout_waits_for_another_writer(tmp_path):
    path = tmp_path / "busy.db"
    d = Database(path, migrations=MIGS, busy_timeout_ms=3000)
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    threading.Timer(0.3, lambda: other.execute("COMMIT")).start()
    t0 = time.monotonic()
    with d.tx():
        d.execute("INSERT INTO items(name) VALUES ('after wait')")
    assert 0.2 < time.monotonic() - t0 < 3
    other.close()
    d.close()


def test_failed_begin_inside_nested_use_keeps_depth_consistent(db, monkeypatch):
    real = db.conn

    class Failing:
        def __init__(self):
            self.n = 0

        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *a):
            if sql.startswith("SAVEPOINT"):
                raise sqlite3.OperationalError("savepoint failed")
            return real.execute(sql, *a)

    with db.tx():
        db.conn = Failing()
        with pytest.raises(sqlite3.OperationalError):
            with db.tx():
                pass
        db.conn = real
        assert db._depth == 1
    assert db._depth == 0 and not real.in_transaction


# ------------------------------------------------------------------ settings, backup, close

def test_settings_are_json_and_tolerate_old_plain_text(db):
    assert db.get_setting("missing") is None and db.get_setting("missing", 3) == 3
    db.set_setting("a", {"x": [1, 2], "n": "ñ"})
    db.set_setting("flag", True)
    db.set_setting("n", 5)
    db.set_setting("a", {"x": []})                                  # upsert
    assert db.get_setting("a") == {"x": []} and db.get_setting("flag") is True and db.get_setting("n") == 5
    db.execute("INSERT INTO settings(key, value) VALUES ('legacy', 'plain text, not json')")
    assert db.get_setting("legacy") == "plain text, not json"
    db.delete_setting("flag")
    assert db.get_setting("flag", "gone") == "gone"


def test_settings_table_that_already_exists_is_reused(tmp_path):
    d = Database(tmp_path / "s.db", migrations=["CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL); INSERT INTO settings VALUES ('k', '\"v\"');"])
    assert d.get_setting("k") == "v"
    d.close()


def test_backup_to_is_a_consistent_copy_while_the_source_keeps_working(db, tmp_path):
    db.executemany("INSERT INTO items(name) VALUES (?)", [(f"n{i}",) for i in range(200)])
    dest = tmp_path / "backups" / "copy.db"
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            db.execute("INSERT INTO items(name) VALUES (?)", (f"w{i}",))
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert db.backup_to(dest) == dest
    finally:
        stop.set()
        t.join()
    copy = sqlite3.connect(dest)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT COUNT(*) FROM items").fetchone()[0] >= 200
    assert copy.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    copy.close()
    assert [p.name for p in dest.parent.iterdir()] == ["copy.db"]       # no temp or -wal leftovers
    db.backup_to(dest)                                                    # overwriting works


def test_close_checkpoints_and_is_idempotent(tmp_path):
    p = tmp_path / "w.db"
    d = Database(p, migrations=MIGS)
    d.executemany("INSERT INTO items(name) VALUES (?)", [(str(i),) for i in range(50)])
    assert (tmp_path / "w.db-wal").exists()
    d.close()
    d.close()
    assert d.closed
    assert not (tmp_path / "w.db-wal").exists() or (tmp_path / "w.db-wal").stat().st_size == 0
    raw = sqlite3.connect(p)
    assert raw.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 50
    raw.close()
    with pytest.raises(sqlite3.ProgrammingError):
        d.query("SELECT 1")


def test_context_manager_and_memory_database():
    with Database(":memory:", migrations=MIGS) as m:
        m.insert("items", {"name": "x"})
        assert m.scalar("SELECT COUNT(*) FROM items") == 1
    assert m.closed


def test_shared_connection_across_threads(db):
    errors = []

    def work(i):
        try:
            for j in range(30):
                db.execute("INSERT INTO items(name) VALUES (?)", (f"{i}-{j}",))
                with db.tx():
                    db.execute("UPDATE items SET qty = qty + 1 WHERE name = ?", (f"{i}-{j}",))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and db.scalar("SELECT COUNT(*) FROM items") == 180 and db.scalar("SELECT SUM(qty) FROM items") == 180
