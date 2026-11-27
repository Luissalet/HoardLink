# Escritorios privados para trabajadores de Ágora

En Windows, cada arranque, reanudación o relevo de un trabajador de Ágora crea un escritorio privado. Se incluyen los trabajadores de Faustus en el PC. El inicio del Hub y la apertura de aplicaciones usan sus lanzadores habituales. En otros sistemas se conserva el lanzamiento anterior.

La opción `agora.worker_private_desktop` viene activada. Para volver temporalmente al lanzador anterior, configura `"agora": {"worker_private_desktop": false}`. Si Windows rechaza el escritorio o el proceso, el Hub devuelve un error de arranque. Los trabajadores requieren un ejecutable nativo. Para las instalaciones npm de Claude y Codex, el Hub ejecuta directamente Node y el script del paquete.

El lanzador asigna la estación, el escritorio y el Job al crear el proceso con `CREATE_NO_WINDOW`. El trabajador hereda el escritorio y un handle de solo consulta que mantiene disponible el nombre del Job tras el cierre del Hub. El Hub registra ese nombre junto al recibo del proceso. Tras un reinicio comprueba la identidad del proceso y su pertenencia al Job antes de readoptarlo. La orden de detener termina todo el Job. La salida normal también recoge los descendientes bajo supervisión. [Creación de procesos](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags) · [Conexión a un escritorio](https://learn.microsoft.com/en-us/windows/win32/winstation/thread-connection-to-a-desktop) · [Permisos del Job](https://learn.microsoft.com/en-us/windows/win32/procthread/job-object-security-and-access-rights)


## Por qué puede aparecer una consola en Default

En el código de Microsoft Terminal, `_isInteractiveUserSession` consulta el PID y la estación del **anfitrión** de la consola. Devuelve falso en Session 0 o si la estación carece de `WSF_VISIBLE`. El nombre del escritorio del cliente no forma parte de ese filtro. Según el código, un escritorio B bajo la misma estación interactiva `WinSta0` tampoco cambia el resultado del filtro. Si el nuevo cliente solicita una consola visible y supera el resto de las comprobaciones, `attemptHandoff` activa `IConsoleHandoff` como servidor COM local fuera de proceso. El flujo puede crear `OpenConsole -Embedding` y llegar a la interfaz de Terminal. [Filtro de sesión y delegación](https://github.com/microsoft/terminal/blob/main/src/server/IoDispatchers.cpp#L124-L291) · [Cadena de activación COM](https://github.com/microsoft/terminal/blob/main/src/host/exe/exemain.cpp#L231-L263)

Esto explica cómo se puede alcanzar Default aunque los procesos ordinarios usen otro escritorio. Los cinco avisos de ventanas reales de #4133, registrados por dirección en el hilo 5066, ya muestran que repetir la prueba adversa en esta sesión puede afectar al escritorio de la persona. Una enumeración de Default que dispare un aborto sirve de detección después del hecho. La ausencia de ventanas en una compilación ordinaria no demuestra que una petición de consola nueva quede contenida.

La delegación sigue siendo posible cuando se solicita expresamente otra consola con el iniciador de procesos de PowerShell o el comando de inicio de CMD. La vigilancia de #4134 es independiente. La verificación de #4158 cubre el uso ordinario de la consola heredada.

## Private desktops for Agora workers

On Windows the Hub starts each Agora worker on a private desktop by default. This covers start, resume, automatic follow-ups, and local Faustus workers. The Hub and interactive apps use their established launch policies. Set `agora.worker_private_desktop` to `false` to use the previous worker launcher.

Workers need a native executable or the supported npm entry point. The launcher assigns the desktop and named Job when it creates the process with `CREATE_NO_WINDOW`. The worker inherits the desktop and a query-only handle that keeps the Job name available across Hub restarts. The new Hub verifies process identity and Job membership when it adopts the worker. Stop terminates the entire Job. An explicit request for a new console can still reach Windows Terminal outside the worker Job. The separate watcher in #4134 remains in place.
