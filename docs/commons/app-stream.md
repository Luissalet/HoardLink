# Pasarela de flujo por aplicación · App stream gateway

## Castellano

Usa el origen del Hub accesible desde tu cliente por el enlace existente. Cada operación es un `POST` bajo `/api/apps/boltagon/stream/` con `Authorization: Bearer <token de familia>`. El Hub resuelve el proveedor registrado. Acepta `cyrano` durante la transición y prefiere la instancia registrada como `boltagon` si existen ambas. Un `404` con `code: "unknown_app"` indica que falta el registro. El campo `error` contiene `"unknown app"` para los clientes que comprueban ese texto.

| Ruta tras `/stream/` | Cuerpo del POST | Respuesta de la aplicación |
| --- | --- | --- |
| `voice/speak` | JSON `{"version":"cyrano.voice/v0","text":"Hola","lang":"es-ES","wait":false}` | Recibo con `job_id`, estado y `undo_token`. |
| `voice/jobs/<job_id>/stream` | Vacío | `application/x-ndjson` con `start`, `audio` y `end`. |
| `voice/jobs/<job_id>/cancel` | Vacío | Estado del trabajo después de pedir la cancelación. |
| `dictation/understand` | JSON `{"version":"cyrano.dictation/v0","audio":{"mime_type":"audio/wav","data_base64":"..."}}` | Resultado de dictado, con el esquema y el contexto opcionales que acepte la app. |

El Hub reenvía los cuerpos y estados válidos de la aplicación. El recibo conserva los enlaces relativos de Boltagon. Construye la URL del flujo o de cancelación en el Hub a partir de `job_id` y usa el mismo token de familia. Consume NDJSON por líneas. Un bloque HTTP puede contener parte de una línea o varias líneas. Lee hasta `end` y comprueba su estado. Cuando se interrumpe la conexión, el Hub corta la lectura y pide `DELETE` del trabajo si la ruta declara esa cancelación. Si cancela el propio cliente tras recibir el identificador, también puede llamar a la ruta explícita. Cerrar la subida de dictado corta el transporte. El contrato de dictado actual no proporciona un identificador de trabajo para cancelar el reconocimiento ya iniciado.

Envía `Content-Type: application/json` y `Content-Length` para los POST con cuerpo. Límites de entrada de 256 KiB para voz y 17 MiB para el JSON de dictado. El contrato de dictado limita el audio decodificado a 12 MiB. Los límites de respuesta son 4 MiB para JSON y 64 MiB para NDJSON, con hasta ocho operaciones activas y cuatro plazas aparte para cancelaciones explícitas por proceso. La carga tiene hasta 60 s y hasta 10 s de inactividad. Tras la carga, el plazo de voz es 65 s al crear, 300 s para el flujo y 13 s al cancelar. Dictado tiene 120 s. Una espera continua de lectura del proveedor puede agotar 60 s. Un fallo antes de las cabeceras devuelve JSON con `code`. Si el fallo ocurre con la respuesta en marcha, el Hub termina el transporte sin completar la respuesta. Cada solicitud se reenvía una vez. Las redirecciones devuelven 502.

El encabezado `X-Hoard-Trace` acepta `<trace_id>/<parent_span>` del contrato `hoard.trace/v1`. El Hub conserva el identificador de traza válido, crea un tramo hijo y envía su cabecera a la aplicación y al cliente. `gateway.stream` mide el tiempo de la pasarela de extremo a extremo, incluida la espera del proveedor. El tramo hijo `gateway.forward_first` mide desde que el Hub recibe el primer bloque de datos hasta que termina de escribirlo hacia el cliente. La pasarela respeta el ajuste de traza del Hub para el origen y el destino. El registrador común recibe ambos mediante `emit_trace_spans` cuando está disponible. En una base anterior los tramos están en el registro de depuración. Los tramos excluyen cuerpos, rutas con identificadores de trabajo y credenciales.

## English

Use the Hub origin reachable through the existing connection. Send all four operations as `POST` under `/api/apps/boltagon/stream/` with `Authorization: Bearer <family token>`. The Hub resolves the registered provider and accepts `cyrano` during the transition. When both IDs are registered, the gateway uses `boltagon`. `404` with `code: "unknown_app"` and `error: "unknown app"` identifies a missing registration.

| Path after `/stream/` | POST body | Application response |
| --- | --- | --- |
| `voice/speak` | JSON `{"version":"cyrano.voice/v0","text":"Hello","lang":"en","wait":false}` | Voice job receipt with `job_id`, state and `undo_token`. |
| `voice/jobs/<job_id>/stream` | Empty | `application/x-ndjson` with `start`, `audio` and `end`. |
| `voice/jobs/<job_id>/cancel` | Empty | Job state after requesting cancellation. |
| `dictation/understand` | JSON `{"version":"cyrano.dictation/v0","audio":{"mime_type":"audio/wav","data_base64":"..."}}` | Dictation result using the application's optional schema and context. |

The gateway forwards valid application bodies and statuses. Job links in the receipt remain app-relative. Use `job_id` to build the Hub stream and cancellation URLs with the same family token. Read NDJSON by line and require an `end` event with the expected state. HTTP blocks need not align with NDJSON lines. When a client disconnects, the Hub closes the upstream connection and calls `DELETE` for a job on a route with declared cancellation. Clients with the job ID can also use the explicit route. Closing a dictation upload closes the transport. The dictation contract has no job ID for stopping recognition that has already started.

Bodies require `Content-Type: application/json` and a `Content-Length` of up to 256 KiB for voice and 17 MiB for the dictation JSON envelope. The dictation contract limits decoded audio to 12 MiB. Response caps are 4 MiB for JSON and 64 MiB for NDJSON, with up to eight active operations per process and four separate slots for explicit cancellations. An upload can take 60 s total with a 10 s idle limit. After upload, the request deadlines are 65 s for voice creation, 300 s for a voice stream, 13 s for cancellation and 120 s for dictation. An upstream read can wait up to 60 s. Before response headers, the Hub reports transport errors as JSON with `code`. Mid-response failures leave the HTTP response incomplete. The gateway forwards each request once and returns 502 for redirects.

`X-Hoard-Trace` accepts `<trace_id>/<parent_span>` from `hoard.trace/v1`. The Hub preserves a valid trace ID, creates a child span and returns its header to the client while forwarding it to the app. `gateway.stream` includes the total gateway time and upstream waiting. Its child `gateway.forward_first` measures the first block from the Hub receiving it to completing the downstream write. The gateway honors Hub trace settings for the caller and callee. It hands the records to `emit_trace_spans` when the shared recorder is present. Older bases log them at debug level. Trace records omit bodies, job IDs and credentials.

## Manifiesto · Manifest

Las apps declaran `x-family.stream_routes` en `faustus-plugin.json`. El bloque es la lista completa de rutas. Una lista vacía desactiva la pasarela para esa app. Cuando el bloque falta, el Hub concede las rutas de voz de la [lista de compatibilidad](../../hoard_link/_data/boltagon-stream-manifest.json) al propietario registrado de `tts` y `dictation/understand` al de `dictation`. El registro resuelve las identidades de transición. La lista no añade una ruta de transcripción independiente para `stt`.

Apps declare `x-family.stream_routes` in `faustus-plugin.json`. An explicit list replaces the full allowlist. An empty list disables streaming for the app. When the block is absent, the compatibility list grants its voice routes to the registered `tts` owner and `dictation/understand` to the registered `dictation` owner. Registry aliases resolve transition identities. The compatibility list provides no separate `stt` route.

Each descriptor binds `route` to a registered local `/api/...` `path` and a `method`. The only parameter is `{job_id}`, a lowercase 32-character hexadecimal ID. `POST` targets declare `body: "json"` or `body: "audio"`. `GET` targets can declare `response: "ndjson"` and link `cancel` to a listed `DELETE` route. Static path segments use lowercase ASCII letters, digits, `_` or `-`. The upstream authority always comes from the registered loopback HTTP origin. The caller's token authenticates to the Hub. The Hub uses the app's own token upstream.
