"""Worker-specific launch policy. App and Hub launchers keep their own policy."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Protocol

from .._process import popen_hidden

IS_WINDOWS = sys.platform == "win32"
NPM_ENTRY_POINTS = {
    "claude": Path("@anthropic-ai", "claude-code", "cli.js"),
    "codex": Path("@openai", "codex", "bin", "codex.js"),
}


class WorkerProcess(Protocol):
    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...


def _native_command(command, env):
    argv = [os.fspath(part) for part in command]
    entry = Path(argv[0])
    shim = NPM_ENTRY_POINTS.get(entry.stem.casefold())
    if entry.suffix.casefold() in {".cmd", ".bat"} and shim is not None:
        # Invoke the supported npm entry directly, without a command shell.
        modules = entry.parent.parent if entry.parent.name.casefold() == ".bin" else entry.parent / "node_modules"
        script = modules / shim
        local_node = entry.parent / "node.exe"
        node = str(local_node) if local_node.is_file() else shutil.which("node.exe", path=env.get("PATH"))
        if node and script.is_file():
            return [node, str(script), *argv[1:]]
    elif entry.suffix.casefold() in {"", ".exe", ".com"}:
        # CreateProcessW receives the application explicitly and does not search PATH.
        if not entry.is_absolute() and entry.parent == Path("."):
            executable = shutil.which(argv[0], path=env.get("PATH"))
            if not executable or Path(executable).suffix.casefold() not in {".exe", ".com"}:
                raise FileNotFoundError(argv[0])
            return [executable, *argv[1:]]
        return argv
    raise OSError("Desktop-isolated workers need a native executable or a supported Node entry point")


def launch_worker(config, command, *, cwd, env, log) -> WorkerProcess:
    settings = getattr(config, "agora", None) or {}
    if not IS_WINDOWS or settings.get("worker_private_desktop") is False:
        return popen_hidden(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            text=True, cwd=cwd, env=env)
    from .windows_job import launch_on_private_desktop

    return launch_on_private_desktop(_native_command(command, env), cwd=cwd, env=env, log=log)
