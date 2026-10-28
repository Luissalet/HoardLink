"""Exact comparison of two trace files, by blocks, with the first line that differs.

* Equal files are detected a megabyte at a time, so a pass over a multi-gigabyte trace is not a Python loop per line.
* An empty or missing trace never passes: "an empty output proves nothing".
* When the files differ, the first differing case is parsed and the first differing *path* inside it is named
  (``ev[3][2]``) together with the event kind (``prng``, ``state``, ``query``), what each side said and the case input.
* Tolerant reference values, for a reference that can only say "somewhere in this interval": ``{"$range": [lo, hi]}`` accepts
  a number, or a list of numbers, inside the closed interval; ``{"$approx": [value, tolerance]}`` accepts a number within
  ``tolerance`` of ``value``; ``{"$oneof": [a, b, ...]}`` accepts any of the listed values (same type, compared exactly:
  the reference observed several legal outcomes, e.g. one per damage roll). Everything else is compared exactly
  (``1`` and ``1.0`` differ).
* ``survey_traces`` compares every case instead of stopping at the first and groups the divergences by kind.
* ``normalize`` (a function on the parsed record) removes what is allowed to differ (ids, dates, rounding) on both sides,
  and then the comparison is by record instead of by bytes.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

CHUNK = 1024 * 1024


class EmptyTraceError(AssertionError):
    """A trace without a single case was offered as proof of parity."""


class TraceMismatch(AssertionError):
    def __init__(self, divergence: "Divergence"):
        super().__init__(divergence.summary)
        self.divergence = divergence


@dataclass
class Divergence:
    line: int
    case_id: Optional[str]
    path: str
    kind: str                     # "event", "result", "length", "missing_case", "extra_case", "unparseable"
    expected: Any
    actual: Any
    summary: str
    event_index: Optional[int] = None
    event_kind: Optional[str] = None
    event_name: Optional[str] = None
    context: dict[str, list[Any]] = field(default_factory=dict)   # the events just before, from each side
    input: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Matched:
    cases: int
    lines: int


def _tolerant(spec: dict[str, Any], actual: Any) -> bool:
    if "$oneof" in spec:
        return any(type(actual) is type(v) and actual == v for v in spec["$oneof"])
    if "$range" in spec:
        lo, hi = spec["$range"]
        values = actual if isinstance(actual, list) else [actual]
        return bool(values) and all(isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi for v in values)
    value, tol = spec["$approx"]
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and abs(actual - value) <= tol


def _is_tolerant(a: Any) -> bool:
    return isinstance(a, dict) and len(a) == 1 and ("$range" in a or "$approx" in a or "$oneof" in a)


def _walk(a: Any, b: Any, path: str = "") -> Optional[tuple[str, Any, Any]]:
    """First path where two JSON values differ (lists by index, dicts by sorted key); NaN-free by construction."""
    if _is_tolerant(a):
        return None if _tolerant(a, b) else (path or "$", a, b)
    if type(a) is not type(b):                      # 1 and 1.0 are different results
        return path or "$", a, b
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}", a.get(k, "<missing>"), b.get(k, "<missing>")
            r = _walk(a[k], b[k], f"{path}.{k}")
            if r:
                return r
        return None
    if isinstance(a, list):
        for i in range(max(len(a), len(b))):
            if i >= len(a) or i >= len(b):
                return f"{path}[{i}]", a[i] if i < len(a) else "<missing>", b[i] if i < len(b) else "<missing>"
            r = _walk(a[i], b[i], f"{path}[{i}]")
            if r:
                return r
        return None
    return (path or "$", a, b) if a != b else None


def _describe(line: int, left: Any, right: Any, inputs: Optional[Callable[[str], Any]]) -> Divergence:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return Divergence(line, None, "$", "unparseable", left, right, f"line {line}: a record is not a JSON object")
    cid = left.get("id", right.get("id"))
    found = _walk(left, right)
    path, exp, act = found if found else ("$", left, right)
    if left.get("id") != right.get("id"):
        return Divergence(line, str(cid), ".id", "missing_case" if cid else "extra_case", left.get("id"), right.get("id"),
                          f"line {line}: the traces list different cases here: {left.get('id')!r} vs {right.get('id')!r}", input=None)
    div = Divergence(line, str(cid), path, "result", exp, act, "", input=inputs(str(cid)) if inputs else None)
    if path.startswith(".ev["):
        idx = int(path[4:path.index("]")])
        div.kind, div.event_index = "event", idx
        ev_l, ev_r = left.get("ev", []), right.get("ev", [])
        ev = ev_l[idx] if idx < len(ev_l) else None
        ev = ev or (ev_r[idx] if idx < len(ev_r) else None)
        if isinstance(ev, list) and len(ev) >= 2:
            div.event_kind, div.event_name = str(ev[0]), str(ev[1])
        div.context = {"expected": ev_l[max(0, idx - 3):idx + 1], "actual": ev_r[max(0, idx - 3):idx + 1]}
        what = f"event {idx} ({div.event_kind} {div.event_name})" if div.event_kind else f"event {idx}"
        if idx >= min(len(ev_l), len(ev_r)):
            div.kind = "length"
            what = f"event {idx}: one side stopped after {min(len(ev_l), len(ev_r))} events"
        div.summary = f"case {cid} (line {line}), {what}, at {path}: expected {exp!r}, got {act!r}"
    else:
        div.summary = f"case {cid} (line {line}), at {path}: expected {exp!r}, got {act!r}"
    return div


def compare_traces(reference: str | Path, candidate: str | Path, *, normalize: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
                   inputs: Optional[Callable[[str], Any]] = None) -> Matched:
    """Raise :class:`EmptyTraceError` or :class:`TraceMismatch` unless both files hold the same non-empty trace."""
    ref, cand = Path(reference), Path(candidate)
    for p, label in ((ref, "reference"), (cand, "candidate")):
        if not p.exists():
            raise EmptyTraceError(f"{label} trace {p} does not exist")
        if p.stat().st_size == 0:
            raise EmptyTraceError(f"{label} trace {p} is empty: an empty output cannot establish parity")
    if normalize is None:
        count, last, nonblank = 0, "", False
        with ref.open(encoding="utf-8", newline="") as a, cand.open(encoding="utf-8", newline="") as b:
            while True:
                x, y = a.read(CHUNK), b.read(CHUNK)
                if x != y:
                    break
                if not x:
                    if not nonblank:
                        raise EmptyTraceError("the traces hold no case: an empty output cannot establish parity")
                    return Matched(count + (0 if last.endswith("\n") else 1), count + (0 if last.endswith("\n") else 1))
                count += x.count("\n")
                nonblank = nonblank or bool(x.strip())
                last = x[-1]
    # slow path: find the first differing record (also the normalised comparison)
    lines = cases = 0
    with ref.open(encoding="utf-8") as a, cand.open(encoding="utf-8") as b:
        for n, (x, y) in enumerate(itertools.zip_longest(a, b), 1):
            if x is None or y is None:
                side = "reference" if x is None else "candidate"
                missing = json.loads(y if x is None else x)
                raise TraceMismatch(Divergence(n, str(missing.get("id")), ".id", "extra_case" if x is None else "missing_case",
                                               None if x is None else missing.get("id"), missing.get("id") if x is None else None,
                                               f"line {n}: the {side} trace ends before case {missing.get('id')!r}",
                                               input=inputs(str(missing.get("id"))) if inputs else None))
            try:
                left, right = json.loads(x), json.loads(y)
            except ValueError:
                if x != y:
                    raise TraceMismatch(Divergence(n, None, "$", "unparseable", x.strip()[:200], y.strip()[:200], f"line {n}: not valid JSON on one side"))
                continue
            if normalize:
                left, right = normalize(left), normalize(right)
            elif x != y and _walk(left, right) is None and not any(k in x for k in ("$range", "$approx", "$oneof")):
                raise TraceMismatch(Divergence(n, str(left.get("id")) if isinstance(left, dict) else None, "$", "format", x.strip()[:200],
                                               y.strip()[:200], f"line {n}: same values, different text (key order or number format)"))
            if _walk(left, right) is not None:
                raise TraceMismatch(_describe(n, left, right, inputs))
            lines += 1
            cases += 1
    if cases == 0:
        raise EmptyTraceError("the traces hold no case: an empty output cannot establish parity")
    return Matched(cases, lines)


@dataclass
class Survey:
    cases: int
    matching: int
    divergences: list[Divergence]            # the first ones, in order
    by_category: dict[str, int]              # how many cases diverge in each way (indices in the path generalised)
    total_divergent: int

    def to_dict(self) -> dict[str, Any]:
        return {"cases": self.cases, "matching": self.matching, "divergent": self.total_divergent,
                "by_category": dict(sorted(self.by_category.items(), key=lambda kv: -kv[1])),
                "examples": [d.to_dict() for d in self.divergences]}


def category(d: Divergence) -> str:
    path = re.sub(r"\[\d+\]", "[*]", d.path)
    return ":".join(str(x) for x in (d.kind, d.event_kind, d.event_name, path) if x)


def survey_traces(reference: str | Path, candidate: str | Path, *, normalize: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
                  inputs: Optional[Callable[[str], Any]] = None, keep: int = 20) -> Survey:
    """Compare every case (aligned by line) and count the divergent ones by category; ``keep`` examples are returned in full.

    An empty trace on either side raises :class:`EmptyTraceError`, as in :func:`compare_traces`."""
    ref, cand = Path(reference), Path(candidate)
    for p, label in ((ref, "reference"), (cand, "candidate")):
        if not p.exists() or p.stat().st_size == 0:
            raise EmptyTraceError(f"{label} trace {p} is missing or empty: an empty output cannot establish parity")
    total = matching = 0
    found: list[Divergence] = []
    cats: dict[str, int] = {}
    bad = 0
    with ref.open(encoding="utf-8") as a, cand.open(encoding="utf-8") as b:
        for n, (x, y) in enumerate(itertools.zip_longest(a, b), 1):
            total += 1
            div: Optional[Divergence] = None
            if x is None or y is None:
                rec = json.loads(y if x is None else x)
                div = Divergence(n, str(rec.get("id")), ".id", "extra_case" if x is None else "missing_case", None, None,
                                 f"line {n}: one trace ends before case {rec.get('id')!r}")
            else:
                try:
                    left, right = json.loads(x), json.loads(y)
                except ValueError:
                    left = right = None
                    div = Divergence(n, None, "$", "unparseable", x.strip()[:200], y.strip()[:200], f"line {n}: not valid JSON on one side")
                if div is None:
                    if normalize:
                        left, right = normalize(left), normalize(right)
                    if _walk(left, right) is not None:
                        div = _describe(n, left, right, inputs)
            if div is None:
                matching += 1
                continue
            bad += 1
            cats[category(div)] = cats.get(category(div), 0) + 1
            if len(found) < keep:
                found.append(div)
    if total == 0:
        raise EmptyTraceError("the traces hold no case: an empty output cannot establish parity")
    return Survey(total, matching, found, cats, bad)
