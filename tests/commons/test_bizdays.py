"""hoard_link.bizdays: Easter, holidays per region, business-day arithmetic, carrier delivery days."""

from __future__ import annotations

from datetime import date

import pytest

from hoard_link import bizdays
from hoard_link.bizdays import Calendar, easter, holidays

D = date


@pytest.mark.parametrize("year,expected", [
    (1961, D(1961, 4, 2)), (2000, D(2000, 4, 23)), (2019, D(2019, 4, 21)), (2022, D(2022, 4, 17)), (2023, D(2023, 4, 9)), (2024, D(2024, 3, 31)),
    (2025, D(2025, 4, 20)), (2026, D(2026, 4, 5)), (2027, D(2027, 3, 28)), (2028, D(2028, 4, 16)), (2038, D(2038, 4, 25)), (2285, D(2285, 3, 22)),
])
def test_easter(year, expected):
    assert easter(year) == expected


def test_national_holidays_2026():
    h = holidays(2026)
    assert list(h) == [D(2026, 1, 1), D(2026, 1, 6), D(2026, 4, 3), D(2026, 5, 1), D(2026, 8, 15), D(2026, 10, 12), D(2026, 11, 1),
                       D(2026, 12, 7), D(2026, 12, 8), D(2026, 12, 25)]
    assert h[D(2026, 4, 3)] == "Viernes Santo"
    assert h[D(2026, 12, 7)] == "Día de la Constitución (trasladado)"      # 6 December 2026 is a Sunday
    assert D(2026, 11, 1) in h                                               # a Sunday that does not move by default


def test_sunday_rule_is_data_driven():
    assert D(2023, 1, 2) in holidays(2023) and D(2023, 1, 1) not in holidays(2023)
    assert D(2024, 12, 9) in holidays(2024)
    assert D(2023, 1, 1) in holidays(2023, move_sunday=[]) and D(2023, 1, 2) not in holidays(2023, move_sunday=[])
    assert D(2026, 11, 2) in holidays(2026, move_sunday=["11-01"])


def test_regions():
    md = holidays(2026, "ES-MD")
    assert md[D(2026, 4, 2)] == "Jueves Santo" and md[D(2026, 5, 2)] == "Fiesta de la Comunidad de Madrid"
    ct = holidays(2026, "ES-CT")
    assert D(2026, 4, 2) not in ct and ct[D(2026, 4, 6)] == "Lunes de Pascua" and ct[D(2026, 9, 11)].startswith("Diada")
    assert D(2026, 12, 26) in ct and D(2026, 12, 7) not in ct and D(2026, 12, 6) in ct   # Catalonia moves nothing by default
    assert D(2026, 3, 19) in holidays(2026, "ES-VC") and D(2026, 10, 9) in holidays(2026, "ES-VC")
    assert D(2026, 2, 28) in holidays(2026, "ES-AN")
    assert D(2026, 7, 25) in holidays(2026, "ES-GA") and D(2026, 5, 17) in holidays(2026, "ES-GA")
    pv = holidays(2026, "es-pv")
    assert D(2026, 4, 2) in pv and D(2026, 4, 6) in pv
    assert holidays(2026, "MD") == md
    assert all(set(holidays(2026)) <= set(holidays(2026, r)) | {D(2026, 12, 7)} for r in bizdays.REGIONS)


def test_unknown_region():
    with pytest.raises(ValueError):
        holidays(2026, "FR")
    with pytest.raises(ValueError):
        Calendar("ES-XX")


def test_business_day_checks():
    cal = Calendar()
    assert cal.is_business_day("2026-10-02") and not cal.is_business_day("2026-10-03") and not cal.is_business_day("2026-10-04")
    assert not cal.is_business_day("2026-10-12")                         # Fiesta Nacional (Monday)
    assert not cal.is_business_day(D(2026, 4, 3)) and cal.is_business_day(D(2026, 4, 2))
    assert not Calendar("ES-MD").is_business_day("2026-04-02")
    assert cal.holiday_name("2026-10-12") == "Fiesta Nacional de España" and cal.holiday_name("2026-10-13") is None


def test_add_business_days():
    cal = Calendar()
    assert cal.add_business_days("2026-10-02", 1) == "2026-10-05"       # text in, text out
    assert cal.add_business_days(D(2026, 10, 2), 1) == D(2026, 10, 5)
    assert cal.add_business_days("2026-10-02", 5) == "2026-10-09"
    assert cal.add_business_days("2026-10-02", 6) == "2026-10-13"       # 12 October is a holiday
    assert cal.add_business_days("2026-10-13", -1) == "2026-10-09"
    assert cal.add_business_days("2026-10-03", 0) == "2026-10-03"       # zero changes nothing
    assert cal.add_business_days("2026-10-03", 1) == "2026-10-05"
    assert Calendar("ES-VC").add_business_days("2026-10-02", 5) == "2026-10-13"   # 9 October (Comunitat Valenciana) and 12 October
    assert Calendar("ES", extra=["2026-10-06"]).add_business_days("2026-10-02", 2) == "2026-10-07"


def test_year_boundaries():
    cal = Calendar()
    assert cal.add_business_days("2026-12-24", 1) == "2026-12-28"       # 25 December is a Friday holiday
    assert cal.add_business_days("2026-12-31", 1) == "2027-01-04"       # 1 January Friday, 6 January later
    assert cal.next_business_day("2026-12-31") == "2027-01-04"


def test_next_and_previous():
    cal = Calendar()
    assert cal.next_business_day("2026-10-02") == "2026-10-05"
    assert cal.next_business_day("2026-10-05") == "2026-10-06" and cal.next_business_day("2026-10-05", include_self=True) == "2026-10-05"
    assert cal.next_business_day("2026-10-03", include_self=True) == "2026-10-05"
    assert cal.previous_business_day("2026-10-13") == "2026-10-09" and cal.previous_business_day("2026-10-13", include_self=True) == "2026-10-13"
    assert cal.previous_business_day("2026-10-04", include_self=True) == "2026-10-02"
    assert cal.next_business_day(D(2026, 10, 2)) == D(2026, 10, 5)


def test_business_days_between():
    cal = Calendar()
    assert cal.business_days_between("2026-10-02", "2026-10-13") == 6    # 2, 5-9 (12 is a holiday)
    assert cal.business_days_between("2026-10-13", "2026-10-02") == -6
    assert cal.business_days_between("2026-10-02", "2026-10-02") == 0
    assert cal.business_days_between("2026-10-03", "2026-10-05") == 0    # weekend only
    assert cal.business_days_between(D(2026, 10, 5), D(2026, 10, 12)) == 5
    assert cal.business_days_between("2026-01-01", "2027-01-01") == 253  # 261 weekdays minus 8 holidays that fall on one


def test_custom_weekend():
    cal = Calendar(weekend=(4, 5))                                       # Friday and Saturday
    assert not cal.is_business_day("2026-10-02") and cal.is_business_day("2026-10-04")
    with pytest.raises(ValueError):
        Calendar(weekend=range(7))


def test_carrier_delivery_days():
    cal = Calendar()
    assert bizdays.CARRIER_WEEKDAYS["amazon"] == (0, 1, 2, 3, 4, 5) and bizdays.CARRIER_WEEKDAYS["correos"] == (0, 1, 2, 3, 4)
    assert bizdays.delivery_weekdays("unknown") == bizdays.CARRIER_WEEKDAYS["default"]
    assert cal.is_delivery_day("2026-10-03", "amazon") and not cal.is_delivery_day("2026-10-03", "correos")
    assert not cal.is_delivery_day("2026-10-12", "amazon")               # a holiday
    assert cal.next_delivery_day("2026-10-02", "amazon") == "2026-10-03"
    assert cal.next_delivery_day("2026-10-02", "ups") == "2026-10-05"
    assert cal.next_delivery_day("2026-10-09", "amazon") == "2026-10-10" and cal.next_delivery_day("2026-10-10", "amazon") == "2026-10-13"
    assert cal.next_delivery_day("2026-10-03", "correos", include_self=True) == "2026-10-05"


def test_every_carrier_key_is_a_known_carrier():
    from hoard_link import tracking
    assert set(bizdays.CARRIER_WEEKDAYS) - {"default"} <= set(tracking.CARRIERS)
