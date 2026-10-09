# Agent sessions (`agent_sessions`)

"What did the agents write, and can I take it back?" Module `hoard_link/hub/agent_sessions.py`, UI tab **Sesiones de agente** / *Agent sessions*
(`ui/agent_sessions.js`; the badge counts sessions that still have something undoable). No data of its own: it reads the write journals the
apps keep ([accountable agents](../commons/accountable-agents.md)) with the token of each app, which the hub already knows.

## What the page shows

* A strip of the apps: *con diario* (journal on), *sin diario todavía* (the app does not keep one yet), *apagada* (not running).
* Agents, newest first, each with its sessions: app, session id, number of writes (failed, undone, still undoable), the reasons given, the tools
  used. *Detalle* lists the lines of the session (time, tool, reason, the masked arguments summary).
* **Deshacer sesión**: asks the app for a dry run, shows what *will be undone*, what is in *conflict* (another agent or session wrote to the same
  object afterwards, or the object changed in the app) and what *cannot be undone* (no handler, no snapshot), and only then, on **Confirmar y
  deshacer**, repeats the call with `confirm`. Writes of other sessions are never touched.
* **Tokens por agente**: pick an app, an agent id and a profile (*solo lectura*, *borradores*, *todo*) and mint a token (shown once, copy it);
  list and revoke the existing ones.

## Routes (operator only)

Only the hub's page or the hub's own bearer token (`data/mcp-token`) may use them; another app's token gets 403.

| Route | Does |
|---|---|
| `GET /api/agent-sessions[?refresh=1&limit=]` | `{apps: [{id, name, state: ok|not_adopted|down|unauthorized, entries, reasons_required, undo_tools}], agents: [{agent, sessions, writes, undoable, last_ts}], sessions: [...], adopted}`; each session `{key, app, app_name, agent, session, first_ts, last_ts, writes, failed, tools, reasons, undoable, undone, can_undo, entries}`. Cached 3 s. |
| `GET /api/agent-sessions/session?app=&session=&agent=` | every journal line of one session |
| `POST /api/agent-sessions/undo {app, session, agent?, dry_run (default true), confirm?, reason?}` | the app's `POST /api/agent/undo` (default reason *Deshacer sesión desde el Hub*) |
| `GET /api/agent-sessions/tokens[?app=]` | the tokens of each app (no secrets) |
| `POST /api/agent-sessions/tokens {app, agent, profile, label?}` | mints through the app's `POST /api/agent/tokens` |
| `POST /api/agent-sessions/tokens/revoke {app, id}` | `DELETE /api/agent/tokens/{id}` on the app |

Tool: `hub_agent_sessions {app?, session?, agent?, refresh?}` (read only). Undoing and minting are not tools on purpose: an agent must not be able to
undo another one.

Events: apps emit `agent.write` and `agent.undo` ([events](../HUB.md#facets-07)); the journals are the source of truth, the events are hints (a rule can
notify on `agent.undo`, for example).

## Relation with the Agora

The [Agora](../AGORA.md) coordinates coding agents with each other (tasks, locks, threads). Agent sessions are about what those agents (or any MCP
client) did to the apps' data. They share the agent id: use the same id in `HOARD_AGENT_ID` and in your Agora heartbeat to find one from the other.
