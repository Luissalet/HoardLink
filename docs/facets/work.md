# Jobs across apps (`worktrack`)

"What is the family doing right now?" Module `hoard_link/hub/worktrack.py`, UI tab "Trabajos" / "Work" (`ui/work.js`,
badge = active jobs), data `<data>/work.json` (active jobs + the last 500 finished).

## The events

Canonical: `<app>.job.queued|started|progress|done|failed|cancelled` with
`{job_id, title, kind, progress (0..1), gpu, eta_s, url, error}` (a progress above 1 is read as a percentage; a job
without `job_id` is keyed by `kind:title`).

Legacy names are mapped by `events.EVENT_ALIASES` when the event is stored (`EventLog.emit` writes the canonical type and
keeps the app's own name in `data._orig_type`; defaults such as `kind` only fill what the event lacks):

| legacy | canonical |
|---|---|
| `pygmalion.job_queued`, `pygmalion.job_done` | `pygmalion.job.queued`, `.done` |
| `hypatia.teacher_job.done` / `.no_model` | `hypatia.job.done` / `.failed` (kind `teacher`) |
| `galton.run.done` | `galton.job.done` (kind `run`) |
| `lumiere.render.done` / `.failed`, `lumiere.job.failed` | `lumiere.job.done` / `.failed` (kind `render`) |
| `links.media.done` / `.failed` | `links.job.done` / `.failed` (kind `download`) |
| `midas.backtest.finished` | `midas.job.done` (kind `backtest`) |
| `vitruvius.render.done`, `vitruvius.assay.done` | `vitruvius.job.done` (kind `render` / `assay`) |
| `hub.backup.done` / `.failed` | `hub.job.done` / `.failed` (app `hub`, kind `backup`) |

Add more with `events.register_alias(legacy, canonical, **defaults)`. Rules and queries written against the old name
keep matching (`rules.rule_matches` and `EventLog.query(type=...)` also look at `_orig_type`).

## Behaviour

* The facet subscribes to the event log; events go through a queue to its own worker thread, so `emit` never waits.
* `hub.lease.granted|released` are joined by `owner` (the job's app, or `app:...`/`app-...`): a running job shows the GPU
  and VRAM it holds. A queued job holds nothing yet.
* A job queued or running without news for 6 hours is `stale` (left out of `active()` unless asked; archived as `stale`
  after a week).
* On `failed`: event `work.failed {app, job_id, title, error}` and `notify.send(priority="high", group="job",
  dedupe_key="<app>:<kind>:failed")` (when the notify facet exists).

## Python / HTTP / tool

`active(app=None, include_stale=False)` (used by the Today facet), `recent(limit, app, failed_only)`, `stats()`.
`GET /api/work?active=1&app=&limit=&failed=1&stale=0`, `GET /api/work/stats`. Tool `hub_work {active, app, limit}`
(read-only; `active: false` adds the finished ones).
