# Compact Agora lists

Agents can use `hub_agora_tasks` through MCP or the worker's `agora_read` tool.
For example, this asks for the approved tasks updated on 23 November in a
time zone one hour ahead of UTC.

```json
{
  "action": "tasks",
  "args": {
    "space_id": "studio-test",
    "status": "approved",
    "since": "2026-11-23T00:00:00+01:00",
    "until": "2026-11-24T00:00:00+01:00",
    "fields": ["id", "title", "status", "repo", "updated"]
  }
}
```

Pass the inner `args` directly to the MCP tool. For `GET /api/agora/tasks`,
use comma-separated `fields` and URL-encode offsets such as `%2B01:00`.
The existing `status` (including `active`), `owner`, `repo` and `kind` filters
combine with the date range.

- `since` includes the start and `until` excludes the end. Either is optional.
  Use an ISO timestamp with `Z` or an explicit offset, or a date `YYYY-MM-DD`
  for midnight UTC. The returned dates are Unix timestamps in seconds.
- `date_field` defaults to `updated`. Use `created` for creation, or `closed_at`
  for closure. A task without a closure date is excluded from a closure window.
  To count approvals by last change, use `status: "approved"` and `updated`.
- Use `compact: true` to request task summaries without a date filter.
  A date filter also selects this view. It returns `id`, `title`, `status`,
  `repo`, `owner`, `updated`, `closed_at` and `submission_revision`.
  Supply `fields` to override this selection. Use `hub_agora_task` to open
  an individual full record. If selected, `activity` contains the
  store's task activity.
- Use `count_only: true` for the total matching count. The total covers the
  entire filtered set, independent of the page size and cursor.
- Compact and projected pages default to 50 entries and cap at 100.
  A page can end earlier to stay within the reader's output budget. Feed
  `next_cursor` back with the same filters until it is null. Tasks are ordered
  by descending ID. New higher IDs and changes to sort fields do not shift the
  cursor. Each page evaluates the filters against the current task values.
  If a selected field makes one entry too large, request fewer fields or
  open that task separately.
- Calls using only the original list arguments keep the original full task
  records, sort order and numeric offset cursor.

`hub_agora_decisions` and `GET /api/agora/decisions` support the same compact,
projection, date and count arguments. With a date window, `updated` uses the
thread's last update and `created` uses the thread's creation date. Both use
the receipt time for production decisions. A page covers resolved threads
first and production decisions second, descending by ID in each source. The
two arrays share the page limit and continuation. Each entry includes the
requested fields available on its source. The compact view omits resolution
text, history, production notes and full receipts. Calls with only the original
arguments keep the original decision log.

## Consultas compactas del Ágora

El cronista puede llamar a `agora_read` con `action: "tasks"` y los argumentos
del ejemplo. La misma consulta se puede hacer con `hub_agora_tasks` o con
`GET /api/agora/tasks`, usando comas para `fields` y codificando el signo
`+` del desfase horario como `%2B`.

- `since` incluye el comienzo y `until` excluye el final. Aceptan fecha y hora
  ISO con `Z` o desfase explícito. Una fecha `YYYY-MM-DD` sola empieza a
  medianoche UTC. Las fechas devueltas son segundos desde la época Unix.
- `date_field` admite `updated` (por defecto), `created` y `closed_at`. Las
  tareas sin fecha de cierre quedan fuera de una ventana de cierre. Para
  consultar las aprobadas por fecha de cambio, usa `status: "approved"`
  junto con `updated`. Se pueden combinar los filtros de estado, dueño,
  repositorio y tipo.
- `compact: true` y las consultas con fecha muestran una ficha por tarea
  con ID, título, estado, repositorio, dueño, cambio, cierre y revisión de
  entrega. `fields` elige campos concretos. `count_only: true` devuelve el
  total del filtro, con independencia del tamaño de página y el cursor.
- Las páginas compactas tienen 50 filas por defecto y 100 como máximo, y
  pueden acabar antes por tamaño. Repite los filtros con `next_cursor`
  hasta que sea nulo. El orden es por ID descendente y los filtros se evalúan
  con los valores vigentes en cada página. Si una fila supera el límite,
  pide menos campos o abre esa tarea con `hub_agora_task`.
- `hub_agora_decisions` acepta los mismos argumentos de proyección, fechas,
  recuento y paginación. En los hilos, `updated` se refiere al último cambio y
  `created` a la creación. En decisiones de producción, ambos usan la fecha
  del recibo. El cursor recorre primero hilos resueltos y luego decisiones de
  producción, por ID descendente dentro de cada grupo. Los dos grupos comparten
  el tamaño de página. Se devuelven los campos solicitados que existan en
  cada grupo.
- Las llamadas que solo usan los argumentos originales conservan los
  registros completos, el orden y la paginación originales.
