"""Seeded corpora: an edge grid plus pseudo-random cases, written as JSONL the oracle and the probe both read.

A corpus is a list of cases ``{"id": "c000042", "input": {...}}``. The same seed always gives the same file, so a
failure can be reproduced from the seed alone and the corpus hash goes into the report. Floats travel as hexadecimal
bit patterns (:func:`f32_bits`, :func:`f64_bits`) so that ``-0.0``, ``nan`` and ``1e-40`` survive any JSON parser.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import struct
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

#: Values that break arithmetic: signed zero, denormals, huge, infinities and NaN.
EDGE_FLOATS: tuple[float, ...] = (-math.inf, -1e30, -1.0, -0.0, 0.0, 1e-40, 0.1, 1.0, 1e30, math.inf, math.nan)
#: Integer boundaries of 8, 16 and 32 bit ranges plus the small values around zero.
EDGE_INTS: tuple[int, ...] = (-2147483648, -32769, -32768, -129, -128, -1, 0, 1, 127, 128, 255, 256, 32767, 32768, 65535, 2147483647)


def f32_bits(value: float) -> str:
    return struct.pack(">f", value).hex()


def f64_bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def from_f32_bits(text: str) -> float:
    return struct.unpack(">f", bytes.fromhex(text))[0]


def from_f64_bits(text: str) -> float:
    return struct.unpack(">d", bytes.fromhex(text))[0]


def dumps(value: Any) -> str:
    """One canonical JSON line: compact separators, keys in the order given."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Corpus:
    """Build cases with a fixed seed. ``rng`` is a ``random.Random`` seeded from ``seed`` (an int or a string)."""

    def __init__(self, seed: int | str):
        self.seed = seed
        self.rng = random.Random(seed if isinstance(seed, int) else int.from_bytes(hashlib.sha256(str(seed).encode()).digest()[:8], "big"))
        self._cases: list[dict[str, Any]] = []

    def add(self, input: Mapping[str, Any]) -> None:
        self._cases.append({"id": f"c{len(self._cases):06d}", "input": dict(input)})

    def grid(self, build: Callable[..., Mapping[str, Any]], **axes: Sequence[Any]) -> int:
        """Every combination of the axes (an edge grid), in axis order; ``build(**combo)`` makes the input."""
        names = list(axes)
        count = 0
        for combo in itertools.product(*(axes[n] for n in names)):
            self.add(build(**dict(zip(names, combo))))
            count += 1
        return count

    def sample(self, count: int, build: Callable[[random.Random], Mapping[str, Any]]) -> None:
        """``count`` cases from ``build(rng)``, drawn from the corpus' own generator."""
        for _ in range(count):
            self.add(build(self.rng))

    def cases(self) -> list[dict[str, Any]]:
        return list(self._cases)

    def __len__(self) -> int:
        return len(self._cases)


def write_jsonl(path: str | Path, cases: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
    """Write the cases, one per line, UTF-8 with ``\\n`` endings. Returns ``(count, sha256 of the file)``."""
    digest = hashlib.sha256()
    count = 0
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for case in cases:
            line = dumps(case) + "\n"
            fh.write(line)
            digest.update(line.encode("utf-8"))
            count += 1
    return count, digest.hexdigest()


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
