"""Executors: the command-line agents installed on this PC, run on behalf of a Faustus that lives elsewhere.

A Faustus on a Spark has no ``claude`` or ``codex`` of its own: those programs, and the sign-in they keep, are on the
person's PC. Faustus already reaches this Hub through the link tunnel, so the Hub is the one place that can answer

* which of the executables Faustus names are installed here (``GET /api/executors?names=claude,codex``), with the host so a
  list can say where each one lives;
* start one task (``POST /api/executors/runs``): Faustus sends the command line it built from its own table of runners,
  the task on stdin when the runner reads it there, a folder and the extra environment. The Hub resolves the program on
  this PC's PATH, runs it detached from the request with its output going to a log file, and answers with a run id;
* follow it (``GET /api/executors/runs/<id>?since=<offset>``): status, exit code and the output from ``since``, so a long
  run is read in pieces;
* stop it (``POST /api/executors/runs/<id>/cancel``), killing the whole process tree.

Runs without a folder get one of their own under ``<data>/executors/work/<id>``. Nothing here knows how a given agent is
invoked: that stays in Faustus, so adding a runner there needs nothing here. The same four operations are tools for agents.
Only the Hub operator (its page or its bearer token) may use them.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from typing import Any, Optional

from .facets import Facet, Request

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MAX_RUNNING = 8
MAX_KEPT = 60
DEFAULT_TIMEOUT_S = 1800.0
READ_LIMIT = 200_000


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def kill_tree(proc: subprocess.Popen) -> None:
    """End the process and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=_flags())
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - already gone
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


class ExecutorsFacet(Facet):
    id = "executors"

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers
    def _root(self) -> str:
        root = os.path.join(self.hub.config.data_dir, "executors")
        os.makedirs(os.path.join(root, "work"), exist_ok=True)
        return root

    @staticmethod
    def _exe(name: str) -> Optional[str]:
        return shutil.which(name) if NAME_RE.match(name or "") else None

    def installed(self, names: list[str]) -> dict[str, Any]:
        """Which of ``names`` are installed on this PC."""
        found = {}
        for name in dict.fromkeys(str(n).strip() for n in names if str(n).strip()):
            path = self._exe(name)
            found[name] = {"installed": bool(path), "path": path or ""}
        return {"ok": True, "host": socket.gethostname(), "executors": found}

    # ------------------------------------------------------------------ runs
    def start_run(self, body: dict[str, Any]) -> dict[str, Any]:
        argv = body.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return {"ok": False, "status": 400, "error": "argv must be a non-empty list of strings"}
        exe = self._exe(argv[0])
        if not exe:
            return {"ok": False, "status": 404, "error": f"{argv[0]} is not installed on this PC"}
        run_id = uuid.uuid4().hex[:12]
        root = self._root()
        cwd = str(body.get("cwd") or "").strip()
        if cwd and not os.path.isdir(cwd):
            return {"ok": False, "status": 400, "error": f"the folder does not exist on this PC: {cwd}"}
        if not cwd:
            cwd = os.path.join(root, "work", run_id)
            os.makedirs(cwd, exist_ok=True)
        env = dict(os.environ)
        for key, value in (body.get("env") or {}).items():
            if isinstance(key, str) and isinstance(value, str) and key:
                env[key] = value
        stdin = body.get("stdin")
        timeout = max(5.0, float(body.get("timeout_s") or DEFAULT_TIMEOUT_S))
        with self._lock:
            if sum(1 for r in self._runs.values() if r["status"] == "running") >= MAX_RUNNING:
                return {"ok": False, "status": 429, "error": f"{MAX_RUNNING} runs are already going on this PC"}
            log = os.path.join(root, f"{run_id}.log")
            kwargs: dict[str, Any] = {"cwd": cwd, "env": env, "stdout": open(log, "wb"), "stderr": subprocess.STDOUT,
                                      "stdin": subprocess.PIPE if stdin is not None else subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = _flags()
            else:
                kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen([exe] + argv[1:], **kwargs)  # noqa: S603 - the program is the one found on PATH
            except OSError as exc:
                kwargs["stdout"].close()
                return {"ok": False, "status": 500, "error": f"could not start {argv[0]}: {exc}"[:300]}
            kwargs["stdout"].close()
            run = {"id": run_id, "label": str(body.get("label") or argv[0])[:80], "program": os.path.basename(exe),
                   "cwd": cwd, "status": "running", "exit_code": None, "started": time.time(), "finished": None,
                   "log": log, "proc": proc, "cancel": False, "timeout_s": timeout}
            self._runs[run_id] = run
            for old in sorted((r for r in self._runs.values() if r["status"] != "running"), key=lambda r: r["started"])[:max(0, len(self._runs) - MAX_KEPT)]:
                self._runs.pop(old["id"], None)
        threading.Thread(target=self._supervise, args=(run, None if stdin is None else str(stdin)), daemon=True,
                         name=f"executor-{run_id}").start()
        return {"ok": True, "id": run_id, "host": socket.gethostname(), "program": run["program"], "cwd": cwd}

    def _supervise(self, run: dict[str, Any], stdin: Optional[str]) -> None:
        proc: subprocess.Popen = run["proc"]
        if stdin is not None:
            def feed() -> None:
                try:
                    proc.stdin.write(stdin.encode("utf-8") + b"\n")  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001 - the program may not read it
                    pass
                finally:
                    try:
                        proc.stdin.close()  # type: ignore[union-attr]
                    except Exception:  # noqa: BLE001
                        pass
            threading.Thread(target=feed, daemon=True).start()
        deadline = time.monotonic() + run["timeout_s"]
        status = None
        while proc.poll() is None:
            if run["cancel"]:
                status = "cancelled"
            elif time.monotonic() >= deadline:
                status = "timeout"
            if status:
                kill_tree(proc)
                break
            time.sleep(0.2)
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        run["exit_code"] = proc.returncode
        run["finished"] = time.time()
        run["status"] = status or ("done" if proc.returncode == 0 else "error")

    def _run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._runs.get(str(run_id))

    def read_run(self, run_id: str, since: int = 0) -> dict[str, Any]:
        run = self._run(run_id)
        if run is None:
            return {"ok": False, "status": 404, "error": f"no run {run_id}"}
        try:
            with open(run["log"], "rb") as fh:
                data = fh.read()
        except OSError:
            data = b""
        since = max(0, min(int(since or 0), len(data)))
        chunk = data[since:since + READ_LIMIT]
        end = since + len(chunk)
        return {"ok": True, "id": run["id"], "label": run["label"], "status": run["status"], "exit_code": run["exit_code"],
                "seconds": round((run["finished"] or time.time()) - run["started"], 2), "cwd": run["cwd"],
                "output": chunk.decode("utf-8", "replace"), "offset": end, "more": end < len(data)}

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        if run is None:
            return {"ok": False, "status": 404, "error": f"no run {run_id}"}
        run["cancel"] = True
        return {"ok": True, "id": run["id"], "status": run["status"]}

    def runs(self) -> dict[str, Any]:
        with self._lock:
            rows = [{"id": r["id"], "label": r["label"], "status": r["status"], "exit_code": r["exit_code"],
                     "started": r["started"], "finished": r["finished"]} for r in self._runs.values()]
        return {"ok": True, "host": socket.gethostname(), "runs": sorted(rows, key=lambda r: -r["started"])}

    def close(self) -> None:
        with self._lock:
            live = [r for r in self._runs.values() if r["status"] == "running"]
        for run in live:
            run["cancel"] = True

    # ------------------------------------------------------------------ http
    @staticmethod
    def _operator(req: Request) -> bool:
        return req.caller() in ("hub", "ui") or req.agent()

    def get(self, req: Request) -> Any:
        if req.path != "/api/executors" and not req.path.startswith("/api/executors/"):
            return None
        if not self._operator(req):
            return {"ok": False, "status": 403, "error": "only the Hub operator may use the executors"}
        if req.path == "/api/executors":
            names = [n for n in str(req.q("names", "")).split(",") if n.strip()]
            return self.installed(names)
        if req.path == "/api/executors/runs":
            return self.runs()
        if req.path.startswith("/api/executors/runs/"):
            return self.read_run(req.path.rsplit("/", 1)[-1], req.q_int("since", 0))
        return None

    def post(self, req: Request) -> Any:
        if not req.path.startswith("/api/executors/"):
            return None
        if not self._operator(req):
            return {"ok": False, "status": 403, "error": "only the Hub operator may use the executors"}
        if req.path == "/api/executors/runs":
            return self.start_run(req.body)
        parts = req.path.strip("/").split("/")
        if len(parts) == 5 and parts[2] == "runs" and parts[4] == "cancel":
            return self.cancel_run(parts[3])
        return None

    # ------------------------------------------------------------------ tools
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        obj = {"type": "object", "additionalProperties": False}
        return [
            {"name": "hub_executors", "description": "Which command-line agents (by executable name) are installed on this PC, "
                                                      "and the host they live on. Pass names, or leave it empty for the usual ones.",
             "inputSchema": {**obj, "properties": {"names": {"type": "array", "items": {"type": "string"}}}},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_executor_run", "description": "Start one command on this PC: argv (program first, found on the PC's PATH), "
                                                         "optional stdin text, folder, extra env and a timeout in seconds. Returns a run id.",
             "inputSchema": {**obj, "properties": {"argv": {"type": "array", "items": {"type": "string"}}, "stdin": {"type": "string"},
                                                    "cwd": {"type": "string"}, "env": {"type": "object"}, "timeout_s": {"type": "number"},
                                                    "label": {"type": "string"}}, "required": ["argv"]}},
            {"name": "hub_executor_status", "description": "Status, exit code and output of a run started with hub_executor_run; "
                                                            "pass since (the offset of the previous answer) to read only what is new. "
                                                            "Without a run id it lists the recent runs.",
             "inputSchema": {**obj, "properties": {"run_id": {"type": "string"}, "since": {"type": "integer", "default": 0}}},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_executor_cancel", "description": "Stop a run and its whole process tree.",
             "inputSchema": {**obj, "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        ]

    def handlers(self) -> dict[str, Any]:
        usual = ["claude", "codex", "opencode", "gemini", "cursor-agent"]
        return {
            "hub_executors": lambda a: self.installed(list(a.get("names") or usual)),
            "hub_executor_run": lambda a: self.start_run(a),
            "hub_executor_status": lambda a: self.read_run(str(a["run_id"]), int(a.get("since") or 0)) if a.get("run_id") else self.runs(),
            "hub_executor_cancel": lambda a: self.cancel_run(str(a.get("run_id") or "")),
        }
