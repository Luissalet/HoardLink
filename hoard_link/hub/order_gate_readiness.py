"""Check that a piece is ready to be shown in a production gate."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable


MIGRATION = """
    ALTER TABLE agora_piece_checks ADD COLUMN resolves_pending TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE agora_piece_defects ADD COLUMN required INTEGER NOT NULL DEFAULT 1;
"""

_PENDING = re.compile(
    r"\b(?:pendientes? de (?:comprobacion|revision|validacion|verificacion|confirmacion|inspeccion|cotejo|mirar|"
    r"comprobar|revisar|validar|verificar|confirmar)|(?:por|falta) (?:comprobar|revisar|validar|verificar|confirmar)|"
    r"sin (?:comprobar|revisar|validar|verificar|confirmar)|(?:not(?: yet)?|still not) (?:checked|reviewed|verified|validated)|"
    r"(?:pending|awaiting|needs?) (?:a )?(?:(?:visual|manual) )?(?:check|review|verification|validation|inspection)|"
    r"still (?:to be|needs to be) (?:checked|reviewed|verified|validated))\b"
)
_DEFECT = re.compile(
    r"\b(?:defectos?|defects?|limitaciones?|limitations?|manchas?|stains?|aranazos?|scratches?|"
    r"parpadeo|flicker|artefacto visual|visual artifact|deformed|deformado|"
    r"(?:dark|bright) spots?|clipping|intersect(?:s|ion)|atraviesa|no se (?:ve|distingue)|"
    r"(?:remains?|still) (?:broken|missing|incorrect)|queda (?:roto|incompleto)|"
    r"(?:is|are) missing)\b|(?:^|[.!?;:] )falta (?:el|la|un|una)\b"
)
_NO_DEFECT = re.compile(
    r"\b(?:sin|no se (?:ven|observan)|no hay|no(?: visible| known| reported)?) "
    r"(?:defectos?|defects?|manchas?|stains?|aranazos?|scratches?|clipping|parpadeo|flicker)\b"
)


def _plain(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


def _pending(note: str, marks: list[dict[str, Any]]) -> bool:
    return any(_PENDING.search(_plain(value)) for value in (
        note, *(mark.get(key, "") for mark in marks for key in ("note", "how")),
    ))


def pending_sources(piece: dict[str, Any], checks: list[dict[str, Any]]) -> list[int | str]:
    """Return open declarations from the piece note and its check history, in order."""
    pending: dict[int | str, None] = {}
    if _pending(piece["note"], []):
        pending["piece_note"] = None
    for check in checks:
        if _pending(check["note"], json.loads(check["keep"])):
            pending[check["id"]] = None
        elif check["ok"]:
            for source in json.loads(check["resolves_pending"]):
                pending.pop(source, None)
    return list(pending)


def validate_resolutions(piece: dict[str, Any], checks: list[dict[str, Any]], sources: Any, *,
                         ok: bool, note: str, marks: list[dict[str, Any]], error: Callable) -> list[int | str]:
    if sources is None or sources == []:
        return []
    pending = pending_sources(piece, checks)
    if (not isinstance(sources, list) or len(sources) > 60
            or any((type(source) is not int and source != "piece_note") or source not in pending
                   for source in sources)):
        raise error("resolves_pending: indica los IDs pendientes de esta pieza o piece_note. / "
                    "List pending check IDs for this piece or piece_note.")
    if not ok or _pending(note, marks) or any(mark["state"] == "missing" for mark in marks):
        raise error("Solo una comprobación satisfactoria puede cerrar una comprobación pendiente. / "
                    "Only a passing check can resolve a pending check.")
    return list(dict.fromkeys(sources))


def _refuse(error: Callable, code: str, es: str, en: str, **context: Any) -> None:
    raise error(f"{es} / {en}", 409, code=code, message_es=es, message_en=en, **context)


def _listed_in(caption: str, text: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(caption) + r"(?!\w)", text))


def validate(self: Any, pieces: list[dict[str, Any]], gate_note: str, error: Callable) -> None:
    """Run in the opening transaction, before writing a gate or changing an order."""
    disclosed = set()
    for piece in pieces:
        pid = piece["id"]
        es, en = f"La pieza #{pid}", f"Piece #{pid}"
        if piece["status"] in ("stale", "cancelled"):
            _refuse(error, "piece_unavailable", f"{es} está cancelada o caduca.",
                    f"{en} is cancelled or stale.", piece_id=pid)
        checks = self.db.query("SELECT * FROM agora_piece_checks WHERE piece_id=? ORDER BY id", (pid,))
        if not checks:
            _refuse(error, "piece_unchecked", f"{es} no tiene ninguna comprobación.",
                    f"{en} has no recorded check.", piece_id=pid)
        if not checks[-1]["ok"]:
            _refuse(error, "piece_check_failed", f"{es} no ha superado la última comprobación.",
                    f"{en} failed its latest check.", piece_id=pid, check_id=checks[-1]["id"])
        pending = pending_sources(piece, checks)
        if pending:
            _refuse(error, "piece_check_pending", f"{es} declara comprobaciones pendientes. Registra su resultado.",
                    f"{en} declares pending checks. Record their result.", piece_id=pid, pending=pending)
        unchecked = [k["id"] for k in json.loads(piece["keep"]) if k.get("state") not in ("ok", "not_visible")]
        if unchecked:
            _refuse(error, "piece_details_unchecked", f"{es} tiene detalles obligatorios sin comprobar o ausentes.",
                    f"{en} has unchecked or missing required details.", piece_id=pid, detail_ids=unchecked)

        title = _plain(piece["title"])
        first_line = _plain(piece["note"].partition("\n")[0])
        defects = self.db.query(
            "SELECT id, note, required FROM agora_piece_defects WHERE piece_id=? "
            "AND (status='open' OR closed_in<>piece_id) ORDER BY id", (pid,),
        )
        for defect in defects:
            did = defect["id"]
            if defect["required"]:
                _refuse(error, "piece_required_defect", f"{es} incumple el encargo. Corrige el defecto #{did}.",
                        f"{en} fails the brief. Fix defect #{did}.", piece_id=pid, defect_id=did)
            caption = _plain(defect["note"]).rstrip(".!?")
            if not caption or not (_listed_in(caption, title) and _listed_in(caption, first_line)):
                _refuse(error, "piece_undisclosed_defect",
                        f"{es} debe indicar el defecto #{did} en el título y en la primera línea de su nota.",
                        f"{en} must show defect #{did} in its title and the first line of its note.",
                        piece_id=pid, defect_id=did)
            disclosed.add(caption)

    context = _plain(gate_note)
    if _PENDING.search(context):
        _refuse(error, "gate_note_pending", "La nota de la puerta declara una comprobación pendiente.",
                "The gate note declares a pending check.")
    for caption in sorted(disclosed, key=len, reverse=True):
        context = re.sub(r"(?<!\w)" + re.escape(caption) + r"(?!\w)", "", context)
    if _DEFECT.search(_NO_DEFECT.sub("", context)):
        _refuse(error, "gate_undisclosed_defect",
                "La nota de la puerta menciona un defecto. Regístralo en la pieza correspondiente.",
                "The gate note mentions a defect. Record it on the affected piece.")
