# Perfiles portátiles de inicio del Hub

Los perfiles de HoardLink agrupan IDs de apps registradas, comandos externos
y apps que se abren en ventanas de escritorio. El paquete es una copia JSON
versionada de esa configuración. No es una cuenta, otro almacén de perfiles
ni una sesión de ejecución.

## Exportar

Usa `POST /api/profiles/export` con `{"name":"video"}` o MCP
`hub_profile_export {"name":"video"}`. La respuesta usa el formato
`hoardlink.launch-profile`, versión `1`, y conserva los campos nativos
`apps`, `desktop` y `commands`. Los IDs de apps aparecen por separado en
`dependencies.apps`; el Hub de destino debe asociarlos con IDs registrados
localmente.

El paquete excluye IDs de proceso, registros de comandos y estado de ejecución.
Omite los valores de variables cuyo nombre parece contener credenciales
(`TOKEN`, `PASSWORD`, `API_KEY`, `SECRET` y similares) e indica el comando y
la clave en `portability.redacted_environment`. La exportación falla si detecta
argumentos habituales con credenciales incrustadas o URLs de salud con
credenciales; el error solo indica el nombre del comando. El detector no puede
reconocer todos los secretos en texto arbitrario, así que revisa el paquete
antes de compartirlo.
Se conservan flags operativos como `AUTH_ENABLED` y `COOKIE_SECURE`; este
detector heurístico no garantiza que encuentre todos los secretos.

`commands[].cwd` y `commands[].health` se copian como ajustes de la máquina de
origen, separados de los IDs de apps. Puede que debas sustituirlos localmente.
La exportación no consulta URLs de salud ni inspecciona procesos.

## Previsualizar e importar

Previsualiza con `POST /api/profiles/import/preview` o MCP
`hub_profile_import_preview`, pasando el paquete en `document`. `app_map`
relaciona los IDs del paquete con IDs registrados en el Hub local; las
dependencias sin resolver bloquean la importación. `command_overrides` se
organiza por nombre de comando y puede reemplazar `cmd`, `cwd` o `health`, y
añadir valores locales a `env` (incluidas credenciales omitidas del paquete).
La vista previa muestra dependencias, conflictos, avisos de rutas y variables
locales que faltan, sin guardar la configuración.

Los campos `cwd` y `health` conservados se muestran como avisos y se pueden
guardar tal cual; sustitúyelos por `null` o por valores locales cuando haga
falta. La vista previa también muestra el comando y sus argumentos efectivos,
además de las claves de entorno que se guardarán. Los nombres siguen la regla
nativa de ser cadenas no vacías. Si el nombre ya existe, elige otro `name` o
pasa `replace: true`; no se reemplazan perfiles en ejecución.

Importa con `POST /api/profiles/import` o MCP `hub_profile_import`, usando los
mismos campos revisados. Se guarda el perfil resuelto en el `hub.json` local,
pero nunca se inicia una app, se abre una ventana ni se ejecuta un comando.
El inicio sigue siendo la acción independiente `hub_profile_start`.

## Referencias y límites

Los perfiles de Docker Compose agrupan servicios opcionales y los activan en
tiempo de ejecución; Compose también valida dependencias entre servicios.
HoardLink ya agrupa apps, comandos y ventanas de escritorio, así que esta
función transfiere ese modelo existente y explicita las dependencias por ID
local: [perfiles de Compose](https://docs.docker.com/reference/compose-file/profiles/).

Hermes ofrece un patrón útil de exportación e importación de un archivo y
excluye expresamente los almacenes de credenciales; también evita sobrescribir
un perfil existente. HoardLink añade una vista previa y resolución local de
apps y rutas porque sus perfiles inician apps Hoard registradas y comandos
arbitrarios de esta máquina:
[exportar e importar perfiles de Hermes](https://hermes-agent.nousresearch.com/docs/reference/profile-commands#hermes-profile-export).

Este intercambio JSON transfiere solo la configuración de inicio. No incluye
las apps referenciadas, no instala dependencias de comandos, no transporta el
estado del shell ni ofrece orden de dependencias entre servicios como Compose.
El comportamiento existente del Hub sigue controlando el inicio y la parada.
