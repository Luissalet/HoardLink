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

La colocación determina dónde se comprueba cada aplicación. Para Faustus,
Hermes, sus compatibilidades y las demás apps de la Spark se usa la salud
de esa máquina. Si la caché falta, caduca o es anterior al traslado, el
comprobador pide la misma lectura pasiva que usa `hub_placement_status`
y espera dentro del plazo de la sonda. `evidence.location` vale `remote`
y el texto añade «en otra máquina». En el PC, `manually_stopped` indica
una parada manual y `no_listener` indica que el puerto registrado carece
de escucha. Ambos conservan `state: stopped`. Una parada en la Spark
se comunica como fallo de la comprobación remota.

Para los destinos en marcha se pueden consultar las rutas GET de salud,
catálogo y capacidades del copiloto de Faustus. El comprobador llama a
`nightingale_modules` si su catálogo anuncia `readOnlyHint: true`. El
catálogo del Hub y MCP `tools/list` se generan con la misma definición.

- `catalogue` comprueba los nombres publicados en un catálogo. Es el alcance de las sondas de Atlas, Mercator, las compatibilidades y los servicios comunes con dueño en una app.
- `declaration` comprueba el registro de una capacidad, como el relay MCP. `prerequisites` comprueba los requisitos publicados o el catálogo de los extremos. Agenda, Hoy y Búsqueda usan este alcance.
- `cached` y `recorded_receipt` usan observaciones previas. La consola busca procesos huérfanos en los espacios cargados.
- Las filas de correo `2.1`, `2.13` y `4.5.mail` reutilizan la última pasada correcta de `mail.db` y la comparan con el intervalo. Si todavía falta una pasada al arrancar, la fila confirma que se pudo leer el estado de la pasarela y muestra «Sin pasada desde el arranque (hace N s)». Superado el plazo inicial y el intervalo, la ausencia de pasada cuenta como fallo.
- Kafka y People muestran la última lectura ordinaria de la pasarela y su resultado. Si aún falta esa lectura, un acuse persistido de una reclamación o un vigilante de correo puede acreditar una lectura anterior. Hermes usa los acuses de flota. La identificación de llamantes muestra si existen sus declaraciones y la última identificación correcta que ha observado este proceso del Hub.
- La cuota publica las horas de registro y medida y su vigencia por proveedor. El Dojo publica el recuento observado en la última operación habitual `hub_dojo list` de la colocación activa. Hasta el primer listado válido, falta ese recuento.
- Las copias de correo se comparan con los bytes actuales y con el historial publicado de cada archivo canónico. Una coincidencia anterior responde con el número de versiones de diferencia. `versions_behind_N` cuenta las copias a esa distancia. Solo quedan en rojo los bytes que no coinciden con ninguna versión de un historial completo.
- El puente al Cerebro combina la salud de Faustus con los recuentos de la cola. Los relevos de producción se verifican en su orden correspondiente.

Cada ID del registro añade las filas `1.2.app.<id>` (catálogo) y
`1.3.app.<id>` (proxy). También se desglosan los IDs de compatibilidad
registrados. Baird conserva su exclusión en la fila `9.1`. En el PC,
una parada manual o la ausencia de escucha se detectan antes de leer
el catálogo o invocar herramientas. Para la transcripción, la fila
`4.5.stt` comprueba el catálogo de Funes en la ruta de compatibilidad
de Talos. Una respuesta válida acredita ese catálogo aunque el registro
de procesos no reconozca un servicio independiente en el puerto.

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
fallo. Una comprobación omitida o la espera de la primera pasada de correo
conserva su fecha.

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

Placement determines which machine the checker uses. For apps on the
Spark and their compatibility IDs, it uses placement health and requests
the same passive refresh as `hub_placement_status` when the cache is
absent, stale or older than the move. The result carries
`evidence.location: remote` and shows “on another machine”. On the PC,
`manually_stopped` means the person stopped the app and `no_listener`
means its registered port has no listener. Both keep `state: stopped`.
An inactive remote service fails its remote check.

The checker uses reviewed GET routes and the allowlisted
`nightingale_modules` read when its schema advertises `readOnlyHint: true`.
The Brain bridge check uses service health and queue counts. Spark
revision checks compare the deployment record with local Git HEAD.
Production handoffs are checked through their own orders.

- Mail rows `2.1`, `2.13` and `4.5.mail` read the last successful pass from `mail.db`. If a pass is still due after startup, the gateway state responds with “No pass since startup (N s ago)”. Once the initial delay and interval have elapsed, the missing pass is a failure.
- Kafka and People show their latest ordinary gateway read and its outcome. A stored claim or fleet mail watcher can provide evidence of an earlier read. Hermes uses fleet receipts. Caller identification shows whether registry declarations exist and the last successful identification observed by the current Hub process.
- Quota evidence contains measurement timestamps and freshness by provider. Dojo evidence contains the exam count from the latest ordinary `hub_dojo list` call to the active placement, once one has been observed.
- A vendored mail copy matching an older published revision responds with its version lag. `versions_behind_N` counts copies with that lag. A copy is marked as a failure when its bytes match no revision in a complete history.

Each registry ID gets a catalogue row (`1.2.app.<id>`) and a proxy row
(`1.3.app.<id>`), including registered compatibility IDs. Baird remains excluded in
row `9.1`. Both checks use passive running state before sending a request.
The `4.5.stt` row checks Funes's catalogue at the Talos compatibility
route, so a valid catalogue can confirm the transcription tools even
when the process registry does not recognize a standalone service.

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
response or confirmed stop ends a recorded failure. A skipped check or the
gateway's initial wait for its first pass preserves its date.
