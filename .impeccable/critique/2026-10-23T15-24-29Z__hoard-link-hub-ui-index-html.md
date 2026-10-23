---
target: "Recorrer el Hub como usuario: confusión, descubrimiento y carencias"
total_score: 18
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
target_identity: "file:C:\\Users\\luism\\Documents\\Codex\\2026-10-23\\es\\work\\hub-walkthrough1296\\hoard_link\\hub\\ui\\index.html"
target_fingerprint: "sha256:5da7f776ed74e7fe4d3e3c82bf33a3817c7a6194e281d543721f126f0f672e8d"
target_path: "C:\\Users\\luism\\Documents\\Codex\\2026-10-23\\es\\work\\hub-walkthrough1296\\hoard_link\\hub\\ui\\index.html"
timestamp: 2026-10-23T15-24-29Z
slug: hoard-link-hub-ui-index-html
---
Method: dual-agent (A: independent live design walkthrough; B: independent browser evidence and one degraded detector run), followed by a parent walkthrough and synthesis.

# Hub: recorrido como usuario — 23 octubre 2026

El Hub sigue exigiendo conocer demasiado del sistema. La navegación principal ha mejorado, pero encontrar una capacidad, retomar un proyecto o interpretar un estado sigue requiriendo traducir nombres internos. Esta es una inspección experta de la interfaz viva, no un estudio con usuarios ni una medida de tiempos o conversión.

## Recorrido y límites

Se recorrieron Inicio, Proyectos y su tablero, Aplicaciones, Bandeja, búsqueda, Automatización y Sistema en http://127.0.0.1:8810/. Se abrieron formularios y se cancelaron. No se guardaron reglas o tareas, se ejecutaron acciones, se arrancaron servicios ni se alteraron correos. Escritorio; móvil, accesibilidad completa por teclado y errores forzados pendientes de evaluar. No se comparó el catálogo completo de proyectos nativos de Faustus.

## Qué funciona

- Las seis secciones principales permiten orientarse mejor que la antigua fila de pestañas.
- Abrir e Iniciar y abrir comunican la acción de una aplicación con claridad.
- Programar una copia diaria tiene campos comprensibles de nombre, frecuencia y hora, y se puede cancelar sin guardar.

## Prioridades

### P1 · No encuentro una función si no recuerdo su nombre

En Aplicaciones, «recetas» no devuelve CookHoard y «novela» no devuelve Writer. Por nombre sí aparecen. Las descripciones largas en inglés no ayudan a quien busca una tarea en español. En búsqueda global, «escribir una novela» devolvió correo irrelevante sin orientar a la aplicación adecuada.

Consecuencia: puedo concluir que el producto no hace algo que sí hace. Cambio: búsqueda por intención y sinónimos en español; descripciones breves orientadas a tareas; resultados separados entre aplicaciones, proyectos y contenido. Criterio de éxito: recetas, novela y escribir encuentran las capacidades pertinentes sin conocer sus nombres propios. Comandos apropiados: $impeccable clarify y $impeccable shape.

### P1 · Automatizar todavía exige programar

La pantalla inicial de Automatización es un historial técnico. Para programar hay que descubrir el selector Trabajos/Reglas/Tareas/Compras. Nueva regla pide links.watch.new como evento y trae Publicar un evento, example.reaction y Parámetros (JSON) en el formulario normal, fuera de Configuración avanzada. Una tarea sencilla sí se entiende; cambiar a una herramienta exige identificadores y JSON. Se observó además hub_backup_run conservado tras cambiar de tipo de acción sin aplicación elegida.

Consecuencia: un formulario visual aparenta facilidad, pero no permite construir una regla sin conocimientos de API. Cambio: entradas «Programar una acción» y «Cuando ocurra algo»; catálogo de eventos y acciones con etiquetas humanas, campos según la herramienta y resumen antes de guardar. Mantener códigos en modo experto. Criterio de éxito: construir una regla habitual sin introducir identificadores ni JSON. Comandos: $impeccable shape y $impeccable clarify.

### P1 · La bandeja obliga a leer demasiado para decidir

Avisos muestra resúmenes muy extensos, marcas Markdown literales y mezcla de idiomas. Un primer resumen puede ocupar casi toda la pantalla; lo siguiente queda enterrado. La interfaz no ofrece una primera lectura breve y priorizada de lo pendiente.

Consecuencia: entrar a comprobar novedades se convierte en leer documentos. Cambio: filas de asunto, origen, fecha, estado y dos líneas de resumen; detalle al abrir; pendientes y acción requerida antes del historial. Criterio de éxito: identificar qué requiere atención sin abrir cada elemento ni recorrer sus cuerpos completos. Comandos: $impeccable distill y $impeccable layout.

### P2 · Entrar en un proyecto no explica dónde retomar el trabajo

Proyectos ofrece Abrir ágora y Tareas y debates; la segunda ruta lleva a un tablero llamado Ágora de proyecto que vuelve a ofrecer Abrir ágora. En Silver se ven Tareas, Conversaciones, Agentes, Sesiones sin asignar, Bloqueos y Decisiones. Las tarjetas priorizan rutas de disco y no muestran el último trabajo o siguiente paso. La colección corresponde a espacios persistentes del Ágora; el nombre Proyectos no aclara esa relación. No se ha demostrado que falten proyectos concretos.

Consecuencia: el usuario tiene que decidir cuál es la entrada real y entender la coordinación de agentes para orientarse. Cambio: una entrada canónica por proyecto, con resumen, actividad reciente, asuntos pendientes y acceso claro a su ágora. El Ágora pertenece al proyecto; el Hub global agrega atención y accesos. Archivos y conversaciones recientes son necesidades a validar con las capacidades reales, no funcionalidades que se den por existentes. Comando: $impeccable shape.

### P2 · Los estados muestran datos sin ayudar a actuar

Sistema muestra unknown, no disponible y parado sin explicar si es normal o impide trabajar. Automatización muestra un trabajo «sin noticias» de más de 26 horas. Una búsqueda inexistente mezcla Sin resultados con seis errores module_disabled y nombres de fuentes técnicas; no propone cómo recuperarse. Conexiones queda debajo de GPU en Recursos; Sistema tiene nueve destinos en un selector.

Consecuencia: no sé si esperar, ignorarlo, reintentar o arreglar algo. Cambio: estado en lenguaje humano, impacto y siguiente acción; separar falta de coincidencias de fuentes desactivadas; resumen de salud antes del diagnóstico técnico. Comandos: $impeccable clarify y $impeccable harden.

## Estructura que falta

Inicio debe responder «qué necesita mi atención» y «dónde sigo»: proyectos recientes, pendientes y accesos a herramientas, con estado del sistema resumido. Proyectos debe ser el lugar para retomar cada trabajo y entrar en su propia ágora. Aplicaciones debe permitir descubrir por propósito. Automatización debe comenzar por crear o consultar rutinas entendibles. Sistema debe concentrar administración y diagnóstico, con detalles técnicos progresivos. Ayuda y ajustes necesitan una ruta visible; no se encontraron en las vistas inspeccionadas, lo que no demuestra su ausencia global.

No basta con cambiar colores ni sustituir pestañas por selectores: esconder nueve destinos reduce lo visible, pero no resuelve su descubrimiento. Visualmente hay una base coherente de azul y superficies claras; el contenido conserva densidad y vocabulario de consola. La personalidad gráfica es reconocible a nivel básico, aunque la mayoría de pantallas siguen una composición uniforme de paneles y listas sin una jerarquía ligada al trabajo del usuario.

## Heurísticas de Nielsen

Escala de calidad 0–4; puntuación orientativa de inspección, no métrica objetiva ni aprobación técnica. Todas aplican; las no ejercitadas de forma completa se puntúan con cautela.

| Heurística | Nota | Evidencia principal |
|---|---:|---|
| 1. Estado visible | 2 | Hay estados, pero unknown y sin noticias no explican el impacto. |
| 2. Lenguaje del usuario | 1 | Eventos, identificadores, JSON y descripciones inglesas en flujos normales. |
| 3. Control y salida | 3 | Navegación estable y cancelación de formularios; deshacer no probado. |
| 4. Consistencia | 3 | Estructura común; Tareas tiene sentidos distintos y Buscar aparece dentro de Bandeja. |
| 5. Prevención de errores | 2 | Formulario guiado parcial; valores técnicos inconsistentes al cambiar acción. |
| 6. Reconocer antes de recordar | 1 | Búsqueda requiere nombres de apps y reglas requieren códigos de eventos. |
| 7. Flexibilidad y eficiencia | 2 | Búsqueda y accesos útiles por nombre; intención y reanudación insuficientes. |
| 8. Minimalismo | 2 | Shell más limpio; avisos y listados largos sin síntesis. |
| 9. Recuperación | 1 | module_disabled y falta de resultados sin salida orientada al usuario. |
| 10. Ayuda | 1 | No se encontró ayuda contextual en las vistas y formularios examinados. |
| Total | 18/40 | Primera referencia para este objetivo; no equivale a una prueba de aceptación. |

## Perspectivas y carga cognitiva

Primera visita: no puede descubrir Writer buscando novela; una regla exige un código que no tiene cómo conocer. Usuario habitual: no ve de inmediato qué cambió en su proyecto ni qué necesita atención; debe leer resúmenes extensos. Administrador: recibe estados y logs, pero no su impacto ni recuperación sugerida. La carga es moderada al navegar por secciones y alta al descubrir capacidades o componer automatizaciones.

## Notas menores y cuestiones de diseño

Buscar es una entrada propia, pero su breadcrumb y selección lo vinculan a Bandeja. Mezclar vocabulario inglés y español añade traducción mental. Una ruta de disco no sustituye una descripción de proyecto. Los controles principales deben responder a acciones del usuario antes de exponer el inventario interno.

Preguntas de diseño para la siguiente implementación: ¿qué necesita ver Luis al abrir el Hub para retomar trabajo? ¿Cuál es la entrada única al proyecto y su ágora? ¿Qué automatizaciones habituales deben poder construirse sin API? El encargo ya fija el alcance global y la necesidad de reducir confusión; no se requiere volver a preguntar permiso para diagnosticarlo.

## Evidencia técnica complementaria

Dos evaluaciones independientes en pestañas nuevas; A terminó antes de que sus conclusiones se sintetizaran con B. B ejecutó el detector una sola vez sobre el HTML: 0 hallazgos, pero en modo DEGRADED por falta de htmlparser2/css-select/css-tree/domutils. El fallback regex no verifica selectores, propiedades CSS ni contraste calculado; por tanto, no certifica la interfaz. Browser sólo lectura para evaluación: sin overlay inyectado ni servidor adicional. Cinco capturas y el informe B quedan en el directorio de outputs del recorrido; no contienen cuerpos de correo privado.
