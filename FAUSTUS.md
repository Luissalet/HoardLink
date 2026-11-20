# Hoard Hub and Hoard Link

## Live Agora activity

The Agora console and Studio's server navigation provide **En directo**, sharing
one view of task columns, repository path locks, observed commands and edit
excerpts, and a selectable channel. Each active member has a cursor using the
App Dock arrow, coloured name stripe and note appearance. Cursor destinations
carry existing source timestamps. Heartbeats refresh availability, cursors dim
after sixty seconds without a signal, and observed ended turns leave the view.
Session details, task threads, following, reduced motion and connection recovery
are available from the view.

Each observed member also has an agent loop ring (plan, act, observe, lit by its
transcript lines), the steps of its turn with each tool's readable name and a short
summary of its arguments, and the practices of the kind of work the turn did (code,
research, files, operations; none for a plain conversation); task cards show the
owner's phase, tool, kind of work and practice count.
`hub_agora_task_submit` leaves that checklist in the task thread for the review and
`hub_agora_practices` reads or posts it.

`GET /api/agora/live` and read-only MCP tool `hub_agora_live` expose the same
server-scoped projection. Observer sessions retain sanitized `last_action` and
`last_text_at` metadata. The view does not read repository files or create new
activity records. It requires a current `apps/agora` build. See
[the live activity guide](docs/AGORA_LIVE.md).

## Agora router

A message the person writes in an Agora channel or thread without `@`-naming anyone is routed to the agents that
should answer it: the hub scores the agents of the server by code (owner of the thread's task, related tasks and
locks, role, last to speak), a small model of the family chooses among them through Hoard Link (rules decide when it
cannot), and the chosen receive the message as a mention. The message carries `route` (`label`, `picks`, `engine`);
`route_pass` hands it to the next candidate. Per-server settings: `route_settings` / `route_settings_set` (`enabled`,
`max_agents`, `model`). MCP: `hub_agora_route`, `hub_agora_route_pass`, `hub_agora_routes`,
`hub_agora_route_settings`, `hub_agora_route_settings_set`. See [the Agora reference](docs/AGORA.md).
## Threads, pins and Brain links in the Agora

Studio's Agora starts a thread from any message or from a stretch of messages (the title is suggested by the family's
model and can be changed), shows how many answers each thread has under its origin message, and lists the threads a
reader follows in **Threads**, with what is new. Threads can be renamed, followed, closed and archived; messages can be
pinned, and the pins of a conversation are a button in its header. `[[Page]]` in a message opens the Brain page and shows
a preview when hovered, and a Brain page lists the messages and tasks of the Agora that cite it. The same operations are
the hub's `hub_agora_thread_from|thread_title|thread_set|thread_follow|thread_list|message_pin|pins|brain_links|
brain_backlinks` tools, also reachable from Faustus's own `agora_read` and `agora_write`.

## Agora work and routines

A message of an agent in the Agora can carry `work` (the tools it used, with result, duration and a short masked
summary; declared by the author, or told by the hub from the author's observed session since its last message) and
`routine` (`id`, `name`, `schedule`) when a routine or scheduled task wrote it. Both come with the message in
`channel_read`, `thread` and `feed`, and `work` reads one in full. `guild` puts `live` (`steps`, `done`, `since`) on the
card of a task whose owner is in the middle of a turn. MCP: `hub_agora_work`; `work` and `routine` on
`hub_agora_post` and `hub_agora_channel_post`. See [the Agora reference](docs/AGORA.md).

## Delivery review in the Agora

A task whose submission lists commits can be read as a real diff. The task page
in the Hub's Agora tab shows **Revisión de la entrega** with **Revisar el diff**
and **Revisión guiada**. The diff reader lists each commit's files, hunks and lines
with their old and new numbers; any line takes a comment (new or old side). The
comments are queued, sent together with an optional general note, and arrive as one
message in the task's thread that mentions the agent that delivered, quoting the
code line each one refers to. If that agent runs as a console session, the delivery
reports it. Comments are stored per submission revision and show as outdated after
a resubmission.

**Revisión guiada** asks the family's language model (the Sparks, through Hoard
Link) to order the diff into chapters, each with a summary, what to check and its
files, shown beside the real diff. A file the model leaves out falls into a last
chapter, so the guide always covers the whole diff. The guide is kept per
submission revision.

HTTP: `GET /api/agora/review_diff`, `GET /api/agora/review_comments`,
`POST /api/agora/review_comment_send`, `POST /api/agora/review_guide`. MCP:
`hub_agora_review_diff`, `hub_agora_review_comments`,
`hub_agora_review_comment_send`, `hub_agora_review_guide`. Terminal:
`scripts/agora.py delivery diff|comments|comment|guide <task>`. Commenting does not
vote; approval and requested changes stay with `hub_agora_task_review`.

## App Dock page bridge

The shared copilot launcher includes the App Dock bridge on FastAPI, standard-library
HTTP and Node hosts. It activates when a framed page receives `dock/hello` from its
parent, then binds subsequent messages to that parent's origin.

The bridge reports title, relative URL, selected text and optional app context.
`app_read` returns visible text and stable control refs. `app_click`, `app_fill`,
`app_select`, `app_navigate`, `app_scroll`, `app_highlight` and `app_wait` operate on
the live page. Text matching ignores case and accents. Ambiguous or missing targets
return candidate labels and refs.

Each actor has a named, coloured cursor with eased travel, press feedback, visible
typing and highlight notes. Multiple actors can act concurrently. Cursor annotations
fit the viewport, idle cursors fade after twenty seconds and reduced-motion skips
travel and typing animation.

Apps register custom tools with `window.HoardApp.registerTool`, supply live context
with `setContext` and refresh through `onChanged`. Registrations queued before the
bridge loads are replayed. `HoardApp.message(text, send)` forwards an app question
to the host. The bridge supports up to 64 tools and a 45-second call deadline.
Custom handlers receive an abort signal for cooperative cancellation.

The Faustus host and server consume the bridge protocol. This repository supplies
the page component. App-specific tool handlers are registered by each application.
Only pages that set `HoardApp.reloadOnChange = true` opt into the default reload.

Run `python -m pytest tests/test_app_bridge.py` for browser-based protocol, schema,
target matching, navigation, form events, concurrent cursor and mobile-bound checks.
`tests/commons/app_bridge_live.py` exercises a live Links Hoard through a host iframe
and writes its results and screenshots to the requested evidence directory.

Distribute the shared files with `scripts/sync_vendored.py --files app_bridge.py
copilot.py copilot_stdlib.py app-bridge.js copilot.js`. Running processes adopt the
updated library on their next ordinary restart.

## Skills

The `skills` block of `faustus-plugin.json` names the procedures of the family's skill library that fit this app's work: `hoard-daily-digest`, `hoard-family-recap`, `hoard-what-happened`, `hoard-meeting-prep`, `team-agent-orchestration`. The hub's Skills tab shows and changes them. This app does not hand them to connecting agents yet.

## Apps kept alive

The Hub keeps a list of apps that live as long as it does (`data/keep-alive.json`,
by default Darwin, Links, Talos and Kafka). The serving Hub starts them when it
comes up and checks them every minute, starting again any that fell, with a
growing wait after failed starts. A stop made by hand holds until the app is
started again or the Hub restarts. Apps whose manifest says `auto_start: false`
are refused. `hub_keep_alive_status` lists each app with its state, attempts and
whether it was stopped by hand; `hub_keep_alive_set` adds (`keep: true`) or
removes an app. `GET/POST /api/keep-alive` is the same over HTTP, and each start
is published as a `hub.keepalive.started` event.

## Hub page and command palette

The Hub page groups its sidebar in three sections and puts each page's actions
next to its title. Ctrl+K (⌘K) opens a command palette that goes to any page,
tab, app or project, or searches all content; its quick-actions mode opens,
starts, stops, restarts or keeps alive an app, starts or stops everything,
starts or stops Faustus, and switches theme (dark by default, light per
browser) or language. Everything the palette runs is also an MCP tool of the Hub.

## Outbound mail service

The Hub owns mail through its existing gateway and connected Faustus accounts. hub_mail_accounts checks outgoing configuration; hub_mail_compose saves a remote draft by default or sends only for explicit mode=send. to/cc/bcc, subject/body/html and absolute gateway-local attachment paths are supported. SMTP acceptance, partial delivery and unknown outcomes are distinguished. Python fam_mail.send and Node fam-services.mailSend call the authenticated gateway without retrying or switching senders. Ledger Cobros uses this service; automatic notice sending is off by default. See docs/commons/services.md#outbound-mail.


## Family MCP through the Hub link

When Faustus runs on another machine, the PC Hub serves each installed app's
manifest-declared stdio MCP over Streamable HTTP at
`/api/mcp/apps/<id>/mcp`, authenticated with the existing Hub-link token.
Each HTTP session reuses its own hidden bridge process and closes it on session
termination or Hub shutdown. Initialization instructions, tools, skills, prompts,
resources, structured results and bidirectional JSON-RPC messages pass through.
Connecting starts the bridge. Starting the app remains an explicit Hub action.

`GET /api/mcp/apps` and `GET /api/mcp/apps/<id>?check=1` describe the available
relays and optional health at the app's current location. MCP `hub_mcp_apps` exposes the same discovery.
Faustus resolves the transport when connecting and checks health through the Hub.
Saved connector commands, environment, disabled preferences and PC browser URLs
retain their meaning. Legacy MCP rows whose command exactly matches a family
manifest can use the relay through `/api/mcp/resolve`.

The separate HTTP tool proxy supports array and envelope catalogues and preserves
tool-level not-found replies. `hub_proxy_check` reports catalogue reachability
across the installed Hoards without starting them. `hub_station_catalog` reads
manifest handoffs, links supported piece types to their technical checks, and
proposes compatible chains. `hub_piece_validate` reports measurements from the
file or registered revision. Checked looks and Orders gates display the
technical verdict alongside the piece. See [production piece checks](docs/piece-checks.md).
`hub_agora_order_create` and `hub_agora_order_update` record a [production dossier](docs/order-dossier.md)
with the audience, constraints, starting material fingerprints and any context compiled by the source station.
`hub_agora_job` delivers it with the worker's brief. Orders can select a [native source dossier](docs/order-intake.md)
in the form. Plans record the station and repository on the job, and `hub_order_metrics` groups returns and median
task durations by station and repository.
The [integration station](docs/INTEGRATION.md) compares focused tests against the committed main branch, preserves
overlapping Git changes, verifies the merged case and closes the approved delivery in the Agora. Its receipts appear
under System → Integrations. The station leaves the person's production gates to their existing interface.
`hub_modules`
and `hub_module_set` use declared native module switches and return an undo
request for a change. See [the Hub reference](docs/HUB.md#production-stations). `hub_eyes_look` turns any
piece (image, video, model, web page, document, Godot scene or an app's object) into views to open, with an
optional vision check of details to keep, and repeats the same camera on a new revision ([docs/eyes.md](docs/eyes.md)).

## Faustus workers from Agora

The console discovers Faustus through the same placement state published by
`GET /api/placement`. A worker uses its Agora space's workspace and a separate
native project/session, with its own coordination identity and the selected
Sparks model. The existing local controller workflow remains available.

Windows repositories execute on the PC. When the main Faustus runs on a Spark,
an explicit worker launch automatically starts a hidden PC Faustus with separate
data under `<hub-data>/agora-console/faustus/<agent>`, an ephemeral loopback port
and that Spark's configured model endpoint. It connects to the real Hub URL and
Hub MCP with the existing token file. Its server and connector processes end
with the worker. Browsing the model catalog starts no worker runtime.

The console launches Faustus workers with no wall-clock deadline or approval leg
limit. A worker stops after an hour without turn events by default. Set
`HOARD_AGORA_WORKER_IDLE_SECONDS` on the Hub to change that interval. Stop
cancels the exact native run. After an interruption the console attempts one
automatic continuation when it confirms the original session is idle. The
worker receipt also offers **Continuar** for that same session and workspace
(`action: continue` over HTTP and MCP).

Managed workers are told to commit changes and submit their Agora task. The
console checks the submission against the commits named in the submit: they are
located in the task's repository and its worktrees, wherever the worker worked
and whether or not the console knows the worker's folder, and only the worktrees
holding those commits are checked for uncommitted files. Claude workers start in
a folder that is not a repository and make their own worktree in the repository
of the task, so no worktree of the Hub is created for them. A leftover
`.claude/worktrees/agora-claude-*` worktree of a finished Claude worker is
retired: its uncommitted changes are written into the task thread first, and one
whose branch holds commits found nowhere else stays. Incomplete or interrupted
work is reported in the worker receipt and task thread. The task keeps its owner
and locks so it can be resumed.

Worker details show the actual server URL, workspace and observed tool counts.
**Detener turno** reads that session's current run ID and cancels it through
`POST /api/chat/stop/<session_id>` with the native run identity and work scope.
The durable receipt reports cancellation separately from task completion.
HTTP `/api/agora/console/worker` and MCP `hub_agora_console_worker` share this
flow, including `action=stop`. Native Spark workspaces can use the placed
instance directly with its connected Agora connector.

### Apps placed on a Spark

For apps managed by placement, the relay resolves the declared bridge on the
current machine. On a Spark it uses placement's existing SSH connection, the
app's native venv and token file, with bridge autostart disabled. Health is read
on that Spark and metadata carries the tailnet URL. Reconnecting after a move
back to the PC resolves the installed local command again. A move in progress
reports that the connector can reconnect when placement finishes.

### Hoards on the PC while Faustus is on a Spark

The family commons (`hoard_link/where.py`, `js/hoard-commons/placement.js`) read `GET /api/placement` through a
short cache. Faustus addresses that apps configure (`127.0.0.1:7000` by default) are tried first and the address the
Hub published for the Spark follows, so a Hoard on the PC reaches the copilot, and can reach the global model through `faustus_origin()`, with Faustus
switched off on the PC. `LinkConfig.faustus_candidates()` lists the addresses in that order and `faustus_origin()`
returns the first one whose health check passes, falling back to the configured address. The Hub itself reads its own
placement state instead of calling its own API. `Link.resolve` finds Faustus the same way (the placed address is read
with `where.placed`, the one rule every Hoard shares). `where.app_url("hermes")` and `appUrl("prometheus")` answer the same
for the other apps that move.
