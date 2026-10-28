"""The Spanish/English alias index: exact names, forms, accents, typos, translation and query expansion."""

from __future__ import annotations

import json
import time

import pytest

from hoard_link.aliases import KINDS, expand, get_index, lookup, stats, translate
from hoard_link.aliases.forms import candidates


def top(query, **kw):
    rows = lookup(query, **kw)
    assert rows, f"no match for {query!r}"
    return rows[0]


def test_data_covers_every_kind_with_both_languages():
    counts = stats()["kinds"]
    assert set(counts) == set(KINDS)
    assert counts["pokemon"] > 1300 and counts["move"] > 900 and counts["ability"] > 300 and counts["item"] > 500
    assert counts["badge"] >= 50 and counts["ingredient"] >= 100 and counts["nature"] == 25 and counts["type"] == 19


@pytest.mark.parametrize("query,kind,en,es", [
    ("lanzallamas", "move", "Flamethrower", "Lanzallamas"),
    ("FLAMETHROWER", "move", "Flamethrower", "Lanzallamas"),
    ("ida y vuelta", "move", "U-turn", "Ida y Vuelta"),
    ("u turn", "move", "U-turn", "Ida y Vuelta"),
    ("Intimidacion", "ability", "Intimidate", "Intimidación"),
    ("pañuelo elección", "item", "Choice Scarf", "Pañuelo Elección"),
    ("panuelo eleccion", "item", "Choice Scarf", "Pañuelo Elección"),
    ("Medalla Cascada", "badge", "Cascade Badge", "Medalla Cascada"),
    ("Ciudad Plateada", "place", "Pewter City", "Ciudad Plateada"),
    ("calabacín", "ingredient", "Courgette", "Calabacín"),
    ("zucchini", "ingredient", "Courgette", "Calabacín"),
    ("Osada", "nature", "Bold", "Osada"),
    ("Lucha", "type", "Fighting", "Lucha"),
])
def test_names_resolve_in_both_languages_without_accents(query, kind, en, es):
    row = top(query, kinds=[kind])
    assert (row["kind"], row["en"], row["es"]) == (kind, en, es) and row["score"] >= 0.95


@pytest.mark.parametrize("query,showdown_id", [
    ("Mega Charizard X", "charizardmegax"), ("Charizard Mega X", "charizardmegax"), ("Mega-Charizard X", "charizardmegax"),
    ("raichu alola", "raichualola"), ("Alolan Raichu", "raichualola"), ("Raichu de Alola", "raichualola"),
    ("Shaymin Forma Cielo", "shayminsky"), ("Rotom Lavado", "rotomwash"), ("Primal Kyogre", "kyogreprimal"),
    ("Kyogre primigenio", "kyogreprimal"), ("Charizard Gigantamax", "charizardgmax"), ("mr mime galar", "mrmimegalar"),
    ("Farfetch’d", "farfetchd"), ("farfetchd de galar", "farfetchdgalar"),
])
def test_pokemon_forms_resolve_from_either_language(query, showdown_id):
    assert top(query, kinds=["pokemon"])["id"] == showdown_id


def test_forms_have_spanish_names():
    row = top("charizardmegax", kinds=["pokemon"])
    assert row["es"] == "Mega-Charizard X" and row["en"] == "Charizard-Mega-X" and row["dex"] == 6
    assert top("raichualola")["es"] == "Raichu de Alola" and top("charizardgmax")["es"] == "Charizard Gigamax"


def test_typos_and_prefixes_are_found_but_ranked_below_exact():
    assert top("garchmp")["en"] == "Garchomp" and top("garchmp")["how"] == "fuzzy"
    assert top("lanzallama")["en"] == "Flamethrower"
    assert top("terremto")["en"] == "Earthquake"
    exact = top("terremoto")
    assert exact["how"] == "exact" and exact["score"] > top("terremto")["score"]


def test_nothing_is_invented_for_noise():
    assert lookup("") == [] and lookup("   ") == []
    assert lookup("qzxwvk") == []


def test_kind_filter_and_limit():
    assert {r["kind"] for r in lookup("tomate", kinds=["ingredient"])} == {"ingredient"}
    assert {r["kind"] for r in lookup("tomate")} >= {"ingredient", "item"}
    assert len(lookup("pika", limit=2)) <= 2


def test_translate_picks_the_other_language_or_the_requested_one():
    assert translate("Lanzallamas")["text"] == "Flamethrower"
    assert translate("Flamethrower")["text"] == "Lanzallamas"
    assert translate("Flamethrower", to="en")["text"] == "Flamethrower"
    res = translate("Medalla Roca")
    assert res["ambiguous"] is True                       # Boulder Badge and Rock Badge are both "Medalla Roca"
    assert translate("qzxwvk")["found"] is False


def test_expand_swaps_known_names_for_their_other_language():
    res = expand("mejor set de Garchomp con Cinta Elección y Terremoto")
    assert [m["span"] for m in res["mentions"]] == ["Garchomp", "Cinta Elección", "Terremoto"]
    assert res["variants"][0]["text"] == "mejor set de Garchomp con Choice Band y Earthquake"
    assert res["terms"] == ["Choice Band", "Earthquake"] and res["changed"]
    back = expand("best Flamethrower moveset")
    assert back["variants"][0]["text"] == "best Lanzallamas moveset"


def test_expand_leaves_plain_text_alone_and_ignores_function_words():
    res = expand("receta de pasta con tomate frito y el pan de ayer")
    assert all(m["span"].lower() not in ("de", "con", "el", "y") for m in res["mentions"])
    none = expand("qué hora es en Madrid")
    assert none["variants"] == [] and none["changed"] is False


def test_expand_can_be_limited_to_kinds():
    res = expand("tomate", kinds=["ingredient"])
    assert res["variants"][0]["text"] == "Tomato" and res["swaps"][0]["kind"] == "ingredient"


def test_form_parser_candidates_match_the_original_rules():
    assert "Delphox-Mega" in [c.title() if c.islower() else c for c in candidates("Mega Delphox")] or "delphox-mega" in candidates("Mega Delphox")
    assert "shaymin-sky" in candidates("Shaymin Forma Cielo")
    assert "charizard-mega-x" in candidates("Charizard Mega X")


def test_lookup_is_fast_enough_for_interactive_search():
    get_index()
    start = time.perf_counter()
    for q in ("garchomp", "lanzallamas", "terremto", "mega charizard x", "tomate", "cinta eleccion"):
        lookup(q)
    assert (time.perf_counter() - start) / 6 < 0.25


def test_data_file_is_valid_and_has_unique_ids_per_kind():
    from hoard_link.aliases.index import load_data
    data = load_data()
    for kind, rows in data["kinds"].items():
        ids = [r[0] for r in rows]
        assert len(ids) == len(set(ids)), kind
        assert all(len(r) == 5 and r[1] and r[2] for r in rows), kind
    json.dumps(data)
