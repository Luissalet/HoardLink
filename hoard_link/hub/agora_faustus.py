"""Faustus workers whose workspace belongs to the PC hosting the Hub.

The main Faustus address follows placement. A Spark cannot execute Windows
paths, so an explicit worker launch starts a separate, headless PC runtime
with its own data and the selected Sparks endpoint. No user data is copied.
"""
from __future__ import annotations

import ntpath
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def service(hub: Any) -> dict[str, Any]:
    """Read the same location as /api/placement, without starting anything."""
    placement = getattr(hub, "placement", None)
    state = placement.state() if placement is not None else {}
    if (state.get("job") or {}).get("running"):
        return {"reachable": False, "error": "Faustus placement is moving"}
    if state.get("location") == "sparks":
        from .contract import _get
        base = str((state.get("urls") or {}).get("faustus") or "").rstrip("/")
        if not base:
            return {"reachable": False, "error": "Faustus placement has no URL"}
        status, body = _get(base + "/api/health", "", 6.0)
        return {"url": base, "reachable": status == 200, "body": body, "remote": True}
    return hub.faustus_status()


def prepare(console: Any, agent: str, model: dict, effort: str) -> tuple[dict | None, str]:
    """Create an independent native project/session when no local controller owns one."""
    root, python = console.hub.faustus_runtime.installation()
    if root is None or python is None:
        return None, "Faustus installation is unavailable for the PC worker."
    root = Path(root)
    agora = console._agora()
    space = agora.space_store.get(console.space_id) if hasattr(agora, "space_store") else None
    workspace = str((space or {}).get("workspace") or root.parent)
    if ntpath.splitdrive(workspace)[0] and not Path(workspace).is_dir():
        return None, "The space's PC workspace directory is unavailable."
    target = service(console.hub)
    if not target.get("reachable"):
        return None, str(target.get("error") or "The placed Faustus service is unavailable.")
    base = str(target.get("url") or "").rstrip("/")
    endpoint_id = str(model["endpoint_id"])
    runtime = None
    if target.get("remote") and Path(workspace).is_dir():
        status, endpoints = console._faustus_session_request("GET", "/api/model-endpoints", base_url=base)
        endpoint = next((row for row in endpoints if row.get("id") == endpoint_id and row.get("is_enabled")), None) if status == 200 and isinstance(endpoints, list) else None
        if not endpoint or not str(endpoint.get("base_url") or "").startswith(("http://", "https://")):
            return None, "The selected Sparks endpoint is unavailable for the PC worker."
        runtime = start_pc(console, agent, root, python, endpoint["base_url"], model["model_id"])
        base, endpoint_id = runtime["url"], "headless"
    try:
        def request(method, path, fields=None):
            return console._faustus_session_request(method, path, fields, base_url=base)
        status, projects = request("GET", "/api/projects")
        if status != 200 or not isinstance(projects, list):
            raise RuntimeError("Faustus project lookup failed")
        project = next((row for row in projects if row.get("workspace") == workspace and row.get("enabled", True)), None)
        if project is None:
            status, project = request("POST", "/api/projects", {"name": ("Agora " + console.space_id)[:80], "workspace": workspace})
            if status != 200 or not isinstance(project, dict) or not project.get("id"):
                raise RuntimeError("Faustus project creation failed")
        # The dedicated runtime contains just the Hub connector. A placed
        # Faustus uses its already connected Agora-capable connector.
        status, servers = request("GET", "/api/mcp/servers")
        connector = next((row for row in servers if row.get("status") == "connected" and row.get("is_enabled", True)
                          and (row.get("id") == "agora" or "agora" in str(row.get("name") or "").lower()
                               or "hoard_link.hub.mcp" in str(row.get("args") or ""))), None) if status == 200 and isinstance(servers, list) else None
        if connector is None:
            raise RuntimeError("The Faustus Agora connector is unavailable")
        status, created = request("POST", "/api/session", {"name": "Agora worker " + agent,
            "model": model["model_id"], "endpoint_id": endpoint_id, "project_id": project["id"], "skip_validation": "true"})
        if status != 200 or not isinstance(created, dict) or not created.get("id") or created.get("model") != model["model_id"]:
            raise RuntimeError("Faustus did not confirm the worker session/model")
        session = created["id"]
        if effort:
            status, saved = request("PATCH", "/api/session/" + session, {"reasoning_effort": effort})
            if status != 200 or not isinstance(saved, dict) or saved.get("reasoning_effort") != effort:
                raise RuntimeError("Faustus did not confirm the worker effort")
        status, saved = request("PUT", "/api/coordination/" + session, {"connector_id": connector["id"],
            "actor": agent, "space_id": console.space_id, "active": True,
            "operations": ["poll", "detail", "task", "board", "heartbeat", "add", "claim", "checkpoint", "post", "submit", "review", "done", "lock", "unlock"],
            "resources": [], "repos": [], "thread_ids": [console.thread_id]})
        binding = saved.get("binding") if isinstance(saved, dict) else None
        if status != 200 or not binding or binding.get("actor") != agent or binding.get("project_id") != project["id"]:
            raise RuntimeError("Faustus did not confirm the worker coordination identity")
        return {"session_id": session, "project_id": project["id"], "workspace": workspace,
                "url": base, "runtime": runtime}, ""
    except Exception:
        if runtime:
            stop_pc(runtime)
        raise


def start_pc(console: Any, agent: str, root: Path, python: str, endpoint: str, model: str) -> dict:
    data = Path(console.hub.config.data_dir).resolve() / "agora-console" / "faustus" / agent
    data.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = console._runner_env()
    env["FAUSTUS_APP_ROOT"] = str(root)
    env["FAUSTUS_AGORA_HUB_PYTHON"] = sys.executable
    env["FAUSTUS_AGORA_HUB_ROOT"] = str(Path(__file__).resolve().parents[2])
    command = [str(python), "-X", "utf8", str(root / "integrations" / "harbor" / "serve_headless.py"),
               "--port", str(port), "--data", str(data), "--endpoint-url", endpoint, "--model", model,
               "--hub-url", str(console.hub.config.url)]
    with (data / "server.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    runtime = {"url": base, "process": process, "data": str(data)}
    from .contract import _get
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"PC Faustus exited during startup ({process.returncode}); see {data / 'server.log'}")
            status, body = _get(base + "/api/health", "", 1.0)
            if status == 200 and isinstance(body, dict) and body.get("service") == "faustus":
                status, servers = console._faustus_session_request("GET", "/api/mcp/servers", base_url=base)
                if status == 200 and any(row.get("id") == "agora" and row.get("status") == "connected" for row in servers if isinstance(row, dict)):
                    return runtime
            time.sleep(0.5)
        raise RuntimeError(f"PC Faustus startup timed out; see {data / 'server.log'}")
    except Exception:
        stop_pc(runtime)
        raise


def stop_pc(runtime: dict) -> None:
    process = runtime["process"]
    if process.poll() is None:
        # A Windows venv launcher can forward to another Python process. Stop
        # the dedicated runtime's descendants as well as that launcher.
        from . import procs
        psutil = procs._psutil()
        children = []
        if psutil is not None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:
                pass
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if children:
            _, alive = psutil.wait_procs(children, timeout=5)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
