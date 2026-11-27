# Monitor capture policy / Política de captura de monitor

This Hub library supplies Baird's worker factory `hoard_link.capture_policy:create`. It is off by default. An operator prepares the same SQLite database used by Talos's screen-memory settings and exclusions. The factory uses an explicit file path and never discovers an installation, starts an app or provisions a database by itself.

## Preparing the integration

- Back up the intended screen-memory database and provision it through `provision(Path(...), monitor, layout)` for the exact stable display ID and layout hash supplied by Baird. The path must be absolute and the existing table must contain a saved boolean `private` setting. The grant journal and triggers belong to this integration. Talos still owns its existing settings and exclusions and uses its existing writes.
- The worker's launcher must make this Hub checkout/package available in its Python import path, set `HOARD_CAPTURE_POLICY_ENABLED=1` and `HOARD_CAPTURE_POLICY_DB` to that same database, and select `BAIRD_MONITOR_POLICY_PROVIDER=hoard_link.capture_policy:create`. Baird's local screen confirmation still applies.
- Missing or inconsistent values, a changed display or layout, missing fences, unresolved acquisitions and failed database access deny permission. The enabled configuration supports one scoped producer at a time. With capture disabled and no unsettled acquire, `remove(Path(...))` removes the integration's triggers and tables and leaves the original settings and exclusion rules intact.

## Privacy ordering

A permit checks the selected monitor, layout, policy revision and medium. It records the outstanding permission durably before the physical call. The writer transaction makes an ordinary Talos edit wait while the call completes. Baird keeps its context until its audio helper acknowledges the call or its exit is confirmed, even if a deadline expires and termination fails. A successful return or an ordinary failure reported after settlement clears the receipt. If a `TimeoutError` escapes the context or the worker goes away before acknowledging, the receipt persists. The database rejects privacy and exclusion edits while the receipt is pending. A write that hits the durable fence or the SQLite busy timeout has not been confirmed. The provider cannot receive a late acknowledgement once the caller has left. An unresolved receipt requires operator recovery with proof that the producer and its helper can no longer act on it. Reopening the database, changing a timeout or disabling the factory does not clear it.

Any enabled Talos exclusion currently blocks capture of both picture and loopback for the entire display. A foreground or title sample cannot order a compositor visibility change with whole-display acquisition. This conservative behavior also applies to exclusions for an application that happens to be closed. The provider copies no app names, window titles, pictures or audio. Test the production setup and its latency with the actual monitor only in the separately authorized hardware check.

## Castellano

La fábrica `hoard_link.capture_policy:create` está apagada por defecto. Dirección prepara la base de memoria de pantalla de Talos con `provision`, para el identificador y la disposición exactos del monitor. El lanzador habilita el proveedor y pasa la ruta absoluta de esa misma base al trabajador de Baird. La fábrica falla cerrada ante una base ausente, una política desconocida, un cambio de disposición o un permiso pendiente. No se inicia ningún servicio al cargarla.

Cada adquisición pide permiso con la revisión vigente. El permiso queda guardado antes de la llamada física. El productor espera al acuse del ayudante de audio o a la confirmación de que ha salido, incluso si vence el plazo y falla la terminación. Talos puede confirmar un cambio de modo privado o de exclusiones cuando se han resuelto los permisos ya emitidos. Si sale un `TimeoutError` del contexto o se interrumpe el productor antes del acuse, el registro sigue bloqueando aperturas y cambios hasta una recuperación autorizada con los procesos implicados detenidos. `remove` revierte la integración cuando el permiso está resuelto.

Mientras el productor mire un monitor completo, cualquier exclusión habilitada bloquea vídeo y audio. Los cambios de ventana del compositor requieren otro mecanismo de sincronización antes de permitir exclusiones por visibilidad. Los datos de verificación deben ser sintéticos.
