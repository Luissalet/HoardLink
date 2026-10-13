# Persistent Agora spaces

The common space registry lives in `hoard_link.coordination.spaces`. The existing Agora service owns its records in the same `agora.db`; it does not create per-client stores. Older task, thread and message IDs remain unchanged and belong to the default `ecosystem` space.

## API

- `GET /api/agora/spaces` lists spaces, current revisions and members.
- `GET /api/agora/spaces/<space_id>` reads one space.
- `POST /api/agora/space_create` accepts `title`, optional `id`, `description`, `project_id` and `workspace`, plus the existing actor identity.
- `POST /api/agora/space_update` accepts `space_id`, the observed `expected_revision` and changed title/description/project/workspace fields. A stale revision returns HTTP409 with `error_code: stale_space` and `current_revision`.
- Existing scoped reads accept `?space_id=<id>`; writes accept the same `space_id` in their body. Omission selects `ecosystem`.

Tasks, conversations, decisions, dependency operations, checkpoints, inbox, sync and read/ack operations stay in the selected space. Direct task/thread IDs from another space return the ordinary missing-object response. Agents are listed by their participation in that space. Quota pools and resource reservations remain global; a model or overlapping path reserved in one space conflicts with requests in another. A heartbeat still renews its existing selection of owned resources across spaces.

Read cursors are independent by actor and space. The original ecosystem cursor remains in the existing `reads` table for compatibility. Acknowledgements are already tied to globally unique thread IDs. The additive migration preserves stored submissions, reviews, history and checkpoints. No migration of Council or Group history is claimed by this first delivery.

## MCP and terminal

Four new tools use the same service: `hub_agora_spaces`, `hub_agora_space`, `hub_agora_space_create` and `hub_agora_space_update`. Existing Agora tools accept optional `space_id`; they retain their previous arguments and behaviour when it is omitted.

```text
agora --as builder spaces
agora --as builder space-create "Research project"
agora --as builder --space <id> board
agora --as builder --space <id> add "Compare alternatives" --kind research
agora --as builder --space <id> space-update --revision 1 --title "New title"
```

`AGORA_SPACE` selects the terminal default. The Hub tab offers a space selector, creation and renaming. Switching spaces retains each open task/conversation composer and new-object form during that page session. Responses from an earlier space selection cannot replace the active view. `space_id` in the page URL allows returning to the selected context.

## Verification

The source tests cover a legacy database migration with unchanged field values, independent cursors, globally conflicting model reservations, concurrent creation in three spaces, scoped handover/sync and restart. Native HTTP, CLI and stdio MCP share the canonical records. Desktop and mobile browser tests change space, retain drafts, rename the same identity and deliberately release an older board response after switching away.

Full Council/Group adoption and the complete standalone Agora interface are subsequent deliveries in the migration plan. The first space delivery is not the completed unification.
