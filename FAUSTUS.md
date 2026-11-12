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

`GET /api/agora/live` and read-only MCP tool `hub_agora_live` expose the same
server-scoped projection. Observer sessions retain sanitized `last_action` and
`last_text_at` metadata. The view does not read repository files or create new
activity records. It requires a current `apps/agora` build. See
[the live activity guide](docs/AGORA_LIVE.md).

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
