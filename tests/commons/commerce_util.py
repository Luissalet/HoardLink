"""Shared helpers for the commerce-cluster twin tests (money, dates, idcheck, tracking, merchants, ics, bizdays).

``norm`` turns what the Python functions return (Decimal, date, dataclasses, sets, tuples) into the JSON shape the
Node twins return, so one vector file checks both sides.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tests.commons.jsrun import load_vectors, python_call, run_js


def norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(norm(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): norm(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: norm(v) for k, v in dataclasses.asdict(value).items()}
    return value


def run_python(module: Any, cases: list[dict[str, Any]]) -> list[Any]:
    return [norm(python_call(module, c)) for c in cases]


def mismatches(cases: list[dict[str, Any]], got: list[Any]) -> list[tuple]:
    return [(c["fn"], c.get("args"), c.get("opts"), g, c["expect"]) for c, g in zip(cases, got) if g != c["expect"]]


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(norm(value)))


__all__ = ["norm", "run_python", "mismatches", "jsonable", "load_vectors", "python_call", "run_js"]
