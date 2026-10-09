# Accountable agents (HoardLink 0.8.2)

Several coding and assistant agents (`codex-sparks`, `cursor`, `faustus`, ...) call the same Hoard apps over MCP or REST. This part of
`hoard_link.agentkit` lets an app answer four questions about what they did, and lets the person take it back:

1. **Who?** The agent and the session of every call.
2. **Why?** A mandatory `reason` on every call that changes something.
3. **What?** A write journal per app, with a short summary of each call (secrets masked).
4. **Undo.** Take back everything one session did, writes of other sessions untouched, with a dry run first.

Plus **permission profiles**: extra tokens that let an agent only read, or only write drafts.

Everything is opt-in per app (default off), so apps adopt it one by one. The standard library plus FastAPI/pydantic (already needed by
`make_agent_router`) is all it uses. The Node router (`makeAgentRoutes`) does not have it yet.

Code: `hoard_link/agentkit.py` (router, `Tool` hooks), `hoard_link/agent_journal.py` (journal, masking, undo engine),
`hoard_link/tokens.py` (agent tokens and CLI), `hoard_link/bridge.py` (identity headers), `hoard_link/hub/agent_sessions.py` (the
Hub facet, see [docs/facets/agent-sessions.md](../facets/agent-sessions.md)). Tests: `tests/commons/test_agent_accountability.py`,
`tests/commons/test_bridge_agent_headers.py`, `tests/hub/test_agent_sessions.py`.

## 1. Who calls: agent and session

A call may carry its identity in any of these places (first one found wins, except that a scoped token fixes the agent):

| What | Where |
|---|---|
| agent id | token from `agent_tokens.json` > header `X-Agent-Id` > body `agent` > body `caller` |
| session id | header `X-Agent-Session` > body `_session` > body `session` |

Both are cleaned (printable, trimmed) and cut to 80 and 120 characters. The session is an opaque id: the agent's chat or run id.

**The MCP bridge** (`CatalogBridge`, which every app's `mcp_server.py` uses) forwards two environment variables, read on every call, as those
headers (and the agent id also as `caller`, so apps that predate the headers still log it):

```
HOARD_AGENT_ID=codex-sparks  HOARD_AGENT_SESSION=run-42   # set by whoever launches the MCP server
```

`bridge.agent_headers()` is the function that does it. Nothing else changes in an app's `mcp_server.py`.

The audit event `agent.call` now carries `session` too.

## 2. Mandatory reason

`make_agent_router(..., reasons=True)`: a tool whose annotations are not `readOnlyHint` needs a `reason` of 3 to 300 characters, in the
body (`{"name", "arguments", "reason"}`) or among the arguments. Otherwise nothing runs and the answer is

```json
400 {"error": "This tool changes data, so the call needs a reason.", "code": "reason_required",
     "hint": "Add a `reason` (3-300 characters): one sentence on why you are making this change. It is kept in the history so the person can review and undo what you did."}
```

* `GET /api/agent/tools` then lists `reason` as a required property in the input schema of every write tool (and adds a sentence to the
  instructions), so the model sees it. The catalogue is unchanged for read tools.
* The reason is taken out of the arguments before the tool validates them, unless the tool's own model declares a `reason` field.
* Only `/api/agent/call` asks for it. An app's own UI does not go through that route (Cicero's REST routes call `call_tool` directly), so it
  is exempt by construction.
* Off (the default): nothing is required; a `reason` that is sent is still recorded in the journal.

## 3. The write journal

Give the router a folder: `make_agent_router(..., data_dir=<path or callable(request)>, tools=TOOLS, ctx_fn=...)`. Every non-read-only call
that reached the tool (successful or not; refused calls did not write anything) appends a line to `<data_dir>/agent_journal.jsonl`:

```json
{"v":1,"id":"01J...","kind":"write","ts":1790000000.1,"app":"cicero","tool":"slide_update","agent":"cursor","session":"run-42",
 "reason":"Fix the typo in the title","args_digest":"sha256:0f3a1b2c3d4e5f60","args_summary":"{\"slide_id\":\"01J...\",\"title\":\"Q3 results\"}",
 "ids":["id=01J...","slide_id=01J..."],"objects":["deck:01J.../slide:01J..."],"etag":"9a8b7c6d5e4f3a2b","ok":true,"error":"",
 "ms":12,"profile":"all","token":"","undoable":true,"before":{"...":"..."}}
```

* `args_digest`: SHA-256 (first 16 hex) of the canonical arguments with secrets masked. `args_summary`: at most 300 characters, strings cut to
  60, lists to 6 items. Secrets are masked in both: values of keys that name a secret (`password`, `token`, `api_key`, `authorization`,
  `cookie`, `credential`, `secret`, ...), bearer tokens, `sk-`/`ghp_`/`xox`/AWS key shapes, JWTs, `user:pass@` in URLs, `password=...` pairs and
  any opaque string of 40+ characters that mixes letters and digits. `reason` and `error` are masked the same way.
* `ids`: the identifiers in the result (`id`, `*_id`, `*_ids` at the top level), plus what the tool's `track` hook adds.
* `objects`: slash-separated paths the write touched (`deck:A/slide:B`). Two writes touch the same object when the paths are equal or one is an
  ancestor of the other. Without a `track` hook the paths are guessed from `*_id` arguments and the result's ids (flat); a `track` that
  returns `objects: []` means "touched nothing that can conflict" and nothing is guessed. `track(args, result, ctx=None)` receives the app
  context when it declares `ctx` (the result it sees is already trimmed to about 20 KB, so read big state from `ctx`).
* `before`: what the tool's `capture` hook returned before the write (at most 256 KB; larger and the write is marked not undoable).
* The file is append-only and rotated at 5 MB: `.jsonl` -> `.jsonl.1` -> `.2` -> `.3`, older lines are dropped. Readers (and undo) see the whole chain.
* Each write also emits the family event **`agent.write`** `{journal_id, tool, ok, agent, session, reason, undoable, objects}` (never the arguments).

**Reading it:** `GET /api/agent/journal?session=&agent=&kind=&since=&limit=&full=` with a bearer token. `limit` is the last N lines (1-1000, default
100), oldest first. Each write line has `undone` (a later undo reversed it) and, unless `full=1` (main token only), `has_snapshot` instead of
`before`. The answer also lists `undo_tools`. A scoped token only sees the lines of its own agent. `404 journal_unavailable` when the app did not
give a `data_dir`.

## 4. Undo a session

Apps register how to take a write back, in the `Tool` definition:

```python
Tool("slide_update", "...", SlideUpdateArgs, ann(False, False, True), run,
     capture=lambda svc, args: {...snapshot of the slide before...},        # runs just before the write; kept as record["before"]
     track=lambda args, result: {"objects": ["deck:D/slide:S"], "etag": digest_of(result)},   # runs just after
     undo=lambda svc, record, dry_run=False: {...})                          # takes it back
```

* `undo(ctx, record)` gets the journal line (`before`, `ids`, `objects`, `etag`, ...). It returns a dict saying what it did and raises
  `AppError("conflict", ...)` when the object no longer matches `record["etag"]`: that is how a change made through the app's own UI, which the
  journal never saw, is detected. A handler that declares `dry_run` is also called with `dry_run=True` and must then only check and describe.
* Use a **content** etag (a hash of the fields that matter), not a revision counter: undoing the newest write of a session restores the state the
  previous write left, and the older write's etag must match it again.

`POST /api/agent/undo {"session", "agent"?, "dry_run"?, "confirm"?, "reason"?}` (main token, or a scoped `all` token for its own agent):

* Looks at the writes of that session (and agent, if given) that succeeded and were not undone, **newest first**, and nobody else's.
* A write is a **conflict** (left alone, reported) when a later write by another session, not itself undone, touched an overlapping object; or when
  its handler raises `conflict`. It is **not undoable** when its tool has no handler, no snapshot was kept (`capture_failed`, `snapshot_too_large`) or
  the handler failed (`undo_failed`). One problem does not stop the rest.
* `dry_run: true` changes and records nothing and says what would happen. Without it `confirm: true` is required (`400 confirm_required`), and
  `reason` too when the app asks for reasons. Unknown session: `404 session_not_found`. A dry run checks each write against the current state,
  except that a write on an object the same session wrote again later is planned as "after the newer ones are taken back" (as a real run does).
* Every real attempt appends a line `kind: "undo"` (`undoes` = the write's id, `ok`, `error`, `reason`, `actor`) to the journal, and the event
  `agent.undo` is emitted once.

```json
200 {"ok": true, "app": "cicero", "session": "run-42", "agent": null, "dry_run": false,
     "undone":      [{"id","tool","agent","session","ts","objects","summary","detail":{...handler result...}}],
     "would_undo":  [],            // filled instead of "undone" on a dry run
     "conflicts":   [{"id","tool",...,"reason":"later_write_by_other_session","message","with":{"id","tool","agent","session","ts"}}],
     "not_undoable":[{"id","tool",...,"reason":"no_handler"|"capture_failed"|"snapshot_too_large"|"undo_failed","message"?}],
     "already_undone": ["01J..."], "ignored_failed": [...], "complete": true,
     "counts": {"writes": 3, "undone": 3, "conflicts": 0, "not_undoable": 0, "already_undone": 0}}
```

`complete` is true when nothing was left over (no conflict, nothing not undoable).

## 5. Permission profiles

Besides the main token (`mcp-token`: every tool and route), an app accepts tokens from `<data_dir>/agent_tokens.json`:

```json
{"<sha256 of the token>": {"agent": "codex-sparks", "profile": "drafts", "label": "laptop", "created": 1790000000.0}}
```

Only hashes are stored (mode 0600). The file is re-read when it changes, so minting or revoking needs no restart.

| Profile | May call |
|---|---|
| `read_only` | tools annotated `readOnlyHint` |
| `drafts` | those, plus tools annotated `draftSafeHint` (`ann(..., draft_safe=True)`: they write drafts or new objects but never delete or publish) |
| `all` | everything (but the token administration) |

A blocked call is `403 profile_forbidden` with a hint (and still an `agent.call` event with the error); an unknown tool is still 404. A tool with no
annotations counts as a write. A scoped token fixes the agent id of its calls, sees only its own journal lines, and can undo only with the `all` profile.

**Minting:**

```
python -m hoard_link.tokens mint   --app-data-dir <app>/data --agent codex-sparks --profile drafts [--label laptop]   # prints the token once
python -m hoard_link.tokens list   --app-data-dir <app>/data
python -m hoard_link.tokens revoke --app-data-dir <app>/data (--id <id> | --agent codex-sparks)
```

or, from the Hub page (**Sesiones de agente** -> *Tokens por agente*), through the app's admin routes, which only the main token may call:
`GET /api/agent/tokens`, `POST /api/agent/tokens {agent, profile, label?}` (returns the token once), `DELETE /api/agent/tokens/{id}`.

The agent then runs its MCP server with `<APP>_TOKEN` (for Cicero, `CICERO_TOKEN`; or `<APP>_TOKEN_FILE`, see `bridge_token`) set to that token instead of reading
`mcp-token`, and with `HOARD_AGENT_ID` / `HOARD_AGENT_SESSION`.

## 6. Adopting it in an app

```python
router = make_agent_router(tools_fn=tool_catalog, call_fn=_call, token_fn=lambda request: services(request).token,
                           instructions=AGENT_INSTRUCTIONS, app_name="cicero",
                           reasons=True, data_dir=lambda request: services(request).config.data_dir, tools=TOOLS)
```

then add `undo` / `capture` / `track` to the tools that can be taken back and `draft_safe=True` to the ones that only create or edit drafts. A tool
without hooks still gets a journal line and can be reported as "not undoable".

| App | Vendors 0.8.2 | Reasons | Journal | Undo handlers | draft_safe tools |
|---|---|---|---|---|---|
| Cicero's Hoard | yes | on | yes | deck_create, deck_update, deck_theme, source_add, source_remove, outline_generate, outline_update, slides_generate, slide_add, slides_add, slide_update, slide_edit_text, slide_set_image, slide_regenerate, slide_approve, slide_revert, slide_delete, slides_reorder | create/add/edit tools, never delete or export |
| Prospero's Hoard | yes | on (`/api/agent/call`; its per-tool routes and web UI are the person's way in and are exempt) | yes | studio_create_project, studio_delete_project, studio_delete_assets, studio_import, studio_trash (restore), studio_cast, studio_style_cards, studio_production_shots, studio_graphic_shot, studio_production_segments, studio_production_settings, studio_production_finishing, studio_production_script, studio_production_create; renders and other GPU jobs are not undoable | project, import, generation, voice and render tools, graphic shot, production create / segments / settings / finishing / script; never delete or send to another app |
| every other app | not yet (the next `sync_vendored.py` run brings the library; the router flags are off until the app turns them on) | | | | |

Update this table when an app adopts it.
