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
Faustus, además de la herramienta de lectura `nightingale_modules` de
Nightingale. Los resultados guardan códigos, recuentos y marcas de tiempo.
El catálogo del Hub y MCP `tools/list` se generan con la misma definición.

- `catalogue` comprueba los nombres publicados en un catálogo. Es el alcance de las sondas de Atlas, Mercator, las compatibilidades y los servicios comunes con dueño en una app.
- `declaration` comprueba el registro de una capacidad, como el relay MCP. `prerequisites` comprueba los requisitos publicados o la salud de los extremos. Agenda, Hoy y Búsqueda usan este alcance.
- `cached` y `recorded_receipt` usan observaciones previas. La pasarela de correo compara la última pasada correcta con su intervalo. Los lectores requieren un acuse de correo en la flota. La colocación usa la salud remota registrada y el estado del túnel. La consola busca procesos huérfanos en los espacios cargados.
- El puente al Cerebro combina la salud de Faustus con los recuentos de la cola. Los relevos de producción se verifican en su orden correspondiente.

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
`nightingale_modules` read and operational metadata. Mail readers need
recorded mail receipts. Spark revision checks compare the deployment record
with local Git HEAD. Brain bridge checks use the service health and queue
counts. Production handoffs are checked through their own orders.

For additional exclusions, set `{"excluded_apps":["atlas"]}` in
`link-check.json` in the Hub data directory. Baird and port 5232 are
excluded by default. The probe and run budgets are eight and 120 seconds.
The Hub retains up to 20 completed runs in `link-check-history.json`. A
response or confirmed stop ends a recorded failure. A skipped check preserves
its date.
