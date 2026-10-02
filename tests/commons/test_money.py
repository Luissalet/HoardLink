"""hoard_link.money and js/hoard-commons/money.js agree on tests/vectors/money.json."""

from __future__ import annotations

from decimal import Decimal

import pytest

from hoard_link import money
from tests.commons.commerce_util import load_vectors, mismatches, run_js, run_python

CASES = load_vectors("money")


def test_python_vectors():
    assert not mismatches(CASES, run_python(money, CASES))


def test_node_twin():
    assert not mismatches(CASES, run_js("money.js", CASES))


# the amounts every app used to read differently (cents, Spanish default)
AUDIT = {
    "1.234": 123400, "2,099": 209900, "1.234.567": 123456700, "-12,50": -1250, "12,5": 1250, "100000": 10000000,
    "€1,234.56": 123456, "$1,299.00": 129900, "1.599,00 SEK": 159900, "1 299 €": 129900, "1.234,50 €": 123450,
    "1.500 €": 150000, "1 234 €": 123400,
}


@pytest.mark.parametrize("text,cents", AUDIT.items())
def test_audit_inputs(text, cents):
    assert money.parse_cents(text) == cents


def test_vectors_cover_every_audit_input():
    seen = {c["args"][0] for c in CASES if c["fn"] == "parse_cents" and not c.get("opts")}
    assert set(AUDIT) <= seen


def test_ambiguous_three_digits_follow_the_hint():
    assert money.parse_amount("1.234") == Decimal("1234")
    assert money.parse_amount("1.234", lang="en") == Decimal("1.234")
    assert money.parse_amount("1.234", currency_hint="USD") == Decimal("1.234")
    assert money.parse_amount("$1.234") == Decimal("1.234")
    assert money.parse_amount("€1.234") == Decimal("1234")
    assert money.parse_amount("2,099", lang="en") == Decimal("2099")
    assert money.parse_amount("1.234", decimal=".") == Decimal("1.234")
    assert money.parse_amount("1.234", decimal=",") == Decimal("1234")


def test_garbage_and_non_numbers():
    for bad in (None, True, "", "abc", "1.2.3,4", "12,34,56", "--5", "1.23.4", float("nan")):
        assert money.parse_amount(bad) is None


def test_decimal_and_numbers_pass_through():
    assert money.parse_amount(Decimal("12.5")) == Decimal("12.5")
    assert money.parse_amount(12) == Decimal(12)
    assert money.parse_amount(12.5) == Decimal("12.5")
    assert money.to_cents(12.005) == 1201
    assert money.to_cents(Decimal("-0.005")) == -1
    assert money.from_cents(1250) == Decimal("12.50")


def test_find_prices_offsets_and_labels():
    text = "Subtotal: 10,00 €\nTotal a pagar: 1.234,50 €"
    hits = money.find_prices(text)
    assert [h.amount for h in hits] == [Decimal("10.00"), Decimal("1234.50")]
    assert [h.label for h in hits] == ["subtotal", "total a pagar"]
    assert text[hits[1].start:hits[1].end] == "1.234,50 €"
    only = money.find_prices("hola 5 € y Total: 7 €", labelled_only=True)
    assert [h.amount for h in only] == [Decimal(7)]


def test_find_prices_does_not_glue_neighbours():
    hits = money.find_prices("Item 3 €2.50")
    assert [(h.amount, h.currency) for h in hits] == [(Decimal("2.50"), "EUR")]
    assert money.find_prices("10-12 €")[0].amount == Decimal(12)
    assert money.find_prices("sin precio 12345") == []


def test_split_shares_always_adds_up():
    for total in (0, 1, 99, 100, 101, 1000, 12345):
        for weights in ([1, 1, 1], [1, 2, 3], [0.25, 0.25, 0.5], [1, 0, 1], [7]):
            parts = money.split_shares(total, weights)
            assert sum(parts) == total and all(p >= 0 for p in parts)
    assert money.split_shares(100, [1, 1, 1]) == [34, 33, 33]
    assert money.split_shares(-100, {"a": 1, "b": 1, "c": 1}) == {"a": -34, "b": -33, "c": -33}
    with pytest.raises(ValueError):
        money.split_shares(100, [0, 0])
    with pytest.raises(ValueError):
        money.split_shares(100, [1, -1])


def test_settle_clears_every_balance_with_few_transfers():
    net = {"a": -5000, "b": 3000, "c": 2000, "d": 1000, "e": -1000, "f": 0}
    transfers = money.settle(net)
    left = dict(net)
    for t in transfers:
        left[t["from"]] += t["cents"]
        left[t["to"]] -= t["cents"]
    assert all(v == 0 for v in left.values())
    assert len(transfers) == 3                       # {e,d} and {a,b,c}: 1 + 2 transfers
    assert money.settle({}) == [] and money.settle({"x": 0}) == []


def test_formats():
    assert money.format_money(1234.5) == "1.234,50 €"
    assert money.format_money(1234.5, "EUR", "en") == "€1,234.50"
    assert money.format_money(-5, "USD", "en") == "-$5.00"
    assert money.format_money(0.004) == "0,00 €"
    assert money.format_money(-0.004) == "0,00 €"
    assert money.format_money(1234, "JPY", "es") == "1.234 ¥"
    assert money.format_money(None) == ""
    assert money.format_money(1234.5, nbsp=True) == "1.234,50 €"
