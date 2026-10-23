# Navegación del Hub

El Hub es el punto de acceso a los proyectos, las aplicaciones y los recursos compartidos. Las ágoras pertenecen a sus proyectos.

## Entradas principales

| Entrada | Contenido |
| --- | --- |
| Inicio | Apertura de aplicaciones, proyectos para continuar, accesos compartidos y resumen de Hoy. |
| Proyectos | Directorio de los espacios persistentes del Ágora, con búsqueda por nombre y ruta. |
| Aplicaciones | Catálogo con búsqueda y estado, una apertura principal por app y Gestionar para sus controles; grupos de arranque desplegables. |
| Bandeja | Abre Avisos; su selector local permite pasar a Correo, Chats, Enlaces o Buscar. |
| Automatización | Abre Trabajos; su selector local permite pasar a Reglas, Tareas programadas o Compras. |
| Sistema | Abre Recursos (GPU, conexiones, motores y carriles); el selector conserva servicios, capacidades, sesiones, repos, eventos, copias, auditoría y web. |

Las categorías abren directamente una página de trabajo. El selector etiquetado de cada sección cambia de herramienta sin una tira horizontal de pestañas. Una herramienta nueva sin grupo asignado aparece en Sistema. En móvil, Menú despliega las seis entradas y Buscar en el documento. El buscador global lleva a aplicaciones y herramientas del Hub y conserva la búsqueda de contenido existente.

## Trabajar en un proyecto

**Abrir ágora** utiliza la dirección nativa de Faustus que devuelve `/api/coordination/config` y añade el `space_id` del proyecto. El nombre del proyecto o **Tareas y debates** abre la vista del mismo espacio dentro del Hub. La cabecera muestra el proyecto seleccionado y su enlace al ágora.

La selección y los enlaces conservan el espacio al recargar, volver o cambiar de proyecto. Las tareas, las conversaciones, los formularios y sus borradores conservan el aislamiento existente. **Gestionar espacio** permite crear o renombrar un espacio y abrir la vista clásica de ese proyecto. El espacio `ecosystem` se muestra como un proyecto más, con su nombre real; no sustituye al resto de proyectos.

El directorio muestra los espacios persistentes que devuelve `/api/agora/spaces`. No inventa ágoras para proyectos que aún no tengan espacio ni mueve tareas o conversaciones entre espacios.

## Páginas y desplazamiento

La fila horizontal de pestañas desaparece de la navegación visible. Cada herramienta ocupa el área principal completa. El listado de tareas y la conversación se alternan mediante **Volver**; el panel general, el tablero y los mensajes utilizan el desplazamiento del documento. Los diálogos de edición, logs y vistas especializadas conservan sus controles propios.

GPU, reservas, servidores y carriles se consultan en **Sistema → Recursos**. Los controles globales de arranque y parada están en **Aplicaciones**. Entrar en el Hub, descubrir aplicaciones o abrir el directorio no arranca ningún Hoard.

## Interiores y lectura

Todo el Hub utiliza navegación azul marino, lienzo claro frío, superficies blancas y acciones cobalto. La tipografía base es de 15px; los botones globales usan 14px y los controles del contenido y diálogos 15px. La lectura larga usa 16px y los metadatos 12–13px. El rail mide 216px y pasa a Menú hasta 900px; los controles del contenido en móvil mantienen 44px de altura mínima hasta 760px. La consola separada de `apps/agora` conserva su diseño propio.

Trabajos y avisos redistribuyen acciones, fecha y metadatos bajo el contenido cuando falta anchura. Chats coloca el mensaje bajo autor y fecha en móvil, y mantiene las conexiones dentro del desplegable **Fuentes y conexiones** para dejar el historial como zona de lectura. Las referencias usan botones accesibles para los resultados y extremos de una relación; búsqueda y URI largas se ajustan al ancho disponible. Sesiones de agente y reservas distribuyen sus datos sin imponer una fila rígida. A 1400px o menos, repositorios y auditoría pasan de tabla a registros con etiquetas procedentes de sus cabeceras; los registros usan tres columnas y, hasta 760px, dos. El detalle de repositorio conserva el desplazamiento del diálogo, con texto que puede envolver; en móvil apila título y acciones, con controles de al menos 44px. Las fuentes desactivadas de Chats conservan sus controles legibles y explican el estado con texto; los resúmenes y motivos de sesiones usan 14px.

### Correo: bandeja y lector

Correo muestra **una bandeja activa**: **Atención**, **Sin dueño** o **Recientes**, con su recuento. No muestra tres columnas simultáneas. Al cargar por primera vez una esfera elige Atención si tiene mensajes; en su defecto Sin dueño y, por último, Recientes. **Leer ahora**, la configuración, las cuentas y el estado de la última lectura permanecen en la zona de bandeja.

Cada página contiene hasta 20 mensajes de los ya cargados. La carga pide hasta 40 de Atención y 100 Sin dueño de los últimos siete días, además de 40 Recientes; los recuentos, rangos y Anterior/Siguiente corresponden a ese conjunto cargado, no a todo el archivo de las cuentas. **Buscar en esta lista** filtra localmente asunto, nombre, dirección del remitente y extracto; no consulta el cuerpo completo ni busca en el servidor. Sin dueño añade un filtro por categoría. Cambiar de bandeja, categoría o búsqueda vuelve a la primera página. La acción de descartar una categoría abarca todos sus mensajes cargados, aunque la búsqueda sólo muestre algunos, y pide confirmación.

Abrir una fila sustituye el listado por un lector completo en el documento, con asunto, remitente, fecha, esfera, reclamaciones, motivos, enlaces y adjuntos disponibles. **Volver a la bandeja** conserva bandeja, categoría, búsqueda y página, y devuelve el foco al mensaje o a la primera fila disponible. Los controles y el formulario de configuración se ocultan durante la lectura; sus cambios sin guardar se conservan, pero un borrador oculto no se presenta al copilot como formulario pendiente. La carga y los fallos del lector ofrecen Volver y, si falla, Reintentar. Un fallo transitorio del listado conserva los mensajes cargados de la misma esfera y muestra el error con Reintentar.

El lector construye el contenido mediante nodos DOM de texto: separa párrafos, reconoce encabezados sencillos y convierte enlaces HTTP/HTTPS y enlaces con etiqueta en enlaces legibles. No interpreta el HTML del mensaje. Los enlaces conservan la URL como destino/título; los adjuntos muestran nombre y tamaño cuando existen. Las rutas relativas se resuelven contra el Hub y sólo se activan si resultan HTTP/HTTPS; los demás destinos se muestran como texto. No se promete una reproducción completa del formato HTML o Markdown del correo original.

Cambiar de esfera cierra el lector, borra el conjunto anterior y reinicia bandeja y filtros. Las respuestas tardías de listas o mensajes se descartan si pertenecen a una petición superada o a otra esfera, para no mezclar datos entre ámbitos. El sondeo del estado no reemplaza un formulario con cambios sin guardar.

En los ajustes de Avisos, las rutas por esfera pasan en móvil a un bloque por esfera con una fila por prioridad y sus canales. **Editar en Esferas** conserva el acceso a su configuración.

## Editar reglas y programaciones

**Nueva regla** y **Nueva tarea** abren formularios de nombre, activación, condición u horario y acciones. Las acciones permiten iniciar/parar aplicaciones o grupos, publicar eventos, usar herramientas o crear una copia. Los parámetros de herramientas/eventos siguen siendo JSON. **Configuración avanzada · JSON** cambia al documento completo y oculta el formulario visual; al cerrar, reconstruye el formulario conservando los campos avanzados. Un JSON inválido mantiene el editor abierto con el texto y el error. Guardar utiliza la API existente y conserva la programación desactivada cuando así se elige. Navegar por estas páginas no ejecuta las acciones.

## Rutas e interfaces

- `#hub/home`, `#hub/projects`, `#hub/apps`: páginas principales.
- `?space_id=<id>#hub/project`: tareas y conversaciones de un proyecto.
- `#hub/<herramienta>`: herramienta compartida, por ejemplo `events` o `resources`.
- `?space_id=<id>#agora-<hilo>`: enlace histórico a una conversación, conservado.
- `?space_id=<id>#agora`: enlace histórico a un tablero, conservado.

Los servicios, la base de datos y las interfaces CLI/HTTP/MCP son los existentes. El cambio afecta a la navegación y a la presentación, sin migración de datos ni cambio de configuración de modelos.

## Verificación de la navegación inicial

Pruebas de espacios, edición, consola, coordinación y copilot; comprobación real por MCP stdio del catálogo y cuatro tableros aislados; navegación con navegador a 1280 y 390 píxeles, búsqueda y vacío, cambio de proyecto, recarga, conversación, controles de aplicaciones y directorios de todas las herramientas. Las pruebas del navegador están en `tests/hub/test_hub_navigation.py` y `tests/hub/test_agora_ui_browser.py`. La revisión visual utiliza datos de prueba propios; no atribuye nuevos resultados a modelos o agentes.

### Corrección de interiores

`tests/hub/test_hub_mail_client.mjs` cubre el cliente real con una frontera DOM/API controlada: bandeja única, búsqueda y páginas, lector/Volver, texto y enlaces seguros, adjuntos, conservación del borrador, recuperación de errores y respuestas cruzadas al cambiar de esfera. La segunda ronda de QA utiliza fixtures nativas aisladas y pobladas: 100 correos, mensajes de Chats, avisos, referencias, trabajos, compras, un repositorio Git de prueba con ruta/rama larga, auditoría y una sesión con escrituras y detalle. Incluye una fuente de Chats desactivada y ajustes de Avisos abiertos. Las capturas `r2-*.png` a 390, 1280 y 1920px y `round2-metrics.json` están en los entregables de `hub-interiors1261`; las métricas registradas no presentan desbordamiento horizontal ni regiones de lectura con scroll vertical anidado en esas vistas. Estas comprobaciones no acreditan todas las aplicaciones reales ni todos los estados. Una vista vacía acredita únicamente su estado vacío; no sustituye una comprobación con contenido. La aprobación independiente final se registra aparte de esta guía.

## Rediseño completo1269

La entrega sustituye la estructura y el sistema visual de todo el Hub. Las pruebas finales del área dan 128 correctas y las tres suites Node cubren correo, contexto y formularios. MCP stdio verifica cinco operaciones de correo y cuatro tableros por proyecto. La QA aislada utiliza contenido sintético en 21 rutas a 1440/390px e Inicio a 1280px; incluye guardar y reabrir una programación desactivada y cambiar hora fija por intervalo. La revisión independiente requirió corregir la exclusión formulario/JSON y el tablero móvil; el veredicto posterior valora ambos hallazgos como resueltos. Las capturas y límites están en `C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-whole1269`. El detector degradado no acredita contraste completo. Los resultados corresponden a esas pruebas y vistas, no a todos los estados posibles de cada aplicación real.
