"""hoard_link.idcheck and js/hoard-commons/idcheck.js agree on tests/vectors/idcheck.json."""

from __future__ import annotations

from hoard_link import idcheck
from tests.commons.commerce_util import load_vectors, mismatches, run_js, run_python

CASES = load_vectors("idcheck")


def test_python_vectors():
    assert not mismatches(CASES, run_python(idcheck, CASES))


def test_node_twin():
    assert not mismatches(CASES, run_js("idcheck.js", CASES))


def test_only_valid_checksums_are_reported():
    text = "12345678A, ES9121000418450200051333, 4539 1488 0343 6468, B12345675, 612345678"
    assert idcheck.scan_pii(text) == []
    assert idcheck.mask_text(text) == text


def test_hits_point_into_the_text_and_never_overlap():
    text = "DNI 12345678Z; IBAN ES91 2100 0418 4502 0005 1332 TOTAL; tarjeta 4539 1488 0343 6467; a@b.es"
    hits = idcheck.scan_pii(text)
    assert [h.kind for h in hits] == ["DNI", "IBAN", "CARD", "EMAIL"]
    assert all(text[h.start:h.end] == h.value for h in hits)
    assert all(a.end <= b.start for a, b in zip(hits, hits[1:]))
    assert hits[1].value == "ES91 2100 0418 4502 0005 1332"          # the trailing TOTAL stays out


def test_masking_is_idempotent_and_keeps_shape():
    obj = {"msg": "mi DNI es 12345678Z", "list": ("ana@x.es", 3), "n": 7}
    masked = idcheck.mask_obj(obj)
    assert masked == {"msg": "mi DNI es <DNI>", "list": ("<EMAIL>", 3), "n": 7}
    assert idcheck.mask_obj(masked) == masked


def test_ean_8_12_13_14_and_nothing_else():
    assert [idcheck.ean_ok(c) for c in ("96385074", "036000291452", "4006381333931", "00012345600012")] == [True] * 4
    assert not any(idcheck.ean_ok(c) for c in ("1234567", "123456789", "12345678901", "123456789012345"))


def test_old_audit_gap_any_digit_run_is_no_longer_an_ean():
    ids = idcheck.identifiers("pedido 12345678 y 1234567890123")
    assert ids["ean"] == []
