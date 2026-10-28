"""Facet ``parity``: the differential test kit (``hoard_link.parity``) as agent tools and HTTP routes.

    POST /api/parity/compare   {reference, candidate, inputs?, survey?}   -> match / first divergence / survey
    GET  /api/parity/status?state_dir=...                                  -> last result of every harness in that folder
    POST /api/parity/run       {harness, state_dir?, fresh?, force?, survey?, stable?} -> starts a run in the background

Comparing and reading are in-process and read-only. A run starts ``python -m hoard_link.parity run`` as a separate process
(a harness may take minutes) and writes its output to ``<state_dir>/last-run.log``; ``status`` shows it while it is running."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..parity import EmptyTraceError, TraceMismatch, compare_traces, survey_traces
from ..parity.corpus import read_jsonl
from ..parity.select import State
from .facets import Facet, Request

_RUNNING: dict[str, subprocess.Popen] = {}
_LOCK = threading.Lock()


def _state_dir(raw: Any) -> Path:
    return Path(str(raw or ".parity")).expanduser().resolve()


class ParityFacet(Facet):
    id = "parity"

    def compare(self, a: dict[str, Any]) -> dict[str, Any]:
        ref, cand = Path(str(a.get("reference") or "")), Path(str(a.get("candidate") or ""))
        inputs = None
        if a.get("inputs"):
            table = {c["id"]: c.get("input") for c in read_jsonl(str(a["inputs"]))}
            inputs = table.get
        try:
            if a.get("survey"):
                sv = survey_traces(ref, cand, inputs=inputs)
                return {"ok": True, "result": "diverge" if sv.total_divergent else "match", "survey": sv.to_dict()}
            m = compare_traces(ref, cand, inputs=inputs)
            return {"ok": True, "result": "match", "cases": m.cases}
        except EmptyTraceError as exc:
            return {"ok": False, "result": "empty", "error": str(exc)}
        except TraceMismatch as exc:
            return {"ok": True, "result": "diverge", "divergence": exc.divergence.to_dict()}
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def status(self, a: dict[str, Any]) -> dict[str, Any]:
        folder = _state_dir(a.get("state_dir"))
        rows = []
        state = State(folder)
        for name, e in sorted(state.data["harnesses"].items()):
            row: dict[str, Any] = {"harness": name, **e}
            rp = folder / name / "report.json"
            if rp.exists():
                try:
                    rep = json.loads(rp.read_text(encoding="utf-8"))
                    row["report"] = {k: rep.get(k) for k in ("status", "finished_at", "duration_s", "corpus", "matched", "scope", "error")}
                    if rep.get("divergence"):
                        row["report"]["divergence"] = rep["divergence"].get("summary")
                    if rep.get("survey"):
                        row["report"]["survey"] = {"divergent": rep["survey"]["divergent"], "cases": rep["survey"]["cases"],
                                                   "by_category": rep["survey"]["by_category"]}
                except ValueError:
                    pass
            rows.append(row)
        with _LOCK:
            proc = _RUNNING.get(str(folder))
            running = bool(proc and proc.poll() is None)
        log = folder / "last-run.log"
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:] if log.exists() else []
        return {"ok": True, "state_dir": str(folder), "running": running, "harnesses": rows, "last_run_log": tail}

    def run(self, a: dict[str, Any]) -> dict[str, Any]:
        harness = Path(str(a.get("harness") or "")).expanduser()
        if not harness.is_file():
            return {"ok": False, "error": f"harness file not found: {harness}"}
        folder = _state_dir(a.get("state_dir"))
        with _LOCK:
            proc = _RUNNING.get(str(folder))
            if proc and proc.poll() is None:
                return {"ok": False, "error": "a run is already in progress for this state_dir", "pid": proc.pid}
            folder.mkdir(parents=True, exist_ok=True)
            argv = [sys.executable, "-m", "hoard_link.parity", "run", str(harness.resolve()), "--state-dir", str(folder)]
            for flag in ("fresh", "force", "survey"):
                if a.get(flag):
                    argv.append("--" + flag)
            if a.get("stable"):
                argv += ["--stable", str(int(a["stable"]))]
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                   "PYTHONIOENCODING": "utf-8"}
            log = open(folder / "last-run.log", "w", encoding="utf-8")
            kwargs: dict[str, Any] = {"creationflags": 0x08000000} if os.name == "nt" else {}
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(harness.resolve().parent), **kwargs)
            _RUNNING[str(folder)] = proc
        return {"ok": True, "started": True, "pid": proc.pid, "state_dir": str(folder), "log": str(folder / "last-run.log"),
                "note": "poll hub_parity_status; a harness that has not changed since its last pass is skipped unless force is set"}

    def get(self, req: Request) -> Optional[Any]:
        if req.path == "/api/parity/status":
            return self.status({"state_dir": req.q("state_dir")})
        return None

    def post(self, req: Request) -> Optional[Any]:
        if req.path == "/api/parity/compare":
            return self.compare(req.body)
        if req.path == "/api/parity/run":
            return self.run(req.body)
        return None

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": "hub_parity_compare",
                "description": "Compare two trace files exactly and report the first divergence. Keywords: paridad, diferencial, parity.\n"
                               "Returns match, or the first differing case with event, path, expected, actual and its input; "
                               "`survey` counts every divergent case by category. An empty trace is an error, never a match.",
                "inputSchema": {"type": "object", "properties": {
                    "reference": {"type": "string", "description": "Trace file of the oracle (JSONL)."},
                    "candidate": {"type": "string", "description": "Trace file of the probe (JSONL)."},
                    "inputs": {"type": "string", "description": "The corpus file, to include the input of the differing case."},
                    "survey": {"type": "boolean", "default": False}},
                    "required": ["reference", "candidate"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "hub_parity_status",
                "description": "Last result of every differential harness in a state folder. Keywords: paridad, parity, informe, report.\n"
                               "Shows pass/fail/error, stability, scope (what is not covered), the first divergence and the survey counts.",
                "inputSchema": {"type": "object", "properties": {
                    "state_dir": {"type": "string", "description": "Folder with state.json and one folder per harness (default .parity)."}},
                    "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "hub_parity_run",
                "description": "Run a differential harness file in the background. Keywords: paridad, ejecutar, parity run.\n"
                               "Starts `python -m hoard_link.parity run`; poll hub_parity_status. Skips when nothing it depends on changed.",
                "inputSchema": {"type": "object", "properties": {
                    "harness": {"type": "string", "description": "Path of the harness .py file."},
                    "state_dir": {"type": "string"}, "fresh": {"type": "boolean"}, "force": {"type": "boolean"},
                    "survey": {"type": "boolean"}, "stable": {"type": "integer"}},
                    "required": ["harness"], "additionalProperties": False},
            },
        ]

    def handlers(self) -> dict[str, Any]:
        return {"hub_parity_compare": self.compare, "hub_parity_status": self.status, "hub_parity_run": self.run}


__all__ = ["ParityFacet"]
