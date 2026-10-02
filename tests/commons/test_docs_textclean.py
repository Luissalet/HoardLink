"""hoard_link.docs.textclean: tidy extracted text, repeated headers, decoding, paging."""

from __future__ import annotations

import pytest

from hoard_link.docs import textclean as tc


def test_clean_text_invisible_characters_and_spaces():
    assert tc.clean_text("Hola­ mundo​\x00 x﻿") == "Hola mundo x"
    assert tc.clean_text("a b c\t d") == "a b c d"
    assert tc.clean_text("uno  \r\n  dos \r tres") == "uno\ndos\ntres"
    assert tc.clean_text("a\n\n\n\n\nb") == "a\n\nb"
    assert tc.clean_text(None) == "" and tc.clean_text("  \n ") == "" and tc.clean_text(12) == "12"


def test_clean_text_dehyphenates_only_before_lowercase():
    assert tc.clean_text("informa-\nción útil, líne-\na") == "información útil, línea"
    assert tc.clean_text("Juan-\nPablo") == "Juan-\nPablo"
    assert tc.clean_text("2026-\n10") == "2026-\n10"
    assert tc.clean_text("mun-\ndo", dehyphenate=False) == "mun-\ndo"


def test_clean_text_max_chars():
    assert tc.clean_text("x" * 50, max_chars=10) == "x" * 10
    assert tc.clean_text("short", max_chars=100) == "short"


def test_unstack_words_only_for_one_word_per_line_text():
    stacked = "\n\n".join(["menor", "uso", "directo", "de", "datos"] * 6)
    out = tc.clean_text(stacked)
    assert "\n" not in out and out.startswith("menor uso directo de datos menor")
    assert tc.unstack_words(stacked).count("\n") == 0
    prose = "\n".join(f"Esta es la línea número {i} de un texto normal." for i in range(30))
    assert tc.unstack_words(prose) == prose and tc.clean_text(prose) == prose
    few = "uno\ndos\ntres"
    assert tc.unstack_words(few) == few
    assert tc.clean_text(stacked, unstack=False).count("\n") > 20


def test_strip_repeated_lines_ignores_digits():
    pages = [f"Informe confidencial\nContenido único {c}\nPágina {i}" for i, c in enumerate("abcd", 1)]
    out = tc.strip_repeated_lines(pages)
    assert out == [f"Contenido único {c}" for c in "abcd"]
    assert tc.strip_repeated_lines(pages[:2]) == pages[:2]                      # fewer than 3 pages: untouched
    varied = ["a\nb", "c\nd", "e\nf"]
    assert tc.strip_repeated_lines(varied) == varied
    keep_long = ["x" * 200 + "\nuno", "x" * 200 + "\ndos", "x" * 200 + "\ntres"]
    assert all(("x" * 200) in p for p in tc.strip_repeated_lines(keep_long))      # long lines are body text
    half = ["cab\na", "cab\nb", "otra\nc", "otra2\nd"]
    assert tc.strip_repeated_lines(half, threshold=0.4)[0] == "a"
    assert tc.strip_repeated_lines(half, threshold=0.5)[0] == "cab\na"


def test_useful_chars():
    assert tc.useful_chars("a1 ,.\n¿ñ?") == 3 and tc.useful_chars("") == 0 and tc.useful_chars(None) == 0 and tc.useful_chars(" - ") == 0


def test_decode_text_boms_and_fallbacks():
    assert tc.decode_text("ya es str") == "ya es str"
    assert tc.decode_text("hola ñ".encode("utf-8-sig")) == "hola ñ"
    assert tc.decode_text("hola ñ".encode("utf-16")) == "hola ñ"
    assert tc.decode_text("hola ñ".encode("utf-16-be").join([b"\xfe\xff", b""])) == "hola ñ"
    assert tc.decode_text("hola ñ".encode("utf-32")) == "hola ñ"
    assert tc.decode_text("hola ñ €".encode("cp1252")) == "hola ñ €"
    assert tc.decode_text(b"caf\xe9 \x81") == "café \x81"                       # cp1252 has no 0x81: latin-1 never fails
    assert tc.decode_text("canción".encode("utf-8")) == "canción"
    assert tc.decode_text(b"") == "" and tc.decode_text(None) == ""


def test_decode_text_rejects_binary_on_request():
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    with pytest.raises(ValueError):
        tc.decode_text(png, reject_binary=True)
    assert isinstance(tc.decode_text(png), str)
    assert tc.decode_text("a\x00b".encode("utf-16"), reject_binary=True) == "a\x00b"      # a UTF-16 BOM is not binary


def test_split_pages():
    assert tc.split_pages("") == [] and tc.split_pages("corto") == ["corto"]
    para = "Una línea de relleno que ocupa sitio.\n" * 10
    text = "\n\n".join(f"Párrafo {i}\n" + para for i in range(30))
    pages = tc.split_pages(text, size=1000)
    assert len(pages) > 5 and all(p.strip() for p in pages)
    assert sum(p.count("Párrafo") for p in pages) == 30                       # nothing lost
    assert all(len(p) <= 2400 for p in pages)
    giant = tc.split_pages("línea de texto sin párrafos\n" * 400, size=500)
    assert len(giant) > 5 and all(len(p) <= 1300 for p in giant)
