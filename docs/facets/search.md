# Global search (`search`)

One question to every app that can answer it. Module `hoard_link/hub/search.py`, UI tab "Buscar" / "Search"
(`ui/search.js`; `Ctrl+K` focuses it from anywhere on the page).

## How it works

1. Catalogues: for every app on the contract (`app.agent_contract`) the hub reads `GET /api/agent/tools` through
   `hub.app_tools` (cached 10 minutes; an app that did not answer is retried after 20 s).
2. Picking tools (`pick_search_tools`): the name ends with `_search`, starts with `search_` or is `search`,
   `recall_search`, `library_search`; it is not marked `readOnlyHint: false` / `destructiveHint: true`; its schema has an
   input property named `query|q|text|term|search`; it requires nothing else (besides `limit`). At most 3 per app, the
   plain `search` first.
3. Calls: all in parallel (thread pool), 6 s each, `{prop: q}` plus `limit` only when the schema has it.
4. Normalising (`normalise`): the first list in the answer (`results|items|hits|matches|links|cards|documents|messages|data|...`,
   else any list of objects, one level into `result`/`data`; JSON text and MCP-style `{content: [{text}]}` are unwrapped)
   becomes `{title, snippet, id, url, score?, ref?}` from the usual keys
   (`title|name|subject|front|label|filename`, `snippet|summary|text|excerpt|back|description`, `id|uid|key|*_id`,
   `url|link|href`). Relative URLs are joined to the app's address; HTML is stripped.
5. The hub's own stores: `mail` (facet `mailgate`, mail and chats), `refs` (labels of linked records) and `notify`
   (notification history). Absent facets add nothing; one that raises becomes a group `error`.

## HTTP and tool

`GET /api/search?q=&apps=a,b&limit=8` -> `{ok, q, groups: [{app, name, tool, ms, results, error?, own?}], hits, skipped, took_ms}`.
`apps` restricts the search (`mail`, `refs`, `notify` name the hub's own groups). Groups with hits come first (best
score, then most hits), then empty ones, failures last. `skipped` lists apps with no search tool or no catalogue.
Tool `hub_search {q, apps, limit}` (read-only).

## UI

Input (Enter), grouped hits with the app's icon (`/api/apps/<id>/icon`); a click opens the hit's URL, or the app when it
has none. Failures and apps without a search of their own are listed under the hits.
