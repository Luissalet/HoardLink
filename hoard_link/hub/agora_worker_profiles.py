"""Space-owned briefing material shared by the console's worker runners."""
from __future__ import annotations

import json
from typing import Any

ROUTE = "/api/agora/console/space-profile"
TOOL = "hub_agora_space_worker_profile"

TEMPLATES = {
    "build": {
        "es": {"label": "Construir", "hint": "Qué hay que cambiar y cómo se dará por bueno"},
        "en": {"label": "Build", "hint": "What to change and how to verify it"},
        "instructions": "Implement the requested outcome through its normal entry points. Verify it with the repository tests and a real call. In your submission include the test path, what you exercised live, and anything still unverified.",
    },
    "probe": {
        "es": {"label": "Sondear", "hint": "Qué caso hay que recorrer y qué piezas debe entregar"},
        "en": {"label": "Probe", "hint": "What scenario to run and what artifacts to hand off"},
        "instructions": "Run the scenario through the available tools. For each station report input, output, how you inspected it and any reproducible obstacle. Keep code changes out of a probe unless the assignment explicitly asks for them.",
    },
    "check": {
        "es": {"label": "Comprobar piezas", "hint": "Qué piezas hay que medir y de qué detalles guardar recorte"},
        "en": {"label": "Check pieces", "hint": "Which pieces to measure and which details to save crops of"},
        "instructions": (
            "In a piece check you only measure and keep crops; the visual judgement is the reviewing master's. "
            "Measure with tools: pixel differences per box, counts, fingerprints, dimensions and validation against "
            "the order. Report every result as «medido» / MEASURED with the tool, the box or detail and the figure. "
            "To say what changed, open the current and previous revisions and measure the same detail in equivalent "
            "views. Cite both revisions and the box and figure of each change. If the previous detail cannot be "
            "checked, write «sin referencia anterior» or 'no previous reference' in that row and report only the "
            "measurements of the current revision. "
            "For each doubtful zone save the crop with inspect_image, enlarged, from the piece and from the reference, "
            "and list it as «recorte» / CROP with its saved path and the box it covers, without describing or "
            "judging it. If the tool cannot save the crop, write the error literally and where the crop ended up. "
            "Do not write that something is right, wrong, better or worse, and do not state that something is present "
            "or absent in what you see; there is no «visto, sin medida» / 'seen, not measured' row: a zone "
            "without a measurement only has its crop, for the master. Do not state the state of a review you have not opened. "
            "When an order task is linked, use its revision_comparisons from hub_agora_job. "
            "Report the checks in the task thread. A measurement or a crop neither approves nor returns a piece: "
            "leave decisions to the person."
        ),
    },
    "review": {
        "es": {"label": "Revisar", "hint": "Qué entrega hay que revisar y qué debe demostrar"},
        "en": {"label": "Review", "hint": "Which submission to review and what it must demonstrate"},
        "instructions": "Inspect the submitted commits and run independent checks in an isolated worktree. Vote approve or changes as yourself on the submission revision you actually inspected. Include reproducible findings and live checks. A review needs a vote, not a new submission or edits to the author's worktree.",
    },
    "integrate": {
        "es": {"label": "Integrar", "hint": "Qué entregas aprobadas hay que integrar y dónde"},
        "en": {"label": "Integrate", "hint": "Which approved submissions to integrate and where"},
        "instructions": "Verify the approved revision and commits, take the repository merge lock, and check the target branch and existing changes. Integrate only what has been assigned. Verify the integrated result and record the final hashes and any differences in the task. Never push.",
    },
}

STUDIO = {
    "rules": (
        "Usa tu propia identidad y el espacio estudio en el Ágora. Empieza con hb, inbox y board. "
        "Lee CRITERIOS.md antes de MEMORY.md y sigue AGENTS.md y las skills del repositorio. Reclama la tarea "
        "y las rutas antes de editar. Trabaja en un worktree aislado, con rama; evita cambios ajenos, enlaces "
        "simbólicos y junctions. Haz commits pequeños en inglés como Luissalet <luissalet@users.noreply.github.com>, "
        "sin trailers. Nunca hagas push ni integres en main salvo encargo explícito de integración. Añadir nunca "
        "quita funciones. Interfaz en castellano de España y en inglés, sin eslóganes ni terminal para la persona. "
        "Expón herramientas breves para agentes y deshacer en las que escriben. No metas artefactos de QA en los "
        "repositorios. Prueba la suite afectada y llama en vivo por la vía normal de un agente; mira el resultado "
        "con navegador sin ventana (Brave, o Chrome por Playwright con el canal chrome; nunca el escritorio). "
        "Sin MCP, llama a las herramientas del Hub con E:\\Development\\IA\\Hoards\\Faustusables\\Hoard Hub\\hub.cmd "
        "por su ruta absoluta desde cualquier carpeta. Usa otro puerto y datos de prueba fuera del repo. No pares ni reinicies el Hub "
        "8810, servicios ajenos ni modelos. Las Sparks sirven el LLM común; respeta los bloqueos y leases de GPU "
        "de CRITERIOS. Actualiza la documentación. Al entregar, declara hashes, pruebas y pendientes reales en el "
        "hilo de la tarea; consulta cuota antes de pedir revisión y apunta cada falta concreta en el hilo 4006."
    ),
    "context": (
        "Estudio de producción dirigido por claude y codex-astra. Los agentes encadenan puestos de investigación, "
        "texto, presentación, producción visual, software, web y extensión del ecosistema. El expediente y cada "
        "pieza conservan origen, decisiones y detalles obligatorios. Verifica cada relevo con una pieza que abra "
        "y devuelve los defectos al puesto que los introdujo, con pasos, vista o pose reproducibles. La persona decide "
        "en la puerta qué conserva. El plan y las primeras sondas están en el hilo 3995."
    ),
    "folders": [r"E:\Development\IA", r"E:\Development\IA\Projects\estudio", r"E:\Development\IA\Hoards\Faustusables"],
    "repositories": [
        r"E:\Development\IA\Hoards\Faustusables\Hoard Hub",
        r"E:\Development\IA\Hoards\Faustusables\Writers hoard desktop",
        r"E:\Development\IA\Hoards\Faustusables\Prospero's Hoard",
        r"E:\Development\IA\Hoards\Faustusables\Vulcan's Hoard",
        r"E:\Development\IA\Hoards\Faustusables\Vitruvius's Hoard",
        r"E:\Development\IA\Hoards\Faustusables\Mercator's Hoard",
    ],
}

FIELDS = ("rules", "context", "folders", "repositories", "auto_review")
REVIEW_RUNNER = "review_runner"
REVIEW_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "default")


def init(db: Any) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS worker_profile (id INTEGER PRIMARY KEY CHECK(id=1), "
               "profile TEXT NOT NULL, revision INTEGER NOT NULL, auto_cursor INTEGER NOT NULL)")


def read(console: Any) -> dict[str, Any]:
    with console._db_lock, console._db() as db:
        row = db.execute("SELECT profile, revision FROM worker_profile WHERE id=1").fetchone()
    profile = {"rules": "", "context": "", "folders": [], "repositories": [], "auto_review": False,
               REVIEW_RUNNER: {}}
    if row is not None:
        profile.update(json.loads(row["profile"]))
    elif console.space_id == "estudio":
        profile.update(STUDIO)
    return {**profile, "revision": row["revision"] if row is not None else 0}


def view(console: Any) -> dict[str, Any]:
    return {"ok": True, "space_id": console.space_id, "profile": read(console), "templates": TEMPLATES}


def save(console: Any, args: dict[str, Any]) -> dict[str, Any]:
    revision = args.get("expected_revision")
    if type(revision) is not int or revision < 0:
        return {"ok": False, "status": 400, "error": "expected_revision must be a non-negative integer"}
    profile = args.get("profile")
    if not isinstance(profile, dict) or not set(FIELDS) <= set(profile) <= {*FIELDS, REVIEW_RUNNER}:
        return {"ok": False, "status": 400, "error": "Supply rules, context, folders, repositories and auto_review"}
    for key, limit in (("rules", 3500), ("context", 2500)):
        if not isinstance(profile[key], str) or len(profile[key]) > limit:
            return {"ok": False, "status": 400, "error": f"{key} must be text up to {limit} characters"}
    for key in ("folders", "repositories"):
        entries = profile[key]
        if not isinstance(entries, list) or len(entries) > 12 or any(
                not isinstance(item, str) or len(item) > 260 or "\n" in item or "\r" in item for item in entries):
            return {"ok": False, "status": 400, "error": f"{key} must be a list of up to 12 single-line paths"}
    if type(profile["auto_review"]) is not bool:
        return {"ok": False, "status": 400, "error": "auto_review must be a boolean"}
    runner = profile.get(REVIEW_RUNNER)
    if runner:
        if (not isinstance(runner, dict) or set(runner) != {"provider", "model", "effort"} or
                runner["provider"] not in ("claude", "codex", "cursor", "faustus") or
                not isinstance(runner["model"], str) or not 0 < len(runner["model"].strip()) <= 120 or
                not runner["model"].isprintable() or runner["effort"] not in REVIEW_EFFORTS):
            return {"ok": False, "status": 400, "error": "Review runner requires a provider, model and effort"}
        runner = {**runner, "model": runner["model"].strip()}
    elif runner is not None and runner != {}:
        return {"ok": False, "status": 400, "error": "Review runner must be an object"}
    clean = {**profile, **{k: list(dict.fromkeys(s.strip() for s in profile[k] if s.strip()))
                          for k in ("folders", "repositories")}, REVIEW_RUNNER: runner or {}}
    body = json.dumps(clean, ensure_ascii=False)
    if len(briefing(clean, "")) > 8000:
        return {"ok": False, "status": 400, "error": "The profile briefing must fit within 8000 characters"}
    cursor = getattr(getattr(console.hub, "events", None), "last_id", 0)
    with console._db_lock, console._db() as db:
        if revision == 0:
            changed = db.execute("INSERT OR IGNORE INTO worker_profile VALUES(1,?,1,?)", (body, cursor)).rowcount
        else:
            changed = db.execute(
                "UPDATE worker_profile SET auto_cursor=CASE WHEN json_extract(profile,'$.auto_review')=0 THEN ? "
                "ELSE auto_cursor END, profile=?, revision=revision+1 WHERE id=1 AND revision=?",
                (cursor, body, revision),
            ).rowcount
        if changed and not clean["auto_review"]:
            db.execute("UPDATE worker_automation SET state='cancelled', detail='profile_disabled' "
                       "WHERE state IN ('queued','waiting_quota','waiting_author')")
    if not changed:
        return {"ok": False, "status": 409, "error": "The space profile changed. Reload it before saving."}
    return view(console)


def briefing(profile: dict[str, Any], mode: str) -> str:
    sections = []
    for key, title in (("rules", "Permanent space rules"), ("context", "Project context"),
                       ("folders", "Project folders"), ("repositories", "Repositories")):
        value = profile[key]
        if value:
            text = "\n".join(f"- {item}" for item in value) if isinstance(value, list) else value
            sections.append(f"{title}:\n{text}")
    if mode:
        sections.append(f"Assignment template ({mode}):\n{TEMPLATES[mode]['instructions']}")
    return "\n\n" + "\n\n".join(sections) if sections else ""
