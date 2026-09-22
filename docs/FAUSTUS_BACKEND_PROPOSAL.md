# Proposal: a plugin-backend lease API in Faustus

**Status:** design note only — nothing in this document is implemented in
this repository. Hoard Link works fully today without it, via the
loopback fallback described in the main README. This is what a future
change on the Faustus side would unlock.

## The gap today

Hoard Link's step 3 (loopback probing) works, but it is blind to
Faustus's own admission control: an app resolving `image` against
ComfyUI, or `llm` against a llama-server, has no way to know that Faustus
is about to start a heavy job of its own, or that VRAM Faustus is
tracking is about to be claimed. Two agents (Faustus and a plugin app)
can both decide independently that a GPU has room and both be right for
a moment and wrong a second later. Nothing coordinates them beyond "ask
`nvidia-smi` and hope."

## What would fix it

Three additions to the Faustus HTTP API, all under the existing bearer
token model:

### 1. A `models:use` token scope

Today a Hoard Link token is minted with scope `chat` and used to read
`GET /api/models` and call the TTS/STT endpoints. A distinct `models:use`
scope would let an app's token be limited to "resolve and use a model
server", without also implying whatever `chat` implies for Faustus's own
chat UI — a narrower blast radius for a token an app keeps in its
`backend.json`.

### 2. `POST /api/plugin-backend/lease`

```
POST /api/plugin-backend/lease
Authorization: Bearer ody_... (scope models:use)
{
  "capability": "image",
  "app": "argus",
  "estimated_vram_mb": 6000,
  "max_wait_s": 30
}
```

Faustus already knows its own resident model's VRAM footprint and
whatever it is doing right now (a chat generation in flight, its own
ComfyUI job queued). A lease call would let Faustus's own admission logic
— not a second, independent `nvidia-smi` read from the app — decide
whether to grant the GPU job now, queue it, or say no with a reason
("Faustus's own image job is running; retry in ~20s"). The response
would carry a lease id and a deadline; the app finishes its GPU work and
(implicitly, on completion, or via a `DELETE`) releases it. This turns
Hoard Link's `gpu_free_mb()` best-effort read into a fallback for when no
Faustus is running at all, rather than the only signal available even
when one is.

### 3. `GET /api/plugin-backend`

```
GET /api/plugin-backend?capability=llm
Authorization: Bearer ody_... (scope models:use)
-> {"default": {"url": "...", "model": "...", "resident": true}, "alternatives": [...]}
```

A single, cheap call that names the default/resident model for a
capability without an app needing to fetch and filter the full
`/api/models` registry itself. `GET /api/models` (what Hoard Link uses
today) is the general registry; this would be the "just tell me the one
I should use" shortcut, useful for a Settings screen that wants to show
"currently: qwen3.8-27b-q8-llamacpp" without re-implementing Hoard Link's
own filtering.

## Why the loopback fallback is the right interim answer

None of the above changes what an app can already do without Faustus
running at all, which matters just as much: a plugin app must work
**with or without Faustus**, and loopback probing of llama.cpp/Ollama/
ComfyUI directly is the only thing that works in the "Faustus is closed"
case. The lease API would only ever be an *additional*, better-informed
path when Faustus is up — resolution order source #2, same as today —
never a replacement for source #3. Building it is worth doing once
Faustus's own admission control exists to lease against; until then, the
best-effort `nvidia-smi` read and Faustus's already-resident model
registry give an honest, if less coordinated, answer.
