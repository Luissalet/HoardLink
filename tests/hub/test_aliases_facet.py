"""The alias service through the hub: HTTP routes, agent tools and the tool catalogue."""

from __future__ import annotations

from hoard_link.hub import tools

from ._hub_fakes import http, make_hub, serve


def test_routes_and_tools(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    try:
        status, res = http(base + "/api/aliases/lookup?q=lanzallamas&limit=2")
        assert status == 200 and res["ok"] and res["matches"][0]["en"] == "Flamethrower" and res["detected_lang"] == "es"
        status, res = http(base + "/api/aliases/lookup?q=Flamethrower&kind=move,item")
        assert res["matches"][0]["es"] == "Lanzallamas" and res["detected_lang"] == "en"
        status, res = http(base + "/api/aliases/translate?q=Mega%20Charizard%20X&to=es")
        assert res["found"] and res["text"] == "Mega-Charizard X"
        status, res = http(base + "/api/aliases/expand?q=Terremoto%20de%20Garchomp")
        assert res["variants"][0]["text"] == "Earthquake de Garchomp"
        status, res = http(base + "/api/aliases/expand", {"text": "Choice Band Garchomp", "kinds": ["item"]})
        assert res["variants"][0]["text"] == "Cinta Elección Garchomp"
        status, res = http(base + "/api/aliases/stats")
        assert res["ok"] and res["kinds"]["pokemon"] > 1000
        status, res = http(base + "/api/aliases/nope")
        assert status == 404
        status, res = http(base + "/api/facets")
        assert "aliases" in [f["id"] for f in res["facets"]]

        out = tools.call(hub, "hub_alias_lookup", {"query": "Cinta Elección"})
        assert out["matches"][0]["en"] == "Choice Band"
        out = tools.call(hub, "hub_alias_lookup", {"query": "Choice Band", "to": "es"})
        assert out["text"] == "Cinta Elección"
        out = tools.call(hub, "hub_alias_expand", {"text": "set con Lanzallamas", "max_variants": 1})
        assert out["terms"] == ["Flamethrower"]
        assert tools.call(hub, "hub_alias_lookup", {"query": ""})["matches"] == []
    finally:
        server.shutdown()
        hub.close()


def test_tools_are_in_the_catalogue_with_hints():
    by = {t["name"]: t for t in tools.all_tools()}
    for name in ("hub_alias_lookup", "hub_alias_expand"):
        assert by[name]["annotations"]["readOnlyHint"] is True
        assert len(by[name]["description"].splitlines()[0]) <= 110
