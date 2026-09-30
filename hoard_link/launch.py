"""Start the shared local backends without Faustus.

Every Hoard app can run on its own. The models live in shared local servers
(ComfyUI for images, video and music; Ollama; a llama.cpp server...). When
Faustus is not there to have started them, an app starts them itself with
this module: it finds the install, starts it detached on loopback, waits
until it answers, and can stop it again.

Machine-wide and shared by the whole family:

* ``~/.hoard/backends.json`` (``HOARD_HOME`` moves ``~/.hoard``): where
  things are installed. Any app's settings can write it::

    {
      "comfyui": {"dir": "D:/ComfyUI", "python": null, "gpu": "auto", "args": []},
      "ollama": {"exe": null},
      "commands": [
        {"id": "llamacpp", "label": "llama.cpp",
         "argv": ["powershell", "-NoProfile", "-File", "D:/LocalAI/Start-LlamaServer.ps1"],
         "cwd": "D:/LocalAI", "health": "http://127.0.0.1:8081/health",
         "capabilities": ["llm", "vision"]}
      ]
    }

  Nothing has to be written for a standard install: ComfyUI is looked for
  in the usual folders (and ``COMFYUI_DIR``), Ollama on ``PATH`` and in its
  default install folder.

* ``~/.hoard/backends/state.json``: the processes the family started (pid
  and creation time, so a recycled pid is never mistaken for ours). Prospero
  can stop a ComfyUI the Hub started, and a restarted app still knows what
  it owns. Only those processes are ever stopped: a server somebody else
  started is reported as running and left alone.

ComfyUI instances are addressed by port (``comfyui@8188``). The default
port uses ComfyUI's own output/user folders; any other port gets its own
output, temp, user and database folders under ``~/.hoard/backends/`` so two
instances on two GPUs never race on file names or the asset database.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

__all__ = ["Launcher", "Service", "hoard_home", "comfy_port_from_url", "list_gpus"]

DEFAULT_COMFY_PORT = 8188
OLLAMA_URL = "http://127.0.0.1:11434"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
IS_WIN = sys.platform.startswith("win")
COMFY_CAPABILITIES = ["image", "video", "music"]
OLLAMA_CAPABILITIES = ["llm", "vision", "embeddings"]
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")


def hoard_home() -> Path:
    env = os.environ.get("HOARD_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hoard"


def comfy_port_from_url(url: Optional[str]) -> Optional[int]:
    """The port of a loopback ComfyUI URL (8188 when none is given); None
    for a remote one, which no local launcher can start."""
    if not url:
        return DEFAULT_COMFY_PORT
    parts = urlsplit(url.strip())
    if (parts.hostname or "") not in LOOPBACK:
        return None
    return parts.port or (443 if parts.scheme == "https" else 80)


@dataclass
class Service:
    id: str                          # "comfyui@8188", "ollama", "cmd:llamacpp"
    kind: str                        # comfyui | ollama | command
    label: str
    capabilities: list[str]
    url: str
    health: str
    argv: Optional[list[str]] = None
    cwd: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    problem: Optional[str] = None    # why it cannot be started on this machine
    install: Optional[str] = None    # where it was found
    port: Optional[int] = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "capabilities": list(self.capabilities),
                "url": self.url, "install": self.install, "problem": self.problem,
                "command": " ".join(self.argv) if self.argv else None}


# ---------------------------------------------------------------- probes --

def _port_open(url: str, timeout: float = 0.35) -> bool:
    """A quick TCP check first: on Windows an HTTP call to a closed loopback
    port takes about 1.5 s to fail, a status page listing five services
    would take seconds."""
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_status(url: str, timeout: float = 2.0) -> Optional[int]:
    if not _port_open(url):
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:  # noqa: S310 - loopback
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def list_gpus() -> list[dict[str, Any]]:
    """NVIDIA GPUs by PCI bus order (the order ``CUDA_DEVICE_ORDER=PCI_BUS_ID``
    gives ComfyUI), with free and total memory. Empty without nvidia-smi."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    kwargs: dict[str, Any] = {}
    if IS_WIN:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run([exe, "--query-gpu=index,name,memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL, **kwargs).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                gpus.append({"index": int(parts[0]), "name": parts[1], "free_mb": int(float(parts[2])),
                             "total_mb": int(float(parts[3]))})
            except ValueError:
                continue
    return gpus


# ------------------------------------------------------------ processes --

def _creation_time(pid: int) -> Optional[float]:
    """Epoch seconds the process was created, None when it is not alive."""
    if pid <= 0:
        return None
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return ticks / 10_000_000 - 11_644_473_600
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return None
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        btime = next(int(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines() if l.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return 0.0  # alive, creation time unknown (macOS): pid check only


def _alive(pid: int, created: Optional[float]) -> bool:
    now_created = _creation_time(pid)
    if now_created is None:
        return False
    if created and now_created and abs(now_created - created) > 2.0:
        return False  # the pid was recycled by another process
    return True


def _detached_popen(argv: list[str], cwd: Optional[str], env: dict[str, str], log) -> subprocess.Popen:
    base = dict(cwd=cwd or None, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
    if not IS_WIN:
        return subprocess.Popen(argv, start_new_session=True, **base)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # Break away from the caller's job object so the server outlives an
        # app (or a launcher) that kills its own job when it closes.
        return subprocess.Popen(argv, creationflags=flags | 0x01000000, **base)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        return subprocess.Popen(argv, creationflags=flags, **base)


def _kill_tree(pid: int, grace_s: float = 6.0) -> None:
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=flags, timeout=20)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and _creation_time(pid) is not None:
            time.sleep(0.3)
        if _creation_time(pid) is not None:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=flags, timeout=20)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and _creation_time(pid) is not None:
        time.sleep(0.2)
    if _creation_time(pid) is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --------------------------------------------------------------- launcher --

class Launcher:
    """Finds, starts, watches and stops the shared local backends."""

    def __init__(self, home: Path | str | None = None, *, app: str = "hoard"):
        self.home = Path(home) if home else hoard_home()
        self.app = app
        self._lock = threading.RLock()

    # -- files ---------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.home / "backends.json"

    @property
    def state_path(self) -> Path:
        return self.home / "backends" / "state.json"

    @property
    def logs_dir(self) -> Path:
        return self.home / "backends" / "logs"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def config(self) -> dict[str, Any]:
        return self._read_json(self.config_path)

    def set_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge ``patch`` into ``backends.json``: ``comfyui``/``ollama`` are
        merged key by key (None or "" removes a key), ``commands`` replaces
        the list (each one needs ``id``, ``argv`` or ``cmd``, ``health``)."""
        with self._lock:
            cfg = self.config()
            for section in ("comfyui", "ollama"):
                if section in patch and patch[section] is not None:
                    if not isinstance(patch[section], dict):
                        raise ValueError(f"{section} must be an object")
                    cur = dict(cfg.get(section) or {})
                    for key, value in patch[section].items():
                        if value is None or value == "":
                            cur.pop(key, None)
                        else:
                            cur[key] = value
                    if "gpu" in cur and cur["gpu"] != "auto":
                        try:
                            cur["gpu"] = int(cur["gpu"])
                        except (TypeError, ValueError):
                            raise ValueError("comfyui.gpu must be \"auto\" or a GPU index") from None
                    if cur:
                        cfg[section] = cur
                    else:
                        cfg.pop(section, None)
            if "commands" in patch and patch["commands"] is not None:
                cmds = []
                for c in patch["commands"]:
                    if not isinstance(c, dict) or not _ID_RE.match(str(c.get("id") or "")):
                        raise ValueError("each command needs an id of lowercase letters, digits, '-', '_' or '.'")
                    if not (c.get("argv") or c.get("cmd")) or not c.get("health"):
                        raise ValueError(f"command {c['id']!r} needs argv (or cmd) and a health URL")
                    if (urlsplit(str(c["health"])).hostname or "") not in LOOPBACK:
                        raise ValueError(f"command {c['id']!r}: the health URL must be on loopback")
                    cmds.append(c)
                cfg["commands"] = cmds
            self._write_json(self.config_path, cfg)
            return cfg

    def _state(self) -> dict[str, dict[str, Any]]:
        raw = self._read_json(self.state_path)
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

    def _save_state(self, state: dict[str, dict[str, Any]]) -> None:
        self._write_json(self.state_path, state)

    # -- discovery -------------------------------------------------------
    def comfy_candidates(self) -> list[Path]:
        cfg_dir = (self.config().get("comfyui") or {}).get("dir")
        out: list[Path] = []
        for d in (cfg_dir, os.environ.get("COMFYUI_DIR")):
            if d:
                out.append(Path(d).expanduser())
        home = Path.home()
        out += [home / "ComfyUI", home / "Documents" / "ComfyUI", home / "Desktop" / "ComfyUI",
                home / "AI" / "ComfyUI"]
        if IS_WIN:
            for drive in "CDEFGH":
                root = Path(f"{drive}:/")
                out += [root / "ComfyUI", root / "LocalAI" / "ComfyUI", root / "AI" / "ComfyUI",
                        root / "ComfyUI_windows_portable" / "ComfyUI"]
        else:
            out += [Path("/opt/ComfyUI"), Path("/srv/ComfyUI")]
        seen, uniq = set(), []
        for p in out:
            key = str(p).lower() if IS_WIN else str(p)
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        return uniq

    def comfy_install(self) -> tuple[Optional[Path], Optional[Path], Optional[str]]:
        """(ComfyUI folder, its Python, problem)."""
        cfg = self.config().get("comfyui") or {}
        folder = None
        for cand in self.comfy_candidates():
            try:
                if (cand / "main.py").is_file() and (cand / "comfy").is_dir():
                    folder = cand
                    break
            except OSError:
                continue
        if folder is None:
            if cfg.get("dir"):
                return None, None, f"no ComfyUI install at {cfg['dir']} (main.py not found)"
            return None, None, "ComfyUI was not found: set its folder (backends.json comfyui.dir or COMFYUI_DIR)"
        pythons: list[Path] = []
        if cfg.get("python"):
            pythons.append(Path(cfg["python"]).expanduser())
        for venv in ("venv", ".venv", "env"):
            pythons += [folder / venv / "Scripts" / "python.exe", folder / venv / "bin" / "python"]
        pythons.append(folder.parent / "python_embeded" / "python.exe")  # the portable build (sic)
        for py in pythons:
            if py.is_file():
                return folder, py, None
        return folder, None, (f"ComfyUI found at {folder} but no Python environment next to it "
                              "(venv, .venv, python_embeded): set comfyui.python")

    def ollama_exe(self) -> Optional[str]:
        cfg = self.config().get("ollama") or {}
        cands = [cfg.get("exe"), shutil.which("ollama")]
        if IS_WIN:
            local = os.environ.get("LOCALAPPDATA")
            if local:
                cands.append(str(Path(local) / "Programs" / "Ollama" / "ollama.exe"))
        else:
            cands += ["/usr/local/bin/ollama", "/usr/bin/ollama"]
        for c in cands:
            if c and Path(c).is_file():
                return str(c)
        return None

    @staticmethod
    def _comfy_flags(folder: Path) -> str:
        try:
            return (folder / "comfy" / "cli_args.py").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def comfy_service(self, port: int = DEFAULT_COMFY_PORT, gpu: Any = None) -> Service:
        url = f"http://127.0.0.1:{port}"
        svc = Service(id=f"comfyui@{port}", kind="comfyui", label=f"ComfyUI :{port}",
                      capabilities=list(COMFY_CAPABILITIES), url=url, health=url + "/system_stats", port=port)
        folder, python, problem = self.comfy_install()
        svc.problem = problem
        if folder is None or python is None:
            svc.install = str(folder) if folder else None
            return svc
        cfg = self.config().get("comfyui") or {}
        flags = self._comfy_flags(folder)
        argv = [str(python), "main.py", "--listen", "127.0.0.1", "--port", str(port)]
        if "--disable-auto-launch" in flags:
            argv.append("--disable-auto-launch")
        if "--preview-method" in flags:
            argv += ["--preview-method", "none"]
        if gpu is not None and "--cuda-device" in flags:
            argv += ["--cuda-device", str(gpu)]
        if port != DEFAULT_COMFY_PORT:
            own = self.home / "backends" / f"comfyui-{port}"
            for flag, sub in (("--output-directory", "output"), ("--temp-directory", "temp"), ("--user-directory", "user")):
                if flag in flags:
                    argv += [flag, str(own / sub)]
            if "--database-url" in flags:
                argv += ["--database-url", "sqlite:///" + (own / "comfyui.db").as_posix()]
        extra = cfg.get("args") or []
        if isinstance(extra, str):
            extra = shlex.split(extra, posix=not IS_WIN)
        argv += [str(a) for a in extra]
        svc.argv, svc.cwd, svc.install = argv, str(folder), str(folder)
        svc.env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
        return svc

    def ollama_service(self) -> Service:
        svc = Service(id="ollama", kind="ollama", label="Ollama", capabilities=list(OLLAMA_CAPABILITIES),
                      url=OLLAMA_URL, health=OLLAMA_URL + "/api/version")
        exe = self.ollama_exe()
        if exe is None:
            svc.problem = "Ollama is not installed (or set ollama.exe in backends.json)"
        else:
            svc.argv, svc.install = [exe, "serve"], exe
        return svc

    def command_services(self) -> list[Service]:
        out = []
        for c in self.config().get("commands") or []:
            if not isinstance(c, dict) or not _ID_RE.match(str(c.get("id") or "")) or not c.get("health"):
                continue
            argv = c.get("argv")
            if not argv and c.get("cmd"):
                argv = shlex.split(str(c["cmd"]), posix=not IS_WIN)
            health = str(c["health"])
            parts = urlsplit(health)
            svc = Service(id=f"cmd:{c['id']}", kind="command", label=str(c.get("label") or c["id"]),
                          capabilities=[str(x) for x in (c.get("capabilities") or [])],
                          url=f"{parts.scheme}://{parts.netloc}", health=health,
                          argv=[str(a) for a in argv] if argv else None, cwd=c.get("cwd"),
                          env={str(k): str(v) for k, v in (c.get("env") or {}).items()}, install=c.get("cwd"))
            if not svc.argv:
                svc.problem = "no argv/cmd configured"
            elif svc.cwd and not Path(svc.cwd).is_dir():
                svc.problem = f"working folder not found: {svc.cwd}"
            out.append(svc)
        return out

    def services(self, comfy_ports: Optional[list[int]] = None) -> list[Service]:
        ports = [DEFAULT_COMFY_PORT] if comfy_ports is None else list(dict.fromkeys(comfy_ports))
        for sid in self._state():  # anything the family started stays visible
            if sid.startswith("comfyui@"):
                try:
                    p = int(sid.split("@", 1)[1])
                except ValueError:
                    continue
                if p not in ports:
                    ports.append(p)
        return [*(self.comfy_service(p) for p in ports), self.ollama_service(), *self.command_services()]

    def get(self, service_id: str, gpu: Any = None) -> Service:
        if service_id.startswith("comfyui@"):
            try:
                port = int(service_id.split("@", 1)[1])
            except ValueError:
                raise KeyError(service_id) from None
            if not 1 <= port <= 65535:
                raise KeyError(service_id)
            return self.comfy_service(port, gpu=gpu)
        if service_id == "comfyui":
            return self.comfy_service(DEFAULT_COMFY_PORT, gpu=gpu)
        if service_id == "ollama":
            return self.ollama_service()
        for svc in self.command_services():
            if svc.id == service_id or svc.id == f"cmd:{service_id}":
                return svc
        raise KeyError(service_id)

    # -- runtime ---------------------------------------------------------
    def _owned(self, service_id: str) -> Optional[dict[str, Any]]:
        st = self._state().get(service_id)
        if st and st.get("pid") and _alive(int(st["pid"]), st.get("created")):
            return st
        return None

    def status(self, svc: Service) -> dict[str, Any]:
        code = _http_status(svc.health)
        own = self._owned(svc.id)
        if code is not None and code < 500:
            state = "running"
        elif own is not None:
            state = "starting"
        elif svc.problem:
            state = "unavailable"
        else:
            state = "down"
        d = svc.public()
        d.update({"state": state, "pid": own.get("pid") if own else None,
                  "started_by": own.get("by") if own else None, "stoppable": own is not None,
                  "gpu": own.get("gpu") if own else None,
                  "log": str(self.log_path(svc.id)) if (own or self.log_path(svc.id).is_file()) else None,
                  "startable": state in ("down",) and svc.argv is not None})
        return d

    def statuses(self, comfy_ports: Optional[list[int]] = None) -> list[dict[str, Any]]:
        services = self.services(comfy_ports)
        results: list[Optional[dict[str, Any]]] = [None] * len(services)

        def one(i: int, s: Service) -> None:
            results[i] = self.status(s)

        threads = [threading.Thread(target=one, args=(i, s), daemon=True) for i, s in enumerate(services)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return [r for r in results if r is not None]

    def log_path(self, service_id: str) -> Path:
        return self.logs_dir / (re.sub(r"[^A-Za-z0-9_.-]+", "_", service_id) + ".log")

    def log_tail(self, service_id: str, chars: int = 1500) -> str:
        try:
            data = self.log_path(service_id).read_bytes()[-chars * 2:]
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")[-chars:]

    def pick_gpu(self, exclude: Optional[set[int]] = None) -> Optional[int]:
        """The GPU with the most free memory, skipping ones another ComfyUI
        the family started already uses (when there is any other left)."""
        gpus = list_gpus()
        if not gpus:
            return None
        busy = set(exclude or ())
        for sid, st in self._state().items():
            if sid.startswith("comfyui@") and st.get("gpu") is not None and _alive(int(st.get("pid") or 0), st.get("created")):
                busy.add(int(st["gpu"]))
        pool = [g for g in gpus if g["index"] not in busy] or gpus
        return max(pool, key=lambda g: g["free_mb"])["index"]

    def start(self, service_id: str, *, gpu: Any = None, wait_s: float = 0.0) -> dict[str, Any]:
        """Start a service detached. ``gpu``: an index, "auto" (the most free
        memory) or None (ComfyUI's setting in backends.json, else "auto").
        ``wait_s`` > 0 waits that long for it to answer."""
        with self._lock:
            chosen_gpu: Optional[int] = None
            if service_id.startswith("comfyui"):
                want = gpu if gpu is not None else (self.config().get("comfyui") or {}).get("gpu", "auto")
                if want in (None, "auto", ""):
                    chosen_gpu = self.pick_gpu()
                else:
                    try:
                        chosen_gpu = int(want)
                    except (TypeError, ValueError):
                        return {"ok": False, "service": service_id, "error": f"gpu must be \"auto\" or an index, got {want!r}"}
            try:
                svc = self.get(service_id, gpu=chosen_gpu)
            except KeyError:
                return {"ok": False, "service": service_id, "error": f"unknown service {service_id!r}"}
            current = self.status(svc)
            if current["state"] in ("running", "starting"):
                out = {"ok": True, "service": svc.id, "already": True, "state": current["state"], "url": svc.url}
                if wait_s > 0 and current["state"] == "starting":
                    out.update(self._wait(svc, wait_s))
                return out
            if svc.problem or not svc.argv:
                return {"ok": False, "service": svc.id, "error": svc.problem or "nothing to start"}
            log_path = self.log_path(svc.id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if svc.kind == "comfyui" and svc.port != DEFAULT_COMFY_PORT:
                for sub in ("output", "temp", "user"):
                    (self.home / "backends" / f"comfyui-{svc.port}" / sub).mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env.update(svc.env)
            env.setdefault("PYTHONUNBUFFERED", "1")
            try:
                with open(log_path, "ab") as log:
                    log.write(f"\n--- started by {self.app} {time.strftime('%Y-%m-%d %H:%M:%S')}: "
                              f"{' '.join(svc.argv)}\n".encode("utf-8"))
                    log.flush()
                    proc = _detached_popen(svc.argv, svc.cwd, env, log)
            except OSError as exc:
                return {"ok": False, "service": svc.id, "error": f"could not start: {exc}", "log": str(log_path)}
            threading.Thread(target=proc.wait, name=f"hoard-launch-{svc.id}", daemon=True).start()
            state = self._state()
            state[svc.id] = {"pid": proc.pid, "created": _creation_time(proc.pid), "started_at": time.time(),
                             "by": self.app, "gpu": chosen_gpu if svc.kind == "comfyui" else None,
                             "command": " ".join(svc.argv)}
            self._save_state(state)
            out = {"ok": True, "service": svc.id, "pid": proc.pid, "url": svc.url, "log": str(log_path),
                   "gpu": chosen_gpu if svc.kind == "comfyui" else None}
        if wait_s > 0:
            out.update(self._wait(svc, wait_s))
        return out

    def _wait(self, svc: Service, wait_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            code = _http_status(svc.health)
            if code is not None and code < 500:
                return {"ready": True, "state": "running"}
            if svc.id in self._state() and self._owned(svc.id) is None:
                return {"ok": False, "ready": False, "state": "exited",
                        "error": f"{svc.label} exited while starting; last log lines:\n{self.log_tail(svc.id, 800)}"}
            time.sleep(1.0)
        return {"ready": False, "state": "starting", "detail": f"not answering after {int(wait_s)} s; still starting"}

    def wait_ready(self, service_id: str, timeout_s: float) -> bool:
        try:
            svc = self.get(service_id)
        except KeyError:
            return False
        return bool(self._wait(svc, timeout_s).get("ready"))

    def stop(self, service_id: str) -> dict[str, Any]:
        """Stop a service the family started. One started elsewhere is
        refused: stop it where it runs."""
        with self._lock:
            if service_id == "comfyui":
                service_id = f"comfyui@{DEFAULT_COMFY_PORT}"
            if service_id != "ollama" and not service_id.startswith(("comfyui@", "cmd:")):
                service_id = f"cmd:{service_id}"
            own = self._owned(service_id)
            state = self._state()
            if own is None:
                state.pop(service_id, None)
                self._save_state(state)
                try:
                    svc = self.get(service_id)
                    running = _http_status(svc.health) is not None
                except KeyError:
                    running = False
                if running:
                    return {"ok": False, "service": service_id,
                            "error": "it is running but was not started from the Hoard family; stop it where it runs"}
                return {"ok": True, "service": service_id, "detail": "not running"}
            _kill_tree(int(own["pid"]))
            gone = _creation_time(int(own["pid"])) is None
            if gone:
                state.pop(service_id, None)
                self._save_state(state)
            return {"ok": gone, "service": service_id, "pid": own["pid"],
                    **({} if gone else {"error": "the process is still alive after the stop request"})}
