"""Send retained production references to workers and require a recorded visual comparison."""

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import agora_orders as orders
from . import order_gate_readiness as readiness


COMPARISONS = {
    "description": "Visual comparisons with kept references using registered images or previews. For reference_defect, "
                   "link each pair to an open defect on reference_piece. / Cotejos con referencias conservadas mediante "
                   "imágenes o vistas registradas. Para reference_defect, enlaza cada par a un defecto abierto de reference_piece.",
    "type": "array", "maxItems": 200, "items": {
        "type": "object", "additionalProperties": False, "required": ["reference_piece", "observed"],
        "properties": {
            "reference_piece": {"type": "integer", "minimum": 1},
            "piece_preview": {"type": "integer", "minimum": 0},
            "reference_preview": {"type": "integer", "minimum": 0},
            "reference_defect_id": {"type": "integer", "minimum": 1,
                                    "description": "Open defect on reference_piece. / Defecto abierto de reference_piece."},
            "observed": {"type": "string", "maxLength": 1000},
        },
    },
}
ORDER_CITATION = re.compile(r"\b(?:orden|order)\s*#\s*([1-9][0-9]{0,17})\b", re.I)
VISUAL_KINDS = {
    "image", "concept-art", "concept-image", "character-sheet", "render", "turntable", "video", "video-clip", "animatic",
    "mesh", "reference-mesh", "rigged-model", "articulated-model", "articulated-mesh", "figure",
    "posed-figure-mesh", "closed-print-mesh", "coloured-3mf", "set", "playable-scene", "blocking",
}


def _cited_orders(self, order):
    """Resolve explicit order IDs and references in orders written before IDs were supported."""
    cited = {order["id"]}
    refs = orders._load(order["refs"], [])
    paths = set()
    for ref in refs:
        if type(ref.get("order_id")) is int:
            cited.add(ref["order_id"])
        cited.update(int(match) for match in ORDER_CITATION.findall(ref.get("note", "")))
        if ref.get("path"):
            paths.add(os.path.normcase(orders._resolve(order["folder"], ref["path"])))
        if ref.get("url"):
            try:
                url = urlsplit(ref["url"])
            except ValueError:
                continue
            if url.hostname not in (None, "localhost", "127.0.0.1", "::1"):
                continue
            if url.path.rstrip("/") == "/api/agora/order":
                oid = parse_qs(url.query).get("order_id", [""])[0]
                if re.fullmatch(r"[1-9][0-9]{0,17}", oid):
                    cited.add(int(oid))
    if paths:
        rows = self.db.query("SELECT order_id, path, stored, previews, previews_stored FROM agora_pieces WHERE space_id=?",
                             (self.space_id,))
        for row in rows:
            if any(path and os.path.normcase(os.path.normpath(path)) in paths for path in
                   [row["path"], row["stored"], *orders._load(row["previews"], []), *orders._load(row["previews_stored"], [])]):
                cited.add(row["order_id"])
    return sorted(cited)


def _kept_rows(self, order_ids):
    marks = ",".join("?" for _ in order_ids)
    return [dict(row) for row in self.db.query(
        "SELECT p.*, d.id AS kept_decision, d.gate_id AS kept_gate, d.note AS kept_note "
        "FROM agora_pieces p JOIN agora_order_decisions d ON d.id=(SELECT MAX(id) FROM agora_order_decisions "
        f"WHERE piece_id=p.id AND decision='keep') WHERE p.space_id=? AND p.status='kept' "
        f"AND p.order_id IN ({marks}) ORDER BY p.order_id, p.id", (self.space_id, *order_ids))]


def _visual(row):
    return (row["kind"].replace("_", "-") in VISUAL_KINDS or
            orders.view_of({**row, "previews": orders._load(row["previews"], [])}) in {"image", "video", "mesh", "turntable"})


def _continuity_reference(row):
    # A draft decision keeps the camera and pose guides for reuse in their own roles.
    return row["kept_gate"] != 0 and _visual(row)


def required_rows(self, order):
    return _kept_rows(self, _cited_orders(self, order))


def _file_url(piece, preview=None):
    path = f"/api/agora/piece_file/{piece}"
    return path if preview is None else f"{path}?preview={preview}"


def reference_packets(self, order):
    packets = []
    for row in required_rows(self, order):
        preview_paths = orders._load(row["previews"], [])
        snapshots = orders._load(row["previews_stored"], [])
        packets.append({
            "piece": row["id"], "order_id": row["order_id"], "title": row["title"], "kind": row["kind"],
            "revision": row["revision"], "sha256": row["sha256"], "source_rev": row["source_rev"],
            "path": row["stored"] or row["path"], "url": row["url"], "app": row["app"],
            "object": row["object"], "place": row["place"], "decision_id": row["kept_decision"],
            "kept_note": row["kept_note"], "comparison_required": _continuity_reference(row),
            "keep": orders._load(row["keep"], []),
            "file_url": _file_url(row["id"]) if row["path"] else "",
            "previews": [{"preview": index, "path": (snapshots[index] if index < len(snapshots) else "") or path,
                          "file_url": _file_url(row["id"], index)} for index, path in enumerate(preview_paths)],
        })
    return packets


def _ancestors(self, piece):
    seen = {piece["id"]}
    pending = orders._parents(piece["derives_from"])
    ancestors = {}
    while pending:
        parent = pending.pop()["piece"]
        if parent in seen:
            continue
        seen.add(parent)
        row = self.db.one("SELECT * FROM agora_pieces WHERE id=? AND space_id=?", (parent, self.space_id))
        if row and row["status"] not in ("cancelled", "stale"):
            ancestors[parent] = dict(row)
            pending.extend(orders._parents(row["derives_from"]))
    return ancestors


def registered_piece(self, order, piece):
    out = orders._piece_dict(self, piece, full=True)
    missing = []
    if piece["status"] not in ("kept", "cancelled", "stale") and (required := required_rows(self, order)):
        ancestors = _ancestors(self, piece)
        missing = [row["id"] for row in required if row["id"] not in ancestors]
    out["missing_required_references"] = missing
    if missing:
        labels = ", ".join(f"#{pid}" for pid in missing)
        out["warnings"].append(f"Faltan referencias conservadas en la procedencia: {labels}.")
    return out


def _comparison_references(self, piece, required):
    ancestors = _ancestors(self, piece)
    by_id = {row["id"]: row for row in required}
    other_orders = sorted({row["order_id"] for row in ancestors.values() if row["status"] == "kept"
                           and row["id"] not in by_id})
    if other_orders:
        by_id.update((row["id"], row) for row in _kept_rows(self, other_orders) if row["id"] in ancestors)
    return {pid: row for pid, row in by_id.items() if pid != piece["id"] and _continuity_reference(row)}


def _image_evidence(piece, preview):
    if preview is not None and (type(preview) is not int or preview < 0):
        raise orders._err("comparison preview must be zero or a positive integer")
    if preview is None:
        stored = piece["stored"]
    else:
        previews = orders._load(piece["previews_stored"], [])
        stored = previews[preview] if preview < len(previews) else ""
    if not stored or not _is_image(Path(stored)):
        raise orders._err("Comparison requires a retained image or image preview of this revision.", 409,
                          error_code="comparison_image_required", piece_id=piece["id"],
                          es="La comparación requiere una imagen o una vista guardada de esta revisión.",
                          en="Comparison requires a retained image or image preview of this revision.")
    return {"piece": piece["id"], "revision": piece["revision"], "preview": preview,
            "sha256": Path(stored).stem, "file_url": _file_url(piece["id"], preview)}


def _is_image(path):
    suffix = path.suffix.lower()
    if suffix not in orders.IMAGE_EXT:
        return False
    try:
        with path.open("rb") as image:
            header = image.read(512)
    except OSError:
        return False
    signatures = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpeg": (b"\xff\xd8\xff",), ".jpg": (b"\xff\xd8\xff",),
                  ".gif": (b"GIF87a", b"GIF89a"), ".bmp": (b"BM",)}
    if suffix == ".webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if suffix == ".svg":
        return bool(re.search(rb"<svg(?:\s|>)", header))
    return header.startswith(signatures[suffix])


def _reference_defect(self, reference, defect_id):
    defect = self.db.one("SELECT id, piece_id, note, status FROM agora_piece_defects WHERE id=? AND space_id=?",
                         (str(defect_id), self.space_id))
    if not defect:
        return None, "reference_defect_missing", f"No existe el defecto #{defect_id} en este espacio.", (
            f"Defect #{defect_id} does not exist in this space.")
    if defect["piece_id"] != reference:
        return None, "reference_defect_wrong_piece", f"El defecto #{defect_id} no pertenece a la referencia #{reference}.", (
            f"Defect #{defect_id} does not belong to reference #{reference}.")
    if defect["status"] != "open":
        return None, "reference_defect_closed", f"El defecto #{defect_id} está cerrado.", f"Defect #{defect_id} is closed."
    return defect, "", "", ""


def _replaced_by(self, reference):
    return self.db.scalar(
        "WITH RECURSIVE later(id, status) AS ("
        "SELECT id, status FROM agora_pieces WHERE supersedes=? AND space_id=? UNION "
        "SELECT p.id, p.status FROM agora_pieces p JOIN later l ON p.supersedes=l.id WHERE p.space_id=?"
        ") SELECT id FROM later WHERE status='kept' AND EXISTS (SELECT 1 FROM agora_order_decisions d "
        "WHERE d.piece_id=later.id AND d.decision='keep' AND d.gate_id<>0) ORDER BY id DESC LIMIT 1",
        (reference, self.space_id, self.space_id),
    )


def check_comparisons(self, piece, mark, state):
    raw = mark.get("comparisons")
    if state == readiness.REFERENCE_DEFECT and not raw:
        raise orders._err("reference_defect requires an image comparison linked to an open defect. / "
                          "reference_defect requiere un cotejo enlazado a un defecto abierto.", 409,
                          error_code="reference_defect_required")
    if not isinstance(raw, list) or not 1 <= len(raw) <= COMPARISONS["maxItems"]:
        raise orders._err("comparisons must be a nonempty list of image comparisons")
    order = orders._order_row(self, piece["order_id"])
    references = _comparison_references(self, piece, required_rows(self, order))
    evidence = {}
    for item in raw:
        if not isinstance(item, dict) or item.keys() - COMPARISONS["items"]["properties"].keys():
            raise orders._err("each comparison names a reference_piece, optional previews, what was observed and, "
                              "for reference_defect, reference_defect_id")
        if not isinstance(item.get("observed"), str):
            raise orders._err("comparison.observed must describe what was seen in the pair")
        reference = orders._int(item.get("reference_piece"), "reference_piece")
        if reference not in references or reference in evidence:
            raise orders._err("comparison must name a distinct kept visual reference required by the order "
                              "or in the piece's lineage", 409)
        defect_id = orders._int(item.get("reference_defect_id"), "reference_defect_id", required=False)
        if state == readiness.REFERENCE_DEFECT:
            if defect_id is None:
                raise orders._err("Link the open defect with reference_defect_id. / "
                                  "Enlaza el defecto abierto con reference_defect_id.", 409,
                                  error_code="reference_defect_required", reference_piece=reference)
            _, code, es, en = _reference_defect(self, reference, defect_id)
            if code:
                raise orders._err(en, 409, error_code=code, es=es, en=en, defect_id=defect_id, reference_piece=reference)
            if replacement := _replaced_by(self, reference):
                en = f"Reference #{reference} was replaced by #{replacement}. Compare the current revision."
                es = f"La referencia #{reference} se ha sustituido por #{replacement}. Coteja la revisión actual."
                raise orders._err(en, 409, error_code="reference_replaced", es=es, en=en, reference_piece=reference,
                                  replacement_piece=replacement, defect_id=defect_id)
        elif "reference_defect_id" in item:
            raise orders._err("reference_defect_id requires state=reference_defect")
        evidence[reference] = {
            "reference_piece": reference, "result": _image_evidence(piece, item.get("piece_preview")),
            "reference": _image_evidence(references[reference], item.get("reference_preview")),
            "observed": orders._txt(item.get("observed"), 1000, "comparison.observed", required=True),
        }
        if defect_id is not None:
            evidence[reference]["reference_defect_id"] = defect_id
    return list(evidence.values())


def validate_gate_references(self, order, pieces, *, recheck_defects=False):
    """Require current comparisons at opening and recheck linked defects before a keep decision."""
    details = orders._load(order["keep"], [])
    required = None
    failures = []
    declarations = []
    for piece in pieces:
        keep = orders._load(piece["keep"], [])
        marked = {k["id"] for k in keep if k.get("state") == readiness.REFERENCE_DEFECT}
        detail_ids = {detail["detail"].casefold(): detail["id"] for detail in keep}
        required_ids = ({detail_ids.get(detail.casefold()) for detail in details}
                        if piece["status"] != "kept" and _visual(piece) else set())
        selected = marked | required_ids
        if not selected:
            continue
        if required is None:
            required = required_rows(self, order)
        references = _comparison_references(self, piece, required)
        if not references and not marked:
            continue
        latest = {}
        for check in self.db.query("SELECT ok, keep FROM agora_piece_checks WHERE piece_id=? ORDER BY id", (piece["id"],)):
            for mark in orders._load(check["keep"], []):
                for evidence in mark.get("comparisons", []):
                    latest[mark["id"], evidence["reference_piece"]] = (check["ok"], mark["state"], evidence)
        if recheck_defects and not any(state == readiness.REFERENCE_DEFECT and detail in selected
                                       for (detail, _), (_, state, _) in latest.items()):
            continue
        states = {}
        checked_defects = set()
        superseded = {}
        for (detail, reference), (ok, state, evidence) in latest.items():
            if reference not in references or detail not in selected:
                continue
            current = (evidence["result"] == _image_evidence(piece, evidence["result"]["preview"])
                       and evidence["reference"] == _image_evidence(references[reference], evidence["reference"]["preview"]))
            if not (ok and current):
                state = "missing"
            elif state == readiness.REFERENCE_DEFECT:
                checked_defects.add(detail)
                defect_id = evidence["reference_defect_id"]
                if replacement := _replaced_by(self, reference):
                    superseded[detail, reference] = (replacement, defect_id)
                    continue
                defect, code, _, _ = _reference_defect(self, reference, defect_id)
                if not defect:
                    failures.append({"piece": piece["id"], "reference_piece": reference, "defect_id": defect_id,
                                     "reason": "comparison_pending", "cause": code})
                else:
                    declarations.append((piece, defect))
            states[detail, reference] = state
        renewed = set()
        for (detail, reference), (replacement, defect_id) in superseded.items():
            if states.get((detail, replacement)) not in readiness.PASSING_DETAIL_STATES:
                failures.append({"piece": piece["id"], "reference_piece": reference, "defect_id": defect_id,
                                 "reason": "comparison_pending", "cause": "reference_replaced",
                                 "replacement_piece": replacement})
            elif detail in required_ids:
                renewed.add(reference)
        for reference in references:
            grades = {states.get((detail, reference), "pending") for detail in required_ids
                      if (detail, reference) not in superseded}
            if grades and ((reference not in renewed and not grades.intersection(("ok", readiness.REFERENCE_DEFECT)))
                           or grades.difference(readiness.PASSING_DETAIL_STATES)):
                failures.append({"piece": piece["id"], "reference_piece": reference,
                                 "reason": "comparison_failed" if "missing" in grades else "comparison_pending"})
        # A persisted mark still needs an applicable comparison if its reference has left the required set.
        for detail in sorted(marked - checked_defects):
            reference = next((rid for (key, rid), (_, state, _) in latest.items()
                              if key == detail and state == readiness.REFERENCE_DEFECT), None)
            failures.append({"piece": piece["id"], "reference_piece": reference, "reason": "comparison_pending"})
    if failures:
        es = ("Falta un cotejo visual satisfactorio con las referencias conservadas. "
              "Regístralo con hub_agora_piece_check en keep[].comparisons.")
        en = ("A passing visual comparison with the kept references is required. "
              "Record it with hub_agora_piece_check in keep[].comparisons.")
        raise orders._err(en, 409, error_code="kept_reference_comparison", es=es, en=en, comparisons=failures)
    disclosed = set()
    for piece, defect in declarations:
        disclosed.update(readiness.disclose_reference_defect(piece, defect, orders._err))
    return disclosed
