"""Differential test kit: run an oracle and a probe over the same seeded corpus and compare their traces exactly.

    python -m hoard_link.parity run harness.py        # run (or skip, when nothing it depends on changed)
    python -m hoard_link.parity compare a.jsonl b.jsonl --inputs inputs.jsonl
    python -m hoard_link.parity status --state-dir .parity

See ``docs/parity.md`` for the harness file, the trace format and the report."""

from __future__ import annotations

from .cache import OracleCache
from .compare import Divergence, EmptyTraceError, Matched, Survey, TraceMismatch, compare_traces, survey_traces
from .corpus import EDGE_FLOATS, EDGE_INTS, Corpus, f32_bits, f64_bits, from_f32_bits, from_f64_bits, read_jsonl, write_jsonl
from .harness import Command, Harness, load_harness, run_harness, write_lock
from .lock import LockError, make_lock, verify_lock
from .select import State, changed_harnesses, git_changed
from .trace import CaseTrace, RecordingRandom, TraceWriter, run_cli

__all__ = [
    "OracleCache", "Divergence", "EmptyTraceError", "Matched", "Survey", "TraceMismatch", "compare_traces", "survey_traces", "EDGE_FLOATS", "EDGE_INTS",
    "Corpus", "f32_bits", "f64_bits", "from_f32_bits", "from_f64_bits", "read_jsonl", "write_jsonl", "Command", "Harness",
    "load_harness", "run_harness", "write_lock", "LockError", "make_lock", "verify_lock", "State", "changed_harnesses",
    "git_changed", "CaseTrace", "RecordingRandom", "TraceWriter", "run_cli",
]
