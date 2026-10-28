"""A differential harness and the runner that executes it.

A harness pairs two programs that must agree: the *oracle* (the reference, the code that is right by definition) and
the *probe* (the code under test). Both are separate processes that read the same JSONL corpus and write a trace file
(see :mod:`hoard_link.parity.trace`). The runner builds the corpus from a seed, runs both, compares the traces exactly
and writes ``report.json``, whose mandatory ``scope`` field says what the harness does **not** cover.

Built on the usual practice of differential testing: oracle and probe as processes, traces of state and
random-source calls instead of final results only, first-divergence reports, pinned versions, selective reruns and a
cached oracle. The code here is written for the Hoard family.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..atomic import write_json_atomic
from .cache import OracleCache
from .compare import EmptyTraceError, TraceMismatch, compare_traces, survey_traces
from .corpus import Corpus, read_jsonl, write_jsonl
from .lock import LockError, ensure_unchanged, make_lock, sha256_file, snapshot, verify_lock
from .select import State, expand, selection_key

REPORT_SCHEMA = 1


@dataclass
class Command:
    """A process. ``{inputs}`` and ``{output}`` in ``argv`` are replaced by the corpus file and the trace file;
    without them both paths are appended in that order. ``env`` adds to the environment."""

    argv: list[str]
    cwd: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 900.0

    def resolve(self, inputs: Path, output: Path) -> list[str]:
        have = any("{inputs}" in a or "{output}" in a for a in self.argv)
        argv = [a.replace("{inputs}", str(inputs)).replace("{output}", str(output)) for a in self.argv]
        return argv if have else [*argv, str(inputs), str(output)]


@dataclass
class Harness:
    name: str
    root: Path
    corpus: Callable[[], Corpus | Iterable[dict[str, Any]]]
    oracle: Command
    probe: Command
    scope: str                                  # what this harness does NOT cover; required
    depends_on: list[str] = field(default_factory=list)          # globs: every file the probe, oracle or corpus reads
    oracle_depends_on: list[str] = field(default_factory=list)   # globs: files that change the oracle's output
    lock: Optional[str] = None                  # lock.json relative to root
    lock_artifacts: dict[str, str] = field(default_factory=dict)  # for `parity lock`: name -> path relative to root
    lock_tools: dict[str, list[str]] = field(default_factory=dict)  # name -> version command
    normalize: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
    survey: bool = False                        # on a divergence, also count every divergent case by category
    description: str = ""
    source_file: Optional[Path] = None

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if not self.scope or not self.scope.strip():
            raise ValueError(f"harness {self.name!r}: `scope` is required and must say what the harness does not cover")
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError("harness name must be a plain identifier")


def load_harness(path: str | Path) -> Harness:
    """Import a harness file; it defines ``harness()`` returning a :class:`Harness` (or a module-level ``HARNESS``)."""
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(f"parity_harness_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
    h = mod.harness() if hasattr(mod, "harness") else getattr(mod, "HARNESS", None)
    if not isinstance(h, Harness):
        raise TypeError(f"{path} must define harness() or HARNESS returning a Harness")
    h.source_file = path
    return h


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(cmd: Command, inputs: Path, output: Path, log: Path) -> tuple[int, float]:
    argv = cmd.resolve(inputs, output)
    env = {**os.environ, **cmd.env}
    start = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with open(log, "wb") as fh:
        try:
            p = subprocess.run(argv, cwd=cmd.cwd, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=cmd.timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[parity] timed out after {cmd.timeout}s\n".encode())
            code = 124
        except OSError as exc:
            fh.write(f"\n[parity] cannot start {argv[0]!r}: {exc}\n".encode())
            code = 127
    return code, time.monotonic() - start


def _tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def run_harness(h: Harness, state_dir: str | Path, *, fresh: bool = False, force: bool = False, stable: int = 1) -> dict[str, Any]:
    """Run one harness and return its report (also written to ``<state_dir>/<name>/report.json``).

    ``status`` is ``pass``, ``fail`` (the traces differ), ``error`` (a process failed, the lock does not hold, the
    traces are empty or a source changed mid-run) or ``skipped`` (nothing it depends on changed since it last passed).
    ``fresh`` ignores the oracle cache; ``force`` ignores the skip; ``stable`` is how many passes in a row a harness
    needs before it counts as stable (a new harness should pass three times)."""
    state_dir = Path(state_dir)
    work = state_dir / h.name
    work.mkdir(parents=True, exist_ok=True)
    started = _now()
    t0 = time.monotonic()
    report: dict[str, Any] = {"schema": REPORT_SCHEMA, "harness": h.name, "description": h.description, "scope": h.scope,
                              "started_at": started, "environment": {"python": platform.python_version(), "platform": platform.platform()}}

    def finish(status: str, **extra: Any) -> dict[str, Any]:
        report.update(status=status, finished_at=_now(), duration_s=round(time.monotonic() - t0, 3), **extra)
        if status != "skipped":                  # a skipped run must not overwrite the report of the run that passed
            write_json_atomic(work / "report.json", report)
        return report

    state = State(state_dir)
    lock_path = h.root / h.lock if h.lock else None
    sources = expand(h.root, [*h.depends_on, *h.oracle_depends_on])
    if h.source_file:
        sources.append(h.source_file)
    if lock_path:
        sources.append(lock_path)
    try:
        lock_hashes: dict[str, str] = {}
        lock_sha = ""
        if lock_path:
            if not lock_path.exists():
                return finish("error", error=f"lock file {h.lock} is missing; create it with `parity lock`")
            import json as _json
            lock_hashes = verify_lock(_json.loads(lock_path.read_text(encoding="utf-8")), h.root, h.lock_tools)
            lock_sha = sha256_file(lock_path)
        key = selection_key(sources, [str(sorted(lock_hashes.items()))])
        why = state.needs_run(h.name, key, stable)
        if why is None and not force:
            prev = state.get(h.name)
            return finish("skipped", reason="nothing it depends on changed since it passed", selection_key=key, stability={"passes": prev.get("passes", 0), "required": stable})
        before = snapshot(sources)

        built = h.corpus()
        cases = built.cases() if isinstance(built, Corpus) else list(built)
        inputs = work / "inputs.jsonl"
        count, inputs_sha = write_jsonl(inputs, cases)
        if count == 0:
            return finish("error", error="the corpus is empty: nothing would be compared")
        report["corpus"] = {"cases": count, "sha256": inputs_sha, **({"seed": built.seed} if isinstance(built, Corpus) else {})}
        by_id = {c["id"]: c.get("input") for c in cases}

        oracle_out, probe_out = work / "oracle.jsonl", work / "probe.jsonl"
        cache = OracleCache(state_dir / "oracle-cache")
        ckey = cache.key(h.oracle.resolve(inputs, oracle_out), inputs_sha, lock_sha,
                         selection_key(expand(h.root, h.oracle_depends_on)))
        t1 = time.monotonic()
        cached = (not fresh) and cache.get(ckey, oracle_out)
        if not cached:
            code, _ = _run(h.oracle, inputs, oracle_out, work / "oracle.log")
            if code != 0:
                return finish("error", error=f"the oracle exited with {code}", log_tail=_tail(work / "oracle.log"))
            if not oracle_out.exists() or oracle_out.stat().st_size == 0:
                return finish("error", error="the oracle wrote no trace: an empty output cannot establish parity", log_tail=_tail(work / "oracle.log"))
            cache.put(ckey, oracle_out)
        report["oracle"] = {"argv": h.oracle.argv, "cached": bool(cached), "seconds": round(time.monotonic() - t1, 3),
                            "output_sha256": sha256_file(oracle_out)}
        t2 = time.monotonic()
        code, _ = _run(h.probe, inputs, probe_out, work / "probe.log")
        if code != 0:
            return finish("error", error=f"the probe exited with {code}", log_tail=_tail(work / "probe.log"))
        report["probe"] = {"argv": h.probe.argv, "seconds": round(time.monotonic() - t2, 3),
                           "output_sha256": sha256_file(probe_out) if probe_out.exists() else None}
        ensure_unchanged(before)
        try:
            matched = compare_traces(oracle_out, probe_out, normalize=h.normalize, inputs=by_id.get)
        except TraceMismatch as exc:
            state.record(h.name, key, "fail", _now())
            extra: dict[str, Any] = {}
            if h.survey:
                extra["survey"] = survey_traces(oracle_out, probe_out, normalize=h.normalize, inputs=by_id.get).to_dict()
            return finish("fail", divergence=exc.divergence.to_dict(), selection_key=key, stability={"passes": 0, "required": stable}, **extra)
        except EmptyTraceError as exc:
            state.record(h.name, key, "error", _now())
            return finish("error", error=str(exc), selection_key=key)
        entry = state.record(h.name, key, "pass", _now())
        return finish("pass", matched={"cases": matched.cases, "lines": matched.lines}, sources={p.name: sha256_file(p) for p in sources if p.exists()},
                      lock={"sha256": lock_sha, "artifacts": lock_hashes} if lock_path else None, selection_key=key,
                      stability={"passes": entry["passes"], "required": stable, "stable": entry["passes"] >= stable})
    except LockError as exc:
        return finish("error", error=str(exc))


def write_lock(h: Harness) -> Path:
    if not h.lock or not h.lock_artifacts:
        raise ValueError("the harness declares no `lock` path or `lock_artifacts`")
    import json
    path = h.root / h.lock
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, make_lock(h.root, h.lock_artifacts, h.lock_tools))
    return path
