"""Background link checks and their bounded, private history."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
import json
import math
import re
import threading
import time
from uuid import uuid4

from ..atomic import read_json, write_json_atomic
from .facets import Facet, Reply
from .link_app_probes import app_links, probe_registered_app
from .link_inventory import LINKS
from .link_probes import FAILED, RESPONDS, STOPPED, UNCHECKED, LinkProbes, Observation

TOOL_NAME = "hub_link_check"
ROUTE = "/api/link-check"
PROBE_TIMEOUT_S = 8.0
RUN_TIMEOUT_S = 120.0
HISTORY_LIMIT = 20
STATES = (RESPONDS, FAILED, STOPPED, UNCHECKED)
LANGUAGES = {
    "es": ("responde", "no responde", "parado", "no comprobado"),
    "en": ("responds", "not responding", "stopped", "not checked"),
}

# Evidence codes stay stable for agents. These messages explain gaps without
# copying an exception, app response, access token, file path or personal data.
REASONS = {
    "app_missing": ("La aplicación no figura en el registro del Hub.", "The app is missing from the Hub registry."),
    "manually_stopped": ("El Hub tiene registrada una parada manual.", "The Hub records a manual stop."),
    "no_listener": ("El registro apunta a un puerto local sin servicio en escucha.", "The registry points to a local port without a listener."),
    "no_running_service": ("El servicio remoto está inactivo.", "The remote service is inactive."),
    "remote_unreachable": ("La comprobación remota no obtuvo una respuesta válida.", "The remote check did not get a valid response."),
    "no_http_response": ("La sonda HTTP no obtuvo respuesta.", "The HTTP probe received no response."),
    "http_error": ("El servidor devolvió un código HTTP de error.", "The server returned an HTTP error status."),
    "missing_tools": ("El catálogo carece de herramientas exigidas por el enlace.", "The catalogue lacks tools required by the link."),
    "invalid_tool_declaration": ("La lista de herramientas declaradas no es válida.", "The declared tool list is invalid."),
    "no_tool_declaration": ("El catálogo respondió. Falta una lista de herramientas declaradas en el manifiesto.", "The catalogue answered. The manifest has no declared tool list."),
    "partial_tool_declaration": ("Las herramientas declaradas están servidas. La declaración es parcial.", "The declared tools are served. The declaration is partial."),
    "tool_catalogue_matches": ("Coinciden las herramientas declaradas y las servidas.", "The declared and served tools match."),
    "tool_catalogue_drift": ("El catálogo difiere de las herramientas declaradas.", "The catalogue differs from the declared tools."),
    "no_http_tool_contract": ("La aplicación carece del contrato HTTP de herramientas.", "The app has no HTTP tool contract."),
    "no_health_probe": ("Falta una herramienta de salud de solo lectura que se pueda llamar sin argumentos.", "A read-only health tool callable without arguments is unavailable."),
    "app_proxy_health": ("La herramienta de salud respondió a través del proxy del Hub.", "The health tool answered through the Hub proxy."),
    "invalid_catalogue": ("La respuesta no tiene el formato de catálogo.", "The response lacks the catalogue format."),
    "catalogue_matches": ("El catálogo respondió y contiene las herramientas exigidas.", "The catalogue answered and contains the required tools."),
    "shared_service_catalogue": ("El catálogo de Funes servido por Talos contiene las herramientas de transcripción.",
                                 "The Funes catalogue served by Talos contains the transcription tools."),
    "mail_fresh": ("La última pasada se completó dentro del intervalo.", "The last successful pass is within its interval."),
    "mail_on_demand": ("Se registró una pasada correcta. La pasarela funciona bajo demanda.",
                       "A successful pass is recorded. The gateway runs on demand."),
    "mail_stale": ("La última pasada correcta supera el intervalo configurado.", "The last successful pass is older than the configured interval."),
    "no_event_receipt": ("El bus aún no tiene eventos de ese origen.", "The bus has no events from that source."),
    "no_foreman_heartbeat": ("Falta un latido explícito del capataz.", "An explicit foreman heartbeat is unavailable."),
    "legacy_workers": ("Hay trabajadores sin una marca de inicio verificable.", "Some workers lack a verifiable start timestamp."),
    "no_console_snapshot": ("Falta una base ya abierta de la consola.", "No active console database is available."),
    "gpu_inventory_unprobed": ("Falta una observación previa de la GPU.", "No previous GPU inventory is available."),
    "group_counts": ("La evidencia reúne los estados de las comprobaciones del grupo.", "The evidence counts the group's individual checks."),
    "operator_excluded": ("No comprobado por orden de la persona.", "Excluded by the operator."),
    "endpoint_not_verified": ("La dirección registrada queda fuera de la sonda.", "The registered endpoint is outside the probe boundary."),
    "remote_state_unknown": ("Falta una observación reciente del servicio remoto.", "A recent remote service observation is unavailable."),
    "listener_state_unknown": ("El estado local no está disponible por lectura pasiva.", "Local state is unavailable through passive observation."),
    "health_route_not_reviewed": ("La ruta de salud no figura en la lista de lectura.", "The health route is outside the read allowlist."),
    "placement_moving": ("Hay un traslado en curso.", "Placement is changing."),
    "not_placed_remotely": ("Enlace remoto inactivo en la colocación actual.", "Remote link inactive in the current placement."),
    "backend_cache_unknown": ("Falta una observación reciente del motor.", "A recent backend observation is unavailable."),
    "fleet_no_snapshot": ("Falta una instantánea previa de la flota.", "No previous fleet snapshot is available."),
    "fleet_no_receipt": ("Falta un acuse registrado del lector o vigilante.", "No recorded reader or watcher receipt is available."),
    "brain_no_receipt": ("Falta un acuse del puente al Cerebro.", "No Brain bridge receipt is available."),
    "mail_no_pass": ("Falta una pasada de correo registrada.", "No successful mail pass is recorded."),
    "mail_since_start": ("Sin pasada desde el arranque (hace {uptime_s} s).", "No pass since startup ({uptime_s} s ago)."),
    "mail_first_pass_overdue": ("La primera pasada supera el intervalo desde el arranque.",
                                "The first pass is overdue since startup."),
    "mail_reader_scan": ("La pasarela completó la última lectura del lector.", "The gateway completed the reader's latest scan."),
    "mail_reader_scan_failed": ("Falló la última lectura del lector en la pasarela.", "The reader's latest gateway scan failed."),
    "mail_reader_claim": ("La pasarela guarda un acuse del lector.", "The gateway has a recorded reader receipt."),
    "mail_caller_used": ("El Hub registró una identificación correcta del llamante.", "The Hub recorded successful caller identification."),
    "mail_caller_declared": ("Hay una declaración de llamantes. Falta registrar su uso desde el arranque.",
                             "A caller declaration exists. No use is recorded since startup."),
    "mail_caller_unknown": ("Faltan la declaración y el acuse del llamante.", "The caller declaration and usage receipt are unavailable."),
    "dojo_exam_count": ("Recuento de exámenes de la última lista del Dojo.", "Exam count from the latest Dojo list."),
    "dojo_no_receipt": ("Falta un recuento del Dojo de esta instalación.", "No Dojo count is recorded for this installation."),
    "quota_measured": ("Hay una medida de cuota vigente.", "A current quota measurement is available."),
    "quota_stale_or_unknown": ("Las medidas de cuota registradas están caducadas o son desconocidas.",
                               "Recorded quota measurements are stale or unknown."),
    "quota_no_receipt": ("Falta una medida de cuota registrada.", "No quota measurement is recorded."),
    "vendor_copies_unmatched": ("Hay copias que no coinciden con una versión publicada.",
                                "Some copies match no published version."),
    "vendor_history_unavailable": ("El historial disponible no basta para clasificar todas las copias.",
                                   "The available history cannot classify all copies."),
    "deployment_unknown": ("Falta el registro de despliegue o la revisión local.", "A deployment record or local revision is unavailable."),
    "purchase_matching": ("El cotejo de compras requiere datos de compras o una acción.", "Matching purchases requires purchase data or an action."),
    "opens_home": ("La operación de apertura arranca Home Hoard.", "The open operation starts Home Hoard."),
    "piece_content": ("La sonda disponible requiere abrir una pieza.", "The available probe requires opening an artifact."),
    "slicer": ("La prueba del laminador requiere ejecutarlo y generar una salida.", "Testing the slicer requires running it and generating output."),
    "app_to_hub_post": ("Falta una sonda de lectura propia del acceso de app al Hub.", "The app-to-Hub path lacks a dedicated read probe."),
    "reader_status": ("Falta una lectura pasiva del lector de correo.", "A passive mail reader status is unavailable."),
    "no_consumer": ("No hay un consumidor con sonda.", "There is no consumer with a probe."),
    "caller_handshake": ("La comprobación requiere las credenciales de un llamante instalado.", "The check requires the credentials of an installed caller."),
    "mail_send": ("Solo se puede comprobar el envío enviando un correo.", "Delivery can only be tested by sending a message."),
    "notification_content": ("El historial disponible contiene avisos de la persona.", "The available history contains the operator's notifications."),
    "maintenance_mirror": ("Falta una lectura del espejo de mantenimiento.", "The maintenance mirror lacks a read probe."),
    "cook_workflow": ("Falta una lectura de los enlaces de Cook.", "The Cook links lack a read probe."),
    "dataset_workflow": ("El enlace requiere conectar un conjunto de datos.", "The link requires connecting a dataset."),
    "maxwell_peer": ("Maxwell carece de una lectura del enlace hacia Vulcan.", "Maxwell lacks a read probe for its link to Vulcan."),
    "isolation_launch": ("La comprobación incluye el arranque y la limpieza de procesos.", "The check involves starting and cleaning up processes."),
    "studio_session": ("La comprobación requiere una sesión de Studio.", "The check requires a Studio session."),
    "dojo": ("Falta una operación de lectura dedicada del Dojo.", "The Dojo lacks a dedicated read operation."),
    "worker_scope": ("Falta una sonda de lectura del alcance de los trabajadores.", "Worker scope lacks a dedicated read probe."),
    "quota_content": ("Las lecturas disponibles incluyen datos de cuentas.", "The available reads include account information."),
    "order_content": ("Este comprobador se limita a datos operativos sin contenido de órdenes.", "This checker is limited to operational metadata without order contents."),
    "inbox_content": ("Las lecturas disponibles incluyen mensajes y revisiones.", "The available reads include messages and reviews."),
    "integration_run": ("Comprobar la integración requiere un expediente de ejecución.", "Checking integration requires an execution record."),
    "production_relay": ("El relevo se verifica en su orden de producción.", "The handoff is checked in its production order."),
    "no_members": ("No hay servicios o aplicaciones registrados para esta sonda.",
                   "No services or apps are registered for this probe."),
    "no_declared_apps": ("No hay declaraciones de relay comprobables.", "There are no relay declarations to check."),
    "run_timeout": ("Se agotó el tiempo global antes de iniciar la sonda.", "The run deadline elapsed before this probe began."),
    "probe_timeout": ("Se agotó el tiempo de la sonda.", "The probe deadline elapsed."),
    "workers_busy": ("Las sondas que superaron su plazo siguen cerrándose.", "Probes that exceeded their deadline are still winding down."),
    "probe_error": ("La sonda no pudo obtener evidencia válida.", "The probe could not obtain valid evidence."),
    "invalid_config": ("La configuración del comprobador no es válida.", "The checker configuration is invalid."),
    "isolated_hub": ("La sonda HTTP se omite en una instancia aislada.", "HTTP probing is skipped in an isolated instance."),
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class LinkCheckFacet(Facet):
    id = "link_check"

    def __init__(self, hub, *, probe_timeout=PROBE_TIMEOUT_S, run_timeout=RUN_TIMEOUT_S):
        super().__init__(hub)
        self._data = Path(hub.config.data_dir)
        self._history_path = self._data / "link-check-history.json"
        history = read_json(self._history_path, {})
        history = history if isinstance(history, dict) else {}
        runs = history.get("runs")
        self._runs = {
            run["run_id"]: run for run in (runs[-HISTORY_LIMIT:] if isinstance(runs, list) else [])
            if isinstance(run, dict) and isinstance(run.get("run_id"), str) and run.get("phase") == "completed"
        }
        failures = history.get("failures")
        self._failures = failures if isinstance(failures, dict) else {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(4)
        self._closed = threading.Event()
        self._thread = None
        self._active = None
        self._probe_timeout = probe_timeout
        self._run_timeout = run_timeout

    @classmethod
    def tools(cls):
        return [{
            "name": TOOL_NAME,
            "description": "Start passive family link, app catalogue and proxy checks or poll by run_id. Comprueba enlaces, catálogos y proxy de las apps en segundo plano y consulta la pasada con run_id.",
            "inputSchema": {"type": "object", "properties": {
                "run_id": {"type": "string", "description": "Run ID to poll. Omit to start or join a run."},
                "lang": {"type": "string", "enum": ["es", "en"]},
            }, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }]

    def handlers(self):
        return {TOOL_NAME: self.call}

    def call(self, args):
        lang = self._language(args.get("lang"))
        if "run_id" in args:
            return self.status(args["run_id"], lang)
        with self._lock:
            if self._closed.is_set():
                return {"ok": False, "status": 503, "error": "Link checker is closing / Comprobador en cierre"}
            if self._active is not None:
                return self._present(self._active, lang)
            run = {"ok": True, "run_id": uuid4().hex, "phase": "running", "started_at": _now()}
            self._active = run
            self._thread = threading.Thread(target=self._run, args=(run,), name="hub-link-check", daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._active = self._thread = None
                return {"ok": False, "status": 503, "error": "Could not start check / No se pudo iniciar la comprobación"}
            return self._present(run, lang)

    def _language(self, lang):
        if lang in ("es", "en"):
            return lang
        return "en" if getattr(self.hub.config, "language", None) == "en" else "es"

    def get(self, req):
        if req.path != ROUTE:
            return None
        if not req.agent():
            return Reply({"ok": False, "error": "Hub bearer token required / Se requiere el token del Hub"}, 401)
        return self.status(req.q("run_id"), self._language(req.q("lang")))

    def status(self, run_id, lang):
        with self._lock:
            if run_id is None:
                run = self._active or next(reversed(self._runs.values()), None)
            elif isinstance(run_id, str):
                run = self._active if self._active and run_id == self._active["run_id"] else self._runs.get(run_id)
            else:
                run = None
            if run is None:
                return {"ok": False, "status": 404, "error": "Run not found / Pasada no encontrada"}
            return self._present(run, lang)

    @staticmethod
    def _present(run, lang):
        result = deepcopy(run)
        states = dict(zip(STATES, LANGUAGES[lang]))
        for row in result.get("links", []):
            row["status"] = states[row["state"]]
            evidence = row["evidence"]
            outcome = evidence["outcome"]
            suffixes = {
                "no_health_probe": ("sin sonda", "no probe"),
                "manually_stopped": ("parada manual", "manual stop"),
                "no_listener": ("sin iniciar", "not started"),
            }
            suffix = suffixes.get(outcome)
            local_text = suffix[lang == "en"] if suffix else None
            remote_text = None
            if evidence.get("location") == "remote":
                remote_text = "on another machine" if lang == "en" else "en otra máquina"
            details = [text for text in (local_text, remote_text) if text]
            if details:
                row["status"] += ": " + ", ".join(details)
            reason = REASONS.get(outcome)
            if reason:
                detail = reason[lang == "en"]
                if outcome == "mail_since_start":
                    detail = detail.format(uptime_s=evidence["metrics"]["uptime_s"])
                evidence["detail"] = detail
            if outcome == "vendor_copies_behind":
                metrics = evidence["metrics"]
                lo, hi = metrics["min_versions_behind"], metrics["max_versions_behind"]
                versions = str(lo) if lo == hi else f"{lo}–{hi}"
                if lang == "en":
                    evidence["detail"] = f"Published copies are {versions} {'version' if hi == 1 else 'versions'} behind."
                else:
                    evidence["detail"] = f"Copias publicadas atrasadas {versions} {'versión' if hi == 1 else 'versiones'}."
        return result

    def _settings(self):
        path = self._data / "link-check.json"
        try:
            with path.open(encoding="utf-8-sig") as file:
                settings = json.load(file)
        except FileNotFoundError:
            return ()
        excluded = settings.get("excluded_apps", []) if isinstance(settings, dict) else None
        if not isinstance(excluded, list) or any(
            not isinstance(app, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", app.strip().lower())
            for app in excluded
        ):
            raise ValueError("Invalid exclusion configuration")
        return tuple(app.strip().lower() for app in excluded)

    def _run(self, run):
        deadline = time.monotonic() + self._run_timeout
        try:
            probes = LinkProbes(self.hub, self._settings())
        except (ValueError, OSError):
            probes = None
        results = {}
        rows = []
        failures = self._failures.copy()
        for link in (*LINKS, *app_links(self.hub.apps)):
            if self._closed.is_set():
                return
            if link.probe == "excluded:baird":
                observation = Observation(UNCHECKED, "link-check.json", "operator_excluded")
            elif probes is None:
                observation = Observation(UNCHECKED, "link-check.json", "invalid_config")
            elif time.monotonic() >= deadline:
                observation = Observation(UNCHECKED, "", "run_timeout")
            else:
                observation = self._execute(probes, link.probe, deadline, results)
            results[link.id] = observation
            checked_at = _now()
            if observation.state == FAILED:
                failures.setdefault(link.id, checked_at)
            elif observation.state in (RESPONDS, STOPPED) and observation.outcome != "mail_since_start":
                failures.pop(link.id, None)
            metrics = {
                key: value for key, value in (observation.metrics or {}).items()
                if value is None or type(value) in (int, bool) or (type(value) is float and math.isfinite(value))
            }
            rows.append({
                **asdict(link),
                "inventory_id": link.inventory_id or link.id,
                "state": observation.state,
                "checked_at": checked_at,
                "failure_since": failures.get(link.id),
                "evidence": {"called": observation.called, "outcome": observation.outcome,
                             "metrics": metrics, "scope": observation.scope,
                             **({"location": observation.location} if observation.location else {})},
            })
        summary = {state: sum(row["state"] == state for row in rows) for state in STATES}
        finished = {**run, "phase": "completed", "finished_at": _now(),
                    "summary": {"total": len(rows), **summary}, "links": rows, "history_saved": True}
        with self._lock:
            if self._closed.is_set():
                return
            runs = [*self._runs.values(), finished][-HISTORY_LIMIT:]
        saved_runs = [{**row, "history_saved": True} for row in runs]
        try:
            write_json_atomic(self._history_path, {"runs": saved_runs, "failures": failures})
        except OSError:
            runs[-1] = {**finished, "history_saved": False}
        else:
            runs = saved_runs
        with self._lock:
            self._failures = failures
            self._runs = {row["run_id"]: row for row in runs}
            self._active = None

    def _execute(self, probes, probe, run_deadline, results):
        deadline = min(run_deadline, time.monotonic() + self._probe_timeout)
        if not self._slots.acquire(blocking=False):
            return Observation(UNCHECKED, "", "workers_busy")
        replies = Queue(maxsize=1)

        def observe():
            try:
                if probe.startswith(("registered_catalogue:", "registered_health:")):
                    value = probe_registered_app(probes, probe, deadline)
                else:
                    value = probes.run(probe, deadline, results.copy())
            except Exception:
                value = Observation(FAILED, probe, "probe_error")
            finally:
                self._slots.release()
            replies.put_nowait((time.monotonic(), value))

        worker = threading.Thread(target=observe, name="hub-link-probe", daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self._slots.release()
            return Observation(FAILED, probe, "probe_error")
        try:
            observed_at, result = replies.get(timeout=max(0, deadline - time.monotonic()))
            if observed_at <= deadline:
                return result
        except Empty:
            pass
        return Observation(FAILED, probe, "probe_timeout")

    def close(self):
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
