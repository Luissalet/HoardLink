"""Low-level process launch for Python code that needs the standard subprocess API.

This module is also shipped by itself with the Linux placement helper.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

IS_WIN = sys.platform.startswith("win")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def is_native_runner(runner: Any) -> bool:
    """Recognize both spellings of the real runner when a caller allows test doubles."""
    return runner is None or runner is run_hidden or runner is subprocess.run


def _hide_console(options: dict[str, Any]) -> None:
    if IS_WIN:
        # Windows ignores CREATE_NO_WINDOW if either of these two flags is set.
        incompatible = (getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
                        | getattr(subprocess, "DETACHED_PROCESS", 0x08))
        options["creationflags"] = (options.get("creationflags", 0) & ~incompatible) | CREATE_NO_WINDOW


def run_hidden(*args: Any, **options: Any) -> subprocess.CompletedProcess:
    """Run with subprocess.run semantics and no Windows console window."""
    _hide_console(options)
    return subprocess.run(*args, **options)


def popen_hidden(*args: Any, **options: Any) -> subprocess.Popen:
    """Start with subprocess.Popen semantics and no Windows console window."""
    _hide_console(options)
    return subprocess.Popen(*args, **options)
