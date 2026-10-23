# Navegación del Hub

El Hub es el punto de acceso a los proyectos, las aplicaciones y los recursos compartidos. Las ágoras pertenecen a sus proyectos.

## Entradas principales

| Entrada | Contenido |
| --- | --- |
| Inicio | Pendientes de Hoy y avisos sin ver, proyectos visitados en este navegador y accesos a herramientas. |
| Proyectos | Directorio de espacios persistentes del Ágora, con búsqueda por nombre/ruta, creación y resumen de cada proyecto. |
| Aplicaciones | Descubrimiento por propósito y categorías, búsqueda, estado y Gestionar; conserva catálogo y grupos de arranque. |
| Bandeja | Abre Avisos; enlaces visibles permiten pasar a Correo, Chats, Relaciones y enlaces o Buscar. |
| Automatización | Empieza por Programar una acción, Cuando ocurra algo, Consultar ejecuciones o Compras y pedidos. |
| Sistema | Abre Estado y conexiones; conserva servicios, capacidades, actividad de agentes, código/repositorios, eventos, copias, diagnóstico y web. |

Los enlaces contextuales muestran las herramientas y se redistribuyen en varias líneas. Las facetas nuevas con pestaña que no tengan grupo asignado aparecen bajo Sistema. Es un fallback para pestañas futuras: Esferas sigue siendo un control de cabecera, no una ruta de pestaña. En móvil, Menú despliega las entradas y Buscar en el documento. Ayuda y ajustes, visible en la navegación, orienta hacia los controles existentes de conexiones/modelos, avisos, agenda y esferas; no crea una nueva configuración de cuentas.

Inicio consulta `/api/today` y avisos sin ver, muestra hasta cinco pendientes y ofrece la agenda completa y Actualizar. Si los avisos no se pueden consultar, lo indica. La continuidad muestra hasta tres espacios según las visitas recordadas en `localStorage` de este navegador: no son archivos recientes ni una medida de actividad del servidor. Las herramientas muestran hasta cuatro aplicaciones, dando prioridad a las que ya están en marcha. Entrar no arranca ninguna.

En Aplicaciones, las categorías son Crear y escribir, Organizar y consultar, Analizar, Vida cotidiana, Equipo y modelos y Otras herramientas. Las entradas conocidas tienen descripciones ES/EN y sinónimos de búsqueda en español: «recetas» o «escribir una novela» ayudan a encontrar herramientas sin recordar su nombre. No es búsqueda semántica ni un inventario auditado de todas las capacidades. Los manifiestos desconocidos siguen visibles, con su propósito/nombre y capacidades reales como fallback. Las descripciones conocidas reflejan el producto: Dorian trata contexto personal y reflexiones con sus fuentes; Pygmalion prepara datasets y desarrolla/evalúa modelos; Atlas organiza archivos y carpetas compartidos de proyectos; Vitruvius trabaja con software, diseño y código; Galton evalúa modelos desde el estudio de Pygmalion. Estas etiquetas no incorporan nuevas capacidades. Si una aplicación no puede iniciarse, se explica si falta configuración de inicio o si la actual no permite arrancar y se dirige a **Gestionar → Detalles**, donde se conserva el motivo técnico original.

## Trabajar en un proyecto

El nombre del proyecto abre su resumen: tareas pendientes del tablero y conversaciones de ese espacio, con accesos directos al detalle. Los enlaces **Resumen** y **Ágora · tareas y conversaciones** mantienen una entrada clara al mismo trabajo. Las listas muestran hasta cinco tareas pendientes y tres conversaciones; no se anuncian archivos recientes ni actividad que no proporcione el tablero. **Ubicación y ajustes del proyecto** despliega la carpeta y acceso a la gestión del espacio.

Dentro del ágora, **Abrir conversación completa** utiliza la dirección nativa de Faustus que devuelve `/api/coordination/config` y conserva el `space_id`. No se fabrica un destino cuando falta esa configuración. La selección y los enlaces conservan el espacio al recargar, volver o cambiar de proyecto. Al cambiar de proyecto desde el ágora se mantiene la vista Ágora del proyecto elegido. Las tareas, conversaciones, formularios y borradores mantienen su aislamiento. **Nuevo proyecto** accede a la creación de un espacio; no crea un proyecto paralelo de Faustus.

El directorio muestra `/api/agora/spaces`, no el catálogo completo nativo de Faustus. No inventa ágoras para proyectos sin espacio ni mueve sus datos. `ecosystem` conserva su nombre y es un espacio más.

## Responder a una pregunta pendiente

En el ágora del proyecto, **Te esperan** muestra el título, origen, fecha y pregunta completa de cada escalación. **Responder aquí** abre un campo junto a esa pregunta; **Ver hilo** conserva el acceso a la conversación completa. **Enviar** publica un comentario y reabre la escalación, sin finalizar su tarea. **Responder y cerrar** resuelve el hilo y solo se ofrece si no es una tarea. Ambas acciones usan los caminos nativos autenticados de publicación/resolución, como Luis desde la interfaz humana.

El envío conserva el proyecto e hilo que tenía al pulsarlo, aunque se cambie de proyecto mientras responde el servidor. Los controles se desactivan durante el envío para evitar duplicados. Un fallo muestra el error y conserva el texto para reintentar; **Cancelar** oculta el campo sin borrar su borrador. Actualizar, cambiar de idioma o pasar a otro proyecto y volver conservan el borrador propio de cada proyecto/hilo en esta sesión de página. No se promete persistencia tras recargar el navegador. Ctrl/Cmd+Intro envía. Las lecturas tardías de la bandeja se descartan para no revertir el estado posterior a un envío o cambio de proyecto.

Preguntas pendientes, conversaciones y tablero se cargan en paralelo. La bandeja de pantalla pide un mensaje ordinario y conserva las escalaciones completas; peticiones simultáneas comparten la lectura en curso. El tablero selecciona por SQL las tareas no finalizadas antes de hidratar, incluidas las abiertas, y conserva quince finalizadas y los recuentos completos del espacio. No se ha añadido API ni herramienta MCP ni se evita la autenticación.

## Páginas y desplazamiento

La fila horizontal de pestañas desaparece de la navegación visible. Cada herramienta ocupa el área principal completa. El listado de tareas y la conversación se alternan mediante **Volver**; el panel general, el tablero y los mensajes utilizan el desplazamiento del documento. Los diálogos de edición, logs y vistas especializadas conservan sus controles propios.

Conexiones y motores aparecen antes de GPU, reservas y carriles en **Sistema → Estado y conexiones**. La página explica que una aplicación parada puede estar disponible bajo demanda y que un servicio sin confirmar necesita comprobarse, sin darlo por fallido. Los controles globales de arranque y parada están en **Aplicaciones**. Entrar en el Hub, descubrir aplicaciones o abrir el directorio no arranca ningún Hoard.

## Interiores y lectura

Todo el Hub utiliza el tema oscuro oficial de la familia Hoard: carbón cálido, texto marfil, acciones doradas y marca con la pila serif familiar. La corrección 1363 responde al rechazo expreso de Luis de la identidad clara de 1301, conservando sus flujos. `html[data-hoard-app="hub"]` selecciona los tokens de `hoard_link/ui/hoard-theme.css`, que no se ha modificado. `workspace.css`, posterior a `navigation.css` e `interiors.css`, conserva la autoridad de distribución y componentes, sin volver a sobrescribir la paleta con blanco/verde. El `app-icon.png` original se empaqueta byte a byte como `hoard_link/hub/ui/hub-icon.png` para cabecera y favicon (SHA256 `7d39b4930c47485506ab6e6bb2889b581224f79c811ec27c068e7b1f1802ee9d`); no se ha generado ni editado el raster. Los diálogos, campos y estados mantienen superficies oscuras. La base es 16px/1.55, el título 34px (30px en móvil) y las secciones 21px (20px en móvil); controles y lectura especializada conservan sus tamaños propios. El rail mide 224px y pasa a Menú hasta 900px; los controles del contenido conservan 44px mínimos hasta 760px. La consola separada de `apps/agora` mantiene su contrato propio.

Trabajos y avisos redistribuyen acciones, fecha y metadatos bajo el contenido cuando falta anchura. Chats coloca el mensaje bajo autor y fecha en móvil, y mantiene las conexiones dentro del desplegable **Fuentes y conexiones** para dejar el historial como zona de lectura. Las referencias usan botones accesibles para los resultados y extremos de una relación; búsqueda y URI largas se ajustan al ancho disponible. Sesiones de agente y reservas distribuyen sus datos sin imponer una fila rígida. A 1400px o menos, repositorios y auditoría pasan de tabla a registros con etiquetas procedentes de sus cabeceras; los registros usan tres columnas y, hasta 760px, dos. El detalle de repositorio conserva el desplazamiento del diálogo, con texto que puede envolver; en móvil apila título y acciones, con controles de al menos 44px. Las fuentes desactivadas de Chats conservan sus controles legibles y explican el estado con texto; los resúmenes y motivos de sesiones usan 14px.

### Correo: bandeja y lector

Correo muestra **una bandeja activa**: **Atención**, **Sin dueño** o **Recientes**, con su recuento. No muestra tres columnas simultáneas. Al cargar por primera vez una esfera elige Atención si tiene mensajes; en su defecto Sin dueño y, por último, Recientes. **Leer ahora**, la configuración, las cuentas y el estado de la última lectura permanecen en la zona de bandeja.

Cada página contiene hasta 20 mensajes de los ya cargados. La carga pide hasta 40 de Atención y 100 Sin dueño de los últimos siete días, además de 40 Recientes; los recuentos, rangos y Anterior/Siguiente corresponden a ese conjunto cargado, no a todo el archivo de las cuentas. **Buscar en esta lista** filtra localmente asunto, nombre, dirección del remitente y extracto; no consulta el cuerpo completo ni busca en el servidor. Sin dueño añade un filtro por categoría. Cambiar de bandeja, categoría o búsqueda vuelve a la primera página. La acción de descartar una categoría abarca todos sus mensajes cargados, aunque la búsqueda sólo muestre algunos, y pide confirmación.

Abrir una fila sustituye el listado por un lector completo en el documento, con asunto, remitente, fecha, esfera, reclamaciones, motivos, enlaces y adjuntos disponibles. **Volver a la bandeja** conserva bandeja, categoría, búsqueda y página, y devuelve el foco al mensaje o a la primera fila disponible. Los controles y el formulario de configuración se ocultan durante la lectura; sus cambios sin guardar se conservan, pero un borrador oculto no se presenta al copilot como formulario pendiente. La carga y los fallos del lector ofrecen Volver y, si falla, Reintentar. Un fallo transitorio del listado conserva los mensajes cargados de la misma esfera y muestra el error con Reintentar.

El lector construye el contenido mediante nodos DOM de texto: separa párrafos, reconoce encabezados sencillos y convierte enlaces HTTP/HTTPS y enlaces con etiqueta en enlaces legibles. No interpreta el HTML del mensaje. Los enlaces conservan la URL como destino/título; los adjuntos muestran nombre y tamaño cuando existen. Las rutas relativas se resuelven contra el Hub y sólo se activan si resultan HTTP/HTTPS; los demás destinos se muestran como texto. No se promete una reproducción completa del formato HTML o Markdown del correo original.

Cambiar de esfera cierra el lector, borra el conjunto anterior y reinicia bandeja y filtros. Las respuestas tardías de listas o mensajes se descartan si pertenecen a una petición superada o a otra esfera, para no mezclar datos entre ámbitos. El sondeo del estado no reemplaza un formulario con cambios sin guardar.

En los ajustes de Avisos, las rutas por esfera pasan en móvil a un bloque por esfera con una fila por prioridad y sus canales. **Editar en Esferas** conserva el acceso a su configuración.

## Editar reglas y programaciones

**Programar una acción** lleva a programaciones; **Cuando ocurra algo**, a reglas. Los formularios conservan nombre, activación, horario/condición y acciones. Las cinco novedades conocidas son: enlace nuevo en un seguimiento, inicio de una aplicación, parada de una aplicación, un agente guarda un cambio y un agente deshace un cambio. Solo pueden responder a eventos que las aplicaciones publiquen realmente; no se inventa cobertura para todas las herramientas. Para inicio/parada, **Aplicación afectada** elige qué aplicación provoca la regla: al seleccionarla se guarda el origen Hub y la condición `where["data.app"]`; no se confunde la aplicación con el emisor del evento. Se conservan otros filtros avanzados.

Crear una copia de seguridad, iniciar/parar/reiniciar una aplicación e iniciar/parar un grupo usan campos normales y nombres del catálogo, sin introducir IDs ni JSON. Las herramientas de aplicaciones/Hub y la publicación de eventos siguen siendo opciones avanzadas con identificadores y parámetros JSON. Otros eventos conservan su configuración técnica. Dentro del editor abierto, cambiar el tipo de una acción y volver recupera el borrador propio de ese tipo: aplicación, herramienta, grupo, evento y parámetros. Esto no promete persistencia tras cerrar o cancelar el editor. **Configuración avanzada · JSON** alterna con el formulario visual; un JSON inválido mantiene texto y error para corregirlo. Guardar usa la API existente. Navegar por estas páginas no ejecuta las acciones.

## Avisos y búsqueda

Cada aviso conserva título, estado, origen, fecha y acciones; el cuerpo se presenta primero como extracto de hasta 180 caracteres, con espacios/Markdown simplificados. Al desplegarlo se ve todo el cuerpo original como texto, sin convertirlo en un resumen nuevo ni prometer formato enriquecido.

La búsqueda global combina destinos locales de aplicaciones/proyectos/herramientas con la búsqueda de contenido existente. Los problemas de fuentes tienen una explicación y detalles técnicos desplegables; el fallo general conserva una salida para reintentar. La ausencia de coincidencias no demuestra que una fuente desactivada se haya consultado. Los resultados locales no requieren arrancar aplicaciones.

## Rutas e interfaces

- `#hub/home`, `#hub/projects`, `#hub/apps`: páginas principales.
- `?space_id=<id>#hub/project`: resumen de un proyecto.
- `?space_id=<id>#hub/agora`: tablero, tareas y conversaciones del mismo proyecto.
- `#hub/automation`, `#hub/help`, `#hub/today`: intención de automatización, ayuda/ajustes y agenda completa.
- `#hub/<herramienta>`: herramienta compartida, por ejemplo `events` o `resources`.
- `?space_id=<id>#agora-<hilo>`: enlace histórico a una conversación, conservado.
- `?space_id=<id>#agora`: enlace histórico a un tablero, conservado.

Los servicios, la base de datos y las interfaces CLI/HTTP/MCP son los existentes. El cambio afecta a la navegación y a la presentación, sin migración de datos ni cambio de configuración de modelos.

## Verificación de la navegación inicial

Pruebas de espacios, edición, consola, coordinación y copilot; comprobación real por MCP stdio del catálogo y cuatro tableros aislados; navegación con navegador a 1280 y 390 píxeles, búsqueda y vacío, cambio de proyecto, recarga, conversación, controles de aplicaciones y directorios de todas las herramientas. Las pruebas del navegador están en `tests/hub/test_hub_navigation.py` y `tests/hub/test_agora_ui_browser.py`. La revisión visual utiliza datos de prueba propios; no atribuye nuevos resultados a modelos o agentes.

### Corrección de interiores

`tests/hub/test_hub_mail_client.mjs` cubre el cliente real con una frontera DOM/API controlada: bandeja única, búsqueda y páginas, lector/Volver, texto y enlaces seguros, adjuntos, conservación del borrador, recuperación de errores y respuestas cruzadas al cambiar de esfera. La segunda ronda de QA utiliza fixtures nativas aisladas y pobladas: 100 correos, mensajes de Chats, avisos, referencias, trabajos, compras, un repositorio Git de prueba con ruta/rama larga, auditoría y una sesión con escrituras y detalle. Incluye una fuente de Chats desactivada y ajustes de Avisos abiertos. Las capturas `r2-*.png` a 390, 1280 y 1920px y `round2-metrics.json` están en los entregables de `hub-interiors1261`; las métricas registradas no presentan desbordamiento horizontal ni regiones de lectura con scroll vertical anidado en esas vistas. Estas comprobaciones no acreditan todas las aplicaciones reales ni todos los estados. Una vista vacía acredita únicamente su estado vacío; no sustituye una comprobación con contenido. La aprobación independiente final se registra aparte de esta guía.

## Rediseño completo 1269

La entrega sustituye la estructura y el sistema visual de todo el Hub. Las pruebas finales del área dan 128 correctas y las tres suites Node cubren correo, contexto y formularios. MCP stdio verifica cinco operaciones de correo y cuatro tableros por proyecto. La QA aislada utiliza contenido sintético en 21 rutas a 1440/390px e Inicio a 1280px; incluye guardar y reabrir una programación desactivada y cambiar hora fija por intervalo. La revisión independiente requirió corregir la exclusión formulario/JSON y el tablero móvil; el veredicto posterior valora ambos hallazgos como resueltos. Las capturas y límites están en `C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-whole1269`. El detector degradado no acredita contraste completo. Los resultados corresponden a esas pruebas y vistas, no a todos los estados posibles de cada aplicación real.

## Replanteamiento 1301: evidencia y límites

La entrega cambia Inicio, continuidad de proyectos, descubrimiento por propósito, automatización y lectura de avisos/búsqueda, además del sistema visual. El registro inicial corresponde a dca9cbe1d832432a9679d49e356b5a2ab7ccedc5, con correcciones posteriores en `b1b2225f202b76175b8514b9e8942d0814723730`. La revisión independiente aprobó submission_revision 2 sobre `785c61e2e307ab47749a93e928fdfc9516583f89` con SHIP acotado al lote F1–F5/R1. La crítica 1296 conserva el diagnóstico original y añade seguimiento de lo efectivamente verificado; no se recalcula su puntuación ni se presenta como un nuevo estudio de usabilidad.

La tanda amplia del autor/raíz obtuvo 214 PASS y 4 FAIL en 633.49 s. Los cuatro recorridos heredados se adaptaron y su ejecución focal posterior pasó 4 casos en 33.54 s; los flujos/etiquetas nuevos pasaron 4 casos en 39.86 s. Cuatro suites Node pasaron. El MCP stdio nativo realizó ocho lecturas de tres espacios aislados, con cantidades y ámbito comprobados. Son ejecuciones distintas; no se anuncia una fullsuite verde. No se arrancaron aplicaciones/modelos reales ni se guardaron automatizaciones reales para estas pruebas.

La tanda inicial reúne 14 capturas de escritorio/móvil en `C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-user-workspace1301`. Sus viewports incluyen 1270x893 o 1280x900 en escritorio y 380x822 en móvil; los controles DOM de desbordamiento a 390px son evidencia separada. La captura móvil inicial de intención de aplicación quedó invalidada por cambio de viewport. La sustituye `fix1-mobile-app-intent.png` (380×822) con `fix1-mobile-app-intent-full.png` como documento completo de 380×1327; `fix1-mobile-rule-form.png` muestra el formulario a 380×822. Son capturas de la corrección, no una nueva certificación de todas las vistas. El detector se ejecutó una vez en modo DEGRADED regex: 43 avisos orientativos (18 colores/25 tamaños) contra el DESIGN anterior. La actualización del contrato registra los colores/rampas y escalas intencionales, sin repetir el detector ni certificar contraste. No se acredita todo el catálogo nativo Faustus, toda automatización personalizada ni todos los estados de aplicaciones reales.

### Comprobaciones de las correcciones

La raíz registra `fix1-workflows-final.txt`: cuatro pruebas focales correctas en 17.15 s, y nuevas pasadas Node F1/F2/F3/F4 correctas. Se citan por separado de las tandas anteriores y no sustituyen una suite completa. El documentador ha leído el diff de la corrección, sin repetir QA de producto. La revisión independiente volvió a ejecutar cuatro casos correctos en 15.60 s y tres suites Node sobre el freeze aprobado. F1–F4 y R1 quedaron resueltos. F5 retira la afirmación de que Esferas hubiera perdido una ruta de pestaña: era un control de cabecera; el fallback para futuras pestañas queda confirmado por inspección de fuente. La revisión fue single-context DEGRADED y no repitió MCP, suite amplia ni detector. Los clientes sintéticos produjeron ConnectionAbortedError 10053 al desconectarse de /api/services, sin fallo de aserción; no se presenta como salida limpia. Las seis capturas sustitutas se inspeccionaron, sin reutilizar la móvil inválida. El informe íntegro es `review1301-r2.md` en el mismo directorio de evidencia. La integración se registra aparte.


## Corrección1363: identidad de familia y respuesta junto a la pregunta

La fuente exacta 260805ebbbd2c89b3f7b4d4ba3eaa77fce0fe437 + 0f52044d04f515a40958a4c99cf795334a0a6f49 recibió un nuevo SHIP/APPROVE independiente en submission_revision 2. El voto no hereda el de 1301. La raíz integró esos hashes exactos por fast-forward y recargó solo Hub con app:hub; cinco assets servidos, incluido el icono, coincidían byte a byte con main. No arrancó Hoards/modelos ni publicó una respuesta real como Luis.

La tanda de área del autor obtuvo 60 PASS / 4 FAIL / 1 SKIP en 162.80 s. Los cuatro fallos esperaban la ruta heredada #hub/project; tras adaptar recorridos a #hub/agora y al selector, el focal final pasó 7 casos en 36.49 s (los cuatro anteriores y tres nuevos). Una tanda ampliada distinta pasó 3 casos en 14.44 s; tres suites Node pasaron. MCP stdio nativo realizó 3 lecturas en 2 espacios aislados con el mismo backend antes del último cambio solo frontend 0f52044; no se repitió después. El primer ensayo temporal también expuso exclusión de tareas abiertas y selectores erróneos del fixture, corregidos antes de 260805e y con sus logs conservados. No se suman tandas distintas ni se anuncia una suite completa verde.

El revisor retuvo revision 2 antes de probar y ejecutó 3 pytest PASS en 8.91 s más 3 Node PASS propios. Comprobó la pregunta antes del tablero retenido, identidad y bytes del icono, respuesta inline, fallo 503/reintento, borrador por proyecto/idioma, tarea abierta y resolución no-task a 1280/390. Inspeccionó las ocho capturas del autor; no abrió un navegador interactivo propio, repitió MCP ni actuó sobre datos reales. No encontró defectos materiales en el alcance revisado. `submission.txt` y `review-1363.txt` conservan los informes íntegros en `C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-family1363`.

Las ocho imágenes válidas son JPEG: `desktop-quick-reply.jpg`, `user-1486-quick-reply.jpg`, `mobile-quick-reply-full.jpg` (380×3035), `mobile-answer-form.jpg` (380×822), `desktop-home.jpg`, `desktop-apps.jpg`, `desktop-rule-dialog.jpg` y `mobile-rule-dialog.jpg`. Los anchos reales de escritorio incluyen 1270/1476; no se equiparan automáticamente con los viewports CSS 1280/1486. `mobile-quick-reply.png` (106×374) es inválida y queda excluida. `mobile-quick-reply-valid.jpg` es un reemplazo válido 380×822 del encuadre superior, sin acreditar por sí solo el formulario o documento completo. Algunos JPEG tenían extensión PNG inicialmente y se renombraron conservando sus bytes.

El único detector dio DEGRADED regex, 130 avisos orientativos (110 fuente, 7 radio, 9 color, 4 sidetab) contra el contrato claro anterior. Este pase documental actualiza roles, escalas y rampas de previsualización; no repite detector ni contexto. Las cuatro muestras calculadas de contraste 14.34/8.3/9.02/15.16 de `contrast-samples.json` son roles representativos: no certifican todos los estados, controles desactivados, accesibilidad o disponibilidad de una fuente concreta en todos los equipos. La crítica 1296 mantiene su registro histórico y no se cierra globalmente.

Tras integrar y recargar Hub, la raíz registró en `live-after.json` tablero 1,863 s en la primera lectura y 0,946 s en la segunda, 303375 B / 53 tareas (38 no finalizadas + 15 finalizadas), frente a 3,442 s / 308527 B / 52 tareas de la observación previa. El recuento finalizado pasó 959→964 con trabajo concurrente. Inbox 0,137 s / 9815 B frente a 0,137 s / 12542 B; su carga ya no espera al tablero. Son observaciones locales con datos y caché cambiantes, no un benchmark estadístico ni una comparación uniformemente en frío. El directorio sigue sin ser el catálogo nativo Faustus completo; las herramientas/eventos personalizados siguen siendo opciones técnicas avanzadas.

La comprobación real de la raíz por CUA abrió y canceló el compositor inline sin escribir ni enviar. Inspeccionó `live-agora-reply.jpg`: fondo calculado rgb(28,26,20), icono original, ámbito ecosystem, formulario visible junto a la pregunta y detalle completo cerrado. Al acabar, las 48 aplicaciones registradas estaban paradas y el SHA de configuración del Hub seguía exacto. Es evidencia de la raíz, no una nueva ejecución del revisor ni del documentador.
