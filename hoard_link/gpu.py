"""Best-effort GPU free-memory query via ``nvidia-smi``.

Used before handing a GPU job (ComfyUI) to a resolved server so an app can
show "not enough VRAM free" instead of a confusing timeout. Never raises:
no ``nvidia-smi`` (no NVIDIA GPU, or not on PATH) simply yields ``[]``.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class GpuMemory:
    index: int
    total_mb: int
    used_mb: int

    @property
    def free_mb(self) -> int:
        return self.total_mb - self.used_mb


def gpu_free_mb(timeout_s: float = 2.0) -> list[GpuMemory]:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[GpuMemory] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            out.append(GpuMemory(index=int(parts[0]), total_mb=int(parts[1]), used_mb=int(parts[2])))
        except ValueError:
            continue
    return out
