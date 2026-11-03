# The family contract (Hoard Link 0.7)

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
(what backups copy; default the token file's folder). In the launch hint a
`{X_DIR}` placeholder with a value in `defaults` (typically `DATA_DIR`) takes
that value, any other `{X_DIR}` is the app's folder, and `%VAR%` is expanded
in `cwd`, `argv` and `env`, so the hub starts the app on the same data folder
that Faustus's MCP bridge uses.

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
| `hub.lane.acquired` / `released` / `expired` | hub | `lease_id, holder, purpose, domain, profile, reason (expired: ttl / owner process gone), waited_s, held_s` |
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
The remaining budget is passed through Link to the backend HTTP call, including
any reasoning/format negotiation. An explicit long timeout is not shortened by
Link's default backend read timeout. Native Python callers can also use
`await link.chat(messages, timeout=900)`; omitting `timeout` retains the existing
timeout selected for the reasoning effort.

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

### Incremental generation

The blocking calls above keep their existing behavior. When the provider supplies
it, their result also includes `finish_reason` and `completed`: `length` means
`completed: false`, even though the HTTP exchange succeeded. A missing reason
from an older provider remains unknown. Do not execute or publish a truncated
answer as a completed result.

For incremental delivery, send the same authenticated `POST /api/link/chat`
with `"stream": true`. Optional `idle_timeout_s` defaults to 120 seconds;
`timeout_s` still defaults to 300 and covers queue, resolution and generation,
up to the existing 1800-second maximum. Set the total explicitly for long
reasoning tasks. Reasoning and tool-argument chunks count as upstream activity;
the lack of visible answer text is not an inactivity timeout. The first real
provider data may wait for prefill within the total budget; the inactivity clock
starts only then. Role-only and empty frames do not start or renew that clock.
Hub heartbeats
neither claim upstream activity nor extend the total deadline.
Empty JSON deltas, empty usage objects and provider keepalive comments do not
renew inactivity. Nonempty usage and finish fields remain valid provider events.
A request whose total deadline has expired cannot start generation after leaving
the queue or after a paused consumer resumes reading its starting status.

After validation the response is `text/event-stream`, with one JSON object per
SSE event. Every event has `type`, `request_id`, monotonic `seq`, and
`elapsed_ms`; the SSE `id` is `<request_id>:<seq>` (identity, not a replay API).

| `type` | Contents |
|---|---|
| `status` | `state`: `queued`, `starting`, `resolving`, or `waiting_backend`; queue counts/timing when known. |
| `heartbeat` | Current waiting/generating state; `upstream_activity: false` when no provider event has arrived. This is not token progress. |
| `text` | `delta`: actual content supplied by the provider. |
| `reasoning` | `delta`: actual out-of-band reasoning supplied by the provider. |
| `tool_call` | `delta`: provider tool-call index/id/name/argument fragment; concatenate arguments by index, do not parse individual fragments. |
| `usage` | `usage`: provider token counts, including usage-only chunks after the last content delta. |
| `finish` | Aggregate `text`, `reasoning`, `tool_calls`, `usage`, `model`, `provider`, `finish_reason`, `completed`, timing, and optional parsed `json`. |
| `error` | `ok: false`, typed `error`, `detail`, and partial text/reasoning/tool deltas when available. `timeout_kind` distinguishes `total` and upstream `inactivity`. |

`finish.ok` describes successful transport, not satisfaction of the requested
task: check `completed`. A `length` finish is incomplete. EOF without a provider
finish reason becomes an error, never a synthetic successful finish. Errors
after SSE headers have HTTP status 200 and arrive as `error` events; auth/body
validation errors still use their ordinary HTTP statuses before SSE starts.

Streaming sends one upstream request. It does not retry unsupported reasoning,
format or tool options, or retry after any partial response. JSON mode adds the
same instruction as blocking mode and parses the final text; it does not silently
restart a stream when the server refuses `response_format`. `tools` and
`tool_choice` are optional streaming request fields forwarded to the provider.
SSE events are delivered only to the caller, not stored on the family event bus.

```python
from contextlib import closing
from hoard_link import family

# Set an optional threading.Event through cancel=... to interrupt a pending read.
with closing(family.stream_chat(messages, timeout=900, idle_timeout=120)) as events:
    for event in events:
        if event["type"] == "text":
            append_text(event["delta"])
        elif event["type"] == "reasoning":
            append_reasoning(event["delta"])
        elif event["type"] == "finish":
            show_result(event, incomplete=not event["completed"])
        elif event["type"] == "error":
            show_error(event)
```

```js
const ctl = new AbortController();
// ctl.abort() cancels while reading/queued; break also closes the stream.
for await (const event of family.streamChat({ messages, timeoutMs: 900000,
    idleTimeoutMs: 120000, signal: ctl.signal })) {
  if (event.type === "text") appendText(event.delta);
  if (event.type === "reasoning") appendReasoning(event.delta);
  if (event.type === "finish") showResult(event, { incomplete: !event.completed });
  if (event.type === "error") showError(event);
}
```

Native async Python apps can use `Link.stream_chat(..., timeout=900,
idle_timeout=120)` with `contextlib.aclosing` and `async for`. It supports the
same capability/images/effort/format options as `Link.chat`, plus optional
`tools`/`tool_choice`. OpenAI-compatible SSE and Ollama NDJSON both stream
incrementally. The normalized reasoning event uses the provider's
`reasoning_content`, `reasoning`, or `thinking` field; inline `<think>` content
remains provider text. Native Link raises backend/timeout exceptions after any
already-yielded events; family clients yield terminal error objects instead.
Pausing a native async iterator does not cancel the consumer's unrelated work;
its next read checks the elapsed total deadline. The Hub enforces its deadline
independently and releases the model slot once upstream cleanup finishes, even
when terminal delivery is waiting for a slow client.

### Model-specific reasoning controls

The client verifies GLM-5.3-Flash checkpoint identity against the selected
server's `/v1/models` and checks that the actual chat request in `/openapi.json`
advertises an integer `thinking_token_budget` and object `chat_template_kwargs`,
including nullable schema variants. Names alone do not verify support. It caches this
read-only check for 30 seconds with shared in-flight probes. A registry label
such as `provider: unknown` neither grants nor prevents native support; no host
or deployed model is hard-coded. An unavailable or incompatible schema leaves
the existing generic/Ollama path unchanged.

For this verified model, generic efforts map as follows:

| Requested | Native effort | Existing profile budget |
|---|---|---|
| omitted / auto without configured effort | server default | omitted |
| off | low | omitted; never a forced zero |
| low | low | 1024 |
| medium | low | 4096 |
| high | high | 8192 |
| max | max | 16384 |

These budgets preserve the SDK's existing explicit effort profiles; they are
not a new default cap. Its existing output widening by profile budget also
remains. Native requests send `thinking_token_budget`, without llama-server's
`thinking_budget_tokens`/`reasoning_budget` aliases. GLM-5.3-Flash always reasons:
off selects its faster Low mode and does not claim to disable thought.
The official model contract has Low/High/Max and recommends `clear_thinking=true`
for chat. [Model card](https://huggingface.co/zai-org/GLM-5.3-Flash),
[vLLM recipe](https://recipes.vllm.ai/zai-org/GLM-5.3-Flash),
[native budget semantics](https://docs.vllm.ai/en/latest/features/reasoning_outputs/#thinking-budget-control).

`chat_template_kwargs` is optional on `Link.chat`, `Link.stream_chat`, both Python
family helpers, `POST /api/link/chat` and MCP `hub_link_chat`; Node helpers use
`chatTemplateKwargs`. For example, `{"clear_thinking": false}` stays false.
Verified GLM defaults that option to true, enables thinking, and puts the chosen
effort only at top level. An explicit template `enable_thinking:false` also maps
to Low rather than forcing an empty thinking block. Other template options are
preserved. No new argument is needed by existing callers.

Verified responses add `reasoning_controls` with advertised levels, source,
`requested_effort`, `effective_effort`, actual budget/clear option, and
`thinking_off_behavior: low_effort` / `thinking_can_disable: false`. The same
capability is visible in native status details and Hub `/api/link/status`.
Unverified models do not gain this metadata. Capability means advertised HTTP
support, not demonstrated semantic quality or correctness of a forced budget.

Blocking calls retain the existing reasoning-error fallback. A refusal strips
native and legacy budgets plus template thinking/effort/clear options before
retrying at server defaults, preserving unrelated template kwargs; metadata
then says `applied:false`, `effective_effort:server_default`. Streaming keeps its
one-request contract and never replays a rejected or partial generation.

Close the iterator when abandoning a request. Python's `closing`/`aclosing`,
JavaScript `return`/`break`, cancellation, or an HTTP disconnect closes the
upstream response, releases its GPU lease, and removes the FIFO ticket or
releases the active Hub slot. SDK total timers include a 15-second transport
grace so the Hub's own deadline error can arrive; that grace does not extend
the generation budget.

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
| 5204 | Heron's | 5205 | Prometheus's |
| 5206 | Gulliver's | 5207 | GamerHoard |
| 5218 | Gutenberg's | 5219 | Coase's |
| 5222 | Dürer's | 5230 | Silver's |
| 5232 | Baird's | 5233 | Darwin's |
| 5241 | Dantzig's | 5243 | Maxwell's |
| 5245 | Shannon's | 5246 | Drucker's |
| 8766 | Writer's (desktop) | 8767 | Gepetto's |
| 8810 | Hoard Hub | 8811–8817 | Babel, Laplace, Funes, Daguerre, Prospero, Scheherazade, DiskHoard |

## 12. Spheres

The person's lives — `personal`, `work`, and any other (a freelance one) — live in the hub's
`data/spheres.json`. An app never decides a sphere: the hub does, from the mail account or chat source a
message came from, and from each sphere's `apps` list (which apps may see that sphere's mail and receive its
notifications). Read the active one with `GET /api/spheres`; switch it with `POST /api/spheres/active {id}`
(any family token, so Faustus can switch it too; the hub emits `hub.sphere.changed {from, to}`).
Details: [facets/spheres.md](facets/spheres.md).

## 13. Notifications and long jobs

**Tell the person through the hub.** `POST <hub>/api/notify {title, body, priority: low|normal|high|urgent,
url, group, dedupe_key, sphere?}` with the app's token (Python `hoard_link.fam_notify.notify(...)`, Node
`notify(...)`). The hub routes it by the sphere's rules, holds it in quiet hours, drops duplicates of the
same `dedupe_key` for six hours, and rate-limits a chatty app. An app keeps its own channels only as the
fallback for `{ok: false, error: "hub unreachable"}`, behind a setting `notify.via = auto | hub | own`.
Details: [facets/notify.md](facets/notify.md).

**Long work** is announced with canonical events, so the hub's Work tab, Today and Cassandra follow it:

| type | data |
|---|---|
| `<app>.job.queued` / `started` / `progress` / `done` / `failed` / `cancelled` | `job_id, title, kind, progress (0..1), gpu, eta_s, url, error, ref` |

Older names are renamed on arrival (the original stays in `data._orig_type`, and rules written for the old
name still fire): `pygmalion.job_queued|job_done`, `hypatia.teacher_job.done|no_model`, `galton.run.done`,
`lumiere.render.done|failed`, `links.media.done|failed`, `midas.backtest.finished`,
`vitruvius.render.done`, `vitruvius.assay.done`. Details: [facets/work.md](facets/work.md).

## 14. The mail gateway

The hub reads the Faustus mail accounts (through Faustus's own Python, so passwords stay in Faustus) once for
everybody, when the person turns the gateway on. An app registers what it is interested in —
`POST /api/mail/interests {spec: {subject_terms, from_domains, from_addresses, text_terms, regex,
has_attachment}}` — reads its share with `GET /api/mail/messages?since_id=&full=1` (only spheres its
`apps` membership allows; the records carry the same keys as the Faustus mail helper's output) and claims what
it turned into a record: `POST /api/mail/claim {ids, kind, ref: "hoard://<app>/<kind>/<id>"}`. A mail nobody
claims shows in the hub's "nobody's" tray. Apps keep their own mail helper as the fallback, behind
`mail.source = auto | hub | faustus`. Python: `hoard_link.fam_mail`; Node: `mail*` in `hoard-link.js`.
Details: [facets/mail.md](facets/mail.md), chat sources in [facets/chats.md](facets/chats.md).

## 15. The agenda

Every app that knows dated things answers

```
GET /api/family/agenda?from=YYYY-MM-DD&to=YYYY-MM-DD&sphere=<id>     Authorization: Bearer <its token>
→ {"ok": true, "items": [{"id", "title", "start", "end"?, "all_day", "kind", "priority", "url", "detail", "sphere"}]}
```

`kind` is one of `deadline, delivery, birthday, followup, maintenance, release, review, cards, incident,
publish, renewal, exam, other`. Declare it in the manifest with `"x-family": {"agenda": true}`. Python:
`hoard_link.fam_agenda.install_fastapi(app, provider)`; Node: `installAgenda(app, provider)`. The hub joins
them into Today, the morning digest and `/calendar.ics`. Details: [facets/today.md](facets/today.md).

## 16. References, search and purchases

`POST /api/refs {from, to, rel, from_label, to_label}` records a link between two records of different apps
(the caller must own one end); `GET /api/refs?uri=` returns both directions (`hoard_link.fam_refs`, Node
`refs*`). The hub's search fans one query out to every running app's read-only `*_search` tools
(`GET /api/search?q=`). The purchases facet follows one purchase through `ledger.mail.recorded`,
`phileas.shipment.new|delivered`, `kafka.document.archived`, `kafka.warranty.created`,
`homehoard.item.created` and `tantalus.watcher.bought`, matched by order number, mail, or merchant + amount +
date. Details: [facets/refs.md](facets/refs.md), [facets/search.md](facets/search.md),
[facets/purchases.md](facets/purchases.md).

## 17. Cross-app tools the rules call

The recommended rules (section 5) call these tools by name; an app that is not installed is skipped, not an
error:

| app | tools |
|---|---|
| Kafka | `deadline_add`, `deadlines_from_minutes`, `tax_pack`, `document_link_tx` |
| Ledger | `tx_find`, `tx_attach_doc`, `forecast_month`, `split_add`, `splits_balance`, `split_settle`, `income_from_sales`, `budget_status`, `report_year` |
| Tantalus | `watchers_match_purchase`, `watcher_mark_bought`, `watcher_add` |
| HomeHoard | `item_add_from_purchase` |
| People | `people_from_minutes`, `gift_idea_add`, `gift_ideas`, `gift_watch`, `contacts_sync_mail` |
| Funes | `minutes_get` |
| Links | `highlights_to_cards`, `resurface` |
| Vulcan | `model_import_file`, `listings_export_catalog` |
| Mercator | `post_draft_from_media`, `sales_batch_get`, `catalog_from_vulcan`, `posts_*` |
| Lumiere | `project_from_timeline` |
| Prospero | `production_export_lumiere`, `cast_import_character`, `production_from_storyboard`, `voice_tts` |
| Scheherazade | `world_export`, `world_import`, `scene_narrate` |
| Cicero | `theme_from_tokens` |
| Galton | `galton_run` |
| Writer's | `wh_character_to_prospero`, `wh_storyboard_to_prospero`, `wh_world_to_scheherazade`, `wh_world_from_scheherazade` |

## Copilot shared with Faustus

`POST /api/link/copilot` transports `{path, method, body?, raw_body?, content_type?, headers?}`
to the configured Faustus service. JSON, native status/error codes, run IDs and SSE
pass through; the Hub stores no chat and runs no model or tools. Python hosts use
`CopilotClient`; Node hosts use `createCopilotFetch` from `js/hoard-copilot.js`.
FastAPI hosts mount `make_copilot_router(app_id, data_dir, ...)` from
`hoard_link.copilot` for a same-origin panel/info/proxy. It creates one stable data
space ID, discovers the service and transports responses. The shared panel,
conversation, prompts, tools execution and memory remain in Faustus.
`hub_copilot_request` exposes the same operations to MCP; SSE admission returns
the detached run identity and status is read with the normal native route.

### Every Hoard gets the copilot from `install_spa` / `installSpa`

An app that called `family.configure(...)` and serves its UI with `install_spa` (Python) or `installSpa` (Node) has the
Faustus copilot with no code of its own:

* `/api/copilot/info`, `/api/copilot/panel.js`, `/api/copilot/proxy` (the relay above; the Faustus token stays on the
  server and the proxy forces `origin.app_id` / `data_space_id`), and `/api/copilot/launcher.js`;
* the launcher tag is added to the app's HTML: a round 48 px button with the **Faustus mark** opens a side panel with `<faustus-chat>`.
  Each turn carries the context Faustus needs: origin, the view (route, title, main heading, locale), the objects the
  route names (`/notes/42` → `{kind: "note", object_id: "42"}`) and the selected text; an app can add real ids with
  `window.hoardCopilot = { context() { return {...}; }, onTurnEnd(detail) {...} }` and listen to the
  `hoard:copilot-turn-end` event to refresh after an agent changed its data. `context()` returning
  `active_objects: []` drops the guesses from the URL (a route like `/trabajos/j_123abcde` would otherwise be guessed
  as an object). The button sits bottom right; an app whose own controls live there moves it with
  `--hoard-copilot-bottom` / `--hoard-copilot-right` (or `<html data-hoard-copilot-position="left">` plus
  `--hoard-copilot-left`). Every HTML page the app serves gets the launcher, including a second SPA under a sub-path. One conversation per data space, kept
  across reloads, and one per project when the app's context names one (`project.external_project_id` / `id`):
  moving to another project shows that project's conversation or a fresh one, never two projects in one thread. The
  launcher checks on every navigation, when the panel opens, when the page dispatches `hoard:copilot-context-changed`
  (a project picked without changing the URL) and right before each turn. A view without a project omits `project`
  (never `null`). A conversation the current Faustus no longer has is dropped and a new one starts. Keys typed in the
  panel never reach the app's shortcuts. A turn queued behind another agent on the shared model lane keeps its place
  for up to 10 minutes (`initial_response_timeout_s`) instead of failing at the endpoint's short default.
  The panel opens under the app's own top bar (a bar pinned to the top of the window, or a `header`/`nav`/`role=banner`
  starting in the first 64 px) so the app's header buttons stay clickable; `--hoard-copilot-top` on `:root` overrides
  it. Escape closes the panel (inside an app dialog it is left to the dialog).
* Strict apps need no exceptions of their own: the launcher's styles and the panel's are constructed stylesheets
  (a `style-src 'self'` policy allows them), the launcher sends `X-Hoard-Request: 1` and the page cookies with
  `/api/copilot/info` and `/proxy` (`COPILOT_REQUEST_HEADER`), and an app with a session guard lets
  `COPILOT_PUBLIC_PATHS` (the launcher script, which the page loads before its cookie exists) through. Both
  constants exist in `hoard_link.copilot` and `hoard-commons/copilot.js`.
* Faustus is found through `<APP>_FAUSTUS_URL`, `HOARD_FAUSTUS_URL` (+ `HOARD_FAUSTUS_TOKEN`) or the link config,
  falling back to `127.0.0.1:7000` / `7001`.

Off switches: `install_spa(..., copilot=False)` / `installSpa(app, dist, { express, copilot: false })` or
`HOARD_COPILOT=0`. Apps that mount their own relay first (`make_copilot_router`, their own `/api/copilot/info`) are
detected and left alone. Apps without `install_spa` call `install_copilot(app, app_id, data_dir, app_name=...)`
(`hoard_link.copilot`) or `installCopilot(app, { appId, appName, dataDir })` (`hoard-commons/copilot.js`) themselves.
The browser launcher is written once in `hoard_link/copilot_launcher.py`; `scripts/sync_vendored.py` mirrors it to
`js/hoard-commons/copilot-launcher.js` (a test fails when the two differ).
`FAUSTUS_MARK_SVG` and `FAUSTUS_BRAND` (`#e06c75`) in that module supply the same
mark and colour to apps with their own button. The icon button has an accessible
Faustus label, with no visible text. FastAPI inserts the launcher before response
compression regardless of middleware registration order. Multipart transport
matches complete MIME delimiter lines and preserves file bytes, even when an
upload contains boundary substrings or a filename named `context`. The Node relay
adds no arbitrary attachment-size cap; the normal upstream transport remains in use.

The same launcher remains available inside native `dialog.showModal()` editors,
including nested dialogs and containers with clipping or transforms. It moves the
existing panel into the active dialog using a manual popover; it keeps the same
conversation, draft and active turn. Closing an editor returns that panel to the
page. Escape typed inside the chat does not close the app's editor. Hosts can open
it through `window.dispatchEvent(new Event('hoard:copilot-open'))` without creating
another launcher. Faustus's component supports both state-preserving `moveBefore`
and synchronous append-based moves; only genuine removal detaches its local stream
observer, and remote work continues.
