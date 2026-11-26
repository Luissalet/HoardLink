# Comprobador de enlaces de la familia

Llama a `hub_link_check` con argumentos vacíos para iniciar una pasada. La
herramienta devuelve un `run_id` y la comprobación sigue en segundo plano.
Mientras haya una pasada activa, las llamadas para iniciar otra devuelven el
mismo identificador. Consulta el resultado con `{"run_id":"…"}` o
`GET /api/link-check?run_id=…`. El GET requiere el token del Hub y, sin
`run_id`, devuelve la pasada activa o la última disponible.

Cada fila conserva el ID, origen, destino, vía y clasificación del inventario.
Las compatibilidades y los servicios comunes tienen comprobaciones individuales
y un resumen en su fila principal. `state` usa los valores estables `responds`,
`not_responding`, `stopped` y `not_checked`. `status` se presenta en castellano
de España o en inglés con `lang=es` o `lang=en`. `evidence` contiene la
operación, el resultado resumido y el alcance de la observación. Los recuentos
de la pasada incluyen todas las filas y `checked_at` y `failure_since` se
expresan en UTC.

El estado de las aplicaciones se obtiene del registro, las paradas manuales
y las escuchas locales o la caché remota del Hub. Para los destinos en marcha,
la lista de sondas admite GET de salud, catálogo y capacidades del copiloto de
Faustus, además de la herramienta de lectura `nightingale_modules` cuando su catálogo HTTP la anuncia
con `readOnlyHint: true`. Los resultados guardan códigos, recuentos y marcas de tiempo.
El catálogo del Hub y MCP `tools/list` se generan con la misma definición.

- `catalogue` comprueba los nombres publicados en un catálogo. Es el alcance de las sondas de Atlas, Mercator, las compatibilidades y los servicios comunes con dueño en una app.
- `declaration` comprueba el registro de una capacidad, como el relay MCP. `prerequisites` comprueba los requisitos publicados o el catálogo de los extremos. Agenda, Hoy y Búsqueda usan este alcance.
- `cached` y `recorded_receipt` usan observaciones previas. La pasarela de correo compara la última pasada correcta con su intervalo. Los lectores requieren un acuse de correo en la flota. La colocación usa la salud remota registrada y el estado del túnel. La consola busca procesos huérfanos en los espacios cargados.
- El puente al Cerebro combina la salud de Faustus con los recuentos de la cola. Los relevos de producción se verifican en su orden correspondiente.

Cada ID del registro añade las filas `1.2.app.<id>` (catálogo) y
`1.3.app.<id>` (proxy). También se desglosan los IDs de compatibilidad
registrados. Baird conserva su exclusión en la fila `9.1`. Una parada
manual, una ausencia de escucha local o una parada remota confirmada da
«parado» antes de leer el catálogo o invocar herramientas.

- `manifest_catalogue` usa el mismo cliente HTTP que `hub_app_tools`.
  Compara los nombres como conjuntos y cuenta los declarados, servidos,
  ausentes y no declarados. Si hay un `bridge.json` del propietario, se
  usan los nombres de su catálogo de compatibilidad. Los controles del
  manifiesto (`x-family.module_controls`) solo permiten un cotejo parcial.
  Los catálogos sin lista completa se distinguen de una coincidencia exacta.
- `app_proxy` usa el transporte de `hub_call_app` mediante el contrato HTTP
  compartido y el token de la app. Exige `annotations.readOnlyHint: true`
  en el catálogo servido y un esquema de objeto sin argumentos requeridos.
  Se comprueba de nuevo el estado pasivo antes del POST. Si falta una sonda
  válida, la fila dice «no comprobado: sin sonda». Con una sonda disponible,
  la fila conserva la operación y el código HTTP. Se descarta el contenido de la respuesta.

El manifiesto `faustus-plugin.json` de la aplicación puede declarar su
catálogo HTTP exacto y una lectura de salud en `x-family.link_check`:

```json
{
  "x-family": {
    "link_check": {
      "tools": ["sample_health", "sample_read", "sample_update"],
      "health_tool": "sample_health"
    }
  }
}
```

`tools` contiene todos los nombres HTTP de la variante servida. La
comprobación de catálogo enumera herramientas aunque sean de escritura.
Solo la lectura identificada como `health_tool` se ejecuta, con `{}`.
Debe devolver `{"ok":true}` mediante la ruta compartida y funcionar sin
arrancar módulos, consultar datos de la persona o activar otros servicios.
El Hub también reconoce las lecturas de estado de módulos de Daguerre,
Prospero y Vitruvius, que devuelven una lista `modules`.

Para excluir más aplicaciones, crea `link-check.json` en la carpeta de datos
del Hub con un objeto como `{"excluded_apps":["atlas"]}`. La exclusión se
aplica también a los alias del propietario. Baird y el puerto 5232 están
excluidos en la configuración base.

Cada sonda dispone de ocho segundos y la pasada reserva 120 segundos para las
sondas. `link-check-history.json` guarda hasta 20 pasadas y conserva la fecha
del primer fallo observado. Una respuesta o parada confirmada cierra ese
fallo. Una comprobación omitida conserva su fecha.

## Family link checker

Call `hub_link_check` with empty arguments to start a background run.
Concurrent calls share the active run. Poll with `{"run_id":"…"}` or
`GET /api/link-check?run_id=…` and the Hub bearer token. Omitting `run_id`
on GET returns the active or latest recorded run. The Hub serves the same
tool definition through `GET /api/agent/tools` and MCP `tools/list`.

Each row includes its inventory ID, source, destination, route and inventory
classification. Results include observation scope, operation, summarized
evidence, UTC timestamp and first observed failure time. Counts cover all
reported rows. Use `lang=en` for English or `lang=es` for Spanish.
A catalogue observation checks published tool names. A declaration
observation checks registration. A cached observation reports earlier
evidence.

The checker uses passive app state, reviewed GET routes, the allowlisted
`nightingale_modules` read when its served schema advertises `readOnlyHint: true`,
and operational metadata. Mail readers need
recorded mail receipts. Spark revision checks compare the deployment record
with local Git HEAD. Brain bridge checks use the service health and queue
counts. Production handoffs are checked through their own orders.

Each registry ID gets a catalogue row (`1.2.app.<id>`) and a proxy row
(`1.3.app.<id>`), including registered compatibility IDs. Baird remains
excluded in row `9.1`. Both checks use passive running state before any
request and report confirmed stops as «stopped».

The catalogue uses the same HTTP client as `hub_app_tools`. It compares
sets of names against the app manifest or the owner's compatibility
`bridge.json`, and counts missing and unexpected names. Module controls
declared in the manifest allow a partial comparison. An incomplete
declaration cannot establish an exact match. The proxy uses the shared
HTTP transport of `hub_call_app`, checks passive state again before the
call, and discards the response body from the evidence. It calls a health
tool only if the served schema declares `annotations.readOnlyHint: true`
and has no required arguments. Missing probes show «not checked: no probe».

In the app manifest, `x-family.link_check.tools` lists all names in the
HTTP catalogue and `x-family.link_check.health_tool` selects the health
tool. That tool accepts `{}` and returns `{"ok":true}` without starting
modules, reading personal content or contacting other services. The Hub
also recognizes the existing module-state reads of Daguerre, Prospero and
Vitruvius, whose replies include a `modules` list.

For additional exclusions, set `{"excluded_apps":["atlas"]}` in
`link-check.json` in the Hub data directory. Baird and port 5232 are
excluded by default. The probe and run budgets are eight and 120 seconds.
The Hub retains up to 20 completed runs in `link-check-history.json`. A
response or confirmed stop ends a recorded failure. A skipped check preserves
its date.
