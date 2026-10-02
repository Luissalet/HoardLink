"""hoard_link.docs.textsearch: FTS queries that cannot break, BM25 re-scoring, highlights."""

from __future__ import annotations

import random
import sqlite3

import pytest

from hoard_link.docs import textsearch as ts

HOSTILE = [
    "hojas AND árboles", 'NEAR(a b) "x" -y *z :c ^d (e', "AND", "OR", "NOT", "NEAR", '"', '""', '"""', "a-b c:d", "-", "--", "*", "^", ":",
    "(", ")", "()", "{}", 'x" OR "1"="1', "title:foo", "foo*bar", "col : val", "AND AND AND", "- -a", "NOT a", "a NOT b", "+x", "x+y",
    "C++ y C#", "ñandú\x00null", "tab\tand\nnewline", "'single'", "\\", "%", "_", "😀", "—", "¿qué?", "uńaccent",
]


@pytest.fixture()
def fts():
    conn = sqlite3.connect(":memory:")
    assert ts.ensure_fts(conn, "docs", ["title", "body"])
    rows = [
        ("Las hojas del árbol", "AND OR NEAR cosas - raras"),
        ("Cosas del NOT", 'comillas " y dos "" y -menos *estrella ^acento :dos (paréntesis'),
        ("Ciudades grandes", "Madrid, Valencia y Sevilla son ciudades"),
        ("Árboles", "los árboles pierden las hojas en otoño"),
        ("title:foo", "foo bar baz"),
    ]
    conn.executemany("INSERT INTO docs(title, body) VALUES (?, ?)", rows)
    return conn


def _count(conn, expr):
    return conn.execute("SELECT count(*) FROM docs WHERE docs MATCH ?", (expr,)).fetchone()[0]


@pytest.mark.parametrize("q", HOSTILE)
@pytest.mark.parametrize("mode", ["and", "or", "prefix"])
def test_hostile_queries_are_valid_fts5(fts, q, mode):
    expr = ts.fts_query(q, mode=mode)
    if expr:
        _count(fts, expr)               # must not raise sqlite3.OperationalError
    for e in ts.fts_ladder(q):
        _count(fts, e)


def test_random_garbage_never_breaks_fts5(fts):
    rng = random.Random(7)
    alphabet = list('abcAND OR NEAR NOT "\'*-:^()+{}\\ñáé\t\n_') + ["AND", "OR", "NEAR", "NOT", " "]
    for _ in range(400):
        q = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        for e in ts.fts_ladder(q):
            _count(fts, e)


def test_operators_are_plain_words(fts):
    # "AND" inside the text is searched as a word, not as the operator
    assert _count(fts, ts.fts_query("AND", mode="and")) == 1
    assert ts.fts_query("hojas AND otoño") == '"hojas" AND "otoño"'      # stopword "and" dropped
    assert ts.fts_query("near") == '"near"'
    assert ts.fts_query('"quoted" -minus') == '"quoted" AND "minus"'
    assert ts.fts_query('he said "hi"') == '"said" AND "hi"'



def test_modes_and_limits():
    assert ts.fts_query("hojas verdes", mode="and") == '"hojas" AND "verdes"'
    assert ts.fts_query("hojas verdes", mode="prefix") == '"hoja"* AND "verd"*'
    assert ts.fts_query("hojas verdes", mode="or") == ts.fts_query("hojas verdes", mode="prefix").replace(" AND ", " OR ")
    assert ts.fts_query("uno dos tres cuatro", max_terms=2) == '"uno" AND "dos"'
    assert ts.fts_query("") == "" and ts.fts_query(None) == "" and ts.fts_query("   ") == ""
    assert ts.fts_query("hola HOLA hola") == '"hola"'
    with pytest.raises(ValueError):
        ts.fts_query("x", mode="nope")


def test_stopwords_are_dropped_but_not_all():
    assert ts.fts_query("¿Dónde están las hojas?") == '"están" AND "hojas"'
    assert ts.fts_query("the of and") == '"the" AND "of" AND "and"'         # only stopwords: keep them
    assert ts.is_stopword("MÁS") and ts.is_stopword("mas") and ts.is_stopword("Donde")
    assert not ts.is_stopword("casa")


def test_ladder_falls_back_from_exact_to_prefix_to_or(fts):
    exact, prefix, wide = ts.fts_ladder("hoja otoño ciudad")
    assert _count(fts, exact) == 0           # "ciudad" is not a whole word in the data
    assert exact == '"hoja" AND "otoño" AND "ciudad"' and prefix == '"hoja"* AND "otoño"* AND "ciudad"*'
    assert _count(fts, prefix) == 0           # no row has all three
    assert _count(fts, wide) >= 2
    ladder = ts.fts_ladder("hojas")
    assert ladder == ['"hojas"', '"hoja"*']  # one term: OR equals the prefix rung and is dropped
    assert _count(fts, ladder[0]) == 2
    assert ts.fts_ladder("") == [] and ts.fts_ladder("x") == ['"x"']


def test_prefix_matches_through_the_stem(fts):
    assert _count(fts, ts.fts_query("árboles", mode="prefix")) == 2          # árbol, Árboles
    assert _count(fts, ts.fts_query("ciudad", mode="prefix")) == 1


def test_fts_available_and_ensure_fts():
    conn = sqlite3.connect(":memory:")
    assert ts.fts_available(conn) is True
    assert ts.ensure_fts(conn, "t", ["a", "b"]) is True
    assert ts.ensure_fts(conn, "t", ["a", "b"]) is True                      # idempotent
    conn.execute("INSERT INTO t(a, b) VALUES ('Cámara', 'x')")
    assert conn.execute("SELECT count(*) FROM t WHERE t MATCH 'camara'").fetchone()[0] == 1     # remove_diacritics 2
    for bad in ("t; DROP TABLE x", "1a", "a b", ""):
        with pytest.raises(ValueError):
            ts.ensure_fts(conn, bad, ["a"])
    with pytest.raises(ValueError):
        ts.ensure_fts(conn, "u", ["a; b"])
    with pytest.raises(ValueError):
        ts.ensure_fts(conn, "u", [])


def test_ensure_fts_external_content_and_tokenizer():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
    assert ts.ensure_fts(conn, "notes_fts", ["body"], content="notes", tokenize="porter unicode61")
    conn.execute("INSERT INTO notes(body) VALUES ('running dogs')")
    conn.execute("INSERT INTO notes_fts(rowid, body) SELECT id, body FROM notes")
    assert conn.execute("SELECT count(*) FROM notes_fts WHERE notes_fts MATCH 'run'").fetchone()[0] == 1


class _NoFts:
    def execute(self, *a, **k):
        raise sqlite3.OperationalError("no such module: fts5")


def test_without_fts5_nothing_is_created():
    assert ts.fts_available(_NoFts()) is False
    assert ts.ensure_fts(_NoFts(), "t", ["a"]) is False


# ---- bm25 ---------------------------------------------------------------------------------------

def test_bm25_common_term_still_ranks():
    # "hola" is on every row: FTS5's own bm25() would give ~0 to all of them; here the title weight still orders them
    rows = [{"title": "otro", "text": "hola mundo hola"}, {"title": "hola", "text": "mundo"}, {"title": "x", "text": "hola"}]
    ranked = ts.bm25_rescore(rows, ["hola"], weights={"title": 3, "text": 1})
    scores = [s for _, s in ranked]
    assert all(s > 0 for s in scores)
    assert ranked[0][0]["title"] == "hola"
    assert scores == sorted(scores, reverse=True)
    assert len({round(s, 6) for s in scores}) == 3


def test_bm25_rarer_terms_weigh_more_and_prefix_terms_work():
    rows = [{"text": "gato perro casa"}, {"text": "gato gato gato"}, {"text": "casa casa"}, {"text": "perro"}]
    top = ts.bm25_rescore(rows, ["gato", "perro"])
    assert top[0][0]["text"] == "gato perro casa"
    pre = ts.bm25_rescore(rows, ["gat*"])
    assert [r["text"] for r, s in pre if s > 0] == ["gato gato gato", "gato perro casa"]
    assert ts.bm25_rescore(rows, [("per", True)])[0][0]["text"] in ("perro", "gato perro casa")
    assert ts.bm25_rescore(rows, []) == [(r, 0.0) for r in rows]
    assert ts.bm25_rescore([], ["x"]) == []


def test_bm25_accepts_strings_and_custom_idf():
    ranked = ts.bm25_rescore(["uno dos", "dos dos dos", "tres"], ["dos"])
    assert ranked[0][0] == "dos dos dos"
    boosted = ts.bm25_rescore(["uno dos", "tres"], ["dos"], idfs={"dos": 5.0})
    assert boosted[0][1] > ts.bm25_rescore(["uno dos", "tres"], ["dos"])[0][1]


def test_bm25_folds_accents_and_case():
    ranked = ts.bm25_rescore([{"text": "ÁRBOL grande"}, {"text": "mesa"}], ts.query_terms("árboles"))
    assert ranked[0][0]["text"] == "ÁRBOL grande" and ranked[0][1] > 0


# ---- highlight -----------------------------------------------------------------------------------

def test_highlight_marks_the_original_text_not_the_folded_one():
    text = "Las hojas del Árbol caen en otoño. Más HOJAS caen después."
    out = ts.highlight(text, ts.query_terms("árboles hojas"), window=200)
    assert out == "Las <mark>hojas</mark> del <mark>Árbol</mark> caen en otoño. Más <mark>HOJAS</mark> caen después."


def test_highlight_offsets_survive_special_characters():
    text = "straße ß İstanbul ﬁne ñandú then target word here"
    out = ts.highlight(text, ["target"], window=300)
    assert "<mark>target</mark>" in out and out.startswith("straße ß İstanbul")


def test_highlight_window_ellipsis_and_escape():
    text = ("palabra " * 40) + "needle <b>&</b> " + ("relleno " * 40)
    out = ts.highlight(text, ["needle"], window=60)
    assert out.startswith("…") and out.endswith("…") and "<mark>needle</mark>" in out
    assert "&lt;b&gt;&amp;&lt;/b&gt;" in out and "<b>" not in out
    raw = ts.highlight("a <i>needle</i>", ["needle"], window=60, escape=False)
    assert raw == "a <i><mark>needle</mark></i>"
    assert ts.highlight("hola mundo", ["zzz"], window=5) == "hola …"
    assert ts.highlight("", ["a"]) == "" and ts.highlight(None, ["a"]) == ""
    assert ts.highlight("hola mundo", ["mundo"], tag=None) == "hola mundo"
    assert ts.highlight("hola mundo", ["mundo"], tag="b") == "hola <b>mundo</b>"
    with pytest.raises(ValueError):
        ts.highlight("x", ["x"], tag="a href=x")


def test_highlight_only_word_starts():
    assert ts.highlight("scatter cat", ["cat"], window=50) == "scatter <mark>cat</mark>"
    assert ts.highlight("category cat", ["cat"], window=50) == "<mark>category</mark> <mark>cat</mark>"


def test_snippet_is_plain_and_centered():
    text = ("antes " * 30) + "AQUÍ está la clave " + ("después " * 30)
    s = ts.snippet(text, ["aqui"], window=40)
    assert "AQUÍ" in s and "<" not in s and s.startswith("…") and s.endswith("…")
    assert ts.snippet("a  b\n c", []) == "a b c"


def test_tokens_and_stem():
    assert ts.tokens("Hola, Mundo! ñandú 123 a_b") == ["hola", "mundo", "nandu", "123", "a", "b"]
    assert ts.tokens("el gato y la casa", stop=ts.STOPWORDS_FOLDED) == ["gato", "casa"]
    assert [ts.stem(w) for w in ["hojas", "árboles", "ciudades", "luz", "pez", "casa"]] == ["hoja", "árbol", "ciudad", "luz", "pez", "casa"]
    assert len(ts.fold("Straße")) == len("Straße")
