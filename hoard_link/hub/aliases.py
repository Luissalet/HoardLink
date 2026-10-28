"""Facet ``aliases``: the family's Spanish/English entity map over HTTP and as agent tools.

    GET  /api/aliases/lookup?q=lanzallamas&kind=move,item&limit=5   -> {"ok", "query", "matches": [...]}
    GET  /api/aliases/translate?q=Mega Charizard X&to=es            -> {"ok", "found", "text", "match", "matches"}
    GET  /api/aliases/expand?q=mejor set de Garchomp con Cinta Elección  (or POST {"text"}) -> mentions, swaps, variants
    GET  /api/aliases/stats                                         -> entity counts per kind and data sources

Read-only and local: the data ships inside ``hoard_link`` (``_data/aliases.json``), nothing is fetched or written.
Used by Faustus to retry an empty search in the other language of the names the query contains."""

from __future__ import annotations

from typing import Any, Optional

from ..aliases import KINDS, SINGULAR, get_index
from .facets import Facet, Request

_KIND = {"type": "array", "items": {"type": "string", "enum": list(KINDS)},
         "description": "Only these kinds (" + ", ".join(f"{k}={SINGULAR[k]}" for k in KINDS) + "); omit for all."}


def _kinds(value: Any) -> Optional[list[str]]:
    if isinstance(value, str):
        value = [v for v in value.replace(" ", "").split(",") if v]
    return [str(v) for v in value] if isinstance(value, (list, tuple)) and value else None


class AliasesFacet(Facet):
    id = "aliases"

    def lookup(self, a: dict[str, Any]) -> dict[str, Any]:
        query = str(a.get("query") or a.get("q") or "")[:200]
        if not query.strip():
            return {"ok": True, "query": query, "matches": [], **get_index().stats()}
        limit = max(1, min(int(a.get("limit") or 5), 50))
        rows = get_index().lookup(query, _kinds(a.get("kinds") or a.get("kind")), limit)
        return {"ok": True, "query": query, "detected_lang": rows[0]["matched_lang"] if rows else None, "matches": rows}

    def translate(self, a: dict[str, Any]) -> dict[str, Any]:
        return get_index().translate(str(a.get("query") or a.get("q") or "")[:200], str(a.get("to") or ""),
                                     _kinds(a.get("kinds") or a.get("kind")))

    def expand(self, a: dict[str, Any]) -> dict[str, Any]:
        return get_index().expand(str(a.get("text") or a.get("q") or ""), max(1, min(int(a.get("max_variants") or 6), 20)),
                                  _kinds(a.get("kinds") or a.get("kind")))

    def get(self, req: Request) -> Optional[Any]:
        if not req.path.startswith("/api/aliases/"):
            return None
        args = {"q": req.q("q", ""), "kind": req.q("kind"), "limit": req.q_int("limit", 5), "to": req.q("to", ""),
                "max_variants": req.q_int("max_variants", 6)}
        op = req.path[len("/api/aliases/"):]
        if op == "lookup":
            return self.lookup(args)
        if op == "translate":
            return self.translate(args)
        if op == "expand":
            return self.expand(args)
        if op == "stats":
            return {"ok": True, **get_index().stats()}
        return None

    def post(self, req: Request) -> Optional[Any]:
        if req.path == "/api/aliases/expand":
            return self.expand(req.body)
        if req.path == "/api/aliases/lookup":
            return self.lookup(req.body)
        if req.path == "/api/aliases/translate":
            return self.translate(req.body)
        return None

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": "hub_alias_lookup",
                "description": "Look up a name in Spanish or English (Pokémon, moves, items, ingredients). Keywords: alias, traducir.\n"
                               "Accent-free and typo-tolerant. Returns kind, id, en, es, score, matched and detected_lang; "
                               "pass `to` to get only the translation.",
                "inputSchema": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "A name in either language, e.g. 'Lanzallamas' or 'Mega Charizard X'."},
                    "kinds": _KIND,
                    "to": {"type": "string", "enum": ["en", "es"], "description": "Return the best match's name in this language."},
                    "limit": {"type": "integer", "default": 5}},
                    "required": ["query"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "hub_alias_expand",
                "description": "Rewrite a query with its names in the other language. Keywords: búsqueda vacía, idioma, alias.\n"
                               "Finds the known names inside the text (Garchomp, Cinta Elección, Terremoto) and returns "
                               "`variants` (the sentence with them swapped es<->en) and `terms`. Use it to retry an empty search.",
                "inputSchema": {"type": "object", "properties": {
                    "text": {"type": "string", "description": "The search text."}, "kinds": _KIND,
                    "max_variants": {"type": "integer", "default": 6}},
                    "required": ["text"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
        ]

    def handlers(self) -> dict[str, Any]:
        def hub_alias_lookup(a: dict[str, Any]) -> dict[str, Any]:
            if a.get("to"):
                return self.translate(a)
            return self.lookup(a)
        return {"hub_alias_lookup": hub_alias_lookup, "hub_alias_expand": self.expand}


__all__ = ["AliasesFacet"]
