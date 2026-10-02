# Notifications and mail: one implementation

Every app that tells the person something (toast, ntfy, Telegram, mail) or reads their inbox used to carry its own
copy of the same code: `build_toast_ps1`, `_scrub`, `_send_ntfy`, `telegram_discover_chat_id`, a Faustus finder, a
`subprocess` call to a vendored `faustus_mail.py`, an `auto | hub | own` switch. Since 0.8 each of those exists once.
Standard library only, Windows first (no console windows), Python in `hoard_link/`, Node in `js/hoard-link.js`.
Tests: `tests/commons/test_notify_channels.py`, `test_notify_router.py`, `test_mail_router.py`,
`tests/hub/test_mailgate_fields.py`.

| Piece | Python | Node |
|---|---|---|
| Toast, ntfy, Telegram, SMTP, quiet hours, secret scrubbing | `hoard_link.notify_channels` | `buildToastPs1`, `showToast` (the rest is HTTP: use `fetch`) |
| The `auto / hub / own` switch | `fam_notify.Router` | `notifyRouter` |
| Find Faustus and its Python | `fam_mail.faustus_dir`, `fam_mail.faustus_python` | `faustusDir`, `faustusPython` |
| Run the mail helper | `fam_mail.FaustusHelper` | `spawnHelperRunner` |
| Hub gateway first, helper as the fallback, one watermark | `fam_mail.MailRouter` | `famMailRouter` |
| The helper itself (IMAP, Gmail, SMTP; runs under Faustus's Python) | `hoard_link/mail_helper.py` | the same file, spawned |

## Notifications

### `hoard_link.notify_channels`

```python
xml_escape(text) -> str                     http_url(url) -> str          clean_url(url) -> str
scrub(text, secrets) -> str                 # removes every secret from an error text
build_toast_ps1(title, body="", url=None, app_name=None, app_id=POWERSHELL_APP_ID) -> str
send_toast(title, body="", url=None, *, app_name=None, runner=None, platform=None, timeout=20) -> {ok, error?, unsupported?}
send_ntfy(server, topic, title, body="", *, priority="normal", url=None, token=None, timeout=10, tags=None, attach=None, http=None) -> {ok, error?}
send_telegram(token, chat_id, text, *, timeout=10, api_base=TELEGRAM_API, parse_mode="HTML", http=None) -> {ok, error?}
telegram_text(title, body="", url=None, link_label=None) -> str
telegram_discover_chat_id(token, *, api_base=TELEGRAM_API, timeout=10, http=None) -> {ok, chat_id, name, error}
send_smtp(cfg, to, subject, body="", *, html=None, smtp_factory=None, default_from="hoard@localhost") -> {ok, error?}
send_via_helper(run, subject, body="", *, html=None, to=None, timeout=60) -> {ok, error?}   # run = FaustusHelper(...).run
in_quiet_hours(now, start, end, *, priority="normal", allow_high=True, days=None) -> bool
```

`cfg` for SMTP: `host, port, user, password, tls, from` (SSL on 465, STARTTLS otherwise). `start` / `end` are `"HH:MM"`
or minutes since midnight; a window may cross midnight; `urgent` is never held and `high` only when `allow_high=False`.
Nothing here raises. The hub's own channel code (`hub/notify.py`) now imports these functions, so the hub and the apps
cannot drift apart.

### The `auto | hub | own` switch

```python
router = fam_notify.Router(via_getter, own_send, app_name="")   # via_getter() -> "auto" | "hub" | "own"
router.send(title, body="", *, priority="normal", url=None, group=None, dedupe_key=None, sphere=None)
    -> {"via": "hub" | "own", "ok": bool, "why": str}          # with the hub also: held, id, hub
router.via_status() -> {setting, effective, hub_available}
```

* `own` uses only `own_send(title, body, *, priority, url, group, dedupe_key, sphere)`.
* `hub` uses only the hub and reports a failure.
* `auto` uses the hub when it answers and `own_send` when it does not. A message the hub **held** on purpose (quiet
  hours, duplicate, digest, rate limit, notifications off) is delivered as far as the app is concerned and is **not**
  repeated through the app's own channels.

Node: `notifyRouter({ via, ownSend, app })` with `send(title, body, { priority, url, group, dedupeKey, sphere })`,
`via()` and `viaStatus()`, plus `showToast(title, body, url, { appName })` and `buildToastPs1(title, body, url)`.
`fam_notify.notify` is unchanged for apps that only want to call the hub.

## Mail

### The helper and its runner

```python
fam_mail.faustus_dir(setting=None) -> Path | None     # setting, FAUSTUS_DIR / HOARD_FAUSTUS_DIR / HOARD_HUB_FAUSTUS_DIR, a "faustus"
                                                      # next to the app, the usual places, then the hub's own hint
fam_mail.faustus_python(root, setting=None) -> str | None
helper = fam_mail.FaustusHelper(setting=None, *, owner=None, python=None, env_drop_prefixes=(), runner=None)
helper.available() -> bool
helper.run(action, payload=None, timeout=180) -> dict  # never raises: {"ok": False, "error": ...}
helper.status(refresh=False) -> dict                   # run("status"), remembered five minutes
```

`helper.run` actions (all read-only: EXAMINE and BODY.PEEK, nothing is marked read, moved or deleted):

| Action | Payload | Answer |
|---|---|---|
| `status` | - | which accounts would be read and what sending would use |
| `fetch` | `since {account: {folder: last_uid}}`, `validity`, `since_days`, `max`, `folders`, `attachments_dir`, `categories` | `accounts[{folders{last_uid, validity, new, remaining}}]`, `messages` oldest first |
| `scan` | `since_days`, `max`, `skip` (Message-IDs), `skip_own`, `query`, `gmail_query`, `subject_terms`, `sender_domains`, `attachments_dir`, `categories` | `accounts[{matches, new}]`, `messages` newest first |
| `headers` | `since_days`, `max` | one small record per message: `message_id, from_address, ts, list_unsubscribe, one_click, category, from_self` |
| `read` | `message_id`, or `account` + `folder` + `uid` | `message` |
| `send` | `subject`, `text`, `html`, `to`, `from_name` | `{ok, error}` through the account's SMTP |

Every message record: `message_id, subject, from_name, from_address, date, ts, to, cc, in_reply_to, references, text,
links, attachments[{name, mime, size, sha, path}], images[{alt, src}], headers{list_unsubscribe, one_click,
gmail_category, message_id}, account, account_id, account_address, folder, uid, from_self`, plus `html` (at most 240000
characters) only when the mail carries structured markup (schema.org JSON-LD or microdata), always with `"html": "all"`,
never with `"html": "none"`. Gmail categories (`promotions`, `social`, `updates`, `forums`) are filled in for `fetch`
by default and for `scan` with `categories: true`.

### The router: hub gateway first, helper second

```python
router = fam_mail.MailRouter(helper, *, source_getter, interest, watermark_get, watermark_set,
                             claim_kind="", page=100, max_pages=6, register_every_s=21600,
                             sphere=None, deep_uses_helper=False, auto_claim=False)
rows = router.scan(**criteria)          # list of message dicts; scan_ex() returns {ok, source, error, messages, more, pending_since, ...}
router.commit()                         # the watermark moves only now, after the app stored the rows
router.claim(rows, ref="hoard://app/thing/1", kind=None)   # "this mail is mine", grouped by ref; ref may be a function of the row
router.last_source                      # "hub" | "faustus" | "" (the scan failed); also last_error, last_meta
router.status() -> {setting, effective, hub_available, helper_available, interest_registered, hub_since_id, ...}
```

* `source_getter()` returns the person's setting: `auto` (hub when its gateway is ready, otherwise the helper; a hub
  that fails is also a reason to fall back), `hub` (never falls back, the error is reported) or `faustus` (`own` and
  `helper` mean the same).
* `interest` is a spec or a function of the criteria. It is registered again every six hours and whenever the spec
  changes. Besides `subject_terms, from_domains, from_addresses, text_terms, regex, has_attachment` (any-of) a spec may
  carry `exclude` (a spec: what matches it is dropped), `all_of` (a list of specs that must all match) and `category`
  (`promo | social | security | dev | other`).
* Criteria: `since_days` (30), `limit` (100), `skip` (Message-IDs already known), `query` (all words must appear),
  `deep` (read everything the hub holds from the start; the watermark is left alone), `fields` (`html`, `images`,
  `headers` or `all` from the hub), `sender_domains`, `skip_own`, `order` (`asc | desc`). Every other key
  (`subject_terms`, `gmail_query`, `attachments_dir`, `account`, ...) goes to the helper unchanged.
* Messages that came from the hub carry `hub_id`; those from the helper do not (so `claim` ignores them).
* `auto_claim=False` is the default on purpose: most apps should claim only what they filed, with a `hoard://` ref.
* The hub's own `GET /api/mail/messages` takes `?fields=html,images,headers|all`; `fam_mail.messages(..., fields=...)` and
  `mailMessages({ fields })` ask for them. Without `fields` the answer is what it always was.

Node: `famMailRouter({ runHelper, helperAvailable, sourceGetter, interest, watermarkGet, watermarkSet, claimKind, page,
maxPages, registerEveryS, sphere, deepUsesHelper, autoClaim })` with `scan, scanEx, commit, claim, status, ensureInterest,
forgetInterest, sourceNow, mode, hubUp, helperUp, watermark, lastSource, lastError, lastMeta` (all async). `runHelper(action,
payload, timeoutMs)` is app-supplied; `spawnHelperRunner({ helperPath, setting, owner, python, envDropPrefixes })` is the
ready-made one. Criteria may be written snake_case or camelCase; the helper always receives snake_case.

## What each app deletes, and what it calls

Vendoring: run `scripts/sync_vendored.py` so the app has `hoard_link/notify_channels.py`, `fam_notify.py`,
`fam_mail.py` and `mail_helper.py` (Node apps: `hoard-link.js` and a copy of `mail_helper.py`). The app's old
`faustus_mail.py` / `faustus_reader.py` is replaced by the vendored `mail_helper.py`; no app keeps a private copy of the
IMAP code.

### Tantalus's Hoard (`tantalus_hoard/notify/__init__.py`, `mail/faustus_reader.py`)

* Delete `xml_escape`, `_http_url`, `_scrub`, `build_toast_ps1`, `Notifier._send_toast`, `_toast_powershell`,
  `_send_ntfy`, `_send_telegram`, `telegram_discover_chat_id`; keep only the channel settings and the severity filter.
  Call `notify_channels.send_toast`, `send_ntfy`, `send_telegram`, `telegram_discover_chat_id`.
* Delete `via_setting`, `_hub_up`, `via_status`, `_send_via_hub`, `hub`: build
  `fam_notify.Router(self.via_setting_raw, self._send_own, "Tantalus's Hoard")`, call `router.send(...)`, show
  `router.via_status()`.
* Delete `faustus_dir`, `faustus_python`, `_faustus_call`, `faustus_status`: use `fam_mail.FaustusHelper(setting)`;
  the e-mail channel becomes `notify_channels.send_via_helper(helper.run, subject, body)`.
* Replace `mail/faustus_reader.py` (headers, scan) with the vendored `mail_helper.py`: `helper.run("headers", ...)` and
  `helper.run("scan", ...)`; the Gmail category and `List-Unsubscribe` come in the record. Where the radar reads mail
  through the hub, use `MailRouter(helper, ...)` and `router.scan(subject_terms=..., sender_domains=...)`.

### Phileas's Hoard (`phileas_hoard/notify/__init__.py`, `mail/faustus_mail.py`)

* Same notifier deletions as Tantalus (toast, ntfy, Telegram, the Faustus finder, the hub switch).
* Delete `mail/faustus_mail.py` (`scan`, `read_one`, `send`, `_search`, `_select_first` ...). Use the vendored helper
  through `FaustusHelper`: `run("scan", {"subject_terms": TRAVEL_TERMS, "gmail_query": ..., "since_days": N})`,
  `run("read", {"message_id": ...})`, `run("send", ...)`. Travel mail is the case for `fields="html"` (reservations with
  JSON-LD): the record's `html` is present exactly when it has markup.
* The trip importer wraps both sources in one `MailRouter`.

### Kafka's Hoard (`kafka_hoard/notify/__init__.py`, `mail/faustus_mail.py`)

* Same notifier deletions; Kafka already has `hub_api`, `via`, `hub_available`, `via_status`, `_send_hub_notify`,
  `_send_own`: replace all of them with one `fam_notify.Router`, whose `own_send` is the old `_send_own` body.
* Delete `mail/faustus_mail.py`. `run("scan", {..., "attachments_dir": DOCS_DIR})` saves PDFs and images as
  `<sha256>.<ext>` exactly as before. The document inbox is a `MailRouter` with `claim_kind="document"` and
  `router.claim(rows, ref="hoard://kafka/doc/<id>")` after each document is filed.

### Ledger's Hoard (`server/notifications.js`, `server/mail-source.js`, `server/mail/faustus_mail.py`)

* Delete `xmlEscape`, `buildToastPs1`, `defaultPowershell`, the temp-file code in `showToast`; import
  `buildToastPs1` / `showToast` from `hoard-link.js` (`showToast` sets `windowsHide`). Keep the toast condensing logic.
* Delete `server/mail/faustus_mail.py` and the spawn code in `createMailSource` (`spawnRunner`, `faustusDir`): use
  `famMailRouter({ runHelper: spawnHelperRunner({ helperPath: HELPER, setting: () => getSetting("mail.faustus_dir"), owner: () => getSetting("mail.faustus_owner") }), sourceGetter: () => getSetting("mail.source", "auto"), interest: ..., watermarkGet, watermarkSet, claimKind: "payment" })`.
  `mail-hub.js` (interest registration, paging, claim) disappears into the router: `await router.scan({ since_days, limit, subject_terms })`,
  then `await router.commit()` and `await router.claim(rows, "hoard://ledger/tx/<id>")`.
* Order and cancellation mail: ask for `fields: "html"` to get the JSON-LD when the mail has it.

### Lumiere's Hoard (`lumiere_hoard/jobevents.py`)

* Has no channels of its own: delete the `notify.via` parsing in `Notifier` (`auto | hub | off`) and the hub-up check;
  `fam_notify.Router(lambda: setting, None, "Lumiere's Hoard").send(...)` does it (`off` stays an app-level check before
  calling). With `own_send=None` an unreachable hub is reported as `{"ok": False, "why": ...}`.

### Prospero's Hoard (`prosperos_hoard/family_settings.py`)

* Same as Lumiere: no channels, only `notify.via`. Keep `FamilySettings`; replace the call site that checks the hub and
  calls `fam_notify.notify` with `Router(...).send(...)`.

### People's Hoard (`server/mailsync.js`, `server/mail-hub.js`, `server/hoard-link.js`)

* Its own vendored `hoard-link.js` copy is replaced by the new one (`mailMessages` gains `fields`).
* Delete the paging loop and the `interest hash` bookkeeping of `mailsync.js` that re-implements registration: build
  `famMailRouter({ interest: (c) => interestSpec(), watermarkGet: () => getSetting(KEYS.since), watermarkSet: (v) => setSetting(KEYS.since, v), sourceGetter: () => "hub", ... })`.
  People never reads the inbox itself, so `runHelper` stays unset and the mode stays `hub`.

### Galton's Hoard (`galton_hoard/util.py`, `watch.py`)

* Delete `util.in_quiet_hours(ts, start, end)` and the hour arithmetic (`local_hour`): call
  `notify_channels.in_quiet_hours(datetime.fromtimestamp(ts), int(start * 60), int(end * 60), allow_high=False)`
  (`start` / `end` are the `watch.quiet_from` / `watch.quiet_to` hours). Galton sends its own notices through
  `fam_notify.Router` when it grows one.

### Faustus (reminders)

* Faustus's reminder delivery (toast, ntfy) uses the same `notify_channels` senders instead of its own PowerShell and
  HTTP code, and `in_quiet_hours(now, start, end, priority=...)` for the person's quiet window. Faustus is the owner of
  the mail helper: `mail_helper.py` stays where it is and is what `FaustusHelper` runs; nothing in Faustus needs to be
  deleted for mail.

## Compatibility notes

* `hoard_link/hub/mail_helper.py` is a shim for the old path (import and script). The gateway, the hub tests and any
  caller of `python hub/mail_helper.py <faustus root>` keep working.
* The hub's mail store gains three columns (`html`, `images`, `headers`) by an additive migration; the default API
  answers are unchanged and the `html` column is only read when `fields` asks for it.
* `fam_mail.register_interest` and the gateway accept `exclude`, `all_of` and `category`; specs without them behave as
  before.
* Not covered: the native `winotify` toast (the old copies did not use it either); the hub-side quiet-hours window in
  `hub/spheres.py` could delegate to `notify_channels.in_quiet_hours` later.
