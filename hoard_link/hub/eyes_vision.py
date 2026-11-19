"""Ask the vision model (the hub's shared ``vision`` capability, served by the Sparks) about a look.

One call per view, at most ``max_views`` of them, two at a time through the hub's model gate. Each call returns
a short description and, for every «detail to keep», whether that view shows it. The whole question has a
deadline: what has arrived by then is returned (``status: partial``) and the views still pending are named, so
an agent never waits forever for a description and knows what it is missing.
"""

from __future__ import annotations

import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable

STATUSES = ("present", "absent", "unclear")

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "details": {"type": "array", "items": {"type": "object", "properties": {
            "detail": {"type": "string"}, "status": {"type": "string", "enum": list(STATUSES)},
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
    "es": ("\nComprueba además cada uno de estos detalles a conservar y di si esta vista lo muestra (present), "
           "muestra que falta o está mal (absent) o no permite saberlo (unclear), con una nota breve. «Izquierda» y "
           "«derecha» son las del propio personaje u objeto (su mano izquierda), no las del espectador, salvo que el "
           "detalle diga «del espectador»:\n{items}"),
    "en": ("\nAlso check each of these details to keep and say whether this view shows it (present), shows it missing "
           "or wrong (absent) or cannot tell (unclear), with a short note. Left and right are the subject's own (its "
           "left hand), not the viewer's, unless the detail says «viewer's»:\n{items}"),
}


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


def merge_keep(keep: list[str], answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per detail: present (some views show it, none say it is wrong), absent (some say it is missing or wrong, none
    show it), mixed (views disagree: lost in some frames or angles) or unclear (no view could tell)."""
    out = []
    for detail in keep:
        seen = []
        for answer in answers:
            for row in answer.get("details") or []:
                if isinstance(row, dict) and str(row.get("detail", "")).strip().lower() == detail.strip().lower():
                    seen.append({"view": answer["view"], "status": row.get("status"), "note": row.get("note") or ""})
        if not seen and len(keep) == 1:   # a model that rewords the only detail still answered it
            seen = [{"view": a["view"], "status": (a.get("details") or [{}])[0].get("status"),
                     "note": (a.get("details") or [{}])[0].get("note") or ""} for a in answers if a.get("details")]
        statuses = {s["status"] for s in seen}
        verdict = ("mixed" if {"present", "absent"} <= statuses else "present" if "present" in statuses else
                   "absent" if "absent" in statuses else "unclear")
        out.append({"detail": detail, "status": verdict, "views": seen})
    return out


def describe(chat: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]], views: list[dict[str, Any]], *,
             kind: str, keep: list[str] | None = None, language: str = "es", deadline_s: float = 90.0,
             max_views: int = 6) -> dict[str, Any]:
    """``chat(body) -> (status, payload)`` is the hub's model call (``hub.link.chat`` bound to an app id)."""
    keep = [str(k).strip() for k in (keep or []) if str(k).strip()]
    language = language if language in PROMPTS else "es"
    chosen = pick(views, max(1, int(max_views)))
    started = time.monotonic()
    deadline = started + max(5.0, float(deadline_s))

    def ask(view: dict[str, Any]) -> dict[str, Any]:
        prompt = PROMPTS[language].format(label=view.get("label") or view["name"], kind=kind)
        if keep:
            prompt += KEEP[language].format(items="\n".join(f"- {k}" for k in keep))
        left = max(5.0, deadline - time.monotonic())
        status, payload = chat({"capability": "vision", "messages": [{"role": "user", "content": prompt}],
                                "images": [_jpeg(view["path"])], "json": SCHEMA, "timeout_s": left,
                                "max_tokens": 900, "temperature": 0.2})
        if not payload.get("ok"):
            return {"view": view["name"], "ok": False, "error": payload.get("error") or f"http_{status}",
                    "detail": str(payload.get("detail") or "")[:300]}
        data = payload.get("json") if isinstance(payload.get("json"), dict) else {}
        return {"view": view["name"], "ok": True, "description": data.get("description") or payload.get("text") or "",
                "details": data.get("details") or [], "model": payload.get("model")}

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
