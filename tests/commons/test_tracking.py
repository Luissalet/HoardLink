"""hoard_link.tracking and js/hoard-commons/tracking.js agree on tests/vectors/tracking.json."""

from __future__ import annotations

import pytest

from hoard_link import tracking
from tests.commons.commerce_util import load_vectors, mismatches, run_js, run_python

CASES = load_vectors("tracking")
CLEAN = [c for c in CASES if c["fn"] == "clean_url"]


def test_python_vectors():
    assert not mismatches(CASES, run_python(tracking, CASES))


def test_node_twin():
    assert not mismatches(CASES, run_js("tracking.js", CASES))


@pytest.mark.parametrize("case", CLEAN, ids=lambda c: c["args"][0][:40])
def test_local_fallback_agrees_with_web_urls(case):
    """The small copy kept for when hoard_link.web.urls cannot be imported gives the same links."""
    s = tracking._unwrap_awstrack(case["args"][0].strip())
    assert tracking._local_clean_url(s) == case["expect"]


def test_node_local_fallback_agrees_too():
    calls = [{"fn": "local_clean_url", "args": [tracking._unwrap_awstrack(c["args"][0].strip())]} for c in CLEAN]
    got = run_js("tracking.js", calls)
    assert got == [c["expect"] for c in CLEAN]


def test_clean_url_works_without_web_urls(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.endswith("web.urls") or (name == "web.urls"):
            raise ImportError("no web")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert tracking.clean_url("https://shop.es/p?utm_source=a&id=1") == "https://shop.es/p?id=1"


def test_phileas_behaviour_is_kept():
    items = tracking.find("Tu paquete UPS 1Z999AA10123456784 y RR123456785CN.")
    assert [(i.number, i.carrier, i.confidence, i.evidence) for i in items] == [
        ("1Z999AA10123456784", "ups", 98, "format"), ("RR123456785CN", "chinapost", 92, "format")]
    assert items[0].to_dict() == {"number": "1Z999AA10123456784", "carrier": "ups", "confidence": 98, "evidence": "format"}
    assert tracking.Found is tracking.Tracking
    # an Amazon order number after a label is not a parcel, a phone number is not a parcel
    assert tracking.find("Número de pedido: 123-1234567-1234567") == []
    assert tracking.find("Seguimiento: 612345678") == []


def test_every_carrier_has_a_name_and_a_link_template():
    for cid, info in tracking.CARRIERS.items():
        assert "name" in info and "url" in info
        if info["url"]:
            assert "{n}" in info["url"]
