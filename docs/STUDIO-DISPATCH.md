# Studio dispatch

## Account availability and automatic review

The Codex reporter reads native app-server quota. It combines the account window and scoped limit buckets. A missing
or expired window is unknown, even when another window has a measurement. A Claude access token's timestamp describes
when the CLI may need to refresh. A recorded authentication refusal and an explicit native sign-out are reported
separately. The periodic quota diagnostic runs `auth status` and makes no model call.

The space's worker profile has a **Review provider**, **Review model ID** and **Review effort**. Leave the provider on
**Automatic (low cost)** to select an economical model. The review starts only with a different provider from the
author, with fresh measured quota and a verified runner. The default never reuses the interactive leader's model and
effort. A configured reviewer that is unavailable falls back to the economical selection. Corrections still use the
existing handover preferences. The same setting is available in the space-profile HTTP contract as
`review_runner: {provider, model, effort}`.

## HTTP and app skills

Both `GET /api/apps/<id>` and `GET /api/apps/<id>/status` return the app state. The Hub treats a browser abandoning a
JSON poll as a closed connection, including Windows errors 10053 and 10054. Other failures still produce the normal
server error.

`hub_proxy_check` probes the selected app or all catalogued apps without launching them. It returns elapsed time and
one of `ready`, `slow`, `unreachable`, `catalog_error` or `no_proxy`. The catalogue gets up to eight seconds by
default, configurable with `timeout_s` (2–30 seconds). A reply after two seconds and a timed-out probe both appear in
`slow`. A timed-out probe has no confirmed catalogue.

Through `hub_call_app`, `skills_list` lists what the app carries and `skill_read` reads an installed skill or its
references. Both use the app's shelf contract, so they also work when its HTTP bridge omits those stdio tools or its
process is stopped. The manifest's shelf settings and the person's shelf selections still apply.

## Isolated app instances

`hub_isolated_app` and `POST /api/instances/isolated` take the same fields:

- `{action: "start", app, worktree}` creates a fresh data folder below the current Hub's data directory, chooses
  loopback ports and launches the app from the linked worktree. Its own generated token identifies it as that app
  for family calls.
- `{action: "call", instance_id, tool, arguments, agent, reason}` targets the instance and passes through the app's
  result, including any app undo receipt. A call can return 503 until the app has written its token.
- `{action: "status", instance_id}` reports process state, ports and the log path. `{action: "list"}` lists instances
  managed by the current Hub process.
- `{action: "stop", instance_id}` stops only the process tree this Hub started and withdraws its token. The returned
  undo action (`resume`) reopens the same isolated data. Starting returns a `stop` action as undo.

The worktree must have its own dependencies. Writer's native development launcher accepts separate bridge and renderer
ports and a hidden window. Its QA profile starts with the agent bridge enabled and generates its own token. Other apps
can declare `x-family.isolated_instance` with `data_env`, `port_env`, and optional `token_env` for their own headless
launcher. Its code must write the token in the isolated folder. Nothing starts the registered installation.
