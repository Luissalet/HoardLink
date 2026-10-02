"""hoard_link.text and its Node twin js/hoard-commons/text.js agree on tests/vectors/text.json."""

from __future__ import annotations

import pytest

from hoard_link import text
from tests.commons.jsrun import load_vectors, normalise, python_call, run_js

CASES = load_vectors("text")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{str(c['args'])[:30]}")
def test_python(case):
    assert normalise(python_call(text, case)) == case["expect"]


def test_node_twin():
    got = run_js("text.js", CASES)
    bad = [(c["fn"], c["args"], g, c["expect"]) for c, g in zip(CASES, got) if g != c["expect"]]
    assert not bad, bad


def test_fold_keeps_offsets():
    s = "Él comió ñoquis en Ålesund ß"
    assert len(text.fold(s)) == len(s)
    i = text.fold(s).index("noquis")
    assert s[i:i + 6] == "ñoquis"


def test_sha256_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hola")
    assert text.sha256_file(p) == text.sha256_text("hola")


def test_now_iso_has_offset():
    v = text.now_iso()
    assert v[10] == "T" and (v.endswith("Z") or v[-6] in "+-")
