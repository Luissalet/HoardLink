# Order receipts and skill tests

Orders → **Receipts and skill tests** shows each recorded call, its arguments and
reply, the job's draft procedures, and tests run against an earlier call. The
section loads on demand. Both the Spanish and English views work without a
terminal.

## Record a call

Use `hub_order_call` with `order_id`, the job's `task_id`, `tool`, and
`arguments`. For an app tool pass `app` as well. The instrumented call returns
the original tool result under `reply`. Failed calls have receipts too. Supply
`space_id` to verify that the order belongs to that space. When a call belongs
to the order as a whole, omit `task_id` and optionally give its `station`.

For an agent session working on one job, set `HOARD_ORDER_RECEIPT` before
starting the Hub CLI or stdio MCP client. Use a JSON object, for example
`{"space_id":"estudio","order_id":12,"task_id":34}`. These clients attach the
context to calls. HTTP clients can pass the same object as `_order_receipt` in a
Hub tool's arguments or at the top level of the app call body.

The Hub records the order, job, station, app, tool, start time, duration, outcome,
arguments and original reply in `order-receipts.sqlite` under its data
directory. Authentication headers and named credential fields are excluded or
redacted. A call whose reply could not be observed keeps its arguments and shows
that there is no recorded reply. Calls outside the Hub and calls with no order
context have no receipt.
The oldest calls remain available through cursor pagination. A receipt's
`receipt_undo` removes the receipt. The original reply retains any undo supplied
by the tool itself, which is what reverses its app or Hub change.

## Draft and test a skill

1. Use `hub_order_skill_draft` with the order and job. Its Markdown lists observed
   steps in order, links to the original receipts, and takes inputs, outputs, and
   the verification instruction from the job and station. It stays a draft.
2. Use `hub_skill_bench_start` with a successful receipt, a description of the
   step, and either a `draft_id` from the job or a published `slug`. By default the
   worker gets the original call's arguments as inputs. Pass `inputs` to supply
   the step's materials or paths in a test profile.
3. The Hub starts a fresh Haiku worker at low effort from an empty directory.
   The worker receives the skill, step, inputs, and how to call the Hub. The
   reference reply, working chat, and project briefing stay outside its prompt.
   Its calls are tagged with a run ID and excluded from draft generation.
4. Read the test with `hub_skill_bench` by run ID or skill slug. The Hub compares
   the worker's observed reply for the reference tool and app. The default is
   the entire reply. Pass RFC 6901 JSON pointers in `compare` to compare stable
   output fields, such as a count, a piece digest, or `/result/rules`, when the
   response includes changing timing or IDs. A missing field differs from
   `null`. A worker that fails or omits the call cannot pass.

Tests use live tools with the inputs supplied. Use disposable profiles and test
outputs for steps that write. Results are stored by skill slug and the hash of
the tested Markdown in the Hub data directory, beside the draft history in the
Orders view. `hub_skill_bench_undo` stops an active run or removes a finished test
from the skill's history. Use each tool's own undo for its changes.

## Link a skill to a handoff

Published `SKILL.md` files can declare inline lists such as
`apps: [vitruvius]`, `accepts: [production-brief, web-page]` and
`produces: [interface-brief, responsive-captures]`. Use the exact piece types in
the station catalogue. The Hub matches both ends of each edge, keeps the
manifest's explicit `skill` and reports compatible library slugs in `skills`.
With several outputs on a handoff, a chain step shows only the matches for its
output. A skill can also match an app when explicitly named by its manifest.

## Read from HTTP

The Hub page and Hub token can read `/api/production/order_receipts`,
`/api/production/order_receipt` and `/api/production/order_skill_draft` with
`order_id` and the relevant `receipt_id` or `draft_id`. Read
`/api/production/skill_bench` with `run_id` or `slug`. The same operations have
agent tools and can be called through the Hub's CLI.
