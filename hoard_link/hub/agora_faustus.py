"""Faustus workers whose workspace belongs to the PC hosting the Hub.

The main Faustus address follows placement. A Spark cannot execute Windows
paths, so an explicit worker launch starts a separate, headless PC runtime
with its own data and the selected Sparks endpoint. Protected endpoints use
the credential from the Faustus installation supplying the catalog.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .._process import popen_hidden
from .faustus_endpoint_key import read_endpoint_key

STUDIO = "estudio"
APPRENTICE = "faustus-aprendiz"
TUTOR_TOOL = "hub_agora_apprentice_instinct"
WORKER_APPROVAL_MODE = "auto"
REGISTRATION_TIMEOUT_S = 5 * 60
DECLARED_PATHS = {
    "type": "object", "additionalProperties": False,
    "properties": {
        **{key: {"type": "array", "maxItems": 32, "items": {"type": "string", "maxLength": 1024}}
           for key in ("read_paths", "write_paths")},
        "read_budget": {
            "type": "integer", "minimum": 0,
            "description": "Reads before a draft reminder. Omit for 15 or set 0 to disable. "
                           "Lecturas antes del aviso de borrador. Omite el valor para usar 15 o indica 0 para desactivar.",
        },
        "apps": {"type": "object", "maxProperties": 8,
                 "description": "App IDs and exact tool IDs for this production task. IDs de aplicaciones y herramientas de esta tarea.",
                 "additionalProperties": {"type": "array", "minItems": 1, "maxItems": 16,
                                          "items": {"type": "string", "minLength": 1, "maxLength": 100}}},
    },
}


class WorkerLaunchError(ValueError):
    def __init__(self, code: str, es: str, en: str, status: int = 409):
        self.code = code
        self.status = status
        self.messages = {"es": es, "en": en}
        super().__init__(f"{en} / {es}")

    def reply(self, lang: str | None) -> dict:
        return {"ok": False, "status": self.status, "code": self.code,
                "error": self.messages[lang] if lang in ("es", "en") else str(self),
                "error_i18n": self.messages}


def worker_data(console: Any, agent: str) -> Path:
    return Path(console.hub.config.data_dir).resolve() / "agora-console" / "faustus" / agent


def new_worker_workspace(console: Any, agent: str) -> str:
    parent = worker_data(console, agent) / "workspaces"
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="work-", dir=parent)


def owns_worker_workspace(console: Any, agent: str, workspace: str) -> bool:
    path = Path(workspace)
    return (path.parent == worker_data(console, agent) / "workspaces"
            and path.name.startswith("work-") and path.is_dir())


def tutor_instinct(console: Any, args: dict, actor: str) -> dict:
    """Teach Faustus with an attributed studio correction."""
    from . import agora_lessons, agora_ranks

    if console.space_id != STUDIO:
        return {"ok": False, "status": 404, "error": "Apprentice instincts belong to estudio."}
    action = args.get("action")
    data_dir = console.hub.config.data_dir
    if action == "list":
        return agora_lessons.list_lessons(data_dir)
    agora = console._agora()
    if actor == APPRENTICE or agora_ranks.level(agora, actor) < agora_ranks.LEVEL["agent"]:
        return {"ok": False, "status": 403, "error": "A studio agent or higher must teach the apprentice."}
    if action == "undo":
        return agora_lessons.undo(data_dir, args.get("token"), author=actor)
    message_id = args.get("message_id")
    if action not in ("remember", "forget") or type(message_id) is not int or not 1 <= message_id < 2**63:
        return {"ok": False, "status": 400, "error": "Choose remember or forget and a positive message_id."}
    message = agora.db.one(
        "SELECT m.author, m.kind, m.body, m.thread_id FROM messages m "
        "JOIN threads t ON t.id=m.thread_id WHERE m.id=? AND t.space_id=?", (message_id, STUDIO))
    if not message or message["kind"] == "system":
        return {"ok": False, "status": 404, "error": "Studio correction message not found."}
    if actor != message["author"]:
        return {"ok": False, "status": 403, "error": "Use a correction you authored in the studio."}
    record = None
    if action == "remember":
        trigger = " ".join(str(args.get("trigger") or "").split())
        correction = " ".join(message["body"].split())
        if not (1 <= len(trigger) <= agora_lessons.TRIGGER_LIMIT
                and 1 <= len(correction) <= agora_lessons.CORRECTION_LIMIT):
            return {"ok": False, "status": 400, "error": (
                f"Use a trigger of 1–{agora_lessons.TRIGGER_LIMIT} characters and a correction of "
                f"1–{agora_lessons.CORRECTION_LIMIT} characters. Publish longer lessons as library skills.")}
        record = {"author": actor, "message_id": message_id, "thread_id": message["thread_id"],
                  "trigger": trigger, "action": correction, "created": time.time()}
    token = agora_lessons.change(data_dir, message_id, record)
    receipt = {"ok": True, "message_id": message_id, "thread_id": message["thread_id"], "agent": APPRENTICE,
               "status": "remembered" if record else "forgotten", "undo": None}
    if token:
        receipt["undo"] = {"tool": TUTOR_TOOL, "arguments": {"space_id": STUDIO, "actor": actor,
                           "action": "undo", "token": token}}
    return receipt


def _declared_paths(raw: Any) -> dict[str, list[str]]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - DECLARED_PATHS["properties"].keys():
        raise ValueError("Use read_paths, write_paths, apps and read_budget. "
                         "/ Usa read_paths, write_paths, apps y read_budget.")
    out = {}
    for key in ("read_paths", "write_paths"):
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError("Use up to 32 folders per access type. / Indica hasta 32 carpetas por tipo de acceso.")
        paths = []
        for value in values:
            if not isinstance(value, str) or len(value) > 1024 or "\0" in value:
                raise ValueError("Use absolute local folders. / Indica carpetas locales con su ruta absoluta.")
            path = Path(value.strip())
            if not path.is_absolute() or path == Path(path.anchor) or path.drive.startswith("\\"):
                raise ValueError("Use absolute local folders. / Indica carpetas locales con su ruta absoluta.")
            path = Path(ntpath.normpath(path) if path.drive else os.path.normpath(path))
            if path == Path(path.anchor) or (path.exists() and not path.is_dir()):
                raise ValueError("Declare a folder instead of a file or drive. / Declara una carpeta concreta.")
            paths.append(str(path))
        out[key] = list(dict.fromkeys(paths))
    return out


def proposed_files(console: Any, task_id: int | None) -> dict:
    """Find revision-backed files recorded for this job, without opening any app."""
    from . import agora_orders

    empty = {"read_files": [], "read_paths": [], "asset_folders": []}
    if task_id is None:
        return empty
    if type(task_id) is not int or task_id <= 0:
        raise ValueError("Use a positive task ID. / Indica un identificador de tarea positivo.")
    agora = console._agora()
    if agora is None or not agora.db.one("SELECT id FROM tasks WHERE id=? AND space_id=?", (task_id, console.space_id)):
        raise ValueError("Task is not in this space. / La tarea no pertenece a este espacio.")
    job = agora.db.one("SELECT order_id, inputs FROM agora_order_jobs WHERE task_id=? AND space_id=? AND removed=0",
                       (task_id, console.space_id))
    if not job:
        return empty
    store = Path(agora_orders._store_dir(agora)).resolve()
    inputs = set(agora_orders._load(job["inputs"], []))
    files, folders, suggestions = set(), set(), set()
    rows = agora.db.query("SELECT id, path, stored, previews, previews_stored, status, supersedes FROM agora_pieces "
                          "WHERE order_id=? AND space_id=?", (job["order_id"], console.space_id))
    inputs.update({row["supersedes"] for row in rows if row["id"] in inputs and row["supersedes"]})
    for row in rows:
        if row["id"] not in inputs and row["status"] != "kept":
            continue
        for original in [row["path"], *agora_orders._load(row["previews"], [])]:
            if original and Path(original).is_absolute():
                suggestions.add(str(Path(original).parent))
        for value in [row["stored"], *agora_orders._load(row["previews_stored"], [])]:
            if not value:
                continue
            path = Path(value)
            if not path.is_file() or not path.resolve().is_relative_to(store):
                continue
            files.add(str(path))
            if path.parent.parent == store / "sites":
                folders.add(str(path.parent))
    return {"read_files": sorted(files), "read_paths": sorted(folders), "asset_folders": sorted(suggestions)}


def file_scope(workspace: str, declared: Any = None, proposal: dict | None = None) -> dict:
    roots = _declared_paths(declared)
    proposal = proposal or {}
    scope = {"workspace": workspace,
             "read_paths": list(dict.fromkeys(roots["read_paths"] + proposal.get("read_paths", []))),
             "write_paths": roots["write_paths"], "read_files": proposal.get("read_files", [])}
    declared = declared or {}
    if "read_budget" in declared:
        budget = declared["read_budget"]
        if type(budget) is not int or budget < 0:
            raise ValueError("read_budget must be a non-negative integer. "
                             "/ read_budget debe ser un número entero igual o mayor que cero.")
        scope["read_budget"] = budget
    apps = declared.get("apps", {})
    if (not isinstance(apps, dict) or len(apps) > 8
            or any(not isinstance(app, str) or not 1 <= len(app) <= 64
                   or not isinstance(tools, list) or not 1 <= len(tools) <= 16
                   or any(not isinstance(tool, str) or not 1 <= len(tool) <= 100 for tool in tools)
                   for app, tools in apps.items())):
        raise ValueError("Declare exact app and tool IDs. / Declara los identificadores exactos de aplicaciones y herramientas.")
    if apps:
        scope["apps"] = apps
    return scope


def worker_order(console: Any, task_id: int | None, apps: dict) -> int | None:
    """Resolve the active order job in the launch space before provisioning."""
    if task_id is not None:
        if type(task_id) is not int or task_id <= 0:
            raise WorkerLaunchError("invalid_task_id", "Indica un identificador de tarea positivo.",
                                    "Use a positive task ID.", status=400)
        agora = console._agora()
        if agora is not None:
            job = agora.db.one("SELECT order_id FROM agora_order_jobs WHERE task_id=? AND space_id=? AND removed=0",
                               (task_id, console.space_id))
            if job:
                return job["order_id"]
    if apps:
        es = (f"La tarea #{task_id} no es un trabajo de una orden de este espacio." if task_id is not None
              else "Indica un trabajo de una orden de este espacio para usar aplicaciones.")
        en = (f"Task #{task_id} is not a job in an order in this space." if task_id is not None
              else "Select a job from an order in this space to use app tools.")
        raise WorkerLaunchError(
            "worker_order_job_required",
            f"{es} Añade el trabajo al plan de la orden con hub_agora_plan_set y usa su task_id.",
            f"{en} Add the job to the order plan with hub_agora_plan_set and use its task_id.",
            status=400,
        )
    return None


def order_warning(task_id: int) -> dict[str, str]:
    return {
        "es": f"La tarea #{task_id} no es un trabajo de una orden de este espacio. "
              "Este trabajador no podrá registrar piezas.",
        "en": f"Task #{task_id} is not a job in an order in this space. "
              "This worker cannot register pieces.",
    }


def production_scope(console: Any, agent: str, scope: dict, task_id: int | None) -> dict:
    order_id = worker_order(console, task_id, scope.get("apps", {}))
    if task_id is not None:
        task = console._agora()._task_row(task_id)
        if order_id is not None or (not task["repo"] and not task["paths"]):
            return {**scope, "production": {"space_id": console.space_id, "task_id": task_id,
                                            "order_id": order_id, "agent": agent}}
    return scope


def require_app_modules(hub: Any, apps: dict) -> None:
    """Read module state without starting an app or enabling a module."""
    data_tools = {"data_ingest", "data_export", "data_export_undo", "data_undo"}
    requested = sorted(data_tools.intersection(apps.get("nightingale", ())))
    if not requested:
        return
    reply = hub.call_app("nightingale", "nightingale_modules", timeout=5.0)
    result = reply.get("result")
    modules = result.get("modules") if reply.get("ok") and isinstance(result, dict) else None
    data = None
    if isinstance(modules, list):
        data = next((module for module in modules if isinstance(module, dict) and module.get("id") == "data"), None)
    tools = ", ".join(requested)
    if data is not None and data.get("enabled") is True:
        return
    if data is not None and data.get("enabled") is False:
        raise ValueError(
            f"Nightingale's Data module is off ({tools}). Enable it in Nightingale before launching this worker. / "
            f"El módulo Datos de Nightingale está desactivado ({tools}). Actívalo en Nightingale antes de lanzar el trabajador."
        )
    raise ValueError(
        f"Could not check Nightingale's Data module ({tools}). Check that Nightingale is running and try again. / "
        f"No se pudo comprobar el módulo Datos de Nightingale ({tools}). Comprueba que Nightingale esté abierto y vuelve a intentarlo."
    )


def service(hub: Any, *, fresh: bool = False) -> dict[str, Any]:
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
    return hub._refresh_faustus() if fresh else hub.faustus_status()


def launch_service(hub: Any) -> dict[str, Any]:
    """Require a current Faustus health response before leaving a worker receipt."""
    target = {}
    try:
        if not getattr(getattr(hub, "config", None), "isolated", False):
            target = service(hub, fresh=True)
    except OSError:
        pass
    if target.get("manually_stopped"):
        raise WorkerLaunchError("faustus_manually_stopped", "Faustus está parado manualmente.",
                                "Faustus was stopped manually.")
    health = target.get("body")
    if (not target.get("reachable") or not target.get("url") or target.get("status", 200) != 200
            or not isinstance(health, dict) or health.get("service") != "faustus"):
        raise WorkerLaunchError("faustus_unreachable", "Faustus está parado (no responde).",
                                "Faustus is not responding.")
    return target


def _registration_seen(agora: Any, worker: Any) -> bool:
    return agora.db.one(
        "SELECT 1 FROM coordination_members m JOIN agents a ON a.id=m.agent "
        "WHERE m.space_id=? AND m.agent=? AND a.last_seen>=?",
        (agora.space_id, worker["agent_id"], worker["agora_start_at"]),
    ) is not None


def registration_details(worker: Any) -> dict[str, str]:
    minutes = REGISTRATION_TIMEOUT_S / 60
    es = f"El trabajador no se registró en el Ágora de este espacio en {minutes:g} minutos."
    en = f"The worker did not register in this Agora space within {minutes:g} minutes."
    if (code := worker["process_exit_code"]) is not None:
        es += f" El proceso terminó con código {code}."
        en += f" The process exited with code {code}."
    elif worker["state"] in ("failed", "unavailable") and not worker["pid"]:
        es += " El proceso del trabajador no llegó a iniciarse."
        en += " The worker process did not launch."
    return {"es": es, "en": en}


def _notify_registration_timeout(agora: Any, worker: Any) -> None:
    task_id = worker["agora_task_id"]
    if task_id is None:
        return
    with agora.db.tx():
        task = agora.db.one("SELECT thread_id FROM tasks WHERE id=? AND space_id=?", (task_id, agora.space_id))
        if not task or not task["thread_id"]:
            return
        thread_id = task["thread_id"]
        # The same receipt can be reconciled again after posting but before the console saves its state.
        prefix = f"{worker['agent_id']} ({worker['id'][:8]})"
        if agora.db.one("SELECT 1 FROM messages WHERE thread_id=? AND author='agora-console' "
                        "AND kind='system' AND substr(body,1,?)=?", (thread_id, len(prefix), prefix)):
            return
        details = registration_details(worker)
        message_id = agora._msg(thread_id, "agora-console", "system",
                                f"{prefix} · No arrancó. {details['es']}\n"
                                f"Did not start. {details['en']}",
                                [worker["launched_by"]] if worker["launched_by"] else [])
    agora._bump("agora.thread.message", {"thread_id": thread_id, "message_id": message_id,
                                        "author": "agora-console", "kind": "system"})


def reconcile_registrations(console: Any, *, now: float | None = None) -> None:
    """Observe first Agora activity independently of the runner's lifecycle."""
    now = time.time() if now is None else now
    with console._registration_lock:
        with console._db_lock, console._db() as db:
            pending = db.execute(
                "SELECT * FROM workers WHERE agora_start_state='pending' OR "
                "(agora_start_state='not_started' AND state IN ('starting','running','paused','unknown'))"
            ).fetchall()
        if not pending or (agora := console._agora()) is None or not hasattr(agora, "db"):
            return
        for worker in pending:
            previous = worker["agora_start_state"]
            if _registration_seen(agora, worker):
                state = "registered"
            elif previous == "not_started":
                continue
            elif worker["state"] in ("stopping", "stopped") or worker["outcome"] == "hub_restart_stopping":
                state = "cancelled"
            elif now >= worker["agora_start_at"] + REGISTRATION_TIMEOUT_S:
                state = "not_started"
                _notify_registration_timeout(agora, worker)
            else:
                continue
            with console._db_lock, console._db() as db:
                db.execute("UPDATE workers SET agora_start_state=? WHERE id=? AND agora_start_state=?",
                           (state, worker["id"], previous))


def prepare(console: Any, agent: str, model: dict, effort: str, *,
            declared_paths: Any = None, task_id: int | None = None,
            target: dict | None = None) -> tuple[dict | None, str]:
    """Create an independent native project/session when no local controller owns one."""
    root, python = console.hub.faustus_runtime.installation()
    if root is None or python is None:
        return None, "Faustus installation is unavailable for the PC worker."
    root = Path(root)
    agora = console._agora()
    space = agora.space_store.get(console.space_id) if hasattr(agora, "space_store") else None
    configured_workspace = (space or {}).get("workspace")
    workspace = str(configured_workspace or root.parent)
    target = target if target is not None else service(console.hub)
    if not target.get("reachable"):
        return None, str(target.get("error") or "The placed Faustus service is unavailable.")
    base = str(target.get("url") or "").rstrip("/")
    endpoint_id = str(model["endpoint_id"])
    runtime = None
    warning = None
    dedicated = target.get("remote") or console.space_id == STUDIO
    if not dedicated and (declared_paths is not None or task_id is not None):
        return None, "File scope requires a dedicated PC worker. / El ámbito de ficheros requiere un trabajador aislado en el PC."
    if not dedicated and ntpath.splitdrive(workspace)[0] and not Path(workspace).is_dir():
        return None, "The space's PC workspace directory is unavailable."
    if dedicated:
        scope = file_scope(workspace, declared_paths, proposed_files(console, task_id))
        scope = production_scope(console, agent, scope, task_id)
        if task_id is not None and (scope.get("production") or {}).get("order_id") is None:
            warning = order_warning(task_id)
        require_app_modules(console.hub, scope.get("apps", {}))
        status, endpoints = console._faustus_session_request("GET", "/api/model-endpoints", base_url=base)
        endpoint = next((row for row in endpoints if row.get("id") == endpoint_id and row.get("is_enabled")), None) if status == 200 and isinstance(endpoints, list) else None
        if not endpoint or not str(endpoint.get("base_url") or "").startswith(("http://", "https://")):
            return None, "The selected Sparks endpoint is unavailable for the PC worker."
        api_key = ""
        if endpoint.get("has_key"):
            placement = console.hub.placement if target.get("remote") else None
            api_key = read_endpoint_key(root, python, endpoint_id, endpoint["base_url"], placement=placement)
            fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
            if not api_key or fingerprint != endpoint.get("api_key_fingerprint"):
                return None, (
                    "No matching credential for the selected endpoint. Add or update its API key in "
                    "Faustus Settings → Added Models. / No se encontró la credencial del endpoint seleccionado. "
                    "Añade o actualiza su clave API en Ajustes de Faustus → Modelos añadidos."
                )
        workspace = scope["workspace"] = new_worker_workspace(console, agent)
        runtime = start_pc(console, agent, root, python, endpoint["base_url"], model["model_id"], file_scope=scope, api_key=api_key)
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
        thread_ids = [console.thread_id]
        if task_id is not None:
            task = agora.db.one("SELECT thread_id FROM tasks WHERE id=? AND space_id=?", (task_id, console.space_id))
            if task and task["thread_id"] and task["thread_id"] not in thread_ids:
                thread_ids.append(task["thread_id"])
        status, saved = request("PUT", "/api/coordination/" + session, {"connector_id": connector["id"],
            "actor": agent, "space_id": console.space_id, "active": True,
            "operations": ["poll", "detail", "task", "board", "heartbeat", "add", "claim", "checkpoint", "post", "submit", "review", "done", "lock", "unlock"],
            "resources": [], "repos": [], "thread_ids": thread_ids})
        binding = saved.get("binding") if isinstance(saved, dict) else None
        if status != 200 or not binding or binding.get("actor") != agent or binding.get("project_id") != project["id"]:
            raise RuntimeError("Faustus did not confirm the worker coordination identity")
        prepared = {"session_id": session, "project_id": project["id"], "workspace": workspace,
                    "url": base, "runtime": runtime, "coordination_threads": thread_ids}
        if warning:
            prepared["warning_i18n"] = warning
        return prepared, ""
    except Exception:
        if runtime:
            stop_pc(runtime)
        raise


def reactivate_apprentice_binding(console: Any, base: str, session: str) -> None:
    """Continuing an older apprentice chat hands its identity back to that chat."""
    path = "/api/coordination/" + session
    status, data = console._faustus_session_request("GET", path, base_url=base)
    binding = data.get("binding") if isinstance(data, dict) else None
    if status != 200 or not isinstance(binding, dict) or binding.get("actor") != APPRENTICE:
        raise RuntimeError("Faustus did not confirm the apprentice chat binding.")
    if binding.get("active"):
        return
    fields = {key: binding[key] for key in ("id", "connector_id", "space_id", "actor",
              "operations", "resources", "repos", "thread_ids")}
    fields.update(active=True, expected_revision=binding["revision"])
    status, data = console._faustus_session_request("PUT", path, fields, base_url=base)
    confirmed = data.get("binding") if isinstance(data, dict) else None
    if status != 200 or not isinstance(confirmed, dict) or not confirmed.get("active"):
        raise RuntimeError("Faustus could not reactivate the apprentice chat binding.")


def start_pc(console: Any, agent: str, root: Path, python: str, endpoint: str, model: str, *,
             file_scope: dict, api_key: str = "", resume: bool = False) -> dict:
    data = worker_data(console, agent)
    if not owns_worker_workspace(console, agent, file_scope["workspace"]):
        raise ValueError("A private worker workspace is required. / Se requiere una carpeta de trabajo privada.")
    production = file_scope.get("production")
    if production and (production["space_id"] != console.space_id or production["agent"] != agent):
        raise ValueError("Production task does not match this worker. / La tarea de producción no corresponde a este trabajador.")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = console._runner_env()
    env["FAUSTUS_AGORA_WORKER_SPACE"] = console.space_id
    env.pop("FAUSTUS_AGORA_WORKER_TASK_THREAD", None)
    if production:
        task = console._agora().db.one("SELECT thread_id FROM tasks WHERE id=? AND space_id=?",
                                       (production["task_id"], console.space_id))
        if task and task["thread_id"]:
            env["FAUSTUS_AGORA_WORKER_TASK_THREAD"] = str(task["thread_id"])
    env["FAUSTUS_APP_ROOT"] = str(root)
    secret_paths = [str(Path(console.hub.config.data_dir) / "hub.json"),
                    *(str(data / name) for name in ("app.db", "app.db-wal", "app.db-shm"))]
    secret_paths.extend(app.token_file for app in getattr(console.hub, "apps", []) if app.token_file)
    env["FAUSTUS_AGORA_PROTECTED_PATHS"] = json.dumps(secret_paths)
    env["FAUSTUS_AGORA_HUB_PYTHON"] = sys.executable
    env["FAUSTUS_AGORA_HUB_ROOT"] = str(Path(__file__).resolve().parents[2])
    # On resume, leave the original endpoint and its encrypted credential in place.
    command = [str(python), "-X", "utf8", str(root / "integrations" / "harbor" / "serve_headless.py"),
               "--port", str(port), "--data", str(data), "--endpoint-url", "" if resume else endpoint,
               "--model", model, "--api-key", "",
               "--hub-url", str(console.hub.config.url),
               "--settings", json.dumps({"tool_approval_mode": WORKER_APPROVAL_MODE}),
               "--agora-file-scope", json.dumps(file_scope)]
    if console.space_id == STUDIO:
        command.extend(["--agora-studio", agent])
    with (data / "server.log").open("a", encoding="utf-8") as log:
        process = popen_hidden(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    from . import procs
    from .contract import _get
    runtime = {"url": base, "process": process, "data": str(data), "endpoint": endpoint,
               "pid": process.pid, "created_at": procs.proc_info(process.pid).created_at,
               "file_scope": file_scope, "tool_approval_mode": WORKER_APPROVAL_MODE}
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"PC Faustus exited during startup ({process.returncode}); see {data / 'server.log'}")
            status, body = _get(base + "/api/health", "", 1.0)
            if status == 200 and isinstance(body, dict) and body.get("service") == "faustus":
                status, servers = console._faustus_session_request("GET", "/api/mcp/servers", base_url=base)
                if status == 200 and any(row.get("id") == "agora" and row.get("status") == "connected" for row in servers if isinstance(row, dict)):
                    if api_key:
                        status, saved = console._faustus_session_request(
                            "PATCH", "/api/model-endpoints/headless", {"api_key": api_key}, base_url=base)
                        if (status != 200 or not isinstance(saved, dict) or saved.get("id") != "headless"
                                or not saved.get("is_enabled")):
                            raise RuntimeError(
                                "PC Faustus could not store the endpoint credential. / "
                                "Faustus no ha podido guardar la credencial del endpoint.")
                    return runtime
            time.sleep(0.5)
        raise RuntimeError(f"PC Faustus startup timed out; see {data / 'server.log'}")
    except Exception:
        stop_pc(runtime)
        raise


def stop_pc(runtime: dict) -> None:
    process = runtime.get("process")
    if process is None:
        # After a Hub restart only the verified PID and creation time remain.
        from . import procs
        if runtime.get("created_at") is not None:
            procs.terminate_tree(runtime["pid"], created_at=runtime["created_at"])
        return
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
