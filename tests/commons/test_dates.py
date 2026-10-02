"""hoard_link.dates and js/hoard-commons/dates.js agree on tests/vectors/dates.json (today is fixed: 2026-10-02)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from hoard_link import dates
from tests.commons.commerce_util import load_vectors, mismatches, run_js, run_python

CASES = load_vectors("dates")
TODAY = date(2026, 10, 2)


def test_python_vectors():
    assert not mismatches(CASES, run_python(dates, CASES))


def test_node_twin():
    assert not mismatches(CASES, run_js("dates.js", CASES))


def test_types_follow_the_input():
    assert dates.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert dates.add_months("2026-01-31", 1) == "2026-02-28"
    assert isinstance(dates.parse_date("15 oct 2026", today=TODAY), date)
    assert dates.parse_date("15 oct", today="2026-10-02") == date(2026, 10, 15)
    assert dates.days_between(datetime(2026, 10, 2, 23, 0), date(2026, 10, 4)) == 2
    assert dates.iso_day(datetime(2026, 10, 2, 8, 0)) == "2026-10-02"
    with pytest.raises(ValueError):
        dates.add_days("nope", 1)


def test_default_today_is_the_real_today():
    got = dates.parse_due("hoy")
    assert got == date.today()


def test_every_month_spelling_resolves_to_the_same_month():
    for lang, names in dates.MONTHS.items():
        assert len(names) == 12
        for i, aliases in enumerate(names, 1):
            for alias in aliases:
                assert dates.month_number(alias) == i, (lang, alias)
                assert dates.month_number(alias + ".") == i
    for alias in ("sep", "sept", "set", "setiembre", "septiembre", "Sept."):
        assert dates.parse_date(f"15 {alias} 2026") == date(2026, 9, 15)


def test_offsets_point_into_the_original_text():
    text = "Vencimiento: 15 de Octubre de 2026 (ÁÉÍ)"
    hit = dates.find_dates(text, today=TODAY)[0]
    assert text[hit.start:hit.end] == hit.text == "15 de Octubre de 2026"
    assert (hit.role, hit.date) == ("due", date(2026, 10, 15))


def test_roles_and_years_by_role():
    hits = dates.find_dates("Fecha factura: 01/10/2026\nVencimiento: 15/10/2026\nEntrega estimada: 20 de octubre\nHasta el 31 dic",
                            today=TODAY)
    assert [(h.role, h.date.isoformat()) for h in hits] == [
        ("issued", "2026-10-01"), ("due", "2026-10-15"), ("delivery", "2026-10-20"), ("expires", "2026-12-31")]
    assert [h.role for h in dates.find_dates("Salida 12 nov. Regreso 19 nov.", today=TODAY)] == ["departure", "return"]


def test_vague_due_phrases_are_opt_in():
    assert dates.parse_due("la semana que viene", today=TODAY) is None
    assert dates.parse_due("la semana que viene", today=TODAY, vague=True) == date(2026, 10, 5)
