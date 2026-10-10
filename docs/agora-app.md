# Ágora desktop console

Ágora has a complete workspace in its own Windows window, and a full browser view at `/agora/` on the local Hub. The existing Hub tab, task histories, revisions, locks, decisions, CLI and MCP remain available. It uses the same Python Hub service; the desktop window is Rust/Tauri with a React/TypeScript interface. There is no data migration into a second coordination system.

The left rail contains Claude, Codex, Cursor and Faustus. Each provider exposes a discovered model catalog and its supported reasoning settings, shared quota observations and child-worker states. Unavailable catalogs and quota readings remain unknown. Quotas belong to account pools, not individual chats; adding a helper does not add capacity. Expired readings retain their provenance and show unknown percentages.

## Quotas: where each reading comes from

The console reads every provider's quota where its own client does, the way unified usage trackers such as CodexBar do. The Hub starts the readers; each publishes through `usage-report` as `<provider>-capacity`, hashes the account identity into an opaque pool key and never stores or logs a token.

- **Codex**: the official CLI's app-server (`account/rateLimits/read`), every 90 s (`agora_capacity.py`).
- **Claude**: `GET https://api.anthropic.com/api/oauth/usage` with Claude Code's OAuth token from `~/.claude/.credentials.json` (`CLAUDE_CONFIG_DIR` when set), every 2 min: session (5 h), the week for all models and per-model weeks. The file is only read. An expired session is published as an unknown window that says how to renew it (`claude /login` on the PC); the reading comes back on the next poll.
- **Cursor**: `GET https://cursor.com/api/usage-summary` with the Cursor app's session, read from its `state.vscdb` opened read-only, every 2 min: the two quotas included in the plan as the dashboard shows them, *Cursor models* (Auto, Composer, Grok) and *Other models*, plus on-demand spend when enabled; they reset at the end of the billing cycle.
- **Faustus**: local models have no account quota; spend caps are reported by Faustus itself.

`HOARD_AGORA_ACCOUNT_USAGE=0` turns the Claude/Cursor readers off. A network failure publishes nothing, so the last real reading stays until it goes stale; bars show the percentage used, with what is left and the reset underneath.

## Live state of each leader

Next to each leader's name the rail shows what that leader's CLI is doing right now, so an idle agent or an expired login is not mistaken for a healthy one. The Hub (`agora_leader_state.py`) reads, read-only, the session files each CLI already writes and keeps only event kinds and timestamps; message bodies, tool inputs and outputs, reasoning and file paths are never copied, and from `~/.claude/.credentials.json` only `claudeAiOauth.expiresAt` is read (never a token).

| State | Chip | How it is derived |
| --- | --- | --- |
| `working` | Trabajando | **Claude Code** (newest `~/.claude/projects/<slug>/*.jsonl`) and **Codex** (newest `~/.codex/sessions/**/rollout-*.jsonl`): a turn is open (prompt, tool call or `task_started` without its end) and the file was written in the last 30 min; with no turn markers, a write in the last 2 min. **Cursor** (`launch-state.json` + `logs/*.jsonl` of the managed runner): `status` is running with a live `cli_pid`, or the log moved in the last 2 min without a final `result`. **Faustus**: its verified native session is working. |
| `waiting` | Esperando | Claude: an `AskUserQuestion`/`ExitPlanMode` call with no result, or any tool call unanswered for 60 s (a permission prompt, best effort). Codex: an approval / `request_user_input` event not followed by the agent going on. Cursor: a run status that says it waits for input. |
| `idle` | Inactivo desde HH:MM | The last turn or run ended (or a turn stayed open for 30 min with nothing written); the time is the end of that turn, shown in the viewer's local time. |
| `auth_expired` | Sesión caducada | Claude: the last turn ended in an authentication error (401) and the credentials were not rewritten since, or `expiresAt` is in the past although the CLI ran after it (the refresh failed). An access token that expired while the CLI was merely idle is not an error: it renews on use. |
| `error` | Error | Claude: the last turn ended in another API error. Codex: the last turn ended with an `error` event. Cursor: `status` failed, a non-zero `exit_code`, an `error`, or a run still marked running whose process is gone. |
| `unknown` | Sin datos | No session files were found, or they could not be read. |

Each state carries `last_activity_at`, `source` (`claude_session`, `claude_credentials`, `codex_session`, `cursor_launch_state`, `cursor_log`, `faustus_native_session`) and a fixed-sentence `detail`. A leader that is `idle`, `auth_expired` or `error` while it owns open Agora tasks (`claimed`, `in_progress` or `changes`, owner equal to its agent id or starting with `<leader>-`) gets `idle_with_work: true`; the rail then outlines it in red and writes how many tasks wait ("2 tareas abiertas sin atender").

The Agora itself is the second witness: a leader whose agents (its id or `<leader>-*`) heartbeat or act in the Agora within 15 minutes is `working` with `source: agora` when its local files say `idle` or `unknown` (a session on another machine or in the cloud writes nothing on this PC), and is never flagged `idle_with_work`. Each state also carries `agora_last_seen_at`.

It is returned as `live_state` and `idle_with_work` on every leader of `GET /api/agora/console/overview`, alone by `GET /api/agora/console/leaders`, by the MCP tool `hub_agora_console_leaders` and by `python scripts/agora.py leaders`. Paths can be overridden with `HOARD_LEADER_CLAUDE_PROJECTS`, `HOARD_LEADER_CLAUDE_CREDENTIALS`, `HOARD_LEADER_CODEX_SESSIONS` and `HOARD_LEADER_CURSOR_DIR` (default `D:\LocalAI\cursor`).

## Conversation and colours

Luis's messages are bubbles on the right. Each leader's messages are cards outlined in that provider's colour, with the avatar and name tinted to match; the name is always written, so colour is never the only cue. The swatch next to each leader opens a colour picker. The choice is stored in the Hub (`POST /api/agora/console/color` `{leader_id, color}`, MCP `hub_agora_console_color`, returned as `color` in the overview), so the browser view and the desktop app show the same colours; an empty colour restores the default (Claude terracotta, Codex green, Cursor blue, Faustus red).

The central conversation uses the canonical team thread. Messages select all four leaders or one recipient. Delivery receipts distinguish queued attention from an actual running agent. A mention alone does not wake an idle desktop chat. Results retain their actual author and source. Model preferences apply to the selected console leader's next execution, without rewriting unrelated existing chats.

The helper control creates a separate session with the selected model and reasoning. It preserves the principal's configuration, history and identity. External workers can register their actual lifecycle through the console API/MCP. A declared model observation is identified as declared; it is not promoted to a measured provider reading. Provider authentication failures are visible and are not represented as working sessions.

The right pane shows pending tasks and recent completed work, with the actual global completion total. Task details open their ordinary history and support replies. Full operations, including task creation, cross review, decisions and locks, remain accessible in the Hub. On narrow browser screens the leader rail becomes a compact strip and the tasks become a drawer. Draft messages survive page reloads on the same client.

## Run and build

From a source checkout, install dependencies with `npm ci` in `apps/agora`, then run `npm run desktop:build`. Start the native window with `scripts/start-agora.ps1`; `-Build` builds first and `-Web` opens the full browser view. The Hub must already be running. The shell only relays `/api/agora/` requests to a loopback Hub; it exposes no arbitrary shell, URL or credential-file access to frontend code.

For development, `npm run dev` in `apps/agora` starts the local UI on port62840 and proxies the Hub API. `AGORA_HUB_URL` can select a different loopback QA Hub. Tests use a separate Hub data directory and synthetic messages, preferences, workers and quotas. The native application contains self-hosted fonts and bundled frontend assets.

Build output and installed local binaries are generated files. Source distributions require the build step for `/agora/`; a missing build produces an explicit error rather than an empty console.

## Validation boundaries

Compilation establishes that the native shell builds; browser checks establish the shared frontend's actual interactions. A scripted provider in isolated QA only checks lifecycle behavior. Real provider participation requires a separate receipt from that provider and must not be inferred from a mock, a queued mention, a heartbeat or a success-shaped response. The task submissions and QA receipts record which providers and interactions were exercised.
