"""Profiles: a named set of apps (plus external commands, plus which apps
to open as desktop windows) started and stopped together.

``hub.json``::

    "profiles": {
      "video": {
        "apps": ["daguerre", "scribe"],
        "commands": [
          {"name": "comfy gpu1", "cmd": "python main.py --port 8189", "cwd": "D:/ComfyUI",
           "health": "http://127.0.0.1:8189/system_stats", "env": {"CUDA_VISIBLE_DEVICES": "1"}}
        ],
        "desktop": ["daguerre"]
      }
    }

A *command* is anything that is not an app of the family (a ComfyUI
instance, a llama-server, a script). It gets the same running/down
treatment as an app: its ``health`` URL when given (any answer below 500
is "running"), else whether the process the hub started is still alive.
The hub remembers the pids it started in ``<data>/commands.json`` so a
restarted hub can still report and stop them, and stops only processes it
started itself (checked by pid *and* creation time).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import procs

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG.sub("-", str(text).lower()).strip("-") or "x"


@dataclass
class Command:
    profile: str
    name: str
    cmd: Any                          # a command line string or a list of arguments
    cwd: Optional[str] = None
    health: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{slug(self.profile)}--{slug(self.name)}"

    def argv(self) -> Any:
        if isinstance(self.cmd, list):
            return [str(a) for a in self.cmd]
        if sys.platform.startswith("win"):
            return str(self.cmd)          # CreateProcess takes a command line as is
        return shlex.split(str(self.cmd))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "profile": self.profile, "name": self.name, "cmd": self.cmd, "cwd": self.cwd,
                "health": self.health, "env": dict(self.env)}


@dataclass
class Profile:
    name: str
    apps: list[str] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    desktop: list[str] = field(default_factory=list)

    @property
    def members(self) -> list[str]:
        """App ids to start: ``apps`` then any ``desktop`` app not in it."""
        return list(dict.fromkeys(self.apps + self.desktop))


def parse(raw: Any) -> tuple[dict[str, Profile], list[str]]:
    """``hub.json``'s ``profiles`` → profiles, plus readable problems. A
    malformed entry is skipped, never fatal."""
    out: dict[str, Profile] = {}
    problems: list[str] = []
    if not isinstance(raw, dict):
        if raw not in (None, {}):
            problems.append("profiles must be an object {name: {apps, commands, desktop}}")
        return out, problems
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            problems.append(f"profile {name!r}: expected an object")
            continue
        p = Profile(name=str(name))
        for key in ("apps", "desktop"):
            val = spec.get(key) or []
            if isinstance(val, str):
                val = [val]
            if not isinstance(val, list):
                problems.append(f"profile {name!r}: {key} must be a list of app ids")
                val = []
            setattr(p, key, [str(v) for v in val if str(v).strip()])
        for i, c in enumerate(spec.get("commands") or []):
            if not isinstance(c, dict) or not c.get("cmd"):
                problems.append(f"profile {name!r}: command #{i + 1} needs at least a cmd")
                continue
            env = c.get("env") if isinstance(c.get("env"), dict) else {}
            p.commands.append(Command(profile=p.name, name=str(c.get("name") or f"command {i + 1}"), cmd=c["cmd"],
                                      cwd=os.path.expanduser(str(c["cwd"])) if c.get("cwd") else None,
                                      health=str(c["health"]) if c.get("health") else None,
                                      env={str(k): str(v) for k, v in env.items()}))
        out[p.name] = p
    return out, problems


class CommandRunner:
    """Starts, watches and stops the external commands of every profile."""

    def __init__(self, state_file: str, logs_dir: str):
        self.state_file = state_file
        self.logs_dir = logs_dir
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return {str(k): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=1)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def log_path(self, cmd: Command) -> str:
        return os.path.join(self.logs_dir, f"cmd-{cmd.id}.log")

    def _pid(self, cmd: Command) -> Optional[int]:
        """The pid the hub started for ``cmd``, if that process still runs."""
        st = self._state.get(cmd.id)
        if not st or not st.get("pid"):
            return None
        pid = int(st["pid"])
        ps = procs._psutil()
        if ps is None:
            return pid if _pid_exists_no_psutil(pid) else None
        try:
            p = ps.Process(pid)
            if st.get("created_at") and abs(p.create_time() - float(st["created_at"])) > 2.0:
                return None
            if p.status() == getattr(ps, "STATUS_ZOMBIE", "zombie"):
                return None
            return pid
        except Exception:  # noqa: BLE001
            return None

    def status(self, cmd: Command) -> dict[str, Any]:
        pid = self._pid(cmd)
        health: dict[str, Any] = {"url": cmd.health, "status": None}
        if cmd.health:
            status, _ = procs.fetch_json(cmd.health)
            health["status"] = status
            if status is not None and status < 500:
                state = "running"
            elif pid is not None:
                state = "starting"          # our process is alive, its health URL does not answer yet
            else:
                state = "down"
        else:
            state = "running" if pid is not None else "down"
        d = cmd.to_dict()
        d.update({"state": state, "pid": pid, "health_check": health, "log": self.log_path(cmd),
                  "started_by_hub": pid is not None, "stoppable": pid is not None})
        return d

    def start(self, cmd: Command) -> dict[str, Any]:
        with self._lock:
            current = self.status(cmd)
            if current["state"] in ("running", "starting"):
                return {"ok": True, "command": cmd.id, "already": True, "detail": f"already {current['state']}"}
            if cmd.cwd and not os.path.isdir(cmd.cwd):
                return {"ok": False, "command": cmd.id, "error": f"working directory not found: {cmd.cwd}"}
            os.makedirs(self.logs_dir, exist_ok=True)
            log_path = self.log_path(cmd)
            env = dict(os.environ)
            env.update(cmd.env)
            env.setdefault("PYTHONUNBUFFERED", "1")
            try:
                log = open(log_path, "ab")
                log.write(f"\n--- hoard-hub start {time.strftime('%Y-%m-%d %H:%M:%S')}: {cmd.cmd}\n".encode("utf-8"))
                proc = subprocess.Popen(cmd.argv(), cwd=cmd.cwd or None, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, close_fds=True, **procs._detached_kwargs())
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "command": cmd.id, "error": f"could not start: {exc}", "log": log_path}
            created = None
            ps = procs._psutil()
            if ps is not None:
                try:
                    created = ps.Process(proc.pid).create_time()
                except Exception:  # noqa: BLE001
                    created = None
            self._state[cmd.id] = {"pid": proc.pid, "created_at": created, "started_at": time.time(),
                                   "cmd": cmd.cmd, "profile": cmd.profile, "name": cmd.name}
            self._save()
            # Keep a handle so the child is reaped when it exits (no zombie on POSIX).
            threading.Thread(target=proc.wait, name=f"hoard-cmd-{cmd.id}", daemon=True).start()
            return {"ok": True, "command": cmd.id, "pid": proc.pid, "log": log_path}

    def stop(self, cmd: Command) -> dict[str, Any]:
        with self._lock:
            pid = self._pid(cmd)
            if pid is None:
                self._state.pop(cmd.id, None)
                self._save()
                if cmd.health and procs.fetch_json(cmd.health)[0] is not None:
                    return {"ok": False, "command": cmd.id,
                            "error": "it answers on its health URL but was not started by the hub; stop it where it runs"}
                return {"ok": True, "command": cmd.id, "detail": "not running"}
            created = self._state.get(cmd.id, {}).get("created_at")
            res = procs.terminate_tree(pid, created_at=float(created) if created else None)
            if res.get("ok"):
                self._state.pop(cmd.id, None)
                self._save()
            res["command"] = cmd.id
            res["pid"] = pid
            return res


def _pid_exists_no_psutil(pid: int) -> bool:
    if sys.platform.startswith("win"):
        return True  # cannot tell cheaply; the health URL is the better signal there
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
