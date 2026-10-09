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

Version 0.8.1 also adds per-tool output-cap opt-out (`Tool(..., capped=False)`),
maps bare `LookupError` / `PermissionError` to `404 not_found` / `403 forbidden`,
and validates arguments as `400 invalid_arguments`. `family.install_fastapi` can
protect direct `POST /api/agent/<tool>` routes with the app token by enabling
`protect_tool_routes=True` together with `token=True`; it refuses the
insecure combination before installing routes. `guard.check_handler` and
`guard.wsgi_middleware` adapt the same host/origin rules to http.server and Flask.
Safe-method checks remain enabled by default; `guard_safe_methods=False` is an
explicit compatibility option for existing read-only GET routes.

Agenda rows can carry a trimmed `dedupe_key` (maximum 200 characters). Hub Today
retains existing id/sphere filtering and uses that key to prefer a row whose
`hoard://<app>/` link belongs to its source app when owners mirror the same item.

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

`backend_settings` and the UI layer are other modules of the commons; `proc` is documented in [media.md](media.md).
`family._write_token_if_missing` delegates to `tokens.read_or_create_token`. The guard, the agent kit, the service helpers and
the MCP bridge are Part 2 below.

# Part 2: guard, agent kit, service, bridge

What every app wrote around its API: the request guard, the `/api/agent` contract, `python -m`, the single-page-app server,
the MCP stdio bridge. Python modules are in `hoard_link/`, the Node twin of all four in `js/hoard-commons/express.js`.
Importing any of them needs the standard library only: `fastapi`, `starlette`, `pydantic`, `uvicorn`, `httpx` and `mcp` are
imported inside the functions that use them (a test imports the four modules with those packages blocked).

| Module | What it replaces | Node twin (`express.js`) |
|---|---|---|
| `guard` | 17 identical `guard.py` (Argus, Borges, Cassandra, Cicero, Echo, Funes/`audio_memory`, Galton, Hypatia, Kafka, Lumiere, Midas, Nightingale, Phileas, Pygmalion, Tantalus, Vitruvius, Vulcan), 4 `guard.js`/`guard.mjs` (Links, Ledger, People, Cook), 7 weaker variants (Daguerre `guard.py`, Babel `api.py:82`, Laplace `security.py`, Prospero `api.py:68`, Scheherazade `api.py:333`, Funes `api.py:269`, Dorian `selfhoard/api.py:70`) | `createGuard`, `checkRequest`, `parseAllowedHosts` |
| `agentkit` | `agent_tools.py` plumbing (17 + Mercator), `api/agent.py` (17), `api/deps.py` `tool()` (17), 9 `errors.py` | `makeAgentRoutes`, `capResult` |
| `service` | 24 `__main__.py` (9 with an `_already_running`), `startup.py` of Pygmalion / Galton and 4 `RotatingFileHandler` set-ups, the `main.py` catch-all and 2 exception handlers (17), `api/pwa.py` (15), `api/health.py` (9) | `installSpa`, `installErrorHandlers`, `runServer` |
| `bridge` | `mcp_server.py` (17 family-A copies of 107-167 lines, 7 static B-family bridges, DiskHoard, HomeHoard, Mercator), 5 Node bridges | `createBridge`, `postJson` |

Tests: `tests/commons/test_guard.py` (shared vectors `tests/vectors/guard.json`, 116 cases run against Python and Node),
`test_agentkit.py`, `test_service.py`, `test_bridge.py`, `test_express_js.py` (the last two also run a real stdio MCP server and the
Node bridge over JSON-RPC; set `HOARD_TEST_NODE_MODULES` to a folder with `express`, `zod` and `@modelcontextprotocol/sdk` to run
the Node side against the real packages too, and `HOARD_TEST_MCP1_PATH` to a folder with `mcp` 1.x to cover `FastMCP`).

## `hoard_link.guard`

```python
host_of(value) -> str                    port_of(value) -> int | None
parse_allowed_hosts(raw | list) -> tuple[str, ...]
is_allowed_host(host, port=None, allowed=(), *, strict_ports=False) -> bool
check_request(method, headers, port=None, allowed=(), *, dev_origins=DEV_ORIGINS, strict_ports=False) -> (403, message) | None
install_guard(app, *, port_getter, allowed_env="ALLOWED_HOSTS", allowed_hosts=None, dev_origins=DEV_ORIGINS, strict_ports=False)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")   DEV_ORIGINS (Vite 5173 / 5174 / 4173)   FRAME_DESTS   class GuardMiddleware
```

* The rules are the 17 copies' (Host must be loopback or in the allowed list; an Origin must pass the host rule or be a dev
  origin; cross-site requests only as top-level navigations; no form posts), so the family A frontends see no change:
  `403 {"error": "<message>"}`. Node: `createGuard({port | portGetter, allowedHosts, devOrigins, strictPorts})` (an Express
  middleware) and `checkRequest(method, headers, port, allowed, {devOrigins, strictPorts}) -> [403, message] | null`; both languages
  pass the same vectors, so Links' Spanish messages become the English ones.
* `install_guard` is a **pure ASGI** middleware (`app.add_middleware(GuardMiddleware, ...)`): no starlette import, no
  `BaseHTTPMiddleware` buffering, and it also guards **WebSocket** upgrades (Host and Origin checked, closed with 1008 before accept),
  which no copy did. `port_getter` is read per request, so the port found by the port search is the one enforced.
  `allowed_hosts` (a list, or the comma-separated `config.allowed_hosts`) and the variable named by `allowed_env`
  (`KAFKA_ALLOWED_HOSTS`) are merged; entries are `name`, `*.suffix` or `name:port` (pinned).
* **Opt-in `strict_ports`** is the B family's rule (`Host: 127.0.0.1:<port>` exactly): a Host naming another port is refused and
  a local Origin must carry this app's port (dev origins excepted). Off by default, because a dev proxy forwards
  `Host: localhost:5173` and the family A guard always let that through. `port` is only consulted with `strict_ports` or a pinned
  allowed host.
* **Bugs fixed:** the 7 weaker guards had no allowed-hosts list (the app only worked on loopback, never on a LAN name or a
  tailnet), 400 vs 403 and three different envelopes; Daguerre did not refuse `Sec-Fetch-Dest: iframe` reads (embeddable from another
  origin); Dorian trusted `request.client.host` (open to DNS rebinding, `Host` was never checked); Babel and Prospero answered
  400 `bad_host`; each app had its own `DEV_ORIGINS`.
* **Migration, family A (17 apps):** delete `guard.py`; `main.py` changes `install_guard(app, config.allowed_hosts)` to
  `install_guard(app, port_getter=lambda: config.port, allowed_hosts=config.allowed_hosts)` (the variable name is read from the
  config as before, or pass `allowed_env="KAFKA_ALLOWED_HOSTS"`). **Family B and Dorian** (Babel, Daguerre, Funes, Laplace,
  Prospero, Scheherazade): replace the middleware class with the same call, add `strict_ports=True` to keep the exact `host:port`
  behaviour or leave it off to accept dev proxies, and check that the frontend does not special-case the old 400 `bad_host` body
  (Prospero, Babel). **Node** (Links, Ledger, People, Cook, and new in JobHunter): `app.use(createGuard({ portGetter, allowedHosts: process.env.LINKS_ALLOWED_HOSTS }))`;
  delete `guard.js` / `guard.mjs`. DiskHoard (header token `X-DH-Token`), Gepetto, Plato, HomeHoard and Mercator were not audited
  and keep their own checks until someone does.

## `hoard_link.agentkit`

```python
@dataclass(frozen=True) Tool(name, description, input_model, annotations, run, timeout_s=None, capped=True,
                              undo=None, capture=None, track=None)   # run(ctx, args); the last three: accountable agents (0.8.2)
ann(read_only=False, destructive=False, idempotent=None, open_world=False, draft_safe=False) -> dict    # idempotent defaults to read_only
Empty                                      # a pydantic model without fields (built on first access)
tool_catalog(tools) -> [{name, description, annotations, inputSchema[, "x-timeout-s"]}]
call_tool(tools, ctx, name, arguments, *, cap=True, post=None) -> dict   # UnknownTool (a KeyError), ValidationError, ValueError
cap_result(data, limit=20_000)    uncapped()    is_uncapped()    confirm(flag, what)
class AppError(code, message, *, hint="", status=None, details=None)  .to_dict()  .STATUS  (subclass it)
make_agent_router(*, tools_fn, call_fn, token_fn, instructions, app_name, error_types=(),
                  reasons=False, data_dir=None, tools=None, ctx_fn=None) -> APIRouter   # the last four: accountable agents (0.8.2)
issues_of(error) / format_issues(error)     # pydantic or fastapi validation errors as [{loc, msg}] / "loc: msg; ..."
```

* **`call_tool`** validates the arguments with the tool's model, runs `tool.run(ctx, args)` (`ctx` is the app's `Services`), wraps a
  non-dict result as `{"result": ...}`, runs `post(result, args)` (Kafka's identifier masking goes there) and caps. `with uncapped():`
  (the web UI calling the same handlers, `deps.tool()`) skips the cap; `uncapped` is a context variable, so it follows threads that copy context.
* **`cap_result`** is Kafka's: over `limit` bytes of JSON the largest top-level list is halved until it fits (one item always
  stays) and `"truncated": {reason, original_lengths, hint}` says what was cut. New: when one huge string is what is left, it is cut
  too, and a list result is returned as `{"result": [...], "truncated": ...}`. The Node `capResult` gives byte-identical output (checked).
* **`AppError`**: stable `code`, `message`, `hint`, `status` (from `STATUS` by code, 400 otherwise), `details` merged into the body.
  `class KafkaError(AppError)` keeps the app's own constructor (the nine `errors.py` shrink to a status table and a subclass).
* **`make_agent_router`** serves `GET /api/agent/tools` (`{instructions, tools, app}`, no token) and `POST /api/agent/call`
  `{name, arguments, caller?}` with a **case-insensitive** `Bearer` (`tokens.check_bearer`, constant time, an empty token never matches).
  A callable that declares a parameter named `request` also gets the request (`call_fn(name, args, request)` reaches
  `request.app.state.services`); a sync `call_fn` runs in the thread pool. Errors are always JSON `{error, code, ...}`: `AppError` and
  `error_types` (any class with `.status` and `.to_dict()`) use their own; unknown tool 404 `unknown_tool`; a `KeyError` from the tool
  404 `not_found`; pydantic `ValidationError` 400 `invalid_arguments` with `issues`; `ValueError` 400 `invalid`; anything else 500
  `internal` (logged). Every call ends in `family.record_call` (the `agent.call` audit event).
* **Accountable agents (0.8.2, opt-in).** `X-Agent-Id` / `X-Agent-Session` headers (the bridge sends them from `HOARD_AGENT_ID` /
  `HOARD_AGENT_SESSION`), `reasons=True` (a `reason` of 3-300 characters on every write, else 400 `reason_required`), `data_dir=` (a write
  journal at `agent_journal.jsonl`, `GET /api/agent/journal`, `POST /api/agent/undo` with the per-tool `undo` / `capture` / `track`
  hooks, extra tokens with a `read_only` / `drafts` / `all` profile and their admin routes). See
  [accountable-agents.md](accountable-agents.md). With none of these arguments the router behaves exactly as before.
* **Bugs fixed:** 11 apps (Argus, Borges, Echo, Vulcan, Vitruvius, Nightingale, Funes-audio, Cassandra, Hypatia, Mercator, Cicero
  without `uncapped`) had no result cap, so a listing could put hundreds of KB into the model's context, and only 6 had `_confirm`;
  `agent.py` compared `Bearer ` case-sensitively while `family.py` did not; an unexpected exception became an HTML/plain-text 500 that
  the bridge could not parse (Nightingale parsed it by hand); validation errors had no machine-readable `issues`.
* **Migration, family A:** `api/agent.py` and the `tool()` helper of `api/deps.py` become
  `make_agent_router(tools_fn=lambda: tool_catalog(TOOLS), call_fn=lambda name, args, request: call_tool(TOOLS, services(request), name, args, post=mask), token_fn=lambda request: services(request).token, instructions=AGENT_INSTRUCTIONS, app_name="kafka", error_types=(KafkaError,))`
  and `deps.tool` is `with uncapped(): return call_tool(...)`. `agent_tools.py` keeps `TOOLS` and the argument models and imports `Tool`, `ann`,
  `Empty` from here; call `family.install_fastapi(app, ..., contract=False)` so the two contract installers do not both register `/api/agent/*`.
  Apps that already cap inside their tools (Kafka, Phileas, Tantalus, Pygmalion, Midas, Galton) can drop their own `cap_result` calls; the
  other 11 get the cap from `call_tool`. **Family B** (Babel, Daguerre, Funes, Laplace, Prospero, Scheherazade): their per-tool
  `POST /api/agent/<tool>` routes (introspected by `family.install_fastapi`) are **unauthenticated**, only `/api/agent/call` checks the
  token; the migration is to require the same Bearer token on them (a dependency on the router) or to drop them and keep only `/call`
  (nothing in the bridges calls them: `/call` is what the catalogue path uses). Until then any local process that passes the guard can
  run a destructive tool without the token. **Node** (Links, Ledger, People, JobHunter, Cook): `makeAgentRoutes({ app, tools, z, token, instructions, recordCall: family.recordCall }).install(app)`
  replaces `agent-routes.js` (the zod issues become the same `invalid_arguments` + `issues` body, the result is capped at 20 000 bytes, and
  `writeToken` becomes `readOrCreateToken` as in Part 1).

## `hoard_link.service`

```python
setup_logging(app_name, logs_dir, *, level=INFO, max_bytes=2_000_000, backups=3, loggers=(), console=True) -> Path | None
say(message)   rotate_log(path, *, max_bytes=2_000_000, backups=3) -> bool   install_excepthook(logger)
install_spa(app, static_dir, *, api_prefix="/api")      install_error_handlers(app)
install_pwa(app, *, name, short_name, theme, background, cache, icons=(), start_url="/", lang="en", static_dir=None, version="0")
health_router(service, version, *, extra=None) -> APIRouter
run_main(*, service, package, default_port, app_factory, data_dir_env=None, port_env=None, argv=None, open_browser_default=True,
         default_data_dir=None, title=None, serve=None) -> int
```

* **`setup_logging`**: `<logs_dir>/<app_name>.log`, rotating, UTF-8, opened on the first record (a second instance that exits early
  creates nothing), idempotent (calling it again replaces its own handlers, never stacks them), console handler only when there is a
  console (`pythonw.exe` has none), `httpx` held at WARNING. `rotate_log` is the same rotation for a file a child appends to
  (the bridge's autostart log).
* **`install_spa`** (call it after the routers are included; `install_pwa` keeps it last): `/api/*` nothing matched is `404 {"error": "Not found.", "code": "not_found"}`
  for any method; a path that is a file **inside** `static_dir` is served (`..`, `%2e%2e`, `%2f`, backslashes, drive letters, NUL,
  dotfiles and symlinks out of the folder all fall through; the old `resolve() in candidate.parents` check did not refuse dotfiles
  and served `index.html` for a missing asset); hashed `assets/` files are `immutable`, every other response is `no-cache`; a missing
  asset or static-extension file is a 404, not an HTML page; other paths get `index.html` (`no-cache`); an unbuilt folder is `503 {"code": "not_built"}`.
  Explicit media types for `.js` / `.css` / `.svg` / `.wasm` / fonts (Windows maps `.js` to `text/plain` when the registry says so).
* **`install_error_handlers`**: `{"error", "code"?, "hint"?, "details"?, "issues"?}` for `HTTPException` (a dict detail with `error` passes
  through), request validation (400 `"loc: msg; loc: msg"`, `code: "invalid_arguments"`, `issues`), `AppError` and any other
  exception (500 `internal`, logged). Replaces the two handlers pasted into 17 `main.py` and the `{error, message}` / `{ok:false,error,code}` variants.
* **`install_pwa`**: `/manifest.webmanifest` and `/sw.js`. The worker is **network-first for navigations** (the cached copy only
  answers when the network fails), cache-first for the hashed `/assets/`, and never touches `/api/`; its cache name is
  `<cache>-<hash of index.html>`, so a new build starts a new cache and activation deletes the old ones. The 15 `pwa.py` served a
  cache-first worker, and without `no-cache` on `index.html` a browser kept an old `index.html` pointing at deleted hashed assets
  (blank page until a forced reload). Both files are `no-cache`.
* **`health_router(service, version, extra=...)`**: `{service, version, **extra, hoard_link: family.health_block()}`; `extra` is a dict
  or a callable (it may take `request`); it cannot override `service` or `hoard_link` and a failing `extra` still answers 200 with
  `health_error` (the launcher, the bridge, `net.already_running` and the hub all poll this).
* **`run_main`** is the whole `__main__.py`:
  `raise SystemExit(run_main(service="kafka-hoard", package="kafka_hoard", default_port=5200, app_factory="kafka_hoard.main:create_app", data_dir_env="KAFKA_DATA_DIR", port_env="KAFKA_PORT", open_browser_default=False))`.
  `--port`, `--data-dir`, `--host`, `--no-browser` / `--browser`. **Before touching the data folder** it asks `net.already_running` and, if
  this very app answers `/api/health` on the wanted port, prints it and returns **0** (opening the browser at it when asked). Then the
  port: `--port`, else `port_env`, else `default_port`; `PORT_STRICT=1` makes a taken port an error (return 1), otherwise the next
  free one. `--data-dir` and the chosen port are exported to the environment (`data_dir_env` / `port_env`, else `HOARD_DATA_DIR` /
  `HOARD_PORT`) **before** the factory runs, so the app's `Config.from_env()` and its guard see them; logging goes to
  `<data>/logs/<service>.log` (default data folder: `<repo>/data`, the folder above the package; `default_data_dir` overrides). The
  factory is `"pkg.main:create_app"` (zero-argument, or taking `port`), `"pkg.main:app"` (an ASGI object) or a callable. `uvicorn.run`
  with `log_config=None` (no console needed); `serve(app, host=, port=)` replaces it. The browser opens only if `open_browser_default`
  or `--browser` is set and neither `--no-browser` nor `HOARD_NO_BROWSER` is: the bridge's autostart sets both, and apps that never
  opened a browser on `python -m` pass `open_browser_default=False`. Exit codes: 0 stopped / already running, 1 failed to start, 2 bad arguments.
* **Deviation from the old apps:** the already-running check happens **regardless of `PORT_STRICT`** (the nine apps that had it only
  ran it in strict mode, so a double click started a second copy on the next port); to run two instances on purpose pass different `--port`s.
* **Bugs fixed:** eight apps (Argus, Borges, Cassandra, Echo, Vulcan, Nightingale, Funes-audio, Hypatia) migrated the database and
  rotated the token before the bind failed; the B family died with a uvicorn traceback; only Pygmalion and Galton logged to a rotated
  file in the family A, so an autostarted app left no trace; an uncaught exception without a console vanished (`install_excepthook`).
* **Migration:** each `__main__.py` is the `run_main(...)` call above; `main.py` loses `spa()`, the two exception handlers and the guard
  class: `install_error_handlers(app)`, `install_guard(...)`, the routers, then `install_pwa(...)` and `install_spa(app, STATIC_DIR)` last;
  `api/health.py` becomes `health_router(SERVICE, __version__, extra=lambda request: {...})` (or `app.include_router`). Babel, Daguerre and
  Prospero already had `no-cache` and `StaticFiles("/assets")`: use `install_spa` and delete their `is_relative_to` copies; their frontends read `message`, so
  check the TypeScript that handled `{error, message}` before switching to `{error}`. **Node:** `runServer({ service, createApp, port, onShutdown })`
  is Links' `index.js` (SIGINT / SIGTERM, 15 s force exit); Ledger and People had no graceful shutdown, so their WAL was not checkpointed
  (give them `onShutdown: () => db.close()`); `installSpa(app, DIST, { express })` and `installErrorHandlers(app)` replace the catch-all and
  the error middleware of the four `app.js`.

## `hoard_link.bridge`

```python
CatalogBridge(*, app, service, package, default_port, data_dir_env=None, token_env=None, token_file=None, url_file=None,
              default_timeout=90.0, autostart=True, refresh_tools_s=60.0, tool_timeouts=None, image_content=False,
              title=None, env_prefix=None, root=None, heartbeat_s=10.0, base_url=None)
    .run_bridge()  .build_server()  async .tools()  async .call(name, arguments, progress=None) -> BridgeResult(content, is_error, body)
    .base_url .data_dir .token() .healthy() .start_app()
ensure_running(package, port, *, service, data_dir, wait_s=45, env=None, cwd=None, port_env=None, log_name=None, args=()) -> bool
bridge_token(app, data_dir=None, *, token_env=None, token_file=None, env_prefix=None) -> str     # FileNotFoundError names the file
tool_timeout(tool, default, *, arguments=None, overrides=None) -> float
```

An app's `mcp_server.py` becomes (the app passes `root=__file__` so `<root>/data` and the autostart working directory are the repo):

```python
from kafka_hoard.hoard_link.bridge import CatalogBridge
CatalogBridge(app="kafka", service="kafka-hoard", package="kafka_hoard", default_port=5200, data_dir_env="KAFKA_DATA_DIR",
              title="Kafka's Hoard", root=__file__, tool_timeouts={"pdf_*": 175, "images_*": 175}).run_bridge()
```

* Variables are derived from `app`: `KAFKA_URL`, `KAFKA_PORT`, `KAFKA_TOKEN`, `KAFKA_TOKEN_FILE`, `KAFKA_BRIDGE_AUTOSTART` (`env_prefix` overrides) plus `data_dir_env`.
  The URL is `$KAFKA_URL`, else the app's `data/url` (`url_file`), else `127.0.0.1:<$KAFKA_PORT or default_port>`; only loopback `http` is accepted.
* **Catalogue refresh:** `GET /api/agent/tools` is repeated when older than `refresh_tools_s` or when a call names a tool the bridge does
  not know (the 17 bridges listed once, so an app that started after its workspace, or gained a tool, stayed stale until a restart); a failed
  refresh keeps the last list. A tool still unknown after the refresh answers `unknown_tool` without a request.
* **Timeouts:** `tool_timeouts` (exact names or `fnmatch` patterns) > the catalogue's `x-timeout-s` (`Tool(timeout_s=...)`) > `default_timeout`;
  a `wait_s` argument stretches it to `clamp_wait(wait_s) + 30` s so the bridge never cuts before the app's own wait ends (Galton 660 s, Lumiere 300 s,
  Kafka's 175 s for `pdf_*` become `default_timeout` / `tool_timeouts` / `x-timeout-s`). Calls longer than `heartbeat_s` send `report_progress`
  **heartbeats** (a no-op unless the client asked for progress) so clients that reset their timeout on progress keep waiting (Links' Node bridge only).
* **`outcome_unknown`:** a tool that is not read-only whose request was sent but got no answer (timeout, connection dropped) returns
  `{"error", "code": "outcome_unknown", "status": "outcome_unknown", "outcome_unknown": true, "reconcile_action": "read_current_state_before_retry"}`
  (Ledger's and People's Node semantics; a Python bridge gave an error identical to "nothing happened"). A refused connection is **not** unknown: the request never left.
  A tool the catalogue does not list counts as a write.
* **Errors:** the app's envelope (`error`, `code`, `hint`, `issues`, `details`, `candidates`, `key`, `params`) is forwarded and results carry `isError`; 401 says
  which token file was refused (Cicero's message, now everywhere); a missing token file with the app running says so instead of offering to start the app;
  a non-JSON answer is reported with its first 200 characters.
* **Transport:** `httpx` with `trust_env=False` always (Prospero, Laplace and Nightingale did not set it, so `HTTP_PROXY` could capture loopback calls),
  or `urllib` with proxies off when httpx is not installed. `image_content=True` turns a result's `_image: {data, mime}` into an MCP image part (Lumiere).
* **Autostart:** when the app does not answer, `python -m <package>` is launched detached without a console window (`proc.popen(detached=True)`),
  with `PORT_STRICT=1`, `HOARD_NO_BROWSER=1` and `<APP>_PORT`, from `root`; stdout / stderr go to `<data>/logs/<app>-app.log`, **rotated** at 2 MB (it grew without limit);
  one child per process (later callers wait for it), a child that exits with an error ends the wait at once instead of after 45 s;
  `<APP>_BRIDGE_AUTOSTART=0` or `autostart=False` turns it off. A failed call to a stopped app starts it and retries once.
* **`mcp` versions:** `mcp` 1.x (`FastMCP`, what the apps pin: `mcp>=1.10,<2`) and 2.x (`MCPServer`, renamed; `FastMCP` no longer exists) both work; `mcp`
  is imported only by `build_server()`. The server name is `service`, the instructions come from the catalogue.
* **Migration, family A (17 bridges):** replace `mcp_server.py` with the call above. Cicero's 401 handling, Tantalus' / Kafka's error details and Galton's
  660 s are now the default behaviour or `default_timeout=660`; Lumiere passes `image_content=True`, Kafka `tool_timeouts`, Hypatia `default_timeout=180`;
  Nightingale gains autostart and `trust_env=False`. The tests that monkeypatched `bridge.ensure_running` / `_healthy` (Argus `tests/test_bridge_autostart.py`)
  move to `CatalogBridge(...).healthy` / `start_app` or to `ensure_running(...)`. **Family B** (Babel 315 lines, Daguerre 340, Funes 476, Laplace 544,
  Scheherazade 408, Prospero 1653) and **Dorian** (134) keep hand-written `@mcp.tool` functions that duplicate their schemas; they can adopt `CatalogBridge`
  once their backends serve a catalogue (`family.install_fastapi(contract=True)` already builds one from the per-tool routes, and Prospero's
  `descriptions_from_fastmcp_source` gives it the texts): that is the biggest and lowest-priority migration, and it is also the moment to put the token on their routes.
  **DiskHoard** (raw JSON-RPC, `X-DH-Token`, 900 s), **HomeHoard** (`http.server` bridge) and **Mercator** (52 lines) migrate only if they adopt `/api/agent/*`.
  **Node:** `createBridge({ app, service, version, McpServer, StdioServerTransport, tools, instructions, baseUrl | urlFile, token | tokenFile, callTimeoutMs, defaultTimeoutMs, heartbeatMs })`
  with the SDK classes the app already imports replaces `mcp.js` and `bridge-call.js` (Links), the `fetch` bridges of Ledger / People / JobHunter, and gives Ledger's
  `outcome_unknown` and Links' heartbeats to all of them; Cook's `apps/mcp/server.mjs` keeps its in-process fallback and can use `postJson`. No Node bridge autostarts the app (Cook's does); that part is not in `createBridge`.

## Node: `js/hoard-commons/express.js`

```js
createGuard({ port, portGetter, allowedHosts, devOrigins, strictPorts })     checkRequest(method, headers, port, allowed, { devOrigins, strictPorts })
hostOf  portOf  parseAllowedHosts  isAllowedHost  LOCAL_HOSTS  DEV_ORIGINS
capResult(data, limit = 20000)    errorBody(error) -> { status, body }
makeAgentRoutes({ app, tools, callTool, z, toJsonSchema, token, tokenGetter, instructions, capLimit, recordCall }) -> { catalog, tools, call, install(app) }
installSpa(app, distDir, { express, apiPrefix })    installErrorHandlers(app, { log })
runServer({ service, createApp, port, host, onShutdown, forceMs, log, exit, signals }) -> { server, port, shutdown }
createBridge({ app, service, version, McpServer, StdioServerTransport, tools, instructions, baseUrl, urlFile, portFile, defaultPort, token, tokenFile,
               callTimeoutMs, defaultTimeoutMs, heartbeatMs, messages, z }) -> { server, handlers, call, start() }
postJson(base, pathname, payload, { token, timeoutMs }) -> { status, ok, body, raw }     # rejects with .code and .connected
```

No npm dependency: `express`, `zod` and the MCP SDK are passed in (`installSpa(app, dist, { express })`, `makeAgentRoutes({ z })`,
`createBridge({ McpServer, StdioServerTransport })`). Checked against real Express 4.22 and 5.2, zod 4 and `@modelcontextprotocol/sdk` 1.31.
`makeAgentRoutes` expects `express.json()` before it; `installSpa` after the API routes; `installErrorHandlers` last. `postJson` uses `node:http` (fetch
gives up on a response that takes over five minutes to start) and tells whether the connection was made, which is what separates "app stopped" from
`outcome_unknown`.

## Deviations and limits

* `check_request` / `is_allowed_host` take the app's `port` (the request's Host and Origin ports are only compared with `strict_ports`), and the dev origins
  include Vite's preview port 4173.
* The agent router and `call_tool` are synchronous-tool oriented: `tool.run` is called without `await` (an `async` tool needs its own wrapper or an `async` `call_fn`).
* `run_main` does not open a browser unless asked; `scripts/launch.py` (the launcher) is not covered here. Setting the app's `Config.port` from the chosen port
  is done through the environment, so an app whose `Config` ignores `<APP>_PORT` must read it.
* The bridge does not send `notifications/tools/list_changed` when the catalogue changes; clients see the new tools at their next `tools/list`.
