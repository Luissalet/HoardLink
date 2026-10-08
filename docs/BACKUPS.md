# Backup source status

`GET /api/backups` and MCP `hub_backup_status` return available `sources` and
`source_errors` separately. A problem in Atlas shared storage does not hide the
snapshot history or prevent inspection and verification of existing snapshots.
Each error identifies its source and explains the configuration/path problem.
The Backups tab displays the problem in English or Spanish alongside the history.

Atlas has two sources: its private metadata (`atlas`) and the configured shared
originals (`atlas-files`). Selecting `atlas` includes both. Selecting
`atlas-files` includes only the originals. The configured storage root must be
an existing safe folder; missing configuration, invalid JSON, relative paths,
missing folders and system/drive roots are reported as unavailable. Validation
happens before resolving a path, so a relative path cannot become the Hub's
working directory. Ordinary apps keep their existing data-folder sources.

`POST /api/backups/run` and MCP `hub_backup_run` refuse the requested backup
before writing a snapshot when one of its selected sources is unavailable.
They return `ok: false`, an error and `source_errors`; they never claim a full
backup after silently omitting Atlas originals. An explicitly selected unrelated
app can still be backed up normally. An empty app selection means the full family.

Shared originals can be restored to a separate review folder. An in-place
restore of `atlas-files` is refused to protect the live shared disk. Source
listing does not repair Atlas configuration or change the user's originals.
