# Hoard Hub reference

The overview lives in the [README](../README.md#hoard-hub-the-desktop-launcher)
([español](../README.es.md#hoard-hub-el-lanzador-de-escritorio)). This page
is the reference for the parts that need more than a paragraph.

## Profiles

`data/hub.json` → `profiles`: `{ "<name>": { "apps": [...], "commands": [...], "desktop": [...] } }`.

| Key | Meaning |
|---|---|
| `apps` | app ids (as `hub_list_apps` lists them) to start |
| `desktop` | app ids to open as desktop windows (started first when needed) |
| `commands` | external processes: `name`, `cmd` (a command line or a list of arguments), `cwd`, `health` (URL; any answer below 500 means running), `env` (extra environment) |

Starting a profile starts its apps and commands in parallel without
waiting for each, then opens the `desktop` apps (which waits for each of
them to be ready). Stopping it stops the commands the hub started and
every app of the profile (an app shared with another profile is stopped
too: a profile is a shortcut, not an owner). A profile's state is
`running` (every member up), `partial`, `stopped` or `empty`; an id that
matches no app is reported as `unknown` and makes a start fail without
stopping the rest.

Commands: the hub records the pid and creation time of what it started in
`data/commands.json`, so a restarted hub still reports and can stop them;
a command that answers its `health` URL but was started elsewhere is shown
as running and never stopped. Output: `data/logs/cmd-<profile>--<name>.log`.

No profile ships in the default configuration. Two examples to adapt
(paths and ports are placeholders):

```json
{
  "profiles": {
    "writing": {
      "apps": ["borges", "scribe", "funes"],
      "desktop": ["hypatia"]
    },
    "video": {
      "apps": ["daguerre", "scribe"],
      "commands": [
        {
          "name": "comfy gpu1",
          "cmd": "D:/ComfyUI/venv/Scripts/python.exe main.py --listen 127.0.0.1 --port 8189",
          "cwd": "D:/ComfyUI",
          "env": {"CUDA_VISIBLE_DEVICES": "1"},
          "health": "http://127.0.0.1:8189/system_stats"
        },
        {
          "name": "comfy gpu2",
          "cmd": "D:/ComfyUI/venv/Scripts/python.exe main.py --listen 127.0.0.1 --port 8190",
          "cwd": "D:/ComfyUI",
          "env": {"CUDA_VISIBLE_DEVICES": "2"},
          "health": "http://127.0.0.1:8190/system_stats"
        }
      ],
      "desktop": ["daguerre"]
    }
  }
}
```

HTTP: `GET /api/profiles` (every profile with each member's state),
`GET /api/profiles/<name>`, `POST /api/profiles/<name>/start`,
`POST /api/profiles/<name>/stop`. Agent tools: `hub_profile_list`
(read-only), `hub_profile_start {name}`, `hub_profile_stop {name}`.

## GPU memory leases

One queue for every program on the machine that wants VRAM. The library
side is `hoard_link.lease()` (see the README); this is the hub side.

### Accounting

For every GPU `nvidia-smi` reports, with the inventory cached for 2 s:

```
reserved   = sum of vram_mb of the granted leases on that GPU
base       = memory used on that GPU the last time it had no lease
used_eff   = max(used, base + reserved)
available  = total - used_eff - lease_headroom_mb
```

`max` is what lets the arbiter count a reservation *before* its model is
loaded (then `base + reserved` is the larger term) without counting it a
second time once it is loaded (then `used` already contains it). Memory a
program without a lease takes later (a llama-server, a manual Ollama load)
shows up in `used` and is respected too. The cost is that memory freed by
a lease-less program while leases are held on that GPU is only seen once
the GPU has no lease again (the estimate errs on the safe side).

### Scheduling

* Order: `priority` (higher first), then arrival.
* A `gpu` index pins the request to that GPU; `"any"` (or omitted) places
  it on the eligible GPU with the most room.
* A queued request that does not fit **blocks the GPUs it could use** for
  every request behind it. A request pinned to another GPU still goes
  ahead; a stream of small loads cannot starve a large render.
* A request larger than the biggest eligible GPU (minus the headroom) is
  refused at once (HTTP 400): it could never be granted.
* With no inventory (no NVIDIA GPU or no `nvidia-smi`) every request is
  granted with `gpu: null` and a note, so callers never wait on a machine
  with nothing to arbitrate.

### Lifetimes

| What | Default | Ends when |
|---|---|---|
| granted lease | `ttl_s` = 1800 s | released, not renewed within `ttl_s`, or its `pid` is gone |
| queued request | 90 s keep-alive | released, not polled for 90 s, or its `pid` is gone |

Polling means any of `POST /api/lease/request {lease_id, wait}`,
`POST /api/lease/renew`, `GET /api/lease/<id>`. The pid check uses psutil
and the process creation time, so a recycled pid does not keep a dead
app's lease alive. Reaping happens on every call (there is no background
thread); the last reaps are listed in `GET /api/lease` under `reaped`.

Leases and the per-GPU `base` are written to `data/leases.json` after
every change and read back on start; anything expired or orphaned while
the hub was down is reaped on load.

### HTTP

No bearer token (like the UI routes: loopback only, cross-site browser
calls refused).

| Route | Body | Answer |
|---|---|---|
| `POST /api/lease/request` | `owner, purpose, vram_mb, gpu, priority, ttl_s, wait, wait_s, pid` | `lease_id, state (granted\|queued), gpu, expires_at, position, lease` |
| `POST /api/lease/request` | `lease_id, wait, wait_s` | the same, for a request already queued (keeps its place) |
| `POST /api/lease/renew` | `lease_id, ttl_s` | the same; 404 when unknown or expired |
| `POST /api/lease/release` | `lease_id` | `released: true\|false` (idempotent) |
| `GET /api/lease` | — | `gpus[] (total, used, free, reserved, base_used, available, leases)`, `leases[]`, `queue[]`, `inventory`, `headroom_mb`, `reaped[]` |
| `GET /api/lease/<id>` | — | one lease; 404 when unknown or expired |

`wait: true` long-polls up to 25 s (`wait_s` shortens it) and returns the
current state; clients loop with `{lease_id, wait: true}`. The Python
client's first request never waits, so it always learns its `lease_id`
and can withdraw on timeout or cancellation.

### Agent tools

`hub_lease_status` (read-only), `hub_lease_request`, `hub_lease_release`,
in the same catalogue as the other hub tools (`/api/agent/*` and the stdio
MCP bridge).

### Configuration

`lease_headroom_mb` in `data/hub.json` (default 256): memory kept free on
every GPU when granting.
