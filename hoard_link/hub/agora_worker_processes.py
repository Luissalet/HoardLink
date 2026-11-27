"""Identify console worker processes independently of their turn's receipt."""

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import procs


def creation_flags() -> int:
    """Start background worker trees at BelowNormal on Windows."""
    return procs.BELOW_NORMAL_PRIORITY_CLASS if procs.IS_WINDOWS else 0


def restore_priority(pid: int, created_at: float) -> None:
    procs.set_tree_priority(pid, created_at, procs.BELOW_NORMAL_PRIORITY_CLASS)


def live_pids():
    ps = procs._psutil()
    if ps is not None:
        try:
            return set(ps.pids())
        except (ps.Error, OSError):
            pass
    return set()


def started_at(pid):
    ps = procs._psutil()
    if ps is not None:
        try:
            return ps.Process(pid).create_time()
        except (ps.Error, OSError):
            pass
    return None


def _paths(row, directory):
    log = Path(row["log_path"])
    stem = log.stem
    worker_id = row["id"]
    if (not row["log_path"] or log.suffix != ".jsonl" or log.parent != directory
            or (stem != worker_id and not re.fullmatch(re.escape(worker_id) + r"-resume-\d+", stem))):
        return None
    result = directory / (worker_id + ".result.txt") if stem == worker_id else log.with_suffix(".txt")
    return log, result


def _option(args, name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return ""


def _legacy_match(row, args, result):
    if not args:
        return False
    executable = Path(args[0]).stem.lower()
    provider = row["parent_id"]
    if provider == "codex":
        return (executable == "codex" and args[1:2] == ["exec"]
                and os.path.normcase(_option(args, "--output-last-message")) == os.path.normcase(str(result)))
    session = row["session_id"]
    if provider == "claude":
        return (executable == "claude" and "--print" in args and bool(session)
                and session in (_option(args, "--session-id"), _option(args, "--resume")))
    if provider == "cursor":
        worktree = "agora-cursor-" + row["id"][:32]
        return (executable == "node" and len(args) > 1 and Path(args[1]).name == "index.js"
                and "--print" in args and (f"--worktree={worktree}" in args
                                            or (session and _option(args, "--resume") == session)))
    if provider == "faustus":
        return (bool(session) and _option(args, "--session") == session
                and any(Path(arg).name == "faustus_run.py" for arg in args[:5]))
    return False


def _command_matches(saved_command, actual, *, prompt_redacted):
    try:
        expected = json.loads(saved_command)
    except ValueError:
        return False
    if (not isinstance(expected, list) or not expected or len(expected) != len(actual)
            or not all(isinstance(arg, str) for arg in expected)):
        return False
    if os.path.normcase(os.path.normpath(expected[0])) != os.path.normcase(os.path.normpath(actual[0])):
        return False
    # The native runner saves a continuation recipe with the prompt removed.
    if prompt_redacted and "-p" in expected:
        index = expected.index("-p") + 1
        if index < len(expected) and not expected[index]:
            actual = [*actual[:index], "", *actual[index + 1:]]
    return expected[1:] == actual[1:]


@dataclass
class AdoptedWorker:
    process: Any
    started_at: float
    paths: tuple[Path, Path]

    @property
    def pid(self):
        return self.process.pid

    def wait(self, hub_stopping):
        ps = procs._psutil()
        while not hub_stopping.is_set():
            try:
                if not self.process.is_running():
                    break
                return self.process.wait(timeout=1)
            except ps.TimeoutExpired:
                try:
                    if self.process.status() == ps.STATUS_ZOMBIE:
                        return None
                except ps.NoSuchProcess:
                    break
                except ps.AccessDenied:
                    pass
            except ps.NoSuchProcess:
                break
            except ps.AccessDenied:
                hub_stopping.wait(1)
        return None

    def terminate(self):
        ps = procs._psutil()
        try:
            # psutil checks the original PID and creation time before signalling.
            self.process.terminate()
        except ps.NoSuchProcess:
            pass
        except ps.AccessDenied as exc:
            raise PermissionError("Could not stop the verified worker process.") from exc


def identify(row, directory):
    """Return a verified live process, or a reason a live PID could not be verified."""
    pid = row["pid"]
    if not pid:
        return None, ""
    ps = procs._psutil()
    if ps is None:
        return None, "process_unavailable"
    try:
        process = ps.Process(pid)
        if process.status() == ps.STATUS_ZOMBIE or not process.is_running():
            return None, ""
        started = process.create_time()
        paths = _paths(row, directory)
        if paths is None:
            return None, "invalid_receipt"
        saved_command = row["launch_command"] if "launch_command" in row.keys() else ""
        if row["process_started_at"] is not None:
            if started != row["process_started_at"]:
                return None, "pid_reused"
        elif not row["created_at"] - 1 <= started <= row["updated_at"]:
            return None, "pid_reused"
        if saved_command:
            if not _command_matches(saved_command, process.cmdline(), prompt_redacted=row["parent_id"] == "faustus"):
                return None, "command_mismatch"
        elif row["process_started_at"] is None and not _legacy_match(row, process.cmdline(), paths[1]):
            return None, "legacy_identity_unverified"
        if not process.is_running():
            return None, ""
    except ps.NoSuchProcess:
        return None, ""
    except (ps.Error, OSError):
        return None, "process_unavailable"
    return AdoptedWorker(process, started, paths), ""


def observe(row, directory):
    """Return a live identity only when it can be tied to this Hub's receipt."""
    worker, _ = identify(row, directory)
    if worker is None:
        return None
    last_activity = None
    for path in worker.paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified >= worker.started_at:
            last_activity = max(last_activity or modified, modified)
    return {"pid": worker.pid, "started_at": worker.started_at, "age_seconds": max(0, int(time.time() - worker.started_at)),
            "last_activity_at": last_activity}
