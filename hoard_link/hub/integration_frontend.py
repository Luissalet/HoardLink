"""Build and publish the coordination surfaces from the exact integration candidate."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

from .._process import run_hidden
from ..atomic import tmp_path_for, write_bytes_atomic, write_json_atomic
from .compiled_ui import CONSOLE_INPUTS, RECEIPT, STUDIO_INPUTS
from .integration_git import IntegrationError


WINDOWS = os.name == "nt"
SESSION_INPUTS = {"hoard_link/hub/ui_session.py", "hoard_link/hub/server.py", "scripts/start-agora.ps1"}


def prepare_frontend(candidate: Path, paths: set[str], folder: Path, *, revision: str):
    def touches(inputs):
        return any(path == source or path.startswith(source + "/") for path in paths for source in inputs)

    console_changed = touches(CONSOLE_INPUTS)
    native_changed = bool(paths & SESSION_INPUTS or any(path.startswith("apps/agora/src-tauri/") for path in paths))
    desktop = WINDOWS and (candidate / "apps/agora/src-tauri/Cargo.toml").is_file() and (
        console_changed or native_changed)
    if (candidate / "apps/agora/package.json").is_file() and (console_changed or desktop):
        workdir, destination, entry = candidate / "apps/agora", "apps/agora/dist", "index.html"
    elif (candidate / "studio/src/screens/agora/AgoraApp.tsx").is_file() and touches(STUDIO_INPUTS):
        workdir, destination, entry = candidate, "static/studio", "studio.js"
    else:
        return None

    npm = shutil.which("npm.cmd" if WINDOWS else "npm")
    if not npm:
        raise IntegrationError("The frontend build needs npm on the integration host")
    output = folder / "frontend-output"
    log = folder / "frontend-build.log"
    target = workdir / "src-tauri/target"
    build_args = ("run", "desktop:build", "--", "--", "--locked") if desktop else (
        "run", "build", "--", "--outDir", str(output))
    with log.open("wb") as stream:
        for args in (("ci", "--no-audit", "--no-fund"), build_args):
            try:
                env = {**os.environ, "CARGO_TARGET_DIR": str(target)} if desktop else None
                result = run_hidden([npm, *args], cwd=workdir, stdout=stream, stderr=subprocess.STDOUT,
                                        env=env, timeout=1800 if desktop else 600,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired) as error:
                raise IntegrationError(f"Frontend build failed for {destination}. See {log}") from error
            if result.returncode:
                raise IntegrationError(f"Frontend build failed for {destination} ({result.returncode}). See {log}")
    if desktop:
        if not (workdir / "dist" / entry).is_file():
            raise IntegrationError(f"Desktop build produced no {entry}. See {log}")
        shutil.move(workdir / "dist", output)
    if not (output / entry).is_file():
        raise IntegrationError(f"Browser build produced no {entry}. See {log}")
    artifact = output / entry
    build = {"destination": destination, "entry": entry,
             "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "log": str(log)}
    write_json_atomic(output / RECEIPT, {"source_commit": revision,
                                         "entry_sha256": build["sha256"], "built_at": artifact.stat().st_mtime})
    if desktop:
        binary = target / "release/hoard-agora.exe"
        if not binary.is_file():
            raise IntegrationError(f"Desktop build produced no {binary.name}. See {log}")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        native_output = folder / "native-output"
        native_output.mkdir()
        shutil.move(binary, native_output / binary.name)
        build["native"] = {"destination": "apps/agora/bin", "entry": binary.name, "sha256": digest}
    return build


def _publications(build: dict):
    yield build, "frontend"
    if "native" in build:
        yield build["native"], "native"


def prepare_publication(repo: Path, build: dict, folder: Path):
    """Save any files the new build will replace before the first write."""
    for build, name in _publications(build):
        _prepare_files(repo, build, folder / f"{name}-output", folder / f"{name}-previous")


def _prepare_files(repo: Path, build: dict, output: Path, backup: Path):
    target = repo / build["destination"]
    files = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
    files.remove(build["entry"])
    files.append(build["entry"])
    changed = []
    for filename in files:
        previous = target / filename
        if previous.is_file():
            previous_bytes = previous.read_bytes()
            if previous_bytes == (output / filename).read_bytes():
                continue
            write_bytes_atomic(backup / filename, previous_bytes)
        changed.append(filename)
    build["files"] = changed


def _retire_native(path: Path) -> Path:
    # A running Windows executable can be renamed even when it cannot be replaced.
    retired = tmp_path_for(path.with_suffix(".retired.exe"))
    path.rename(retired)
    return retired


def _discard_retired(path: Path):
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        # It can be removed by the next native update once the old window closes.
        pass


def _activate_native(path: Path, content: bytes):
    for retired in path.parent.glob(path.with_suffix(".retired.exe").name + ".*.tmp"):
        _discard_retired(retired)
    staged = tmp_path_for(path)
    write_bytes_atomic(staged, content)
    try:
        try:
            staged.replace(path)
        except PermissionError:
            if not path.is_file():
                raise
            retired = _retire_native(path)
            try:
                staged.replace(path)
            except OSError:
                retired.replace(path)
                raise
            _discard_retired(retired)
    finally:
        staged.unlink(missing_ok=True)


def publish_frontend(repo: Path, build: dict, folder: Path):
    # Publish hashed dependencies first and keep the previous ones for tabs still using them.
    for artifact, name in _publications(build):
        target = repo / artifact["destination"]
        for filename in artifact["files"]:
            publish = _activate_native if name == "native" else write_bytes_atomic
            publish(target / filename, (folder / f"{name}-output" / filename).read_bytes())


def restore_frontend(repo: Path, build: dict, folder: Path):
    for artifact, name in reversed(list(_publications(build))):
        target = repo / artifact["destination"]
        for filename in reversed(artifact["files"]):
            previous = folder / f"{name}-previous" / filename
            path = target / filename
            if previous.is_file():
                publish = _activate_native if name == "native" else write_bytes_atomic
                publish(path, previous.read_bytes())
            elif name == "native" and path.is_file():
                _discard_retired(_retire_native(path))
            else:
                path.unlink(missing_ok=True)
