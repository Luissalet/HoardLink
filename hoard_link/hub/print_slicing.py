"""Headless slicing of a registered order artifact and its technical report."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

from . import agora_orders, print_colours, print_gcode


_LOG_LEVEL = re.compile(r"(?:\[(warning|warn|error|fatal)\]\s*|^\s*(warning|warn|error|fatal)\s*:\s*)(.+)", re.I)
_BED_PREVIEW = "calc_exclude_triangles:Unable to create exclude triangles"
_RUN_ID = re.compile(r"[a-f0-9]{32}")
_VERSION_BANNER = re.compile(r"cli mode, Current OrcaSlicer Version ([\w.\-+]+)")


def _root(agora):
    return Path(agora.db.path).resolve().parent / "print-slices"


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _diagnostics(log, engine_result):
    findings = {}
    version = None
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            match = _LOG_LEVEL.search(line)
            if not match:
                continue
            severity = (match[1] or match[2]).lower()
            if severity == "warn":
                severity = "warning"
            message = match[3].strip()
            banner = _VERSION_BANNER.fullmatch(message)
            if severity == "warning" and banner:
                version = banner[1]
                continue
            code = "bed_exclusion_preview" if _BED_PREVIEW in message else "slicer_log"
            key = severity, message
            item = findings.setdefault(key, {"severity": severity, "message": message, "code": code, "count": 0})
            item["count"] += 1
    if engine_result.is_file():
        try:
            result = json.loads(engine_result.read_text(encoding="utf-8"))
            messages = [(plate.get("warning_message"), "warning") for plate in result.get("sliced_plates", [])]
            if result.get("return_code") != 0:
                messages.append((result.get("error_string"), "error"))
            for message, severity in messages:
                if message and not any(message in item["message"] for item in findings.values()):
                    findings[severity, message] = {"severity": severity, "message": message, "code": "slicer_log", "count": 1}
        except (OSError, ValueError, TypeError, AttributeError):
            findings["error", "result.json"] = {"severity": "error", "message": "result.json",
                                                 "code": "slicer_result", "count": 1}
    return list(findings.values()), version


def _preset(work, name, supplied, kind):
    path = Path(supplied).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name}: supply an absolute preset path")
    data = path.read_bytes()
    content = json.loads(data.decode("utf-8-sig"))
    if not isinstance(content, dict) or content.get("type") != kind:
        raise ValueError(f"{name}: use an OrcaSlicer {kind} preset JSON")
    copy = work / f"{name}.json"
    copy.write_bytes(data)
    return copy, {"name": content.get("name") or path.stem, "sha256": hashlib.sha256(data).hexdigest()}


def run(agora, source, artifact, executable, version, args):
    """Slice one plate into a fresh directory. The source revision and preset bytes are pinned."""
    selected_plate = args.get("plate")
    budget = args.get("timeout_s", 75)
    filaments = args.get("filaments")
    expected_filaments = args.get("expected_filaments")
    if selected_plate is not None and (type(selected_plate) is not int or selected_plate < 1):
        raise ValueError("plate must be a positive integer")
    if type(budget) is not int or not 1 <= budget <= 900:
        raise ValueError("timeout_s must be an integer from 1 to 900")
    if not isinstance(filaments, list) or not filaments or not all(isinstance(path, str) for path in filaments):
        raise ValueError("Supply filament preset paths in slot order")
    if expected_filaments is not None and (type(expected_filaments) is not int or expected_filaments < 1):
        raise ValueError("expected_filaments must be a positive integer")
    arrange = args.get("arrange", True)
    require_support = args.get("require_support", False)
    if not isinstance(arrange, bool) or not isinstance(require_support, bool):
        raise ValueError("arrange and require_support must be booleans")

    work = _root(agora) / uuid4().hex
    work.mkdir(parents=True)
    # Keep logs and deliverables, while removing the isolated process profile and input copies.
    staging = work / "work"
    staging.mkdir()
    log = work / "slicer.log"
    receipt = work / "receipt.json"
    try:
        machine, machine_record = _preset(staging, "machine", args.get("machine", ""), "machine")
        process, process_record = _preset(staging, "process", args.get("process", ""), "process")
        slots = [_preset(staging, f"filament-{i}", preset, "filament") for i, preset in enumerate(filaments, 1)]
        pinned = staging / "source.3mf"
        shutil.copyfile(artifact, pinned)
        if _hash(pinned) != source["sha256"]:
            raise ValueError("The 3MF changed while copying its registered revision")

        command = [executable, "--debug", "2", "--datadir", str(staging / "profile"),
                   "--load-settings", f"{machine};{process}", "--load-filaments", ";".join(str(p) for p, _ in slots),
                   "--orient", "0", "--arrange", str(int(arrange)), "--outputdir", str(work),
                   "--slice", str(selected_plate or 0), str(pinned)]
        options = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
            options["env"] = {**os.environ, **dict.fromkeys(("APPDATA", "LOCALAPPDATA", "TEMP", "TMP"), str(staging))}
        timed_out = False
        failure = None
        with log.open("wb") as stream:
            try:
                exit_code = subprocess.run(command, cwd=staging, stdout=stream, stderr=subprocess.STDOUT,
                                           timeout=budget, **options).returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = None
            except OSError as exc:
                failure = str(exc)
                exit_code = None
        diagnostics, logged_version = _diagnostics(log, work / "result.json")
        outputs = [p for p in work.glob("*.gcode") if p.is_file() and p.stat().st_size]
        if timed_out:
            diagnostics.append({"severity": "error", "code": "slicer_timeout", "message": str(budget), "count": 1})
        elif failure:
            diagnostics.append({"severity": "error", "code": "slicer_launch", "message": failure, "count": 1})
        elif exit_code != 0:
            diagnostics.append({"severity": "error", "code": "slicer_exit", "message": str(exit_code), "count": 1})
        elif len(outputs) != 1:
            diagnostics.append({"severity": "error", "code": "slicer_output", "message": str(len(outputs)), "count": 1})
        gcode = outputs[0] if exit_code == 0 and len(outputs) == 1 else None
        measures = print_gcode.measure(gcode) if gcode else None
        colour_check = None
        try:
            colour_check = print_colours.read(pinned)
        except ValueError:
            diagnostics.append({"severity": "error", "code": "filament_colours_unreadable", "message": "", "count": 1})
        if measures and colour_check:
            colour_check = print_colours.compare(colour_check, measures["filaments"])
            if colour_check["differences"]:
                changes = ", ".join(f'{row["slot"]} · {row["expected"] or "—"} → {row["actual"] or "—"}'
                                    for row in colour_check["differences"])
                diagnostics.append({"severity": "error", "code": "filament_colours_mismatch", "message": changes, "count": 1})
        if measures and (not measures["layers"] or not measures["filaments"] or measures["estimated_seconds"] is None
                         or measures["support_mm3"] is None or measures["filament_changes"] is None):
            diagnostics.append({"severity": "error", "code": "slicer_unmeasured", "message": "", "count": 1})
        if measures and expected_filaments and len(measures["filaments"]) < expected_filaments:
            diagnostics.append({"severity": "warning", "code": "filaments_missing",
                                "message": f"{len(measures['filaments'])} / {expected_filaments}", "count": 1})
        if measures and require_support and not measures["support_mm3"]:
            diagnostics.append({"severity": "warning", "code": "support_missing", "message": "", "count": 1})
        actual_plate = re.fullmatch(r"plate_(\d+)\.gcode", gcode.name) if gcode else None
        report = {"engine": "OrcaSlicer", "version": (measures or {}).get("version") or logged_version or version,
                  "source_piece": source["id"], "source_revision": source["revision"], "source_sha256": source["sha256"],
                  "plate": int(actual_plate[1]) if actual_plate else selected_plate,
                  "arrange": arrange, "expected_filaments": expected_filaments, "require_support": require_support,
                  "exit_code": exit_code, "diagnostics": diagnostics, "measures": measures, "colour_check": colour_check,
                  "profiles": {"machine": machine_record, "process": process_record, "filaments": [record for _, record in slots]},
                  "gcode": gcode.name if gcode else None, "gcode_sha256": _hash(gcode) if gcode else None}
        receipt.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return gcode or receipt, receipt, log, report
    finally:
        # Keep outputs only when there is a report, including a report of a failed slice.
        shutil.rmtree(staging if receipt.is_file() else work)


def report_for_piece(agora, row):
    """Only load reports from this station's private run directories."""
    if row.get("kind") not in ("gcode", "slice-report") or not row.get("path"):
        return None
    path = Path(row["path"])
    if path.parent.parent != _root(agora) or not _RUN_ID.fullmatch(path.parent.name):
        return None
    receipt = path.parent / "receipt.json"
    try:
        data = receipt.read_bytes()
        if hashlib.sha256(data).hexdigest() != row.get("source_rev"):
            return None
        report = json.loads(data)
        if row["kind"] == "gcode":
            return report if report["gcode"] == path.name and report["gcode_sha256"] == row["sha256"] else None
        return report if receipt == path and row["source_rev"] == row["sha256"] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def reports_for_pieces(agora, piece_ids):
    ids = {int(value) for value in piece_ids if re.fullmatch(r"[1-9][0-9]{0,17}", value)}
    reports = {}
    for piece_id in ids:
        row = agora.db.one("SELECT * FROM agora_pieces WHERE id=? AND kind IN ('gcode', 'slice-report')", (piece_id,))
        report = report_for_piece(agora, dict(row)) if row else None
        if report is not None:
            reports[piece_id] = report
    return reports


def defect_note(diagnostic):
    labels = {"slicer_timeout": "Tiempo de laminado agotado (s) / Slicing timed out (s)",
              "slicer_launch": "No se pudo iniciar el laminador / Could not start the slicer",
              "slicer_exit": "Código de salida del laminador / Slicer exit code",
              "slicer_output": "Número inesperado de ficheros G-code / Unexpected G-code file count",
              "slicer_unmeasured": "Mediciones de laminado incompletas / Incomplete slicing measurements",
              "slicer_result": "Resultado del laminador ilegible / Unreadable slicer result",
              "filaments_missing": "Filamentos usados frente a los esperados / Used versus expected filaments",
              "filament_colours_mismatch": "Color por ranura del 3MF → G-code / 3MF → G-code slot colours",
              "filament_colours_unreadable": "Paleta 3MF ilegible / Unreadable 3MF palette",
              "support_missing": "No se han medido los soportes requeridos / Required supports were not measured"}
    label, message = labels.get(diagnostic["code"]), diagnostic["message"]
    return f"{label}: {message}" if label and message else label or message


def advisory(report):
    """Compact note for the order gate's existing technical-advisory surface."""
    def metric(value, unit=""):
        return f"{value:.2f}".rstrip("0").rstrip(".") + unit if value is not None else "Sin datos / No data"

    items = [" ".join(filter(None, (report["engine"], report["version"])))]
    measures = report["measures"]
    if measures:
        items.extend((f"Soportes / Supports {metric(measures['support_mm3'], ' mm³')}",
                      f"Cambios de filamento / Filament changes {metric(measures['filament_changes'])}",
                      f"Tiempo total estimado / Estimated total time {metric(measures['estimated_seconds'], ' s')}"))
        for row in measures["filaments"]:
            slot = f"Filamento / Filament {row['slot']}" + (f" {row['colour']}" if row["colour"] else "")
            material = next((metric(row[key], unit) for key, unit in
                             (("material_g", " g"), ("material_cm3", " cm³"), ("material_mm", " mm"))
                             if row[key] is not None), metric(None))
            items.append(f"{slot}, {material}, {metric(row['deposition_seconds'], ' s')} de trazado / path time")
    else:
        items.append("Sin G-code / No G-code")
    for diagnostic in report["diagnostics"]:
        severity = "Aviso / Warning" if diagnostic["severity"] == "warning" else "Error"
        count = diagnostic["count"]
        items.append(f"{severity} {defect_note(diagnostic)}" + (f" (×{count})" if count > 1 else ""))
    return " · ".join(items)


def slice_piece(hub, program, args):
    agora = hub.facet("agora").agora.for_space(args.get("space_id"))
    agent = agora._agent(args.get("agent"))
    source = agora_orders._piece_row(agora, args.get("piece_id"))
    if Path(source["path"] or "").suffix.lower() != ".3mf":
        raise ValueError("The source piece must be a registered 3MF file")
    if source["status"] in ("cancelled", "stale"):
        raise ValueError("Choose a source revision that is still current")
    task_id = agora_orders._int(args.get("task_id"), "task_id")
    task = agora.db.one("SELECT station FROM agora_order_jobs WHERE task_id=? AND order_id=? AND removed=0",
                        (task_id, source["order_id"]))
    if not task:
        raise ValueError("The slicing task must be an active job of the same order")
    supersedes = args.get("supersedes")
    if supersedes is not None:
        previous = agora_orders._piece_row(agora, supersedes)
        if previous["order_id"] != source["order_id"] or previous["task_id"] != task_id or not report_for_piece(agora, previous):
            raise ValueError("supersedes must be a slicing output of the same job")
    artifact = agora_orders.piece_file(agora, source["id"])
    output, receipt, log, report = run(agora, source, artifact, program["path"], program.get("version"), args)
    is_gcode = report["gcode"] is not None
    title = f"{source['title'][:145]} · {('G-code' if is_gcode else 'Informe / Report')}"
    if report["plate"]:
        title += f" · {report['plate']}"
    registration = {
        "agent": agent, "order_id": source["order_id"], "task_id": task_id, "derives_from": [source["id"]],
        "model": " ".join(filter(None, (report["engine"], report["version"]))),
        "kind": "gcode" if is_gcode else "slice-report", "path": str(output),
        "source_rev": _hash(receipt),
        "title": args.get("title") or title,
        "supersedes": supersedes,
    }
    with agora.db.tx():
        result = agora_orders.piece_add(agora, registration)
        piece = result["piece"]
        undo = [result["undo"]]
        for finding in report["diagnostics"]:
            severity = "Aviso / Warning" if finding["severity"] == "warning" else "Error"
            count = finding["count"]
            note = f"{severity} · {defect_note(finding)}" + (f" (×{count})" if count > 1 else "")
            defect = agora_orders.piece_defect(agora, {
                "agent": agent, "piece_id": piece["id"], "step": "Laminado / Slicing", "note": note[:agora_orders.TEXT_MAX],
            })
            undo.append(defect["undo"])
            piece = defect["piece"]
    return {"ok": True, "piece": {**piece, "slice_report": report}, "gcode_generated": is_gcode,
            "checks_clear": is_gcode and not report["diagnostics"],
            "receipt": str(receipt), "log": str(log), "undo": [token for token in reversed(undo) if token]}
