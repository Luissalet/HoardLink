# Cohesión de Faustus y la familia Hoard

Auditoría local iniciada el 3 de octubre y continuada el 4 de octubre de 2026.
El inventario base encuentra 35 manifiestos: el Hub y 34 aplicaciones. Faustus
se incorpora como raíz explícita porque tiene su propio gestor de servidor:
el inventario final contiene 36 entradas.
BookHoard y WatchHoard son aplicaciones independientes, según la aclaración
del usuario del 4 de octubre. Su revisión aparte no forma parte de las
carencias ni del plan de cohesión de esta familia.

## Una máquina, responsabilidades distintas

HoardLink mantiene los contratos y el código común; el Hub descubre, conecta,
coordina GPU, correo, eventos, agenda, notificaciones, referencias y búsqueda.
Faustus dirige el trabajo del agente. Las aplicaciones conservan sus datos y
su criterio de dominio. Una referencia o un resultado derivado no sustituye
el original ni amplía los permisos del usuario.

| Solape | Reparto que evita duplicar funciones y datos |
|---|---|
| Faustus, Funes, Argus, Echo, Borges, Dorian, People | Argus captura pantalla; Echo conserva portapapeles; Funes une actividad y audio en una cronología; Borges indexa fuentes con citas; Dorian mantiene contexto personal revisado y permissionado; People mantiene contactos y seguimientos; Faustus mantiene memoria operativa del agente. No convertir capturas o resúmenes en biografía confirmada. |
| Kafka, Borges, Hypatia, Nightingale | Kafka lee/OCR y tramita documentos; Borges indexa; Hypatia convierte fuentes en estudio; Nightingale analiza tablas con linaje. El texto extraído es evidencia, no otro documento maestro. |
| Links, Tantalus, Hub Web, Faustus Reach | Links guarda lecturas y descarga medios; Tantalus vigila productos con reglas de dominio; el Hub comparte fetch/caché/robots; Reach ofrece extracción y rastreo dirigido por el agente. Mantener credenciales y perfiles por propietario; los límites de red se conservan. |
| Prospero, Lumiere, Daguerre, Cicero | Prospero produce voz y material generado; Lumiere edita y renderiza vídeo; Daguerre cataloga fotos originales sin modificarlas; Cicero compone presentaciones. Compartir medios mediante rutas/referencias y conservar el proyecto fuente. |
| Writer, Scheherazade, Plato, Cicero | Writer es el manuscrito; Scheherazade es el estado y canon del mundo; Plato edita documentos; Cicero edita diapositivas. La exportación de una sesión es una importación con procedencia, no dos capítulos editables sincronizados silenciosamente. |
| Gepetto, Vulcan, Mercator | Gepetto crea y modifica 3D; Vulcan organiza modelos imprimibles y sus fichas; Mercator controla catálogo, ventas y publicaciones. La pieza, su ficha y sus ventas son registros diferentes unidos por referencias. |
| Ledger, Kafka, Phileas, HomeHoard, CookHoard | Ledger registra dinero; Kafka documentos/garantías/plazos; Phileas envío y viaje; HomeHoard ubicación/aparatos/mantenimiento; CookHoard despensa/menú. Una compra puede tener todos esos registros sin repetir el saldo ni generar cinco avisos. El Hub ya dispone de purchases, refs, agenda y notificaciones. |
| Laplace, Nightingale, Midas, widgets de Faustus | Laplace calcula de forma exacta y acotada; Nightingale limpia y analiza datasets; Midas investiga mercados con sus controles; los widgets resuelven operaciones pequeñas dentro del borrador. Las unidades y cálculos complejos pertenecen a Laplace; no ampliar cada widget hasta formar otro motor. |
| Galton, Pygmalion, Cassandra, trazas de Faustus | Galton mide comportamiento y publica rutas; Pygmalion entrena y evalúa contra una base; Cassandra observa la salud de servicios; Faustus registra sus llamadas. Latencia o ausencia de errores no prueba calidad del modelo. |
| Hub, Cassandra, DiskHoard | Hub ejecuta arranque/parada y coordina; Cassandra observa y solicita recuperación; DiskHoard mide y limpia almacenamiento. Evitar supervisores enfrentados: la parada manual del Hub debe prevalecer y los procesos tienen propietario. |
| Atlas, Borges, DiskHoard, proyectos de cada Hoard | Atlas mantiene carpetas de trabajo y referencias a originales compartidos; Borges indexa sus fuentes; DiskHoard mide almacenamiento; cada Hoard conserva su proyecto y criterio de dominio. Un archivo compartido tiene una ruta viva única. Un índice, una copia de seguridad o una importación que copia siguen siendo derivados distintos. |
| Babel, Vitruvius, Faustus | Babel comprueba APIs instaladas; Vitruvius evalúa interfaz con criterios y capturas; Faustus implementa y coordina. La documentación indexada, el criterio y el código final tienen dueños distintos. |
| JobHunter, People, Dorian | JobHunter mantiene candidaturas; People contactos profesionales; Dorian contexto aportado y revisado. La esfera laboral limita qué información participa; no compartir todo el perfil personal automáticamente. |

## Cambios implementados

1. **Un contrato de propietarios para Python y Node.** El Hub y los clientes
   leen la misma definición. La prueba de paridad ejecuta ambos lenguajes.
   El catálogo exige también las herramientas para seguir y cancelar trabajos.
2. **Diagnóstico utilizable por el agente.** `hub_cohesion` y su ruta HTTP
   exponen propietarios, solapes declarados, diferencias del código compartido,
   requisitos del flujo de importación y estados guardados. Es una lectura;
   no arranca aplicaciones, modelos ni tareas.
3. **Importación con recuperación.** Links → Funes → Borges mantiene un
   registro atómico, reanuda por IDs y evita repetir un evento de descarga.
   La transcripción conserva procedencia y hash; una modificación detiene
   la indexación. Una respuesta perdida exige comprobar el proveedor.
4. **Trabajos en pausa en el centro común.** El registro de trabajos distingue
   pausa de ejecución/fallo y conserva las pausas hasta que se resuelven.
5. **OCR compartido antes de cargar otro modelo.** Argus mantiene Windows OCR
   como primera opción rápida; después usa Kafka y luego sus alternativas locales.
   Una elección explícita del motor conserva prioridad.
6. **Paso de historias por el Hub.** El script de Scheherazade a Writer usa
   los IDs de aplicación y el token propio del Hub. La vía directa es explícita
   y requiere ambos tokens. Conserva la paginación completa y el importador de
   Writer que detecta ediciones y reintentos.
7. **Distribución común comprobable.** El inventario detectó diferencias
   previas en `launch.py` en 28 copias Python. La sincronización actualiza
   las copias desde el repositorio canónico. Los directorios temporales de
   integración quedan fuera; no se toca el código particular de cada aplicación.
8. **Entregas de texto y autenticación coherentes.** El proxy admite llamadas
   autenticadas de hasta 4 MiB, manteniendo límites menores para metadatos.
   Evita que un capítulo aceptado por Writer falle por el límite previo del Hub.
   Las llamadas de familia/proveedor no siguen redirecciones con credenciales.

## Carencias que requieren extensión

| Prioridad | Carencia comprobada o límite | Extensión que corresponde |
|---|---|---|
| Alta | Las entregas entre apps no son una transacción. El flujo nuevo conserva checkpoints, pero una respuesta perdida necesita conciliación y los trabajos stateless de Funes caducan. | Generalizar el patrón de journal e idempotencia en el Hub para otros flujos y acordar request_id persistente con los proveedores. No anunciar ejecución exactamente una vez. |
| Alta | Hay referencias e índices derivados, pero eso no demuestra que borrar/revocar una fuente invalide todo lo derivado en cualquier Hoard. Dorian ya impone permisos/revocación propios. | Protocolo de invalidación con ámbito, versión de fuente y acuse por consumidor. Probar revocación sin reautorizar conexiones ni borrar originales ajenos. |
| Media | Cicero tiene descubrimiento del estudio con fallback directo; la ilustración opcional de Scheherazade aún usa su adaptador REST/puerto. Writer conserva un puente Electron propio y una versión de familia independiente. | Llevar esos adaptadores al proxy del Hub donde exista una herramienta equivalente, conservando permisos, salida, tiempos y acceso al fichero. No confundir un puerto fijo de la propia app con una dependencia entre apps. |
| Media | El estado del servicio prueba herramientas, no modelos listos, formatos compatibles o calidad. | Health de dependencias y pruebas de contrato por capacidad; publicar pruebas de Galton para calidad y conservar model/dim en embeddings. |
| Media | Mercator conserva historia local, pero los contadores de usuarios/Workers se obtienen de Supabase y Cloudflare. | Separar explícitamente métricas remotas de biblioteca local y mostrar fecha/estado de la última lectura; no presentar caché como dato actual. Su README ya declara esta dependencia. |
| Media | La sincronización por copia precisa disciplina: versiones iguales pueden llevar bytes distintos. | Usar `hub_cohesion` y el comprobador de sincronización en las verificaciones locales; actualizar siempre el origen, nunca una copia particular. |

Estas prioridades son una evaluación arquitectónica de las fuentes locales,
no un resultado de benchmarks de todos los servicios ni una auditoría completa
de todos sus flujos de negocio.

## Nuevos Hoards

La aclaración posterior del usuario sobre un **SSD compartido** sí identifica un
dominio propio: carpetas normales donde varios Hoards y herramientas como Paint
o Gimp abren el mismo original. Se implementa **Atlas's Hoard**, separado de la
coordinación del Hub, el índice de Borges y la inspección de disco de DiskHoard.

Atlas ofrece proyectos comunes, carpetas `shared/` y `hoards/<app>/`, registro de
archivos sin copia, revisiones, recibos persistentes, contexto acotado y referencias
a resultados reutilizables por hashes y receta. El Hub descubre el servicio y
respeta la identidad y pertenencia del consumidor. Sus copias incluyen el disco
externo y restauran los originales compartidos en una carpeta separada.

Lumiere añade `media_shared`: referencia viva y actualización de cachés manteniendo
el ID usado por sus montajes. Los clientes comunes Python/Node se distribuyen a la
familia; los importadores que ya copiaban siguen copiando hasta adoptar enlaces.
No hay migración automática de bases privadas, archivos existentes o todos los
proyectos nativos. Las carpetas pueden guardar formatos nativos cuando el Hoard
permita elegir destino. La pertenencia a proyectos no sustituye los permisos del SO.

El Hub añade un árbitro cooperativo de CPU/RAM/disco, aparte del de GPU. Sus reservas
requieren adopción explícita por el consumidor; no limitan por fuerza procesos
nativos ni todos los trabajadores existentes. El contexto de Atlas incluye sólo
el objetivo, ámbito y referencias explícitas de una tarea, no toda la memoria.

La integración real Atlas–Hub comprueba el mismo archivo entre dos consumidores,
clientes Python/Node, cambios de revisión, reutilización e invalidación, rechazo
de identidad falsificada y restauración que conserva la edición en uso. Todo
en carpetas aisladas con datos sintéticos; no equivale a probar todos los flujos
reales de todos los Hoards. Registro: [validación de Atlas](atlas-validation.md).

Un Hoard adicional para orquestación, scraping genérico, memoria genérica o tareas
duplicaría Hub, Reach, Funes/Borges/Dorian o las agendas ya existentes.

## Cómo comprobarlo

Desde HoardLink:

```powershell
python scripts/audit_cohesion.py --roots "RUTA_DE_LA_FAMILIA" "RUTA_DE_FAUSTUS" --probe --output inventory.json
python scripts/sync_vendored.py --roots "RUTA_DE_LA_FAMILIA" --check
```

El inventario no lee bases privadas, documentos ni tokens para volcarlos.
`--probe` lee salud y catálogos locales con los mecanismos existentes y no
arranca nada. Sus señales de código solo indican archivo/línea: hay que leerlas
antes de llamar duplicación a una alternativa legítima.

Validación y límites de este cambio: [registro de pruebas](cohesion-validation.md).
