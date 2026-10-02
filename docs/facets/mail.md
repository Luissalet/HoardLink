# Mail gateway (facet `mailgate`)

The hub reads the inbox once for the whole family and hands each app only the mail it asked for and may see. Apps stop
carrying their own IMAP code (they keep it as the fallback for when the hub, or its gateway, is not there). Module
`hoard_link/hub/mailgate.py`, helper `hoard_link/hub/mail_helper.py`, UI `hub/ui/mail.js` (tab "Correo" / "Mail"), app
clients `hoard_link/fam_mail.py` and `js/parts/mail.js`.

## How it reads

The hub never opens IMAP itself. It runs `mail_helper.py` under **Faustus's own Python** (`[python, mail_helper.py,
faustus_root]`, JSON on stdin, one JSON line on stdout, cwd = Faustus root), which imports Faustus's
`mcp_servers.email_server` (accounts, IMAP connect, header decoding, send configuration). So accounts and passwords stay
where Faustus keeps them. Actions: `status`, `fetch`, `read`, `send`.

* **Incremental**: per account and folder the helper keeps a UID watermark plus UIDVALIDITY (a changed validity restarts
  that folder). The first read takes the newest `max_per_pass` messages of the last `since_days`; later reads take the
  oldest not yet seen.
* Messages land in `<data>/mail.db` (SQLite; chat messages share the table, `kind = 'chat'`). Bodies are stored with a
  folded `search_text` column; attachments as files under the hub data dir.
* **`enabled` defaults to false**: the hub reads no mail until the person turns the gateway on. `send_mail` works either way.

## File `<data>/mail.json`

```json
{"enabled": false, "interval_min": 10, "retention_days": 60, "owner": "", "max_per_pass": 300, "since_days": 14}
```

`interval_min: 0` = manual only (no background thread). `retention_days` clears the text, snippet, links and attachment
files of old messages; the headers stay. `owner` selects the Faustus accounts of one person.

## Spheres, priority, interests

Each message gets the sphere of its account (`spheres.sphere_of_account`) and a priority from
`spheres.classify` (attention / normal / low). With no `spheres` facet everything is `personal` / `normal` and every app
is allowed. **Mail text is never returned to an app outside the sphere's `apps`**; the hub's page (`ui`) and the hub
token see everything.

An app registers an **interest**: `{subject_terms, from_domains, from_addresses, text_terms, regex, has_attachment}`. A
message matches when ANY non-empty criterion matches (accents and case ignored; a domain matches its subdomains).
Registering again replaces the previous interest of that app, and the stored mail is re-matched retroactively.
`claim(ids, kind, ref)` says "this one is mine" (`kind` payment / document / shipment …, `ref` a `hoard://` uri); a claimed
message leaves the "needs you" and unclaimed lists. Unclaimed mail is grouped by a guessed category in the page.

## Python API (`hub.facet("mailgate")`)

| call | returns |
|---|---|
| `send_mail(subject, text, to=None, html="") -> dict` | helper answer `{ok, error, account, from, to, server}`; works with the gateway off (used by the `notify` email channel) |
| `ingest(rec, *, sphere=None, source=None, emit=True) -> dict` | store one Kafka-style record (used by `chats` for the Outlook inbox) |
| `run_pass(*, force=False) -> dict` | one read now; `{ok, new, read, ...}` |
| `attention(sphere=None, days=7, limit=50, kind="mail") -> list` | unclaimed, undismissed, priority attention (for Today) |
| `unclaimed(sphere=None, days=7, limit=50) -> list` | no interest matched and no claim |
| `search(q, sphere=None, days=30, limit=20, kind=None) -> list` | text search over mail and chats (for `search`) |
| `get_message(id, full=True) -> dict \| None` | |
| `status(refresh=False) -> dict` | `ready` = enabled and Faustus found and at least one ok pass |
| `store` | `MailStore` (`insert_message`, `query`, `get`, `dismiss`, `claim`, `set_interest`, `prune`, `counts` …) |
| `runner` | a `subprocess.run`-compatible callable; tests inject a fake |

Attributes for tests: `background` (False = no thread), `first_delay_s`, `next_pass_ts`.

## HTTP

| route | who | |
|---|---|---|
| `GET /api/mail/status` | any family caller | `?refresh=1` (ui/hub) also asks the helper for the accounts |
| `GET /api/mail/config`, `POST /api/mail/config` | ui/hub | validated; a bad value is `400` naming the field |
| `GET /api/mail/messages` | any | `since_id` (ascending + `last_id` cursor), `limit`, `sphere`, `q`, `interest`, `unclaimed`, `full`, `kind`, `source`, `folder`, `days`, `order`, `hide_dismissed`; apps default `kind=mail`, `interest=1`, and only see their spheres |
| `GET /api/mail/messages/<id>` | any | full message; `404` when it is outside the app's spheres (existence is not revealed) |
| `GET /api/mail/attachments/<sha>` | any | bytes, `nosniff` |
| `GET /api/mail/interests`, `POST /api/mail/interests`, `POST /api/mail/interests/remove` | app (own) / ui, hub (any `app`) | |
| `GET /api/mail/attention`, `GET /api/mail/unclaimed` | any | |
| `POST /api/mail/claim` | app | `{ids, kind, ref}` -> `{ok, claimed, skipped}` |
| `POST /api/mail/dismiss` | ui/hub | `{ids, undo?}` |
| `POST /api/mail/fetch` | ui/hub | `409` when disabled, `502` when the helper fails |

Event: `mail.received {mail_id, sphere, source, from_domain, subject (<=120), priority, interests}`.

## Tools (`hub_tools`)

`hub_mail_status`, `hub_mail_search`, `hub_mail_get` (text capped at 8000, `truncated`), `hub_mail_attention`,
`hub_mail_unclaimed` (all read-only) and `hub_mail_fetch`.

## For apps

```python
from hoard_link import family, fam_mail
family.configure("ledger", DATA_DIR)
if fam_mail.available():            # gateway on, read at least once, and not stalled
    fam_mail.register_interest({"subject_terms": ["factura"], "has_attachment": True})
    page = fam_mail.messages(since_id=last_seen)      # {ok, messages, last_id}
    for m in page["messages"]:
        ...                                           # also carries Kafka's keys: from, from_address, date, ts, account, from_self
        fam_mail.claim([m["id"]], "payment", "hoard://ledger/tx/12")
        fam_mail.copy_attachment(m["attachments"][0], dest_dir)
else:
    ...                                               # the app's own helper
```

`available()` is cached for 30 s and false when the last good pass is older than `interval*180 + 900` s. Nothing in
`fam_mail` raises: an unreachable hub is `{"ok": False, "error": "hub unreachable"}`. The Node twin is
`js/parts/mail.js` (`mailAvailable`, `mailRegisterInterest`, `mailMessages`, `mailClaim`, `mailCopyAttachment`).

## Notes

* `mail.db` is inside the hub data dir, so a backup includes it; add it to `backup.exclude` if mail should not be copied.
* Tests never touch real timers or an IMAP server: `tests/hub/test_mailgate.py` (fake runner), `tests/hub/test_mail_helper.py`
  (the real helper in a subprocess against a fake Faustus root and a fake IMAP connection), `tests/test_fam_mail.py`.
