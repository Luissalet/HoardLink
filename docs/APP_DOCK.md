# App Dock: a Hoard open beside the chat

A Hoard's full interface opens in a panel next to a Faustus conversation. The person works in it by hand; the agent sees what is on screen and acts on it with a visible, named cursor. Both work on the same live page.

Three parts:

| Part | Where | What it does |
|---|---|---|
| **Bridge** | `hoard_link/app_bridge.py` (`BRIDGE_JS`) and `js/hoard-commons/app-bridge.js`, appended to `/api/copilot/launcher.js`, so every Hoard page already loads it | Inside the Hoard page: answers the host, reports what is on screen, runs actions, draws the agents' cursors |
| **Host** | Faustus Studio, side panel tab **App** | Frames the Hoard, relays messages between the page and Faustus's server |
| **Server** | Faustus `src/app_dock.py`, `routes/app_dock_routes.py`, agent tools `app_open`, `app_view`, `app_act` | Keeps, per conversation, which app is docked, its tools and its screen context; hands actions to the page and waits for the result |

The bridge does nothing unless the page is framed **and** the parent sends `dock/hello`. A Hoard opened in its own window or in the Hub behaves exactly as before.

## Messages (window.postMessage)

Every message is an object with `hoardDock: 1` and a `type`. The bridge accepts host messages only from `window.parent` and, after `dock/hello`, only from that message's origin. The host accepts page messages only from the frame it created and its origin.

### Host → page

| type | fields | meaning |
|---|---|---|
| `dock/hello` | `host` ("faustus"), `theme` ("light"/"dark"), `locale`, `session` | Start. The page answers `dock/ready`. Sent again on every frame load. |
| `dock/call` | `id`, `tool`, `args`, `actor` (`{id, name, color}`) | Run a tool. The page answers `dock/result` with the same `id`. |
| `dock/changed` | `server`, `tool` | The agent just changed this app's data through its MCP tools. The page refreshes what it shows (`HoardApp.onChanged`, default: a `hoard:changed` DOM event; apps that do not listen are reloaded softly only if they opted in with `HoardApp.reloadOnChange = true`). |
| `dock/theme` | `theme` | Theme changed. |
| `dock/cursor` | `actor`, `state` ("idle"/"thinking"/"gone") | Presence of an agent without an action (its cursor rests, pulses or leaves). |

### Page → host

| type | fields | meaning |
|---|---|---|
| `dock/ready` | `app` (`{id, name}`), `tools` (`[{name, description, inputSchema}]`), `context` | Handshake answer. Sent again when the tool list changes. |
| `dock/context` | `context` | What is on screen now. Debounced (400 ms); the last one replaces the previous one. |
| `dock/result` | `id`, `ok`, `result` or `error` | Answer to `dock/call`. |
| `dock/message` | `text`, `send` (bool) | Put text in the chat composer (and send it when `send`). For "ask about this" buttons inside the app. |

### Context

```json
{"title": "Vídeos · Links Hoard", "url": "/#/videos/3df9", "view": "videos",
 "selection": "texto seleccionado por la persona (≤ 500)",
 "focus": {"kind": "video", "id": "3df9", "label": "Video by _fojciko"},
 "summary": "línea libre que la app quiera dar (≤ 400)"}
```

`title` and `url` are always filled by the bridge; `selection` when text is selected. `view`, `focus` and `summary` come from the app (`HoardApp.setContext`) and are optional.

## Tools every page has (generic, no work in the app)

All take `actor` from the call. Targets are found by `ref` (from `app_read`), else by visible text, label, placeholder, title or aria-label (exact match first, then contains; case and accents ignored). An action on a target moves that actor's cursor to it first.

| tool | args | result |
|---|---|---|
| `app_read` | `scope` ("screen" default, or "page"), `max_chars` (default 6000) | Compact outline of what is visible: headings, text, and every control as `[ref] kind "label" (state)`. Refs are stable until the next navigation. |
| `app_click` | `ref` or `text` | `{clicked: label, url_after}` |
| `app_fill` | `ref` or `label`, `value`, `submit` (bool) | Types the value (fires input/change), Enter when `submit`. |
| `app_select` | `ref` or `label`, `option` | For `<select>`, radios, checkboxes, tabs. |
| `app_navigate` | `to` (hash or same-origin path) | `{url_after}` |
| `app_scroll` | `ref`/`text`, or `by` ("down"/"up"/"top"/"bottom") | |
| `app_highlight` | `ref` or `text`, `note` | Points at something for the person: cursor + ring + a small note bubble. Changes nothing. |
| `app_wait` | `text` (appears) or `ms` (≤ 5000) | |

Errors are `{ok:false, error:{code, message}}` with codes `not_found` (lists the nearest labels), `ambiguous` (lists the candidates with refs), `disabled`, `timeout`, `bad_args`.

## Tools an app adds

```js
window.HoardApp.registerTool("open_video", {
  description: "Open one saved video by id or title",
  inputSchema: {type: "object", properties: {id: {type: "string"}, title: {type: "string"}}}
}, async (args, {actor, cursor}) => { await cursor.to(rowEl); openVideo(id); return {opened: id}; });
window.HoardApp.setContext(() => ({view: "videos", focus: {kind: "video", id, label}}));
window.HoardApp.onChanged(({tool}) => reloadList());
```

`window.HoardApp` exists on every page (a queue before the bridge loads is honoured: `window.HoardApp = window.HoardApp || {q: []}` then `HoardApp.q.push(["registerTool", ...])`). Limits: 64 tools, names `[a-z][a-z0-9_]{1,40}`, 45 s per call.

## Cursors

One cursor per `actor.id`, drawn by the bridge in a fixed, pointer-events-none layer above the page: an arrow in the actor's colour with its name on a small label. On an action it travels to the target along an eased curve (250–600 ms by distance), presses (ring ripple), and for `app_fill` the text appears as typing. Several actors can act at once; each keeps its own cursor. A cursor idle for 20 s fades; `dock/cursor` with `gone` removes it. With `prefers-reduced-motion` the cursor jumps instead of travelling. The person's own pointer is never moved or blocked.

## Server (Faustus)

State per chat session: `{app_id, name, url, tools, context, ready, updated}`.

| Route | What |
|---|---|
| `GET /api/app-dock/apps` | The family's apps from the Hub (`id, name, url, state, icon`). |
| `POST /api/app-dock/open` `{session, app}` | Dock an app (starts it through the Hub when it is down). Emits `open` to the host. |
| `POST /api/app-dock/close` `{session}` | |
| `GET /api/app-dock/state?session=` | Current docked app, tools and context. |
| `POST /api/app-dock/state` `{session, ready?, tools?, context?}` | From the host: what the page reported. |
| `GET /api/app-dock/events?session=` | Server-sent events for the host: `open`, `close`, `call`, `changed`, `cursor`. |
| `POST /api/app-dock/result` `{session, id, ok, result, error}` | From the host: answer to a `call`. |

Agent tools:

- `app_open(app)`: dock a Hoard beside the chat (by id or name).
- `app_view(scope?)`: which app is docked, what the person has on screen (context) and the outline (`app_read`), plus the app's own tools.
- `app_act(action, target?, value?, args?)`: one generic action (`click`, `fill`, `select`, `navigate`, `scroll`, `highlight`, `wait`) or one of the app's own tools (`action` = its name, `args`).

While an app is docked, the turn's context carries one short block: the app, its `context`, and the names of its own tools. After any MCP tool call on the docked app's connector, the server emits `changed`.

When no host is listening for the session (the panel is closed, a background turn), `app_view`/`app_act` answer `no_host` at once with what to do (`app_open` first), never a 45 s wait.
