"""Material lineage and the missing-model handoff for takes and frames."""

from pathlib import Path
from typing import Any

from . import agora_orders as orders


def eligible_model(piece: dict[str, Any]) -> bool:
    return (piece["kind"] in orders.ARTICULATED_KINDS and piece["status"] not in ("cancelled", "stale")
            and bool(piece["sha256"]) and Path(piece["path"]).suffix.lower() == ".glb"
            and any(Path(piece[key]).is_file() for key in ("stored", "path") if piece[key]))


def _take_or_frame(self: Any, piece: dict[str, Any]) -> bool:
    view = orders.view_of(piece)
    if view == "video":
        return True
    if view != "image":
        return False
    if piece["kind"] in ("frame", "video-frame", "keyframe"):
        return True
    return any(orders.view_of(dict(row)) == "video" for row in _parents_in_order(self, piece))


def _parents_in_order(self: Any, piece: dict[str, Any]):
    for relation in orders._parents(piece["derives_from"]):
        if relation["role"] == "material":
            parent = self.db.one("SELECT * FROM agora_pieces WHERE order_id=? AND id=?",
                                 (piece["order_id"], relation["piece"]))
            if parent:
                yield parent


def add_model_materials(self: Any, piece: dict[str, Any], derivation: list) -> list:
    """Pin the GLB revisions that the take or frame used through its materials."""
    if not derivation or not _take_or_frame(self, piece):
        return derivation
    declared = {relation["piece"] for relation in derivation}
    models = {}
    for parent in _parents_in_order(self, piece):
        lineage = [parent] if parent["kind"] in orders.ARTICULATED_KINDS else orders._figure_materials(
            self, parent, same_order=True,
        )
        for model in lineage:
            if model["id"] not in declared and eligible_model(model):
                models[model["id"]] = {"piece": model["id"], "role": "material"}
    return [*derivation, *(models[pid] for pid in sorted(models))][:orders.MAX_ITEMS]


def missing_model(self: Any, piece: dict[str, Any]) -> dict | None:
    if not piece["sha256"] or piece["status"] in ("cancelled", "stale") or not _take_or_frame(self, piece):
        return None
    if any(eligible_model(model) for model in orders._figure_materials(self, piece, same_order=True)):
        return None
    return {
        "station": "vulcan",
        "piece_id": piece["id"],
        "task_id": _model_task(self, piece),
    }


def _model_task(self: Any, piece: dict[str, Any]) -> int | None:
    row = self.db.one("SELECT task_id FROM agora_order_jobs WHERE order_id=? AND branch=? AND removed=0",
                      (piece["order_id"], f"figure-model-{piece['id']}"))
    return int(row["task_id"]) if row else None


def draft_model_request(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Queue the modelling step for a take or frame with no usable material GLB."""
    with self.db.tx():
        order = orders._order_row(self, a.get("order_id"))
        piece = orders._piece_row(self, a.get("piece_id"))
        if (piece["order_id"] != order["id"] or piece["status"] in ("cancelled", "stale")
                or not piece["sha256"] or not _take_or_frame(self, piece)):
            raise orders._err("Choose an available take or frame from this order. / "
                              "Elige una toma o un fotograma disponible de esta orden.")
        step = missing_model(self, piece)
        if step is None:
            return {"ok": True, "status": "model_available", "undo": None}
        if step["task_id"]:
            return {"ok": True, **step, "status": "queued", "existing": True, "undo": None}

        inputs = [piece["id"], *(p["id"] for p in _parents_in_order(self, piece)
                                  if p["status"] not in ("cancelled", "stale"))][:orders.MAX_ITEMS]
        position = f"#{piece['id']} r{piece['revision']} ({piece['sha256']})"
        plan = orders.plan_set(self, {
            "order_id": order["id"], "agent": a.get("agent"), "_person": bool(a.get("_person")),
            "jobs": [{
                "key": "model", "title": f"Modelo articulado a partir de la pieza #{piece['id']}",
                "station": step["station"], "skill": "blender-asset-production",
                "branch": f"figure-model-{piece['id']}", "inputs": inputs, "produces": ["rigged-model"],
                "body": (f"Revisa los materiales de la pieza {position}. Si localizas el GLB articulado de origen, "
                         "regístralo en esta orden y vuelve a registrar la toma o el fotograma con ese material. "
                         "Si partió solo de texto o imagen, prepara un GLB articulado a partir de las vistas "
                         "disponibles y registra esas vistas como referencia para el modelo nuevo. "
                         "Indica qué pose y qué detalles conserva para que se pueda preparar la figura."),
                "check": "Abre el GLB, reproduce la pose y coteja la silueta y los agarres con las vistas de entrada.",
            }],
        }, undo_for_person=True)
    return {"ok": True, **step, "status": "queued", "task_id": plan["keys"]["model"], "undo": plan["undo"]}
