# Notifications (facet `notify`, "Hermes")

One place decides how the person is told. Apps send to the hub instead of carrying their own toast / ntfy /
Telegram / mail code (they keep it as the fallback for when the hub is unreachable). Module
`hoard_link/hub/notify.py`, UI `hub/ui/notify.js` (tab "Avisos" / "Notifications"), app clients
`hoard_link/fam_notify.py` and `js/parts/notify.js`.

## What the hub does with a notification

1. `enabled: false` → held `disabled`.
2. **Duplicate**: same `(app, dedupe_key)` — or app + title when no key — inside `dedupe_window_s`
   (6 h) → held `duplicate` (the original anchors the window).
3. **Route**: the sphere (given, else the first sphere whose `apps` allows the app, else the active one) maps
   the priority (`low|normal|high|urgent`) to channels.
4. **Quiet hours** of that sphere hold everything but `urgent` → held `quiet`; it is also emitted as a
   `digest.item` so it shows up in the digest.
5. A route with only `digest` → held `digest` (+ `digest.item {title, url, watch: app, kind: group|"notify", sphere}`).
   A route with `digest` and channels pushes and keeps.
6. **Rate**: more than `rate_per_app` (20) pushes of one app in an hour → the rest held `rate`, and one
   summary push per app and hour (`group: "rate-summary"`).
7. Delivery to every channel in parallel; each result is stored. Events: `notify.sent {id, app, sphere,
   priority, title, channels, delivered}` or `notify.held {id, app, sphere, reason, title}`.

`channels_override` (used by the Today digest) names the channels directly and skips routing, quiet hours and
the rate limit; duplicates are only checked when `dedupe_key` is given.

## Channels

| channel | config | notes |
|---|---|---|
| `windows` | `enabled` | PowerShell toast (`Windows.UI.Notifications`), run on its own thread with a 20 s timeout; a late failure is written to the row. Off Windows: `{"ok": false, "unsupported": true}` |
| `ntfy` | `server` (https://ntfy.sh), `topic`, `token` | JSON POST to the server root; priority 2..5, `click` = url |
| `telegram` | `bot_token`, `chat_id`, `api_base` | `sendMessage` HTML; "Detectar chat" calls `getUpdates` |
| `email` | `via: "faustus"\|"smtp"`, `to`, and for smtp `host, port, user, password, tls, from` | `faustus` = the mail gateway facet's `send_mail(subject, text, to=None, html="")`; without it `{"ok": false, "error": "mail gateway not available"}`. `smtp` = smtplib (port 465 → SSL, else STARTTLS when `tls`) |

Every channel sender is injectable (tests, other front-ends): `NotifyFacet.senders = {"windows": fn}` or
`facet.senders = {...}`; `fn(note, channel_config) -> dict | bool | str | None` (a string is an error text).

## Config `<data>/notify.json`

```json
{"enabled": true, "dedupe_window_s": 21600, "rate_per_app": 20,
 "channels": {"windows": {"enabled": true},
              "ntfy": {"enabled": false, "server": "https://ntfy.sh", "topic": "", "token": ""},
              "telegram": {"enabled": false, "bot_token": "", "chat_id": "", "api_base": "https://api.telegram.org"},
              "email": {"enabled": false, "via": "faustus", "to": [], "host": "", "port": 465, "user": "", "password": "", "tls": true, "from": ""}}}
```

Secrets (`ntfy.token`, `telegram.bot_token`, `email.password`) are stored here and **never returned**: GET shows
`••••last4` (just `••••` when short). Posting a masked or empty secret keeps the stored one, `null` clears it.
Per-sphere routing is read here and edited in the spheres dialog. History is in `<data>/notify.db`.

## HTTP

| route | who |
|---|---|
| `POST /api/notify` `{title, body, priority, url, group, dedupe_key, tags, sphere, icon_app}` | any family token — the caller is the app; naming another app is `403`. Hub / page may set `app` and `channels` |
| `GET /api/notify?limit=&app=&sphere=&since=&since_id=&held=&unseen=1&q=&group=` | loopback. `since`: epoch or ISO; `held`: a reason, `any`, `none` |
| `GET /api/notify/config` | loopback, secrets masked, plus `status` per channel and `routing` per sphere |
| `POST /api/notify/config` | hub token or page — partial merge, validated |
| `POST /api/notify/test` `{channel}` | hub token or page — ignores enabled/routing/quiet hours |
| `POST /api/notify/read` `{ids \| all: true}` | hub token or page |
| `POST /api/notify/telegram/discover` | hub token or page — finds the chat id |

```bash
curl -s -H "Authorization: Bearer $APP_TOKEN" -d '{"title": "Payment failed", "body": "Netflix 12.99", "priority": "high", "dedupe_key": "pay:42"}' http://127.0.0.1:8810/api/notify
```

## Python API (`hub.facet("notify")`)

`send(title, body="", *, app="hub", sphere=None, priority="normal", url="", group="", dedupe_key="", tags=(), icon_app=None, now=None, channels_override=None) -> dict`
(`{ok, id, app, sphere, priority, held, channels: [{channel, ok, error?, skipped?, unsupported?}], delivered, duplicate_of?}`),
`history(limit=50, *, app, sphere, since, since_id, held, unseen, q, group) -> list[dict]`, `get_notification(id)`,
`unseen_count()`, `mark_read(ids=None)`, `test_channel(name)`, `config()` / `config_view()` / `update_config(patch)`.

## Tools

`hub_notify {title, body, priority, url, sphere, group, dedupe_key, channels}` ("avísame…") and
`hub_notify_history {limit, app, sphere, held, unseen, q}` (read-only).

## From an app

```python
from .hoard_link import fam_notify
res = fam_notify.notify("Payment failed", "Netflix 12.99", priority="high", url=url, group="payment", dedupe_key=f"pay:{tx_id}")
if res.get("error") == "hub unreachable":
    own_toast(...)                       # the app's own channel, as before
fam_notify.hub_available()               # cached 30 s; True only for a hub that has this facet
```

```js
const res = await family.notify("Payment failed", "Netflix 12.99", { priority: "high", url, group: "payment", dedupeKey: `pay:${id}` });
if (!res.ok && res.error === "hub unreachable") ownToast(...);
await family.hubAvailable();
```
