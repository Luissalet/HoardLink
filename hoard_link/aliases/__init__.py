"""Spanish/English alias service: one canonical map of the family's named entities (Pokémon and their forms, moves,
abilities, items, natures, types, gym badges, places and kitchen ingredients) with accent-free fuzzy lookup.

    from hoard_link.aliases import lookup, translate, expand
    lookup("lanzallamas")            # -> [{"kind": "move", "id": "flamethrower", "en": "Flamethrower", "es": "Lanzallamas", ...}]
    translate("Mega Charizard X")    # -> {"text": "Mega-Charizard X", ...}
    expand("mejor set de Garchomp con Cinta Elección")   # other-language spellings for an empty search

The Hub serves it at ``/api/aliases/*`` and as the tools ``hub_alias_lookup`` / ``hub_alias_expand``."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .index import KINDS, SINGULAR, AliasIndex, Entity, compact, get_index

__all__ = ["KINDS", "SINGULAR", "AliasIndex", "Entity", "compact", "get_index", "lookup", "translate", "expand", "annotate", "stats"]


def lookup(query: str, kinds: Optional[Iterable[str]] = None, limit: int = 5) -> list[dict[str, Any]]:
    return get_index().lookup(query, kinds, limit)


def translate(query: str, to: str = "", kinds: Optional[Iterable[str]] = None) -> dict[str, Any]:
    return get_index().translate(query, to, kinds)


def annotate(text: str, kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    return get_index().annotate(text, kinds)


def expand(text: str, max_variants: int = 6, kinds: Optional[Iterable[str]] = None) -> dict[str, Any]:
    return get_index().expand(text, max_variants, kinds)


def stats() -> dict[str, Any]:
    return get_index().stats()
