"""Pokémon names the way a person writes them, turned into names Showdown's dex knows: "Mega Delphox" -> Delphox-Mega,
"Shaymin Forma Cielo" -> Shaymin-Sky, "Raichu alola" / "Alolan Raichu" -> Raichu-Alola, "Charizard Mega X" -> Charizard-Mega-X,
"Primal Kyogre" -> Kyogre-Primal, "Rotom Lavado" -> Rotom-Wash.

Generalised from the form parser of Silver's Hoard (same author, same rules); it only produces candidate spellings, the
index says which of them exists."""

from __future__ import annotations

import re
import unicodedata

# words that only say "the form of": dropped
FILLER = {"forma", "form", "forme", "formas", "de", "del", "of", "the", "la", "el", "pokemon", "modo", "mode", "estilo", "style"}
# form words in Spanish, and adjectives, as Showdown's forme names
WORDS = {
    "cielo": "sky", "tierra": "land", "origen": "origin", "avatar": "incarnate", "totem": "therian",
    "alolan": "alola", "galarian": "galar", "hisuian": "hisui", "paldean": "paldea",
    "gigamax": "gmax", "gigantamax": "gmax", "primigenio": "primal", "primigenia": "primal",
    "calor": "heat", "lavado": "wash", "frio": "frost", "ventilador": "fan", "corte": "mow",
    "ataque": "attack", "defensa": "defense", "velocidad": "speed",
    "hembra": "f", "femenino": "f", "macho": "", "masculino": "",
}
# what Showdown writes after the species: regions, primal reversion, gigantamax
TAIL = {"alola", "galar", "hisui", "paldea", "primal", "gmax"}


def candidates(text: str) -> list[str]:
    """Spellings to try, the text as written first. Mega goes after the species (with X or Y after it), regions and other
    after-words go at the end; Spanish form words are translated."""
    raw = str(text or "").strip()[:80]
    out = [raw]
    plain = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", plain) if w and w not in FILLER]
    words = [WORDS.get(w, w) for w in words]
    words = [w for w in words if w]
    mega: list[str] = []
    if "mega" in words:
        i = words.index("mega")
        words.pop(i)
        if i < len(words) and words[i] in ("x", "y"):
            mega = ["mega", words.pop(i)]
        elif words and words[-1] in ("x", "y") and len(words) > 1:
            mega = ["mega", words.pop()]
        else:
            mega = ["mega"]
    tail = [w for w in words if w in TAIL]
    head = [w for w in words if w not in TAIL]
    if head:
        out += ["-".join(head + mega + tail), "-".join(head + tail + mega), " ".join(head + mega + tail)]
    return list(dict.fromkeys(x for x in out if x))
