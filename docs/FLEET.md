# Fleet monitoring

The Hub reconciles declared family apps, its jobs and rules, published watcher/task metadata and explicitly named Windows scheduled tasks every 60 seconds. Faustus Home shows the same published state. `GET /api/fleet` requires an existing family bearer token or the Hub's own page authority; MCP `hub_fleet_status` uses that existing credential. Both return the rows, coverage and five brief groups: today, waiting for you, blocked, unverified and paused. The morning digest includes these groups.

## States and timing

Explicit pause wins. An app whose health check fails is only **silent** once the failure is confirmed: two failed probes at least 60 seconds apart (until then it is unverified, reason `app_down_unconfirmed`, because one 1.5 s probe is not an outage); a service mismatch is an error. A confirmed finished failure or partial watcher result is an error. Started/running attempts never become successful receipts. Without an error, a recurring item becomes silent strictly after its second expected opportunity counted from the latest of: its last success, its declaration, and **its last resume from a pause**. Resuming restarts the clock: a task switched back on after days off is not silent the moment it is enabled. The Hub learns about a resume from the producer (`resumed_ts`: Hub jobs stamp it when they are re-enabled, Faustus stamps `resumed_at` on every paused-to-active change, a registered expectation re-enabled or reporting again after `paused`) or, for any source, from seeing the item go from paused to running. Published `expected_slots` computed before a resume are ignored until the producer republishes them. Daily/weekday jobs use the next two actual slots, so a weekend is not a pair of missed working days. Native Faustus cron/timezone calculations publish two slots. Items with no declared cadence (event rules, event tasks, one-off tasks) expose only their last result; their timing is not certified, so unless they failed, wait for you or are paused they are listed in the separate `no_cadence` group of the brief (a collapsed "no schedule declared" list in Home) and never in Today or Unverified. They stay in `items`. Unavailable sources, invalid clocks and unsupported cadences remain unverified.

Windows metadata exposes a completed result together with its **start** time, because Task Scheduler does not supply a finish time through this interface. That start is only a lower bound. Fleet persists when it first observed that completed result and measures silence from that observation, retaining it across refresh/restart; it does not invent a finish timestamp. A new completed start resets that observation. Running result 267009 is not a success. Only unbounded time-trigger repetition, simple daily and weekly schedules are calculated. Finite repetition windows, logon/event triggers, multiple triggers, expiry, random delay and timezone-bound calendar triggers are unverified. See [Microsoft's repetition semantics](https://learn.microsoft.com/en-us/windows/win32/taskschd/repeating-a-task).

Faustus tasks with run-window/period guards or `run_all` DST policy keep their recorded outcomes but have unverified cadence. One-off and event tasks do not acquire a recurring cadence. No prompt, answer, error body, task action/arguments, URL credentials or foreign database is read by the Hub.

## Configuration

The existing Hub configuration accepts `fleet`:

```json
{
  "fleet": {
    "enabled": true,
    "notifications": true,
    "autostart": true,
    "expected_apps": ["tantalus"],
    "providers": [],
    "faustus": false,
    "windows_tasks": [],
    "rule_cadences": {"declared-rule-id": {"every": "2h"}}
  }
}
```

Autostart profile members and app manifests with `x-family.fleet: true` are included automatically. Tantalus publishes its retained finished watcher results (with the cadence the scheduler really keeps, and nothing for a watcher with nothing to check) through authenticated `/api/family/fleet` and read-only MCP `fleet_status`. Setting `faustus: true` uses the first configured `faustus_urls` endpoint and existing Hub credential to read Faustus's authenticated `/api/family/fleet`; it never starts Faustus. Windows task names/path+names must be explicitly listed. Only metadata of those tasks is read; no task is enabled, launched, stopped or edited. Missing tasks and truncated/unavailable providers are visible in coverage.

Other scheduled producers register through Hub-token authenticated `POST /api/fleet/expectations` / MCP `hub_fleet_register` with id, owner, kind and either `every` (at least one minute) or `at`/`days`. They report `ok`, `error`, `paused` or `waiting` through `/api/fleet/receipt` / `hub_fleet_report`; app tokens can report only expectations of that app. Waiting reasons are bounded to login, approval, configuration and other. Older receipts are ignored and owner reassignment is rejected. Read-only status does not send notifications or consume a transition.

## Notifications and limits

One normal notification is routed through the existing sphere/channel rules on entry into **silent** or **waiting-for-user** state. A confirmed failure (`error`) does not push: it is shown in Blocked, and the producing app has its own alerts. Repeated reconciliations, reads and restarts retain the transition generation and do not repeat it. A later recovery followed by another failure is a new transition. Explicit manual app stops are pauses, so they do not alert as spontaneous failures. Monitoring never retries, restarts, approves or changes the work it observes.

- **First sight is a baseline.** The first time the Hub observes an item (a new install, a lost journal, a provider that just appeared) it records the state and does not push; an old task that is already silent is not news. The one exception is a `waiting` receipt less than 10 minutes old.
- **One summary, not a storm.** More than 3 pushes in one reconcile become a single notification ("Flota: 5 cosas necesitan tu atención") whose body lists the items (8 at most, then "and N more"). Every change is still emitted on the event bus.
- **Apps are Cassandra's first.** An app outage is pushed by Cassandra's "App con problemas" (once per burst). Fleet shows the same outage in Home and the brief but only pushes if it is still silent 60 seconds after it was confirmed, Cassandra has opened no incident for that app in the last 30 minutes, and Fleet has not pushed that app in the last 30 minutes. A flapping app is one burst, one push.
- **Labels.** A label that masks to nothing (a task named only after a secret) is shown and pushed as its kind and id, e.g. "Tarea del Hub abc123".

## Responsiveness

`GET /api/fleet`, `hub_fleet_status` and the Today page answer from the cache the background reconcile keeps (every 60 s, and woken a few seconds after a registration or receipt). Nothing waits for health probes, providers or the Windows query. `refresh=1` asks for a background pass and waits at most about one second; if it is not done the current cache comes back with `"refreshing": true`. `/api/today` never reconciles inline. A cold Hub answers `refreshing` with an empty view and coverage error `warming_up` until its first pass ends. A read never sends or consumes a notification.

The Hub must be running for reconciliation and pushes. This is not an independent monitor of the Hub itself. Existing notification quiet hours/disabled channels still apply. Metadata receipts certify the app's reported result, not an independent successful purchase, delivery, stock detection or LLM interpretation. Tantalus keeps at most 5000 runs; the Hub caps each provider and the merged view at 1000 rows and reports truncation. Unknown providers do not become healthy. The persisted `fleet.json` journal contains only bounded public metadata and state; invalid journals are preserved rather than silently overwritten.
