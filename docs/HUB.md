# Hoard Hub reference

The overview lives in the [README](../README.md#hoard-hub-the-desktop-launcher)
([español](../README.es.md#hoard-hub-el-lanzador-de-escritorio)). This page
is the reference for the parts that need more than a paragraph.

## Command line

```
python -m hoard_link.hub [--port N] [--roots "A;B"] [--data-dir DIR]
                         [--no-window | --browser | --native] [--stay] [--profile NAME] [-v]
python -m hoard_link.hub --install-autostart [--profile NAME] [--no-window | --window] [--port/--data-dir/--roots]
python -m hoard_link.hub --uninstall-autostart
python -m hoard_link.hub --autostart-status
```

* `--profile NAME` starts that profile once the hub listens (in the
  background: the window opens right away). When a hub is already running,
  the new launch asks it to start the profile, opens a window on it and
  exits.
* `--install-autostart` (Windows) writes
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Hoard Hub.cmd`:

  ```bat
  @echo off
  rem hoard-hub autostart (python -m hoard_link.hub --uninstall-autostart removes it)
  cd /d "<this repository>"
  start "" "<venv>\Scripts\pythonw.exe" -m hoard_link.hub --no-window --profile video
  ```

  `pythonw` is taken from next to the interpreter that ran the command
  (falls back to that interpreter). Headless is the default (`--no-window`);
  `--window` launches `--stay` instead, which opens the hub's window at
  login and keeps serving after it is closed. Under `pythonw` the hub logs
  to `data/logs/hub.log`. Exit code 0 on success, 1 when nothing was
  installed.
* `--uninstall-autostart` removes that file, only if it carries the
  `rem hoard-hub autostart` marker. `--autostart-status` prints whether it
  is installed, headless or with a window, the profile, and the command.
* Linux/macOS: the three commands only print a message (install exits 1
  and prints the command line to put in a systemd user unit or a launchd
  agent). Nothing is written.

## App windows

`POST /api/apps/<id>/open` (`hub_open_app`, the **Open window** button)
opens the app's UI as a desktop window through one of two routes, picked by
`window_engine` in `data/hub.json` (`HOARD_HUB_WINDOW_ENGINE`):

| `window_engine` | Route |
|---|---|
| `auto` (default) | the Hoard Window shell when `shell/node_modules/electron` exists, else Chromium |
| `shell` | Hoard Window (falls back to Chromium when it is not installed) |
| `chromium` | an Edge/Chrome/Brave `--app` window |

Both carry `--user-data-dir=<data>/profiles/<id>`, which is how
`list_windows` finds them again (processes named msedge/chrome/chromium/
brave/electron with that flag; children are folded into their root) and
how **Close windows** closes them. The result of an open says which route
ran: `{"mode": "app-window", "engine": "shell" | "chromium", "pid": …}`;
`/api/apps` (`hub.window_engine`) and `/api/config` (`window_engine_used`) report the route as resolved now.

The shell writes `hoard-window.json` (bounds, maximised, zoom) and
`hoard-window.log` (start, page loads, failed loads, crashed child
processes) into the app's profile folder. It turns off Chromium's native
occlusion tracking: a window launched by a background process is not given
the foreground, and with occlusion tracking on it was treated as hidden and
never painted (a blank window with only the caption buttons).

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
      "apps": ["borges", "funes"],
      "desktop": ["hypatia"]
    },
    "video": {
      "apps": ["daguerre", "funes"],
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

## Repos

The **Repos** tab (and the `hub_repos*` tools) show the state of every git
repository of the family in one table: what was never pushed, what has
uncommitted work, which vendored copies drifted, which branches should not be
there. It exists because those things pile up quietly across thirty-odd
repositories, and the repositories are what gets looked at from outside.

**The hub never changes a repository.** It does not push, commit, reset,
clean, delete, check out or merge. Every git call is a read, run as
`git --no-optional-locks -c core.quotepath=off -C <repo> …` with
`GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, a 10 s timeout
and no console window on Windows, so a scan never takes `index.lock` (a
test watches the `.git` folder during scans) and never rewrites the index.
The one network call is `fetch` (`git fetch --quiet --prune`, 60 s), only
when asked for by name, and it only updates remote-tracking refs. Pushing is
text: the hub gives the command and you run it.

### What is scanned

Every direct child folder with a `.git` of each root the hub scans
(`roots`, and the parent folder of every app it lists), plus the entries in
`repos.roots`, `repos.extra` and Faustus. Names with spaces and apostrophes
(`Phileas's Hoard`) work everywhere, including in routes
(`/api/repos/Phileas%27s%20Hoard`) and tools, which also accept a unique
fragment (`phileas`). `data/hub.json`:

```json
"repos": {
  "roots": [],                 // more folders whose children are repositories
  "extra": [],                 // repositories anywhere else
  "exclude": ["scratch"],      // folder names or absolute paths to leave out
  "faustus_dir": "D:/LocalAI/faustus",   // default: the hub's faustus_dir
  "portfolio_dir": "…/portfolio-react",
  "stray_prefixes": ["claude/"],
  "default_branches": ["main", "master"],
  "ci": true,
  "expected_emails": ["luissalet@users.noreply.github.com"]
}
```

Per repository: current branch (or detached) and HEAD; remotes with
credentials stripped and the GitHub `owner/repo` (https, `ssh://` and the
`git@Alias:owner/repo.git` form); upstream and ahead/behind from local refs
(nothing is fetched); `unpushed` = commits on HEAD that are in no remote ref
(`rev-list --count HEAD --not --remotes`, also without an upstream);
`never_pushed` when no remote branch exists at all; staged / modified /
untracked counts and the first 50 paths; stashes; an unfinished rebase,
merge, cherry-pick or revert; an `index.lock` older than 10 minutes; local
branches other than the default, flagged `stray` when the name starts with a
`stray_prefixes` entry or the branch is fully merged into the default; the
last 10 commits with a pushed flag; the authors of unpushed commits; whether
README.md, README.es.md, LICENSE, `.github/workflows/*.yml`,
`faustus-plugin.json` and `app-icon.png` exist; and tracked files that look
like secrets (`.env`, `.env.*` except `.env.example`/`.sample`/`.template`,
`*.pem`, `*.key`, `id_rsa*`, `mcp-token`, anything under the root `data/`
except `.gitkeep`).

### Drift

The checks reuse `hoard_link/hub/drift.py`, the same code
`scripts/sync_vendored.py` and `scripts/sync_theme.py` use to fix what is
reported, so the two cannot disagree:

* **vendored library**: every `hoard_link/` folder (with an `__init__.py` or
  a `VENDORED.txt`, at most 3 levels deep, not under `node_modules`, `venv`,
  `dist`…) and `server/hoard-link.js`, compared with this repository's
  package; the stale files are listed (changed, missing, or gone upstream).
* **theme**: copies of `hoard-theme.css` that differ from
  `hoard_link/ui/hoard-theme.css`.
* **manifest**: `faustus-plugin.json` against
  `<faustus_dir>/plugins/<id>/plugin.json`, compared as JSON (key order and
  layout do not matter). States: `ok`, `differs` (with the keys),
  `missing_in_faustus`, `invalid`, `unchecked` (no Faustus folder),
  `n/a` (no manifest).

### CI and the portfolio

With `repos.ci` on, `gh` on the PATH and a GitHub remote, the latest run is
read with `gh run list -R owner/repo --limit 1 --json …` (15 s timeout,
cached 10 minutes per repository) and shown as passing / failing / running /
none. It is filled in after the snapshot is published, never in front of it.
Without `gh`, or without being logged in, the state is `unknown` and nothing
is reported. With `portfolio_dir`, the text files of its `src` are searched
once per scan for each repository's name or GitHub slug (case-insensitive);
`in_portfolio` is true/false, and `null` when no portfolio is configured.

### Issues

Each repository carries a list of issues with a `kind`, a severity and a
text in Spanish and English:

| severity | kinds |
|---|---|
| error | `in_progress` (rebase/merge/cherry-pick/revert), `stale_lock`, `ci_failing`, `tracked_secret` |
| warn | `unpushed`, `never_pushed`, `stray_branch`, `detached_head`, `vendored_drift`, `manifest_drift`, `scan_error` |
| info | `dirty`, `behind`, `theme_drift`, `manifest_missing`, `no_readme`, `no_readme_es`, `no_license`, `unexpected_author`, `not_in_portfolio` |

`unexpected_author` is a hint, not an error: an unpushed commit whose author
email is not in `expected_emails`.

### Snapshot and refresh

The last snapshot is kept in memory and in `data/repos.json` (the first paint
after a restart). Reading never waits for git: `GET /api/repos` returns the
cache with `age_s` and starts a background refresh when it is older than
5 minutes (or when there is none). One refresh runs at a time, repositories
are scanned in parallel (6 at once). Nothing is scanned when the hub starts;
the "Scan the git repositories" job template (`every: 30m`) keeps it fresh
without the page open.

### HTTP

| Route | Answer |
|---|---|
| `GET /api/repos` | summary + one compact row per repository, `age_s`, `refreshing`, `ci_pending` |
| `GET /api/repos/<name>` | the full record: issues, dirty paths, branches, commits, remotes, drift, CI |
| `GET /api/repos/<name>/push-command` | `{command}`, text only (`git -C "<path>" push`, or `push -u origin <branch>` when the branch has no upstream); 409 when there is no remote or HEAD is detached |
| `POST /api/repos/refresh` | `{wait: false}` by default (returns at once; poll `GET`); `{wait: true}` waits for the git phase |
| `POST /api/repos/<name>/fetch` | `git fetch --prune` there, then rescans that repository |
| `POST /api/repos/<name>/folder` | opens its folder in the file manager |

Same guard as every hub route (cross-site requests refused). Actions are
recorded as events (`hub.repos.fetch`, `hub.repos.push_command`).

### Tools

`hub_repos` (`filter`: all, issues, unpushed, dirty, drift, ci_failing; `text`),
`hub_repo` (`name`), `hub_repos_refresh`, `hub_repo_fetch` (`name`; marked
`openWorldHint`) and `hub_repo_push_command` (`name`). `hub_repos` waits for
the very first scan only.

### Events and the rule

After each full refresh the hub emits `hub.repos.scan`
`{repos, with_issues, unpushed_total, errors, scan_errors}` (`errors` counts
error-level issues). When a repository gains an error-level issue it did not
have in the previous snapshot it emits `hub.repos.issue`
`{repo, kind, severity, text, url}` (`text` in the hub's language, Spanish
unless `language` is `en`); the very first scan, with no previous snapshot,
emits none. The recommended rule *Repo problem → digest note*
(`rule-repo-issue-digest`) turns each of those into a `digest.item` event.
