# Order sources and job ownership

In **Orders → New order → Source dossier**, choose a Drucker engagement, a Writer project and chapter, or a Coase
company dossier. The reader uses the app's existing bridge, with the app running and the relevant module enabled.
It leaves the app's record in place. **Dossier file** also accepts saved JSON results from those source tools,
including Coase's backend evidence packet. File selection works with the source app closed. Both paths use the
[production dossier](order-dossier.md) contract and carry the original payload, its SHA-256 and the source identity
to each job. The same form accepts an order without a native dossier.

For agents, `hub_agora_order_create` accepts `dossier.native` directly. `hub_agora_plan_set` accepts a station's app
ID as `jobs[].station` and an optional `jobs[].repo` repository name. A discovered app supplies the default
repository. Use an explicit name for a job that works in a different repository. `hub_station_catalog` shows
repositories with its handoffs. Jobs and their Agora tasks carry the recorded metadata. Updates return the normal
`hub_agora_order_undo` token.

## Isolated browser QA

With a separate data directory, roots and port, set `HOARD_HUB_ISOLATED_CATALOG=1` on the test Hub process. Its
browser header receives empty backend and service catalogues and a local response for the Copilot assets, and
Faustus status uses an empty probe list. App discovery and the selected apps' real proxy routes still use the test
roots. Leave the instance on fresh placement data with isolated app ports and tokens. This setting covers browser
startup discovery. For offline QA, set `HOARD_AGORA_ACCOUNT_USAGE=0` to skip the separate account quota reporter.
Explicit model calls and service controls remain separate.
