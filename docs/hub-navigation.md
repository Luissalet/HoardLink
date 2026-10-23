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

Los enlaces contextuales muestran las herramientas y se redistribuyen en varias líneas. En móvil, Menú despliega las entradas y Buscar en el documento. Ayuda y ajustes, visible en la navegación, orienta hacia los controles existentes de conexiones/modelos, avisos, agenda y esferas; no crea una nueva configuración de cuentas.

Inicio consulta `/api/today` y avisos sin ver, muestra hasta cinco pendientes y ofrece la agenda completa y Actualizar. Si los avisos no se pueden consultar, lo indica. La continuidad muestra hasta tres espacios según las visitas recordadas en `localStorage` de este navegador: no son archivos recientes ni una medida de actividad del servidor. Las herramientas muestran hasta cuatro aplicaciones, dando prioridad a las que ya están en marcha. Entrar no arranca ninguna.

En Aplicaciones, las categorías son Crear y escribir, Organizar y consultar, Analizar, Vida cotidiana, Equipo y modelos y Otras herramientas. Las entradas conocidas tienen descripciones ES/EN y sinónimos de búsqueda en español: «recetas» o «escribir una novela» ayudan a encontrar herramientas sin recordar su nombre. No es búsqueda semántica ni un inventario auditado de todas las capacidades. Los manifiestos desconocidos siguen visibles, con su propósito/nombre y capacidades reales como fallback.

## Trabajar en un proyecto

El nombre del proyecto abre su resumen: tareas pendientes del tablero y conversaciones de ese espacio, con accesos directos al detalle. Los enlaces **Resumen** y **Ágora · tareas y conversaciones** mantienen una entrada clara al mismo trabajo. Las listas muestran hasta cinco tareas pendientes y tres conversaciones; no se anuncian archivos recientes ni actividad que no proporcione el tablero. **Ubicación y ajustes del proyecto** despliega la carpeta y acceso a la gestión del espacio.

Dentro del ágora, **Abrir conversación completa** utiliza la dirección nativa de Faustus que devuelve `/api/coordination/config` y conserva el `space_id`. No se fabrica un destino cuando falta esa configuración. La selección y los enlaces conservan el espacio al recargar, volver o cambiar de proyecto. Las tareas, conversaciones, formularios y borradores mantienen su aislamiento. **Nuevo proyecto** accede a la creación de un espacio; no crea un proyecto paralelo de Faustus.

El directorio muestra `/api/agora/spaces`, no el catálogo completo nativo de Faustus. No inventa ágoras para proyectos sin espacio ni mueve sus datos. `ecosystem` conserva su nombre y es un espacio más.

## Páginas y desplazamiento

La fila horizontal de pestañas desaparece de la navegación visible. Cada herramienta ocupa el área principal completa. El listado de tareas y la conversación se alternan mediante **Volver**; el panel general, el tablero y los mensajes utilizan el desplazamiento del documento. Los diálogos de edición, logs y vistas especializadas conservan sus controles propios.

Conexiones y motores aparecen antes de GPU, reservas y carriles en **Sistema → Estado y conexiones**. La página explica que una aplicación parada puede estar disponible bajo demanda y que un servicio sin confirmar necesita comprobarse, sin darlo por fallido. Los controles globales de arranque y parada están en **Aplicaciones**. Entrar en el Hub, descubrir aplicaciones o abrir el directorio no arranca ningún Hoard.

## Interiores y lectura

Todo el Hub utiliza navegación blanca, lienzo neutro verde, superficies blancas y acciones verdes. `workspace.css`, posterior a `navigation.css` e `interiors.css`, es la autoridad visual final. La base es 16px/1.55, el título 34px (30px en móvil) y las secciones 21px (20px en móvil); controles y lectura especializada conservan sus tamaños propios. El rail mide 224px y pasa a Menú hasta 900px; los controles del contenido conservan 44px mínimos hasta 760px. La consola separada de `apps/agora` mantiene su contrato propio.

Trabajos y avisos redistribuyen acciones, fecha y metadatos bajo el contenido cuando falta anchura. Chats coloca el mensaje bajo autor y fecha en móvil, y mantiene las conexiones dentro del desplegable **Fuentes y conexiones** para dejar el historial como zona de lectura. Las referencias usan botones accesibles para los resultados y extremos de una relación; búsqueda y URI largas se ajustan al ancho disponible. Sesiones de agente y reservas distribuyen sus datos sin imponer una fila rígida. A 1400px o menos, repositorios y auditoría pasan de tabla a registros con etiquetas procedentes de sus cabeceras; los registros usan tres columnas y, hasta 760px, dos. El detalle de repositorio conserva el desplazamiento del diálogo, con texto que puede envolver; en móvil apila título y acciones, con controles de al menos 44px. Las fuentes desactivadas de Chats conservan sus controles legibles y explican el estado con texto; los resúmenes y motivos de sesiones usan 14px.

### Correo: bandeja y lector

Correo muestra **una bandeja activa**: **Atención**, **Sin dueño** o **Recientes**, con su recuento. No muestra tres columnas simultáneas. Al cargar por primera vez una esfera elige Atención si tiene mensajes; en su defecto Sin dueño y, por último, Recientes. **Leer ahora**, la configuración, las cuentas y el estado de la última lectura permanecen en la zona de bandeja.

Cada página contiene hasta 20 mensajes de los ya cargados. La carga pide hasta 40 de Atención y 100 Sin dueño de los últimos siete días, además de 40 Recientes; los recuentos, rangos y Anterior/Siguiente corresponden a ese conjunto cargado, no a todo el archivo de las cuentas. **Buscar en esta lista** filtra localmente asunto, nombre, dirección del remitente y extracto; no consulta el cuerpo completo ni busca en el servidor. Sin dueño añade un filtro por categoría. Cambiar de bandeja, categoría o búsqueda vuelve a la primera página. La acción de descartar una categoría abarca todos sus mensajes cargados, aunque la búsqueda sólo muestre algunos, y pide confirmación.

Abrir una fila sustituye el listado por un lector completo en el documento, con asunto, remitente, fecha, esfera, reclamaciones, motivos, enlaces y adjuntos disponibles. **Volver a la bandeja** conserva bandeja, categoría, búsqueda y página, y devuelve el foco al mensaje o a la primera fila disponible. Los controles y el formulario de configuración se ocultan durante la lectura; sus cambios sin guardar se conservan, pero un borrador oculto no se presenta al copilot como formulario pendiente. La carga y los fallos del lector ofrecen Volver y, si falla, Reintentar. Un fallo transitorio del listado conserva los mensajes cargados de la misma esfera y muestra el error con Reintentar.

El lector construye el contenido mediante nodos DOM de texto: separa párrafos, reconoce encabezados sencillos y convierte enlaces HTTP/HTTPS y enlaces con etiqueta en enlaces legibles. No interpreta el HTML del mensaje. Los enlaces conservan la URL como destino/título; los adjuntos muestran nombre y tamaño cuando existen. Las rutas relativas se resuelven contra el Hub y sólo se activan si resultan HTTP/HTTPS; los demás destinos se muestran como texto. No se promete una reproducción completa del formato HTML o Markdown del correo original.

Cambiar de esfera cierra el lector, borra el conjunto anterior y reinicia bandeja y filtros. Las respuestas tardías de listas o mensajes se descartan si pertenecen a una petición superada o a otra esfera, para no mezclar datos entre ámbitos. El sondeo del estado no reemplaza un formulario con cambios sin guardar.

En los ajustes de Avisos, las rutas por esfera pasan en móvil a un bloque por esfera con una fila por prioridad y sus canales. **Editar en Esferas** conserva el acceso a su configuración.

## Editar reglas y programaciones

**Programar una acción** lleva a programaciones; **Cuando ocurra algo**, a reglas. Los formularios conservan nombre, activación, horario/condición y acciones. Las cinco novedades conocidas son: enlace nuevo en un seguimiento, inicio de una aplicación, parada de una aplicación, un agente guarda un cambio y un agente deshace un cambio. Solo pueden responder a eventos que las aplicaciones publiquen realmente; no se inventa cobertura para todas las herramientas.

Crear una copia de seguridad, iniciar/parar/reiniciar una aplicación e iniciar/parar un grupo usan campos normales y nombres del catálogo, sin introducir IDs ni JSON. Las herramientas de aplicaciones/Hub y la publicación de eventos siguen siendo opciones avanzadas con identificadores y parámetros JSON. Otros eventos conservan su configuración técnica. **Configuración avanzada · JSON** alterna con el formulario visual; un JSON inválido mantiene texto y error para corregirlo. Guardar usa la API existente. Navegar por estas páginas no ejecuta las acciones.

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

La entrega cambia Inicio, continuidad de proyectos, descubrimiento por propósito, automatización y lectura de avisos/búsqueda, además del sistema visual. El registro inicial corresponde a dca9cbe1d832432a9679d49e356b5a2ab7ccedc5 y está pendiente de conciliar la revisión independiente y sus correcciones. La crítica 1296 se conserva con sus hallazgos; documentar una solución no equivale a marcarla verificada.

La tanda amplia del autor/raíz obtuvo 214 PASS y 4 FAIL en 633.49 s. Los cuatro recorridos heredados se adaptaron y su ejecución focal posterior pasó 4 casos en 33.54 s; los flujos/etiquetas nuevos pasaron 4 casos en 39.86 s. Cuatro suites Node pasaron. El MCP stdio nativo realizó ocho lecturas de tres espacios aislados, con cantidades y ámbito comprobados. Son ejecuciones distintas; no se anuncia una fullsuite verde. No se arrancaron aplicaciones/modelos reales ni se guardaron automatizaciones reales para estas pruebas.

Hay 14 capturas de escritorio/móvil en `C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-user-workspace1301`. Sus viewports incluyen 1270x893 o1280x900 en escritorio y 380x822 en móvil; los controles DOM de desbordamiento a 390px son evidencia separada. La captura móvil de intención de aplicación quedó invalidada por cambio de viewport y está pendiente de sustitución. El detector se ejecutó una vez en modo DEGRADED regex: 43 avisos orientativos (18 colores/25 tamaños) contra el DESIGN anterior. La actualización del contrato registra los colores/rampas y escalas intencionales, sin repetir el detector ni certificar contraste. No se acredita todo el catálogo nativo Faustus, toda automatización personalizada ni todos los estados de aplicaciones reales.
