"""hoard_link.docs.chunking: offsets, pages, sections, markdown."""

from __future__ import annotations

import random

import pytest

from hoard_link.docs import chunking as ck

PARA = "Primera frase del texto. Segunda frase, con una coma. ¿Tercera frase? ¡Cuarta! "


def test_short_slides_keep_physical_numbers_and_separate_boundaries():
    units = [ck.Unit('slide', 2, 'Cost', 'Mango 12.50'), ck.Unit('slide', 3, 'Next', 'Other 8.00')]
    chunks = ck.chunk_units(units)
    assert [(c.page, c.section, c.text) for c in chunks] == [(2, 'Cost', 'Mango 12.50'), (3, 'Next', 'Other 8.00')]


def _check_invariants(text, chunks, size, min_tail=200):
    for i, c in enumerate(chunks):
        assert c.ordinal == i
        assert text[c.char_start:c.char_end] == c.text, "offsets must point at the chunk text"
        assert c.text == c.text.strip() and c.text
        assert len(c.text) <= size + min_tail + 50
    covered = [False] * len(text)
    for c in chunks:
        for k in range(c.char_start, c.char_end):
            covered[k] = True
    assert all(covered[i] or text[i].isspace() for i in range(len(text))), "every non-space character is in some chunk"
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts)


def test_empty_and_short_text():
    assert ck.chunk_text("") == [] and ck.chunk_text(None) == [] and ck.chunk_text("  \n ") == []
    one = ck.chunk_text("Un texto corto.")
    assert len(one) == 1 and one[0].text == "Un texto corto." and one[0].line == 1 and one[0].page is None
    assert (one[0].char_start, one[0].char_end) == (0, 15)


@pytest.mark.parametrize("size,overlap", [(300, 50), (900, 150), (120, 20), (500, 0)])
def test_chunk_text_invariants(size, overlap):
    rng = random.Random(size)
    words = ["casa", "árbol", "x", "información", "a,", "fin.", "¿qué?", "uno\n", "dos\n\n"]
    text = " ".join(rng.choice(words) for _ in range(2500))
    chunks = ck.chunk_text(text, size=size, overlap=overlap)
    assert len(chunks) > 3
    _check_invariants(text, chunks, size)


def test_cuts_prefer_paragraph_then_sentence_then_word():
    text = ("a" * 100 + ". ") * 4 + "\n\n" + ("b" * 100 + ". ") * 4
    chunks = ck.chunk_text(text, size=500, overlap=0, min_tail=20)
    assert chunks[0].text.endswith("a" * 100 + ".")        # the paragraph break wins over later sentence breaks
    assert chunks[1].text.startswith("b")
    sentences = ck.chunk_text("Frase uno aquí. " * 60, size=200, overlap=0)
    assert all(c.text.endswith(".") for c in sentences)
    words = ck.chunk_text("palabra " * 100, size=100, overlap=0)
    assert all(not c.text.endswith("pal") and c.text.endswith("palabra") for c in words)


def test_overlap_and_tail():
    text = "palabra " * 200
    chunks = ck.chunk_text(text, size=300, overlap=60)
    for a, b in zip(chunks, chunks[1:]):
        assert b.char_start < a.char_end                    # chunks overlap
        assert a.char_end - b.char_start <= 61
    tail = ck.chunk_text("x " * 440 + "fin", size=400, overlap=0, min_tail=100)       # 883 chars: last piece must not be tiny
    assert min(len(c.text) for c in tail) >= 100


def test_overlap_is_limited_so_it_always_advances():
    chunks = ck.chunk_text(PARA * 40, size=100, overlap=500)
    assert 2 < len(chunks) < 200


def test_hard_cut_when_there_is_no_break():
    chunks = ck.chunk_text("x" * 2000, size=300, overlap=50)
    assert chunks[0].char_end - chunks[0].char_start == 300
    _check_invariants("x" * 2000, chunks, 300)


def test_lines_follow_the_text():
    text = "\n".join(f"Línea {i} con contenido de relleno para ocupar espacio." for i in range(80))
    for c in ck.chunk_text(text, size=300, overlap=40):
        assert c.line == 1 + text.count("\n", 0, c.char_start)


def test_pages_are_never_merged_and_keep_their_number():
    units = [
        {"kind": "page", "number": 1, "title": "", "text": "a b " * 100},
        {"kind": "page", "number": 2, "title": "", "text": "corta"},
        {"kind": "page", "number": 3, "title": "", "text": "Frase de la página tres. " * 40},
    ]
    chunks = ck.chunk_units(units, size=300, overlap=40)
    assert [c.page for c in chunks if c.unit_index == 1] == [2]
    assert {c.page for c in chunks} == {1, 2, 3}
    assert [c.unit_index for c in chunks] == sorted(c.unit_index for c in chunks)
    for c in chunks:
        assert c.text in units[c.unit_index]["text"]
    assert ck.merge_small_units([ck.Unit("page", 1, "", "x"), ck.Unit("page", 2, "", "y")]) == [ck.Unit("page", 1, "", "x"), ck.Unit("page", 2, "", "y")]


def test_short_sections_are_merged_into_their_neighbour():
    units = [
        ck.Unit("section", 1, "Intro", "breve", 1),
        ck.Unit("section", 2, "Cuerpo", "Contenido del cuerpo, con frases. " * 30, 5),
        ck.Unit("section", 3, "Fin", "otro final corto", 50),
    ]
    merged = ck.merge_small_units(units, 200)
    assert len(merged) == 1 and merged[0].title == "Cuerpo" and merged[0].line_start == 5
    assert merged[0].text.startswith("Intro\nbreve") and merged[0].text.endswith("Fin\notro final corto")
    chunks = ck.chunk_units(units, size=400, overlap=60)
    assert {c.section for c in chunks} == {"Cuerpo"}
    only_short = ck.merge_small_units([ck.Unit("section", 1, "A", "uno"), ck.Unit("section", 2, "B", "dos más largo")], 200)
    assert len(only_short) == 1 and only_short[0].title == "B"


def test_tiny_chunks_are_glued_within_a_unit():
    text = ("Una frase normal que ocupa bastante espacio en el texto. " * 15).strip() + " fin"
    chunks = ck.chunk_text(text, size=300, overlap=0, min_tail=10)
    assert all(len(c.text) >= 120 for c in chunks)


def test_units_accept_dicts_objects_and_missing_keys():
    class U:
        kind, number, title, text, line_start = "section", 1, "T", "hola " * 100, 10

    assert ck.chunk_units([U()], size=200)[0].line == 10
    assert ck.chunk_units([{"text": "hola"}])[0].text == "hola"
    assert ck.chunk_units([]) == []


def test_chunk_version_includes_physical_slide_boundaries():
    assert ck.CHUNK_VERSION == 4


# ---- markdown -------------------------------------------------------------------------------------

MD = """---
title: Guía
author: "Ana"
---
Intro text

# Guide
text one

## Install
Run it.
```sh
# not a heading
```

### Windows
Use the exe.

## Usage
Call it.
"""


def test_markdown_breadcrumbs_lines_and_frontmatter():
    chunks = ck.chunk_markdown(MD)
    assert [(c.section, c.text.splitlines()[0]) for c in chunks] == [
        ("Guía", "Intro text"),
        ("Guide", "text one"),
        ("Guide § Install", "Run it."),
        ("Guide § Install § Windows", "Use the exe."),
        ("Guide § Usage", "Call it."),
    ]
    lines = MD.splitlines()
    for c in chunks:
        assert lines[c.line - 1].startswith(c.text.splitlines()[0]), (c.line, c.text)
    assert "# not a heading" in chunks[2].text                       # fenced code is not a heading
    assert not any("author" in c.text for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(5)) and all(c.page is None for c in chunks)


def test_markdown_options():
    flat = ck.chunk_markdown(MD, breadcrumbs=False)
    assert [c.section for c in flat] == ["Guía", "Guide", "Install", "Windows", "Usage"]
    kept = ck.chunk_markdown(MD, frontmatter=False)
    assert kept[0].text.startswith("---") and kept[0].line == 1
    assert ck.markdown_title(MD) == "Guía" and ck.markdown_title("# Uno\n\ntexto") == "Uno" and ck.markdown_title("sin título", "dflt") == "dflt"
    assert ck.parse_frontmatter(MD)[0] == {"title": "Guía", "author": "Ana"}
    assert ck.parse_frontmatter("no front")[1] == 0


def test_markdown_skipped_levels_and_default_title():
    chunks = ck.chunk_markdown("### Deep\nbody\n\n# Top\nmore", default_title="Doc")
    assert [c.section for c in chunks] == ["Deep", "Top"]
    plain = ck.chunk_markdown("just text, no headings", default_title="Doc")
    assert plain[0].section == "Doc" and plain[0].text == "just text, no headings"
    assert ck.chunk_markdown("") == [] and ck.chunk_markdown("   \n") == []


def test_markdown_packs_paragraphs_and_cuts_big_ones_at_sentences():
    paras = "\n\n".join(f"Párrafo {i}. " + "Texto de relleno aquí. " * 5 for i in range(20))
    chunks = ck.chunk_markdown("# T\n" + paras, size=400)
    assert all(len(c.text) <= 400 for c in chunks) and len(chunks) > 4
    assert all(c.text.startswith("Párrafo") for c in chunks)          # packed on paragraph boundaries
    big = ck.chunk_markdown("# T\n" + "Una frase larga y completa. " * 100, size=300)
    assert all(len(c.text) <= 300 and c.text.endswith(".") for c in big)
    assert all(c.section == "T" for c in big)
    nospace = ck.chunk_markdown("x" * 1000, size=300)
    assert [len(c.text) for c in nospace] == [300, 300, 300, 100]
