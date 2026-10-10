"""Read Codex account rate limits through the installed Codex app-server.

This module never reads Codex auth files or tokens. It asks the official CLI's
app-server for the current account and rate limits, hashes the account identity
in memory into an opaque shared-pool key, and reports only sanitized windows
through the normal Agora CLI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

LOGGER = logging.getLogger(__name__)
SOURCE = "codex-app-server:account/rateLimits/read"
_CLIENT_INFO = {
    "name": "hoardlink_agora_capacity",
    "title": "HoardLink Agora Capacity Reader",
    "version": "0.1.0",
}
_RPC_TIMEOUT_S = 12.0
_PROCESS_CLOSE_TIMEOUT_S = 2.0
_REPORTER_LOCK = threading.Lock()
_REPORTER_THREAD: Optional[threading.Thread] = None
_REPORTER_STOP: Optional[threading.Event] = None


class CodexCapacityError(RuntimeError):
    """A sanitized, non-sensitive failure code from quota collection."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc_iso(timestamp: Any) -> Optional[str]:
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _limit_id(value: Any) -> str:
    raw = str(value or "codex").strip()
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-._")
    return (safe or "codex")[:48]


def _pool_id(account: Mapping[str, Any]) -> str:
    """Hash the account/read identity in memory; never return or store it raw."""
    identity = next((account.get(key) for key in
                     ("chatgptAccountId", "accountId", "id", "email")
                     if isinstance(account.get(key), str) and account.get(key).strip()), None)
    if identity is None:
        raise CodexCapacityError("account_identity_unavailable")
    material = f"hoardlink:codex-account-pool:v1:{identity.strip().casefold()}".encode("utf-8")
    return "codex-" + hashlib.sha256(material).hexdigest()[:40]


def build_usage_report(account: Mapping[str, Any], rate_limits: Mapping[str, Any], *,
                       now: Optional[float] = None) -> dict[str, Any]:
    """Convert account/rateLimits responses into Agora's sanitized report DTO."""
    if not isinstance(account, Mapping):
        raise CodexCapacityError("account_unavailable")
    pool_id = _pool_id(account)
    buckets = rate_limits.get("rateLimitsByLimitId")
    if not isinstance(buckets, Mapping) or not buckets:
        legacy = rate_limits.get("rateLimits")
        if isinstance(legacy, Mapping):
            fallback_id = legacy.get("limitId") or "codex"
            buckets = {_limit_id(fallback_id): legacy}
        else:
            raise CodexCapacityError("rate_limits_unavailable")

    observed_at = time.time() if now is None else float(now)
    windows: list[dict[str, Any]] = []
    for bucket_key, bucket in sorted(buckets.items(), key=lambda pair: str(pair[0])):
        if not isinstance(bucket, Mapping):
            continue
        bucket_id = _limit_id(bucket.get("limitId") or bucket_key)
        for period in ("primary", "secondary"):
            window = bucket.get(period)
            used = window.get("usedPercent") if isinstance(window, Mapping) else None
            reset_unix = window.get("resetsAt") if isinstance(window, Mapping) else None
            reset_at = _utc_iso(reset_unix)
            valid_used = (isinstance(used, (int, float)) and not isinstance(used, bool)
                          and 0 <= float(used) <= 100)
            expired = (isinstance(reset_unix, (int, float)) and not isinstance(reset_unix, bool)
                       and float(reset_unix) <= observed_at)
            if valid_used and not expired:
                measurement = "measured"
                used_percent = float(used)
                remaining_percent = 100.0 - used_percent
            else:
                measurement = "unknown"
                used_percent = None
                remaining_percent = None
            duration = window.get("windowDurationMins") if isinstance(window, Mapping) else None
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
                duration = None
            suffix = f"{bucket_id}.{period}"
            windows.append({
                "window_id": suffix,
                "label": f"{bucket_id} {period}",
                "measurement": measurement,
                "used_percent": used_percent,
                "remaining_percent": remaining_percent,
                "reset_at": reset_at if not expired else None,
                "source": SOURCE,
                "window_duration_mins": duration,
            })
    if not windows:
        raise CodexCapacityError("rate_limits_empty")
    return {
        "provider_id": "codex",
        "pool_id": pool_id,
        "pool_label": "Codex account",
        "source": SOURCE,
        "windows": windows,
    }


def _read_json_rpc(process: Any, output_queue: "queue.Queue[str]", request_id: int,
                   timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexCapacityError("app_server_timeout")
        try:
            line = output_queue.get(timeout=remaining)
        except queue.Empty:
            raise CodexCapacityError("app_server_timeout") from None
        try:
            message = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            raise CodexCapacityError("app_server_rpc_error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexCapacityError("app_server_invalid_response")
        return result


def _read_codex_account_and_limits(*, executable: Optional[str] = None,
                                   timeout: float = _RPC_TIMEOUT_S,
                                   popen: Callable[..., Any] = subprocess.Popen
                                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read account identity and rate limits using only app-server read methods."""
    command = executable or shutil.which("codex")
    if not command:
        raise CodexCapacityError("codex_cli_unavailable")
    try:
        process = popen([command, "app-server", "--stdio"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        encoding="utf-8", errors="replace", bufsize=1)
    except OSError:
        raise CodexCapacityError("app_server_start_failed") from None

    output: "queue.Queue[str]" = queue.Queue()

    def collect_stdout() -> None:
        try:
            for line in process.stdout:
                output.put(line)
        except Exception:
            return

    reader = threading.Thread(target=collect_stdout, name="codex-capacity-app-server-reader", daemon=True)
    reader.start()

    def send(message: dict[str, Any]) -> None:
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, AttributeError):
            raise CodexCapacityError("app_server_pipe_closed") from None

    try:
        send({"method": "initialize", "id": 0, "params": {"clientInfo": dict(_CLIENT_INFO)}})
        _read_json_rpc(process, output, 0, timeout)
        send({"method": "initialized", "params": {}})
        send({"method": "account/read", "id": 1, "params": {"refreshToken": False}})
        account_result = _read_json_rpc(process, output, 1, timeout)
        account = account_result.get("account")
        if not isinstance(account, dict):
            raise CodexCapacityError("account_unavailable")
        send({"method": "account/rateLimits/read", "id": 2})
        limits_result = _read_json_rpc(process, output, 2, timeout)
        return account, limits_result
    finally:
        try:
            process.stdin.close()
        except (OSError, AttributeError):
            pass
        try:
            process.wait(timeout=_PROCESS_CLOSE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=_PROCESS_CLOSE_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


def collect_codex_usage_report(*, executable: Optional[str] = None,
                               now: Optional[float] = None,
                               timeout: float = _RPC_TIMEOUT_S,
                               popen: Callable[..., Any] = subprocess.Popen) -> dict[str, Any]:
    """Return only the sanitized DTO; raw account/read identity stays local."""
    account, limits = _read_codex_account_and_limits(executable=executable, timeout=timeout, popen=popen)
    return build_usage_report(account, limits, now=now)


def _report_via_agora_cli(report: Mapping[str, Any], *, repo_root: Optional[Path] = None,
                          env: Optional[Mapping[str, str]] = None,
                          runner: Callable[..., Any] = subprocess.run,
                          timeout: float = 15.0) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[2]
    cli = root / "scripts" / "agora.py"
    if not cli.is_file():
        return {"ok": False, "reason": "agora_cli_unavailable"}
    command = [sys.executable, str(cli), "--json", "--as", "codex-capacity",
               "usage-report", "--data-file", "-"]
    child_env = dict(os.environ if env is None else env)
    try:
        completed = runner(command, input=json.dumps(report, separators=(",", ":")),
                           text=True, encoding="utf-8", errors="replace", capture_output=True,
                           timeout=timeout, cwd=str(root), env=child_env, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "agora_cli_timeout"}
    except OSError:
        return {"ok": False, "reason": "agora_cli_start_failed"}
    try:
        result = json.loads(completed.stdout or "{}")
    except (TypeError, ValueError):
        return {"ok": False, "reason": "agora_cli_invalid_response"}
    if not isinstance(result, dict) or not result.get("ok") or completed.returncode:
        return {"ok": False, "reason": "agora_report_rejected"}
    # Never return the opaque pool key or any CLI response payload to logs/callers.
    return {"ok": True, "provider_id": "codex", "window_count": len(report["windows"]),
            "reported_at": result.get("reported_at")}


def collect_and_report_codex_usage(*, executable: Optional[str] = None,
                                   now: Optional[float] = None,
                                   reporter: Callable[..., dict[str, Any]] = _report_via_agora_cli,
                                   reporter_kwargs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Collect and publish one snapshot; returned status contains no account identity."""
    try:
        report = collect_codex_usage_report(executable=executable, now=now)
    except CodexCapacityError as exc:
        return {"ok": False, "provider_id": "codex", "reason": exc.code}
    except Exception:
        LOGGER.debug("Codex quota collection failed", exc_info=False)
        return {"ok": False, "provider_id": "codex", "reason": "collection_failed"}
    try:
        return reporter(report, **(reporter_kwargs or {}))
    except Exception:
        LOGGER.debug("Codex quota report failed", exc_info=False)
        return {"ok": False, "provider_id": "codex", "reason": "report_failed"}


def start_codex_capacity_reporter(*, interval_seconds: int = 90) -> threading.Thread:
    """Start one daemon poller; the default cadence is 90 seconds."""
    global _REPORTER_THREAD, _REPORTER_STOP
    if isinstance(interval_seconds, bool) or not 60 <= int(interval_seconds) <= 120:
        raise ValueError("interval_seconds must be between 60 and 120")
    with _REPORTER_LOCK:
        if _REPORTER_THREAD is not None and _REPORTER_THREAD.is_alive():
            return _REPORTER_THREAD
        stop = threading.Event()

        def run() -> None:
            while not stop.is_set():
                result = collect_and_report_codex_usage()
                if not result.get("ok"):
                    LOGGER.info("Codex quota observation unavailable (%s)", result.get("reason", "unknown"))
                stop.wait(interval_seconds)

        _REPORTER_STOP = stop
        _REPORTER_THREAD = threading.Thread(target=run, name="agora-codex-capacity", daemon=True)
        _REPORTER_THREAD.start()
        return _REPORTER_THREAD


def stop_codex_capacity_reporter(timeout: float = 1.0) -> None:
    """Signal the optional daemon poller to stop; does not affect Codex itself."""
    global _REPORTER_THREAD, _REPORTER_STOP
    with _REPORTER_LOCK:
        thread, stop = _REPORTER_THREAD, _REPORTER_STOP
        if stop is not None:
            stop.set()
    if thread is not None:
        thread.join(timeout=max(0.0, float(timeout)))
    with _REPORTER_LOCK:
        if _REPORTER_THREAD is thread and (thread is None or not thread.is_alive()):
            _REPORTER_THREAD = None
            _REPORTER_STOP = None
