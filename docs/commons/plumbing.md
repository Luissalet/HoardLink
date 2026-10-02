# Plumbing: atomic files, tokens, ids, SQLite, paths, ports, config, lanes, waiting

The code every app wrote for itself and that was copied between them. Standard library only, Windows first
(see `docs/COMMONS.md` for the rules). Python modules live in `hoard_link/`, the Node twin in
`js/hoard-commons/server.js` (ESM, no npm dependencies; the database needs Node 22.5+ for `node:sqlite`).
Tests: `tests/commons/test_<module>.py`, shared vectors in `tests/vectors/` (`tokens.json`, `ids.json`,
`waiting.json`, `paths.json`).

| Module | What it replaces | Node twin |
|---|---|---|
| `atomic` | ~35 bare `os.replace` calls, 2 different retry loops, ~12 non-atomic writes of state | `writeJsonAtomic`, `readJson`, `replaceWithRetry` |
| `tokens` | 7 regenerating `write_token`, 10 `read_or_create_token`, `family._write_token_if_missing`, 4 Node `writeToken` | `readOrCreateToken`, `checkBearer` |
| `ids` | ~10 `new_id` formats | `newUlid`, `newId` |
| `sqlkit` | 16 copies of `class Database`, 6 variants in the B family, 3 `server/db.js` | `openDatabase` |
| `paths` | 2 forks of `paths.py`, the `is_dir()`-only checks of Borges and Vulcan, ad hoc quote stripping | - |
| `net` | 16 `port.py`, 5 `port.js`, 9 `_already_running`, the `os.startfile` part of 15 `scripts/launch.py` | `canListen`, `findAvailablePort`, `alreadyRunning` |
| `appconfig` | `_env` / `_int` / `_float` in ~15 `config.py`, 5 `load_dotenv`, the data-folder properties of 17 `Config`s, 2 Node env-flag parsers | `envFlag`, `envInt`, `envStr`, `resolveDataDir` |
| `lanes` | 4 `scheduler.py` (Kafka, Phileas, Tantalus, Galton), ~12 job queues | `startBackground` |
| `waiting` | 7 different `wait_s` caps (120 s to 7200 s) | `waitFor`, `clampWait`, `MAX_WAIT_S` |

## `hoard_link.atomic`

```python
replace_with_retry(src, dst, *, attempts=40, delay=0.05, replace=os.replace, sleep=time.sleep) -> None
write_bytes_atomic(path, data, *, fsync=True, mode=None) -> None
write_text_atomic(path, text, *, encoding="utf-8", fsync=True) -> None
write_json_atomic(path, obj, *, indent=2, ensure_ascii=False, sort_keys=False, fsync=True) -> None
read_json(path, default=None, *, encoding="utf-8-sig") -> Any        # missing / empty / corrupt -> default
update_json(path, fn, default=None, ...) -> Any                      # read-modify-write under one lock per file
tmp_path_for(path) -> Path                                           # {name}.{pid}.{thread_id}.{rand}.tmp
```

* The temp file is a sibling (same volume, so the rename is atomic), parent folders are created, the temp file is
  removed on any failure and the old file is untouched. The JSON is serialised *before* anything is written.
* `replace_with_retry` retries `PermissionError` and `OSError` with `winerror` 5/32/33, sleeping
  `min(0.25, delay * (1 + i))` (about ten seconds in all), and raises any other `OSError` immediately.
* Text goes out as bytes: `\n` stays `\n` on Windows (`Path.write_text` turns it into `\r\n`).
* **Bugs fixed:** `PermissionError: [WinError 5/32]` when an antivirus, the indexer, OneDrive or a UI poll holds the
  destination for a few milliseconds (only Prospero and Galton retried, of ~35 sites plus the hub); empty files after
  a power cut (only Plato, HomeHoard and `pose_batch` flushed); two threads of one process choosing the same temp
  name (Pygmalion, Lumiere used `.{pid}.tmp`); truncated `backend.json` (only Babel was atomic).
* **Migration order** (from the audit): hub (`hub/jobs.py`, `rules.py`, `spheres.py`, `lease.py`, `today.py`,
  `chats.py`, `repos.py`, `worktrack.py`, `profiles.py`, `notify.py`, `mailgate.py`, `backup.py`, `launch.py:322`),
  Prospero (`util.py` becomes a re-export; 4 `config_path.write_text` in `backend.py`), Pygmalion (`util.py:89`,
  `services.py:227`, `datasets/service.py:100`, `workers/_stio.py:180`, `train_lora.py`), Vulcan, Cassandra, Hypatia,
  Plato, Lumiere (`util.py:108`), Kafka (`files.py:57`), HomeHoard, Gepetto, DiskHoard, Daguerre, Galton
  (`routes.replace_retrying` goes away; its injectable `replace` / `sleep` are the same parameters here), Funes
  (`sources.py:175`, pid file), Midas, Nightingale, Scheherazade, Cook (`scheduler.mjs`).

## `hoard_link.tokens`

```python
read_or_create_token(path, *, min_len=32) -> str
read_token(path, *, min_len=32) -> str | None
new_token(min_len=32) -> str
write_url(path, url) -> None            # data/url
read_url(path) -> str | None
check_bearer(header, token) -> bool     # case-insensitive scheme, compare_digest, empty token never matches
```

* The token is **stable across restarts**: only a missing, unreadable or too-short file (or one with whitespace in
  it) gets a new token. Creation is atomic and race-safe: the finished temp file is hard-linked into place, so of
  two processes starting together one wins and the other adopts its token. Mode 0600 where supported.
* **Bug fixed:** Argus, Borges, Cassandra, Echo, Vulcan, Nightingale, Funes/`audio_memory` and the 4 Node apps
  rewrote `mcp-token` in `Services.__init__`, before the port bind; a second instance (autostart, double click)
  changed the file while the running one kept the old token in memory, and the bridge answered
  `401 Invalid MCP token`. Also: `family._write_token_if_missing` accepted an empty or corrupt file, and `agent.py`
  parsed `Bearer ` case-sensitively while `family.py` did not.
* **Migration:** delete `write_token` / `write_url` from `services.py` / `config.py` in the 17 family-A apps and call
  `read_or_create_token(config.token_path)` / `write_url(config.url_path, url)`; Node: replace `writeToken` in `app.js`
  / `agent-routes.js` of Links, Ledger, People, Cook. `family._write_token_if_missing` should become
  `tokens.read_or_create_token` (it is not edited by this change). The behaviour change (stable token) is wanted: the
  bridge already re-reads the file.

## `hoard_link.ids`

```python
new_ulid(now=None) -> str               # 26 Crockford chars, monotonic within the process
new_id(prefix="", *, sep="_") -> str    # "job_01J..."
short_id(n=8) -> str
id_time(value) -> float | None          # seconds since the epoch, from a ULID or prefix + sep + ULID
is_ulid(value) -> bool
```

* Monotonic under a lock **even when the clock does not advance or goes backwards** (the random part counts up;
  80-bit overflow moves to the next millisecond). With an explicit `now` the time is exact and process state is not
  touched (imports, tests).
* **Bug fixed:** Windows clocks tick every ~15 ms, so Kafka / Tantalus / Pygmalion ids (time prefix + random) were
  neither unique nor ordered inside a tick; four incompatible formats made it impossible to sort or validate a
  generic id.
* **Migration:** new ids only; never rewrite stored ids. Format validators in Kafka, Tantalus and Pygmalion (regex)
  must accept both the old and the ULID form. Prospero's `ids.py` becomes `from hoard_link.ids import *`. Lowest
  priority of the set.

## `hoard_link.sqlkit`

```python
class Database(path, *, migrations=(), busy_timeout_ms=15000, wal=True, foreign_keys=True, synchronous="NORMAL",
               journal_mode=None, check_same_thread=False, on_open=None)
    .query(sql, params=()) -> list[Row]      .one(...) -> Row | None      .scalar(sql, params=(), default=None)
    .execute(sql, params=()) -> Cursor       .executemany(sql, seq)       .script(sql)
    .insert(table, dict, *, on_conflict=None) -> lastrowid
    .tx(*, immediate=True)                   # context manager, re-entrant; .transaction() is an alias
    .schema_version                          # property
    .get_setting(key, default=None)  .set_setting(key, value)  .delete_setting(key)   # JSON values
    .backup_to(dest) -> Path                 # online backup (sqlite3 backup API) through a temp file
    .close()                                 # wal_checkpoint(TRUNCATE), idempotent
check_fts5(conn_or_db) -> bool
split_statements(sql) -> list[str]
dumps(obj) -> str    loads(text, default=None)    row_dict(row)    row_dicts(rows)
```

* One shared connection plus one `RLock` (the family convention), `sqlite3.Row`, autocommit, `PRAGMA busy_timeout`.
  `tx()` is re-entrant: the outermost call does `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, a nested call is a
  `SAVEPOINT`, so a caught inner failure rolls back only the inner part. **If `BEGIN` fails the lock is released.**
* Migrations are SQL strings (scripts are split with `sqlite3.complete_statement`, so triggers and `;` in strings
  are fine) or callables `fn(conn)`; the 1-based position is the version, each runs in its own transaction with its
  `schema_version` row, and the version is re-read inside the transaction so two processes starting together apply
  it once. The `schema_version` table of the old copies (`(version)` only, or `version PRIMARY KEY, applied_at NOT
  NULL` as in Links) keeps working. `on_open(conn)` runs after the pragmas and before the migrations (Dorian:
  `journal_mode="DELETE"`, `on_open=lambda c: c.execute("PRAGMA secure_delete = ON")`).
* `get_setting` returns old plain-text values (written by the old copies) as text when they are not JSON.
* **Bugs fixed:** the **lock leak** (`_Transaction.__enter__` took the lock, then ran `BEGIN IMMEDIATE`; one
  `database is locked` left the lock held and every other thread hung; present in all 16 copies); no re-entrant
  `transaction()` (only Hypatia); no explicit `busy_timeout` (family A: Python's 5 s default; Scheherazade, Laplace,
  Nightingale: none); `foreign_keys` missing in Cassandra, Vitruvius, Midas; `executescript` committing implicitly, so a
  failed migration could leave partial DDL with the version not advanced.
* **Migration:** each `db.py` keeps its `MIGRATIONS` list and becomes
  `Database = functools.partial(sqlkit.Database, migrations=MIGRATIONS)`; calls to `db.transaction()` keep working.
  Family B (Babel, Daguerre, Prospero, Scheherazade, Funes, Laplace, Nightingale): turn `_migrate` /
  `_ADDED_COLUMNS` (PRAGMA table_info + ADD COLUMN) into callable migrations or an `on_open` hook; their per-thread
  connections can move to the shared connection. Dorian keeps `journal_mode="DELETE"`. `check_sqlite` in Argus,
  Borges, Echo, Funes-audio, Hypatia, Vulcan becomes `check_fts5`. `hub/backup.py` can call `backup_to`.
  Not covered on purpose: the soft-delete columns (too tied to each domain).

## `hoard_link.paths`

```python
clean_user_path(raw) -> str
unsafe_folder(path, *, data_dir=None, allow_data_subdir=None, lang="es") -> str | None   # reason or None
unsafe_file(path, *, data_dir=None, allow_data_subdir=None, lang="es") -> str | None
unsafe_output_dir(path, *, data_dir=None, allow_data_subdir=None, lang="es") -> str | None
is_inside(child, parent) -> bool
safe_member(base, name) -> Path          # zip-slip: ValueError for absolute names, drive letters, "..", escaping symlinks
hidden_parts(path) -> list[str]
reason(code, lang="es") -> str           # REASONS holds the es/en texts
```

* `clean_user_path` strips whitespace and the quote pairs Explorer's "Copy as path" adds, expands `~`, `$VAR` and
  `%VAR%`. Every `unsafe_*` function calls it first.
* Refused as a folder: a drive root, the user profile folder (`C:\Users\<name>`, `C:\Users`, `/home/<name>`, `/home`,
  the real home), system folders (`C:\Windows`, `Program Files`, `ProgramData`, `$Recycle.Bin`, `/etc`, `/usr` ...),
  configuration and secret folders anywhere in the path (`.ssh`, `.gnupg`, `.aws`, `.git`, `.config`, `AppData`,
  `node_modules`; below the system temp folder they do not count), the app's own `data_dir` (except
  `allow_data_subdir`: one path or a list, absolute or relative to `data_dir`) **and any folder that contains it**
  (the old forks let you watch the repo root and with it `mcp-token` and the database). Files: credential-looking
  names (`.env*`, `id_rsa`, `mcp-token`, `*.pem`, `*.key`, `*.kdbx` ...) plus the same folder rules on the parent.
* **Windows paths are analysed as Windows paths on every OS** (pure text: case-insensitive, `..` collapsed, trailing
  dots and spaces ignored, `\\?\` stripped, UNC shares); the existence check is skipped in that mode and policy runs
  *before* existence, so results do not depend on the machine. On Windows the real filesystem is used (`resolve()`).
* Differences from the old forks: system folders match only as the *top-level* folder of a drive (`D:\Proyectos\Windows`
  is fine, `D:\Program Files` is not); return value is `None` for "ok" (the forks returned `""`); messages are Spanish
  by default (`lang="en"` for the old English texts).
* **Bug fixed:** Borges (`store.py:97 add`) and Vulcan (`add_root`) accepted `C:\`, `C:\Windows` or the whole profile
  folder as an indexing root, with `.ssh` inside, and did not strip pasted quotes: the indexer reads the content and
  exposes it to the MCP tools. Also the open-coded `_inside` checks (DiskHoard zipsplit, Prospero `engine.py:1806`,
  Plato `_safe_member`, Prospero charpack import, Vitruvius `render_file_path`).
* **Migration order:** Borges and Vulcan first (`add` / `add_root`), then Kafka and Pygmalion (their `paths.py` becomes a
  thin wrapper that passes `data_dir` and `allow_data_subdir=("inbox", "workshop")`), then Daguerre `library.add_root`,
  Nightingale `services.py:300`, Vulcan `organize.py:84`, and the zip-slip sites.

## `hoard_link.net`

```python
can_listen(port, host="127.0.0.1") -> bool
find_available_port(preferred, *, span=20, host="127.0.0.1") -> int     # RuntimeError / ValueError
free_port(host="127.0.0.1") -> int
already_running(service, port, *, host="127.0.0.1", timeout=1.0) -> bool
wait_healthy(url, service=None, timeout=30.0, *, poll=0.2, sleep=..., clock=...) -> bool
open_in_browser(url) -> bool            # os.startfile on Windows, webbrowser elsewhere; http(s)/file URLs only
fetch_health(url, *, timeout=1.0) -> dict | None     health_url(base) -> str
```

* `already_running` = the port is taken **and** `GET /api/health` answers `{"service": service}`. Decide it before
  touching the data folder, then exit 0. HTTP probes use `urllib` with proxies off, so `HTTP_PROXY` cannot hijack a
  loopback call, and httpx is not needed.
* **Bugs fixed:** on Windows `SO_REUSEADDR` lets a probe bind a port that is already in use, so the old `can_listen`
  said "free"; Windows now uses `SO_EXCLUSIVEADDRUSE`. Eight apps (Argus, Borges, Cassandra, Echo, Vulcan, Nightingale,
  Funes-audio, Hypatia) had no "already running" pre-check, so a second start migrated the database and rotated the
  token before the bind failed. The B family (Babel, Funes, Prospero, Scheherazade, Daguerre) used `_port_in_use` or
  nothing and died with a uvicorn traceback.
* **Migration:** delete `port.py` and the `_already_running` of `__main__.py`; `scripts/launch.py` becomes
  `find_available_port` + `wait_healthy` + `open_in_browser` around the subprocess. Node: `port.js` / `port.mjs` and the
  `scripts/launch.mjs` copies in Links, Ledger, People, JobHunter, Cook.

## `hoard_link.appconfig`

```python
env_str(*names, default=None)    env_int(*names, default=None, minimum=None, maximum=None)    env_float(...)
env_flag(name, default=False)    # 0/false/no/off -> False, 1/true/yes/on -> True, else default
load_dotenv(path=None) -> dict   # never overrides the real environment; quotes, comments, "export", BOM
@dataclass AppPaths(app, root, data_dir, configured=False)
    .db_path (<data>/<app>.db)  .token_path (mcp-token)  .url_path (url)  .logs_dir  .backend_json_path  .cache_dir  .ensure()
resolve_paths(app, repo_root, *, env_prefix) -> AppPaths      # <PREFIX>_DATA_DIR or <repo_root>/data
```

* A blank variable counts as unset; an unparsable number is skipped, not an error. `AppPaths.configured` replaces
  `data_dir_configured`.
* **Migration:** each `Config` keeps its own fields and holds (or inherits from) an `AppPaths`; delete `_env`, `_int`,
  `_float`, `load_dotenv` and the five path properties. The Node apps use `envFlag` (Links and People
  `background.js` duplicated the `"0","false","no","off"` list) and `resolveDataDir`.

## `hoard_link.lanes`

```python
@dataclass(eq=False) Job(kind, key=None, fn=None, lane="default", every_s=None, first_delay_s=0.0, enabled=None, reason="schedule")

class LaneScheduler(lanes: {name: workers}, *, state_path=None, enabled=True, paused=lambda: False, on_error=None,
                    clock=time.time, tick_s=20.0, name="lanes")
    .register(job)  .submit(job) -> Job | None  .run_now(kind_or_job, timeout=240)  .enqueue_due(now=None) -> int
    .is_due(job, now=None)  .status() -> dict  .start()  .stop(timeout=5.0)

class JobQueue(db, lanes=None, *, table="jobs", clock=time.time, on_done=None, on_event=None, inline=False,
               max_waiting_s=1800.0, name="jobs")
    .register(kind, fn, *, lane="work", on_restart="interrupted"|"requeue")
    .submit(kind, payload=None, lane=None, *, label="", dedupe_key=None) -> job_id
    .get(id) -> dict (JobNotFound)  .list(*, state=None, kind=None, limit=50)  .cancel(id)  .wait(id, wait_s)
    .start() -> int  .stop()  .requeue_interrupted() -> int  .purge_finished(older_than_s)
class JobCtx: id, kind, payload, attempt, .progress(pct 0..100, msg=None, *, force=False), .check()
JobCancelled, JobNotFound, WaitingForResources(reason, *, retry_in_s=15.0), jobs_schema(table="jobs")
```

* `LaneScheduler` is the `scheduler.py` of Kafka, Phileas, Tantalus and Galton: lanes with N workers, jobs deduplicated
  by key (a job that is queued or running is not queued again), periodic jobs (`every_s` may be a callable for settings
  that change at runtime; `enabled` is a per-job predicate), `run_now` that waits (inline when the lanes are not started),
  `paused` that holds back only automatic jobs, a dedicated tick thread, injectable clock. **New:** `state_path`
  keeps the last-run times in a JSON file (atomic), so after a restart a job that ran two minutes ago is not run again
  and `first_delay_s` staggers the ones that never ran. A job that raises is logged, counted in `status()["jobs"]` and
  passed to `on_error`; it never stops its lane.
* `JobQueue` persists jobs in a table of a `sqlkit.Database` (`jobs_schema()` gives the SQL if you prefer to put it in a
  migration; the queue also creates it with `IF NOT EXISTS`). States: `queued`, `running`, `waiting`, `done`, `error`,
  `cancelled`, `interrupted`. Handlers receive a `JobCtx` and return a JSON-serialisable result.
  Cancel is cooperative (`ctx.check()` / `ctx.progress()` raise `JobCancelled`); `stop()` cancels with reason `shutdown`
  so running jobs end as `interrupted`. On `start()`, what was `running` becomes `interrupted` (default) or is queued
  again (`on_restart="requeue"`, for idempotent work); `waiting` jobs go back to `queued`. `WaitingForResources` (not
  enough VRAM now) parks the job in `waiting` and queues it again after `retry_in_s`, giving up after `max_waiting_s`.
  `progress` is 0..100 (the old queues used 0..1 fractions: multiply when migrating).
* **Bugs fixed:** state not persisted across restarts (everything overdue at boot); jobs lost on restart (Funes, in
  memory) or left `running` forever; no cancellation (Babel); four near-identical schedulers diverging.
* **Migration:** Kafka, Phileas, Tantalus and Galton replace `scheduler.py` with ~30 lines registering their jobs
  (`engine.scan_folders` etc. as `Job(...)`), passing `state_path=data_dir/"scheduler.json"`. Babel, Daguerre, Lumiere,
  Prospero move to `JobQueue` incrementally (medium risk; Lumiere's `kind in RENDER_KINDS` lane becomes
  `register(..., lane="render")`, Prospero's gpu / cpu / orchestrator lanes become three lanes, its
  `GPU_WAIT_TIMEOUT_S` becomes `max_waiting_s`). Watchers, Argus/Borges/Vulcan `worker.py` and the in-process pools are
  not covered.
* Node: `startBackground({name, intervalMs, firstDelayMs, tick, envFlag, env, log})` replaces the two near-identical
  `background.js` (Links, People): no overlapping passes, a failing tick is logged and the loop goes on, timers are
  unref'd, `envFlag` names the variable that turns it off (`LINKS_RESURFACE=0`). It returns `{name, enabled, stop()}`.

## `hoard_link.waiting`

```python
MAX_WAIT_S = 150                       # MCP clients cut a tool call at about 180 s
DONE_STATES = ("done", "error", "cancelled", "interrupted", "failed")
clamp_wait(wait_s) -> float            # 0..MAX_WAIT_S; None / junk / NaN / negative -> 0
wait_for(get_job, wait_s, *, poll=0.25, done_states=DONE_STATES, sleep=..., clock=...) -> dict
```

* Always returns the last state it saw (a copy) with `waited_s`; when it gave up the copy also has
  `still_running: True`, so the tool answers `{"status": "running", "job_id": ...}` and the agent asks again.
* **Bug fixed:** `wait_s` caps of 120 / 150 / 300 / 600 / 900 / 3600 / 7200 s against bridge timeouts of 90 / 180 / 300 /
  660 / 900 s: either the bridge cut first (Pygmalion 90 s, Lumiere 300 s, Funes 90 s + wait) or the client did at ~180 s
  and the job was orphaned.
* **Migration:** Pygmalion (`wait_s` 600 / 900 / 3600), Lumiere (7200 / 3600), Funes `scribe_import_file` (3600), Galton
  (600), Hypatia `teacher/jobs.py wait` (120), DiskHoard `disk_zip_status` (150), Prospero `api.py` `MAX_WAIT_S = 300`:
  clamp with `clamp_wait`, poll with `wait_for`, return the job with `still_running`.

## Node: `js/hoard-commons/server.js`

Same behaviour as the Python modules (checked against the shared vectors where it is the same function, and by
cross-tests where a file written by one language is read by the other):

```js
writeJsonAtomic(file, obj, { indent, fsync, retries, delayMs })   readJson(file, def)   writeTextAtomic   writeFileAtomic   replaceWithRetry
readOrCreateToken(file, minLen = 32)   readToken   writeUrl   readUrl   checkBearer(header, token)
newUlid(now?)   newId(prefix = "", { sep })   shortId(n)   isUlid   idTime
envStr(names, def)   envInt(names, def)   envFlag(name, def, env)   resolveDataDir(prefix, root, env)
validPort   canListen(port, host)   findAvailablePort(preferred, { span, host })   freePort   alreadyRunning(service, port, { host, timeoutMs })
MAX_WAIT_S   clampWait   waitFor(getJob, waitS, { poll, doneStates })   startBackground({ ... })
openDatabase(file, { migrations, busyTimeoutMs = 15000, wal, foreignKeys, synchronous })
   -> { raw, exec, run, get, all, tx(fn), schemaVersion(), getSetting, setSetting, backupTo, isOpen, close }
checkFts5(db)
```

* `openDatabase` needs `node:sqlite` (Node 22.5+); without it it throws a clear error and the rest of the module still
  works. `tx(fn)` is re-entrant (nested = `SAVEPOINT`), `fn` must be synchronous (a promise rolls back and throws),
  migrations are strings or `(raw) => void` in their own transaction with the `schema_version` row (same table shapes as
  Python: a database written by one language is opened by the other without re-applying anything), `busy_timeout`
  is set (the three `server/db.js` had none) and `close()` checkpoints the WAL (Ledger and People did not).
  `backupTo` uses `VACUUM INTO` through a temp file.
* **Migration:** Links, Ledger, People: `server/db.js` keeps `MIGRATIONS` and calls `openDatabase`; their module-level
  `depth` counter in `transaction()` goes away. `app.js` `writeToken` becomes `readOrCreateToken`. `port.js` /
  `background.js` are replaced as above. JobHunter and Cook use `writeJsonAtomic` for `scheduler.mjs` and the library
  files (`library.mjs:55`).

## What is not here

`guard`, `bridge`, `agent`, `proc`, `service` (SPA, logging, health), `backend_settings` and the UI layer are other
modules of the commons. `family._write_token_if_missing` still exists and should delegate to `tokens.read_or_create_token`.
