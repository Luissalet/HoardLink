"""Rebuild ``hoard_link/_data/aliases.json``, the Spanish/English entity map served by ``hoard_link.aliases``.

The file is generated once and committed; the runtime never reads another repository. Re-run this when a source
changes::

    python scripts/build_alias_data.py --work <scratch dir>

Sources (read-only, found next to this repository by default):

* Showdown's data (``@pkmn/sim`` in Silver's ``node_modules``): English names, ids and form names of species, moves,
  abilities, items, natures and types.
* HoardDex's ``src/data/catalog.json`` (PokeAPI data in Spanish): Spanish names, and its ``search-aliases.json``
  (names in other languages) as the English fallback for entries Showdown does not have.
* Silver's ``client/src/battle/names-es.js`` (Spanish names keyed by Showdown id): Spanish names the catalog lacks.
* Silver's ``game_leagues.py``: gym badges and the cities they belong to, in both languages.
* CookHoard's ingredient dictionary (``packages/core/src/dictionary.ts``): ingredients with their Spanish aliases.

Entry shape per kind: ``[id, en, es, [aliases...], extra]`` where ``extra`` is the national dex number for species,
the category for ingredients and the type for badges, and 0/"" otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PARENT = REPO.parent
OUT = REPO / "hoard_link" / "_data" / "aliases.json"

# Showdown forme names -> English adjectives people use, and the Spanish way of writing the form after the species.
FORME_EN = {"alola": "Alolan {}", "galar": "Galarian {}", "hisui": "Hisuian {}", "paldea": "Paldean {}",
            "gmax": "Gigantamax {}", "primal": "Primal {}", "mega": "Mega {}", "mega-x": "Mega {} X", "mega-y": "Mega {} Y"}
FORME_ES = {"alola": "{} de Alola", "galar": "{} de Galar", "hisui": "{} de Hisui", "paldea": "{} de Paldea",
            "gmax": "{} Gigamax", "primal": "{} Primigenio", "mega": "Mega-{}", "mega-x": "Mega-{} X", "mega-y": "Mega-{} Y"}


def key(text: str) -> str:
    """Showdown-style id: lowercase letters and digits only (accents folded)."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", plain)


def run_node(script: Path, out: Path, cwd: Path, *extra: str) -> None:
    subprocess.run(["node", *extra, str(script), str(out)], cwd=cwd, check=True)


def titlecase_identifier(identifier: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in identifier.replace("-", " ").split())


def build(paths: dict[str, Path], work: Path) -> dict:
    showdown = json.loads((work / "showdown.json").read_text(encoding="utf-8"))
    cook = json.loads((work / "cook.json").read_text(encoding="utf-8"))
    catalog = json.loads((paths["fielddex"] / "src/data/catalog.json").read_text(encoding="utf-8"))
    multi = json.loads((paths["fielddex"] / "src/data/search-aliases.json").read_text(encoding="utf-8"))
    js = (paths["silver"] / "client/src/battle/names-es.js").read_text(encoding="utf-8")
    silver_es = json.loads(js[js.index("export default ") + len("export default "):].rstrip().rstrip(";"))

    kinds: dict[str, list] = {}

    # ---- species and forms -------------------------------------------------------------------------------
    dex_es: dict[int, str] = {}
    form_es_by_id: dict[str, str] = {}
    for p in catalog["pokemon"]:
        if p["default"]:
            dex_es[p["species"]] = p["names"]["es"]
        elif p["form"] and p["form"][0].get("name"):
            form_es_by_id[key(p["slug"])] = p["form"][0]["name"]
    sd_species = {s["id"]: s for s in showdown["species"]}
    pokemon = []
    for s in showdown["species"]:
        base_es = dex_es.get(s["num"]) or s["base"]
        forme = key(s["forme"]) and s["forme"].lower().replace(" ", "-")
        en_alias: list[str] = []
        es_alias: list[str] = []
        if s["forme"]:
            tpl_en = FORME_EN.get(forme)
            tpl_es = FORME_ES.get(forme)
            if tpl_en:
                en_alias.append(tpl_en.format(s["base"]))
            if tpl_es:
                es = tpl_es.format(base_es)
                es_alias.append(f"{base_es} {s['forme']}")
            elif s["id"] in form_es_by_id:
                name = form_es_by_id[s["id"]]
                if name.lower().startswith("forma de "):
                    es = f"{base_es} de {name[9:]}"
                elif base_es.lower() in name.lower():
                    es = name
                else:
                    es = f"{base_es} {name}"
                es_alias.append(f"{base_es} {name}")
            else:
                es = f"{base_es} {s['forme']}"
        else:
            es = base_es
        aliases = list(dict.fromkeys(a for a in en_alias + es_alias if key(a) not in (key(s["name"]), key(es))))
        pokemon.append([s["id"], s["name"], es, aliases, s["num"]])
    kinds["pokemon"] = pokemon

    # ---- moves, abilities, items, natures, types -------------------------------------------------------------
    def simple(kind: str, sd_rows: list[dict], cat_rows: list[dict], silver_map: dict[str, str], multi_map: dict[str, list]) -> None:
        sd_by = {r["id"]: r["name"] for r in sd_rows}
        es_by = {key(r["identifier"]): (r["identifier"], r["name"], str(r["id"])) for r in cat_rows if r.get("identifier")}
        ids = list(dict.fromkeys([*sd_by, *es_by, *silver_map]))
        rows = []
        for i in ids:
            ident, es_cat, pid = es_by.get(i, ("", "", ""))
            es = es_cat or silver_map.get(i, "")
            en = sd_by.get(i)
            if not en:
                title = titlecase_identifier(ident) if ident else ""
                en = next((n for n in multi_map.get(pid, []) if title and key(n) == key(title)), title)
            if not en:
                continue
            extra = [n for n in ([silver_map.get(i, "")] if silver_map.get(i) and silver_map.get(i) != es else []) if key(n) not in (key(en), key(es))]
            rows.append([i, en, es or en, extra, 0])
        kinds[kind] = rows

    simple("move", showdown["moves"], catalog["moves"], silver_es["moves"], multi["moves"])
    simple("ability", showdown["abilities"], catalog["abilities"], silver_es["abilities"], multi["abilities"])
    simple("item", showdown["items"], catalog["items"], silver_es["items"], multi["items"])
    nat_es = {key(r["identifier"]): r["name"] for r in catalog["natures"]}
    kinds["nature"] = [[r["id"], r["name"], nat_es.get(r["id"]) or silver_es["natures"].get(r["id"], r["name"]), [], 0] for r in showdown["natures"]]
    type_es = {key(r["slug"]): r["name"] for r in catalog["types"]}
    kinds["type"] = [[r["id"], r["name"], silver_es["types"].get(r["id"]) or type_es.get(r["id"], r["name"]), [], 0] for r in showdown["types"]]

    # ---- badges and the cities they belong to (Silver's game_leagues.py) ----------------------------------------
    tree = ast.parse((paths["silver"] / "silvers_hoard/game_leagues.py").read_text(encoding="utf-8"))
    badges: dict[str, list] = {}
    cities: dict[str, list] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            try:
                rows = ast.literal_eval(node.value)
            except ValueError:
                continue
            for row in rows:
                if isinstance(row, tuple) and len(row) >= 5 and str(row[3]).endswith("Badge"):
                    city, city_es, typ, badge, badge_es = row[:5]
                    badges.setdefault(key(badge), [key(badge), badge, badge_es, [], typ])
                    cities.setdefault(key(city), [key(city), city, city_es, [], 0])
    # the same badge name exists in several regions (Rock Badge): keep one entry per id
    kinds["badge"] = sorted(badges.values())
    kinds["place"] = sorted(cities.values())

    # ---- ingredients (CookHoard's dictionary) ---------------------------------------------------------------
    ingredients = []
    for i in cook:
        ingredients.append([i["id"], i["nameEn"] or i["name"], i["name"],
                            [a for a in dict.fromkeys(i["aliases"]) if key(a) not in (key(i["name"]), key(i["nameEn"]))], i["category"]])
    kinds["ingredient"] = ingredients

    return {"schema": 1,
            "sources": {
                "showdown": "@pkmn/sim 0.10.11 data (MIT)", "catalog": "HoardDex catalog.json (PokeAPI data, CC BY-SA/ MIT mix as in HoardDex docs)",
                "silver": "Silver's Hoard names-es.js and game_leagues.py", "cookhoard": "CookHoard ingredient dictionary"},
            "kinds": kinds}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, type=Path, help="scratch folder for the intermediate dumps")
    ap.add_argument("--silver", type=Path, default=PARENT / "Silver's Hoard")
    ap.add_argument("--fielddex", type=Path, default=PARENT / "FieldDex")
    ap.add_argument("--cookhoard", type=Path, default=PARENT / "CookHoard")
    ap.add_argument("--skip-dump", action="store_true", help="reuse showdown.json / cook.json already in --work")
    args = ap.parse_args()
    paths = {"silver": args.silver, "fielddex": args.fielddex, "cookhoard": args.cookhoard}
    args.work.mkdir(parents=True, exist_ok=True)
    if not args.skip_dump:
        sd = args.work / "dump_showdown.mjs"
        sd.write_text((HERE / "alias_dump_showdown.mjs").read_text(encoding="utf-8").replace("__SILVER__", args.silver.as_posix()), encoding="utf-8")
        run_node(sd, args.work / "showdown.json", args.silver)
        ck = args.work / "dump_cook.mjs"
        ck.write_text((HERE / "alias_dump_cook.mjs").read_text(encoding="utf-8").replace("__COOK__", args.cookhoard.as_posix()), encoding="utf-8")
        run_node(ck, args.work / "cook.json", args.cookhoard, "--import", "tsx")
    data = build(paths, args.work)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print({k: len(v) for k, v in data["kinds"].items()}, OUT.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
