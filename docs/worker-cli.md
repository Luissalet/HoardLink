# Hub commands for agents

Run `hub.cmd` from the Hub checkout, or invoke it by its absolute path from a work folder. Relative
argument files are opened from the caller's current directory. The equivalent on other platforms is
`python -m hoard_link.hub.cli` from an environment where Hoard Link is importable.

```text
hub.cmd state writer
hub.cmd start writer
hub.cmd tools writer
hub.cmd call writer <tool> --args-file arguments.json --json
hub.cmd stop writer
hub.cmd stations
hub.cmd chain <input-type> <output-type> --json
hub.cmd programs --refresh
hub.cmd tool hub_programs --json
hub.cmd --data-dir <isolated-data-folder> tool hub_list_apps --json
hub.cmd --hub-url http://127.0.0.1:8934 --token-file <bearer-file> tool hub_list_apps --json
```

`--args-file` takes a UTF-8 JSON object containing the tool arguments. `call` accepts `--reason`
and a 1–900 second `--timeout` (default 120 seconds). It sends the call through the Hub's normal
app proxy. For Hub tools, use `tool <MCP-name>`. The CLI prints a readable summary or indented
result by default. With `--json`, it writes one JSON value to stdout, including the original
result envelope on tool calls. The process exits with 0 on success, 1 on request or tool failure
and 2 for invalid command-line syntax. A timed-out write is not retried.

Set `HOARD_AGENT_ID` and `HOARD_AGENT_SESSION` before starting the CLI or the Hub's MCP bridge
to attribute proxied writes in apps with an agent journal. Each variable is optional. Without
them, the app sees the proxy's existing default identity and an empty session.

The client prints the selected Hub URL to stderr before making a request. For an isolated instance,
use `--data-dir` or `HOARD_HUB_DATA_DIR`. The selected folder supplies the URL written by the running
Hub and its `mcp-token`. An absent URL file stops the command. Use `--hub-url` to select a destination
directly and add `--token-file` when it requires a bearer token. These options can go before or after
the subcommand. Explicit selection takes precedence over inherited settings. The legacy
`HOARD_HUB_URL` and `HOARD_HUB_TOKEN_FILE` overrides apply when no isolated data directory or explicit
URL is selected. The CLI connects to an existing Hub. App lifecycle changes are explicit commands.

## HTTP and MCP

Use tool names from MCP `tools/list` or the authenticated `GET /api/agent/tools` catalogue to
identify tools across Hub versions. For example, `hub_agora_console_overview` reads console turns
and their liveness classifications.

Use `GET /api/agent/tools/<name>` for the schema and `POST /api/agent/tools/<name>` for invocation.
Send the Hub bearer token and the arguments object as the POST body. The named POST and the
shared `POST /api/agent/call` return the same `{ok, tool, result}` envelope, using the same
handler as MCP. Existing app routes still work. `GET /api/apps/<id>/status` is an explicit alias
for `GET /api/apps/<id>`. Use the named `hub_app_status` tool to include the log tail.

For `hub_call_app`, set `X-Agent-Id` and `X-Agent-Session` on either endpoint, or put `caller`
and `session` alongside `arguments` in a shared `POST /api/agent/call` request. Headers take
precedence. Put the reason for a write in the `hub_call_app` arguments.

## Local programs

Open **System → Capabilities → Local programs** in the Hub. The page and `hub_programs` show the
detected path, version when available and a headless usage example. When a command example starts
with a program name, substitute the reported path if the program is outside `PATH`. The Playwright
path is its Python interpreter. For ComfyUI, discovery and the Hub's service controls look in the same
locations, including portable installs under nearby `Tools` directories. An explicit `backends.json`
or `COMFYUI_DIR` location takes precedence.

Use `hub_service_start` with `id: "comfyui@<port>"` and the physical `gpu` index, and
`hub_service_stop` with the same ID when done. To coordinate physical GPU use, request the GPU
through `hub_lease_request` before starting and release the reservation with `hub_lease_release`
after stopping. Apps that already acquire a lease for each render need enough unreserved memory to
obtain their own lease when they use the same arbiter. A non-default port uses separate input,
output, temp, user and database paths on versions supporting the corresponding flags.

The inventory reads standard installation locations, nearby `Tools` directories, the Hub's
ComfyUI settings, Python interpreters used by app launch specifications and virtual environments
beside registered apps or under `Tools/engines`. Some version descriptions come from local metadata and others from short
version commands. Detection starts no service, browser, model or application session. Unknown
versions and missing paths are shown explicitly. An automatic scan is cached for five minutes.
Use **Detect again**, `hub.cmd programs --refresh` or `hub_programs {"refresh":true}` to refresh it.

Use `hub_vulcan_blender_set {"version":"4.3"}` to select an installed Blender release by major and minor version
for Vulcan. The Hub keeps the setting in its data folder and lists it as **Blender · Vulcan** in Local programs.
The setting takes effect the next time the Hub launches Vulcan. Pass `null` for automatic selection, or use the
returned undo call to restore the previous selection if it is still current.
For primitive blocking, pin 4.3 with `hub_vulcan_blender_set {"version":"4.3"}`. Blender 5.2 is also compatible with the tested map pass.

For programs outside discovery, use **Register a program** or call `hub_program_register` with
`name`, an absolute `path`, `headless` instructions and an optional `version`. This records the
entry in Hub data. Registering the same values again returns the same entry. To remove it, use
the page's **Undo registration** action or call the `tool` with the `arguments` returned in
`undo`. Undo is specific to that registration.

Headless command references.
[Blender](https://docs.blender.org/manual/en/latest/advanced/command_line/arguments.html),
[Godot](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html),
[OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer/wiki/cli_mode) and
[Playwright](https://playwright.dev/python/docs/browsers).
