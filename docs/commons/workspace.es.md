# Proyectos compartidos desde Hub

La faceta `workspace` de Hub reenvía la API real de proyectos compartidos de Atlas mediante la llamada autenticada habitual entre aplicaciones. Atlas conserva los datos, las membresías, las rutas, los identificadores y las revisiones. Hub no mantiene una segunda base de proyectos.

## REST

Usa `GET /api/workspace/projects?sphere=work` para listar los proyectos visibles a la identidad familiar autenticada. `POST /api/workspace/call` acepta `{"tool":"project_create","arguments":{"name":"Investigación","goal":"..."}}` (el contrato existente) o el campo equivalente `operation`. Si se envían ambos, deben coincidir. Los nombres disponibles son `projects`, `project`, `project_create`, `project_update`, `location`, `file_register`, `file_resolve`, `file_link_source`, `derived_publish`, `derived_lookup` y `context`. Las llamadas correctas devuelven directamente el objeto de resultado de Atlas.

La identidad procede del bearer de Hub; se ignora el campo `caller` del cuerpo. Las llamadas con el token de una aplicación llegan a Atlas con el ID autenticado de esa aplicación y conservan la comprobación de membresía. El token de Hub es la identidad de operador de Atlas. Solo esa identidad puede usar `file_link_source` desde esta fachada.

Consulta `location` para obtener una carpeta compartida o nativa de una aplicación. Guarda allí el archivo normal y llama a `file_register` con su ruta relativa al proyecto. El registro calcula el hash del archivo existente sin copiarlo ni modificarlo. `file_link_source` registra una ruta absoluta externa como entrada viva de solo lectura, sin copiarla; los cambios se reflejan al usar `file_resolve`. Esta fachada no expone `file_import`: Atlas conserva esa acción explícita de operador que copia un archivo.

## MCP

MCP ofrece una herramienta por operación de Atlas: `hub_atlas_project_create`, `hub_atlas_project_update`, `hub_atlas_projects`, `hub_atlas_project`, `hub_atlas_location`, `hub_atlas_file_register`, `hub_atlas_file_resolve`, `hub_atlas_file_link_source`, `hub_atlas_derived_publish`, `hub_atlas_derived_lookup` y `hub_atlas_context`. Cada una publica directamente el esquema de parámetros de Atlas, incluidos los campos obligatorios y las enumeraciones. La herramienta compatible `hub_workspace` acepta la forma anterior `{tool, arguments}`; para disponer de guía de parámetros, es preferible usar las herramientas granulares. Ninguna de las dos superficies ofrece `file_import`.

`hub_atlas_file_link_source` usa el token MCP de Hub y llega a Atlas como operador. `hub_atlas_context` requiere un ID explícito de proyecto y acepta como máximo 20 IDs de archivo explícitos; devuelve metadatos y referencias, no el contenido de los archivos, y no llama a ningún modelo. La membresía controla el acceso a la API, no los permisos de archivos de Windows.
