"""Build the Agora browser surfaces from the exact integration candidate."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

from ..atomic import write_bytes_atomic
from .integration_git import IntegrationError


def prepare_frontend(candidate: Path, paths: set[str], folder: Path):
    console_inputs = {"apps/agora/index.html", "apps/agora/package.json", "apps/agora/package-lock.json",
                      "apps/agora/vite.config.ts", "apps/agora/tsconfig.json",
                      "hoard_link/hub/ui/production_decisions_data.js",
                      "hoard_link/hub/ui/production_decisions_data.d.ts"}
    studio_inputs = {"package.json", "package-lock.json", "vite.config.ts", "tsconfig.json"}
    if (candidate / "apps/agora/package.json").is_file() and (
            paths & console_inputs or any(path.startswith("apps/agora/src/") for path in paths)):
        workdir, destination, entry = candidate / "apps/agora", "apps/agora/dist", "index.html"
    elif (candidate / "studio/src/screens/agora/AgoraApp.tsx").is_file() and (
            paths & studio_inputs or any(path.startswith("studio/") for path in paths)):
        workdir, destination, entry = candidate, "static/studio", "studio.js"
    else:
        return None

    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise IntegrationError("The browser build needs npm on the integration host")
    output = folder / "frontend-output"
    log = folder / "frontend-build.log"
    with log.open("wb") as stream:
        for args in (("ci", "--no-audit", "--no-fund"), ("run", "build", "--", "--outDir", str(output))):
            try:
                result = subprocess.run([npm, *args], cwd=workdir, stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired) as error:
                raise IntegrationError(f"Browser build failed for {destination}. See {log}") from error
            if result.returncode:
                raise IntegrationError(f"Browser build failed for {destination} ({result.returncode}). See {log}")
    if not (output / entry).is_file():
        raise IntegrationError(f"Browser build produced no {entry}. See {log}")
    return {"destination": destination, "entry": entry,
            "sha256": hashlib.sha256((output / entry).read_bytes()).hexdigest(), "log": str(log)}


def prepare_publication(repo: Path, build: dict, folder: Path):
    """Save any files the new build will replace before the first write."""
    output = folder / "frontend-output"
    backup = folder / "frontend-previous"
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


def publish_frontend(repo: Path, build: dict, folder: Path):
    # Publish hashed dependencies first and keep the previous ones for tabs still using them.
    target = repo / build["destination"]
    for filename in build["files"]:
        write_bytes_atomic(target / filename, (folder / "frontend-output" / filename).read_bytes())


def restore_frontend(repo: Path, build: dict, folder: Path):
    target = repo / build["destination"]
    for filename in reversed(build["files"]):
        previous = folder / "frontend-previous" / filename
        if previous.is_file():
            write_bytes_atomic(target / filename, previous.read_bytes())
        else:
            (target / filename).unlink(missing_ok=True)
