# Mail gateway (facet `mailgate`)

The hub reads the inbox once for the whole family and hands each app only the mail it asked for and may see. When the
gateway is unavailable, enabled apps can use the direct helper with an identity verified by the installation. Module
`hoard_link/hub/mailgate.py`, helper `hoard_link/hub/mail_helper.py`, UI `hub/ui/mail.js` (tab "Correo" / "Mail"), app
clients `hoard_link/fam_mail.py` and `js/parts/mail.js`.

## How it reads

The hub never opens IMAP itself. It runs `mail_helper.py` under **Faustus's own Python** (`[python, mail_helper.py,
faustus_root]`, JSON on stdin, one JSON line on stdout, cwd = Faustus root), which imports Faustus's
`mcp_servers.email_server` (accounts, IMAP connect, header decoding, send configuration). So accounts and passwords stay
where Faustus keeps them. Actions: `status`, `fetch`, `read`, `send`.

Helper protocol 3 authenticates `caller: {app, token}` against a Hub declared by the installation. The gateway supplies
its own bearer token, including relocation notices sent from the Spark through the placement link. Python and Node apps
send the family token configured for their own app. Direct helper access also requires an enabled entry in the mail
settings and permission for each account's sphere. The PC Hub and Spark placement write `data/mail_installation.json`
at the Faustus installation. The helper takes its validation addresses from that file. `HOARD_MAIL_REAL_HUB_URLS`
still serves the clients' isolation check. Launcher variables and request bodies cannot select the helper's validator.

Placement writes the Spark loopback link, the registered app IDs and names, a copy of the sphere mail rules and SHA-256
fingerprints of the deployed Phileas and Tantalus module tokens. If the link fails before it returns a response, the
helper can identify those two modules with the declaration after a restart. It still checks the current mail app list
and filters each account by sphere. A responding Hub's refusal, invalid reply or sphere decision takes precedence.
Placement keeps a separate declaration on each machine. When the data move, the person's `mail_callers.json` moves
with them. Refresh placement after changing module token files or sphere rules used during outages.

On first creation, the mail app list enables the registered entries among Kafka, Hermes, Ledger, Phileas and Tantalus.
Existing lists keep the person's choices. The settings API can show app IDs and names from the declaration while the
Spark link is down.

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
`spheres.classify` (attention / normal / low). Family app reads and account selection require a sphere policy that
grants access. When that policy is missing or fails, the gateway and direct helper deny access. The hub's page (`ui`),
internal tools and the hub token keep their privileged access.

An app registers an **interest**: `{subject_terms, from_domains, from_addresses, text_terms, regex, has_attachment}`. A
message matches when ANY non-empty criterion matches (accents and case ignored; a domain matches its subdomains).
Registering again replaces the previous interest of that app, and the stored mail is re-matched retroactively.
`claim(ids, kind, ref)` says "this one is mine" (`kind` payment / document / shipment …, `ref` a `hoard://` uri); a claimed
message leaves the "needs you" and unclaimed lists. Unclaimed mail is grouped by a guessed category in the page.

## Installation and rollback

Use the maintenance window authorized by the operator and keep the previous five revisions available. Check each
step before continuing. Leave a manually stopped app stopped.

1. **Hub on the PC.** Install the Hub first so existing clients have the new identity routes before any of them uses
   the new helper. Check that Faustus's `data/mail_installation.json` contains the local Hub origin. Check that
   `/api/family/caller` resolves a registered app's token to that app and refuses an unidentified caller. Check a
   gateway mail status request with the Hub's own identity.
2. **Faustus.** Install the helper and Settings changes. Open Settings > Mail in Spanish and English. Check that the
   list shows registered apps and that toggling one updates the list and can be undone. Preserve existing choices
   and confirm Ledger is enabled on a newly created list. Check an authorized direct status and one denied account.
3. **Clients.** Install the client copies. Use Kafka, Phileas and Tantalus's regular status paths to verify the
   gateway and the direct helper. Confirm the PC Hub registrations for Phileas and Tantalus resolve the tokens from
   each module's effective data directory. Verify Ledger's registration and configuration while it remains
   stopped. Check its regular
   status path when the operator separately decides to resume it.
4. **Spark.** Apply placement or refresh the deployed stack. Check that placement declared the link and the two
   module fingerprints before it replaced helpers on an existing Spark installation or started services on a
   fresh move. Compare those fingerprints with the PC registrations
   without logging bearer values. Verify both modules' status over the link, then repeat via
   direct fallback with the link unavailable, including after a restart. Confirm disabled apps and accounts in a
   forbidden sphere stay denied. Restore the link and check status over it again. Use the Spark rollout only
   after these checks pass.

For rollback, restore the previously deployed helper copies first, including the standalone copy uploaded by
placement and the gateway copy. Protocol 2 accepts the extra `caller` field, so the clients can still ask those
helpers for status. Check both Spark modules with the link unavailable if the Spark is deployed. Restore the
previous clients, then Faustus, then the Hub and the previous placement files. Check gateway and direct status
after each restoration. Keep `mail_callers.json` with the person's data so a later upgrade retains the choices.

The check covers the production callers that use the updated helpers. Idle vendored protocol 2 scripts in other
repositories can still be invoked directly by a process with access to the installation. The installation files and
the mail database are accessible to processes running under the same operating-system identity, so this contract
does not provide an operating-system permission boundary. The unauthenticated family mail app catalog returns IDs
and names only. Identity and sphere decisions come from the authenticated caller route.

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
| `GET /api/family/caller` | family bearer token | Returns the authenticated app ID and registered app names. With an `X-Hoard-Mail-Account` JSON header containing `[account, id, address]`, returns whether the caller can access that account's sphere. |
| `GET /api/family/mail-apps` | any | Registered app IDs and names for the mail settings. |
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
    ...                                               # the direct helper if the installation Hub can authenticate the app
```

`available()` is cached for 30 s and false when the last good pass is older than `interval*180 + 900` s. Nothing in
`fam_mail` raises: an unreachable hub is `{"ok": False, "error": "hub unreachable"}`. The Node twin is
`js/parts/mail.js` (`mailAvailable`, `mailRegisterInterest`, `mailMessages`, `mailClaim`, `mailCopyAttachment`).

## Notes

* `mail.db` is inside the hub data dir, so a backup includes it; add it to `backup.exclude` if mail should not be copied.
* Tests never touch real timers or an IMAP server: `tests/hub/test_mailgate.py` (fake runner), `tests/hub/test_mail_helper.py`
  (the real helper in a subprocess against a fake Faustus root and a fake IMAP connection), `tests/test_fam_mail.py`.
