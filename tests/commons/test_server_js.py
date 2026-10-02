"""js/hoard-commons/server.js: the Node twin of atomic, tokens, ids, env, net, sqlkit, waiting and background timers.

The functions that behave identically in both languages are also checked against the shared vectors in
test_tokens.py, test_ids.py and test_waiting.py; here the rest runs in Node and is cross-checked with Python
(a database or a token written by one side is read by the other)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from hoard_link import atomic, ids, sqlkit, tokens
from tests.commons.jsrun import JS_DIR, node

MODULE = (JS_DIR / "server.js").resolve().as_uri()


def run_node(body: str, *args, env: dict | None = None, timeout: float = 60.0, flags: tuple[str, ...] = ()):
    """Run `body` (the async function body of main(...args)) in Node with the module imported as `s`; returns the JSON it returns."""
    exe = node()
    script = (
        f'import * as s from {json.dumps(MODULE)};\n'
        'import fs from "node:fs"; import path from "node:path";\n'
        "const args = JSON.parse(process.argv[2]);\n"
        "async function main(...args) {\n" + textwrap.indent(textwrap.dedent(body), "  ") + "\n}\n"
        "const out = await main(...args);\n"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));\n"
    )
    full_env = {**os.environ, **(env or {}), "NODE_NO_WARNINGS": "1"}
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "run.mjs"
        file.write_text(script, encoding="utf-8")
        proc = subprocess.run([exe, *flags, str(file), json.dumps(list(args))], capture_output=True, text=True, timeout=timeout, encoding="utf-8", env=full_env)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout)


def has_sqlite() -> bool:
    try:
        return bool(run_node('try { await import("node:sqlite"); return true; } catch { return false; }'))
    except AssertionError:
        return False


def tmp_files(folder: Path):
    return [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]


# ------------------------------------------------------------------ atomic files

def test_write_and_read_json(tmp_path):
    f = tmp_path / "a" / "b" / "state.json"
    out = run_node("""
        const [f] = args;
        s.writeJsonAtomic(f, { name: "Ñandú ✓", n: [1, 2] });
        const back = s.readJson(f);
        fs.writeFileSync(f + ".bad", "{broken"); fs.writeFileSync(f + ".empty", "  ");
        fs.writeFileSync(f + ".bom", "\\uFEFF" + '{"a":1}');
        return { back, missing: s.readJson(f + ".nope", "d"), bad: s.readJson(f + ".bad", "bad"), empty: s.readJson(f + ".empty", []), bom: s.readJson(f + ".bom") };
    """, str(f))
    assert out == {"back": {"name": "Ñandú ✓", "n": [1, 2]}, "missing": "d", "bad": "bad", "empty": [], "bom": {"a": 1}}
    assert f.read_text(encoding="utf-8").endswith("\n") and tmp_files(f.parent) == []
    assert atomic.read_json(f) == {"name": "Ñandú ✓", "n": [1, 2]}                  # Python reads what Node wrote


def test_python_and_node_agree_on_atomic_files(tmp_path):
    f = tmp_path / "py.json"
    atomic.write_json_atomic(f, {"k": "ñ"})
    assert run_node("return s.readJson(args[0]);", str(f)) == {"k": "ñ"}


def test_replace_with_retry(tmp_path):
    src = tmp_path / "x.tmp"
    src.write_text("data")
    out = run_node("""
        const sleeps = []; let n = 0;
        const flaky = (a, b) => { n++; if (n <= 3) { const e = new Error("locked"); e.code = ["EPERM", "EBUSY", "EACCES"][n - 1]; throw e; } };
        s.replaceWithRetry("a", "b", { rename: flaky, sleep: (ms) => sleeps.push(ms) });
        const first = { n, sleeps };
        let thrown = null; const sleeps2 = [];
        try { s.replaceWithRetry("a", "b", { rename: () => { const e = new Error("gone"); e.code = "ENOENT"; throw e; }, sleep: (ms) => sleeps2.push(ms) }); } catch (e) { thrown = e.code; }
        let exhausted = null; let calls = 0;
        try { s.replaceWithRetry(args[0], "b", { retries: 3, rename: () => { calls++; const e = new Error("held"); e.code = "EPERM"; throw e; }, sleep: () => {} }); } catch (e) { exhausted = e.code; }
        return { first, thrown, sleeps2, exhausted, calls, srcLeft: fs.existsSync(args[0]) };
    """, str(src))
    assert out == {"first": {"n": 4, "sleeps": [50, 100, 150]}, "thrown": "ENOENT", "sleeps2": [], "exhausted": "EPERM", "calls": 3, "srcLeft": False}


def test_failed_write_keeps_the_old_file_and_cleans_tmp(tmp_path):
    f = tmp_path / "keep.json"
    out = run_node("""
        const [f] = args;
        s.writeJsonAtomic(f, { ok: true });
        let err = null;
        try { s.writeJsonAtomic(f, { big: 10n }); } catch (e) { err = e.name; }       // BigInt cannot be serialised
        return { err, back: s.readJson(f) };
    """, str(f))
    assert out == {"err": "TypeError", "back": {"ok": True}} and tmp_files(tmp_path) == []


# ------------------------------------------------------------------ tokens

def test_tokens_are_stable_and_shared_with_python(tmp_path):
    f = tmp_path / "data" / "mcp-token"
    out = run_node("""
        const [f] = args;
        const a = s.readOrCreateToken(f), b = s.readOrCreateToken(f);
        fs.writeFileSync(f + "2", "short");
        const c = s.readOrCreateToken(f + "2");
        fs.writeFileSync(f + "3", "\\uFEFF" + "z".repeat(40) + "\\r\\n");
        return { a, same: a === b, c, bom: s.readOrCreateToken(f + "3"), mode: (fs.statSync(f).mode & 0o777).toString(8), len: a.length };
    """, str(f))
    assert out["same"] and out["len"] >= 32 and len(out["c"]) >= 32 and out["bom"] == "z" * 40
    if os.name != "nt":
        assert out["mode"] == "600"
    assert tokens.read_or_create_token(f) == out["a"]                                # Python keeps Node's token
    py = tmp_path / "py-token"
    mine = tokens.read_or_create_token(py)
    assert run_node("return s.readOrCreateToken(args[0]);", str(py)) == mine         # and Node keeps Python's


def test_url_roundtrip(tmp_path):
    f = tmp_path / "url"
    assert run_node("s.writeUrl(args[0], ' http://127.0.0.1:5181 \\n'); return [s.readUrl(args[0]), s.readUrl(args[0] + 'x')];", str(f)) == ["http://127.0.0.1:5181", None]
    assert tokens.read_url(f) == "http://127.0.0.1:5181"


# ------------------------------------------------------------------ ids

def test_ids_monotonic_and_formats():
    out = run_node("""
        const made = Array.from({ length: 3000 }, () => s.newUlid());
        const sorted = [...made].sort();
        const id = s.newId("job");
        return { ordered: JSON.stringify(made) === JSON.stringify(sorted), unique: new Set(made).size, id,
                 idOk: s.isUlid(id.slice(4)), t: s.idTime(id), exact: s.idTime(s.newUlid(1700000000.5)), none: s.newId(), sep: s.newId("x", { sep: "-" }),
                 short: [s.shortId(), s.shortId(5), s.shortId(1)] };
    """)
    assert out["ordered"] and out["unique"] == 3000 and out["id"].startswith("job_") and out["idOk"]
    assert abs(out["t"] - __import__("time").time()) < 5 and out["exact"] == 1_700_000_000.5
    assert ids.is_ulid(out["none"]) and out["sep"].startswith("x-")
    assert [len(x) for x in out["short"]] == [8, 5, 1]


def test_node_ulids_decode_in_python():
    u = run_node("return s.newUlid(1700000000.25);")
    assert ids.id_time(u) == 1_700_000_000.25
    assert run_node("return s.idTime(args[0]);", ids.new_ulid(1_600_000_000.0)) == 1_600_000_000.0


# ------------------------------------------------------------------ environment

ENV_CASES = [
    ("envFlag", ["X"], {"X": "off"}, True, False), ("envFlag", ["X"], {"X": "FALSE"}, True, False), ("envFlag", ["X"], {"X": " No "}, True, False),
    ("envFlag", ["X"], {"X": "0"}, True, False), ("envFlag", ["X"], {"X": "on"}, False, True), ("envFlag", ["X"], {"X": "1"}, False, True),
    ("envFlag", ["X"], {"X": "Yes"}, False, True), ("envFlag", ["X"], {"X": "true"}, False, True), ("envFlag", ["X"], {"X": "maybe"}, True, True),
    ("envFlag", ["X"], {"X": "maybe"}, False, False), ("envFlag", ["X"], {"X": ""}, True, True), ("envFlag", ["X"], {}, True, True), ("envFlag", ["X"], {}, False, False),
]


def test_env_flag_matches_python(monkeypatch):
    from hoard_link.appconfig import env_flag
    script = """
        const out = [];
        for (const [name, env, def] of args) { out.push(s.envFlag(name, def, env)); }
        return out;
    """
    got = run_node(script, *[[c[1][0], c[2], c[3]] for c in ENV_CASES])
    for case, js in zip(ENV_CASES, got):
        _, (name,), env, default, expect = case
        for key in env:
            monkeypatch.setenv(name, env[key])
        if not env:
            monkeypatch.delenv(name, raising=False)
        assert env_flag(name, default) == expect == js, case


def test_env_str_and_int():
    out = run_node("""
        const env = { A: "  hi ", B: "8000", C: "x", D: "7.5", E: "  " };
        return [s.envStr(["Z", "A"], { env }), s.envStr("E", { default: "d", env }), s.envStr("Z", "dflt"),
                s.envInt(["C", "B"], { env }), s.envInt("D", { default: 1, env }), s.envInt("B", { maximum: 100, env }), s.envInt("Z"),
                s.resolveDataDir("links", "/repo", { LINKS_DATA_DIR: "/custom" }), s.resolveDataDir("links", "/repo", {})];
    """)
    assert out[:7] == ["hi", "d", "dflt", 8000, 1, 100, None]
    assert out[7].replace("\\", "/").endswith("/custom") and out[8].replace("\\", "/") == "/repo/data"


# ------------------------------------------------------------------ ports

def test_ports_and_already_running():
    out = run_node("""
        import("node:http").then(() => {});
        const http = await import("node:http");
        const net = await import("node:net");
        const srv = http.createServer((req, res) => {
          if (req.url === "/api/health") { res.setHeader("content-type", "application/json"); res.end(JSON.stringify({ service: "links-hoard" })); }
          else { res.statusCode = 404; res.end(); }
        });
        await new Promise((r) => srv.listen(0, "127.0.0.1", r));
        const port = srv.address().port;
        const other = http.createServer((req, res) => res.end("<html>")); await new Promise((r) => other.listen(0, "127.0.0.1", r));
        const result = {
          busy: await s.canListen(port), freeAfter: await s.canListen(await s.freePort()), bad: [await s.canListen(0), await s.canListen(70000), await s.canListen("x")],
          valid: [s.validPort("5180", 1), s.validPort(0, 7), s.validPort(70000, 7), s.validPort("x", 7), s.validPort(5.5, 7)],
          next: (await s.findAvailablePort(port)) > port,
          running: await s.alreadyRunning("links-hoard", port), wrong: await s.alreadyRunning("kafka-hoard", port),
          html: await s.alreadyRunning("links-hoard", other.address().port), free: await s.alreadyRunning("links-hoard", await s.freePort()),
        };
        let threw = null; try { await s.findAvailablePort(0); } catch (e) { threw = e.message; }
        result.threw = threw;
        srv.close(); other.close();
        return result;
    """)
    assert out["busy"] is False and out["freeAfter"] is True and out["bad"] == [False, False, False]
    assert out["valid"] == [5180, 7, 7, 7, 7] and out["next"] is True
    assert out["running"] is True and out["wrong"] is False and out["html"] is False and out["free"] is False
    assert "Not a port" in out["threw"]


# ------------------------------------------------------------------ waiting and background timers

def test_wait_for():
    out = run_node("""
        let n = 0;
        const done = await s.waitFor(async () => ({ id: "j", state: ++n < 3 ? "running" : "done" }), 5, { poll: 0.01 });
        const gaveUp = await s.waitFor(() => ({ state: "running", progress: 3 }), 0.1, { poll: 0.02 });
        const zero = await s.waitFor(() => ({ state: "running" }), 0);
        const missing = await s.waitFor(() => null, 1);
        const custom = await s.waitFor(() => ({ status: "ok" }), 1, { doneStates: ["ok"] });
        return { done, gaveUp, zero, missing, custom, max: s.MAX_WAIT_S, clamp: [s.clampWait(1e9), s.clampWait(-1), s.clampWait("x"), s.clampWait(null)] };
    """)
    assert out["done"]["state"] == "done" and "still_running" not in out["done"] and out["done"]["id"] == "j"
    assert out["gaveUp"]["still_running"] is True and out["gaveUp"]["progress"] == 3 and out["gaveUp"]["waited_s"] >= 0.1
    assert out["zero"]["still_running"] is True and out["zero"]["waited_s"] == 0
    assert out["missing"] == {"state": "missing", "waited_s": 0} or out["missing"]["state"] == "missing"
    assert out["custom"]["status"] == "ok" and out["max"] == 150 and out["clamp"] == [150, 0, 0, 0]


def test_start_background():
    out = run_node("""
        let ticks = 0, overlapping = 0, maxOverlap = 0; const logs = [];
        const h = s.startBackground({ name: "digest", intervalMs: 20, firstDelayMs: 5, log: (m) => logs.push(m),
          tick: async () => { overlapping++; maxOverlap = Math.max(maxOverlap, overlapping); ticks++; await new Promise((r) => setTimeout(r, 50)); overlapping--;
                              if (ticks === 2) throw new Error("boom"); } });
        await new Promise((r) => setTimeout(r, 400));
        h.stop();
        const after = ticks; await new Promise((r) => setTimeout(r, 100));
        const off = s.startBackground({ name: "x", intervalMs: 10, tick: () => { ticks += 1000; }, envFlag: "HL_OFF", env: { HL_OFF: "off" } });
        const on = s.startBackground({ name: "y", intervalMs: 1000000, firstDelayMs: 1000000, tick: () => {}, envFlag: "HL_ON", env: { HL_ON: "1" } });
        const unset = s.startBackground({ name: "z", intervalMs: 1000000, firstDelayMs: 1000000, tick: () => {}, envFlag: "HL_NOPE", env: {} });
        on.stop(); unset.stop();
        let bad = null; try { s.startBackground({ intervalMs: 1 }); } catch (e) { bad = e.name; }
        return { ticks: after, maxOverlap, stopped: ticks === after, logs, off: off.enabled, on: on.enabled, unset: unset.enabled, tooBig: ticks >= 1000, bad };
    """)
    assert out["ticks"] >= 3 and out["maxOverlap"] == 1 and out["stopped"] and out["logs"] == ["digest: boom"]
    assert out["off"] is False and out["on"] is True and out["unset"] is True and out["tooBig"] is False and out["bad"] == "TypeError"


# ------------------------------------------------------------------ SQLite

needs_sqlite = pytest.mark.skipif(not (__import__("shutil").which("node") and has_sqlite()), reason="node:sqlite needs Node 22.5+")


@needs_sqlite
def test_open_database_migrations_tx_and_settings(tmp_path):
    f = tmp_path / "data" / "x.db"
    out = run_node("""
        const [f] = args;
        const db = s.openDatabase(f, { migrations: [
          "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL); CREATE INDEX items_name ON items(name);",
          (raw) => { raw.exec("ALTER TABLE items ADD COLUMN qty INTEGER NOT NULL DEFAULT 0"); raw.exec("INSERT INTO items(name) VALUES ('seed')"); },
        ] });
        const r = { version: db.schemaVersion(), journal: db.get("PRAGMA journal_mode").journal_mode, fk: db.get("PRAGMA foreign_keys").foreign_keys, busy: db.get("PRAGMA busy_timeout").timeout };
        db.tx(() => { db.run("INSERT INTO items(name) VALUES (?)", "outer");
          try { db.tx(() => { db.run("INSERT INTO items(name) VALUES (?)", "inner"); throw new Error("inner fails"); }); } catch {}
          db.tx(() => { db.run("INSERT INTO items(name) VALUES (?)", "second"); }); });
        try { db.tx(() => { db.run("INSERT INTO items(name) VALUES (?)", "lost"); throw new Error("outer fails"); }); } catch {}
        let asyncErr = null; try { db.tx(async () => 1); } catch (e) { asyncErr = e.name; }
        r.names = db.all("SELECT name FROM items ORDER BY id").map((x) => x.name);
        r.asyncErr = asyncErr; r.inTx = db.raw.isTransaction ?? null;
        db.setSetting("a", { x: [1] }); db.setSetting("n", 5);
        db.exec("INSERT INTO settings(key, value) VALUES ('legacy', 'plain text')");
        r.settings = [db.getSetting("a"), db.getSetting("n"), db.getSetting("legacy"), db.getSetting("none", "dflt")];
        r.fts = typeof s.checkFts5(db);
        db.backupTo(path.join(path.dirname(f), "bk", "copy.db"));
        db.close(); db.close(); r.open = db.isOpen();
        let sqlErr = null; try { s.openDatabase(path.join(path.dirname(f), "bad.db"), { migrations: ["CREATE TABLE ok (x)", "CREATE TABLE half (x); INSERT INTO nope VALUES (1);"] }); } catch (e) { sqlErr = e.message; }
        r.sqlErr = !!sqlErr;
        const again = s.openDatabase(path.join(path.dirname(f), "bad.db"), { migrations: ["CREATE TABLE ok (x)", "CREATE TABLE half (x)"] });
        r.retried = again.schemaVersion(); again.close();
        return r;
    """, str(f))
    assert out["version"] == 2 and out["journal"] == "wal" and out["fk"] == 1 and out["busy"] == 15000
    assert out["names"] == ["seed", "outer", "second"] and out["asyncErr"] == "TypeError"
    assert out["settings"] == [{"x": [1]}, 5, "plain text", "dflt"] and out["fts"] == "boolean" and out["open"] is False
    assert out["sqlErr"] is True and out["retried"] == 2
    assert (tmp_path / "data" / "bk" / "copy.db").exists() and not [p for p in (tmp_path / "data" / "bk").iterdir() if p.name.endswith(".tmp")]


@needs_sqlite
def test_database_files_are_shared_between_python_and_node(tmp_path):
    migs = ["CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);", "ALTER TABLE items ADD COLUMN qty INTEGER NOT NULL DEFAULT 0;"]
    path = tmp_path / "shared.db"
    py = sqlkit.Database(path, migrations=migs)
    py.insert("items", {"name": "from python"})
    py.set_setting("k", {"a": [1, "ñ"]})
    py.close()
    out = run_node("""
        const db = s.openDatabase(args[0], { migrations: JSON.parse(args[1]) });          // nothing is re-applied
        const r = { version: db.schemaVersion(), rows: db.all("SELECT name, qty FROM items"), setting: db.getSetting("k") };
        db.run("INSERT INTO items(name, qty) VALUES (?, ?)", "from node", 3); db.setSetting("n", { z: 1 });
        db.close();
        return r;
    """, str(path), json.dumps(migs))
    assert out["version"] == 2 and out["rows"] == [{"name": "from python", "qty": 0}] and out["setting"] == {"a": [1, "ñ"]}
    py = sqlkit.Database(path, migrations=migs)
    assert [dict(r) for r in py.query("SELECT name, qty FROM items ORDER BY id")] == [{"name": "from python", "qty": 0}, {"name": "from node", "qty": 3}]
    assert py.get_setting("n") == {"z": 1} and py.schema_version == 2
    py.close()


@needs_sqlite
def test_database_opened_by_node_reads_old_schema_version_shapes(tmp_path):
    out = run_node("""
        const { DatabaseSync } = (await import("node:module")).createRequire(import.meta.url)("node:sqlite");
        const f = args[0];
        const old = new DatabaseSync(f);
        old.exec("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL); INSERT INTO schema_version VALUES (1, 'x');");
        old.exec("CREATE TABLE links (id TEXT)"); old.close();
        const db = s.openDatabase(f, { migrations: ["CREATE TABLE links (id TEXT)", "ALTER TABLE links ADD COLUMN url TEXT"] });
        const v = db.schemaVersion(); db.close(); return v;
    """, str(tmp_path / "old.db"))
    assert out == 2


@needs_sqlite
def test_node_database_refuses_bad_pragma_values(tmp_path):
    out = run_node("""
        let err = null; try { s.openDatabase(args[0], { synchronous: "NORMAL; DROP TABLE x" }); } catch (e) { err = e.message; }
        return err;
    """, str(tmp_path / "p.db"))
    assert "bad synchronous" in out


def test_clear_error_when_node_sqlite_is_missing():
    """Without node:sqlite (Node older than 22.5, or the module disabled) openDatabase says what is wrong; the rest of
    the module still imports and works."""
    out = run_node("""
        let msg = null;
        try { s.openDatabase(args[0], { migrations: [] }); } catch (e) { msg = e.message; }
        return { msg, token: typeof s.readOrCreateToken, id: s.isUlid(s.newUlid()) };
    """, "/nonexistent-dir-hl/x.db", flags=("--no-experimental-sqlite",))
    if out["msg"] is None:
        pytest.skip("this Node cannot disable node:sqlite")
    assert "node:sqlite" in out["msg"] and "22.5" in out["msg"] and out["token"] == "function" and out["id"] is True
