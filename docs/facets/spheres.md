# Spheres (facet `spheres`)

Two lives kept apart: `personal` and `work` exist by default; the person may add more. A sphere says which
mail accounts and chat sources are its own, who and what counts as "attention" or "low", when not to disturb,
how a notification of each priority is delivered, when the daily digest is composed and which apps may see
its mail. Module `hoard_link/hub/spheres.py`, UI `hub/ui/spheres.js` (a chip in the top bar).

## File `<data>/spheres.json`

```json
{"active": "personal",
 "spheres": [{
   "id": "work", "name": {"es": "Trabajo", "en": "Work"}, "color": "#3d7bd9",
   "mail_accounts": ["corp", "me@corp.com"],   // Faustus account id / name / address; "*" = every account nobody claims
   "chat_sources": ["slack-acme"],             // ids of the sources of the chats facet
   "vip": ["boss@corp.com", "@bigclient.io"],  // addresses or @domains (subdomains included): always "attention"
   "keywords": ["urgente", "asap"],            // whole words, accents and case ignored: "attention"
   "mute": ["@promo.net", "oferta"],           // addresses, @domains or words: "low"
   "quiet_hours": {"start": "19:00", "end": "08:30", "days": "daily"},   // days: daily | weekdays | weekends | ["mon", ...]
   "notify": {"urgent": ["windows"], "high": ["windows"], "normal": ["digest"], "low": ["digest"]},
   "digest": {"enabled": true, "at": "08:45", "days": "weekdays", "summarize": true, "channels": ["windows"]},
   "apps": ["kafka"]                           // apps that may see this sphere's mail; "*" = all; [] = none
 }]}
```

Channels: `windows`, `ntfy`, `telegram`, `email`, and `digest` ("do not push, keep for the digest").
The file is re-read when it changes on disk; a missing or unreadable file means the defaults. `personal`
cannot be removed. Writes are validated: a bad colour, time, day list or channel is refused with `400` naming the field.

## Classification

`classify` decides in this order: a **VIP** sender is `attention`; a **muted sender** (address, `@domain`,
or a word of the display name) is `low`; **keywords** in subject/text, `mentions_me` or `direct` make it
`attention`; a **muted word** makes it `low`; else `normal`. `reasons` lists what matched
(`vip:<entry>`, `keyword:<word>`, `mention`, `direct`, `mute:<entry>`).

Quiet hours may cross midnight (a window belongs to the day it starts on); `start == end` or empty times turn them off.

## Python API (`hub.facet("spheres")`)

| call | returns |
|---|---|
| `active() -> str` | id of the active sphere |
| `get(id) -> dict \| None`, `list() -> list[dict]`, `ids()` | copies |
| `set_active(id) -> dict` | `{ok, active, from, changed}`; emits `hub.sphere.changed {from, to}` when it changed |
| `upsert(sphere) -> dict`, `save_all(list) -> dict`, `remove(id) -> dict` | validated edits |
| `sphere_of_account(selector, address="") -> str` | explicit lists first, then the sphere with `"*"`, default `personal` |
| `sphere_of_chat_source(source_id) -> str` | |
| `sphere_of_app(app_id) -> str` | first sphere whose `apps` allows the app, else the active one |
| `classify(sphere_id, *, sender, subject="", text="", mentions_me=False, direct=False)` | `{priority: attention\|normal\|low, reasons}` |
| `in_quiet_hours(sphere_id, now=None) -> bool` | |
| `app_allowed(sphere_id, app_id) -> bool` | an unknown sphere allows no app |

## HTTP

| route | who | |
|---|---|---|
| `GET /api/spheres` | anyone on loopback | `{ok, active, spheres}` |
| `POST /api/spheres` | hub token or the hub's page | `{spheres: [...]}` replaces all, `{sphere: {...}}` upserts one |
| `POST /api/spheres/active` `{id}` | any family token | Faustus and the apps switch it |
| `POST /api/spheres/remove` `{id}` | hub token or page | not `personal` |

```bash
curl -s -H "Authorization: Bearer $(cat data/mcp-token)" -d '{"sphere": {"id": "work", "vip": ["boss@corp.com"]}}' http://127.0.0.1:8810/api/spheres
curl -s -H "Authorization: Bearer $APP_TOKEN" -d '{"id": "work"}' http://127.0.0.1:8810/api/spheres/active
```

## Tools

`hub_spheres` (read-only) and `hub_sphere_set {id}`.

## UI

The chip shows the active sphere's name and colour; click to switch or open "Configurar esferas…"
(form per sphere, plus an "Edit JSON" fallback). On load and after every switch it sets
`<html data-sphere="<id>">` and the `--sphere-color` variable, and dispatches
`ctx.bus.dispatchEvent(new CustomEvent("sphere", {detail: id}))` so other facets follow. It polls every
5 s, so a switch made by Faustus shows up too. `window.HubSpheres.openDialog()` opens the dialog (the
notifications tab links to it).
