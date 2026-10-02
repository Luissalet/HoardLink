# Today, the agenda, the digest and the family calendar

Facet `today` (`hoard_link/hub/today.py`, page script `hub/ui/today.js`, section "Hoy" / "Today" above the family
panel). It answers one question: *what needs me today, across every app?*

## The agenda contract (what an app answers)

```
GET <app>/api/family/agenda?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>&sphere=<id>      Authorization: Bearer <the app's own token>
→ {"ok": true, "items": [
     {"id": "kafka:deadline:41", "title": "Renew the lease",
      "start": "2026-10-05" | "2026-10-05T09:30:00+02:00", "end": optional, "all_day": bool,
      "kind": "deadline|delivery|birthday|followup|maintenance|release|review|cards|incident|publish|renewal|exam|other",
      "priority": "low|normal|high|urgent", "url": "http://127.0.0.1:5200/#/...", "detail": "short", "sphere": "personal"}]}
```

* `id` is stable (`<app>:<kind>:<n>`); `start`/`end` are ISO. A bare date is an all-day item (its `end` is inclusive);
  a date-time with an offset is a moment, one without is the viewer's local time. `sphere` is optional: an item
  without one belongs to the sphere the request asked for (or to the first sphere that allows the app).
* `sphere` in the query is `personal`, `work`… or absent when the hub wants everything. The hub filters by sphere
  itself, so an app may ignore it.
* Answer only what you know; default window when `from`/`to` are missing: today-7 … today+60.
* A wrong or missing token → 401. A provider error → `{"ok": false, "error": "...", "items": []}` (never a 500).

### Adding it to an app

Python (FastAPI) — `hoard_link/fam_agenda.py`:

```python
from . import family, fam_agenda          # vendored hoard_link/

def provider(date_from, date_to, sphere):  # date, date, str ("" = everything); sync or async
    return [{"id": "kafka:deadline:41", "title": "...", "start": "2026-10-05", "kind": "deadline"}]

family.install_fastapi(app, "kafka", DATA_DIR)      # configures the token file
fam_agenda.install_fastapi(app, provider)           # adds GET /api/family/agenda (moved ahead of any SPA catch-all)
```

Node (Express) — `js/parts/agenda.js`, merged into `server/hoard-link.js`:

```js
family.configure({ app: "kafka", dataDir: DATA_DIR });
family.installAgenda(app, async (from, to, sphere) => [{ id: "kafka:deadline:41", title: "...", start: "2026-10-05", kind: "deadline" }]);
```
(register it before the SPA catch-all; `from`/`to` are `YYYY-MM-DD` strings.)

Then add `"x-family": {"agenda": true}` to the app's `faustus-plugin.json`. The flag is only a hint: the hub also
probes running apps that do not carry it (one request; a 404 or an HTML page is remembered for an hour), and
`"agenda": false` opts an app out.

Both helpers drop items without a title or a usable start, fold unknown kinds to `other` and unknown priorities to
`normal`, keep only `http(s)://` and `hoard://` URLs, cap titles/details and answer at most 500 items.

## What the hub does with it

* Asks every **running** app (health check first) in parallel, 5 s timeout each, answers cached 60 s per
  `(app, from, to, sphere)`. An app that is stopped is skipped silently; an app that fails is listed under `errors`.
  When a sphere is requested, only apps the sphere allows (`spheres.app_allowed`) are asked.
* `agenda(date_from, date_to, sphere=None) -> {items, errors, apps}`; items are sorted by day, all-day first, then
  time, priority, title, and carry `app` and `app_name`.
* `today(sphere=None)` → `{date, sphere, agenda: {overdue, today, tomorrow, week}, attention: {mail, chats},
  news, system: {incidents, jobs}, notifications, last_digest, errors?}`.
  * *overdue*: items that ended before today, except history kinds (birthday, exam, release, incident); looks back 30 days.
  * *today*: starts today or spans today (a timed item earlier today stays); *tomorrow*; *week*: days 2…7 ahead.
  * *attention*: `mailgate.attention(sphere)` for mail, and for chats `chats.attention(sphere)` when that facet
    offers it, else `mailgate.attention(sphere, kind="chat")`.
  * *news*: `digest.item` events since the last digest of that sphere (24 h when there has been none). An item whose
    `data.sphere` is set belongs to that sphere; without it → personal.
  * *system*: `cassandra.incident.opened` events (last 7 days) without a later `cassandra.incident.closed` for the
    same `incident_id`, and `worktrack.active()`; both filtered by `app_allowed`.
  * *notifications*: today's `notify.sent` / `notify.held` events.

## The digest

`compose_digest(sphere)` writes markdown with the sections **Hoy / Atención / Novedades / Sistema** (Spanish when
the hub language is `es`, or `auto` and the OS locale is not English; English otherwise). When the sphere's
`digest.summarize` is true and a language model is **already loaded** (`Link` with `only_resident`, no GPU lease,
90 s timeout) it prepends up to five lines under "Lo que necesita tu atención", written from the items only (the
prompt forbids inventing; "NADA" means none). On any error or when no model is resident, the template alone is used.

`send_digest(sphere)` stores `<data>/digests/<YYYY-MM-DD>-<sphere>.json`, sends it through `notify.send(...,
group="digest", priority="normal", channels_override=<the sphere's digest.channels>)` and emits
`hub.today.digest {sphere, items, date, summary}`. A notify facet without `channels_override` still gets it (priority
`high` when the sphere routes "normal" to the digest only, otherwise it would never be pushed).

**Jobs.** When the hub runs its scheduler (`jobs_enabled`, the default), at start and every 30 s the facet makes sure each sphere with `digest.enabled` has a job
`Resumen — <name>` in `hub.jobs` (`at`/`days` from the sphere, action `{"kind": "hub", "tool": "hub_today_digest",
"args": {"sphere": "<id>"}}`). `<data>/today.json` remembers which jobs it created: a job the person deleted is
**not** recreated; a changed time, days or name of the sphere is applied once (a time edited by hand in the Jobs tab
is left alone until the sphere changes); a sphere that turns its digest off disables *its* job, and turning it on
re-enables it (a job the person disabled is never re-enabled). An existing job with the same name is adopted.

The legacy `digest.wanted` event (the example job "Morning digest event") also works: every sphere with a digest
that has not had one today gets it.

## The family calendar

`GET /calendar.ics?token=<ics token>&sphere=<id|all>` — RFC 5545: CRLF, lines folded at 75 octets, `, ; \` and
newlines escaped; one `VEVENT` per item (all-day as `VALUE=DATE` with the exclusive end, moments in UTC when they
carry an offset, floating when they do not), `UID = <item id>@hoard`, `SUMMARY = "<title> (<app name>)"`, `URL`,
`DESCRIPTION`, `CATEGORIES`, `PRIORITY`. Window: -14 … +120 days. The token lives in `<data>/today.json`
(`ics_token`); `POST /api/today/ics/rotate` makes a new one (the old link stops working).

* `ics.export_path` (a folder, e.g. a synced cloud folder): every hour the hub writes `hoard-all.ics` and
  `hoard-<sphere>.ics` there (atomic; the files carry no token).
* `ics.lan_port` (default 0 = off): a second tiny server on `0.0.0.0:<port>` that serves **only** `/calendar.ics`
  and only with the token. Anyone on the network who has the link can read the calendar: share it with your own devices.
* Set both with `POST /api/today/ics/config {export_path?, lan_port?}`.

## HTTP

| route | who | what |
|---|---|---|
| `GET /api/today?sphere=&refresh=` | family token / the page | the Today payload |
| `GET /api/today/agenda?from=&to=&sphere=&refresh=` | family token / the page | agenda only (`from`/`to`: ISO, `today`, `tomorrow`, `+7d`; default today … +14) |
| `GET /api/today/digest?sphere=&summary=0\|1` | family token / the page | the digest text, nothing sent |
| `POST /api/today/digest {sphere, send=true, summarize?}` | hub token / the page | compose + send (`send: false` = only compose) |
| `GET /api/today/ics` | family token / the page | `{url, lan_url, urls, lan_urls, export_path, lan_port, lan_error, token_set}` |
| `POST /api/today/ics/rotate` · `/config` · `/export` | hub token / the page | rotate the token · set folder/port · write the folder now |
| `GET /calendar.ics?token=&sphere=` | the token | the calendar |

## Tools

`hub_today {sphere}` (read) · `hub_agenda {from, to, sphere}` (read) · `hub_today_digest {sphere, send=true,
summarize?}` · `hub_ics_link` (read).

## Data

`<data>/today.json`: `ics_token`, `ics: {export_path, lan_port}`, `jobs: {sphere: {id, sig, disabled_by_us?}}`,
`last_digest: {sphere: {ts, date, title, file, sent, summary}}`. `<data>/digests/`: one file per sent digest.
