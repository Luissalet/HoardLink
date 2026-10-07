# Portable Hub launch profiles

HoardLink profiles group registered app IDs, external command definitions and
desktop-window app IDs. A profile package is a versioned JSON snapshot of that
configuration. It is not an account, a second profile store, or an exported
runtime session.

## Export

Use `POST /api/profiles/export` with `{"name":"video"}`, or MCP
`hub_profile_export {"name":"video"}`. The response uses the
`hoardlink.launch-profile` format, version `1`, and preserves the native
`apps`, `desktop` and `commands` fields. App IDs are listed separately under
`dependencies.apps`; the destination must map them to IDs registered in its
own Hub.

The package excludes process IDs, command logs and runtime status. Environment
values whose keys look credential-related (`TOKEN`, `PASSWORD`, `API_KEY`,
`SECRET`, and similar) are omitted and their command/key names are listed under
`portability.redacted_environment`. Common inline credential flags and
credential-bearing health URLs make export fail with a command name only.
This scanner cannot infer every secret in arbitrary command text, so inspect a
package before sharing it. Operational flags such as `AUTH_ENABLED` and
`COOKIE_SECURE` are retained; this heuristic does not guarantee that every
secret is detected.

`commands[].cwd` and `commands[].health` are copied as machine-specific
settings, not treated as app IDs. They may need local replacement. The
export response does not probe health URLs or inspect process state.

## Preview and import

Preview with `POST /api/profiles/import/preview` or MCP
`hub_profile_import_preview`. Supply the package as `document`. `app_map`
maps package IDs to local registered IDs; unresolved dependencies block import.
`command_overrides` is keyed by command name and can replace `cmd`, `cwd`, or
`health`, and can add local `env` values (including credentials omitted from
the package). The preview lists dependencies, conflicts, path warnings and
missing local environment values without saving configuration.

Retained `cwd`/`health` fields appear as review warnings and can be saved as-is;
the preview also shows the effective command/argument vector and environment
keys that will be saved. Override machine-specific values with `null` or
suitable local values when needed. Profile names follow the native config's
nonempty-string rule. A name collision requires a new `name` or explicit
`replace: true`; running profiles cannot be replaced.

Import with `POST /api/profiles/import` or MCP `hub_profile_import`, using the
same reviewed fields. Import writes the resolved profile to local `hub.json`.
It never starts an app, opens a desktop window or runs a command. Starting the
profile remains a separate `hub_profile_start` action.

## Design references and limits

Docker Compose profiles group optional services and activate them at runtime;
Compose also validates service dependencies. HoardLink profiles already group
apps, commands and desktop windows, so this feature transfers that existing
model and makes local app-ID dependencies explicit:
[Compose profiles](https://docs.docker.com/reference/compose-file/profiles/).

Hermes provides a useful single-file export/import pattern and explicitly
excludes credential stores; it also refuses to overwrite an existing profile.
HoardLink adopts a preview step and local dependency/path resolution because
its profiles launch this machine's registered Hoard apps and arbitrary
commands:
[Hermes profile export/import](https://hermes-agent.nousresearch.com/docs/reference/profile-commands#hermes-profile-export).

This JSON exchange transfers launch configuration only. It does not package
the referenced apps, install command dependencies, transport shell state, or
provide Compose-style service dependency ordering. The Hub's existing start
and stop behavior remains the runtime authority.
