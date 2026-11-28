"""Windows process ownership and a same-user sealed Hub launch record."""
from __future__ import annotations

import ctypes
import stat
from ctypes import wintypes
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(content, *, decrypt=False):
    source_buffer = (ctypes.c_ubyte * len(content)).from_buffer_copy(content)
    source, target = Blob(len(content), source_buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    transform = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    transform.argtypes = (
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    )
    transform.restype = wintypes.BOOL
    if not transform(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free(target.data)


def read_owner(pid, checkout, *, with_environment=False):
    import psutil

    process = psutil.Process(pid)
    argv = process.cmdline()
    program = Path(argv[0]).resolve()
    cwd = Path(process.cwd()).resolve()
    if (cwd != checkout or program.name.lower() != "pythonw.exe" or not program.is_file()
            or argv[1:] != ["-m", "hoard_link.hub", "--no-window"]):
        raise ValueError("The listener does not match the installed headless Hub launch")
    record = {"argv": argv, "cwd": str(cwd), "pid": pid, "started": process.create_time()}
    if with_environment:
        record["env"] = process.environ()
    return record


def reject_redirected(path):
    if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Expected a real file or directory: {path}")


def package_runtime_probe():
    """Script for probing a venv with isolated Python, without loading an engine."""
    return """
import importlib.metadata as metadata
import json
from pathlib import Path
import runpy
import sys

if len(sys.argv) > 1:
    runpy.run_path(sys.argv[1], run_name='installation_probe')
packages = {}
for package in ('torch', 'torchaudio', 'qwen-tts', 'faster-qwen3-tts'):
    try:
        packages[package] = metadata.version(package)
    except metadata.PackageNotFoundError:
        pass
print(json.dumps({'python': sys.version.split()[0], 'prefix': sys.prefix, 'packages': packages}))
"""
