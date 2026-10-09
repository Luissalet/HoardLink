# Heartbeat and lock renewal

Heartbeat always records the valid actor's state and activity independently of which of its locks should be renewed. `POST /api/agora/heartbeat`, the native MCP tool `hub_agora_heartbeat` and the CLI share the same optional `renew_resources` array:

- Omitted: preserve the existing behaviour and renew every live lock owned by this actor.
- `[]`: record heartbeat metadata without renewing locks.
- A list: renew only those exact normalized resource rows that are still live and owned by this actor. A parent path does not match descendants. Foreign, absent and expired rows are not renewed or created.

`lock_renewal` reports `mode: all|selected` and the actual `renewed_resources`. Existing TTL values are not changed; each selected expiry becomes current time plus that row's TTL. Invalid selector types or resource names remain code errors, with no partial metadata write.

CLI: `agora --as actor hb "work" --renew-resource path:Repo/src/file.py` (repeat the option), or `--no-renew-locks` for metadata only. These mutually exclusive flags also apply to `sync`, which passes the selector through its heartbeat. Existing commands without either flag keep legacy behaviour.

The live `locks` response advertises `capabilities.heartbeat_selective_renewal: true`. This comes from the running backend, not merely the local stdio MCP catalogue, which can be newer than an already-running Hub. Clients that promise exact scoped renewal verify both the live capability and MCP selector schema, then check the returned receipt. Native clients retain legacy compatibility when their snapshot contains no owned outside lock; this cannot guarantee selective renewal across concurrent lock changes on an old backend. With outside locks and no selective contract, they return a partial result without dispatch or a fabricated heartbeat acknowledgement. Upgrade/reconnect is recoverable and requests no owner approval. Deploy the Hub first and refresh the catalogue for exact renewal.

This does not authorize new resources, change ownership or mint leases. Controllers that own model reservations can renew them separately with the same general API; model identifiers receive no special exception. Isolated validation uses real API/MCP/CLI and SQLite expiries, plus a normal HTTP model and browser, without GPU inference.
