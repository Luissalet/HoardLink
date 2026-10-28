# Kit de prueba diferencial (`hoard_link.parity`)

Compara dos programas que deben dar lo mismo sobre el mismo conjunto de casos: el **oráculo** (la referencia, lo que es
correcto por definición) y la **sonda** (el código que se prueba). Los dos son procesos que leen un fichero JSONL y escriben
una **traza**; el kit compara las trazas exactamente y dice cuál es la primera línea que difiere. Solo biblioteca estándar.

Las ideas (corpus con semilla, oráculo y sonda como procesos, trazas de estado y de llamadas al generador aleatorio,
comparación por bloques, versiones ancladas, informe con `scope`, ejecución selectiva y caché del oráculo) son las de la prueba
diferencial habitual; el código es propio.

## Qué hay

| Pieza | Para qué |
| --- | --- |
| `Corpus(seed)` | casos reproducibles: `grid()` para la rejilla de casos límite, `sample()` para casos aleatorios con la semilla; `f32_bits`/`f64_bits` para floats exactos (`-0.0`, `nan`) |
| `Command`, `Harness` | el oráculo, la sonda, el corpus, `depends_on`, `lock` y el `scope` obligatorio |
| `run_harness()` | ejecuta, compara y escribe `report.json`; salta si nada de lo que depende ha cambiado |
| `compare_traces()` | comparación exacta por bloques de 1 MB, con la primera divergencia (caso, evento, ruta, esperado/obtenido, entrada) |
| `survey_traces()` | recorre todos los casos y cuenta cuántos divergen por categoría |
| `make_lock()` / `verify_lock()` | hashes de artefactos y versiones de herramientas; un lock que no se sostiene detiene la ejecución |
| `OracleCache` | repite la salida del oráculo si no cambió su comando, el corpus, el lock ni los ficheros que lee |
| `TraceWriter`, `RecordingRandom`, `run_cli` | para escribir oráculos y sondas en Python |

## Formato de traza

Una línea por caso:

```json
{"id":"c000007","ev":[["state","after",{"hp":41}],["prng","randint",93],["query","typeMultiplier",2,["fire"]]],"out":{"damage":41}}
```

`ev` es la lista **ordenada** de eventos: `["state", nombre, valor]`, `["prng", nombre, valor]` (cada llamada al generador
aleatorio) y `["query", nombre, resultado, args]` (cada consulta a un colaborador). El orden y el número forman parte del
resultado: dos motores que tiran los dados en distinto orden divergen en la primera tirada distinta, no en el resultado final.
`out` es el resultado del caso, o `err` si lanzó una excepción. Cualquier lenguaje escribe lo mismo: el corpus entra en
`inputs.jsonl` (`{"id","input"}` por línea) y cada programa recibe `<inputs> <salida>` como últimos argumentos (o las marcas
`{inputs}` y `{output}` en su `argv`).

### Valores con tolerancia

Cuando la referencia solo puede decir «en este intervalo», el oráculo escribe `{"$range":[lo,hi]}` (acepta un número, o una lista
de números, dentro de `[lo,hi]`), `{"$approx":[valor,tolerancia]}` o `{"$oneof":[a,b,...]}` (acepta cualquiera de los valores, del mismo tipo y exactos: el oráculo
vio varios resultados legales, por ejemplo uno por tirada de daño). Todo lo demás se compara exacto, y `1` y `1.0` son
resultados distintos. El informe lo recuerda en su `scope`.

## Fichero de harness

```python
from hoard_link.parity import Harness, Command, Corpus

def harness():
    def corpus():
        c = Corpus(seed=42)
        c.grid(lambda hp, dmg: {"hp": hp, "dmg": dmg}, hp=[0, 1, 255], dmg=[0, 1, 300])
        c.sample(2000, lambda rng: {"hp": rng.randrange(1, 500), "dmg": rng.randrange(0, 600)})
        return c
    return Harness(name="damage", root=HERE, corpus=corpus,
                   oracle=Command(["node", "oracle.mjs"]), probe=Command([sys.executable, "probe.py"]),
                   depends_on=["probe.py", "src/**/*.py"], oracle_depends_on=["oracle.mjs"],
                   lock="lock.json", lock_artifacts={"engine": "vendor/engine.js"},
                   scope="Qué NO cubre este harness (obligatorio).")
```

## Uso

```
python -m hoard_link.parity run harness.py [--state-dir .parity] [--fresh] [--force] [--stable 3] [--survey]
python -m hoard_link.parity run h1.py h2.py --changed origin/main      # solo los que un diff puede afectar
python -m hoard_link.parity run h1.py --changed-files lista.txt        # lo mismo para rutas listadas en un fichero
python -m hoard_link.parity compare oraculo.jsonl sonda.jsonl --inputs inputs.jsonl [--survey]
python -m hoard_link.parity status --state-dir .parity
python -m hoard_link.parity lock harness.py                            # fija hashes y versiones
```

Códigos de salida: 0 pasa o se salta, 1 divergencia, 2 error (proceso caído, lock roto, traza vacía, fuentes editadas durante
la ejecución).

## Reglas que aplica

- **Una salida vacía no demuestra nada**: una traza vacía o ausente es un error, nunca un pase.
- **Anclaje**: con `lock`, un hash o una versión de herramienta distintos detienen la ejecución; si una fuente del harness
  cambia mientras corre, la ejecución se aborta.
- **`scope` obligatorio**: un `Harness` sin `scope` no se crea. `report.json` lleva lo que el harness **no** cubre.
- **Selección**: la clave de un harness es el hash de lo que cubre `depends_on` (más el fichero del harness y el lock); si no
  cambia y el último resultado fue un pase, se salta sin tocar su `report.json`. `--stable N` exige N pases seguidos (un
  harness nuevo debería pasar tres veces).
- **Caché del oráculo**: clave = comando + hash del corpus + lock + ficheros de `oracle_depends_on`; la copia se verifica contra
  el hash guardado y una entrada dañada se descarta.

## Informe (`<state-dir>/<harness>/report.json`)

`status` (`pass`, `fail`, `error`), `corpus` (casos, sha256, semilla), `oracle`/`probe` (comando, segundos, sha256 de la salida,
si el oráculo vino de caché), `matched`, `divergence` (si falla: línea, caso, ruta, tipo de evento, esperado, obtenido,
contexto de los eventos anteriores y entrada del caso), `survey` (con `--survey`), `lock`, `sources`, `stability` y `scope`.

## Hub

El Hub expone `hub_parity_compare` (compara dos trazas), `hub_parity_status` (estado de una carpeta de ejecuciones) y
`hub_parity_run` (ejecuta un fichero de harness); ver `docs/HUB.md`.

## Primeros usos

Silver's Hoard: poke-engine contra `@pkmn/sim` (`engine/parity/`), con el informe de la primera divergencia en daño, orden y efectos.
