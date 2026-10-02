"""hoard_link.docs.pageranges: the ways people write page ranges, and the errors they get."""

from __future__ import annotations

import pytest

from hoard_link.docs.pageranges import PageRangeError, describe, parse_groups, parse_ranges


@pytest.mark.parametrize("text,expected", [
    ("1-3,5,8-", [1, 2, 3, 5, 8, 9, 10]),
    ("last", [10]), ("-1", [10]), ("-2", [9]), ("3-last", [3, 4, 5, 6, 7, 8, 9, 10]), ("-3--1", [8, 9, 10]),
    ("odd", [1, 3, 5, 7, 9]), ("impares", [1, 3, 5, 7, 9]), ("even", [2, 4, 6, 8, 10]), ("pares", [2, 4, 6, 8, 10]),
    ("all", list(range(1, 11))), ("todas", list(range(1, 11))), ("TODAS", list(range(1, 11))),
    ("última", [10]), ("ÚLTIMA", [10]), ("final", [10]), ("2-", list(range(2, 11))),
    ("pág. 3", [3]), ("p. 2-4", [2, 3, 4]), ("pagina 4", [4]), ("1 a 3", [1, 2, 3]), ("3 hasta last", [3, 4, 5, 6, 7, 8, 9, 10]),
    ("2 y 5", [2, 5]), ("1–3", [1, 2, 3]), ("1—3", [1, 2, 3]), ("1..3, 7", [1, 2, 3, 7]), ("1;2;3", [1, 2, 3]), ("1 2 3", [1, 2, 3]),
    ("5-5", [5]), (" 4 ", [4]), ("1, 3 - 4", [1, 3, 4]),
])
def test_accepted_forms(text, expected):
    assert parse_ranges(text, 10) == expected


def test_order_unique_and_groups():
    assert parse_ranges("5,1-3,2", 10) == [5, 1, 2, 3]
    assert parse_ranges("5,1-3,2", 10, unique=False) == [5, 1, 2, 3, 2]
    assert parse_ranges("1,1,1", 10, unique=False) == [1, 1, 1]
    assert parse_groups("1-3,5,8-", 10) == [[1, 2, 3], [5], [8, 9, 10]]
    assert parse_groups("pares, 1", 6) == [[2, 4, 6], [1]]


def test_default_all_and_reversed():
    assert parse_ranges("", 4, default_all=True) == [1, 2, 3, 4] and parse_ranges("  ", 4, default_all=True) == [1, 2, 3, 4]
    assert parse_ranges(None, 3, default_all=True) == [1, 2, 3]
    assert parse_ranges("5-3", 10, allow_reversed=True) == [3, 4, 5]
    assert parse_ranges("last-8", 10, allow_reversed=True) == list(range(8, 11))


@pytest.mark.parametrize("text,total,es,en", [
    ("", 10, "Indica las páginas.", "Say which pages."),
    ("0", 10, "La página 0 no existe", "Page 0 does not exist"),
    ("11", 10, "La página 11 no existe: el documento tiene 10 página(s).", "Page 11 does not exist: the document has 10 page(s)."),
    ("-11", 10, "queda antes de la primera página", "is before the first page"),
    ("5-3", 10, "está al revés", "is backwards"),
    ("abc", 10, "No entiendo «abc» como página o rango.", "“abc” is not a page or a range."),
    ("1-99", 10, "La página 99 no existe", "Page 99 does not exist"),
    ("1", 0, "El documento no tiene páginas.", "The document has no pages."),
    ("1,x", 10, "«x»", "“x”"),
    ("-", 10, "No entiendo", "is not a page"),
])
def test_errors_are_clear_in_both_languages(text, total, es, en):
    with pytest.raises(PageRangeError) as e_es:
        parse_ranges(text, total)
    assert es in str(e_es.value) and es in e_es.value.message_es and en in e_es.value.message_en
    assert "Ejemplos" in e_es.value.es and "Examples" in e_es.value.en
    with pytest.raises(PageRangeError) as e_en:
        parse_ranges(text, total, lang="en")
    assert en in str(e_en.value) and e_en.value.lang == "en"
    assert isinstance(e_en.value, ValueError)


def test_describe():
    assert describe([1, 2, 3, 5, 8, 9]) == "1-3,5,8-9" and describe([3, 1, 2, 2]) == "1-3" and describe([]) == "" and describe([7]) == "7"
    assert parse_ranges(describe([2, 3, 4, 9]), 10) == [2, 3, 4, 9]
