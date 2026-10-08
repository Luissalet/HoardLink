# Validación de la cohesión — 4 de octubre de 2026

## Inventario y distribución

El inventario inicial del día 3 reúne 35 manifiestos: el Hub y 34 aplicaciones.
El inventario final añade Faustus como raíz explícita y contiene 36 entradas.
WatchHoard y BookHoard, situados dentro de otras carpetas, se revisaron por sus
fuentes y documentación y no cuentan como dos manifiestos adicionales. El
usuario aclaró después que son independientes: quedan fuera del plan de familia.

- [Inventario inicial](cohesion-inventory-2026-10-03.json): 28 copias Python
  tenían una diferencia previa en `launch.py` respecto al origen común.
- [Inventario final](cohesion-inventory-2026-10-04.json): ninguna diferencia
  en las copias de código común examinadas.
- Sincronización desde HoardLink en 34 repositorios consumidores: 29 copias
  Python y cinco Node. El comprobador `sync_vendored.py --check`, limitado
  explícitamente a esos consumidores, acaba con código 0 y cero cambios.
- `git diff --check` correcto en los 36 repositorios del inventario final.
  No se han realizado commits, push, migraciones de datos reales ni reinicios
  de instancias del usuario durante este cambio.

El sondeo encontró una aplicación respondiendo a salud (Plato); las demás
no ofrecieron un catálogo utilizable en ese momento. Una aplicación parada
no implica un defecto de integración. No se arrancó toda la familia para
convertir el inventario en una prueba de funcionamiento real.

## Pruebas del núcleo

La suite completa de HoardLink pasó con **3.498 pruebas y 74 omitidas**
en 23 min 18 s. Esa corrida comenzó antes de los últimos ajustes de transporte;
estos se comprobaron después con los grupos específicos siguientes:

| Comprobación posterior | Resultado |
|---|---:|
| Transporte autenticado, familia, leases, servicios Python y Node | 131 correctas |
| Cohesión, flujo HTTP, recuperación, trabajos en pausa, contrato, sincronización, historias y chat | 104 correctas |
| Última revisión del flujo y recuperación, incluida negativa explícita de Borges | 16 correctas |
| Adaptador compartido de Faustus tras la última sincronización | 7 correctas |

Hay solapamiento entre estas corridas: no se suman como casos distintos.
Las comprobaciones cubren una respuesta perdida sin reenvío de mutaciones,
reinicios con IDs conservados, conciliación explícita, eventos repetidos,
transcripciones modificadas, estados terminales, journal corrupto conservado,
pausas que no se archivan por antigüedad, y permisos del operador.

La prueba de integración HTTP arranca un Hub real y tres proveedores de
prueba en loopback. Verifica el paso Links → Funes → Borges, sus credenciales,
el registro de trabajo y la ausencia de llamadas adicionales al repetir
un evento. Sus archivos y respuestas son sintéticos: no mide ASR ni RAG.

El transporte verifica que una redirección no recibe los tokens del proveedor,
que un capítulo mayor de 256 KiB atraviesa el proxy autenticado y que se
rechazan solicitudes sin autorización o superiores a 4 MiB antes de leer
su contenido. La prueba de contratos ejecuta también el cliente Node.

## Consumidores

Ejecutados en sus propios entornos, con concurrencia limitada a tres.
Los registros y el resumen local están en
`D:\LocalAI\qa\cohesion-2026-10-04`.

| Aplicación | Pruebas correctas | Omitidas | Alcance adicional |
|---|---:|---:|---|
| Argus | 102 | 0 | Prioridad Windows → Kafka; alternativa local por caída, recuperación y ausencia de fallback ante fallo de autorización |
| Babel | 182 | 0 | Build del frontend |
| Daguerre | 157 | 0 | Build y arranque `--demo` en una carpeta aislada |
| Dorian | 68 | 0 | Suite completa |
| Funes | 24 | 0 | `tests/test_backend.py`, alcance de la actualización de su copia común |
| Galton | 1.147 | 2 | Suite y generador de documentación API |
| Kafka | 581 | 0 | Suite y generador de documentación API |
| Laplace | 252 | 0 | Suite completa |
| Pygmalion | 784 | 40 | Suite y generador de documentación API |
| Scheherazade | 346 | 0 | Suite completa |

La vista de Daguerre se inspeccionó en el navegador: 87 fotos sintéticas,
miniaturas visibles y biblioteca renderizada. El proceso de demostración
se detuvo después de verificar su identidad; no se tocaron fotos del usuario.
Las pruebas omitidas no cuentan como comprobación de modelos o hardware real.

Faustus pasó además una selección anterior de 88 pruebas de familia,
esferas, plugins, Reach y navegador. La repetición final de siete casos
comprueba su adaptador después de copiar el último transporte.

## Límites

`done` en una importación significa que la transcripción está guardada y
Borges ha aceptado solicitar su indexación. No demuestra que la indexación
termine ni que el texto ya sea recuperable. El estado de servicios comprueba
catálogos, no disponibilidad de modelos ni calidad.

Los trabajos del proveedor pueden caducar y una respuesta de mutación perdida
necesita conciliación explícita. No hay garantía de ejecución exactamente una
vez ni recuperación automática de todos los flujos de la familia.

No se han unificado todos los adaptadores REST heredados ni establecido una invalidación universal de datos
derivados. Esas carencias siguen enumeradas y priorizadas en
[la auditoría de cohesión](cohesion.md). Las instancias ya abiertas cargarán
el nuevo código mediante su reinicio normal.
