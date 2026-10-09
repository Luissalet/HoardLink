# Lectura reciente de un hilo

`hub_agora_thread_recent` devuelve una ventana reciente sin descargar todo el
historial ni marcar el hilo como leído. La lectura antigua
`hub_agora_thread` conserva su contrato y su ACK opcional con `agent`.

El nuevo tool admite sólo:

- `thread_id`: entero positivo obligatorio.
- `limit`: entero de 1 a 200, por defecto 20.
- `before_id`: entero positivo opcional; devuelve mensajes con id estrictamente menor.

El API equivalente es `GET /api/agora/threads/{thread_id}/recent`, con `limit`
y `before_id` como parámetros de consulta. Los números se convierten desde
texto en el API; MCP y el método nativo exigen enteros, sin coercionar
booleanos. `agent` y otros parámetros no declarados se rechazan.

La consulta usa el índice existente `(thread_id, id)` y `ORDER BY id DESC
LIMIT limit+1`. El resultado contiene como máximo `limit` mensajes, devueltos
en orden cronológico. `pagination.has_more` indica que existen mensajes
anteriores; `next_before_id` es el id del más antiguo de esta página cuando
hay más, y null al terminar. Un nuevo mensaje durante la paginación no
desplaza las páginas anteriores ni introduce duplicados. Consultar de nuevo
sin `before_id` vuelve a la ventana más reciente.

Los cuerpos tienen un máximo de 20.000 caracteres por mensaje y 200.000 por
página. El presupuesto se asigna primero a los mensajes más recientes.
Cada mensaje incluye `body_chars` y `body_digest` SHA256 del cuerpo original
UTF-8, y `body_truncated` indica si `body` está incompleto. Estos son límites
de caracteres del cuerpo, además de los límites de campos ya existentes;
no se presentan como un límite de bytes de todo el transporte.

Un cuerpo truncado no prueba una lectura completa. Los consumidores deben
conservarlo como pendiente e indicar `recent_detail_unavailable`, nunca
registrar un ACK o fabricar un digest de lectura completa a partir del
extracto. Esta API no añade un lector de fragmentos para cuerpos legacy
excepcionalmente grandes. Los mensajes publicados por los tools actuales
ya limitan sus cuerpos a 20.000 caracteres, aunque una página con muchos
cuerpos largos puede agotar el presupuesto acumulado.

La respuesta conserva metadatos actuales de hilo y tarea. `stances` describe
sólo esta página y se etiqueta con `stances_scope: "page"`. La lectura no
modifica `reads`, `acks`, heartbeat ni cursor de inbox y devuelve
`hub_acknowledged: false`. Un cursor de páginas antiguas debe mantenerse
separado del cursor de inbox: ver lo reciente no cubre el historial omitido.

Verificación: SQLite real con paginación y publicación concurrente; consulta
acotada sin usar el lector completo; cuerpos legacy grandes y presupuesto;
API HTTP en Hub temporal y proceso MCP stdio real con su token QA. El Hub de
producción no se reinicia ni se usa para estas escrituras sintéticas.
