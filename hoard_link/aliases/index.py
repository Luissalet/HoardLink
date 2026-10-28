"""The Spanish/English entity index: exact, form-parsed, prefix, token and fuzzy lookup without accents.

Data is ``hoard_link/_data/aliases.json`` (built by ``scripts/build_alias_data.py``): per kind a list of
``[id, en, es, aliases, extra]``. Standard library only.
"""

from __future__ import annotations

import bisect
import difflib
import json
import re
import threading
from dataclasses import dataclass
from importlib import resources
from typing import Any, Iterable, Optional

from ..text import fold
from .forms import candidates as form_candidates

#: Kinds in tie-break order (a Pokémon name beats a move with the same spelling).
KINDS = ("pokemon", "move", "ability", "item", "nature", "type", "badge", "place", "ingredient")
SINGULAR = {"pokemon": "Pokémon", "move": "movimiento", "ability": "habilidad", "item": "objeto", "nature": "naturaleza",
            "type": "tipo", "badge": "medalla", "place": "lugar", "ingredient": "ingrediente"}
#: Words that are never a mention on their own (function words of both languages).
STOP = frozenset("""a al algo ante con contra de del desde el ella en entre es esta este esto la las le les lo los me mi muy
no nos o para pero por que se si sin sobre su sus te todo tu un una uno y ya the an and are as at be by for from has have how in is it
its of on or that to was what when where which who with you your""".split())
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


def compact(text: Any) -> str:
    """Comparison key: lowercase, no accents, letters and digits only ("Farfetch’d" -> "farfetchd", "Ida y Vuelta" -> "idayvuelta")."""
    return re.sub(r"[^a-z0-9]+", "", fold(text, keep_length=False))


def _tokens(text: Any) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", fold(text, keep_length=False)) if t]


@dataclass(frozen=True)
class Entity:
    kind: str
    id: str
    en: str
    es: str
    aliases: tuple[str, ...]
    extra: Any

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "id": self.id, "en": self.en, "es": self.es}
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.kind == "pokemon" and self.extra:
            out["dex"] = self.extra
        elif self.kind in ("ingredient", "badge") and self.extra:
            out["category" if self.kind == "ingredient" else "type"] = self.extra
        return out


class AliasIndex:
    def __init__(self, data: dict[str, Any]):
        self.sources = dict(data.get("sources") or {})
        self.entities: list[Entity] = []
        self.names: dict[str, list[tuple[int, str]]] = {}      # compact name -> [(entity index, "en"|"es"|"alias"|"id")]
        self._tok: dict[str, frozenset[str]] = {}               # compact name -> its tokens
        for kind in KINDS:
            for row in (data.get("kinds") or {}).get(kind, []):
                eid, en, es, aliases, extra = row[:5]
                ent = Entity(kind, str(eid), str(en), str(es), tuple(aliases or ()), extra)
                idx = len(self.entities)
                self.entities.append(ent)
                self._add(ent.en, idx, "en")
                if compact(ent.es) != compact(ent.en):
                    self._add(ent.es, idx, "es")
                for a in ent.aliases:
                    self._add(a, idx, "alias")
                self._add(ent.id, idx, "id")
        self._keys = sorted(self.names)
        self._tri: dict[str, list[str]] = {}
        for k in self._keys:
            for g in {k[i:i + 3] for i in range(max(1, len(k) - 2))}:
                self._tri.setdefault(g, []).append(k)
        self._by_token: dict[str, set[str]] = {}
        for k, toks in self._tok.items():
            for t in toks:
                self._by_token.setdefault(t, set()).add(k)

    # -- building --------------------------------------------------------------------------------------------
    def _add(self, name: str, idx: int, lang: str) -> None:
        key = compact(name)
        if not key:
            return
        rows = self.names.setdefault(key, [])
        if all(r[0] != idx for r in rows):
            rows.append((idx, lang))
        if key not in self._tok:
            self._tok[key] = frozenset(_tokens(name))

    # -- lookup ----------------------------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for e in self.entities:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        return {"entities": len(self.entities), "names": len(self.names), "kinds": counts, "sources": self.sources}

    def _exact(self, key: str, score: float, how: str, hits: dict[int, dict[str, Any]], kinds: Optional[set[str]]) -> None:
        for idx, lang in self.names.get(key, ()):
            ent = self.entities[idx]
            if kinds and ent.kind not in kinds:
                continue
            s = score - (0.02 if lang == "alias" else 0.0) - (0.01 if lang == "id" else 0.0)
            self._offer(hits, idx, s, how, key, lang)

    def _offer(self, hits: dict[int, dict[str, Any]], idx: int, score: float, how: str, key: str, lang: str) -> None:
        cur = hits.get(idx)
        if cur is None or score > cur["score"]:
            hits[idx] = {"score": round(score, 4), "how": how, "key": key, "lang": lang}

    def lookup(self, query: str, kinds: Optional[Iterable[str]] = None, limit: int = 5, min_score: float = 0.78) -> list[dict[str, Any]]:
        q = compact(query)
        if not q:
            return []
        wanted = {k for k in (kinds or ()) if k in KINDS} or None
        hits: dict[int, dict[str, Any]] = {}
        self._exact(q, 1.0, "exact", hits, wanted)
        if not wanted or "pokemon" in wanted:
            for cand in form_candidates(query)[1:]:
                self._exact(compact(cand), 0.96, "form", hits, {"pokemon"})
        best = max((h["score"] for h in hits.values()), default=0.0)
        if best < 0.95:
            self._prefix(q, hits, wanted)
            self._token_subset(query, hits, wanted)
        if best < 0.9:
            self._fuzzy(q, hits, wanted, min_score)
        rows = []
        for idx, h in hits.items():
            if h["score"] < min_score:
                continue
            ent = self.entities[idx]
            row = ent.public()
            matched = {"en": ent.en, "es": ent.es}.get(h["lang"]) or (ent.id if h["lang"] == "id" else next(
                (a for a in ent.aliases if compact(a) == h["key"]), ent.en))
            row.update(score=h["score"], how=h["how"], matched=matched, matched_lang=h["lang"])
            rows.append(row)
        order = {k: i for i, k in enumerate(KINDS)}
        rows.sort(key=lambda r: (-r["score"], order[r["kind"]], r["en"]))
        return rows[:max(1, min(int(limit), 50))]

    def _prefix(self, q: str, hits: dict[int, dict[str, Any]], wanted: Optional[set[str]]) -> None:
        if len(q) < 3:
            return
        i = bisect.bisect_left(self._keys, q)
        n = 0
        while i < len(self._keys) and self._keys[i].startswith(q) and n < 60:
            k = self._keys[i]
            if k != q:
                for idx, lang in self.names[k]:
                    if wanted and self.entities[idx].kind not in wanted:
                        continue
                    self._offer(hits, idx, 0.8 + 0.12 * len(q) / len(k), "prefix", k, lang)
                n += 1
            i += 1

    def _token_subset(self, query: str, hits: dict[int, dict[str, Any]], wanted: Optional[set[str]]) -> None:
        toks = [t for t in _tokens(query) if len(t) >= 3]
        if len(toks) < 2 or len(toks) > 6:
            return
        pools = [self._by_token.get(t, set()) for t in toks]
        if not all(pools):
            return
        common = set.intersection(*pools)
        for k in list(common)[:60]:
            ratio = len(toks) / max(1, len(self._tok[k]))
            for idx, lang in self.names[k]:
                if wanted and self.entities[idx].kind not in wanted:
                    continue
                self._offer(hits, idx, 0.78 + 0.12 * min(1.0, ratio), "tokens", k, lang)

    def _fuzzy(self, q: str, hits: dict[int, dict[str, Any]], wanted: Optional[set[str]], min_score: float) -> None:
        if len(q) < 4:
            return
        grams = {q[i:i + 3] for i in range(len(q) - 2)}
        seen: dict[str, int] = {}
        for g in grams:
            for k in self._tri.get(g, ()):
                seen[k] = seen.get(k, 0) + 1
        need = max(1, int(len(grams) * 0.4))
        pool = sorted((k for k, c in seen.items() if c >= need), key=lambda k: -seen[k])[:400]
        for k in pool:
            if abs(len(k) - len(q)) > max(3, len(q) // 2):
                continue
            r = difflib.SequenceMatcher(None, q, k).ratio()
            if r >= min_score:
                for idx, lang in self.names[k]:
                    if wanted and self.entities[idx].kind not in wanted:
                        continue
                    self._offer(hits, idx, r * 0.95, "fuzzy", k, lang)

    # -- translation and search expansion ---------------------------------------------------------------------
    def translate(self, query: str, to: str = "", kinds: Optional[Iterable[str]] = None) -> dict[str, Any]:
        """The best match for ``query`` with its name in the other language (or in ``to``: 'en' or 'es')."""
        rows = self.lookup(query, kinds, limit=3)
        if not rows:
            return {"ok": True, "query": query, "found": False, "matches": []}
        top = rows[0]
        target = to if to in ("en", "es") else ("en" if top["matched_lang"] == "es" else "es")
        return {"ok": True, "query": query, "found": True, "to": target, "text": top[target], "match": top,
                "ambiguous": len(rows) > 1 and rows[1]["score"] >= top["score"] - 0.02, "matches": rows}

    def annotate(self, text: str, kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        """Entity mentions inside a sentence: the longest exact spans first, left to right, no overlaps."""
        words = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]
        taken = [False] * len(words)
        out: list[dict[str, Any]] = []
        for n in range(min(5, len(words)), 0, -1):
            for i in range(0, len(words) - n + 1):
                if any(taken[i:i + n]):
                    continue
                span = text[words[i][1]:words[i + n - 1][2]]
                key = compact(span)
                if n == 1 and (len(key) < 3 or key in STOP):
                    continue
                if key not in self.names:
                    continue
                for j in range(i, i + n):
                    taken[j] = True
                rows = [r for r in self.lookup(span, kinds, limit=3) if r["score"] >= 0.95]
                if rows:
                    out.append({"span": span, "start": words[i][1], "end": words[i + n - 1][2], "matches": rows,
                                "ambiguous": len({r["kind"] + r["id"] for r in rows}) > 1})
        out.sort(key=lambda m: m["start"])
        return out

    def expand(self, text: str, max_variants: int = 6, kinds: Optional[Iterable[str]] = None) -> dict[str, Any]:
        """Other-language spellings of the entities in ``text`` and the sentences with them swapped in.

        Meant for a search that came back empty: the same query in the other language of the names it contains."""
        text = str(text or "")[:500]
        mentions = self.annotate(text, kinds)
        swaps = []
        for m in mentions:
            top = m["matches"][0]
            other = top["en"] if top["matched_lang"] == "es" else top["es"] if top["matched_lang"] == "en" else (
                top["en"] if compact(m["span"]) != compact(top["en"]) else top["es"])
            if compact(other) == compact(m["span"]):
                continue
            swaps.append({"span": m["span"], "start": m["start"], "end": m["end"], "replacement": other, "kind": top["kind"],
                          "id": top["id"], "ambiguous": m["ambiguous"]})
        variants: list[dict[str, Any]] = []

        def build(chosen: list[dict[str, Any]]) -> str:
            out, pos = [], 0
            for s in chosen:
                out.append(text[pos:s["start"]]); out.append(s["replacement"]); pos = s["end"]
            out.append(text[pos:])
            return "".join(out)
        if swaps:
            variants.append({"text": build(swaps), "replaced": [s["span"] for s in swaps]})
            if len(swaps) > 1:
                for s in swaps:
                    variants.append({"text": build([s]), "replaced": [s["span"]]})
        seen: set[str] = set()
        variants = [v for v in variants if not (v["text"] in seen or seen.add(v["text"]))][:max(1, max_variants)]
        terms = list(dict.fromkeys(s["replacement"] for s in swaps))
        return {"ok": True, "text": text, "mentions": mentions, "swaps": swaps, "variants": variants, "terms": terms,
                "changed": bool(variants)}


_INDEX: Optional[AliasIndex] = None
_LOCK = threading.Lock()


def load_data() -> dict[str, Any]:
    with resources.files("hoard_link").joinpath("_data/aliases.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def get_index() -> AliasIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = AliasIndex(load_data())
        return _INDEX
