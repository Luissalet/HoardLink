"""Ask the vision model (the hub's shared ``vision`` capability, served by the Sparks) about a look.

One call per view, at most ``max_views`` of them, two at a time through the hub's model gate. Each call returns
a short description and, for every «detail to keep», whether that view shows it. The whole question has a
deadline: what has arrived by then is returned (``status: partial``) and the views still pending are named, so
an agent never waits forever for a description and knows what it is missing.

Left and right are not left to the model. A detail such as «tubo a la izquierda del personaje» is split here into
what to look for («tubo») and the side it must be on; the model is only asked whether it sees the thing and where
it is in the picture as the viewer sees it (a box and a word). The subject's side then follows from the view's
orientation, which a look knows for models (front, three-quarter, back, profile) and the caller declares for
pictures (``facing``). When the thing is seen but its side does not match, or cannot be told, the verdict is
``doubtful`` with the reason; ``absent`` only ever means the thing itself is not seen.
"""

from __future__ import annotations

import base64
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Optional

STATUSES = ("present", "absent", "unclear")
WHERE = ("left", "center", "right")
#: Orientation of a view: ``front`` (the subject faces the viewer, so its left is on the image's right), ``back``
#: (its left is on the image's left), ``profile`` (its left and right run along the line of sight) or ``unknown``.
FACINGS = ("front", "back", "profile", "unknown")

_BOX = {"type": "array", "items": {"type": "number"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "subject_box": _BOX,
        "details": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"}, "detail": {"type": "string"},
            "status": {"type": "string", "enum": list(STATUSES)},
            "where": {"type": "string", "enum": [*WHERE, "unknown"]}, "box": _BOX,
            "note": {"type": "string"}}, "required": ["detail", "status"]}},
    },
    "required": ["description"],
}

PROMPTS = {
    "es": ("Eres los ojos de un estudio de producción. Describe con precisión lo que se ve en esta imagen ({label}, "
           "{kind}): sujeto, pose, objetos, colores, composición y defectos visibles, en 3 a 6 frases en castellano. "
           "No inventes lo que no se ve."),
    "en": ("You are the eyes of a production studio. Describe precisely what this image shows ({label}, {kind}): "
           "subject, pose, objects, colours, composition and visible defects, in 3 to 6 sentences. Do not invent what "
           "is not visible."),
}
KEEP = {
    "es": ("\nComprueba además cada uno de estos detalles numerados. Para cada uno devuelve n, si la imagen lo muestra "
           "(present), muestra que falta o está mal (absent) o no permite saberlo (unclear), y una nota breve. No "
           "razones sobre la izquierda o la derecha del personaje: eso se calcula aparte. Di solo dónde está en la "
           "imagen tal como la ves: «box» es su caja [x0, y0, x1, y1] en coordenadas de 0 a 1000 (0,0 es la esquina "
           "superior izquierda de la imagen) y «where» es left, center o right según quede a la izquierda, en el "
           "centro o a la derecha de la figura principal en la imagen. Devuelve también «subject_box», la caja de la "
           "figura principal, en las mismas coordenadas.\n{items}"),
    "en": ("\nAlso check each of these numbered details. For each, return n, whether the image shows it (present), "
           "shows it missing or wrong (absent) or cannot tell (unclear), and a short note. Do not reason about the "
           "subject's left or right: that is worked out separately. Say only where it is in the image as you see it: "
           "«box» is its box [x0, y0, x1, y1] in 0-1000 coordinates (0,0 is the image's top left corner) and «where» "
           "is left, center or right of the main figure in the image. Also return «subject_box», the main figure's "
           "box, in the same coordinates.\n{items}"),
}

# -- sides named in a detail ---------------------------------------------------------------------------------------

_VIEWER = re.compile(r"espectador|observador|imagen|c[aá]mara|encuadre|pantalla|viewer|camera|image|screen|"
                     r"picture|frame", re.IGNORECASE)
_SIDE_ES = re.compile(
    r"(?:\b(?:a|en|por|hacia)\s+(?:la|el|su)\s+(?:lado\s+)?)?"
    r"\b(?P<side>izquierd[oa]s?|(?<!al )(?<!del )derech[oa]s?)\b"
    r"(?:\s+(?:de\s+la|de\s+su|del?)\s+(?P<who>\w+))?", re.IGNORECASE)
_SIDE_EN = re.compile(
    r"(?:\b(?:on|to|at)\s+(?:the\s+)?)?(?:\b(?P<owner>\w+)'s\s+)?"
    r"\b(?P<side>left|right)\b(?:\s+side)?(?:\s+(?:of|from)\s+(?:the\s+)?(?P<who>\w+))?", re.IGNORECASE)


def sides(detail: str) -> tuple[str, list[dict[str, str]]]:
    """Split a detail into what to look for and the sides it names: «tubo a la izquierda del personaje» gives
    ``("tubo", [{"side": "left", "frame": "subject"}])``. A side is the viewer's when the detail says so («del
    espectador», «viewer's», «of the image»); otherwise it is the subject's own, as in «her left hand»."""
    mentions: list[dict[str, str]] = []

    def cut(match: re.Match[str]) -> str:
        who = " ".join(g for g in (match.groupdict().get("owner"), match.group("who")) if g)
        mentions.append({"side": "left" if match.group("side").lower().startswith(("izq", "left")) else "right",
                         "frame": "viewer" if _VIEWER.search(who) else "subject"})
        return " "

    what = _SIDE_EN.sub(cut, _SIDE_ES.sub(cut, detail))
    what = re.sub(r"\(\s*\)|\[\s*\]", " ", what)
    what = re.sub(r"\s+([,.;:)])", r"\1", re.sub(r"\s{2,}", " ", what)).strip(" ,.;:-")
    what = re.sub(r"\s+\b(?:a|en|y|and|on|at|to)$", "", what).strip()
    return (what or detail.strip()), mentions


def _box(value: Any) -> Optional[list[float]]:
    """A [x0, y0, x1, y1] box in 0-1000 image coordinates, or None. Fractions (all ≤ 1) are scaled up."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if max(box) <= 1.0:
        box = [v * 1000 for v in box]
    if box[2] <= box[0] or box[3] <= box[1] or min(box) < 0 or max(box) > 1000:
        return None
    return box


def place(row: dict[str, Any], subject_box: Any, frame: str) -> tuple[str, str]:
    """Where a seen detail is as the viewer sees it (``left``, ``center``, ``right`` or ``unknown``) and how that was
    decided. Its box is measured against the main figure's centre (subject sides) or the image's (viewer sides); the
    model's word is the fallback, and a word that contradicts the box leaves the place ``unknown``."""
    word = str(row.get("where") or "").lower()
    word = word if word in WHERE else ""
    box, figure = _box(row.get("box")), _box(subject_box)
    measured = ""
    if box:
        if frame == "subject" and figure:
            mid, span = (figure[0] + figure[2]) / 2, figure[2] - figure[0]
        else:
            mid, span = 500.0, 1000.0
        offset = ((box[0] + box[2]) / 2 - mid) / max(span, 1.0)
        measured = "center" if abs(offset) < 0.06 else "right" if offset > 0 else "left"
    if measured and word and {measured, word} == {"left", "right"}:
        return "unknown", f"its box puts it on the image's {measured} but the model says {word}"
    if measured:
        return measured, "box"
    return (word, "word") if word else ("unknown", "the model gave no place for it")


def subject_side(where: str, facing: str) -> Optional[str]:
    """The subject's own side for a place in the image, given the view's orientation; None if it cannot be told."""
    if where not in ("left", "right") or facing not in ("front", "back"):
        return None
    if facing == "back":
        return where
    return "left" if where == "right" else "right"


def judge(detail: str, row: dict[str, Any], subject_box: Any, facing: str) -> dict[str, Any]:
    """One view's verdict on one detail: the model says whether it sees the thing; its side is checked here."""
    seen = row.get("status") if row.get("status") in STATUSES else "unclear"
    out: dict[str, Any] = {"status": seen, "seen": seen}
    _, mentions = sides(detail)
    if seen != "present" or not mentions:
        return out
    subject = {m["side"] for m in mentions if m["frame"] == "subject"}
    viewer = {m["side"] for m in mentions if m["frame"] == "viewer"}
    out["facing"] = facing
    if len(subject) > 1 or len(viewer) > 1:
        return {**out, "status": "doubtful", "reason": "The detail names both sides; check the view by eye"}
    # The subject's side is checked whenever the view's orientation is known (a profile then says it cannot be
    # told); a side «of the viewer» is checked as written when it is all the detail names or the orientation is not
    # known.
    use_subject = bool(subject) and (facing != "unknown" or not viewer)
    frame, wanted = ("subject", next(iter(subject))) if use_subject else ("viewer", next(iter(viewer)))
    where, how = place(row, subject_box, frame)
    out.update(where=where, wanted={"side": wanted, "of": frame})
    if frame == "subject" and facing not in ("front", "back"):
        if facing == "profile":
            reason = "Seen, but a profile view does not tell which side it is on: look at the front or back views"
        else:
            at = f" on the image's {where}" if where in ("left", "right") else ""
            hint = f"; if the subject faces the viewer that is its {subject_side(where, 'front')}" if at else ""
            reason = (f"Seen{at}, but the view's orientation is unknown{hint}. Give facing (front, back or profile) "
                      "to check its side")
        return {**out, "status": "doubtful", "reason": reason}
    if where not in ("left", "right"):
        why = how if where == "unknown" else "it sits in the middle of the figure"
        return {**out, "status": "doubtful", "reason": f"Seen, but its side cannot be told: {why}"}
    found = where if frame == "viewer" else subject_side(where, facing)
    out["side"] = found
    if found != wanted:
        whose = "image's" if frame == "viewer" else "subject's"
        return {**out, "status": "doubtful", "side_ok": False,
                "reason": f"Seen, but on the {whose} {found} and the detail asks for the {wanted}; open the view "
                          "before sending it back"}
    return {**out, "side_ok": True}


def _jpeg(path: str, longest: int = 1024) -> str:
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((longest, longest), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def pick(views: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Spread ``limit`` views evenly over the list (first and last included); crops come after the whole views."""
    whole = [v for v in views if not v.get("of")] or views
    if len(whole) <= limit:
        return whole
    step = (len(whole) - 1) / max(1, limit - 1)
    return [whole[round(i * step)] for i in range(limit)]


def things(keep: list[str]) -> tuple[list[str], list[int]]:
    """What the model is asked about: each distinct thing once, without its side («tubo a la izquierda» and «tubo a
    la derecha» are one question about «tubo», or the model would look for a second tube), and, for each detail,
    the index of its thing."""
    asked: list[str] = []
    index: list[int] = []
    for detail in keep:
        what = sides(detail)[0]
        key = what.lower()
        if key not in (a.lower() for a in asked):
            asked.append(what)
        index.append([a.lower() for a in asked].index(key))
    return asked, index


def _row(rows: list[Any], index: int, what: str, count: int) -> Optional[dict[str, Any]]:
    """The model's answer about thing ``index``: by its number, by its words, or by position."""
    rows = [r for r in rows if isinstance(r, dict)]
    for row in rows:
        if row.get("n") == index + 1:
            return row
    for row in rows:
        if str(row.get("detail", "")).strip().lower() == what.lower():
            return row
    if len(rows) == count:   # a model that rewords the details still answers them in order
        return rows[index]
    return None


def merge_keep(keep: list[str], answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per detail: present (some views show it, none say it is missing, none put it on the wrong side), absent (no
    view sees it), mixed (seen in some views, missing in others: lost in some frames or angles), doubtful (seen,
    but its side is wrong or cannot be told: open the views named) or unclear (no view could tell)."""
    out = []
    asked, index = things(keep)
    for number, detail in enumerate(keep):
        seen = []
        for answer in answers:
            row = _row(answer.get("details") or [], index[number], asked[index[number]], len(asked))
            if row is not None:
                verdict = judge(detail, row, answer.get("subject_box"), answer.get("facing") or "unknown")
                seen.append({"view": answer["view"], **verdict, "note": row.get("note") or ""})
        statuses = {s["status"] for s in seen}
        wrong_side = any(s.get("side_ok") is False for s in seen)
        verdict = ("mixed" if {"present", "absent"} <= statuses else
                   "doubtful" if "present" in statuses and wrong_side else
                   "present" if "present" in statuses else "doubtful" if "doubtful" in statuses else
                   "absent" if "absent" in statuses else "unclear")
        item: dict[str, Any] = {"detail": detail, "status": verdict, "views": seen}
        if verdict == "doubtful":
            item["reason"] = next(s["reason"] for s in seen if s["status"] == "doubtful")
        out.append(item)
    return out


def describe(chat: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]], views: list[dict[str, Any]], *,
             kind: str, keep: list[str] | None = None, language: str = "es", deadline_s: float = 90.0,
             max_views: int = 6) -> dict[str, Any]:
    """``chat(body) -> (status, payload)`` is the hub's model call (``hub.link.chat`` bound to an app id). Each view
    may carry ``facing`` (see ``FACINGS``) for checking the sides a detail names."""
    keep = [str(k).strip() for k in (keep or []) if str(k).strip()]
    language = language if language in PROMPTS else "es"
    chosen = pick(views, max(1, int(max_views)))
    started = time.monotonic()
    deadline = started + max(5.0, float(deadline_s))

    def ask(view: dict[str, Any]) -> dict[str, Any]:
        prompt = PROMPTS[language].format(label=view.get("label") or view["name"], kind=kind)
        if keep:
            prompt += KEEP[language].format(items="\n".join(f"{i}. {t}" for i, t in enumerate(things(keep)[0], 1)))
        left = max(5.0, deadline - time.monotonic())
        status, payload = chat({"capability": "vision", "messages": [{"role": "user", "content": prompt}],
                                "images": [_jpeg(view["path"])], "json": SCHEMA, "timeout_s": left,
                                "max_tokens": 1200, "temperature": 0.2})
        if not payload.get("ok"):
            return {"view": view["name"], "ok": False, "error": payload.get("error") or f"http_{status}",
                    "detail": str(payload.get("detail") or "")[:300]}
        data = payload.get("json") if isinstance(payload.get("json"), dict) else {}
        return {"view": view["name"], "ok": True, "description": data.get("description") or payload.get("text") or "",
                "details": data.get("details") or [], "subject_box": data.get("subject_box"),
                "facing": view.get("facing") or "unknown", "model": payload.get("model")}

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-eyes-vision")
    futures = {pool.submit(ask, view): view for view in chosen}
    done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    answers, failures = [], []
    for future in done:
        try:
            row = future.result()
        except Exception as exc:  # noqa: BLE001
            row = {"view": futures[future]["name"], "ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}
        (answers if row.get("ok") else failures).append(row)
    order = {v["name"]: i for i, v in enumerate(chosen)}
    answers.sort(key=lambda a: order.get(a["view"], 0))
    status = "complete" if answers and not pending and not failures else "partial" if answers else "failed"
    return {
        "status": status, "language": language,
        "model": next((a.get("model") for a in answers if a.get("model")), None),
        "description": "\n".join(f"[{a['view']}] {a['description']}".strip() for a in answers),
        "views": answers, "keep": merge_keep(keep, answers) if keep else [],
        "pending": [futures[f]["name"] for f in pending], "failed": failures,
        "asked": [v["name"] for v in chosen], "ms": int((time.monotonic() - started) * 1000),
        "deadline_s": round(deadline - started, 1),
    }
