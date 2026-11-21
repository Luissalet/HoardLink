"""Send retained production references to workers and require a recorded visual comparison."""

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import agora_orders as orders


COMPARISONS = {
    "description": "Visual comparisons with kept references, using registered image files or previews.",
    "type": "array", "maxItems": 200, "items": {
        "type": "object", "additionalProperties": False, "required": ["reference_piece", "observed"],
        "properties": {
            "reference_piece": {"type": "integer", "minimum": 1},
            "piece_preview": {"type": "integer", "minimum": 0},
            "reference_preview": {"type": "integer", "minimum": 0},
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
    return ancestors, {pid: row for pid, row in by_id.items() if pid != piece["id"] and _continuity_reference(row)}


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


def check_comparisons(self, piece, mark):
    raw = mark.get("comparisons")
    if not isinstance(raw, list) or not 1 <= len(raw) <= COMPARISONS["maxItems"]:
        raise orders._err("comparisons must be a nonempty list of image comparisons")
    order = orders._order_row(self, piece["order_id"])
    ancestors, references = _comparison_references(self, piece, required_rows(self, order))
    evidence = {}
    for item in raw:
        if not isinstance(item, dict) or item.keys() - COMPARISONS["items"]["properties"].keys():
            raise orders._err("each comparison names a reference_piece, optional previews and what was observed")
        if not isinstance(item.get("observed"), str):
            raise orders._err("comparison.observed must describe what was seen in the pair")
        reference = orders._int(item.get("reference_piece"), "reference_piece")
        if reference not in references or reference not in ancestors or reference in evidence:
            raise orders._err("comparison must name a distinct kept visual reference in the piece's lineage", 409)
        evidence[reference] = {
            "reference_piece": reference, "result": _image_evidence(piece, item.get("piece_preview")),
            "reference": _image_evidence(references[reference], item.get("reference_preview")),
            "observed": orders._txt(item.get("observed"), 1000, "comparison.observed", required=True),
        }
    return list(evidence.values())


def validate_gate_references(self, order, piece_ids):
    """Reject before the gate write, even when the producer omitted a required reference entirely."""
    details = orders._load(order["keep"], [])
    if not details:
        return
    required = required_rows(self, order)
    failures = []
    for piece_id in piece_ids:
        piece = orders._piece_row(self, piece_id)
        if piece["status"] == "kept" or not _visual(piece):
            continue
        ancestors, references = _comparison_references(self, piece, required)
        if not references:
            continue
        latest = {}
        for check in self.db.query("SELECT ok, keep FROM agora_piece_checks WHERE piece_id=? ORDER BY id", (piece["id"],)):
            for mark in orders._load(check["keep"], []):
                for evidence in mark.get("comparisons", []):
                    latest[mark["id"], evidence["reference_piece"]] = (check["ok"], mark["state"], evidence)
        detail_ids = {detail["detail"].casefold(): detail["id"] for detail in orders._load(piece["keep"], [])}
        for reference in references:
            states = []
            if reference in ancestors:
                for detail in details:
                    ok, state, evidence = latest.get((detail_ids.get(detail.casefold()), reference), (False, "pending", None))
                    if evidence:
                        current = (evidence["result"] == _image_evidence(piece, evidence["result"]["preview"])
                                   and evidence["reference"] == _image_evidence(
                                       references[reference], evidence["reference"]["preview"]))
                        states.append(state if ok and current else "missing")
                    else:
                        states.append("pending")
            if "ok" not in states or any(state not in ("ok", "not_visible") for state in states):
                failures.append({"piece": piece["id"], "reference_piece": reference,
                                 "reason": "missing_reference" if reference not in ancestors else
                                 "comparison_failed" if "missing" in states else "comparison_pending"})
    if failures:
        es = "Falta una comparación visual aprobada con las referencias conservadas."
        en = "A passing visual comparison with the kept references is required."
        raise orders._err(en, 409, error_code="kept_reference_comparison", es=es, en=en, comparisons=failures)
