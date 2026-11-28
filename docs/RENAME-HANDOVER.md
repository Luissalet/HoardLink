# Cambio de identidad de la aplicación de voz · r28

El renombrado conserva las fichas, mueve la carpeta de voz con sus datos y reinicia el Hub una sola vez durante el avance. El mismo programa ejecuta la instalación y la vuelta atrás. La decisión de instalarlas corresponde a dirección. El ensayo usa el Hub real en modo aislado, con las dos aplicaciones sustituidas por servicios de prueba.

| Repositorio | Partida y vuelta atrás | Candidato |
| --- | --- | --- |
| Hoard Hub | `084674f21d2be8502c969896b80e7d5b973f96f7` | `8665faf24b740d1254f02420aa45bbed3b51eb16` |
| Cyrano's Hoard | `a0a63a3152fe5e63f081eef042968fa3a4078621` | `49f1078503d4484a29e6110b6c8e3a655443a6dd` |
| Vitruvius's Hoard | `9c73ad92f806d59ebb465178d38328620e0a81c0` | `4e58e4d7f832ce322b10dbce3ef57babd4f5314b` |

Dirección autoriza el dispositivo de la tanda. El recibo vincula su UUID al SHA de Boltagon y a las mediciones de la ejecución. El ensayo aislado comprueba la migración con la síntesis apagada. La comparación A–B–B–A aprobó el candidato a8092ec de r12. El instalador exige la medición del SHA exacto de la tabla. El banco de esta revisión pasó para el candidato actualizado. Quedan pendientes la repetición independiente y el previo coordinado por dirección.

## Ensayo aislado

Revisa los scripts y el lanzador. Usa tu tarea en el Ágora para reservar cuatro nombres de worktree con un mismo prefijo propio y los sufijos `-hub`, `-voice`, `-vit` y `-boltagon`. Guarda este bloque como un `.ps1` en la carpeta de evidencias. Invoca `nw.ps1` con `&` desde PowerShell y pasa el fichero con `-Cmd @('powershell.exe','-NoProfile','-NonInteractive','-File',$Fichero,'-Prefix',$Prefijo,'-ServicePython',$PythonVoz,'-WebsocketsWheel',$Rueda)`. Usa el worktree de código del Hub en `-Cwd` y un log de evidencias en `-Log`. Pasa la ruta absoluta del intérprete del venv de voz que esté instalado y la rueda local que coincida con su versión de websockets. Los dos se usan en modo de solo lectura. El ensayo registra la versión, la huella de la rueda y la importación desde cada entorno temporal de voz.

~~~powershell
param([Parameter(Mandatory)][string]$Prefix, [Parameter(Mandatory)][string]$ServicePython, [Parameter(Mandatory)][string]$WebsocketsWheel)
$ErrorActionPreference = 'Stop'
$Qa = 'E:\Development\IA\Research\_qa\capataz'
if ($Prefix -notmatch '^[a-z0-9][a-z0-9-]+$') { throw 'Indica el prefijo de tus worktrees reservados' }
(Get-Content -LiteralPath (Join-Path $Qa 't25\reglas.txt') -Encoding UTF8 | Where-Object {$_.StartsWith('$env:HOARD_HUB_URL=')}) | Invoke-Expression
[Diagnostics.Process]::GetCurrentProcess().PriorityClass = 'BelowNormal'
$Root = Join-Path $env:TEMP ('rename-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $Root
$env:FAUSTUS_DIR = Join-Path $Root 'faustus'
$env:CYRANO_TTS_ENGINE = 'off'
$env:BOLTAGON_TTS_ENGINE = 'off'
$env:CUDA_VISIBLE_DEVICES = '-1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$Worktree = Join-Path $Root ($Prefix + '-hub')
$Before = '084674f21d2be8502c969896b80e7d5b973f96f7'
$After = '8665faf24b740d1254f02420aa45bbed3b51eb16'
git worktree add -b ('rehearsal/' + (Split-Path -Leaf $Root) + '/hub') $Worktree $Before
if ($LASTEXITCODE) { throw 'No se pudo abrir el worktree temporal del Hub' }
& $ServicePython -B .\scripts\rehearse_rename_handover.py --worktree $Worktree --launcher (Join-Path $Qa 't24v\nw.ps1') --window-watch (Join-Path $Qa 'vigia-ventanas.log') --hub-before $Before --hub-after $After --websockets-wheel $WebsocketsWheel
~~~

El ensayo crea los dos worktrees de las aplicaciones de prueba dentro de la carpeta reservada. Usa el CLI real con `python -P -m hoard_link.hub.cli --json` y valida el envoltorio `ok/tool/result`. Ejecuta el Hub de la base y el del candidato desde el worktree temporal. El lanzador abre PowerShell sin ventana y carga «CAMINO CERRADO» dentro del proceso creado por WMI. El registro del ensayo guarda los identificadores de ventanas visibles y el recuento del vigía antes y después de cada arranque. El Hub de partida conserva su checkout exacto y recibe el asignador de puertos en memoria para el ensayo. El Hub candidato usa el asignador que lleva en Git. Ambos eligen entre 49152 y 65535. Antes de asignar un puerto, excluyen los reservados del estudio, los rangos TCP excluidos por Windows y los puertos TCP ya ocupados, incluso los vinculados sin escucha. Una denegación de Windows detiene el intento e identifica la dirección y el puerto.

La API aislada relee el manifiesto al arrancar una app. Para los dos dobles, el ensayo materializa en ese manifiesto el ejecutable, el directorio, el argv y el entorno devueltos por el mismo resolvedor de fichas que usa el Hub. La API asigna el puerto, el token y los datos temporales y fuerza el modo sin ventana. Antes de iniciar el Hub, el ensayo genera un manifiesto temporal ignorado por Git en cada worktree de prueba. Las plantillas rastreadas pasan por Git durante el avance y la vuelta atrás. `observed-launches.json` recoge lo declarado por los propios procesos antes, después del avance y tras la recuperación. El doble de voz confirma que CUDA está enmascarada, que la síntesis está desactivada y que el intérprete configurado existe en la ruta correspondiente a cada fase. El doble de Vitruvius lee `frases.json` mediante `VITRUVIUS_VOICE_CACHE`.

El directorio temporal conserva `receipts.json`, el registro de cada fase y sus logs. Añade `--phase 0` a la última orden para repetir el caso completo o `--phase N` para una interrupción concreta. El Hub y el ciclo de procesos son reales. La transcripción y el dictado usan datos sintéticos. El worktree de Vitruvius contiene un archivo de procedencia con un cambio local sin preparar y un directorio `obj/` sin rastrear. Se comprueban las dos columnas de estado Git y las huellas de ambos archivos antes del avance, después de mover los SHA y al restaurarlos. Cada fase guarda `protected-local-files.json`. El ensayo comprueba que no arranca ningún hijo de voz.

## Orden de instalación

El procedimiento inventaría las bases, guarda el arranque del Hub y las fichas, comprueba las copias y para las dos aplicaciones antes de parar el Hub. Dentro de la misma parada, avanza los tres SHA en orden Hub, Boltagon y Vitruvius. El Hub prepara el registro del ID nuevo y el bearer de la ruta efectiva. El paquete de Boltagon aporta el manifiesto canónico y los alias de entrada. Vitruvius consume el catálogo resultante y puede dar servicio con cualquiera de los dos ID durante la transición.

Después mueve una vez la carpeta de voz, reconstruye su venv en la ruta nueva y publica el registro completo. Arranca primero el Hub, vuelve a leer el catálogo y comprueba que reconoce Boltagon y Vitruvius. Después arranca Boltagon y luego Vitruvius. Así el asistente hace su primer descubrimiento cuando el proveedor ya está disponible con el puerto y el token definitivos. Comprueba salud, dictado y vista previa antes de aceptar.

## Instalación parcial

Las tres piezas de código se pueden adelantar en cualquier orden, manteniendo la carpeta y el registro instalados. El manifiesto de voz conserva cyrano para el Hub anterior y declara boltagon como ID canónico para el Hub actualizado. El Hub actualizado acepta la clave cyrano del lanzador. Si están presentes las dos claves, usa la canónica para la ficha canónica. Mantiene una sola aplicación registrada y añade la entrada cyrano en /api/apps para clientes que buscan ese ID literal. La interfaz del Hub muestra una sola tarjeta.

| Piezas adelantadas | Qué sigue funcionando | Qué queda pendiente |
| --- | --- | --- |
| Hub | El lanzador y catálogo existentes conservan cyrano. Los perfiles, puertos y paradas guardados con ese ID siguen aplicándose. | El manifiesto canónico llega con Boltagon. |
| Boltagon | El Hub anterior sigue leyendo el ID cyrano y usa su ficha. El módulo cyrano_hoard delega en Boltagon. Los clientes antiguos lo descubren por el catálogo. | La publicación canónica del ID llega con el Hub actualizado. |
| Vitruvius | El asistente nuevo encuentra cyrano como alternativa y acepta CYRANO_URL. | La ruta canónica queda disponible al adelantar la voz y el Hub. |
| Hub y Boltagon | Se publica boltagon con una entrada de compatibilidad cyrano. El Hub usa la ficha cyrano y su ruta de token efectiva. Vitruvius instalado encuentra la entrada anterior. | La versión nueva del asistente aún no está instalada. |
| Hub y Vitruvius | El Hub conserva la ficha cyrano y el asistente nuevo la encuentra por compatibilidad. | Sigue pendiente el paquete de voz que declara el ID canónico. |
| Boltagon y Vitruvius | El Hub anterior mantiene la ficha cyrano. El asistente nuevo descubre esa ficha y llega al contrato de voz. | Sigue pendiente la publicación del ID canónico. |

Con un manifiesto que publicase solo boltagon, el Hub anterior perdería su launch_override cyrano al releer el catálogo y Vitruvius anterior dejaría de descubrir la voz. El manifiesto con los dos nombres y la entrada de catálogo anterior cubren esos dos casos. El proxy de agentes y el CLI siguen aceptando el ID cyrano. El cliente de voz de Hyde lee CYRANO_URL y usa en su defecto el puerto 5253. Llama directamente a las rutas de estado, síntesis, flujo y cancelación con el contrato cyrano.voice/v0 y conserva la concesión del perfil. La ficha de Boltagon conserva el puerto y los datos de voz, incluidas las concesiones. El dictado conserva sus rutas y la versión cyrano.dictation/v0. Los asistentes ya incrustados mantienen la ruta del proyecto hacia Vitruvius y pasan por el mismo descubrimiento de voz. Los permisos, los datos del proyecto y el disparo de acciones permanecen a cargo de sus componentes actuales.

Los ensayos parciales con los intérpretes instalados comprobaron el Hub anterior y el actualizado con Boltagon nuevo y Vitruvius de partida. En ambos casos, el dictado de Vitruvius interpretó 2.000 céntimos, un formulario propio guardado volvió a abrir con el asistente, y la llamada de agente y el CLI aceptaron cyrano. Un envío con el mismo contrato HTTP que Hyde devolvió el estado disabled y la respuesta voice_disabled esperada con el motor apagado. La matriz de las ocho combinaciones usa respuestas HTTP sintéticas para comprobar todos los órdenes.

El avance solo de código conserva hub.json y la carpeta de voz. Mover la carpeta por separado dejaría rutas del lanzador, el intérprete auxiliar y los recursos de voz apuntando al nombre anterior. Para la mudanza, dirección ejecuta la operación coordinada desde las bases verificadas. El paso 7 publica el registro migrado después de la copia, la mudanza y la reconstrucción del venv. El instalador vuelve a comprobar los tres SHA antes de parar servicios. Los despliegues de código adelantados deben figurar en el inventario del plan de mudanza antes de usarlo.

## Referencias instaladas de cyrano en hub.json

El inventario de solo lectura encontró estas 18 menciones. La tabla identifica claves y posiciones de valores mediante JSON Pointer sin reproducir sus contenidos. Todas permanecen durante el avance de código. La última columna describe el paso 7 de la mudanza coordinada en caso de ejecutarse.

| JSON Pointer | Mención | En la mudanza coordinada |
| --- | --- | --- |
| /launch_overrides/cyrano | Clave | Se publica la ficha como boltagon. Ambos ID siguen aceptándose en la API. |
| /launch_overrides/cyrano/executable | Valor | Se fija el Python del venv reconstruido bajo la carpeta nueva. |
| /launch_overrides/cyrano/argv/1 | Valor | Se usa el módulo boltagon_hoard. El módulo antiguo sigue disponible como entrada. |
| /launch_overrides/cyrano/cwd | Valor | Se usa la carpeta nueva. |
| /launch_overrides/cyrano/env/CYRANO_PORT | Clave | Pasa a BOLTAGON_PORT con el mismo puerto. |
| /launch_overrides/cyrano/env/CYRANO_TTS_ENGINE | Clave | Se añade BOLTAGON_TTS_ENGINE y se conserva la clave antigua con el mismo motor. |
| /launch_overrides/cyrano/env/CYRANO_TTS_PYTHON | Clave | Se añade BOLTAGON_TTS_PYTHON y se conserva la clave antigua. |
| /launch_overrides/cyrano/env/CYRANO_TTS_PYTHON | Valor | Las dos claves apuntan al mismo intérprete auxiliar después de mover su ruta. |
| /launch_overrides/cyrano/env/CYRANO_TTS_EXPECTED_GPU_UUID | Clave | Se añade el nombre canónico con el mismo UUID y se conserva el antiguo. |
| /launch_overrides/cyrano/env/CYRANO_TTS_QWEN_GPU | Clave | Se añade el nombre canónico con el mismo selector de GPU y se conserva el antiguo. |
| /launch_overrides/cyrano/env/CYRANO_TTS_QWEN_VRAM_MB | Clave | Se añade el nombre canónico con la misma reserva y se conserva el antiguo. |
| /launch_overrides/cyrano/env/CYRANO_TTS_QWEN_MODEL_DIR | Clave | Se añade el nombre canónico y se conserva el antiguo. |
| /launch_overrides/cyrano/env/CYRANO_TTS_QWEN_MODEL_DIR | Valor | Se actualiza el prefijo de ambas rutas. Se conservan los mismos pesos. |
| /launch_overrides/cyrano/env/CYRANO_TTS_QWEN_REFERENCE_WAV | Clave | Se añade el nombre canónico y se conserva el antiguo. |
| /launch_overrides/cyrano/env/CYRANO_TTS_QWEN_REFERENCE_WAV | Valor | Se actualiza el prefijo de ambas rutas. Se conserva el mismo WAV. |
| /launch_overrides/cyrano/env/CYRANO_TTS_QWEN_REFERENCE_TEXT | Clave | Se añade el nombre canónico con el mismo texto de referencia y se conserva el antiguo. |
| /launch_overrides/cyrano/env/CYRANO_TTS_PREWARM | Clave | Se añade el nombre canónico con el mismo ajuste y se conserva el antiguo. |
| /launch_overrides/cyrano/env/CYRANO_TTS_PRELOAD | Clave | Se añade el nombre canónico con el mismo ajuste y se conserva el antiguo. |

## Estado que recupera cada pieza

| Pieza | Estado anterior exacto |
| --- | --- |
| Hub | El SHA de partida de la tabla, el lanzador original capturado en hub-launch.dpapi y las copias verificadas de hub.json, app-ports.json, manual-stops.json, rules.json y jobs.json que existían en el inventario. Se retiran los archivos de ese conjunto creados durante la migración. El catálogo recupera cyrano y vitruvius. |
| Boltagon | El SHA de partida de la tabla, la carpeta Cyrano's Hoard y su ruta de trabajo, la ficha cyrano y el venv original guardado en voice-venv-original. Los datos y el intérprete auxiliar vuelven con la carpeta. Se aparta el venv candidato antes de recuperar el original. Se confirma la salud por el ID previo. |
| Vitruvius | El SHA de partida de la tabla, la ficha vitruvius original con el lanzador y la ruta de caché completos, y el mismo estado de Git y las mismas huellas locales inventariadas. Se reabre solo si estaba activo al iniciar. |

La acción rollback aplica esos tres estados desde el mismo plan y registro. Para la pareja de voz y registro hay una dependencia física. Si se restaura solo la carpeta, las rutas de arranque y de caché siguen apuntando a Boltagon's Hoard. Si se restaura solo la ficha, el Hub apunta a Cyrano's Hoard mientras la voz sigue en la carpeta nueva.

En un ensayo donde solo hubiera avanzado Git y los servicios y ficheros siguieran en su estado original, la reparación de cada pieza se limita a su SHA de partida con git reset --keep y la comparación del estado local. Una vez que hay un registro de la migración, se usa la vuelta atrás coordinada descrita abajo. Para y valida los propietarios, recupera el venv, restaura los tres SHA, devuelve la carpeta de voz, verifica los ficheros locales y las copias, arranca el Hub y repone únicamente las aplicaciones activas en el inventario. Los reinicios y las restauraciones de estas rutas corresponden a dirección.

## Medición previa con el motor real

Usar el banco de voz compartido en `E:\Development\IA\Research\_qa\capataz\banco-voz` con un worktree limpio del Ágora en el SHA completo de Boltagon de la tabla. La entrada de compatibilidad `cyrano_hoard.main.create_app` delega en la fábrica canónica del mismo worktree. La base `a0a63a3` incluye la guarda de duración, la admisión de voz por UUID y la detección temprana de `4c6daad`. El candidato conserva el WAV acotado en directo, los controles de calidad, el WebSocket de reconocimiento incremental y la verificación de procedencia del audio de perfil de la base instalada, y el ajuste de reconocimiento de secuencias de dígitos en español, también cuando el idioma se detecta en vez de indicarse. La medición anterior de `f322812` queda como referencia histórica.

Antes de arrancar, revisar que la revisión del banco que se va a ejecutar excluye los puertos reservados antes de abrir el listener. El banco debe tomar `t24\gpu2.lock`, consultar por UUID la GPU autorizada y el índice que tiene en ese arranque, abrir el servicio con `t24v\nw.ps1` y cargar CAMINO CERRADO en el PowerShell hijo. Usar datos y cachés propios del banco. Tras detenerlo, conservar la comprobación del vigía, del listener, de los procesos y de la liberación del cerrojo.

Pasar al banco `-Worktree` con ese worktree, `-Sha 49f1078503d4484a29e6110b6c8e3a655443a6dd`, `-Owner` con la identidad propia del Ágora, `-GpuUuid` con el UUID completo aprobado y `-ServicePython` con el intérprete de pruebas acordado. El banco imprime el directorio `run-*` que contiene `result.json`, la tabla Markdown y `gpu2-before.csv`.

- Medir 20 frases nuevas de al menos 15 palabras con importes, IBAN, fechas, cifras largas y pausas marcadas. Cronometrar desde `POST /api/voice/speak` con `wait:false` hasta el primer evento NDJSON de audio. Cada frase debe acabar con PCM válido y audible, primer audio en un máximo de 1.200 ms y el primer bloque y el búfer inicial por debajo del 20 % de los frames del WAV.
- Cancelar otras 20 locuciones a mitad de flujo. Cada una debe terminar como cancelada. La frase posterior debe emitir audio y completarse en el mismo hijo de voz que inició la tanda. El revisor repite la medición con el mismo SHA.
- Conservar `result.json` y `gpu2-before.csv` en el mismo directorio de evidencias. Fijar la huella SHA-256 de `result.json` y el UUID aprobado con el veredicto del revisor. El recibo registra `gpu_index` cuando la tarjeta usa un índice diferente del 2. La línea base y los recibos sintéticos quedan como evidencias de comparación y de la migración aislada.

El recibo de esta revisión está en `E:\Development\IA\Research\_qa\capataz\t25\rename-4148\codex-worker-1ef72a32\t28-r16\voice-compat\gpu\run-49f1078-20261128-125539-75f1db7e\result.json`, con SHA-256 `6821e82377cd8e509d307a189075ce55d770049184eaa3af07db99b88123f726` y UUID `GPU-15d17fee-8c0c-4be3-be46-35fb3e32f2aa`. El primer audio tardó como máximo 446,43 ms. Las veinte frases fueron audibles y las veinte cancelaciones conservaron el mismo hijo. El validador del instalador aceptó el recibo. El banco marcó cinco avisos de discrepancia con la transcripción de control en las frases 2, 4, 7, 12 y 14.

La comprobación focal del recibo está en `tests/hub/test_rename_handover_voice.py`. Con CAMINO CERRADO y BelowNormal, ejecutar ese focal para el SHA del Hub y repetir los diez casos del ensayo aislado. El focal cubre, entre otros rechazos, un SHA distinto, falta de integridad, GPU distinta, 1.201 ms, el límite exacto del 20 %, audio mudo y un PID cambiado. Si la guarda de duración termina el directo antes del WAV del reintento, el recibo debe llevar la marca `duration_guard`, acreditar la misma frecuencia y mantener los límites del banco.

## Ventana autorizada de instalación

Esta sección es para dirección después de aprobar los candidatos y el ensayo. Usa la sesión normal con la misma cuenta que inició el Hub. Comprueba la admisión de la voz residente según la guía `t24\voz-interprete.md`. Usa el WAV de veinte euros verificado en el Plan B y su directorio de ruedas local. Guarda el plan y el registro en una carpeta nueva en el mismo volumen que los repositorios.

El procedimiento parte de `t24\inst-planb\script-desviacion.ps1` y conserva las bases exactas, el control de aplicaciones por ID y las comprobaciones de salud. Captura la orden y el entorno del Hub propietario del puerto en un fichero protegido por DPAPI para esa cuenta. El mismo lanzador sin consola se usa en el ensayo. Vitruvius conserva el cliente de la base `9c73ad9`. Antes de parar, el programa comprueba que las fuentes de Vite y el lockfile coinciden. El inventario conserva las dos columnas de estado Git para reconocer el archivo de procedencia modificado sin preparar y el directorio de compilación sin rastrear. La migración preserva estos archivos locales y la caché de voz ya registrada.

~~~powershell
$ErrorActionPreference = 'Stop'
$Qa = 'E:\Development\IA\Research\_qa\capataz'
$Repos = 'E:\Development\IA\Hoards\Faustusables'
$Hub = Join-Path $Repos 'Hoard Hub'
$Voice = Join-Path $Repos "Cyrano's Hoard"
$Vit = Join-Path $Repos "Vitruvius's Hoard"
$Launcher = Join-Path $Qa 't24v\nw.ps1'
$RunnerRoot = $env:RENAME_SOURCE_WORKTREE
if (!$RunnerRoot -or ![IO.Path]::IsPathRooted($RunnerRoot)) { throw 'Indica el worktree aprobado del Hub' }
$Runner = Join-Path $RunnerRoot 'scripts\rename_handover.py'
if (!(Test-Path -LiteralPath $Runner -PathType Leaf)) { throw 'Falta el programa del renombrado' }
git -C $RunnerRoot diff --quiet '8665faf24b740d1254f02420aa45bbed3b51eb16' -- scripts/rename_handover.py scripts/rename_handover_voice.py scripts/rename_handover_windows.py
if ($LASTEXITCODE) { throw 'El programa debe coincidir con el candidato aprobado' }
$Evidence = Join-Path $Qa ('t25\rename-r28-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$VoiceResult = $env:RENAME_VOICE_RESULT
$VoiceResultHash = $env:RENAME_VOICE_RESULT_SHA256
$VoiceGpuUuid = $env:RENAME_VOICE_GPU_UUID
if (!$VoiceResult -or ![IO.Path]::IsPathRooted($VoiceResult) -or
    !(Test-Path -LiteralPath $VoiceResult -PathType Leaf) -or $VoiceResultHash -notmatch '^[a-fA-F0-9]{64}$' -or
    $VoiceGpuUuid -notmatch '^GPU-[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$') {
    throw 'Indica el result.json, su huella y el UUID de la GPU confirmados por revisión'
}
$null = New-Item -ItemType Directory -Path $Evidence
$PlanPath = Join-Path $Evidence 'plan.json'
$Python = Join-Path $Hub '.venv\Scripts\python.exe'
$Plan = [ordered]@{
    mode = 'installed'
    root = $Evidence
    snapshot = (Join-Path $Evidence 'registro')
    hub_data = (Join-Path $Hub 'data')
    port = 8810
    python = $Python
    launcher = $Launcher
    window_watch = (Join-Path $Qa 'vigia-ventanas.log')
    new_voice_root = (Join-Path $Repos "Boltagon's Hoard")
    project = 'ad32060bd8f444bd'
    wav = (Join-Path $Qa 't24\inst-0830\veinte-euros.wav')
    voice_wheels = (Join-Path $Voice 'data\instalacion-voz-cb8b621\ruedas')
    voice_extra_wheels = $env:RENAME_VOICE_EXTRA_WHEELS
    voice_benchmark = @{result=$VoiceResult; sha256=$VoiceResultHash; gpu_uuid=$VoiceGpuUuid}
    repos = [ordered]@{
        hub   = @{path=$Hub;   before='084674f21d2be8502c969896b80e7d5b973f96f7'; after='8665faf24b740d1254f02420aa45bbed3b51eb16'}
        voice = @{path=$Voice; before='a0a63a3152fe5e63f081eef042968fa3a4078621'; after='49f1078503d4484a29e6110b6c8e3a655443a6dd'}
        vit   = @{path=$Vit;   before='9c73ad92f806d59ebb465178d38328620e0a81c0'; after='4e58e4d7f832ce322b10dbce3ef57babd4f5314b'}
    }
    approved_commits = @('8665faf24b740d1254f02420aa45bbed3b51eb16','49f1078503d4484a29e6110b6c8e3a655443a6dd','4e58e4d7f832ce322b10dbce3ef57babd4f5314b')
}
[IO.File]::WriteAllText($PlanPath, ($Plan | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
$Invoke = Join-Path $Evidence 'ejecutar.ps1'
$Entry = @'
param($Python,$Runner,$PlanPath,$Action,$Confirmation)
$ErrorActionPreference = 'Stop'
[Diagnostics.Process]::GetCurrentProcess().PriorityClass = 'BelowNormal'
& $Python -B $Runner $Action $PlanPath --confirm $Confirmation
exit $LASTEXITCODE
'@
[IO.File]::WriteAllText($Invoke, $Entry, [Text.UTF8Encoding]::new($true))
$Hidden = @('powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$Invoke,$Python,$Runner,$PlanPath)
& $Launcher -Log (Join-Path $Evidence 'instalacion.log') -Cwd $Hub -Cmd ($Hidden + @('apply','INSTALL'))
~~~

Si la rueda exacta de websockets del venv instalado queda fuera del directorio principal, indica un directorio local adicional mediante RENAME_VOICE_EXTRA_WHEELS. El inventario verifica que todos los pines se pueden reconstruir sin consultar la red.

El lanzador devuelve un PID. Antes del inventario, el instalador valida la huella, el SHA, el UUID aprobado y su correspondencia con el inventario, las veinte frases y las veinte cancelaciones. Si falla esa validación, el instalador sale antes de parar servicios. `registro\voice-benchmark-receipt.json` guarda los datos del recibo aceptado. El final de `instalacion.log` debe mostrar `LANZADOR EXIT 0` y deben figurar las nueve fases. `registro\accepted.json` recoge salud, PID del hijo, primer audio de frase nueva, transcripción, dictado y la ruta estable que se abrió después de `app_project_preview start`. Si falla después de crear `registro\journal.json`, conserva el plan tal cual y ejecuta la vuelta atrás con ese fichero.

~~~powershell
& $Launcher -Log (Join-Path $Evidence 'vuelta.log') -Cwd $Hub -Cmd ($Hidden + @('rollback','ROLLBACK'))
~~~

La vuelta atrás termina cuando `vuelta.log` muestra `LANZADOR EXIT 0` y el campo `rolled_back` de `registro\journal.json` es verdadero. Git debe mostrar de nuevo las tres bases exactas de la tabla. La vuelta atrás recupera también la guarda de duración de la versión de voz instalada. Si falta una copia, recupera el fichero íntegro cuya huella figura en `registro\recovery.json` y repite con el mismo plan. Las fichas sueltas nunca sustituyen el registro entero.

## Fases y vuelta atrás

Para volver, usa el mismo plan y el mismo registro de evidencias. El programa comprueba que el plan corresponde a ese registro y que los artefactos necesarios conservan su huella antes de parar un servicio. Si el Hub ya estaba parado, la recuperación sigue desde esa situación.

| Fase alcanzada | Riesgo | Qué hace la vuelta atrás |
| --- | --- | --- |
| 1. Inventario de Git, configuración y aplicaciones | Seguro | Conserva los procesos y las carpetas iniciales. |
| 2. Copia de los ficheros del registro y de los datos de arranque | Seguro | Conserva los procesos y las carpetas iniciales. |
| 3. Validación de copias | Seguro | Conserva los procesos y las carpetas iniciales. |
| 4. Parada de las aplicaciones y del Hub | Con riesgo | Recupera el Hub y las dos aplicaciones que figuraban activas en el inventario. Si el Hub original sigue activo, vuelve a arrancar únicamente la aplicación que falte. |
| 5. Avance rápido de los tres repositorios y mudanza de la carpeta | Con riesgo | Devuelve cada SHA y el nombre original de la carpeta. Repara los metadatos si se trata de un worktree enlazado. |
| 6. Aparcado y reconstrucción de la `.venv` de voz | Con riesgo | Devuelve a `.venv` el intérprete inventariado y aparta la reconstrucción incompleta. |
| 7. Migración de las fichas, rutas y registro | Con riesgo | Restaura byte por byte cada copia y quita los ficheros de configuración que creó la migración. |
| 8. Arranque del Hub y alta de las dos aplicaciones | Con riesgo | Para las aplicaciones candidatas y el Hub propietario del puerto. Restituye carpeta, SHA y registro, y resuelve de nuevo el catálogo con el ID original. |
| 9. Salud, dictado, voz y vista previa | Con riesgo | Hace la misma recuperación completa de la fase 8. |

La ficha conserva el lanzador entero de Vitruvius y su caché efectiva. Ajusta la ruta de la caché si cuelga de la carpeta de voz que se muda. La ficha de voz queda bajo `boltagon` con el módulo `boltagon_hoard`. Las variables `BOLTAGON_TTS_*` se completan a partir del perfil instalado y se mantienen las claves `CYRANO_TTS_*` de transición. El intérprete auxiliar, los pesos y la muestra viven debajo de `data` y se mudan con ella. La reconstrucción afecta a la `.venv` principal. Los entornos históricos se quedan dentro de la carpeta.
