# Chat sources (facet `chats`)

Work may run on Slack or on Microsoft 365. The hub reads both, plus JSON-lines files, with the standard library only, and
stores what it reads in the same `mail.db` `messages` table as the mail (`kind = 'chat'`), so spheres, priority, search
and Today treat chats and mail alike. Module `hoard_link/hub/chats.py`, UI `hub/ui/chats.js` (tab "Chats").

## Sources: `<data>/chats.json`

A list of `{id, kind, name, sphere, enabled, interval_min, ...}`. `sphere` empty = whatever the `spheres` facet says
(`sphere_of_chat_source`), else `personal`.

| kind | fields | reads |
|---|---|---|
| `slack` | `token, channels, include_dms, me, api_base, backfill_days` | `conversations.list` / `conversations.history` (per-channel `oldest` watermark, cursor paging), `users.info` (cached), `auth.test`. `channels` empty = every conversation the token is in |
| `graph` | `access_token` or `tenant + client_id` (device-code login), `teams_chats, outlook, api_base, login_base, scope` | Teams chats (`/me/chats`, `/chats/{id}/messages`) and, with `outlook`, the inbox, stored as `kind = 'mail'` through `mailgate.ingest` (deduped by `internetMessageId`) |
| `jsonl` | `path` | one JSON message per line `{ts, channel, from, text, direct, mentions_me}`, by byte offset; a partial last line waits; a rewritten file restarts |

Secrets (`token, access_token, refresh_token, client_secret`) are returned masked (`••••` + last 4); saving the masked
value back keeps the stored secret. Per-source state (watermarks, last fetch, login, `retry_after_ts`) is in
`<data>/chats_state.json`. A Slack/Graph `429` backs the source off until `Retry-After`.

Microsoft login: `POST /api/chats/sources/<id>/login` starts the device-code flow (returns `user_code`, `verification_uri`,
`expires_in`, `interval`); a thread polls the token endpoint and the source then refreshes its own token.

## Python API (`hub.facet("chats")`)

| call | returns |
|---|---|
| `sources() -> list`, `get_source(id)`, `save_source(raw)`, `remove_source(id)` | masked copies |
| `fetch_source(id, force=False)`, `fetch_all(force=False, only_due=False)` | `{ok, new, ...}` |
| `test_source(id)`, `start_login(id)` | |
| `recent(sphere=None, source=None, limit=50, days=None)` | newest chat messages |
| `attention(sphere=None, days=7, limit=50)` | priority attention, undismissed |
| `search(q, sphere=None, days=30, limit=20)` | |

Test attributes: `background`, `tick_s`, `first_delay_s`, `login_poll_s`.

## HTTP

| route | who | |
|---|---|---|
| `GET /api/chats/sources` | ui/hub | masked, with `state` (`last_fetch_ts, last_ok, last_error, last_new, login, me, retry_after_ts, due_in_s`) |
| `GET /api/chats/messages` | any | `sphere, source, since_id, q, channel, days, limit, order`; apps only see the spheres they are allowed in |
| `POST /api/chats/sources`, `/api/chats/sources/remove` | ui/hub | |
| `POST /api/chats/fetch` | ui/hub | `{id?, force?}` |
| `POST /api/chats/sources/<id>/test`, `/login` | ui/hub | |

Event: `chat.received {mail_id, sphere, source, channel, from, text (<=120), priority}`.

## Tools

`hub_chat_recent {sphere, source, limit}` and `hub_chat_search {q, sphere, days, limit}` (both read-only).

## Tests

`tests/hub/test_chats.py` runs against local fake Slack and Graph servers (configurable `api_base`), no real network.
