# Family services: media, speech, documents, embeddings

Five capabilities have state that should exist once per machine: one yt-dlp and its updater, one Whisper model in memory, one set of
voices, one OCR engine and PDF workshop, one embedding model. Each has **one owner app**; every other app calls it through the hub with a
`fam_*` client that never raises and says `{"ok": false, "error": "hub unreachable", ...}` when nobody answers. This page is **the contract**:
the owner apps implement exactly these tools, the clients (`hoard_link/fam_media.py`, `fam_docs.py`, `fam_embed.py`, `js/hoard-commons/fam-services.js`)
call exactly these tools, and the migration table at the end says which app switches what. Rules of the commons: `docs/COMMONS.md`.

| Service | Owner (hub app id) | Python client | Node twin (`fam-services.js`) |
|---|---|---|---|
| Media download from a link | `links` (Links Hoard, Node) | `fam_media.download`, `status`, `cancel`, `info`, `subtitles`, `audio_for_asr`, `tools` | `mediaDownload`, `mediaStatus`, `mediaCancel`, `mediaInfo`, `mediaSubtitles`, `mediaAudioForAsr`, `mediaTools` |
| Speech to text | `funes` (Funes's Hoard, Python) | `fam_media.transcribe`, `transcribe_status`, `transcribe_cancel` | `transcribe`, `transcribeStatus`, `transcribeCancel` |
| Text to speech | `prospero` (Prospero's Hoard, Python) | `fam_media.speak`, `speak_bytes` | `speak` |
| PDF operations, extraction, OCR | `kafka` (Kafka's Hoard, Python) | `fam_docs.*` | `pdf*`, `imagesToPdf`, `imagesCompress`, `docsExtract`, `docsOcr*` |
| Text embeddings | `borges` (Borges's Hoard, Python) | `fam_embed.status`, `embed_texts`, `embed_query` | `embedStatus`, `embedTexts`, `embedQuery` |

`available(service)` (`fam_media.available("media" | "stt" | "tts")`, `fam_docs.available()`, `fam_embed.available()`; Node: `serviceAvailable`) is
`GET /api/apps/<owner>` -> `state == "running"`, cached 30 s. It is for settings pages and routing hints; the calls themselves do not need it
(a call to an app that is down answers at once).

---

## 1. How a call works

```
app --fam_*--> hub  POST /api/apps/<owner>/call  {tool, arguments, timeout_s}   (bearer: the calling app's own token)
                    --> owner  POST /api/agent/call  {name, arguments}           (bearer: the owner's own token)
```

* `family.configure(app_id, data_dir)` (or `install_fastapi`) first: the token file is how the hub knows who is calling. A token the hub
  refuses is `kind: "auth"` and is **never** papered over by a local fallback (it is a configuration bug).
* **Envelope the hub returns** (`hoard_link/hub/contract.py:call_app`): `{ok, app, tool, status, contract, ms, result | error}`.

  | Situation | HTTP | Body |
  |---|---|---|
  | the tool ran | 200 | `ok: true`, `status: 200`, `result: <the tool's own value>` |
  | the tool refused or failed (bad path, missing file...) | the tool's status (400, 404, 409, 503...) | `ok: false`, `status`, `error: "<message>"` |
  | the owner app is not running | 502 | `ok: false`, `status: null`, `error: "not reachable"` |
  | the owner is not in the hub | 404 | `ok: false`, `error: "unknown app"` |
  | the owner has no such tool | 404 | `ok: false`, `status: 404`, `error: "unknown tool: <name>"` |
  | the hub itself does not answer | - | (no body: the client says `hub unreachable`) |

  `timeout_s` is clamped by the hub to **1..900 s**. A tool's own `{"ok": false, "error": ...}` inside an HTTP 200 is treated as a failure by the
  clients **unless the value has a `status` field** (a job view says `ok: false` while it runs).
* **Owner tools must be listed in the owner's `/api/agent/tools`** and routed by `/api/agent/call` (`family.install_fastapi` does both for FastAPI
  apps; Links has `server/agent-tools.js`). Tool errors are HTTP 4xx with `{"ok": false, "error": "<message in the user's language>"}`: 400 bad
  arguments, 404 something asked for does not exist (a job id, a subtitle language), 409 wrong state, 503 not ready.
* **Paths, never bytes.** Files travel as absolute paths on the shared disk; the owner reads and writes them. No base64 over the service proxy.
  Relative paths are made absolute by the client (`os.path.abspath` / `path.resolve`).
  Authenticated proxy/agent calls accept at most 4 MiB of JSON (enough for chapter
  handoffs); ordinary metadata routes retain 256 KiB. Authentication and size are
  checked before reading larger bodies. Model chat has its separate 12 MiB limit.
  Family API clients never follow redirects to another service with credentials.

### Failure vocabulary (all clients, both languages)

Every failure is `{"ok": false, "error": "<text>", "via": "<owner id or local>", "kind": "<kind>"}` (+ `detail` and sometimes `result`/`job_id`).

| `kind` | Meaning | Falls back to the local code? |
|---|---|---|
| `hub_down` | nothing answered at the hub (`error` is exactly `"hub unreachable"`) | yes |
| `app_down` | the hub answered, the owner is not running (`"<owner> unreachable"`) | yes |
| `app_missing` | the hub does not know the owner | yes |
| `tool_missing` | an older owner without the tool (`"<owner> has no tool <t> (update the app)"`) | yes |
| `timeout` | the caller's `timeout_s` ran out; a job is **still running** (`job_id` / `id`, `still_running: true`) | no |
| `auth` | the hub refused this app's token | no |
| `tool_error` | the tool ran and said no (the owner's message is `error`) | no |
| `client_error` | bad arguments caught by the client, or an unexpected exception inside it | no |

When a fallback is tried and fails too, `via` is `"local"`, `error` carries both reasons (`"hub unreachable; local fallback failed: ..."`) and
`hub_error` the first. `fam_media.available`/`fam_docs.available`/`fam_embed.available` answer the routing question without a call.

### The waiting rule (long work)

MCP clients cut a call at about 180 s, and the apps used to have every cap between 120 s and 7200 s. One rule (`hoard_link/waiting.py`,
`MAX_WAIT_S = 150`): a tool that can take long accepts `wait_s` (0..150, default as noted), runs as a **job** and answers `{job_id | id, status}`;
if the job is done within `wait_s` the answer already is the result. The status tool takes the same `wait_s`. Clients: the first call waits at most
150 s, then the status tool is polled with 150 s waits until the caller's `timeout_s`; past it the answer is `kind: "timeout"` with the id and the
job **keeps running** (it is never cancelled by the client). A call made by an app through the hub may use up to 900 s of `timeout_s` in total.
A job tool that finishes synchronously may omit `status`: no `status` means done.

Job states: `queued`, `running` (also `downloading`, `processing` for downloads) while active; `done`, `error`, `cancelled`, `interrupted`, `failed`
when over (`waiting.DONE_STATES`). `progress` is 0..1 (a value above 1 is read as a percentage).

---

## 2. Media download: owner `links` (Links Hoard)

Code: `server/agent-tools.js` (`TOOLS`, `agentView`), `server/media.js` (`startDownload`, `waitForDownload`, `probeUrl`, `toolsStatus`).
The queue has concurrency one: calls from several apps wait their turn (the first call can answer `queued`).

**The download view** (`agentView`, already returned by `media_download` / `media_status` / `media_cancel` / `media_retry`):
`{id, ok, status, platform, format, kind, title, uploader, dir, files: [{path, name, size, kind}], total_bytes, link_id, progress, speed, eta, detail,
error, message, existing}`. `ok` is true only when `status == "done"` and `files` is not empty. `status`: `queued | downloading | processing`
(active) `| done | failed | cancelled`. The client keeps the view and adds `path` (first file), `via: "links"`, renames Links' `kind` (video/audio/image)
to `media_kind` (`kind` is the failure vocabulary) and sets `still_running: true` while active.

| Tool | Exists | Arguments | Result |
|---|---|---|---|
| `media_download` | yes; **4 arguments to add** | `url`, `format` (`auto|video|audio|image`, default `auto`), `quality` (`best` or a height), `dir`, `save_link` (default **true** in Links; the client sends `false` unless asked), `playlist`, `max_items`, `cookies_browser`, `wait` (default true), `timeout_s` (5..3600, default 150). **Add:** `sections` (`[[start_s, end_s], ...]`, at most 10, `0 <= start < end`: yt-dlp `--download-sections "*s-e"` + `--force-keyframes-at-cuts`; with several sections the files are `<title> [part N]`), `max_duration_s` (refuse a longer video before downloading: `status: failed`, `error: "the video lasts 2h10 (limit 20 min)"`), `max_height` (cap the resolution; applies on top of `quality`, the lower wins), `dest_dir` (alias of `dir`: the client sends both) | the download view |
| `media_status` | yes | `id` (+ `wait_s` 0..150), or no `id` for the list | the view with `detail: true` (`url`, `upload_date`, `duration`, `description`, `created_at`, `finished_at`) |
| `media_cancel` | yes | `id` | the view (`status: cancelled`) |
| `media_retry`, `media_delete`, `media_probe` | yes | (not wrapped by the clients; an app may `family.call` them) | |
| `media_tools` | yes | `update` (default false), `tools` | `{yt-dlp/gallery-dl/ffmpeg: {found, path, version, how}, settings, update?}` |
| `media_info` | **add** (`media.js:probeUrl` plus two fields) | `url`, `playlist` (false), `cookies_browser` | `{url, platform, title, description (<= 2000), uploader, duration (s or null), upload_date, thumbnail (URL), subtitle_langs: ["es", "en"] (manual and automatic, deduplicated), heights, is_playlist, entries?, photo_post, id, extractor, is_live}`. Errors: 400 with the `MediaError` message. Internal timeout 90 s. `media_probe` stays as an alias (the client falls back to it with `partial: true`). |
| `media_subtitles` | **add** | `url`, `langs` (default `["es", "en"]`, first available wins, manual before automatic), `cookies_browser` | `{text, lang, source: "manual"|"auto", cues: [{start_s, end_s, text}]}`: yt-dlp `--skip-download --write-subs --write-auto-subs --sub-langs ... --sub-format vtt` into a temp folder, parsed with `parseVtt` / `subtitleText` of `hoard-commons/media.js` (rolling auto-captions collapsed). 404 `no subtitles in es, en (available: ...)` when none; the folder is removed. |
| `media_audio_for_asr` | **add** | `url`, `sections`, `max_duration_s`, `wait` (true), `timeout_s` (5..3600, default 150) | **the download view** of a download whose file is a **mono 16 kHz PCM WAV** (`ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le`) under `<downloads>/asr/` (or `$HOARD_HOME/tmp/asr`), `format: "audio"`, `save_link` never; `files[0].path` is the WAV. A long video is polled with `media_status` like any download. (An owner answering just `{path}` is accepted.) |

Client behaviour: `download(url, ..., wait=True, timeout_s=150)` calls `media_download` with `wait: true, timeout_s: <= 150`, then polls
`media_status(id, wait_s <= 150)` until done or `timeout_s`. `wait=False` returns the queued view at once. `info()` falls back to `media_probe`,
`audio_for_asr()` to an MP3 download (`converted: False`) when Links is older. Timeouts the client gives the hub: wait chunk + 20 s.

## 3. Speech to text: owner `funes` (Funes's Hoard)

Code: `funes_hoard/audio_memory/agent_tools.py` (a `Tool(...)` + a pydantic input model each, like `ImportFileArgs`/`run_import_file`),
route list in `funes_hoard/api.py` (`for _tool in ("scribe_minutes", "minutes_get", "scribe_import_file")`), `mcp_server.py` (`_audio_call`),
the transcriber in `audio_memory/transcribe/whisper.py` (to be `hoard_link.media.stt.Transcriber`: one model in memory, GPU lease, hallucination
filter). **Stateless**: no session, no row in Funes's database, no copy of the file; the job lives in memory (kept 1 h, newest 50).

| Tool | Exists | Arguments | Result |
|---|---|---|---|
| `scribe_import_file` | yes (CookHoard `importers/video.mjs` calls it today) | `path`, `title`, `kind`, `language`, `wait_s` (0..3600) | `{session, status, transcript_text, segments, next_from_s}` - creates a **session** in Funes's memory. Stays for the person's own recordings; apps that only want the text use `transcribe_file`. |
| `transcribe_file` | **add** | `path` (absolute; same checks as `run_import_file`: exists, regular file, extension in `ALLOWED_EXT`, 1 byte..2 GiB), `language` (`"auto"` or a code, default `auto`), `model` (Whisper size or `null` = Funes's configured one), `word_timestamps` (true), `initial_prompt` (`""`, <= 1000 chars), `vad` (true), `wait_s` (0..150, default 0) | the job: `{job_id, status, progress, language, language_probability, duration_s, text, segments, model, device, note, stats, error?}`. While `queued`/`running` only `job_id`, `status`, `progress` are guaranteed; when `done` all fields. `segments`: `[{start_s, end_s, text, words: [{start_s, end_s, word, p}], avg_logprob?, no_speech_prob?}]` - the output of `Transcriber.transcribe(...).as_dict()` (hallucination-filtered). No speaker labels (diarisation is a session feature). Errors: 400 bad path / extension / empty / too large, 503 `no_model` when faster-whisper cannot load. |
| `transcribe_status` | **add** | `job_id`, `wait_s` (0..150) | same job view; 404 `no such job: <id>` (finished jobs are kept 1 h) |
| `transcribe_cancel` | **add** | `job_id` | `{job_id, status: "cancelled"}`; idempotent (a finished job is left as it is and its own status returned) |

The three new tools are registered like `scribe_import_file`: `/api/agent/<name>`, in `/api/agent/tools`, allow-listed in `tests/test_security.py`.
The job runs on the lane the importer already uses (one transcription at a time, one model); the GPU lease is the Transcriber's.

Client behaviour: `transcribe(path, language="auto", model=None, word_timestamps=True, initial_prompt="", timeout_s=600, local_fallback=True, progress=None)`:
`transcribe_file(wait_s: 150)`, then `transcribe_status` until `timeout_s`; `progress(fraction)` per poll. The result is normalised to
`hoard_link.media.stt.Transcript.as_dict()` (`start`/`end` -> `start_s`/`end_s`, `probability` -> `p`, `text` rebuilt from the segments when absent) plus
`ok`, `via` (`"funes"` | `"local"`) and `job_id`. A job Funes ran and that failed is **not** repeated locally; only `hub_down`, `app_down`, `app_missing`
and `tool_missing` fall back (to `Transcriber(size=model or $HOARD_WHISPER_MODEL or "small")`, kept loaded between calls).

## 4. Text to speech: owner `prospero` (Prospero's Hoard)

Code: `prosperos_hoard/family_api.py` (`TtsBody`, `agent_voice_tts`), `family_tools.py:tts`, `mcp_server.py:voice_tts`. Synchronous (Piper is fast).

| Tool | Exists | Arguments | Result |
|---|---|---|---|
| `voice_tts` | yes; **2 arguments to add** | `text` (<= 20000 chars), `voice` (a library voice id or name, or an engine's own voice id; empty = best installed engine), `lang` (`es`, `en`...). **Add:** `engine` (an engine id such as `piper`; 400 `unknown_engine` when it is not installed; with a library `voice` the voice's own engine wins), `speed` (0.5..2.0, 1.0 normal; passed to `synthesize(speed=)`) | `{ok, path, engine_id, bytes}`: a WAV under `<data>/exports/tts/` |
| `voice_speak`, `voice_audiobook`, `voice_transcribe`, `voice_list` | yes | (not wrapped: `voice_speak` saves a project asset, `voice_audiobook` is a job) | |

Errors: 400 `empty_text`, `text_too_long`, `tts_not_installed` (no engine), `unknown_engine`. Prospero's own `voice_transcribe` stays for its studio; **speech to
text for other apps is Funes's**.

Client behaviour: `speak(text, *, voice, engine, lang, speed, timeout_s=300, local_fallback=True, link=None, out_dir=None) -> {ok, path, bytes, engine_id, via}`;
`speak_bytes(...)` reads the file. Fallback when Prospero cannot be reached: `link.tts(text, voice)` of a `hoard_link.Link` (the app's own, or one built for the
app; needs `httpx`), written under `out_dir` or `$HOARD_HOME/tmp/tts` (`temp: True`; `engine`, `lang` and `speed` are ignored there). More than 20000 characters is
`client_error` (split the text; narration of long texts is `voice_audiobook`).

## 5. PDF operations, extraction and OCR: owner `kafka` (Kafka's Hoard)

Code: `kafka_hoard/agent_tools.py` (`TOOLS`, `_Out`, `run_pdf_*`, `_ws_done`), `kafka_hoard/workshop/` (the PDF workshop), `kafka_hoard/ocr.py` (`Ocr`, rapidocr),
`kafka_hoard/readers.py` and `kafka_hoard/extract/` (the readers, to be `hoard_link.docs.readers_lite` plus the OCR pass).

**Workshop tools (all exist; the client uses Kafka's own names and arguments).** A `file`/`files`/`images`/`sources` argument is an absolute path, a Kafka document id
(`d_...`), or (images) a folder. Results are never overwritten: a taken name becomes `name (2)`. Every result: `{ok, operation, outputs: [{path, ...}], output
(the one path when there is one), options, notes, filed?, doc_ids?}`; the client adds `paths` (all output paths) and `via: "kafka"`.

| Client (`fam_docs` / Node) | Kafka tool | Arguments (Kafka's names) |
|---|---|---|
| `pdf_info(file, password)` / `pdfInfo` | `pdf_info` | `file`, `password` -> `{ok, pages, sizes, encrypted, metadata, has_text, ...}` |
| `pdf_merge(files, ranges, password, output, out_dir, file_result)` / `pdfMerge` | `pdf_merge` | `files` (2..300), `ranges` (same length: `"1-3"`, `""` = all), `password`, `output`, `out_dir`, `file_result` |
| `pdf_split(file, mode, ranges, every, password, out_dir, file_result)` / `pdfSplit` | `pdf_split` | `mode` `pages|ranges|every`, `ranges` (`"1-3,4-6,7-"`), `every` |
| `pdf_pages(file, action, pages, degrees, order, password, output, out_dir)` / `pdfPages` | `pdf_pages` | `action` `extract|delete|rotate|reorder`, `pages` (`"1-3,5,8-"`, `last`, `odd`), `degrees` (90/180/270), `order` (`"3,1,2"`, `reverse`) |
| `pdf_compress(file, preset, target_mb, engine, ...)` / `pdfCompress` | `pdf_compress` | `preset` `screen|ebook|printer|prepress`, `target_mb`, `engine` `auto|ghostscript|pypdf` |
| `pdf_watermark(file, text, opacity, angle, font_size, color, pages, ...)` / `pdfWatermark` | `pdf_watermark` | as named |
| `pdf_protect(file, action, password, owner_password, current_password, allow_*, ...)` / `pdfProtect` | `pdf_protect` | `action` `protect|unprotect`; passwords are never echoed |
| `pdf_metadata_set(file, title, author, subject, keywords, ...)` / `pdfMetadataSet` | `pdf_metadata_set` | `""` clears, absent keeps |
| `pdf_to_images(file, pages, format, dpi, quality, password, out_dir)` / `pdfToImages` | `pdf_to_images` | `format` `png|jpg`, `dpi` 36..600 |
| `images_to_pdf(images, page_size, margin_mm, orientation, output, out_dir, file_result)` / `imagesToPdf` | `pdf_from_images` | `page_size` `A4|Letter|fit` |
| `pdf_from_office(file, engine, ...)` / `pdfFromOffice` | `pdf_from_office` | `.docx/.doc/.odt/.rtf` |
| `images_compress(sources, limit_mb|limit_kb, recursive, lossless_only, skip_small, out_dir, time_limit_s)` / `imagesCompress` | `images_compress` | stops after `time_limit_s` (default 70) and reports `pending`; call again with the same `out_dir` |
| (not wrapped) | `doc_add_file`, `doc_get`, `extract_preview` | CookHoard `tickets.mjs` already calls `doc_add_file`/`doc_get` with `family.call`; they **file a document** in Kafka, which `doc_extract` does not |

Default per-call hub timeouts: 60 s for `pdf_info`, 120 s metadata, 300 s most operations, 600 s compress and render.

**Extraction and OCR (to add; stateless, nothing is filed in Kafka).**

| Tool | Arguments | Result |
|---|---|---|
| `doc_extract` | `path` (absolute), `ocr` (`auto` = only pages without a text layer, `off`, `force` = every page; default `auto`), `max_pages` (default 400), `lang` (`es`), `wait_s` (0..150, default 150) | the `readers_lite.read_any` shape: `{kind, title, text, units: [{kind, number, title, text}], needs_ocr, notes, mime, error?}` plus `pages_ocr` (pages recognised), plus `job_id`/`status`/`pages_done`/`pages_total` while it runs. `kind`: `pdf|docx|odt|pptx|xlsx|epub|rtf|html|eml|text|image`. `needs_ocr` stays true only if a page is **still** unreadable (OCR off or unavailable). A damaged or unsupported file: `error` set (HTTP 200) - the client turns it into `ok: false`. |
| `ocr_image` | `path`, `lang` (`es`), `blocks` (false) | `{text, blocks: [{text, box: [[x, y] x4], score}], backend}`; `blocks` only when asked. 503 `ocr_unavailable` (with the install hint of `Ocr.available()`) when the engine is not there. |
| `ocr_pdf` | `path`, `pages` (`"1-5,9"`, default the first `max_pages`), `dpi` (200), `lang`, `max_pages` (40), `wait_s` (0..150, default 150) | the job: `{job_id, status, text, pages: [{page, text}], pages_done, pages_total, backend}` |
| `ocr_status` | `job_id` (`""`), `wait_s` | no `job_id`: the engine, `{available, backend, languages, reason?}` (`Ocr.available()`); with `job_id`: that job - a `doc_extract` or `ocr_pdf` one - in its result shape (404 `no such job`) |

Client behaviour: `extract(path, ocr="auto", max_pages=400, lang="es", local_fallback=True, timeout_s=900)` follows the job with `ocr_status(job_id, wait_s)`.
Result: the `read_any` shape plus `ok`, `via` (`"kafka"` | `"local"`), `pages_ocr`. With `hub_down | app_down | app_missing | tool_missing` and `local_fallback`:
`hoard_link.docs.readers_lite.read_any(name, bytes)` on the file (<= 200 MB; everything but OCR; a scan comes back `needs_ocr: True` with the note
`"OCR was not run: Kafka's Hoard is not available"`). `ocr_image` and `ocr_pdf` have no local fallback (the engine is Kafka's).

## 6. Text embeddings: owner `borges` (Borges's Hoard)

Code: `borges/embedder.py` (`Embedder.embed_documents` / `embed_query`, `FastembedEmbedder`, `make_embedder`; backends `fastembed`, `fake` for tests, `none`),
`borges/agent_tools.py` (`Tool(...)`), `borges/services.py`. Library search (`library_search` and friends) already exists and is **not** part of this contract.

| Tool | Arguments | Result |
|---|---|---|
| `embed_texts` | `texts` (list of 1..512 strings, each cut to the model's limit), `kind` (`document` | `query`, default `document`: `embed_documents` or `embed_query` per text), `normalize` (true) | `{model, dim, vectors: [[float]], normalized: bool}`; vectors in the order of `texts`. When the model is still loading or downloading the tool waits for `ensure_loaded()` up to 100 s, then 503 `embedder_loading`. 400 `embeddings_disabled` when the backend is `none`; 400 `too_many_texts`. |
| `embed_status` | - | `Embedder.status()` plus `ready`: `{backend, model, dim, state: disabled|loading|downloading|ready|error, error, load_seconds, ready}` |

**Vector spaces.** Two models give incomparable vectors. `model` and `dim` identify the space: a caller stores both next to its vectors and re-embeds when they change
(`embed_status()` tells before indexing). The client refuses a call where the model changes between batches (never a mix), and the Link fallback answers with
`via: "local"` and **its own** `model` (it is a different model: an app that indexed with Borges must not mix them).

Client behaviour: `embed_texts(texts, kind="document", normalize=True, batch=64, timeout_s=120, local_fallback=True, link=None)` sends batches of `batch` (<= 512) and
checks count and shape; `embed_query(text)` is `kind="query"` of one. Fallback (only when the *first* batch finds nobody: `hub_down`, `app_down`, `app_missing`, `tool_missing`): `link.embed(texts)` of a `hoard_link.Link`,
normalised with `hoard_link.docs.vecmath.normalize` when asked (`kind` is ignored there: no query/document prefixes). A failure after the first batch is an error.
Store vectors with `vecmath.pack_vec`, search with `vecmath.topk`.

---

## 7. Python and Node API at a glance

```python
from hoard_link import family, fam_media, fam_docs, fam_embed
family.configure("lumiere", DATA_DIR)

fam_media.download(url, *, format="auto", quality="best", dest_dir=None, sections=None, max_duration_s=None, max_height=None,
                   save_link=False, playlist=False, max_items=50, cookies="auto", wait=True, timeout_s=150) -> view
fam_media.status(id, wait_s=0) ; fam_media.cancel(id) ; fam_media.info(url) ; fam_media.subtitles(url, langs=("es", "en"))
fam_media.audio_for_asr(url, *, sections=None, timeout_s=150) ; fam_media.tools(update=False)
fam_media.transcribe(path, *, language="auto", model=None, word_timestamps=True, initial_prompt="", timeout_s=600, local_fallback=True, progress=None, vad=True)
fam_media.transcribe_status(job_id, wait_s=0) ; fam_media.transcribe_cancel(job_id)
fam_media.speak(text, *, voice=None, engine=None, lang=None, speed=None, timeout_s=300, local_fallback=True, link=None, out_dir=None)
fam_media.speak_bytes(text, **same) ; fam_media.available("media" | "stt" | "tts")

fam_docs.pdf_info / pdf_merge / pdf_split / pdf_pages / pdf_compress / pdf_watermark / pdf_protect / pdf_metadata_set / pdf_to_images /
         images_to_pdf / pdf_from_office / images_compress          # Kafka's arguments, keyword-only after the file(s)
fam_docs.extract(path, *, ocr="auto", max_pages=400, lang="es", local_fallback=True, timeout_s=900)
fam_docs.ocr_image(path, *, lang="es", blocks=False) ; fam_docs.ocr_pdf(path, *, pages=None, dpi=200, lang="es", max_pages=40, timeout_s=900)
fam_docs.ocr_status(job_id="", wait_s=0) ; fam_docs.available()

fam_embed.status() ; fam_embed.embed_texts(texts, *, kind="document", normalize=True, batch=64, timeout_s=120, local_fallback=True, link=None)
fam_embed.embed_query(text, *, normalize=True, timeout_s=60) ; fam_embed.available()
```

```js
import { mediaDownload, transcribe, docsExtract, embedTexts, serviceAvailable } from "./hoard-commons/fam-services.js";
const got = await mediaDownload(url, { format: "audio", sections: [[30, 75]], destDir, timeoutS: 150 });
const text = await transcribe(got.path, { language: "auto", progress: (f) => ... });   // no local fallbacks in Node: kind "hub_down" etc.
```

Options in Node are one trailing camelCase object (`destDir`, `maxDurationS`, `timeoutS` in seconds). `fam-services.js` imports `call` and `status` from `../hoard-link.js`
(the vendored layout: `server/hoard-link.js` + `server/hoard-commons/fam-services.js`).

Vendoring: the Python modules travel with `hoard_link/` (`scripts/sync_vendored.py`; `_famsvc.py` is their shared plumbing), the Node twin with `js/hoard-commons/`.

## 8. Who switches to the clients (and what it replaces)

Order of work: first the **owner** tools of section 2-6 that are marked *add*, then the callers. Until an owner has them, a client says `tool_missing` and the app
keeps (or falls back to) its own code, so a caller can adopt its client first and nothing breaks.

| App | Replace (file: function) | With |
|---|---|---|
| **Prospero** | `prosperos_hoard/media_download.py: download` + `download_job` (the yt-dlp call, `check_url`, MAX_DURATION_S / MAX_HEIGHT) - its yt-dlp **and** the optional dependency | `fam_media.download(url, format="audio" if audio_only else "video", sections=[[start_s, end_s]], max_duration_s=1200, max_height=1080, dest_dir=<tmp>)`, then `engine.import_asset(...)` on `result["path"]`; keep `check_url` only for the message |
| **Prospero** | `voice_engines.py: FasterWhisperEngine` for the *other apps'* needs - nothing changes in Prospero's own studio, but it stops being what apps ask; and its `voice_transcribe` agent tool stays | other apps use `fam_media.transcribe` (Funes) |
| **CookHoard** | `server/media.mjs: fetchInfo`, `fetchSubtitles`, `fetchAudio` (and its yt-dlp/ffmpeg discovery: `detectTools`, `commandFor`, `run`, `cookieArgs`, `explainYtdlp`); `server/importers/video.mjs: transcribe` (the `callApp('funes','scribe_import_file', ...)` that makes a session in Funes) | `mediaInfo(url)`, `mediaSubtitles(url, {langs})`, `mediaAudioForAsr(url)` + `transcribe(path)` (no session, no `CookHoard:` titles in the person's recordings). `fetchFrames` stays in Cook (frames are not a service yet) but gets its video through `mediaDownload(url, {format: "video", maxHeight: 480})` |
| **Writers** (Electron) | `electron/main.ts` `downloadToFile` handler and the embedded yt-dlp / "Universal video downloader" Flask backend behind `src/services/mediaDownloader.ts` (`checkHealth`, `detectPlatform`, `downloadInBrowser`) | `mediaDownload(url, {format, destDir: app.getPath("downloads")})` in the main process (`link` hub client = `server/hoard-link.js` equivalent); keep the embedded path as the fallback when `kind` is `hub_down | app_down | tool_missing`; `serviceAvailable("media")` for the health badge |
| **Hypatia** | `hypatia/voice.py: speak` (+ `prospero_url`, `prospero_available`, `reset_cache`, the direct `POST /api/voice/speak` with `httpx`) | `fam_media.speak(text, voice=voice_ref, engine=engine)` -> `result["path"]`; `fam_media.available("tts")` for `prospero_available`; `HYPATIA_PROSPERO_URL` stops mattering (the hub finds Prospero) |
| **Hypatia** | `hypatia/notebook/llm.py: embed` (the `link.resolve("embeddings")` + `link.embed(batch)` loop) and the `embed_source` vectors | `fam_embed.embed_texts(texts, kind="document")` / `embed_query`; store `model` and `dim` with the vectors; its Link stays as the fallback (`link=` argument) |
| **Hypatia** | `hypatia/notebook/sources.py: index_source` ("¿PDF escaneado sin OCR?" at the extraction step) | `fam_docs.extract(path, ocr="auto")` |
| **Lumiere** | `lumiere_hoard/analysis/speech.py: transcribe`, `_run`, `_load_audio`, `engine_status`, `cuda_dll_dirs` (keep `find_fillers`, `cut_ranges_for_words`) | `fam_media.transcribe(path, language=..., word_timestamps=True, progress=...)` with the result's `segments[*].words`; the `TranscriberUnavailable` error maps from `kind` |
| **Prospero** `FasterWhisperEngine`, **Faustus** audio / video loaders (the faster-whisper loaders in its `src/` that load their own model) | each one's own Whisper loading | `fam_media.transcribe` (Faustus keeps its own model when it runs the local `Transcriber` itself, e.g. while serving the person's microphone) |
| **Borges** (`borges/extract/pdf.py`: `needs_ocr = ...` at line 155), **Cicero** (`cicero_hoard/extract.py: extract_bytes`, the "A scanned PDF needs OCR first." hint), **JobHunter** (`server/context.js: readSource`, "PDF sin texto extraíble... no se ha ejecutado OCR"), **Writers** (`projectTools.ts` PDF imports), **Hypatia** (above) | "no text layer -> give up / ask the person for a transcription" | `fam_docs.extract(path, ocr="auto")` / `docsExtract(path)`: scans are read; `needs_ocr` only when OCR is genuinely off. JobHunter and Writers call `docsExtract` (Node). Keep each app's own reader as the `local_fallback` (Python apps get `readers_lite` for free) |
| **Echo** | `echo/ocr.py: read_text` (tesseract via `shutil.which`, `ECHO_OCR_LANG`, 12 s timeout, 20000-char cut) | `fam_docs.ocr_image(path, lang=...)["text"]` (it passes a **path**: Echo writes the screenshot to its data folder first, or keeps its tesseract call as the fallback on `kind in (hub_down, app_down, tool_missing)`) |
| **Vitruvius** | `vitruvius_hoard/dense.py: Embedder` / `FastembedEmbedder` / `DenseIndex.build` (its own fastembed model and download) | `fam_embed.embed_texts(chunks, kind="document")` / `embed_query` (the index table keeps `model` and `dim` per vector; a change of model re-embeds); the in-process `FastembedEmbedder` stays as the offline fallback |
| **Babel** | `babels_hoard/backend.py: link.resolve("embeddings")` + `link.embed(texts)` (line ~250, the re-ranking) | `fam_embed.embed_texts(texts, kind="document")` / `embed_query` (the Link remains the `link=` fallback) |
| **Kafka** | its PDF workshop and OCR become the owner side | adds `doc_extract`, `ocr_image`, `ocr_pdf`, `ocr_status` (section 5); its own pipeline calls the same functions in-process |
| **Funes** | adds `transcribe_file` / `transcribe_status` / `transcribe_cancel` (section 3) and moves its whisper loader to `hoard_link.media.stt.Transcriber` | |
| **Links** | adds the media tool arguments and `media_info`, `media_subtitles`, `media_audio_for_asr` (section 2) | |
| **Borges** | adds `embed_texts`, `embed_status` (section 6) | |
| **Prospero** | adds `engine` and `speed` to `voice_tts` (section 4) | |

## 9. Owner checklist (what to build, where, how to test)

* **Links** (`server/agent-tools.js`, `server/media.js`): `sections`, `max_duration_s`, `max_height`, `dest_dir` on `media_download` (and in `startInput`); `media_info`
  (= `probeUrl` + `thumbnail`, `subtitle_langs`, `id`, `extractor`, `is_live`); `media_subtitles`; `media_audio_for_asr`. Tests with a fake yt-dlp (`resolveTool` accepts `node:/x/fake.js`).
* **Funes** (`audio_memory/agent_tools.py`, `api.py`, `mcp_server.py`, `tests/test_security.py`): the three `transcribe_*` tools, an in-memory job table, the allow-list. Test with a
  fake `Transcriber` (`model_factory`).
* **Prospero** (`family_api.py:TtsBody`, `family_tools.py:tts`, `mcp_server.py:voice_tts`): `engine`, `speed`; `tests/test_family.py` has `FakeTTS`.
* **Kafka** (`agent_tools.py`, `ocr.py`, `readers.py`): `doc_extract`, `ocr_image`, `ocr_pdf`, `ocr_status`, one in-memory job table shared by the last three; `Ocr(factory=...)` for tests.
* **Borges** (`agent_tools.py`, `embedder.py`): `embed_texts`, `embed_status`; `FakeEmbedder` for tests.
* Every new tool: in the owner's `/api/agent/tools` with the description style of its app (English first line + Spanish synonyms line), routed by `/api/agent/call`, `readOnlyHint` /
  `idempotentHint` set (`transcribe_file`, `doc_extract`, `ocr_*`, `embed_*` are read-only toward the person's data; `media_download`, `transcribe_cancel` are not).
* The client tests (`tests/commons/test_fam_{media,docs,embed}.py`, `test_fam_services_js.py`) run against a fake hub (`tests/commons/fam_hub.py`) that mimics the table in section 1; an
  owner's own tests should assert the argument and result shapes above, and the integration test is: `family.call("<owner>", "<tool>", {...})` through a real hub.

## 10. Environment

`HOARD_HUB_URL`, `HOARD_HUB_DATA_DIR`, `HOARD_APP_ID`, `HOARD_TOKEN_FILE` (the family client's); `HOARD_HOME` (default `~/.hoard`; the local TTS fallback writes under `tmp/tts`);
`HOARD_WHISPER_MODEL` (the local transcription fallback's Whisper size, default `small`), `HOARD_GPU_LEASE`, `HOARD_WHISPER_VRAM_MB`, `HOARD_LEASE_TIMEOUT_S` (see `media.md`).

Tests: `tests/commons/test_fam_media.py` (48), `test_fam_docs.py` (23), `test_fam_embed.py` (17), `test_fam_services_js.py` (10); the fake hub is `tests/commons/fam_hub.py`.

## 11. Hub service discovery and media imports

HoardLink 0.8.1 adds the Services tab and `hub_services_status` (also
`GET /api/services/status?refresh=1`). The rows identify Web (Hub), downloads
(Links), speech recognition (Funes), speech synthesis (Prospero), document OCR
(Kafka) and embeddings (Borges). Owner catalogues are checked concurrently with
a two-second timeout and a fifteen-second cache. `ready` means the required tools
are exposed; it does not load models or guarantee that the next inference will succeed.
Missing owners, unreachable apps and incompatible catalogues have distinct states.

`hub_media_import({url|download_id, folder, language?, model?})` starts an asynchronous
download → stateless transcription → transcript collection workflow. Poll
`hub_media_import_status({job_id, wait_s?})` (maximum wait 150 seconds). An existing
Links download can be reused without downloading it again. Files must be safe and
inside the selected folder; protected folders are refused. Funes creates no recording
session. Only the recovered text and its source are saved as `*.transcript.txt`, then
Borges is asked to add/watch the folder and reindex its transcript files.

`status=done, stage=index_requested` means Borges accepted the indexing request,
not that indexing has finished. Confirm the document in Borges before claiming it
is searchable. Owner errors, empty speech and unsafe paths stop the workflow.
The Hub holds at most two running imports. The atomic local journal
`<data>/media-imports.json` retains finished jobs for seven days, preserves paused
jobs, and refuses new work at 100 retained jobs. Restart changes running imports
to `paused`; nothing resumes automatically. `hub_media_imports` lists them and
`hub_media_import_resume({job_id})` follows saved Links/Funes job IDs and skips
completed steps. Repeated `download_id` + folder + language/model requests reuse
the retained running, paused or completed import. URL-only requests are fresh work.
A provider poll has a ten-minute limit and all common terminal states stop polling.

Before each mutation the journal records the intent. If its reply is lost, resume
requires reconciliation with the owner: `download_id`, `transcribe_job_id`,
`collection_id`, or `index_requested: true` as appropriate. This avoids blindly
duplicating work; it is not an exactly-once transaction across HTTP services.
Funes's stateless job retention still applies: an expired job may need a new,
explicit import. Closing the Hub cannot cancel a provider request already in flight.

Each transcript has a `*.transcript.provenance.json` sidecar with the provider IDs,
model/language when reported, a source URL without credentials/query/fragment,
and the transcript SHA-256. Resume checks the hash before indexing and preserves
edited evidence. The private journal retains the original URL to resume the task;
public job views omit it. It belongs in the existing private data/backup policy.
Started, progress, paused, done and failed events feed the existing family jobs
view; paused imports are not falsely reported as running or swept away as stale.

HTTP operator routes: `GET /api/services/imports`,
`POST /api/services/imports/resume`. They require the Hub operator or its token.

### Machine-readable ownership and cohesion

`hoard_link/_data/family-services.json` is the canonical owner/tool contract.
Python clients and the Hub consume it; Node consumes a byte-identical mirror in
`js/hoard-commons/family-services.json`, regenerated before vendoring. Discovery
checks status/cancellation tools as well as job starters. `workflows` checks
the Links/Funes/Borges import prerequisites separately: a working embedding
service does not prove that collection registration and reindexing exist.

`hub_cohesion` / `GET /api/services/cohesion` reports the actual registry,
declared capability overlaps, Python/Node shared-code drift, owner catalogues,
workflow prerequisites and import states. Overlaps mean review responsibilities;
they do not prove identical implementations. Models are checked on execution.
The read-only source inventory is `scripts/audit_cohesion.py`; see
[the cohesion audit](cohesion.md) for boundaries and remaining gaps.

The recommended rule `rule-download-transcribe-index` listens for
`links.job.done` with `data.media_kind=audio` and reuses `data.job_id` / `data.dir`.
It is installed **disabled**: enable it only for downloads whose transcripts belong
in the library. Indexing and disk writes are explicit side effects.

Faustus uses the shared Hub fetch client, Funes for local speech recognition and
Kafka for bounded OCR of scanned PDF attachments. Existing local fallbacks remain
available when the owner cannot be reached. Attachment ingestion with vision disabled
does not start OCR. Its adapter reuses the configured token file; older installations
can discover the default local Hub from registered Hoard servers without copying tokens.
Discovered credentials are never attached to a custom Hub endpoint.

The common Whisper engine remembers missing CUDA libraries for subsequent `auto`
instances in the same process, so later jobs use CPU instead of retrying a broken
native runtime. Memory pressure does not disable CUDA, and explicit `cuda` remains
an explicit choice. Restart after installing the missing libraries to retry automatic GPU use.

Windows validation and known limitations: [validation record](windows-validation.md).

### Shared live storage (Atlas)

Atlas owns ordinary shared project folders, file revisions and references to
reusable results. It does not replace the Hub's coordination or Borges's index.
Python `fam_workspace` and Node `fam-workspace.js` use Atlas through the Hub with
the caller's own credential. `hub_workspace` exposes the same operations to the
operator. Native programs open the returned path directly; registering it never
copies or modifies its content. Existing copying importers still copy unless
they explicitly adopt live references. Lumiere adds `media_shared(file_id)`.

Projects contain `shared/` and `hoards/<app>/` folders. Membership and sphere
scope API access and task context; they are not operating-system ACLs. Files
stay accessible in native tools when Atlas stops. Project renames/archive keep
paths stable. No automatic migration of existing files or private app databases.

`lookup(project_id, source_ids, recipe)` refreshes source hashes and checks any
output hash. Publish requires `source_revisions` from the lookup: edits during
processing reject publication. Recipes need operation/version, options and the
actual model revision where applicable. Results remain normal files in that
project; editing them invalidates reuse rather than overwriting human work.
Context contains a goal, sphere and at most twenty explicit file references;
source text is never promoted to agent instructions. Mutation request IDs have
durable receipts, not cross-owner exactly-once guarantees.

Hub backups include Atlas's configured shared root as `atlas-files` as well as
its private data. Shared-file restores require a separate review destination.
Existing backup size limits/exclusions and concurrent-file snapshot limits apply.

### Cooperative CPU, RAM and disk admission

The resources facet complements the GPU lease arbiter. Authenticated siblings
request/release claims through `/api/resources/request` and `/api/resources/release`.
`hub_resource_status` reports capacity and claims. Python's
`fam_resources.claim(cpu_slots=1, ram_mb=..., io_slots=..., mode='background')`
waits for admission, renews the claim and releases it on exit. A Hub failure stops
admission; long work should check the yielded lost-reservation event between chunks.
The consumer must honor its budget; this mechanism does not throttle native
processes or change every application's worker count automatically.

Claims persist across Hub restart until expiry, belong to the authenticated
caller and reserve foreground capacity. Config lives in `hub.json.resources`:
`cpu_slots`, `interactive_reserve` and `io_slots`. RAM uses live availability
with headroom. No available-memory reading means no positive-RAM grant.
