# Navegación del Hub

El Hub es el punto de acceso a los proyectos, las aplicaciones y los recursos compartidos. Las ágoras pertenecen a sus proyectos.

## Entradas principales

| Entrada | Contenido |
| --- | --- |
| Inicio | Proyectos con su acceso al ágora, aplicaciones en marcha, herramientas compartidas y resumen de Hoy. |
| Proyectos | Directorio de los espacios persistentes del Ágora, con búsqueda por nombre y ruta. |
| Aplicaciones | Catálogo, filtro, grupos de arranque, ventanas, logs y controles de cada aplicación. |
| Comunicación | Buscar, avisos, enlaces, correo y chats. |
| Automatización | Reglas, tareas programadas, trabajos y compras. |
| Sistema | Recursos, eventos, copias, auditoría, repositorios, sesiones de agente, capacidades, web y servicios. |

En móvil, el botón Menú despliega la navegación en el documento. Las herramientas de una categoría también aparecen en su directorio. Una nueva herramienta que no tenga grupo asignado aparece en Sistema.

## Trabajar en un proyecto

**Abrir ágora** utiliza la dirección nativa de Faustus que devuelve `/api/coordination/config` y añade el `space_id` del proyecto. El nombre del proyecto o **Tareas y debates** abre la vista del mismo espacio dentro del Hub. La cabecera muestra el proyecto seleccionado y su enlace al ágora.

La selección y los enlaces conservan el espacio al recargar, volver o cambiar de proyecto. Las tareas, las conversaciones, los formularios y sus borradores conservan el aislamiento existente. **Gestionar espacio** permite crear o renombrar un espacio y abrir la vista clásica de ese proyecto. El espacio `ecosystem` se muestra como un proyecto más, con su nombre real; no sustituye al resto de proyectos.

El directorio muestra los espacios persistentes que devuelve `/api/agora/spaces`. No inventa ágoras para proyectos que aún no tengan espacio ni mueve tareas o conversaciones entre espacios.

## Páginas y desplazamiento

La fila horizontal de pestañas desaparece de la navegación visible. Cada herramienta ocupa el área principal completa. El listado de tareas y la conversación se alternan mediante **Volver**; el panel general, el tablero y los mensajes utilizan el desplazamiento del documento. Los diálogos de edición, logs y vistas especializadas conservan sus controles propios.

GPU, reservas, servidores y carriles se consultan en **Sistema → Recursos**. Los controles globales de arranque y parada están en **Aplicaciones**. Entrar en el Hub, descubrir aplicaciones o abrir el directorio no arranca ningún Hoard.

## Rutas e interfaces

- `#hub/home`, `#hub/projects`, `#hub/apps`: páginas principales.
- `?space_id=<id>#hub/project`: tareas y conversaciones de un proyecto.
- `#hub/<herramienta>`: herramienta compartida, por ejemplo `events` o `resources`.
- `?space_id=<id>#agora-<hilo>`: enlace histórico a una conversación, conservado.
- `?space_id=<id>#agora`: enlace histórico a un tablero, conservado.

Los servicios, la base de datos y las interfaces CLI/HTTP/MCP son los existentes. El cambio afecta a la navegación y a la presentación, sin migración de datos ni cambio de configuración de modelos.

## Verificación

Pruebas de espacios, edición, consola, coordinación y copilot; comprobación real por MCP stdio del catálogo y cuatro tableros aislados; navegación con navegador a 1280 y 390 píxeles, búsqueda y vacío, cambio de proyecto, recarga, conversación, controles de aplicaciones y directorios de todas las herramientas. Las pruebas del navegador están en `tests/hub/test_hub_navigation.py` y `tests/hub/test_agora_ui_browser.py`. La revisión visual utiliza datos de prueba propios; no atribuye nuevos resultados a modelos o agentes.
