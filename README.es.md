# Hoard Link

### Una sola respuesta compartida a "¿qué servidor de modelo uso ahora mismo?"

**El backend de modelos compartido para aplicaciones controladas por
agentes: elige el servidor local que ya está cargado para una capacidad
en vez de cargar una segunda copia de un modelo.**

[English](README.md) · [Primeros pasos](#instalación-vendorización) ·
[Uso con Faustus](#por-qué-importa-compartir-en-una-máquina-limitada-por-gpu) ·
[API](#api) ·
[Portfolio](https://luissalet.github.io/Portfolio/#projects)

Hoard Link es una librería de Python pequeña — solo librería estándar más
`httpx`, sin servidor, sin puerto, sin interfaz — que un conjunto de
aplicaciones locales pueden vendorizar (cada una con su propia copia) para
responder a una sola pregunta: **para la capacidad X (`llm`, `vision`,
`embeddings`, `tts`, `stt`, `image`, `video`, `music`), ¿qué servidor y qué
modelo uso ahora mismo, y por qué?** — y después hacer la llamada de verdad.

## Por qué importa compartir en una máquina limitada por GPU

En una máquina que ejecuta **Faustus** (un espacio de trabajo de IA local)
más varias aplicaciones plugin, la GPU es el recurso escaso. Una máquina
típica aquí tiene un `llama-server` (llama.cpp) sirviendo un modelo de 27B
que por sí solo ocupa la mayor parte de una tarjeta de 60 GB, a veces
también Ollama y ComfyUI. Si cada aplicación que quisiera hacer una
llamada a un LLM cargara su *propio* modelo, la máquina se quedaría sin
VRAM al arrancar la segunda aplicación. El trabajo de Hoard Link es hacer
que cada aplicación pregunte "¿alguien ya está sirviendo lo que
necesito?" antes de pedirle a un servidor que cargue nada, y no molestar
nunca la conversación que la persona esté teniendo con Faustus en ese
momento.

Hoard Link no ejecuta su propio servidor de modelos ni gestiona ciclos de
vida. Resuelve una dirección y — para chat/embeddings/tts — hace la
llamada HTTP por ti contra el servidor que ha encontrado.

## Orden de resolución

Para cada capacidad, en este orden:

1. **Configuración explícita** — el `backend.json` propio de la
   aplicación y las variables de entorno `HOARD_<CAP>_URL` /
   `HOARD_<CAP>_MODEL` / `HOARD_FAUSTUS_URL` / `HOARD_FAUSTUS_TOKEN` /
   `HOARD_COMFY_URL`. Una capacidad con `url` (o `command`) gana sin más
   y no se comprueba; una dirección caducada aparece como `BackendError`
   (status `0`: sin respuesta HTTP) la primera vez que se usa de verdad.
   Un `model` sin `url` es solo una *preferencia*, que se aplica allí
   donde los pasos siguientes tienen donde elegir (modelos residentes de
   Ollama, la lista de modelos de Faustus, la lista de un servidor
   compatible con OpenAI).
2. **Faustus** — si una instancia de Faustus responde `GET /api/health`
   como saludable en `127.0.0.1:7000` o `:7001` (configurable), Hoard Link
   lee `GET /api/models` (con un token si hay uno configurado) para
   obtener el registro de servidores de modelos que usa el propio
   Faustus, y habla con ese servidor **directamente** — el mismo
   servidor, el mismo modelo residente, que es la forma real de
   compartir. Solo se usan entradas locales (categoría `local`, URL de
   loopback): un endpoint en la nube del registro de Faustus se ignora
   para que los datos de la aplicación nunca salgan de la máquina. Una
   entrada de Ollama se contrasta con el `/api/ps` de ese Ollama antes de
   declararla residente. Sin token configurado la petición va sin él (un
   Faustus con la autenticación desactivada responde igual); un 401
   entonces indica que hace falta un token. Para `tts`/`stt` también prueba los propios
   `/api/tts/*`/`/api/stt/*` de Faustus; un 401/403 ahí se registra como
   motivo (una sesión solo de navegador) y la resolución sigue adelante
   en vez de fallar.
3. **Servidores compartidos en loopback**, sondeados en paralelo con 1s de
   timeout de reloj por petición y cacheados 30s (sin duplicados: las
   resoluciones simultáneas comparten un mismo sondeo; el cliente de
   sondeo ignora `HTTP(S)_PROXY`): llama.cpp (`8080`–`8090`, vía
   `/props`, `/v1/models`, `/slots`), Ollama (`11434`, vía `/api/ps` para
   modelos **residentes**, `/api/tags`, `/api/show` para capacidades), un
   servidor de chat genérico compatible con OpenAI en el puerto `1234` (solo
   `llm`), y ComfyUI (`8188`, `image`/`video`). Un puerto solo cuenta como
   llama-server si su `/props` trae claves de llama-server. Ningún sondeo
   lanza nunca una excepción, responda el puerto con el JSON que sea.
4. **Nada** — la capacidad vuelve como `unavailable`, con la lista de
   motivos recogidos por el camino (por qué Faustus no respondió, por qué
   ningún servidor en loopback encajaba, etc.) para que la pantalla de
   Ajustes de una aplicación pueda mostrarle a una persona exactamente qué
   arreglar.

## Políticas

- **`only_resident = True` por defecto.** Hoard Link elige modelos que ya
  están cargados — `/api/ps` de Ollama, o un llama-server, que siempre
  sirve exactamente el único modelo con el que se arrancó. No le pedirá a
  un servidor que cargue un modelo distinto salvo que la configuración de
  la aplicación ponga `allow_load: true` para esa capacidad (u
  `only_resident: false` en general), y nunca envía `keep_alive`. Un
  modelo que habría que cargar se comprueba igualmente (`/api/show`) para
  la capacidad pedida, y un modelo solo de embeddings nunca se elige para
  `llm`.
- **Cede el paso al primer plano.** `await link.wait_idle("llm",
  max_wait_s=30)` es `True` de inmediato cuando el llama-server resuelto
  no tiene ningún slot con `is_processing`, y si no, sondea cada 2s hasta
  que lo esté o hasta que pasen `max_wait_s` (entonces `False`, y quien
  llama decide posponer su trabajo en segundo plano). Funciona venga el
  llama-server del sondeo, del registro de Faustus o de la configuración
  explícita (`provider: "llamacpp"`). Ollama no expone
  ninguna señal de ocupado, así que `wait_idle` devuelve siempre `True`
  para él — documentado aquí en vez de dejarlo a adivinar.
- **Los trabajos de GPU conocen la VRAM libre antes.** `gpu_free_mb()`
  ejecuta `nvidia-smi --query-gpu=index,memory.total,memory.used
  --format=csv,noheader,nounits` (best effort — sin GPU NVIDIA o sin
  `nvidia-smi` en el PATH simplemente da `[]`; en Windows se ejecuta con
  `CREATE_NO_WINDOW` y también busca en la carpeta antigua `NVSMI`).
  `resolve("image")` lo ejecuta en un hilo aparte e informa de la GPU con
  más memoria libre y de una lista por GPU. `ComfyClient` nunca llama
  a `/free` por su cuenta; solo cuando quien llama lo pide.
- **Cada resolución se explica sola.** `Resolution.reason` es una frase
  apta para una pantalla de Ajustes, p. ej. `"llm -> llama.cpp at
  127.0.0.1:8081 (qwen3.8-27b-q8-llamacpp), from Faustus registry;
  resident"`.

## Instalación (vendorización)

Hoard Link está pensado para ser **copiado**, no instalado como
dependencia de terceros, para que cada aplicación lleve una copia
comprometida en su repositorio del comportamiento exacto contra el que se
escribieron sus tests. Copia el directorio `hoard_link/` entero (todos
los `.py`, sin `__pycache__/`) dentro de tu paquete, no edites la copia,
y para actualizar sustituye el directorio completo y anota el commit de
Hoard Link del que copiaste:

```
<app_pkg>/
  hoard_link/        <- copia del directorio hoard_link/ de este repo
  ...
```

```python
from .hoard_link import Link, LinkConfig
```

Si tu aplicación ya gestiona sus propias dependencias y tiene `httpx`,
esa es la única dependencia de terceros. Python 3.11+, librería estándar +
`httpx>=0.27,<1.0`, sin código específico de plataforma — funciona igual
en Windows y en Linux.

## Esquema de `backend.json`

Todas las claves son opcionales; lo que no se fije cae al siguiente paso
(Faustus, luego el sondeo en loopback).

```json
{
  "only_resident": true,
  "faustus": {"url": "http://127.0.0.1:7000", "token": "ody_..."},
  "comfy": {"url": "http://127.0.0.1:8188"},
  "capabilities": {
    "llm": {
      "url": "http://127.0.0.1:8081/v1/chat/completions",
      "model": "qwen3.8-27b-q8-llamacpp",
      "api": "openai",
      "provider": "llamacpp",
      "allow_load": false
    },
    "tts": {"command": ["piper", "--model", "es_ES.onnx", "--output_file", "{out}"]}
  }
}
```

Variables de entorno (máxima prioridad, se aplican encima del archivo):
`HOARD_<CAP>_URL`, `HOARD_<CAP>_MODEL` (p. ej. `HOARD_LLM_URL`,
`HOARD_VISION_MODEL`), `HOARD_FAUSTUS_URL`, `HOARD_FAUSTUS_TOKEN`,
`HOARD_COMFY_URL`. Una variable vacía cuenta como no definida.

- `url` puede ser la raíz del servidor (`http://127.0.0.1:8081`), una base
  `/v1` o un endpoint completo; chat y embeddings añaden cada uno la ruta
  que necesitan. Una URL bajo `/api/` implica `"api": "ollama"`; si no,
  `"openai"`.
- `command` es una lista de cadenas. `{text}`, `{voice}` y `{out}` se
  sustituyen literalmente; sin `{text}` el texto se escribe en la stdin
  del comando (así lo lee Piper), sin `{out}` el audio se lee de stdout.
  Se ejecuta sin ventana de consola y con un timeout de 120 s.
- El archivo se lee como UTF-8 con o sin BOM (lo que guarda el Bloc de
  notas por defecto).

## Cómo generar un token de Faustus

Desde Faustus, genera un token con el scope `chat` (el que aceptan hoy el
registro de modelos y los endpoints de TTS/STT) y ponlo en el
`backend.json` de la aplicación bajo `faustus.token`, o expórtalo como
`HOARD_FAUSTUS_TOKEN` para el proceso de la aplicación. Los tokens de
Faustus tienen la forma `ody_...`; trátalos como cualquier otro secreto
local (mantenlos fuera del propio historial de git de la aplicación —
`backend.json` suele vivir bajo el `data/` de la aplicación, que está en
el `.gitignore`).

## API

```python
from hoard_link import Link, LinkConfig

link = Link(LinkConfig.load(path_to_backend_json, env=os.environ, app="argus"))

res = await link.resolve("vision")   # Resolution(capability, provider, url, model, api, state, reason, details)

status = await link.status()         # dict para GET /api/backend en la aplicación: la Resolution de cada capacidad

text = await link.chat(
    [{"role": "user", "content": "..."}],
    images=[jpeg_bytes],
    max_tokens=300,
    temperature=0.2,
    capability="vision",
    response_format=None,
)                                     # -> ChatResult(text, model, provider, usage, elapsed_ms, reasoning)

vecs = await link.embed(["a", "b"])  # cuando resuelve un servidor de embeddings; si no, lanza Unavailable

wav = await link.tts("Hola", voice=None)   # TTS de Faustus o un comando configurado; si no, Unavailable

comfy = await link.comfy()           # ComfyClient, o None si no resuelve nada

idle = await link.wait_idle("llm", max_wait_s=30)   # True/False, ver Políticas arriba

link.sync.chat(...)                  # las mismas llamadas, bloqueantes, para código de aplicación síncrono
```

- `api` es `"openai"` (`/v1/chat/completions`, imágenes como URLs de datos
  `image_url` en el último mensaje de usuario) u `"ollama"` (`/api/chat`,
  `images: [base64]` en el último mensaje de usuario, `stream: false`).
- El razonamiento se mantiene fuera de `ChatResult.text` y se expone en
  `ChatResult.reasoning`: bloques `<think>...</think>` cerrados, un
  `</think>` huérfano (la plantilla de chat abrió la etiqueta en el
  prompt), un `<think>` sin cerrar (cortado por `max_tokens`) y los campos
  aparte (`reasoning_content` de llama-server, `thinking` de Ollama).
- `response_format` se pasa tal cual en el dialecto OpenAI y se traduce al
  `format` de Ollama (`json_object` -> `"json"`, `json_schema` -> el
  esquema). Las imágenes llevan su tipo MIME real (JPEG, PNG, GIF, WebP).
- `ComfyClient(url)`: `system_stats()`, `object_info(node=None)`,
  `upload_image(bytes, filename, overwrite=True)`,
  `queue(workflow: dict, client_id) -> prompt_id`,
  `wait(prompt_id, timeout_s, on_progress=None)`,
  `outputs(prompt_id) -> list[OutputFile]`, `download(output) -> bytes`,
  `interrupt()`, `free(unload_models=False, free_memory=False)`.
  `queue()` valida el **formato API** (un diccionario de id de nodo ->
  `{class_type, inputs}`) y lanza un `ValueError` claro si recibe la
  exportación de la interfaz, `{"nodes": [...], "links": [...]}`. Los
  errores HTTP lanzan `BackendError` con el cuerpo de la respuesta (así
  los `node_errors` de `/prompt` llegan a quien llama), y `wait()` lanza
  `BackendError` si el trabajo terminó con un error de ejecución.
- Errores: `Unavailable(capability, reasons)` cuando no se resuelve nada,
  `BackendError(provider, status, body_excerpt)` para cualquier llamada
  fallida a un servidor resuelto: un estado que no es 2xx, una respuesta
  que no es el JSON esperado, o `status == 0` cuando no hubo respuesta
  HTTP en absoluto.
- `link.sync.*` corre en un hilo con su propio bucle de eventos y su
  propio cliente HTTP, así que una aplicación puede mezclar `await
  link.chat()` y `link.sync.chat()` en el mismo `Link`. Si inyectas tu
  propio `httpx.AsyncClient`, se comparte tal cual; no mezcles ambos
  estilos sobre él. Llamar a `link.sync.*` desde ese hilo privado (p. ej.
  dentro de un callback) lanza `RuntimeError` en vez de bloquearse.

## Límites (lo que esta librería no hace)

- **Sin generación de música.** El `object_info` de ComfyUI no da una
  señal fiable para grafos de audio/música como sí la da
  `CheckpointLoaderSimple` para checkpoints de imagen, así que `music`
  solo se resuelve mediante configuración explícita o una entrada del
  registro de Faustus que encaje, nunca mediante sondeo en loopback.
- **Sin progreso por websocket para ComfyUI.** `ComfyClient.wait()`
  sondea `/history/{id}`; no abre `/ws` para progreso empujado. La
  especificación lo permitía solo "si era sencillo" — sondear cada
  segundo es sencillo, correcto y comprobable sin conexión; un cliente de
  websocket es una cosa más que mantener viva y reconectar, que no
  compensaba para esperar a que termine un trabajo.
- **`resolve()` no verifica la configuración explícita.** La fuente 1 del
  orden de resolución se confía tal cual; una entrada caducada en
  `backend.json` aparece como un `BackendError` con `status == 0` en la
  primera llamada real, en vez de comprobarse por adelantado. Así la
  configuración explícita hace una sola cosa (tener prioridad sobre todo
  lo demás) en vez de dos.
- **La fachada síncrona es un hilo de fondo por `Link`.** Está pensada
  para una aplicación cuyos propios manejadores de peticiones son
  síncronos; no es un pool de hilos y no paraleliza llamadas al mismo
  `Link`. Llamada desde código asíncrono bloquea ese hilo hasta tener el
  resultado, así que ahí mejor `await`.
- **Sin TTS por URL HTTP.** `tts` funciona con el servicio TTS de Faustus
  o con un `command` configurado; una capacidad `tts` con solo `url` se
  resuelve pero `link.tts()` lanza `Unavailable`.
- **Sin endpoints en la nube.** Las entradas no locales del registro de
  Faustus se ignoran a propósito.

## Tests

```
python3 -m venv .venv
. .venv/bin/activate   # .venv\Scripts\activate en Windows
pip install -e ".[dev]"
pytest -q
```

163 tests, sin conexión (`httpx.MockTransport`), en unos 3 segundos. Los
únicos sockets reales están en los tests de la fachada síncrona, que
arrancan un servidor HTTP mínimo en un puerto efímero de `127.0.0.1`
para reproducir la reutilización de conexiones entre bucles de eventos;
los tests del comando TTS usan el propio intérprete de Python como
"binario de TTS". Ningún test necesita red ni descargar un modelo, así
que el flujo de CI de abajo funciona igual sin conexión.

## Licencia

MIT — ver [LICENSE](LICENSE).
