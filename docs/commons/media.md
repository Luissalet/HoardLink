# Media: processes, tools, ffmpeg, subtitles, speech to text, yt-dlp

Everything the family wrote for itself around `ffmpeg`, `ffprobe`, `yt-dlp`, `faster-whisper` and subtitle files.
Python modules in `hoard_link/` (stdlib only at import; optional pieces are imported lazily and raise
`Unavailable` / `missing_dependency` when absent), the Node twin in `js/hoard-commons/media.js` (ESM, no dependencies, Node 18+).
Rules: `docs/COMMONS.md`. Tests: `tests/commons/test_proc.py`, `test_media_{bins,ffmpeg,subs,stt,ytdlp,js}.py`; shared vectors
`tests/vectors/media_subs.json` (81 cases) and `media_ytdlp.json` (124 cases) are run by Python and by Node.

| Module | What it does | Node twin (`media.js`) |
|---|---|---|
| `hoard_link.proc` | no-console subprocesses, process-tree kill, cancel/timeout, streaming lines, exe lookup, reveal in file manager | `runProcess`, `runCapture`, `killTree`, `which`, `parseCommandSpec` |
| `hoard_link.media.bins` | find / verify / report / update ffmpeg, ffprobe, yt-dlp, gallery-dl, piper, node; yt-dlp argument builders, output parser, failure classifier, URL check | `resolveTool`, `toolsStatus`, `updateTools`, `downloadRelease`, `buildYtdlpArgs`, `parseYtdlpLine`, `classifyFailure`, `explainFailure`, `normalizeMediaUrl`, `MediaQueue`, `ProgressTracker` |
| `hoard_link.media.ffmpeg` | `FFmpeg` class: safe probe, progress, PCM / frames / waveform, loudness, conversions, "plays everywhere" | `parseCodecs`, `needsTranscode` (the rest stays Python) |
| `hoard_link.media.subs` | SRT / VTT / LRC / TXT writers, SRT / VTT / LRC / json3 parsers, rolling auto-caption dedupe, ASS helpers | `toSrt`, `toVtt`, `parseVtt`, `subtitleText` ... (same names, camelCase) |
| `hoard_link.media.stt` | `Transcriber` (faster-whisper): lazy load, GPU lease, CPU fallback, hallucination filter | - |

## `hoard_link.proc`

```python
run(args, *, timeout=None, cwd=None, env=None, input=None, check=False, text=True, cancel=None, grace_s=2.0) -> CompletedProcess
run_streaming(args, on_line, *, stderr_line=None, timeout=None, cancel=None, cwd=None, env=None, grace_s=2.0, low_priority=False) -> int
popen(args, *, detached=False, low_priority=False, **kw) -> Popen
kill_tree(target: int | Popen, *, grace_s=5.0, force=True) -> bool      # True when it is gone
request_stop(proc) -> None                                               # polite: CTRL_BREAK_EVENT / SIGTERM to the group
pid_alive(pid) -> bool                                                   # a zombie counts as gone
exe_candidates(name, *, explicit=None, env_vars=(), extra_dirs=(), allow_scripts=False) -> [(path, how)]
find_exe(name, *, explicit, env_vars, extra_dirs, verify_args=None, allow_scripts=False) -> str | None
which(name) -> str | None
reveal_command(path, platform=None) -> list | None ; reveal_in_file_manager(path) -> bool
build_env(base=None, **extra) -> dict ; no_window_kwargs() -> dict ; tail_lines(text, n=12, limit=1200) -> str
class Cancelled(HoardLinkError)(message="Cancelled.", partial=None)
```

* **Argument lists only** (a string raises `TypeError`); `stdin` closed unless `input` is given; text output is UTF-8 with
  `errors="replace"`; `PYTHONUTF8=1` for Python children (`build_env`).
* **Windows:** `CREATE_NO_WINDOW` on every child (no flashing console), `CREATE_NEW_PROCESS_GROUP`, `detached=True` tries
  `CREATE_BREAKAWAY_FROM_JOB` first; `kill_tree` is `taskkill /T` then `/F` after `grace_s`. **POSIX:** new session, `killpg`
  (psutil or `/proc` when the child does not lead a group), `SIGTERM` then `SIGKILL`.
* `run` kills the **tree** on `timeout` (`subprocess.TimeoutExpired` with the output so far) and when the `cancel` event is set
  (`Cancelled`, `partial` = output so far). `run_streaming` also splits on `\r` (progress meters) and swallows callback errors.
* `exe_candidates` order: explicit > env vars (a path, a folder, or a command name) > `extra_dirs` > PATH (`.exe`/`.com` only on
  Windows, `.cmd`/`.bat` shims need `allow_scripts`) > WinGet links > WindowsApps > Program Files / Homebrew / `/usr/bin` >
  `$HOARD_HOME/bin`. `find_exe(..., verify_args=["-version"])` skips candidates that do not exit 0.
* `reveal_command` returns `["explorer.exe", "/select,", path]` as a list, so a quote or space in a path cannot alter the command.

## `hoard_link.media.bins`

```python
find(name, *, refresh=False, legacy_env=(), extra_dirs=(), runner=None, clock=None) -> Tool
status(*, refresh=False) -> dict            # for a settings page: per tool found/path/version/how/tried/hint, python, bin_dir, install_command
update(name="ytdlp", *, method="auto", runner=None, fetch=None, verify_sha=True) -> dict   # tool, ok, method, before, after, updated, output, error?
bin_dir(create=False) ; reset_cache() ; install_hint(name, plat=None) ; tool_missing(name) -> Unavailable ; parse_command_spec(spec, bin_name="")
class Tool: name, path, argv, version, how, tried, source, hint ; .found ; bool(tool) ; .command(*args) ; .public()
TOOL_NAMES = ffmpeg, ffprobe, ytdlp, gallerydl, piper, node
```

* **Order:** `HOARD_<NAME>` > the legacy per-app variables (`LINKS_FFMPEG`, `LUMIERE_FFMPEG`, `COOKHOARD_FFMPEG`, `PROSPERO_FFMPEG`,
  `IMAGEIO_FFMPEG_EXE`, `LINKS_YTDLP`, `COOKHOARD_YTDLP`, `LINKS_GALLERYDL`, `PROSPERO_PIPER`, `*_FFPROBE`, plus the caller's
  `legacy_env`) > (ffprobe: next to the ffmpeg found) > `$HOARD_HOME/bin` and `extra_dirs` > PATH / WinGet / system folders >
  `python -m yt_dlp` > imageio-ffmpeg. An env value may be a path, a folder, a command, `"python -m yt_dlp"` or a `.py` script.
* **Every candidate is run** (`-version` / `--version`) and skipped when it does not exit 0; `tried` says why. A missing tool never
  raises: the `Tool` is falsy and carries a `hint`. Cached 30 s found / 4 s missing; the key includes the relevant environment.
* `update`: standalone yt-dlp `-U`; when that says it came from a package manager, or the tool is a Python module, `pip install -U`;
  else the release is downloaded into `$HOARD_HOME/bin` and **checked against `SHA2-256SUMS`** (a mismatch installs nothing).
* **Pure helpers (Node-twinned):** `video_selector`, `cookie_attempts`, `cookie_args`, `ytdlp_base_args`, `build_ytdlp_args`,
  `build_gallery_args`, `build_probe_args`, `parse_ytdlp_line`, `format_speed`, `format_eta`, `ytdlp_age_days`, `ytdlp_stale`,
  `classify_failure`, `detect_platform`, `file_kind`, `normalize_media_url`.
  yt-dlp always gets `--ignore-config`; with a Node path (and yt-dlp >= 2025.11.12, or an unknown version) also
  `--js-runtimes node:<path> --remote-components ejs:github`. Progress goes through `LHP|` / `LHPP|` / `LHSEL|` / `LHMETA|` templates.
* **`classify_failure(tool, stderr, code=None)`** -> `no_ffmpeg | no_video | unsupported | network | forbidden | login | outdated | unavailable | unknown`.
  `Unable to download webpage: <urlopen error [Errno -2] Name or service not known>` is **network** (the Cook regex read the word "age"
  inside "webpage" as an age restriction and asked for a login); `Sign in to confirm you're not a bot` is **login**;
  `HTTP Error 403: Forbidden` is **forbidden** (an outdated yt-dlp is the usual cause) unless the text also says sign in / cookies.
  The JS `explainFailure(tool, stderr, {code, lang})` returns a `MediaError` with a message the user can act on and the Links flags
  (`kind`, `noVideo`, `unsupported`, `authLike`, `outdatedLike`, `fatal`).
* **`normalize_media_url`** adds `https://`, normalises, and raises `MediaUrlError` (JS: `MediaError`) for non-http(s), single-label hosts,
  `localhost`, `*.local`/`*.internal`, and IP literals in any spelling (`2130706433`, `0x7f.1`, `[::ffff:127.0.0.1]`) that are loopback,
  private, link-local, CGNAT, multicast or reserved. No DNS lookup: a public name resolving to a private address needs the fetcher's own guard.

## `hoard_link.media.ffmpeg`

```python
FFmpeg(tools=None)                 # tools: {"ffmpeg": path | argv | Tool, "ffprobe": ...}; default: bins.find on first use
  .available() ; .version ; .ffmpeg / .ffprobe (argv lists)
  .probe(path, safe=False, *, timeout=None) -> dict ; FFmpeg.summarize(info) -> dict ; .duration(path) -> float
  .run(args, *, duration_s=0.0, progress=None, cancel=None, timeout=None, cwd=None, low_priority=False) -> str   # -progress pipe:1
  .capture(args, *, binary=False, timeout=3600.0, cancel=None) -> (stdout, stderr)
  .extract_pcm(path, *, rate=16000, start_s, duration_s, stream, cancel) -> bytes        # mono s16le
  .extract_frames(path, *, width, fps, start_s, duration_s, gray, cancel) -> (bytes, w, h)
  .grab_frame(path, at_s, out, width=0) -> Path ; .waveform_peaks(path, buckets=800, *, stream=0, rate=8000) -> [float]
  .loudness(path, *, stream=0) -> {integrated_lufs, lra, true_peak_dbfs} ; .loudnorm_measure(...) ; FFmpeg.loudnorm_second_pass(measured, ...) -> str
  .encoders / .filters / .has_encoder(n) / .has_filter(n) ; .video_encoder(preference="auto", codec="h264")
  .to_wav16k_mono(src, dst) / .to_mp3 / .to_ogg_opus ; .ensure_playable(path, *, replace=True, crf=20, preset="medium", cancel, progress) -> Path
  .convert_wav(data, channels, sampwidth, rate) -> bytes ; .concat_wavs([(wav_bytes, pause_s)]) -> (wav_bytes, duration_s)
module: FFmpegError(message, code, returncode, stderr) ; needs_transcode(summary) ; atempo_chain(factor) ; peaks_from_pcm(pcm, buckets)
        filter_path(path) ; filter_text(text) ; seconds(t) ; fps_fraction(fps) ; is_media_file(path) ; MEDIA_EXTENSIONS ; DEMUXERS
```

* **`probe(safe=True)`** is for files an agent or an upload named (Faustus' hardened probe): local regular non-empty files up to 8 GiB,
  no URLs / UNC paths, `-protocol_whitelist file`, the demuxer forced and whitelisted from the extension (a playlist renamed `.mp4`
  cannot make ffprobe read another file), `-max_alloc`, `-probesize`, `-max_streams`, 10 s timeout, 64 KB output cap.
  `FFmpegError.code`: `failed`, `timeout`, `invalid_media`, `invalid_path`, `file_too_large`, `output_limit`, `nvenc_unavailable`.
* Inputs that start with `-` or look like a protocol (`concat:`, `http:`) are neutralised (`./-x`, `file:`). `out_time_us` and
  `out_time_ms` are both microseconds; progress callbacks are never duplicated and end at `1.0`.
* **Plays everywhere** (Links rule): H.264 8-bit 4:2:0 + AAC/MP3. `ensure_playable` converts otherwise (temp `<stem>.hoard-tmp.mp4`, then
  replace; `replace=False` writes `<stem>.h264.mp4` and never overwrites), returns the original when it already qualifies, is not a video,
  is unreadable or the conversion fails, and removes the partial file on cancel.
* `concat_wavs` needs no ffmpeg when every clip has the first clip's format.

## `hoard_link.media.subs` (and its twin)

```python
Cue(start_s, end_s, text, speaker="", words=())          # JS / JSON wire form: {start_s, end_s, text, speaker, words}; dicts are accepted everywhere
to_srt / to_vtt(cues, *, speakers=False, min_duration_s=0.0) ; to_lrc(cues, *, metadata=None) ; to_txt(cues, *, timestamps=False, speakers=True)
parse_srt / parse_vtt(content, *, per_line=False) ; parse_lrc(content, *, last_s=4.0) ; parse_json3(content, *, dedupe=False)
dedupe_rolling(cues) ; cues_to_text(cues, gap_s=1.2) ; subtitle_cues(content) ; subtitle_text(content, gap_s=1.2)
srt_time / vtt_time(t) ; ass_escape(text) ; ass_color("#RRGGBB", alpha=0) -> "&HAABBGGRR" ; ass_time(t)
```

* Milliseconds are rounded **half up before** the fields are split: `59.9996` is `00:01:00,000`, never `00:00:59,1000` (only one of the five copies
  rounded correctly; Python's `round` is half-to-even and `format` differs from JS, so both sides use `floor(x + 0.5)`).
* `subtitle_text` is "what the caption file says": the rolling auto-captions of YouTube (each line shown twice, each cue extending the last)
  collapse, and lines join into continuous speech with a newline only where the speaker stopped (gap > 1.2 s or sentence end).
* `ass_color` raises `ValueError` on garbage; `ass_escape` neutralises braces and backslashes so text cannot inject override tags.

## `hoard_link.media.stt`

```python
Transcriber(models_dir=None, size="small", device="auto", compute="auto", lease=True, *, owner="hoard", model_factory=None, lease_factory=None)
  .transcribe(audio, *, language=None, word_timestamps=True, vad=True, initial_prompt="", progress=None, cancel=None, beam_size=5, clean=True, extra_phrases=()) -> Transcript
  .reconfigure(size=None, device=None, compute=None) ; .ensure_loaded() ; .close() ; .info() -> dict
Transcript(language, duration_s, text, segments, language_probability, model, device, note, stats) ; .as_dict()
clean_segments(segments, *, extra_phrases=(), stats=None, ...) -> list ; CleanStats ; is_hallucination(text, ...) ; collapse_ngram_loops(text) ; normalize(text)
cuda_dll_dirs(*, roots=None, platform=None) ; cuda_available() ; model_present(models_dir, size) ; model_repo(size) ; whisper_vram_mb(size) ; available()
```

* Models live under `$HOARD_HOME/models/whisper`. `audio` is a path or a 16 kHz mono int16 / float32 array (numpy only then).
  Segments: `{start_s, end_s, text, words: [{start_s, end_s, word, p}], avg_logprob, no_speech_prob, compression_ratio}`.
* **GPU lease:** a CUDA load asks the hub for the model's VRAM (`hoard_link.lease`); the lease covers the load and the first inference.
  A lease timeout or refusal, a CUDA load failure, or a CUDA error in the middle of an inference (`cublas64_12.dll is not found`) reloads on
  the **CPU**, sets `Transcript.note`, and the model stays loaded (no 120 s wait on every file). `HOARD_GPU_LEASE=0` disables leasing;
  `HOARD_WHISPER_VRAM_MB`, `HOARD_LEASE_TIMEOUT_S` tune it (the old `SCRIBE_*` names still work).
* `cuda_dll_dirs()` registers the `nvidia/*/bin` folders of the pip wheels on Windows before the first load.
* **Hallucination filter** (`clean_segments`): drops segments the decoder doubted (`no_speech_prob` > 0.6, `avg_logprob` < -1.0,
  `compression_ratio` > 2.4), whole-segment artifacts ("Thanks for watching", "Subtitulos por la comunidad de Amara.org", music markers,
  one-word "you"/"so"), collapses loops of 4+ repeats ("the the the the" -> "the"; three are natural speech) and near-duplicate neighbours.
  `Transcript.stats` says what was removed. Cancel raises `proc.Cancelled` whose `partial` holds the segments so far.

## Node twin: what is different

* Same pure functions, same vectors. `buildYtdlpArgs` / `buildProbeArgs` default `nodePath` to `process.execPath` (the server is
  Node); the Python builders add the JS-runtime flags only when `node_path` is passed.
* `resolveTool` accepts `node:/x/fake.js` (or any `.js/.mjs/.cjs`) as an override, so tests and portable setups can inject a fake
  program; results are `{tool, found, how, source, path, version, command: {cmd, args}, tried, hint}`.
* `runProcess(command, args, {signal, onStdoutLine, onStderrLine, env, cwd, timeoutMs, onSpawn, lowPriority})` never rejects on a
  non-zero exit; abort kills and **awaits** the tree (`killTree` is async) so the next queued job never overlaps the old one.
* `MediaQueue` is the Links FIFO (concurrency one, `cancel(id)`, `cancelAll()`, `idle()`); `ProgressTracker` folds video+audio streams and
  playlist items into one 0-99 bar. Messages are English; `setLanguage("es")` or `{lang: "es"}` restores Links' Spanish ones.

## Environment

`HOARD_HOME` (default `~/.hoard`), `HOARD_FFMPEG`, `HOARD_FFPROBE`, `HOARD_YTDLP`, `HOARD_GALLERYDL`, `HOARD_PIPER`, `HOARD_NODE`,
`PYTHON` (Node side), `HOARD_GPU_LEASE`, `HOARD_WHISPER_VRAM_MB`, `HOARD_LEASE_TIMEOUT_S`, plus the legacy names listed above.

## Migration

Each app deletes its copy, imports the commons, and keeps its own legacy environment variable working through `legacy_env`.

| App | Replace | With |
|---|---|---|
| **Links** | `server/media-tools.js`, `server/media-queue.js`, the pure parts of `server/media.js`, the SRT/VTT code of `server/transcript.js` | `resolveTool`, `updateTools`, `runProcess`, `killTree`, `MediaQueue`, `buildYtdlpArgs`, `parseYtdlpLine`, `ProgressTracker`, `explainFailure`, `normalizeMediaUrl` (new: the SSRF check), `subtitleText` / `parseVtt`. Call `setLanguage("es")` at startup to keep the Spanish messages; `LINKS_YTDLP` / `LINKS_FFMPEG` / `LINKS_GALLERYDL` still work. The "download then ensure playable" step becomes `FFmpeg.ensure_playable` on the Python side (or Links keeps its Node ffmpeg call and uses `parseCodecs` / `needsTranscode`). |
| **Writers** (`src/services/mediaDownloader.ts`, `scrapperMedia.ts`, `scripts/fetch-ytdlp.mjs`) | its own yt-dlp lookup / download | `resolveTool("ytdlp")` and `downloadRelease` / `updateTools` (SHA-256 checked); `buildYtdlpArgs` for the flags; `explainFailure` instead of string matching. |
| **Cook** (`server/media.mjs`, `server/importers/video.mjs`, `packages/core/src/video.ts`) | its yt-dlp / ffmpeg discovery, `explainFailure` regex (the "age in webpage" bug), `subtitleCues` / `dedupe` | `resolveTool`, `classifyFailure` / `explainFailure`, `subtitleCues`, `subtitleText`. `COOKHOARD_YTDLP` / `COOKHOARD_FFMPEG` still work. |
| **Prospero** (`procutil.py`, `video.py` `run_ffmpeg_with_progress`, `dubbing.py` `atempo_chain`, `voice_engines.py` `_split_ms`, `media_download.py` `check_url`, `backend.py` `ffmpeg_path`) | process helpers, ffmpeg progress, atempo chain, SRT time splitting, URL check, ffmpeg lookup | `proc.run` / `run_streaming` / `kill_tree`, `FFmpeg.run(progress=...)`, `atempo_chain`, `subs.srt_time` / `to_srt`, `normalize_media_url`, `bins.find("ffmpeg")`; `PROSPERO_FFMPEG` / `PROSPERO_PIPER` still work. |
| **Lumiere** (`ffmpeg.py`, `render/ass.py`, `analysis/speech.py`) | ffprobe wrapper, ASS escaping / colour / time, whisper loading | `FFmpeg.probe` + `summarize` (same field names plus `duration_s`), `subs.ass_escape` / `ass_color` / `ass_time`, `filter_path`, `Transcriber`. |
| **Funes** (`audio_memory/transcribe/whisper.py`, `filter.py`, `export.py`) | whisper loader, hallucination filter, SRT/VTT export, `hms` | `Transcriber`, `clean_segments`, `subs.to_srt` / `to_vtt` / `to_txt`. |
| **Faustus** (`src/media_inspection.py`, `stt_cleanup.py`, `research_podcast.py` `concat_wavs`, `services/youtube/youtube_handler.py`) | hardened ffprobe, STT cleanup, WAV concat, YouTube caption parsing | `FFmpeg.probe(safe=True)` + `summarize`, `clean_segments`, `FFmpeg.concat_wavs`, `subs.subtitle_cues` / `subtitle_text`, `bins.build_probe_args`. |
| **Vitruvius** (`browser.py:466` raw `ffmpeg` for the recording, `services.py:62` `ffmpeg_available`, `mcp_server.py:114` detached flags) | `shutil.which("ffmpeg")`, an unchecked `subprocess` call, hand-written creation flags | `FFmpeg().available()`, `FFmpeg.run(...)` (no console window, tree kill, progress), `proc.popen(detached=True)`. |
| **Hypatia**, **Echo**, **Galton** (`mcp_server.py` in each; Galton also `galton_hoard/procs.py`) | hand-written `CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW`; Galton's `taskkill` / `killpg` helpers | `proc.popen(detached=True)`, `proc.kill_tree`, `proc.no_window_kwargs`. `procs.py` can become a re-export. |
| **Daguerre** (`api.py:292` `subprocess.Popen(f'explorer /select,"{path}"')`) | the string form that a quote in a file name can break | `proc.reveal_in_file_manager(path)`. |
| **DiskHoard** (`server.py:492-494` `explorer` calls) | both `explorer` branches | `proc.reveal_in_file_manager(path)` (selects the file; opens the folder when the file is gone). |
| **Babel** (`environments.py:66`) | its `CREATE_NO_WINDOW` kwargs helper | `proc.no_window_kwargs()`; `proc.run` for its pip / venv commands (UTF-8, tree kill on timeout). |
| **Pygmalion** (`procs.py`) | process-tree kill and no-window flags | `proc.kill_tree`, `proc.popen`, `proc.pid_alive`. |

Not moved here on purpose: video rendering pipelines (Lumiere), voice synthesis engines (Prospero), the browser-based capture
(Writers `pageCapture.ts`), and any app's own download folder / library bookkeeping. They call these modules; they do not become them.
