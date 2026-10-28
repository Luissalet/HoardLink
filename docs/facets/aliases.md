# Names in Spanish and English (`aliases`)

One entity map for the whole family, in `hoard_link/aliases/` with the data in `hoard_link/_data/aliases.json` (about 290 KB,
shipped with the package, nothing fetched at run time). Module `hoard_link/hub/aliases.py`, facet id `aliases`. Read-only.

## What it knows

| kind | entries | Source |
|---|---|---|
| `pokemon` (species and forms: Alola, Galar, Hisui, Paldea, Gigantamax, Primal, Mega, Mega X/Y) | 1416 | Showdown data (English), HoardDex catalogue and Silver's `names-es.js` (Spanish), form names by rule |
| `move`, `ability`, `item`, `nature`, `type` | 990, 378, 2272, 25, 19 | the same |
| `badge`, `place` (gyms, leagues, cities) | 61, 67 | Silver's `game_leagues.py` |
| `ingredient` | 172 | the CookHoard ingredient dictionary |

Regenerate with `python scripts/build_alias_data.py --work <dir>` (flags `--silver`, `--fielddex`, `--cookhoard`,
`--skip-dump`; the Node dumps are `scripts/alias_dump_showdown.mjs` and `scripts/alias_dump_cook.mjs`). The sources used are
recorded in the file's `sources`.

## Matching

Accents, case, punctuation and spacing do not matter (`hoard_link.text.fold`). Stages, in order, with the score they give:
exact name or alias (1.0), form parser such as `Charizard Mega X` or `Raichu de Alola` (0.96), prefix, every query token
inside the name, then fuzzy (difflib over a trigram index) for typos. `how` in each match says which stage matched.

## HTTP

| Route | Result |
|---|---|
| `GET /api/aliases/lookup?q=lanzallamas&kind=move,item&limit=5` (or `POST {query, kinds, limit}`) | `{ok, query, detected_lang, matches: [{kind, id, en, es, score, how, matched, matched_lang, extra}]}` |
| `GET /api/aliases/translate?q=Mega Charizard X&to=es` | `{ok, found, text, match, matches}`: the name in the other language |
| `GET /api/aliases/expand?q=mejor set de Garchomp con Cinta Elección` (or `POST {text, kinds, max_variants}`) | `{ok, mentions, swaps, variants: [{text, replaced}], terms, ambiguous}` |
| `GET /api/aliases/stats` | counts per kind and sources |

`expand` finds the longest known names inside a sentence (stop words are ignored) and returns the sentence with those
names swapped to the other language, plus the swapped names as `terms`. A caller whose search came back empty retries with
`variants[0].text`, or with `terms`.

## Agent tools

`hub_alias_lookup` (query, kinds, to, limit; with `to` it returns the translation) and `hub_alias_expand` (text, kinds,
max_variants). Both are read-only (`readOnlyHint`).

## Boundaries

Names only: it does not know stats, learnsets or prices. A name that exists only in a language other than Spanish and
English is not found. Compound forms such as `Tauros Paldea-Blaze` get a plain Spanish name.
