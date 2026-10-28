"""The trace format and the helpers a Python probe or oracle uses to write it.

One line per case::

    {"id": "c000007", "ev": [["state", "after", {...}], ["prng", "random", 0.25], ["query", "damage", [90, 2], 41]], "out": {...}}

``ev`` is the ordered list of events: ``["state", name, value]`` (a state dump), ``["prng", name, value]`` (one call to
the random source) and ``["query", name, result, args]`` (a call to a collaborator, with its arguments). The *order and
number* of events is part of the result: two engines that roll the same dice in a different order diverge at the first
roll that differs, not at the final outcome. ``out`` is the final result of the case, ``err`` replaces it when the case
raised. Any other language writes the same JSON lines; nothing here is Python specific.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .corpus import dumps, read_jsonl


class CaseTrace:
    """Events of one case. Use as ``with writer.case(case) as t:`` and call ``t.state``, ``t.prng``, ``t.query``."""

    def __init__(self, case_id: str):
        self.id = case_id
        self.ev: list[list[Any]] = []
        self.out: Any = None
        self.err: Optional[str] = None

    def state(self, name: str, value: Any) -> None:
        self.ev.append(["state", name, value])

    def prng(self, name: str, value: Any) -> None:
        self.ev.append(["prng", name, value])

    def query(self, name: str, result: Any, *args: Any) -> Any:
        self.ev.append(["query", name, result, list(args)] if args else ["query", name, result])
        return result

    def result(self, value: Any) -> None:
        self.out = value

    def record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"id": self.id, "ev": self.ev}
        if self.err is not None:
            rec["err"] = self.err
        else:
            rec["out"] = self.out
        return rec


class RecordingRandom:
    """A seeded ``random.Random`` that logs every call into the current :class:`CaseTrace` (method name and result)."""

    def __init__(self, seed: Any, trace: CaseTrace):
        self._rng = random.Random(seed)
        self._trace = trace

    def _log(self, name: str, value: Any) -> Any:
        self._trace.prng(name, value)
        return value

    def random(self) -> float:
        return self._log("random", self._rng.random())

    def randrange(self, *a: int) -> int:
        return self._log("randrange", self._rng.randrange(*a))

    def randint(self, a: int, b: int) -> int:
        return self._log("randint", self._rng.randint(a, b))

    def choice(self, seq: Any) -> Any:
        return self._log("choice", self._rng.choice(seq))


class TraceWriter:
    """Write a trace file: ``with TraceWriter(path) as w: with w.case(case) as t: ...``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh: Any = None
        self.count = 0

    def __enter__(self) -> "TraceWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8", newline="\n")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh:
            self._fh.close()

    def case(self, case: dict[str, Any]) -> "_CaseContext":
        return _CaseContext(self, CaseTrace(str(case["id"])))

    def _write(self, trace: CaseTrace) -> None:
        self._fh.write(dumps(trace.record()) + "\n")
        self.count += 1


class _CaseContext:
    def __init__(self, writer: TraceWriter, trace: CaseTrace):
        self.writer, self.trace = writer, trace

    def __enter__(self) -> CaseTrace:
        return self.trace

    def __exit__(self, etype: Any, exc: Any, tb: Any) -> bool:
        if isinstance(exc, Exception):
            self.trace.err = f"{etype.__name__}: {exc}"
        self.writer._write(self.trace)
        return isinstance(exc, Exception)   # a case that raised is a recorded result, not a crash of the probe


def run_cli(execute: Callable[[dict[str, Any], CaseTrace], None], argv: Optional[list[str]] = None) -> int:
    """Entry point for a Python oracle or probe: ``execute(input, trace)`` once per case of ``argv[1]``, trace to ``argv[2]``."""
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print("usage: <inputs.jsonl> <trace.jsonl>", file=sys.stderr)
        return 2
    with TraceWriter(args[1]) as w:
        for case in read_jsonl(args[0]):
            with w.case(case) as t:
                execute(case["input"], t)
    return 0
