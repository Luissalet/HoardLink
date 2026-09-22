# Hoard Link

### One shared answer to "which model server do I use right now?"

**The shared model backend for agent-controlled apps: pick the
already-loaded local server for a capability instead of loading a second
copy of a model.**

[Español](README.es.md) · [Quick start](#installing-vendoring) ·
[Use with Faustus](#why-sharing-matters-on-a-gpu-bound-machine) ·
[API](#api) ·
[Portfolio](https://luissalet.github.io/Portfolio/#projects)

Hoard Link is a small Python library — standard library plus `httpx`, no
server, no port, no UI — that a set of local apps can each vendor a copy
of to answer one question: **for capability X (`llm`, `vision`,
`embeddings`, `tts`, `stt`, `image`, `video`, `music`), which server and
which model do I use right now, and why?** — and then to actually call it.

## Why sharing matters on a GPU-bound machine

On a machine running **Faustus** (a local AI workspace) plus several
plugin apps, the GPU is the scarce resource. A typical box here has a
`llama-server` (llama.cpp) serving a 27B model that alone holds most of a
60 GB card, sometimes Ollama and ComfyUI as well. If every app that wants
an LLM call loaded its *own* model, the machine would run out of VRAM
after the second app started. Hoard Link's job is to make every app ask
"is someone already serving what I need?" before it ever asks a server to
load anything, and to never disturb whatever the human is doing with
Faustus at that moment.

Hoard Link does not run its own model server and does not manage
lifecycles. It resolves an address, and — for chat/embeddings/tts — makes
the HTTP call for you against the server it found.

## Resolution order

For each capability, in order:

1. **Explicit configuration** — the app's own `backend.json` and
   `HOARD_<CAP>_URL` / `HOARD_<CAP>_MODEL` / `HOARD_FAUSTUS_URL` /
   `HOARD_FAUSTUS_TOKEN` / `HOARD_COMFY_URL` environment overrides. A
   capability with a `url` (or a `command`) wins outright and is not
   probed; a stale address surfaces as `BackendError` (status `0`: no
   HTTP response) the first time it's actually called. A `model` without
   a `url` is only a *preference*, applied wherever the later steps have
   a choice (Ollama resident models, the Faustus model list, an
   OpenAI-compatible server's model list).
2. **Faustus** — if a Faustus instance answers `GET /api/health` as
   healthy on `127.0.0.1:7000` or `:7001` (configurable), Hoard Link reads
   `GET /api/models` (with a bearer token when one is configured) for the
   model-server registry Faustus itself uses, and talks to that server
   **directly** — same server, same resident model, which is the actual
   sharing. Only local entries (category `local`, loopback URL) are used:
   a cloud endpoint in Faustus's registry is skipped so the app's data
   never leaves the machine. An Ollama entry is cross-checked against
   that Ollama's `/api/ps` before it is called resident. With no token
   configured the request is sent without one (a Faustus with auth
   disabled still answers); a 401 then says a token is needed. For `tts`/`stt` it also tries Faustus's own
   `/api/tts/*`/`/api/stt/*` endpoints; a 401/403 there is recorded as a
   reason (a browser-only session) and resolution moves on rather than
   failing.
3. **Shared servers on loopback**, probed in parallel with a 1s
   wall-clock timeout per request and cached for 30s (single-flight:
   concurrent resolutions share one probe; the probing client ignores
   `HTTP(S)_PROXY`): llama.cpp (`8080`–`8090`, via `/props`,
   `/v1/models`, `/slots`), Ollama (`11434`, via `/api/ps` for **resident**
   models, `/api/tags`, `/api/show` for capabilities), a generic
   OpenAI-compatible chat server on `1234` (`llm` only), and
   ComfyUI (`8188`, `image`/`video`). A port only counts as a
   llama-server if its `/props` carries llama-server keys. No probe ever
   raises, whatever JSON a port answers with.
4. **Nothing** — the capability comes back `unavailable`, with the list of
   reasons collected along the way (why Faustus didn't answer, why no
   loopback server matched, etc.) so an app's Settings screen can show a
   human exactly what to fix.

## Policies

- **`only_resident = True` by default.** Hoard Link picks models that are
  already loaded — Ollama's `/api/ps`, or a llama-server, which always
  serves exactly the one model it was started with. It will not ask a
  server to load a different model unless the app's config sets
  `allow_load: true` for that capability (or `only_resident: false`
  globally), and it never sends `keep_alive`. A model that would load is
  still checked (`/api/show`) for the capability asked for, and an
  embedding-only model is never picked for `llm`.
- **Yield to the foreground.** `await link.wait_idle("llm", max_wait_s=30)`
  is `True` immediately when the resolved llama-server has no slot
  `is_processing`, otherwise it polls every 2s until it does or
  `max_wait_s` elapses (then `False`, and the caller decides to postpone
  its background job). This works whether the llama-server came from
  probing, the Faustus registry or explicit config (`provider:
  "llamacpp"`). Ollama exposes no busy signal, so `wait_idle` always
  returns `True` for it — documented here rather than guessed at.
- **GPU jobs know free VRAM first.** `gpu_free_mb()` shells out to
  `nvidia-smi --query-gpu=index,memory.total,memory.used
  --format=csv,noheader,nounits` (best effort — no NVIDIA GPU or no
  `nvidia-smi` on PATH just yields `[]`; on Windows it runs with
  `CREATE_NO_WINDOW` and also looks in the legacy `NVSMI` folder).
  `resolve("image")` runs it in a worker thread and reports the GPU with
  the most free memory plus a per-GPU list. `ComfyClient` never calls
  `/free` on its own; only when the caller asks for it.
- **Every resolution explains itself.** `Resolution.reason` is a sentence
  fit for a Settings screen, e.g. `"llm -> llama.cpp at 127.0.0.1:8081
  (qwen3.8-27b-q8-llamacpp), from Faustus registry; resident"`.

## Installing (vendoring)

Hoard Link is meant to be **copied**, not installed as a third-party
dependency, so each app ships one committed copy of the exact behaviour
its tests were written against. Copy the whole `hoard_link/` directory
(every `.py` file, no `__pycache__/`) into your package, never edit the
copy, and to update, replace the directory wholesale and note the Hoard
Link commit you copied from:

```
<app_pkg>/
  hoard_link/        <- copy of this repo's hoard_link/ directory
  ...
```

```python
from .hoard_link import Link, LinkConfig
```

If your app already manages its own dependencies with `httpx` present,
that's the only third-party requirement. Python 3.11+, stdlib +
`httpx>=0.27,<1.0`, no platform-specific code — it runs the same on
Windows and Linux.

## `backend.json` schema

Every key is optional; everything not set falls through to Faustus, then
loopback probing.

```json
{
  "only_resident": true,
  "faustus": {"url": "http://127.0.0.1:7000", "token": "ody_..."},
  "comfy": {"url": "http://127.0.0.1:8188"},
  "capabilities": {
    "llm": {
      "url": "http://127.0.0.1:8081/v1/chat/completions",
      "model": "qwen3.8-27b-q8-llamacpp",
      "api": "openai",
      "provider": "llamacpp",
      "allow_load": false
    },
    "tts": {"command": ["piper", "--model", "es_ES.onnx", "--output_file", "{out}"]}
  }
}
```

Environment overrides (highest priority, layered on top of the file):
`HOARD_<CAP>_URL`, `HOARD_<CAP>_MODEL` (e.g. `HOARD_LLM_URL`,
`HOARD_VISION_MODEL`), `HOARD_FAUSTUS_URL`, `HOARD_FAUSTUS_TOKEN`,
`HOARD_COMFY_URL`. An empty variable counts as unset.

- `url` may be a server root (`http://127.0.0.1:8081`), a `/v1` base or a
  full endpoint; chat and embeddings each append the path they need. A
  URL under `/api/` implies `"api": "ollama"`, otherwise `"openai"`.
- `command` is a list of strings. `{text}`, `{voice}` and `{out}` are
  replaced literally; without `{text}` the text is written to the
  command's stdin (how Piper reads it), without `{out}` the audio is read
  from stdout. It runs without a console window, with a 120 s timeout.
- The file is read as UTF-8 with or without a BOM (Notepad's default).

## Minting a Faustus token

From Faustus, mint a token scoped to `chat` (the scope the model registry
and TTS/STT endpoints accept today) and put it in the app's
`backend.json` under `faustus.token`, or export it as
`HOARD_FAUSTUS_TOKEN` for the app's process. Faustus tokens look like
`ody_...`; treat them like any other local secret (keep them out of the
app's own git history — `backend.json` typically lives under the app's
gitignored `data/` directory).

## API

```python
from hoard_link import Link, LinkConfig

link = Link(LinkConfig.load(path_to_backend_json, env=os.environ, app="argus"))

res = await link.resolve("vision")   # Resolution(capability, provider, url, model, api, state, reason, details)

status = await link.status()         # dict for GET /api/backend in the app: every capability's Resolution

text = await link.chat(
    [{"role": "user", "content": "..."}],
    images=[jpeg_bytes],
    max_tokens=300,
    temperature=0.2,
    capability="vision",
    response_format=None,
)                                     # -> ChatResult(text, model, provider, usage, elapsed_ms, reasoning)

vecs = await link.embed(["a", "b"])  # when an embeddings server resolves; else raises Unavailable

wav = await link.tts("Hola", voice=None)   # Faustus TTS or a configured command; else Unavailable

comfy = await link.comfy()           # ComfyClient, or None if nothing resolves

idle = await link.wait_idle("llm", max_wait_s=30)   # True/False, see Policies above

link.sync.chat(...)                  # same calls, blocking, for synchronous app code
```

- `api` is `"openai"` (`/v1/chat/completions`, images as `image_url` data
  URLs on the last user message) or `"ollama"` (`/api/chat`, `images:
  [base64]` on the last user message, `stream: false`).
- Reasoning is kept out of `ChatResult.text` and exposed as
  `ChatResult.reasoning`: closed `<think>...</think>` blocks, an orphan
  `</think>` (the chat template opened the tag in the prompt), an
  unterminated `<think>` (cut off by `max_tokens`), and out-of-band
  fields (`reasoning_content` from llama-server, Ollama's `thinking`).
- `response_format` is passed through on the OpenAI dialect and mapped to
  Ollama's `format` (`json_object` -> `"json"`, `json_schema` -> the
  schema). Images get their real MIME type (JPEG, PNG, GIF, WebP).
- `ComfyClient(url)`: `system_stats()`, `object_info(node=None)`,
  `upload_image(bytes, filename, overwrite=True)`,
  `queue(workflow: dict, client_id) -> prompt_id`,
  `wait(prompt_id, timeout_s, on_progress=None)`,
  `outputs(prompt_id) -> list[OutputFile]`, `download(output) -> bytes`,
  `interrupt()`, `free(unload_models=False, free_memory=False)`.
  `queue()` validates the **API format** (a dict of node id ->
  `{class_type, inputs}`) and raises a clear `ValueError` if handed the
  UI's `{"nodes": [...], "links": [...]}` export instead. HTTP errors
  raise `BackendError` with the response body (so `/prompt`'s
  `node_errors` reach the caller), and `wait()` raises `BackendError`
  when the job finished with an execution error.
- Errors: `Unavailable(capability, reasons)` when nothing resolved,
  `BackendError(provider, status, body_excerpt)` for any failed call to a
  resolved server: a non-2xx status, a reply that is not the expected
  JSON, or `status == 0` when there was no HTTP response at all.
- `link.sync.*` runs on a private event-loop thread with its own HTTP
  client, so an app can mix `await link.chat()` and `link.sync.chat()`
  on one `Link`. If you inject your own `httpx.AsyncClient`, it is shared
  as given; don't mix both styles on it. Calling `link.sync.*` from that
  private thread (e.g. inside a callback) raises `RuntimeError` instead
  of deadlocking.

## Boundaries (what this library does not do)

- **No music generation.** ComfyUI's `object_info` gives no reliable
  signal for audio/music graphs the way `CheckpointLoaderSimple` does for
  image checkpoints, so `music` only resolves through explicit
  configuration or a matching Faustus registry entry, never through
  loopback probing.
- **No websocket progress for ComfyUI.** `ComfyClient.wait()` polls
  `/history/{id}`; it does not open `/ws` for push progress. The spec
  allowed this only "if simple" — a poll every second is simple, correct,
  and testable offline; a websocket client is a second thing to keep
  alive and reconnect, which wasn't worth it for a job-completion wait.
- **`resolve()` does not verify explicit configuration.** Resolution-order
  source #1 is trusted as given; a stale `backend.json` entry surfaces as
  a `BackendError` with `status == 0` on the first real call rather than
  being probed ahead of time. This keeps explicit configuration doing exactly one
  thing (override everything else) instead of two.
- **The sync facade is one background thread per `Link`.** It is meant
  for an app whose own request handlers are synchronous; it is not a
  thread pool and does not parallelize calls to the same `Link`. Called
  from async code it blocks that thread until the result is ready, so
  prefer `await` there.
- **No TTS over HTTP URL.** `tts` works through Faustus's TTS service or
  a configured `command`; a `tts` capability with only a `url` resolves
  but `link.tts()` raises `Unavailable`.
- **No cloud endpoints.** Faustus registry entries that are not local
  are ignored on purpose.

## Tests

```
python3 -m venv .venv
. .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest -q
```

163 tests, offline (`httpx.MockTransport`), in about 3 seconds. The only
real sockets are in the sync-facade tests, which start a tiny HTTP
server on an ephemeral `127.0.0.1` port to reproduce connection reuse
across event loops; the TTS-command tests run the current Python
interpreter as the "TTS binary". No test needs network access or a
downloaded model, so the CI workflow below runs the same way offline.

## License

MIT — see [LICENSE](LICENSE).
