from __future__ import annotations

import subprocess
import sys

import pytest

from hoard_link import gpu


def test_gpu_probe_allows_slow_driver_startup(monkeypatch):
    run = subprocess.run

    def slow_driver(*args, **kwargs):
        return run([sys.executable, "-c", "import time; time.sleep(2.2); print('3, 16311, 56')"], **kwargs)

    monkeypatch.setattr(gpu.subprocess, "run", slow_driver)
    result = gpu.probe_gpu_memory()
    assert result.status == "ready"
    assert result.gpus[0].free_mb == 16255


def test_gpu_probe_respects_explicit_short_deadline(monkeypatch):
    run = subprocess.run

    def slow_driver(*args, **kwargs):
        return run([sys.executable, "-c", "import time; time.sleep(2.2)"], **kwargs)

    monkeypatch.setattr(gpu.subprocess, "run", slow_driver)
    result = gpu.probe_gpu_memory(timeout_s=0.1)
    assert result.status == "error"
    assert result.error_type == "timeout"
    assert result.gpus == ()


def test_gpu_free_mb_parses_nvidia_smi_output(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="0, 24576, 20000\n1, 24576, 100\n", stderr=""
        )

    monkeypatch.setattr(gpu.subprocess, "run", fake_run)
    result = gpu.gpu_free_mb()
    assert len(result) == 2
    assert result[0].index == 0
    assert result[0].total_mb == 24576
    assert result[0].used_mb == 20000
    assert result[0].free_mb == 4576
    assert result[1].free_mb == 24476


def test_gpu_free_mb_returns_empty_list_when_nvidia_smi_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(gpu.subprocess, "run", fake_run)
    assert gpu.gpu_free_mb() == []


def test_gpu_free_mb_returns_empty_list_on_nonzero_exit(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="error")

    monkeypatch.setattr(gpu.subprocess, "run", fake_run)
    assert gpu.gpu_free_mb() == []


def test_gpu_free_mb_returns_empty_list_on_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=2.0)

    monkeypatch.setattr(gpu.subprocess, "run", fake_run)
    assert gpu.gpu_free_mb() == []
