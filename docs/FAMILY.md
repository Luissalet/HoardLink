# The family contract (Hoard Link 0.4)

Twenty local apps, one assistant, one machine. This page is the contract
every app of the family follows so the hub can list, start, back up, call
and listen to all of them with one piece of code — and so a new app is on
the bus the day it ships. Everything here is loopback-only.

## 1. The manifest: `faustus-plugin.json`

One file at the repository root. Faustus reads it to adopt the app; the
hub reads the same file to list, start and back it up. Keys Faustus does
not know are refused **unless they start with `x-`** (or `x_`): that is
the extension namespace — `x-tools`, `x-desktop`, `x-launch_hint_linux` —
skipped by Faustus, free for every other reader.

The hub takes from it: `id`, `name`, `purpose`, `capabilities`,
`app.url_default`, `app.health` (`path` + `expect.service`),
`app.launch_hint`, and from `defaults`: `TOKEN_FILE` (where the app keeps
its bearer token; default `<folder>/data/mcp-token`) and `DATA_DIR`
(what backups copy; default the token file's folder).

## 2. The agent contract

```
GET  /api/health                → {"service": "<id>-hoard", ..., "hoard_link": {version, family, events, app, hub}}
GET  /api/agent/tools           → {"instructions": "...", "tools": [{name, description, inputSchema, annotations}]}
POST /api/agent/call            → Authorization: Bearer <data/mcp-token>
                                  {"name": "<tool>", "arguments": {...}, "caller": "<who asks>"}
```

The token is written by the app into its own `data/mcp-token` (Node apps
rotate it at every start; that is fine — the hub reads it at call time).
The `hoard_link` block in `/api/health` is how the hub's audit knows the
app is on the contract and emits events.

The six oldest apps (Babel, Laplace, Funes, Daguerre, Prospero,
Scheherazade) expose one `POST /api/agent/<tool>` per tool. They keep
those routes and gain the shared two through
`hoard_link.family.install_fastapi(app, "<id>", data_dir,
mcp_source="mcp_server.py")`, which builds the catalogue from their
FastMCP adapter's docstrings (no `mcp` import) and dispatches `call` to
the existing endpoints behind a token it writes itself.

**Tool descriptions**: the first line ≤ 110 characters, with the words an
English *or* Spanish request would use; read-only tools carry
`readOnlyHint`. `hub_family_audit` flags the ones that exceed it.

## 3. The bus: events

`POST <hub>/api/events` with the app's own bearer token:
`{"type": "scribe.transcript.done", "data": {"session_id": 12}}` — the hub
records `source` from the token, so an app can only speak for itself.
Types are dotted lower-case; `data` carries ids and short titles, never
contents (16 KB cap). Conventions:

| type | who | data |
|---|---|---|
| `agent.call` | every app, one per `/api/agent/call` | `tool, ok, ms, caller, error?` |
| `<app>.<thing>.<verb>` | the app | ids: `scribe.transcript.done {session_id, title}`, `links.watch.new {watch, title, url, link_id}` |
| `hub.app.started` / `stopped` | hub | `app, pid` |
| `hub.call` | hub, one per proxied call | `app, tool, ok, ms, caller` |
| `hub.backup.done` / `failed` / `restored` | hub | `snapshot, apps, files, new_bytes` |
| `hub.rule.ran` / `hub.job.ran` | hub | `rule|job, ok, ms, results` |
| `hub.lease.granted` / `released` | hub | `lease_id, owner, gpu, vram_mb` |
| `cassandra.incident.opened` / `closed` | Cassandra | `incident_id, app, to_state, probable_cause` |

Reading: `GET /api/events?since_id=&type=scribe.*&source=&since=&until=&text=&limit=`
(newest first; `order=asc` with `since_id` to tail), `GET /api/events/stream`
(Server-Sent Events, `since_id`), `GET /api/events/stats`. The hub keeps
the last 20 000 rows (`events_keep`); Cassandra mirrors them for good.

From an app: `hoard_link.family.emit(type, data)` (Python, fire-and-forget)
or `family.emit(type, data)` from `server/hoard-link.js` (Node).
`HOARD_EVENTS=0` silences an app.

## 4. Calls between apps: the proxy

`POST <hub>/api/apps/<id>/call` `{"tool": ..., "arguments": {...}}` with
*any* family token (the caller's own). The hub calls the target with the
target's token and answers `{ok, app, tool, status, result|error, contract, ms}`.
No app needs another app's port or token file any more (Funes's federated
`recall` uses it as its fallback). From code: `family.call("hypatia",
"cards_suggest", {...})`. `GET /api/apps/<id>/tools` lists what an app
offers.

## 5. Rules and jobs

A **rule** is `when` (an event pattern) → `then` (actions); a **job** is a
clock (`every: "6h"` or `at: "04:00"` + `days`) → `then`. Both live in
`data/rules.json` / `data/jobs.json`, both are edited from the hub's UI or
the `hub_rule_*` / `hub_job_*` tools, both record every run as an event
and in their `last` field.

Actions: `{kind: tool, app, tool, args}`, `{kind: hub, tool, args}`,
`{kind: event, type, data}`, `{kind: start_app|stop_app|restart_app, app}`,
`{kind: profile_start|profile_stop, name}`. Strings may use
`${event.data.x}`, `${event.type}`, `${event.source}`, `${today}`,
`${now}`, `${results.0.result.id}`. Rules run on one worker thread, in
order; never fire on `hub.rule.*`, never on an event one of their own
actions emitted, and respect `cooldown_s` (5 s default). A job missed while
the hub was down runs at the next tick (`catch_up`), once per day for
`at` jobs.

## 6. Backups

`POST /api/backups/run` (`hub_backup_run`) snapshots every app's
`DATA_DIR` plus the hub's own data into `data/backups/` (or `backup.dir`
in `hub.json`): files stored once by SHA-256 under `objects/`, one
manifest per snapshot under `snapshots/`. SQLite files are copied through
the online backup API (consistent while the app writes); `logs/`,
`profiles/`, caches, `*-wal/-shm`, files over `max_file_mb` (512) and
`backup.exclude` globs are skipped and listed. `restore` writes one app's
files to a side folder (`data.restored-<stamp>`) or in place when the app
is stopped (the live folder is moved aside first); `verify` re-hashes;
`prune` keeps the last N snapshots and drops unreferenced objects. The
example job "Nightly backup" at 04:00 is one click away in the Jobs tab.

## 7. Vendoring the library

Python apps vendor `hoard_link/` (minus `hub/`) next to their package and
import it relatively (`from .hoard_link import family`). Node apps copy
`js/hoard-link.js` to `server/hoard-link.js`. **`python
scripts/sync_vendored.py`** in this repository refreshes every copy in the
sibling folders (`--install <ids>` vendors it where missing, `--dry-run`
shows what would change); `VENDORED.txt` records the version.
`hub_family_audit` reports which app lags.

## 8. The audit

`GET /api/audit` (`hub_family_audit`): per app — running, which contract
shape it answers (`shared` / `per-tool` / unknown), token file present,
`hoard_link` block and `events` in its health, vendored version vs the
hub's, data folder present and git-ignored, size, tool names whose first
line is too long — and a summary with recommendations. Cassandra's
`secrets_audit` is the other half (what git tracks).
