# Actualización de iconos del Hub

El Hub comprueba los iconos seleccionados durante la consulta normal de aplicaciones, cada cinco segundos. Detecta iconos nuevos, reemplazos, eliminaciones y candidatos de mayor prioridad sin reiniciar el Hub ni volver a escanear los manifiestos. Los nombres locales de cada aplicación tienen prioridad; un archivo coincidente en una carpeta compartida `Icons` configurada sirve como alternativa.

La clave de caché del navegador combina la ruta seleccionada, el tamaño y la fecha de modificación. Los archivos estables conservan la misma URL y siguen en caché; cuando cambia la selección, cambia la URL. Las demás vistas del Hub que usan `/api/apps/{id}/icon` se actualizan con la misma consulta. Esto actualiza el archivo seleccionado; no copia un icono maestro compartido a la carpeta de una aplicación.

La revisión usa metadatos del archivo, no un hash de contenido. Si una herramienta conserva deliberadamente el tamaño y la fecha al cambiar los bytes, también debe actualizar la fecha de modificación para que el cambio se detecte automáticamente; volver a escanear no cambia esta clave de caché.
