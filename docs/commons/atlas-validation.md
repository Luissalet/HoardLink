# Atlas: validación local, 4 de octubre de 2026

La aclaración del usuario exige un disco normal compartido: un PNG, una ruta,
varias aplicaciones. Atlas implementa ese contrato sin migrar archivos existentes.
BookHoard y WatchHoard siguen fuera del ecosistema compartido.

## Comprobaciones ejecutadas

| Alcance | Resultado | Qué demuestra |
| --- | --- | --- |
| Atlas, suite completa | 20 pasan | Carpetas, pertenencia, registro sin copia, revisiones, recibos tras reinicio, caché, contexto, servidor y navegación con respuestas fuera de orden |
| HoardLink, selección final | 65 pasan, 7 omitidas | Procesos UTF-8, catálogo de propietarios, reservas CPU/RAM/disco, contrato e integración HTTP Atlas–Hub, servidor del Hub |
| HoardLink, sincronización y traspaso de historias | 11 pasan | Herramientas de distribución y traspaso existentes tras la actualización |
| Faustus, plugins de familia y adaptador común | 50 pasan | Atlas reconocido con puerto 5203 e identidad propia; compatibilidad del adaptador |
| Lumiere, suite completa | 248 casos, ejecución correcta | Regresión del editor, incluida referencia viva a un PNG sintético |
| Lumiere, repetición final de almacenamiento/familia | 14 pasan | Referencia viva, caché e integración común tras la última distribución |
| Babel, suite completa | 182 pasan | Compatibilidad tras distribuir la biblioteca común final |
| Daguerre, suite completa | 156 pasan, 1 excluida | Compatibilidad; el modelo real excluido no se ha validado |
| Prospero, suite completa final | 714 pasan, 3 omitidas | Compatibilidad después de distribuir la última corrección UTF-8; 572,41 segundos |
| Babel, Daguerre y Prospero, compilación de interfaz | Correcta | Sin errores de TypeScript; Prospero mantiene el aviso de tamaño de un paquete |
| Copias comunes Python/Node y Faustus | Sin diferencias | Comparación final con la fuente canónica; sólo aplicaciones de la familia seleccionadas |

La integración HTTP ejecuta servidores reales de Atlas y Hub en loopback, con
carpetas temporales y consumidores sintéticos autenticados como Writer y Lumiere.
Comprueba la misma ruta y revisión entre clientes Python y Node, reutilización e
invalidación, edición durante el procesamiento, identidad falsificada y una
restauración separada que conserva la edición en uso. No equivale a ejecutar
todos los flujos de esos productos ni todos los Hoards.

Una ejecución amplia anterior de HoardLink dio **3522 correctas, 74 omitidas y
2 fallos**. Se corrigieron ambos: el entorno UTF-8 de procesos hijos y una
expectativa fija del catálogo que no contemplaba Atlas. La selección final de
65 casos pasó después de las correcciones; no se afirma que toda aquella suite
se haya repetido con el último código.

## Aplicación y revisión visual

Se creó desde el navegador un proyecto de demostración aislado con un PNG y
archivos sintéticos. Se revisaron escritorio (1366 × 1000), móvil (390 × 1200)
y ventana de uso (1280 × 900). La revisión detectó una carrera al cambiar de
proyecto; se corrigió y se añadió una prueba que entrega respuestas en orden
inverso. El revisor confirmó esa corrección y dio veredicto `ship` sobre su
alcance. Las capturas están en `Atlas's Hoard/.impeccable/review/`.

El detector mecánico funcionó en modo degradado, sin sus analizadores HTML/CSS.
Su resultado vacío no demuestra contraste calculado ni una auditoría completa
de accesibilidad. La inspección visual y las pruebas de interacción son evidencia
independiente; no existe una composición aprobada que permita afirmar fidelidad.

La instalación real se verificó en `http://127.0.0.1:5203`: salud `atlas-hoard`
versión `0.1.0`, once herramientas HTTP, inicialización/listado/llamada por MCP
stdio con su propio Python sin dependencias externas. El disco real es
`D:\LocalAI\HoardStorage` y contiene cero proyectos. El Hub existente se volvió
a explorar y reconoció Atlas, con 35 aplicaciones en su catálogo. No se movieron
archivos del usuario ni se reiniciaron sus aplicaciones.
El proceso de demostración aislado se detuvo comprobando antes su identidad;
Atlas real permanece funcionando y se dejó abierta su interfaz vacía.

## Límites de adopción

El Hub y Lumiere que ya estuvieran abiertos cargan las nuevas funciones mediante
su reinicio normal. Atlas rechaza llamadas de un Hub antiguo que no adjunte la
identidad del consumidor. La pertenencia a proyectos gobierna la API, no los
permisos del sistema operativo.

`media_shared` de Lumiere abre el original y renueva sus cachés al volver a
resolverlo; no hay vigilancia continua. Los importadores antiguos que copian,
incluido el de Prospero, siguen copiando. Las carpetas admiten formatos nativos,
pero no se han trasladado todas las bases privadas ni añadido guardado externo
a cada aplicación.

Las reservas CPU/RAM/disco requieren cooperación explícita del consumidor;
no limitan por fuerza todos los procesos existentes. La inspección por hash de
Atlas tiene un límite de 512 MiB por archivo. Las copias de seguridad conservan
los límites y exclusiones del Hub y no son una instantánea atómica de varios
editores trabajando a la vez. No se han medido modelos ni GPU reales en esta fase.
