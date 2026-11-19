"""Model-free checks of production pieces and their declared requirements."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .. import proc
from .facets import Facet
from .production import _tool

PROFILES = {
    "print-mesh": ("closed-print-mesh", "coloured-3mf", "posed-figure-mesh"),
    "game-mesh": ("articulated-mesh", "game-mesh", "game-ready-mesh", "rigged-mesh"),
    "video": ("video-clip",),
    "image": ("concept-image", "character-sheet", "rendered-image", "render-image"),
    "web": ("web-page",),
    "slides": ("editable-deck", "presentation-pptx"),
    "document": ("editable-report", "research-report", "document"),
}
TYPE_PROFILES = {kind: profile for profile, kinds in PROFILES.items() for kind in kinds}
KINDS_BY_SUFFIX = {
    ".3mf": "print-mesh", ".stl": "print-mesh", ".glb": "game-mesh", ".gltf": "game-mesh",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".tif": "image",
    ".html": "web", ".htm": "web", ".pptx": "slides", ".md": "document", ".docx": "document",
    ".txt": "document", ".pdf": "document",
}
ENGINES = {"print-mesh": "vulcan", "game-mesh": "vulcan", "web": "vitruvius"}
VERDICTS = {
    "pass": {"en": "All requested checks passed.", "es": "La pieza supera las comprobaciones solicitadas."},
    "fail": {"en": "At least one check found a defect.", "es": "Al menos una comprobación ha detectado un defecto."},
    "unmeasured": {"en": "At least one check could not be completed.",
                   "es": "Al menos una comprobación ha quedado pendiente."},
}


def descriptor(kind):
    profile = TYPE_PROFILES.get(kind)
    return {"tool": "hub_piece_validate", "profile": profile} if profile else None


class Report:
    def __init__(self):
        self.checks = []
        self.measures = {}

    def add(self, key, passed, en, es, **details):
        self.checks.append({"id": key, "verdict": "unmeasured" if passed is None else "pass" if passed else "fail",
                            "reason": {"en": en, "es": es}, **details})

    def payload(self):
        states = {entry["verdict"] for entry in self.checks}
        verdict = "fail" if "fail" in states else "unmeasured" if "unmeasured" in states or not states else "pass"
        return {"verdict": verdict, "reason": VERDICTS[verdict], "measures": self.measures, "checks": self.checks}


def run_checks(path, profile, requirements):
    from . import piece_checks_layout, piece_checks_media, piece_checks_mesh, piece_checks_sources

    handlers = {
        "image": piece_checks_media.check_image,
        "video": piece_checks_media.check_video,
        "print-mesh": piece_checks_mesh.check_print,
        "game-mesh": piece_checks_mesh.check_game,
        "web": piece_checks_layout.check_web,
        "slides": piece_checks_layout.check_slides,
        "document": piece_checks_sources.check_document,
    }
    report = Report()
    try:
        handlers[profile](path, requirements, report)
    except (OSError, ValueError, KeyError, TypeError, IndexError, ImportError, RuntimeError,
            subprocess.SubprocessError) as exc:
        report.add("inspection", None, "Inspection could not be completed.",
                   "No se ha podido completar la inspección.", detail=str(exc))
    return report.payload()


def _runtime(app):
    root = Path(app.manifest_path).resolve().parent
    candidates = [root / folder / executable for folder in ("venv", ".venv")
                  for executable in ("Scripts/python.exe", "bin/python")]
    return root, next((str(path) for path in candidates if path.is_file()), sys.executable)


def _engine(hub, engine, path, profile, requirements):
    app = hub.get(engine)
    if app is None or not app.manifest_path:
        report = Report()
        report.add("engine", None, f"The {engine} inspection engine is unavailable.",
                   f"El motor de comprobación {engine} no está disponible.")
        return report.payload()
    root, python = _runtime(app)
    hub_root = Path(__file__).resolve().parents[2]
    env = proc.build_env(PYTHONPATH=os.pathsep.join(map(str, (hub_root, root))),
                         PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    command = [python, "-m", "hoard_link.hub.piece_checks", profile, str(path),
               json.dumps(requirements, ensure_ascii=True)]
    try:
        process = proc.run(command, cwd=hub_root, env=env, timeout=35)
        if process.returncode:
            raise RuntimeError(proc.tail_lines(process.stderr or process.stdout))
        return json.loads(process.stdout)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report = Report()
        report.add("engine", None, f"The {engine} inspection engine could not finish.",
                   f"El motor de comprobación {engine} no ha completado la inspección.", detail=str(exc))
        return report.payload()


class PieceChecksFacet(Facet):
    id = "piece_checks"

    def _registered(self, args):
        from . import agora_orders

        agora = self.hub.facet("agora").agora
        if args.get("space_id"):
            agora = agora.for_space(args["space_id"])
        row = agora_orders._piece_row(agora, args["piece_id"])
        source = agora_orders.piece_file(agora, row["id"]) if row["path"] else row["url"]
        return agora, row, source

    def validate(self, args):
        started = time.monotonic()
        supplied = [key for key in ("path", "piece_id") if args.get(key) is not None]
        if len(supplied) != 1:
            return {"ok": False, "error": "Supply exactly one of path or piece_id."}
        row = None
        try:
            if supplied[0] == "piece_id":
                _, row, source = self._registered(args)
            else:
                source = args["path"]
            # The web engine serves an explicit local piece. A publication URL is a separate handoff.
            if not source or str(source).startswith(("http://", "https://")):
                return {"ok": False, "error": "This check needs a local artifact."}
            path = Path(source).expanduser().resolve(strict=True)
            kind = row["kind"] if row else args.get("kind")
            profile = args.get("profile") or TYPE_PROFILES.get(kind)
            profile = profile or ("web" if path.is_dir() else KINDS_BY_SUFFIX.get(path.suffix.lower()))
            requirements = args.get("requirements", {})
            if profile not in PROFILES or not isinstance(requirements, dict):
                return {"ok": False, "error": "Choose a supported profile and an object of requirements."}
        except (ImportError, OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        if kind == "character-sheet":
            requirements = {**requirements, "sheet": True}
        if row and profile == "document":
            requirements = {**requirements, "_base": str(Path(row["path"]).parent)}
        engine = ENGINES.get(profile)
        result = (_engine(self.hub, engine, path, profile, requirements) if engine
                  else run_checks(path, profile, requirements))
        result.update(ok=True, path=str(path), profile=profile,
                      elapsed_ms=round(1000 * (time.monotonic() - started)))
        if row:
            result["piece"] = {key: row[key] for key in ("id", "kind", "revision", "sha256")}
        return result

    def record(self, args):
        """Record a freshly computed result through Orders and return its undo token."""
        result = self.validate(args)
        if not result.get("ok"):
            return result
        from . import agora_orders

        agora, row, _ = self._registered(args)
        if any(result["piece"][key] != row[key] for key in ("sha256", "revision")):
            return {"ok": False, "error": "The registered piece changed during inspection."}
        note = _note(result, args.get("language", "es"))
        stored = agora_orders.piece_check(agora, {"piece_id": row["id"],
                                                  "agent": args.get("agent"), "note": note[:16000],
                                                  "ok": result["verdict"] == "pass"})
        return {**result, "record": stored, "undo": stored.get("undo")}

    def gate(self, args):
        """Open the native gate with technical verdicts in its visible note."""
        pieces = args.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            return {"ok": False, "error": "Supply the pieces to check before opening the gate."}
        results = []
        for piece in pieces:
            if not isinstance(piece, dict) or "piece_id" not in piece:
                return {"ok": False, "error": "Each gate piece needs a piece_id."}
            result = self.validate({**piece, "space_id": args.get("space_id")})
            if not result.get("ok"):
                return result
            results.append(result)

        from . import agora_orders

        language = args.get("language", "es")
        notes = [_note(result, language, detail_limit=max(120, 10000 // len(results))) for result in results]
        agora = self._registered({"piece_id": pieces[0]["piece_id"], "space_id": args.get("space_id")})[0]
        body = {key: args.get(key) for key in ("order_id", "title", "agent")}
        body.update(piece_ids=[result["piece"]["id"] for result in results],
                    note=("\n\n".join([*notes, args.get("note") or ""]))[:19000])
        gate = agora_orders.gate_open(agora, body)
        return {**gate, "technical_checks": results}

    def look(self, args):
        """Measure a piece before starting its native visual review."""
        result = self.validate(args)
        if not result.get("ok"):
            return result
        eyes = self.hub.facet("eyes")
        if eyes is None:
            return {"ok": False, "error": "Visual review is unavailable.", "technical_check": result}
        options = args.get("visual", {})
        if not isinstance(options, dict) or {"app", "path", "url", "same_as"} & options.keys():
            return {"ok": False, "error": "Supply visual options for the checked piece."}
        language = args.get("language", "es")
        look = eyes.look({**options, "path": result["path"], "language": language,
                          "agent": args.get("agent"),
                          "label": _note(result, language, detail_limit=200)})
        return {**look, "technical_check": result}

    @classmethod
    def tools(cls):
        string = {"type": "string", "minLength": 1}
        properties = {"piece_id": {"type": "integer", "minimum": 1}, "space_id": string,
                      "profile": {"type": "string", "enum": list(PROFILES)},
                      "requirements": {"type": "object"}}
        actor = {"agent": string, "language": {"enum": ["en", "es"]}}
        file = {**properties, "path": string, "kind": string}
        return [
            _tool("hub_piece_validate", "Check a piece without a model. Comprueba una pieza sin modelo.",
                  file),
            _tool("hub_piece_validate_record", "Check and record an order piece. Comprueba y anota una pieza de una orden.",
                  {**properties, **actor}, ("piece_id",), read=False),
            _tool("hub_piece_gate_checked", "Show technical checks on a gate. Muestra las comprobaciones en la puerta.",
                  {"pieces": {"type": "array", "items": {"type": "object", "properties": properties,
                                                       "required": ["piece_id"], "additionalProperties": False}},
                   "order_id": {"type": "integer", "minimum": 1}, "space_id": string,
                   "title": string, "note": string, **actor}, ("pieces", "order_id"), read=False),
            _tool("hub_piece_look_checked", "Check before visual review. Comprueba la pieza antes de la revisión visual.",
                  {**file, **actor, "visual": {"type": "object"}}, read=False),
        ]

    def handlers(self):
        return {"hub_piece_validate": self.validate, "hub_piece_validate_record": self.record,
                "hub_piece_gate_checked": self.gate, "hub_piece_look_checked": self.look}


def _note(result, language, detail_limit=3000):
    language = "en" if language == "en" else "es"
    label = "Technical check" if language == "en" else "Comprobación automática"
    piece = result.get("piece")
    identity = _piece_identity(piece) if piece else ""
    labels = {
        "measured": ("Measured", "Medido"), "expected": ("Expected", "Esperado"),
        "maximum": ("Maximum", "Máximo"), "shot": ("Shot", "Plano"),
        "details": ("Findings", "Hallazgos"), "detail": ("Detail", "Detalle"),
    }
    findings = []
    for check in result["checks"]:
        if check["verdict"] != "pass":
            facts = []
            for key, names in labels.items():
                value = check.get(key)
                if value is None or value == []:
                    continue
                if key in ("measured", "expected") and isinstance(value, list) and all(isinstance(n, (int, float)) for n in value):
                    value = " × ".join(map(str, value))
                shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                facts.append(f"{names[language == 'es']} {shown}")
            findings.append(f"{check['reason'][language]} {', '.join(facts)}".rstrip())
    details = "\n".join(findings)[:detail_limit]
    return f"{label} · {identity}{result['profile']} · {result['reason'][language]}\n{details}".rstrip()


def _piece_identity(piece):
    return f"#{piece['id']} · r{piece['revision']} · {piece['sha256']} · "


def gate_advisories(agora, pieces, gate_note):
    """Expose pending checks or a recorded result in the ordinary Orders gate."""
    output = []
    for piece in pieces:
        profile = TYPE_PROFILES.get(piece["kind"])
        if not profile:
            continue
        marker = _piece_identity(piece)
        if marker in gate_note:
            continue
        previous = agora.db.one("SELECT note FROM agora_piece_checks WHERE piece_id = ? AND "
                                "(note LIKE ? OR note LIKE ?) ORDER BY id DESC LIMIT 1",
                                (piece["id"], "Technical check · " + marker + "%",
                                 "Comprobación automática · " + marker + "%"))
        output.append({"piece_id": piece["id"], "title": piece["title"], "profile": profile,
                       "note": previous["note"] if previous else None})
    return output


if __name__ == "__main__":
    profile, source, encoded = sys.argv[1:]
    print(json.dumps(run_checks(Path(source), profile, json.loads(encoded)), ensure_ascii=True, allow_nan=False))
