"""hoard_link.merchants and js/hoard-commons/merchants.js agree on tests/vectors/merchants_cases.json, share one data file."""

from __future__ import annotations

import json
import subprocess

import pytest

from hoard_link import merchants, tracking
from tests.commons.commerce_util import load_vectors, mismatches, run_js, run_python
from tests.commons.jsrun import JS_DIR, ROOT, node

CASES = load_vectors("merchants_cases")
PY_JSON = ROOT / "hoard_link" / "_data" / "merchants.json"
JS_JSON = JS_DIR / "merchants.json"


def test_python_vectors():
    assert not mismatches(CASES, run_python(merchants, CASES))


def test_node_twin():
    assert not mismatches(CASES, run_js("merchants.js", CASES))


def test_the_two_data_files_are_byte_identical():
    assert PY_JSON.read_bytes() == JS_JSON.read_bytes()


def test_required_similarity_cases():
    sim = merchants.merchant_similar
    assert sim("PcComponentes", "PC COMPONENTES SL")
    assert sim("Amazon", "AMZN Mktp ES")
    assert not sim("Amazon Prime", "Amazon Web Services")
    assert sim("NETFLIXCOM", "Netflix")
    assert sim("El Corte Inglés", "ECI")


def test_every_pattern_compiles_in_node_too():
    exe = node()
    script = (
        "const d = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));\n"
        "const bad = [];\n"
        "for (const m of d.merchants) for (const p of m.patterns) { try { new RegExp(p); } catch (e) { bad.push(m.id + ': ' + p); } }\n"
        "try { new RegExp(d.bank_pattern); } catch (e) { bad.push('bank_pattern'); }\n"
        "process.stdout.write(JSON.stringify(bad));\n"
    )
    out = subprocess.run([exe, "-e", script, str(JS_JSON)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == []


def test_data_is_consistent():
    ids = [m["id"] for m in merchants.MERCHANTS]
    assert len(ids) == len(set(ids)) >= 150
    for m in merchants.MERCHANTS:
        assert m["category"] in merchants.DATA["category_hints"], m["id"]
        assert (m["patterns"] or m["aliases"] or m["domains"]) and all(p == p.lower() for p in m["patterns"]), m["id"]
        if m.get("tracking_id"):
            assert m["carrier"] and m["tracking_id"] in tracking.CARRIERS, m["id"]
        assert not (m["carrier"] and not m.get("tracking_id")), m["id"]


def test_every_entry_resolves_to_itself():
    """Its display name, aliases and domains find the entry back (no earlier entry shadows it)."""
    for m in merchants.MERCHANTS:
        for probe in [m["display"], *m["aliases"], *m["domains"]]:
            assert merchants.merchant_id(probe) == m["id"], (m["id"], probe)


def test_lookup_returns_a_copy():
    a = merchants.lookup("Netflix")
    a["domains"].append("x.test")
    a["category"] = "zzz"
    b = merchants.lookup("Netflix")
    assert "x.test" not in b["domains"] and b["category"] == "streaming" and b["key"] == "netflix"


def test_carrier_ids_feed_tracking():
    assert tracking.carrier_name(merchants.carrier_of("Correos Express")) == "Correos Express"
    assert merchants.carrier_of("Packlink") == "other"


def test_order_refs_agree_with_the_old_apps():
    # Ledger: first reference wins; Phileas: a UPS number is not an order
    assert merchants.find_order_ref("Pedido n.º 123-1234567-7654321") == "123-1234567-7654321"
    assert merchants.find_order_refs("Order number: 1Z999AA10123456784") == []
    assert merchants.order_key("123 1234567 7654321") == merchants.order_key("123-1234567-7654321")
