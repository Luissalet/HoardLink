# The family contract (Hoard Link 0.6)

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
`{"type": "links.watch.new", "data": {"watch": 12}}` — the hub
records `source` from the token, so an app can only speak for itself.
Types are dotted lower-case; `data` carries ids and short titles, never
contents (16 KB cap). Conventions:

| type | who | data |
|---|---|---|
| `agent.call` | every app, one per `/api/agent/call` | `tool, ok, ms, caller, error?` |
| `<app>.<thing>.<verb>` | the app | ids: `links.watch.new {watch, title, url, link_id}` |
| `hub.app.started` / `stopped` | hub | `app, pid` |
| `hub.call` | hub, one per proxied call | `app, tool, ok, ms, caller` |
| `hub.backup.done` / `failed` / `restored` | hub | `snapshot, apps, files, new_bytes` |
| `hub.rule.ran` / `hub.job.ran` | hub | `rule|job, ok, ms, results` |
| `hub.lease.granted` / `released` | hub | `lease_id, owner, gpu, vram_mb` |
| `hub.repos.scan` | hub, after each repository scan | `repos, with_issues, unpushed_total, errors, scan_errors` |
| `hub.repos.issue` | hub, a repository gained an error-level issue | `repo, kind, severity, text, url` |
| `hub.repos.fetch` / `push_command` | hub | `repo, ok, error, ms` / `repo, command` |
| `tantalus.alert` | Tantalus | `event_id, type, severity, title, summary, url, price, currency, confidence, watcher_name` |
| `phileas.update` | Phileas | `event_id, type, severity, title, summary, url, shipment_id, status, eta_likely, label, carrier, tracking_number` |
| `phileas.status` | Phileas | `shipment_id, from, to, label, source` |
| `cassandra.incident.opened` / `closed` | Cassandra | `incident_id, app, service_kind (app\|external\|user), to_state, probable_cause` |
| `phileas.trip.new` / `phileas.trip.changed` | Phileas (travel) | `trip_id, title, start, end` |
| `phileas.checkin.open` / `phileas.trip.update` | Phileas (travel) | `trip_id, segment_id, title, opens_at|kind` |
| `cookhoard.pantry.expiring` | CookHoard (daily) | `count, items` (names, at most 10) |
| `cookhoard.recipe.imported` / `cookhoard.menu.planned` | CookHoard | `recipe_id, title, source` / `week` |
| `homehoard.item.added` / `homehoard.maintenance.done` | HomeHoard | `item_id, name` / `task_id, title, next_due` |
| `homehoard.maintenance.due` | HomeHoard (daily) | tasks due within 7 days: ids and titles |
| `funes.minutes.ready` | Funes | `session_id, title, action_items, started_at` (People ingests it) |
| `people.commitment.added` / `done` / `overdue` | People | `commitment_id, direction, person_id, due` |
| `hub.link.chat` | hub, one per model call served to an app | `app, capability, ok, ms, model` |

Reading: `GET /api/events?since_id=&type=links.*&source=&since=&until=&text=&limit=`
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

Six **recommended rules** ship with the hub and are installed together by
the "Install the recommended rules" button (Rules tab) or
`hub_rule_install_defaults` (`POST /api/rules/install-defaults`, idempotent
by rule id; `refresh: true` brings an installed rule's `when`/`then` back to
the current template without touching what you set — enabled, cooldown):
`hub.app.stopped` → backup of that app; `links.watch.new` → a `digest.item`
event (what the daily-recap skill reads); `tantalus.alert` (a confirmed restock,
price drop, pre-order or new product from Tantalus's Hoard) → a `digest.item`
with `title, url, watch, kind`; `phileas.update` (any notification of Phileas's Hoard,
the shipment tracker) → a `digest.item` with `title, url, watch` (the parcel) and `kind`; `cassandra.incident.opened` with
`to_state: down` and `service_kind: app` → start the app again (cooldown 5 min);
`hub.repos.issue` (a repository gained an unfinished rebase, a stale `index.lock`, a
tracked secret or failing CI) → a `digest.item` with `title, url, watch` (the repository)
and `kind`.

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
`backup.exclude` globs are skipped and listed — as is a file the app holds
exclusively (a DuckDB database while Nightingale runs): it is reported as
"locked by the app", and the rule template "Backup when an app stops"
catches it the next time that app is stopped. `restore` writes one app's
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
`hub_family_audit` reports which app lags. The vendored `hoard-link.js` is
the Node client for everything on this page, including the models (section
10): `chat()` and `linkStatus()` arrived in 0.6, so a Node app that wants
them refreshes its `server/hoard-link.js` with the script.

## 8. The audit

`GET /api/audit` (`hub_family_audit`): per app — running, which contract
shape it answers (`shared` / `per-tool` / unknown), token file present,
`hoard_link` block and `events` in its health, vendored version vs the
hub's, data folder present and git-ignored, size, tool names whose first
line is too long — and a summary with recommendations. Cassandra's
`secrets_audit` is the other half (what git tracks).

## 9. References between apps

An artifact keeps its own app as the source of truth. A receiving app records
`source_ref` (`hoard://<app>/<kind>/<encoded-id>`) and `source_revision`
(`sha256:<hex>` of the exact exported bytes) beside its local record. It also
records its own local revision when imported. Repeating the same import with the
same source revision returns the existing record. When the source revision has
changed, a receiver offers a comparison or a new proposed version; it must not
overwrite local edits. References contain no token, file path or private text.
`hoard_link.artifacts` creates and parses these references and computes the
revision. For example, a chapter exported from Scheherazade can be linked to a
Writer manuscript item, then a Prospero asset can point to that item.

`python scripts/story_to_writer.py --world <id> --session <id> --project <id>`
is a concrete chapter handoff. It pages through the entire Scheherazade export
and calls Writer's `wh_import_story_session` once. Set `WH_BRIDGE_TOKEN` in
the process environment and keep both local apps running. Repeating the command
returns the same writing; a changed source is reported without replacing local
work. `--refresh` applies a changed source only when Writer's imported text is
still untouched. The script sends its token only to Writer's loopback API.

## 10. Models for every app

Python apps reach the local models through the vendored `hoard_link.Link`.
Node apps cannot (Link is Python) and a small standard-library-only app may
not want `httpx`. The hub is Python, always running and already has Link, so
it serves the same thing over HTTP: the same model resolution, the same GPU
lease when a model has to be loaded, the same reasoning effort, the same
wait for an idle server. Loopback only, like the rest of the hub.

```
POST /api/link/chat      Authorization: Bearer <the app's own token | the hub's data/mcp-token>
body: {"capability": "llm" | "vision",               default "llm"
       "messages": [{"role": "system"|"user"|"assistant", "content": "..."}],
       "images": ["<base64 png/jpg>", ...],          vision only; attached to the last user message
       "json": true | {<JSON Schema>},               ask for JSON; the hub parses it
       "effort": "off"|"low"|"medium"|"high"|"max",  how hard the model reasons; omitted = the server's default
       "max_tokens": 2048, "temperature": 0.2,
       "timeout_s": 300}                             default 300, at most 1800; covers the queue and the call
200 -> {"ok": true, "text": "...", "json": <parsed or null>, "model": "...", "provider": "...",
        "ms": 1234, "queued_ms": 0, "usage": {...}, "effort": "low"}
```

| Status | `error` | Meaning |
|---|---|---|
| 400 | `bad_request` | The body is not what the contract says (`detail` names the field). |
| 401 | — | No family token, or one the hub does not know. |
| 413 | `too_large` | Over 12 MB (messages and base64 images together). Refused before the body is read. |
| 503 | `no_model` | Nothing can serve that capability right now; `detail` has the reasons the resolution collected. |
| 503 | `gpu_busy` | A model had to be loaded and no GPU memory was granted in time. |
| 504 | `timeout` | `timeout_s` ran out, waiting in the queue or in the model. |
| 502 | `backend_error` | The server answered with an error (`backend_status`, `provider`, `detail`). |

Never a made-up answer: with no model the answer is `503 no_model`, and an
app must show that state (or use its deterministic fallback), not pretend.

**JSON.** `"json": true` asks for one JSON object. A schema object is passed
to the server as its response format when it supports one (a server that
rejects the field is asked again without it), and in both cases a system
instruction says the same in words, so servers that ignore the field still
comply. The hub reads the answer leniently: a bare document, a fenced block,
or the first balanced object/array inside prose. `json` is the parsed value;
`text` is always the model's raw answer, so what the model produced stays
next to what was extracted from it. When nothing parses, `json` is `null`,
the call is still `ok`, and `json_error` says so.

**Concurrency.** At most `link_chat_concurrency` model calls run at once
(`data/hub.json`, default 2, 1 to 16; `HOARD_HUB_LINK_CHAT_CONCURRENCY`);
the rest wait in arrival order, inside their own `timeout_s`. A call that
gives up while queued never reaches the model.

**What it resolves with.** The hub's own Link, configured from
`<hub data>/backend.json` (the same schema as an app's, see the README) plus
the `HOARD_*` variables, with the hub's Faustus URLs and the hub itself as
the lease arbiter. As for any app, only an already-resident model is used
unless that file says `only_resident: false` or `allow_load` for the
capability. There is one Link per calling app, so a GPU lease taken to load a
model is owned by the app that asked for it.

**Events.** Every call (also the refused ones that got past the token) emits
`hub.link.chat {app, capability, ok, ms, model, error?}`. Contents are never
logged or stored.

```
GET /api/link/status      -> {"ok": true,
                              "llm": {"available": true, "model": "...", "provider": "...", "reason": "..."},
                              "vision": {...}, "embed": {...}, "tts": {...},
                              "chat": {"concurrency": 2, "active": 0, "queued": 0}, "checked_at": ...}
```

`reason` says why a capability is unavailable (or how the available one was
chosen). The answer is cached for a few seconds; `?force=1` probes again.
Same guard as the other read routes: no token needed, loopback only.

**Clients.**

```js
// Node: server/hoard-link.js (vendored). Never throws.
import * as family from "./hoard-link.js";
const r = await family.chat({ messages: [{ role: "user", content: "..." }], json: { type: "object" }, effort: "low" });
if (r.ok) use(r.json ?? r.text); else show(r.error, r.detail);   // no_model | timeout | hub_down | http_<code>
const s = await family.linkStatus();                             // { llm: {available, model, ...}, ... }
```

```python
# Python without Link (standard library only): hoard_link.family. Never raises.
from hoard_link import family
r = family.chat([{"role": "user", "content": "..."}], json=True, effort="low")
s = family.link_status()
```

Both return `{ok, text, json, model, provider, ms, error, detail}`; on
failure `error` is `no_model`, `timeout`, `hub_down` (the hub is not
running) or `http_<code>`, and `code` repeats the hub's own word (`gpu_busy`,
`bad_request`, `backend_error`). They use the app's own token like `call()`.
Python apps that vendor `hoard_link.Link` should keep calling it directly.

For an assistant, the same call is the hub tool `hub_link_chat` (not
read-only: it can make the server load a model; never destructive) and the
state is `hub_link_status` (read-only).

Every LLM-based feature of an app keeps the evidence (the source text) next
to what the model produced, and has either a deterministic fallback or an
explicit "no model" state.


## 11. Ports in use

Every app keeps one fixed loopback port (`PORT_STRICT=1` in its launch hint), so
the hub, Cassandra and the assistant always find it. Before giving a new app a
port, check this list and the manifests under the roots the hub scans
(`hub_apps`): two apps on one port is the first thing that breaks.

| Port | App | Port | App |
|---|---|---|---|
| 5000 | Plato's | 5192 | Midas's |
| 5178 | JobHunter's | 5193 | Atlas's (archived) |
| 5180 | Ledger's | 5194 | Cicero's |
| 5181 | Links | 5195 | Mercator's |
| 5182 | People's | 5196 | HomeHoard |
| 5183 | Argus's | 5197 | Tantalus's |
| 5184 | Borges's | 5198 | Lumiere's |
| 5186 | Vulcan's | 5199 | Phileas's |
| 5187 | Hypatia's | 5200 | Kafka's |
| 5188 | Echo's | 5201 | Galton's |
| 5189 | Nightingale's | 5202 | Pygmalion's |
| 5190 | Cassandra's | 5210 | CookHoard |
| 5191 | Vitruvius's | 8741 | Dorian's |
| 8766 | Writer's (desktop) | 8767 | Gepetto's |
| 8810 | Hoard Hub | 8811–8817 | Babel, Laplace, Funes, Daguerre, Prospero, Scheherazade, DiskHoard |
