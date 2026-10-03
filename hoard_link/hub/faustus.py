"""Control the discovered Faustus checkout through its ownership-aware runtime."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from urllib.parse import urlsplit

from ..fam_mail import faustus_dir, faustus_python
from . import procs


class FaustusRuntime:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._starting = None
        self._cancelled = threading.Event()

    def prepare_start(self):
        self._cancelled.clear()

    def _execute(self, argv, *, cwd, creationflags):
        options = dict(cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, creationflags=creationflags)
        with self._lock:
            if self._cancelled.is_set():
                raise subprocess.SubprocessError("Faustus start was cancelled")
            process = subprocess.Popen(argv, **options)
            self._starting = process
        try:
            stdout, stderr = process.communicate(timeout=180)
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            self.cancel_start()
            process.communicate(timeout=10)
            raise
        finally:
            with self._lock:
                if self._starting is process:
                    self._starting = None

    def cancel_start(self):
        """Release the starter's launch lock; the subsequent stop uses Faustus's own ownership checks."""
        with self._lock:
            self._cancelled.set()
            process = self._starting
        if process is None or process.poll() is not None:
            return
        ps = procs._psutil()
        if ps is not None:
            try:
                root = ps.Process(process.pid)
                for child in root.children(recursive=True):
                    argv = child.cmdline()
                    # Windows venv Python forwards to a second Python. Stop only
                    # that CLI starter, not the server's detached launch profiles.
                    if "start" in argv and any(str(arg).endswith("server_runtime.py") for arg in argv):
                        child.kill()
            except ps.Error:
                pass
        try:
            process.kill()
        except OSError:
            pass  # it exited between poll() and the cancellation

    def installation(self):
        root = faustus_dir(self.config.faustus_dir, ask_hub=False)
        python = faustus_python(root, self.config.faustus_python)
        return (root, python) if root and python and (root / "server_runtime.py").is_file() else (None, None)

    def available(self):
        return self.installation()[0] is not None

    def run(self, action):
        root, python = self.installation()
        if root is None:
            return {"ok": False, "error": "Faustus runtime is not installed; configure faustus_dir in hub.json"}
        argv = [python, str(root / "server_runtime.py"), action]
        if action == "start":
            port = 7000
            for url in self.config.faustus_urls:
                parsed = urlsplit(url)
                if parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port and 1024 <= parsed.port <= 65535:
                    port = parsed.port
                    break
            argv += ["--port", str(port), "--owner", "web"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            if action == "start":
                result = self._execute(argv, cwd=root, creationflags=flags)
            else:
                result = subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, timeout=180, creationflags=flags)
            raw = json.loads(result.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            if action == "start" and self._cancelled.is_set():
                return {"ok": False, "cancelled": True, "error": "Faustus start cancelled"}
            return {"ok": False, "error": f"Faustus {action} failed: {exc}"}
        if not isinstance(raw, dict):
            return {"ok": False, "error": "Faustus returned an invalid runtime result"}
        # The start result contains the ownership token. Never send it to the UI or event bus.
        out = {k: raw[k] for k in ("started", "stopped", "healthy", "port", "owner", "pids", "remaining", "error") if k in raw}
        out["ok"] = result.returncode == 0 and not raw.get("error") and not raw.get("remaining")
        if not out["ok"]:
            out.setdefault("error", "Faustus could not stop every process" if raw.get("remaining") else "Faustus runtime failed")
        # This checkout's optional model stopper removes its watchdog before the model process.
        models = root / "Stop-Local-Models.ps1"
        if action == "stop-all" and out["ok"] and os.name == "nt" and models.is_file():
            try:
                stopped = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(models)],
                                         cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                         timeout=90, creationflags=flags)
                if stopped.returncode:
                    out.update(ok=False, error="Faustus stopped, but its local model stop command failed",
                               model_error=(stopped.stderr or stopped.stdout).strip()[-500:])
            except (OSError, subprocess.SubprocessError) as exc:
                out.update(ok=False, error=f"Faustus stopped, but its local models could not stop: {exc}")
        return out
