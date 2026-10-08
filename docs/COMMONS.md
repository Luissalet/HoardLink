# The commons (HoardLink 0.8.1)

The Hoard apps grew by copying each other: a page fetcher here, an ffmpeg finder there, a money parser in
four flavours. Since 0.8 that code lives once, in this repository, and every app vendors it with the rest of
`hoard_link/` (`scripts/sync_vendored.py`). Node apps get the same functions from `js/hoard-commons/`,
vendored as `server/hoard-commons/`.

Two shapes:

* **Library** — pure code an app runs in its own process (`hoard_link.web.urls`, `hoard_link.money`,
  `hoard_link.media.ffmpeg` …). Use it directly.
* **Family service** — a capability with state that should exist once per machine (one throttle per web
  host, one yt-dlp and its updater, one Whisper model in memory, one OCR engine, one embedding model). One
  owner runs it; the others call it through the hub with a `fam_*` client that never raises and says
  `{"ok": false, "error": "hub unreachable"}` when nobody answers, so the app can fall back to the library.

| Capability | Library | Service (owner) | Client |
|---|---|---|---|
| Fetch pages, robots, throttle, block cooldown, cache | `hoard_link.web.fetch` | hub facet `web` | `fam_web` |
| Web search | `hoard_link.web.search` (parsers, fusion) | hub facet `web` | `fam_web.search` |
| Browser (Playwright, one family profile) | `hoard_link.web.browser` | hub facet `web` | `fam_web.fetch(tier="browser")` |
| Download media from a link (yt-dlp, gallery-dl) | `hoard_link.media.bins` | Links Hoard | `fam_media.download` |
| Speech to text | `hoard_link.media.stt` | Funes's Hoard | `fam_media.transcribe` |
| Text to speech | — | Prospero's Hoard | `fam_media.speak` |
| PDF operations, document extraction, OCR | `hoard_link.docs.*` (light readers) | Kafka's Hoard | `fam_docs` |
| Text embeddings | `hoard_link.docs.vecmath` | Borges's Hoard | `fam_embed` |
| Mail, notifications, agenda, references | — | hub facets | `fam_mail`, `fam_notify`, `fam_agenda`, `fam_refs` |

## Rules for code in the commons

The Windows follow-up in 0.8.1 adds per-tool result cap opt-out, explicit direct-route authentication,
http.server and WSGI guard adapters, agenda deduplication by owner, and Windows-safe test helpers.
The Hub's **Services** tab checks the owners' tool catalogues without loading their models.
An optional, initially disabled rule connects completed audio downloads to Funes and a transcript collection in Borges.
See [the service contract](commons/services.md#11-hub-service-discovery-and-media-imports).

The cohesion follow-up adds one Python/Node owner contract, `hub_cohesion`,
checkpointed media imports with explicit resume, provenance and deduplication,
paused work in the family jobs view, and authenticated text handoffs through the
Hub. See [responsibility boundaries and remaining gaps](commons/cohesion.md).

1. **Importing never needs more than the standard library.** Optional dependencies (`httpx`, `bs4`,
   `playwright`, `PIL`, `numpy`, `faster_whisper` …) are imported inside the functions that need them, and a
   missing one raises `hoard_link.errors.missing_dependency(package, feature)` (an `Unavailable`), never `ImportError` at import
   time. (`httpx` is the library's only declared dependency, but `family`, `fam_*` and the commons must work
   without it.)
2. **One behaviour, two languages.** When a Node app needs the same function, its twin goes in
   `js/hoard-commons/<module>.js` (ESM, Node 18+, no npm dependencies), and both are tested against the same
   vectors in `tests/vectors/<module>.json` (`tests/commons/jsrun.py` runs the Node side).
3. **Windows first.** Paths with spaces and accents, `CREATE_NO_WINDOW` for every subprocess, retries on
   `PermissionError` when replacing files, `.exe` lookup, no POSIX-only calls without a fallback.
4. **No surprises for callers.** Functions document what they return on failure; network code takes a
   timeout and a size cap; nothing logs secrets.
5. **English** in code, comments and docstrings. No product names of other companies' assistants in code or
   commit messages.

## Modules

Each area has its own page with the full API, the copies it replaces (with paths) and a migration note per app.

| Area | Python | Node (`js/hoard-commons/`) | Page |
|---|---|---|---|
| Web | `web.urls`, `web.safety`, `web.blocks`, `web.robots`, `web.htmltext`, `web.meta`, `web.feeds`, `web.watch`, `web.fetch`, `web.browser`, `web.search` | `web.js` | [commons/web.md](commons/web.md) |
| Media | `proc`, `media.bins`, `media.ffmpeg`, `media.subs`, `media.stt` | `media.js` | [commons/media.md](commons/media.md) |
| Documents and search | `docs.textsearch`, `docs.chunking`, `docs.sniff`, `docs.pageranges`, `docs.vecmath`, `docs.citations`, `docs.textclean`, `docs.readers_lite`, `docs.imaging`, `docs.archives` | `docs.js` | [commons/docs.md](commons/docs.md) |
| Money, dates and personal data | `text`, `money`, `dates`, `idcheck`, `tracking`, `merchants`, `ics`, `bizdays` | `text.js`, `money.js`, `dates.js`, `idcheck.js`, `tracking.js`, `merchants.js`, `ics.js` | [commons/commerce.md](commons/commerce.md) |
| Notifications and mail | `notify_channels`, `fam_notify.Router`, `mail_helper`, `fam_mail.FaustusHelper`, `fam_mail.MailRouter` | in `hoard-link.js` | [commons/notify-mail.md](commons/notify-mail.md) |
| App plumbing | `atomic`, `tokens`, `ids`, `sqlkit`, `paths`, `net`, `appconfig`, `lanes`, `waiting`, `guard`, `agentkit`, `service`, `bridge` | `server.js`, `express.js` | [commons/plumbing.md](commons/plumbing.md) |

Family services (who owns what, the tools, arguments, results and polling rules): [commons/services.md](commons/services.md).
Atlas shared projects through the Hub: [commons/workspace.md](commons/workspace.md).
The web service in the hub: [commons/web.md](commons/web.md#family-service-hub-facet-web).

The shared test vectors are in `tests/vectors/`; `tests/commons/` runs every vector against the Python module and,
when `node` is installed, against its Node twin.
