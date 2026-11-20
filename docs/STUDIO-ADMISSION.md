# Studio work admission

## Machine turns

Workers or their coordinator request a turn through `hub_work_ticket` before starting a heavy process, a test suite
or another worker. All spaces on one Hub share the same capacities. A ticket may reserve several classes at once.

| Class | Slots |
| --- | ---: |
| `cpu` | 2 |
| `ram` | 2 |
| `suite` | 1 |
| `worker` | 4 |

Call `request` with `agent`, `resources` such as `{"suite":1,"cpu":1}`, optional `key` for an idempotent retry, and
`priority` (`low`, `normal`, `high`). Wait until `state` is `running` before launching. Poll with the same agent and
ticket to maintain the reservation while queued and while running. Release it on completion or cancellation. Request
and poll return the release arguments as undo. Tickets expire after 90 seconds without a poll or when this Hub stops.
Priority applies to queued work, with arrival order within each priority. The existing GPU lease still reserves VRAM.

## Order budgets

Set order limits with `hub_order_budget` and `{space_id, order_id, agent, retries, gpu_seconds, returns}`. Budgets
are assigned per order. Workers call `hub_order_attempt` with the order, its task, an attempt `key`, and
`action: "begin"` before doing work. Give the planned GPU seconds for this attempt so other stations see the
reservation. Finish with `action: "pass"` or `"fail"`, the same key and the actual seconds. Repeating a key reads its
existing attempt. Failed attempts consume a retry for that station job. Returns are counted from the order's recorded
piece defects, including decisions that send a piece back.

At the limit the order waits and new attempts return `state: "waiting"` with the reason. It keeps any open gate,
or opens one with the available, current draft pieces. When the order has no draft, show the budget hold in
Orders until a draft is available. Only the person's gate can keep or cancel pieces. After an exhausted budget,
continuing requires a revised budget with capacity remaining. The budget and attempt operations return a token for
`hub_agora_order_undo`, which also rolls back a gate opened by that write if its state is still current.

Report actual GPU time from the station. The budget reserves planned seconds and stops subsequent work when actual
use reaches the limit. This accounting does not interrupt an external process midway through its own GPU lease.

## Capability routing

`hub_worker_capability` accepts `reasoning_high`, `vision_strict` or `fast_local`. The Hub filters its known runner
catalogues and quota observations. Strict vision requires a model that declares image input. Local fast work prefers
the configured shared service and can fall back to another runner with quota. The normal
`hub_agora_console_worker` create action accepts the same `capability` and tries another provider after a start
confirmed unavailable. An explicitly named worker keeps its provider and identity. Existing explicit provider,
model and effort selections remain available.

## Measures

The Orders page and `hub_order_metrics` show:

- Orders with a fully decided first gate and all first decisions kept, grouped by template.
- Logical pieces whose first decision kept revision one. Superseding revisions share the piece's lineage.
- Defects returned, elapsed work and reported GPU use by station. Passed and failed attempt costs appear in their own
  columns. An older station with only a defect receipt shows missing time and GPU fields.

HTTP uses `/api/work-admission/metrics` and `/api/work-admission/capability` for reads, and
`POST /api/work-admission/ticket`, `/api/work-admission/budget` and `/api/work-admission/attempt` for work.
