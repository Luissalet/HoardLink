"""Identify console worker processes independently of their turn's receipt."""

import os
import re
import time
from pathlib import Path

from . import procs


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


def observe(row, directory):
    """Return a live identity only when it can be tied to this Hub's receipt."""
    paths = _paths(row, directory)
    ps = procs._psutil()
    if ps is None or not row["pid"] or paths is None:
        return None
    try:
        process = ps.Process(row["pid"])
        started = process.create_time()
        if row["process_started_at"] is not None:
            if started != row["process_started_at"]:
                return None
        elif (not row["created_at"] - 1 <= started <= row["updated_at"]
              or not _legacy_match(row, process.cmdline(), paths[1])):
            return None
        if process.status() == ps.STATUS_ZOMBIE or not process.is_running():
            return None
    except (ps.Error, OSError):
        return None
    last_activity = None
    for path in paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified >= started:
            last_activity = max(last_activity or modified, modified)
    return {"pid": process.pid, "started_at": started, "age_seconds": max(0, int(time.time() - started)),
            "last_activity_at": last_activity}
