"""Station input requirements and revision-pinned references for order details."""

from collections import deque
import hashlib
import json
import math
import os
import re


def _requirement(key, es, en):
    return {"id": key, "es": es, "en": en}


VOLUME = (
    _requirement("orthographic", "Entrega vistas ortográficas de frente, perfil y espalda.",
                 "Supply front, side and back orthographic views."),
    _requirement("neutral-background", "Deja el fondo neutro o transparente.",
                 "Use a neutral or transparent background."),
    _requirement("separable-parts", "Muestra sin oclusiones las partes que se deben separar.",
                 "Show the parts that must separate without occlusion."),
)
VIDEO = (
    _requirement("keyframes", "Fija los fotogramas inicial y final con el mismo personaje y objetos.",
                 "Provide first and last frames with consistent character and props."),
)
PRINT = (
    _requirement("print-dimensions", "Fija el tamaño final en milímetros y los límites del volumen de impresión.",
                 "Set final dimensions in millimetres and the build volume limits."),
    _requirement("print-wall", "Fija el grosor mínimo según el material y compruébalo a escala final.",
                 "Set the minimum wall thickness for the material and check it at final scale."),
)
GAME = (
    _requirement("game-interaction", "Separa las partes móviles y fija pivotes, colisión y presupuesto de triángulos.",
                 "Separate moving parts and specify pivots, collision and the triangle budget."),
)

# Defaults extend installed handoffs without requiring synchronized upgrades of the native apps.
# Output aliases cover existing Orders plans as well as catalogue piece types.
DEFAULTS = {
    ("vulcan", "character-sheet", "reference-mesh"): VOLUME,
    ("prospero", "character-sheet", "video-clip"): VIDEO,
    ("phidias", "posed-figure-mesh", "closed-print-mesh"): PRINT,
    ("nikias", "closed-print-mesh", "coloured-3mf"): PRINT,
}
LEGACY = {
    ("vulcan", "mesh"): VOLUME,
    ("prospero", "video"): VIDEO,
    ("phidias", "figure"): PRINT,
    ("nikias", "figure"): PRINT,
    ("godot", "playable-scene"): GAME,
    ("unity", "playable-scene"): GAME,
}


def _kind(value):
    return value.replace("_", "-")


def _requirements(rows):
    return {r["id"]: {k: r[k] for k in ("id", "es", "en")} for r in rows
            if isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k].strip() for k in ("id", "es", "en"))}


def declared_requirements(app, handoff):
    """Native declarations extend or override the defaults by requirement id."""
    defaults = {}
    for output in handoff["produces"]:
        defaults.update(_requirements(DEFAULTS.get((app, _kind(handoff["accepts"]), _kind(output)), ())))
    declared = handoff.get("requires")
    if isinstance(declared, list):
        defaults.update(_requirements(declared))
    return list(defaults.values())


def _job_requirements(job, handoffs):
    app = job["station"]
    outputs = {_kind(output) for output in job["produces"]}
    defaults = {}
    for (station, output), requirements in LEGACY.items():
        if station == app and output in outputs:
            defaults.update(_requirements(requirements))
    for (station, _, output), requirements in DEFAULTS.items():
        if station == app and output in outputs:
            defaults.update(_requirements(requirements))
    candidates = [handoff for handoff in handoffs if outputs.intersection(map(_kind, handoff["produces"]))]
    if job["skill"]:
        by_skill = [handoff for handoff in handoffs if handoff["skill"] == job["skill"]]
        candidates = [handoff for handoff in by_skill if not outputs or handoff in candidates] or candidates
    for handoff in candidates:
        defaults.update(_requirements(declared_requirements(app, handoff)))
    return defaults.values()


def predecessors(plan, start):
    """Return the producing job and its in-plan ancestors, each visited once."""
    seen = {start}
    pending = deque([start])
    while pending:
        task_id = pending.popleft()
        yield task_id
        for parent in plan[task_id]["after"]:
            if parent in plan and parent not in seen:
                seen.add(parent)
                pending.append(parent)


def apply_requirements(jobs, stations):
    """Walk back from each requiring job; preserve its identity through branch joins."""
    plan = {job["task_id"]: job for job in jobs if not job["removed"]}
    by_app = {station["app"]: station["handoffs"] for station in stations}
    for job in jobs:
        job["requirements"] = []
    for task_id, job in plan.items():
        rules = list(_job_requirements(job, by_app.get(job["station"], ())))
        if not rules:
            continue
        origin = {"task_id": task_id, "station": job["station"],
                  "title": (job["task"] or {}).get("title", "")}
        for ancestor in predecessors(plan, task_id):
            plan[ancestor]["requirements"].extend({**rule, "from": origin} for rule in rules)


ANCHOR_SCHEMA = {
    "type": "object", "required": ["piece"], "additionalProperties": False,
    "properties": {
        "piece": {"type": "integer", "minimum": 1},
        "sha256": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"},
        "preview": {"type": "integer", "minimum": 0},
        "view": {"type": "string", "maxLength": 120},
        "region": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                   "minItems": 4, "maxItems": 4},
        "palette": {"type": "array", "items": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
                    "minItems": 1, "maxItems": 32},
        "measure": {"type": "object", "required": ["value", "unit"], "additionalProperties": False,
                    "properties": {"value": {"type": "number"}, "unit": {"type": "string", "maxLength": 40}}},
    },
}


def pin_anchor(raw, piece, preview_paths, error):
    """Pin a selector to the registered bytes, and fingerprint the selector itself."""
    if not isinstance(raw, dict) or raw.keys() - ANCHOR_SCHEMA["properties"].keys():
        raise error("anchor must name a piece and an optional preview, view, region, palette or measure")
    preview = raw.get("preview")
    sha = piece["sha256"]
    if preview is not None:
        if type(preview) is not int or not 0 <= preview < len(preview_paths):
            raise error("anchor preview does not exist", 404)
        stored = preview_paths[preview]
        # Preview snapshots use their byte digest as the filename in the piece store.
        sha = os.path.splitext(os.path.basename(stored))[0] if stored and os.path.isfile(stored) else ""
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise error("anchor needs a piece or preview with a registered content digest")
    if "sha256" in raw and str(raw["sha256"]).lower() != sha:
        raise error("anchor content digest differs from the registered revision", 409)
    selector = {}
    if preview is not None:
        selector["preview"] = preview
    if "view" in raw:
        if not isinstance(raw["view"], str) or not 0 < len(raw["view"].strip()) <= 120:
            raise error("anchor view must be a name of up to 120 characters")
        selector["view"] = raw["view"].strip()
    if "region" in raw:
        box = raw["region"]
        if (not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in box)
                or not (0 < box[2] <= 1 - box[0] + 1e-12 and 0 < box[3] <= 1 - box[1] + 1e-12)):
            raise error("anchor region is [x, y, width, height] within the normalized image")
        selector["region"] = [float(v) for v in box]
    if "palette" in raw:
        colors = raw["palette"]
        if (not isinstance(colors, list) or not 0 < len(colors) <= 32
                or any(not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in colors)):
            raise error("anchor palette needs 1 to 32 colors in #RRGGBB format")
        selector["palette"] = [c.lower() for c in colors]
    if "measure" in raw:
        measure = raw["measure"]
        if not isinstance(measure, dict) or measure.keys() != {"value", "unit"}:
            raise error("anchor measure needs a numeric value and a unit")
        value, unit = measure["value"], measure["unit"]
        if (type(value) not in (int, float) or not -1e100 <= value <= 1e100 or not math.isfinite(value)
                or not isinstance(unit, str) or not 0 < len(unit.strip()) <= 40):
            raise error("anchor measure needs a finite value and a unit of up to 40 characters")
        selector["measure"] = {"value": float(value), "unit": unit.strip()}
    pinned = {"piece": piece["id"], "revision": piece["revision"], "sha256": sha, **selector}
    digest = hashlib.sha256(json.dumps(pinned, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    url = f"/api/agora/piece_file/{piece['id']}" + (f"?preview={preview}" if preview is not None else "")
    return {**pinned, "anchor_sha256": digest, "url": url}


def anchored_details(pieces):
    entries = {}
    for piece in pieces:
        for detail in piece["keep"]:
            anchor = detail.get("anchor")
            if anchor:
                entries.setdefault((detail["detail"].casefold(), anchor["anchor_sha256"]),
                                   {"detail": detail["detail"], "from_piece": piece["id"], "anchor": anchor})
    return list(entries.values())
