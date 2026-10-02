"""hoard_link.docs.* and js/hoard-commons/docs.js agree on tests/vectors/docs_*.json (golden vectors)."""

from __future__ import annotations

import dataclasses
import importlib
import re
from typing import Any

import pytest

from tests.commons.docs_helpers import approx, jsonable, run_js_bytes, unb64
from tests.commons.jsrun import load_vectors

FILES = ["docs_fts", "docs_ranges", "docs_chunks", "docs_sniff", "docs_cite", "docs_vec"]
JS_NAME = {"describe": "describe_ranges"}                 # Python and Node spell this one differently
FLOAT_FILES = {"docs_vec"}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _snake_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {(_snake(k) if k != "__b64__" and k != "__error__" else k): _snake_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snake_keys(v) for v in value]
    return value


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _plain(dataclasses.asdict(value))
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _py(case: dict[str, Any]) -> Any:
    mod = importlib.import_module(f"hoard_link.docs.{case['mod']}")
    try:
        res = getattr(mod, case["fn"])(*unb64(case.get("args", [])), **(case.get("opts") or {}))
    except Exception as exc:  # noqa: BLE001 - errors are part of the vectors
        return {"__error__": str(exc)}
    return jsonable(_plain(res))


def _case_id(case: dict[str, Any]) -> str:
    return f"{case['fn']}:{str(case['args'])[:36]}"


@pytest.mark.parametrize("name", FILES)
def test_python_matches_vectors(name):
    bad = []
    for case in load_vectors(name):
        got = _py(case)
        ok = approx(got, case["expect"], 1e-5) if name in FLOAT_FILES else got == case["expect"]
        if not ok:
            bad.append((case["fn"], str(case["args"])[:80], got, case["expect"]))
    assert not bad, bad[:5]


@pytest.mark.parametrize("name", FILES)
def test_node_matches_vectors(name):
    cases = load_vectors(name)
    calls = [{"fn": JS_NAME.get(c["fn"], c["fn"]), "args": c.get("args", []), "opts": c.get("opts")} for c in cases]
    got = run_js_bytes("docs.js", calls)
    bad = []
    for case, g in zip(cases, got):
        g = _snake_keys(g)
        ok = approx(g, case["expect"], 1e-5) if name in FLOAT_FILES else g == case["expect"]
        if not ok:
            bad.append((case["fn"], str(case["args"])[:80], g, case["expect"]))
    assert not bad, bad[:5]
