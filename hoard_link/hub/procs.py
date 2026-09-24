"""What is running, and starting or stopping it.

Three questions per app, answered separately so a card can show exactly
what is known:

* **health** — ``GET <url><health.path>`` answered, and did it say the
  ``service`` the manifest expects? ``healthy`` when it did, ``foreign``
  when something else answers on that port (the hub will never stop that
  process: it is not the app), ``down`` when nothing answers.
* **process** — which pid listens on the port (psutil, or ``netstat`` /
  ``ss`` when psutil is missing), with name, memory, uptime and command
  line when psutil is there.
* **actions** — ``start`` spawns the manifest's launch hint detached from
  the hub (closing the hub does not close the apps; their output goes to
  ``<data>/logs/<id>.log``) and waits for readiness; ``stop`` terminates
  the listener's process tree, and only when the health check says the
  listener really is that app. Every stop refuses this process and its
  ancestors, whatever the caller says.

Nothing here raises to the caller: results are dicts with ``ok`` and a
human-readable ``error``/``detail``, because the callers are a JSON API
and an MCP tool.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from .registry import App

HEALTH_TIMEOUT_S = 1.5
STOP_GRACE_S = 4.0


def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

@dataclass
class Health:
    state: str                       # healthy | foreign | down
    status: Optional[int] = None
    service: Optional[str] = None
    detail: str = ""
    body: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "status": self.status, "service": self.service, "detail": self.detail}


def fetch_json(url: str, timeout: float = HEALTH_TIMEOUT_S) -> tuple[Optional[int], Any]:
    """``(status, json-or-None)``; ``(None, None)`` when nothing answered.
    Ignores proxies: this is loopback, and a system proxy would otherwise
    swallow every probe."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hoard-hub"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception:  # noqa: BLE001
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, None


def fetch_json_post(url: str, body: Any, timeout: float = 120.0) -> tuple[Optional[int], Any]:
    """POST JSON to a loopback URL; same contract as :func:`fetch_json`."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "hoard-hub"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except Exception:  # noqa: BLE001
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, None


def health(app: App, listeners: Optional[dict[int, int]] = None) -> Health:
    """``listeners`` (from :func:`listening_pids`) short-circuits the HTTP
    probe when nothing listens on the port at all: on Windows a connection
    to a closed loopback port can take the whole timeout to fail."""
    if listeners is not None and app.port is not None and app.port not in listeners:
        return Health("down", detail=f"nothing listens on port {app.port}")
    status, body = fetch_json(app.health_url())
    if status is None:
        return Health("down", detail="no answer on " + app.url)
    service = None
    if isinstance(body, dict):
        service = body.get("service") or body.get("app") or body.get("name")
    if app.expect_service:
        if isinstance(service, str) and service.lower() == app.expect_service.lower():
            return Health("healthy", status, service, "", body)
        if status < 500 and service is None and body is None:
            # Answers, but not with JSON — some other server on the port.
            return Health("foreign", status, None, f"port {app.port} answers but not as {app.expect_service}", body)
        return Health("foreign", status, service if isinstance(service, str) else None,
                      f"port {app.port} answers as {service or 'unknown'}, expected {app.expect_service}", body)
    return Health("healthy" if status < 500 else "down", status, service if isinstance(service, str) else None, "", body)


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------

@dataclass
class ProcInfo:
    pid: int
    name: str = ""
    exe: str = ""
    cmdline: str = ""
    cwd: str = ""
    rss_mb: Optional[float] = None
    created_at: Optional[float] = None
    uptime_s: Optional[int] = None
    children: int = 0
    ports: list[int] = field(default_factory=list)
    protected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid, "name": self.name, "exe": self.exe, "cmdline": self.cmdline, "cwd": self.cwd,
            "rss_mb": self.rss_mb, "created_at": self.created_at, "uptime_s": self.uptime_s,
            "children": self.children, "ports": list(self.ports), "protected": self.protected,
        }


_PROTECTED: Optional[set[int]] = None
_PROTECTED_LOCK = threading.Lock()


def _protected_pids() -> set[int]:
    """This process and its ancestors — fixed for our lifetime, so computed
    once (walking parents costs ~0.2s on Windows), under a lock so parallel
    callers do not all pay for it."""
    global _PROTECTED
    if _PROTECTED is not None:
        return _PROTECTED
    with _PROTECTED_LOCK:
        if _PROTECTED is not None:
            return _PROTECTED
        _PROTECTED = _compute_protected()
        return _PROTECTED


def _compute_protected() -> set[int]:
    pids = {os.getpid()}
    ps = _psutil()
    if ps is not None:
        try:
            for parent in ps.Process(os.getpid()).parents():
                pids.add(parent.pid)
        except Exception:  # noqa: BLE001
            pass
    return pids


def listening_pids() -> dict[int, int]:
    """``{port: pid}`` for every TCP listener on this machine."""
    ps = _psutil()
    out: dict[int, int] = {}
    if ps is not None:
        try:
            for conn in ps.net_connections(kind="tcp"):
                if conn.status == ps.CONN_LISTEN and conn.laddr and conn.pid:
                    out.setdefault(conn.laddr.port, conn.pid)
            return out
        except Exception:  # noqa: BLE001
            pass
    return _listening_pids_cli()


def _listening_pids_cli() -> dict[int, int]:
    out: dict[int, int] = {}
    try:
        if sys.platform.startswith("win"):
            text = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10).stdout
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                    m = re.search(r":(\d+)$", parts[1])
                    if m:
                        out.setdefault(int(m.group(1)), int(parts[4]))
        else:
            text = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=10).stdout
            for line in text.splitlines():
                m = re.search(r":(\d+)\s.*pid=(\d+)", line)
                if m:
                    out.setdefault(int(m.group(1)), int(m.group(2)))
    except Exception:  # noqa: BLE001
        pass
    return out


def proc_info(pid: int, ports: Optional[list[int]] = None) -> ProcInfo:
    info = ProcInfo(pid=pid, ports=list(ports or []), protected=pid in _protected_pids())
    ps = _psutil()
    if ps is None:
        return info
    try:
        p = ps.Process(pid)
        with p.oneshot():
            info.name = p.name()
            try:
                info.exe = p.exe()
            except Exception:  # noqa: BLE001
                pass
            try:
                info.cmdline = " ".join(p.cmdline())
            except Exception:  # noqa: BLE001
                pass
            try:
                info.cwd = p.cwd()
            except Exception:  # noqa: BLE001
                pass
            info.rss_mb = round(p.memory_info().rss / (1024 * 1024), 1)
            info.created_at = p.create_time()
            info.uptime_s = int(time.time() - info.created_at)
            info.children = len(p.children(recursive=True))
    except Exception:  # noqa: BLE001
        pass
    return info


def pid_running(pid: int) -> bool:
    """Is ``pid`` a live (non-zombie) process? Unknown counts as running."""
    ps = _psutil()
    if ps is not None:
        try:
            return ps.Process(pid).status() != getattr(ps, "STATUS_ZOMBIE", "zombie")
        except Exception:  # noqa: BLE001  (NoSuchProcess, AccessDenied)
            return ps.pid_exists(pid)
    if sys.platform.startswith("win"):
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_app_process(app: App, listeners: Optional[dict[int, int]] = None) -> Optional[ProcInfo]:
    port = app.port
    if port is None:
        return None
    listeners = listeners if listeners is not None else listening_pids()
    pid = listeners.get(port)
    if not pid:
        return None
    return proc_info(pid, [port])


def terminate_tree(pid: int, grace_s: float = STOP_GRACE_S, *, created_at: Optional[float] = None) -> dict[str, Any]:
    """Terminate ``pid`` and its descendants. Refuses protected pids and a
    pid whose creation time no longer matches ``created_at`` (recycled)."""
    if pid in _protected_pids():
        return {"ok": False, "error": "refusing to stop the hub itself or one of its ancestors"}
    ps = _psutil()
    if ps is None:
        return {"ok": False, "error": "psutil is not installed: the hub cannot stop processes without it (pip install psutil)"}
    try:
        root = ps.Process(pid)
    except ps.NoSuchProcess:
        return {"ok": True, "stopped": [], "detail": "already gone"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if created_at is not None:
        try:
            if abs(root.create_time() - created_at) > 2.0:
                return {"ok": False, "error": "pid was recycled since it was listed; refresh and try again"}
        except Exception:  # noqa: BLE001
            pass
    family = [root]
    try:
        family += root.children(recursive=True)
    except Exception:  # noqa: BLE001
        pass
    for p in family:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    gone, alive = ps.wait_procs(family, timeout=grace_s)
    for p in alive:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    if alive:
        ps.wait_procs(alive, timeout=2.0)
    return {"ok": True, "stopped": [p.pid for p in family], "killed": [p.pid for p in alive]}


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def _detached_kwargs() -> dict[str, Any]:
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def start_app(app: App, logs_dir: str, *, wait: bool = True) -> dict[str, Any]:
    """Spawn the manifest's launch hint and (optionally) wait for its
    readiness URL. The child is detached and its output appended to
    ``<logs_dir>/<id>.log``."""
    if not app.launchable or app.launch is None:
        return {"ok": False, "error": app.launch_reason or "not launchable"}
    current = health(app)
    if current.state == "healthy":
        return {"ok": True, "already": True, "detail": "already running", "health": current.to_dict()}
    if current.state == "foreign":
        return {"ok": False, "error": current.detail}
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, f"{app.id}.log")
    spec = app.launch
    env = dict(os.environ)
    env.update(spec.env)
    env.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [spec.executable] + list(spec.argv)
    try:
        log = open(log_path, "ab")
        log.write(f"\n--- hoard-hub start {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(cmd)}\n".encode("utf-8"))
        proc = subprocess.Popen(
            cmd, cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            close_fds=True, **_detached_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not start: {exc}", "log": log_path}
    result: dict[str, Any] = {"ok": True, "pid": proc.pid, "log": log_path, "command": cmd}
    if not wait or not spec.readiness_url:
        return result
    deadline = time.time() + max(1.0, spec.readiness_timeout_s)
    while time.time() < deadline:
        if proc.poll() is not None and app.kind != "window-app":
            result.update(ok=False, error=f"process exited with code {proc.returncode} before becoming ready", exit_code=proc.returncode)
            result["log_tail"] = tail(log_path, 30)
            return result
        status, _ = fetch_json(spec.readiness_url, timeout=1.0)
        if status is not None and status < 500:
            result["ready"] = True
            result["health"] = health(app).to_dict()
            return result
        time.sleep(0.5)
    result.update(ready=False, detail=f"not ready after {spec.readiness_timeout_s:.0f}s (still starting?)")
    result["log_tail"] = tail(log_path, 30)
    return result


def tail(path: str, lines: int = 60, width: int = 400) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line[:width] for line in text.splitlines()[-lines:]]
