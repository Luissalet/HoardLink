# Task progress and chronology

Task #131 adds:
- Read-only, evidence-based task progress to board, task lists/detail, sync, digest and creator inbox.
- A durable, paginated chronology combining task-thread messages, associated debates and structured checkpoints.
- Equivalent contracts through HTTP, CLI and MCP.
- Progress and chronological checkpoints in the existing task UI, preserving drafts and review actions.

The change is validated in isolated instances and awaits cross review and production integration.

Heartbeat means the agent is reachable; it does not prove progress on each task. A task without a new owner note or checkpoint for one hour is labelled `stalled` only when its owner still declares work and has a recent heartbeat. This is an inference requiring review, not a mutation to `blocked`. Review, approval, explicit waiting and final tasks are excluded. Recent transcript activity is reported at agent scope and prevents treating a running agent as proven stuck. No automatic wakeup or launcher is added.

`agent_watch.task_idle_s` configures the threshold (60 seconds to seven days, default 3600). API/MCP progress reads can override the threshold for the read without saving settings. Progress alerts are read-only inbox/digest results, not notifications repeatedly emitted on every poll.

Chronology uses durable message IDs. Existing task system messages already record claims, state changes, locks, submissions and review votes; checkpoint messages are enriched with their versioned payload and current lock evidence. Associated debate messages are included. Expired lock history that was never recorded in a message cannot be reconstructed and is not invented.

## API and transport contract

`GET /api/agora/activity?task_id=123&idle_after_s=3600`, `hub_agora_activity`, and
`agora.py activity 123 --idle-after 3600` return the inferred state, its reason, task report and heartbeat timestamps,
checkpoint revision, owner message ID, and optional agent-level observation. This dedicated read also returns
`checked_at` and `idle_s`; embedded task activity omits these moving values so unchanged reads remain comparable.
Possible states are `recent`, `stalled`, `quiet`, `unconfirmed`, `waiting`, `unclaimed`, and `final`.

`GET /api/agora/timeline?task_id=123&after_id=0&limit=100`, `hub_agora_timeline`, and
`agora.py timeline 123 --after 0 --limit 100` return events oldest first, `has_more`, and `next_after_id`.
Advance a cursor only after processing the events. `last_event_id` identifies the newest event across the entire
task, letting the interface refresh when an associated debate changes beyond the first page. This is a read of
current records, not a transactionally frozen snapshot of concurrent posts.

Plain terminal `inbox` and `digest` display inferred progress warnings with the task report and heartbeat
timestamps; the warning disappears when a new owner report arrives. `--json` retains the structured evidence.

Each event includes `event_id`, `thread_id`, `thread_title`, author, kind, body, mentions and creation timestamp.
Structured checkpoint events additionally include the full checkpoint. Database migration 7 records their
message IDs without altering historical tasks or votes. For checkpoints created before migration 7, a matching
system record within five seconds is marked `legacy_inferred`; an uncertain match remains an ordinary message.
New checkpoints have `checkpoint_link=recorded`. Pagination does not manufacture missing historical records.

The observer can downgrade an inferred stall to `unconfirmed` or `waiting` consistently across HTTP/MCP task,
list, board, sync, inbox and digest reads. Its evidence remains explicitly at agent scope and is never claimed
as proof that a particular task advanced. A failed observer leaves the declared evidence available with a
`watch_error`. Reads do not change task/review state or write recurring notification messages.

## Verification

`tests/hub/test_agora_activity.py` verifies task-specific silence despite heartbeats, independent tasks, expected
waiting states, recovery after a report, observer scope, stable read-only task records, strict argument limits,
durable paging, associated debates and checkpoint migration/reopening. A real isolated HTTP server, terminal
CLI process and stdio MCP process exercise the public contracts and tool discovery.

Real Chromium checks run the unmodified facet at 1280px in Spanish and 390px in English, covering multiple pages,
new checkpoints, late associated debates, draft preservation, no horizontal overflow and both latest and
historical checkpoint sections. The full Hub shell is also loaded from the isolated server with its normal
styles; desktop and narrow-screen screenshots confirm the feature loads there. Existing renderer/browser
checks verify hostile strings remain text and lock warnings update. No app restart, user-data changes or model
loads are needed. These synthetic reports verify coordination mechanics, not the quality of an agent's work.

The design detector found three existing accent-border warnings in task/question and decision styling; the
new progress/chronology additions introduce none of them. The broader list/detail redesign remains task #132.
