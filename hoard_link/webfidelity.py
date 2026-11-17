"""Web fidelity: how close a rebuilt site is to its reference, case by case and checkpoint by checkpoint.

A *case* is a short script run in a browser on both sites: steps that act (``goto``, ``click``, ``fill``, ``press``, ``select``,
``scroll``, ``wait``), steps that check what the page shows (``expect_text``, ``expect_no_text``, ``expect_url``, ``expect_count``,
``expect_value``) and ``checkpoint`` steps that take a picture of the viewport. A case passes when every check passes on the
candidate and every checkpoint reaches the threshold of normalized SSIM against the reference's picture of the same checkpoint
(same route, same viewport width, same name). A site's score is the share of its cases that pass.

Normalized SSIM: the RGB SSIM of the two pictures (mean of the three channels, 7x7 windows), rescaled against a flat baseline: the
SSIM the reference would get against a flat picture of its own median colour. ``(ssim - baseline) / (1 - baseline)``, kept in
[0, 1], so a blank page does not score well against a mostly white reference. A reference that is itself flat keeps the plain SSIM.

The scoring needs numpy and Pillow, imported when it runs. The step runner only calls methods of a Playwright ``Page`` given by the
caller, so a browser worker can load this file by path without numpy, Pillow or the rest of the package.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional

THRESHOLD = 0.99
WINDOW = 7
PAD = (255, 0, 255)         # what lies below the end of a shorter candidate: never equal to the reference
C1 = (0.01 * 255) ** 2
C2 = (0.03 * 255) ** 2

ACTIONS = ("goto", "click", "fill", "press", "select", "scroll", "wait")
CHECKS = ("expect_text", "expect_no_text", "expect_url", "expect_count", "expect_value")
STEP_KINDS = ACTIONS + CHECKS + ("checkpoint",)

# Runs before the page's own scripts: no transitions or animations, no caret, a fixed clock and seeded random numbers, so two
# loads of the same page draw the same pixels.
FREEZE_JS = r"""
(() => {
  const FIXED = 1767268800000;
  const RealDate = Date;
  const Frozen = new Proxy(RealDate, {
    construct(t, a, n) { return Reflect.construct(t, a.length ? a : [FIXED], n === Frozen ? t : n); },
    apply() { return new RealDate(FIXED).toString(); },
    get(t, p, r) { return p === 'now' ? () => FIXED : Reflect.get(t, p, r); },
  });
  try { Object.defineProperty(window, 'Date', { value: Frozen, writable: true, configurable: true }); } catch (e) {}
  let seed = 0x2545f491;
  Math.random = () => { seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5; return ((seed >>> 0) % 1000000) / 1000000; };
  const css = '*,*::before,*::after{transition:none!important;animation:none!important;caret-color:transparent!important;scroll-behavior:auto!important}';
  const add = () => { const s = document.createElement('style'); s.textContent = css; (document.head || document.documentElement).appendChild(s); };
  if (document.documentElement) add(); else document.addEventListener('DOMContentLoaded', add);
})();
"""


# ---------------------------------------------------------------------------------------------------------------- pictures
def load_rgb(image: Any):
    """A picture as a float64 HxWx3 array: a path, PNG/JPEG bytes, a Pillow image or an array."""
    import io

    import numpy as np
    from PIL import Image

    if isinstance(image, np.ndarray):
        arr = image
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        return arr[..., :3].astype("float64")
    if isinstance(image, (bytes, bytearray)):
        image = Image.open(io.BytesIO(image))
    elif not hasattr(image, "convert"):
        image = Image.open(str(image))
    with image:
        return np.asarray(image.convert("RGB"), dtype="float64")


def fit(ref, cand, pad: tuple[int, int, int] = PAD):
    """The candidate cut or padded to the reference's size: what is missing counts as different, what is extra is not scored."""
    import numpy as np

    h, w = ref.shape[:2]
    out = np.empty_like(ref)
    out[...] = np.asarray(pad, dtype="float64")
    ch, cw = min(h, cand.shape[0]), min(w, cand.shape[1])
    out[:ch, :cw] = cand[:ch, :cw]
    return out


def _box(x, k: int):
    """Mean over every k x k window (valid positions only), with an integral image."""
    import numpy as np

    s = np.cumsum(np.cumsum(np.pad(x, ((1, 0), (1, 0))), axis=0), axis=1)
    return (s[k:, k:] - s[:-k, k:] - s[k:, :-k] + s[:-k, :-k]) / float(k * k)


def ssim_map(ref, cand, window: int = WINDOW):
    """Per-window SSIM averaged over R, G and B; shape (H - window + 1, W - window + 1). Both arrays must have the same shape."""
    import numpy as np

    k = max(1, min(int(window), ref.shape[0], ref.shape[1]))
    total = None
    for c in range(3):
        a, b = ref[..., c], cand[..., c]
        ma, mb = _box(a, k), _box(b, k)
        va = _box(a * a, k) - ma * ma
        vb = _box(b * b, k) - mb * mb
        cov = _box(a * b, k) - ma * mb
        m = ((2 * ma * mb + C1) * (2 * cov + C2)) / ((ma * ma + mb * mb + C1) * (va + vb + C2))
        total = m if total is None else total + m
    return np.clip(total / 3.0, -1.0, 1.0)


def ssim(ref, cand, window: int = WINDOW) -> float:
    return float(ssim_map(ref, cand, window).mean())


def flat_like(ref):
    """A flat picture of the reference's median colour (its background, as a rule)."""
    import numpy as np

    colour = np.median(ref.reshape(-1, 3), axis=0)
    out = np.empty_like(ref)
    out[...] = colour
    return out


def normalized_ssim(reference: Any, candidate: Any, window: int = WINDOW) -> dict[str, Any]:
    """``{score, ssim, baseline, size, candidate_size}``: the score is the SSIM rescaled against the flat baseline, in [0, 1]."""
    ref, cand = load_rgb(reference), load_rgb(candidate)
    fitted = fit(ref, cand)
    s = ssim(ref, fitted, window)
    base = ssim(ref, flat_like(ref), window)
    if base < 1.0 - 1e-9:
        score = (s - base) / (1.0 - base)
    else:
        score = s
    return {"score": round(min(1.0, max(0.0, score)), 5), "ssim": round(s, 5), "baseline": round(base, 5),
            "size": [int(ref.shape[1]), int(ref.shape[0])], "candidate_size": [int(cand.shape[1]), int(cand.shape[0])]}


def diff_regions(reference: Any, candidate: Any, *, cell: int = 24, floor: float = 0.9, limit: int = 8,
                 window: int = WINDOW) -> list[dict[str, Any]]:
    """The zones that differ most: cells of ``cell`` px whose mean SSIM is under ``floor``, joined with their neighbours into boxes.

    Each box: ``{x, y, w, h, ssim, severity}`` in the reference's pixels, the worst first (severity = (1 - mean SSIM) x share of
    the picture it covers, so a large mildly wrong zone and a small very wrong one both show)."""
    import numpy as np

    ref, cand = load_rgb(reference), load_rgb(candidate)
    cand = fit(ref, cand)
    h, w = ref.shape[:2]
    k = max(1, min(int(window), h, w))
    full = np.ones((h, w), dtype="float64")
    off = k // 2
    m = ssim_map(ref, cand, k)
    full[off:off + m.shape[0], off:off + m.shape[1]] = m
    gh, gw = -(-h // cell), -(-w // cell)
    grid = np.ones((gh, gw), dtype="float64")
    for gy in range(gh):
        for gx in range(gw):
            grid[gy, gx] = full[gy * cell:(gy + 1) * cell, gx * cell:(gx + 1) * cell].mean()
    bad = grid < floor
    seen = np.zeros_like(bad)
    boxes: list[dict[str, Any]] = []
    for gy in range(gh):
        for gx in range(gw):
            if not bad[gy, gx] or seen[gy, gx]:
                continue
            stack, cells = [(gy, gx)], []
            seen[gy, gx] = True
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                    if 0 <= ny < gh and 0 <= nx < gw and bad[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            ys, xs = [c[0] for c in cells], [c[1] for c in cells]
            x0, y0 = min(xs) * cell, min(ys) * cell
            x1, y1 = min(w, (max(xs) + 1) * cell), min(h, (max(ys) + 1) * cell)
            mean = float(np.mean([grid[c] for c in cells]))
            share = len(cells) * cell * cell / float(h * w)
            boxes.append({"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0), "ssim": round(mean, 4),
                          "severity": round((1.0 - mean) * min(1.0, share), 6)})
    boxes.sort(key=lambda b: b["severity"], reverse=True)
    return boxes[: max(0, int(limit))]


def overlay(image: Any, regions: Iterable[dict[str, Any]], *, colour: tuple[int, int, int] = (229, 57, 53), dim: float = 0.0):
    """A Pillow picture with every region outlined and numbered in order (1 = the worst). ``dim`` > 0 washes the rest out."""
    from PIL import Image, ImageDraw

    arr = load_rgb(image)
    img = Image.fromarray(arr.clip(0, 255).astype("uint8"), "RGB")
    if dim > 0:
        img = Image.blend(img, Image.new("RGB", img.size, (255, 255, 255)), min(0.9, float(dim)))
    draw = ImageDraw.Draw(img)
    for n, r in enumerate(regions, 1):
        x0, y0, x1, y1 = r["x"], r["y"], r["x"] + max(1, r["w"]) - 1, r["y"] + max(1, r["h"]) - 1
        for t in range(3):
            if x1 - t < x0 + t or y1 - t < y0 + t:      # a thin zone gets as many strokes as fit
                break
            draw.rectangle([x0 + t, y0 + t, x1 - t, y1 - t], outline=colour)
        label = str(n)
        lw, lh = 8 * len(label) + 8, 16
        draw.rectangle([x0, y0, x0 + lw, y0 + lh], fill=colour)
        draw.text((x0 + 4, y0 + 2), label, fill=(255, 255, 255))
    return img


# ---------------------------------------------------------------------------------------------------------------- cases
def checkpoint_key(route: str, width: int, name: str) -> str:
    """How a checkpoint of the candidate finds its reference: route (path and query, no host), viewport width and name."""
    return f"{route or '/'}@{int(width)}#{name}"


def route_of(url: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.-]*://[^/]*(/[^#]*)?", url or "", re.I)
    path = (m.group(1) if m else url) or "/"
    return path


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    """The steps of a case, checked: each one is an object with exactly one known step kind."""
    if not isinstance(steps, list) or not steps:
        raise ValueError("a case needs a non-empty list of steps")
    out = []
    for n, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"step {n} is not an object")
        kinds = [k for k in step if k in STEP_KINDS]
        if len(kinds) != 1:
            raise ValueError(f"step {n} needs exactly one of: {', '.join(STEP_KINDS)}")
        out.append(dict(step))
    return out


def validate_case(case: Any) -> dict[str, Any]:
    if not isinstance(case, dict) or not str(case.get("id") or "").strip():
        raise ValueError("a case needs an id")
    return {**case, "id": str(case["id"]).strip()[:80], "steps": validate_steps(case.get("steps"))}


def _kind(step: dict[str, Any]) -> str:
    return next(k for k in step if k in STEP_KINDS)


def _visible_text(page: Any, selector: str) -> str:
    loc = page.locator(selector or "body")
    if loc.count() == 0:
        return ""
    return "\n".join(loc.nth(i).inner_text() for i in range(min(loc.count(), 50)))


def run_case(page: Any, base_url: str, case: dict[str, Any], *, width: int,
             shoot: Optional[Callable[[str, int, str], str]] = None, timeout_ms: int = 8000) -> dict[str, Any]:
    """Run one case on a Playwright page already sized to ``width``.

    ``shoot(route, width, name)`` takes the checkpoint picture and returns the file it wrote. Returns ``{id, checks: [{step, kind,
    ok, detail}], checkpoints: [{name, route, width, key, file}], error}``; a step that cannot run fails its case's checks and stops it.
    """
    base = base_url.rstrip("/")
    checks: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    error = ""
    page.set_default_timeout(timeout_ms)
    for n, step in enumerate(case["steps"], 1):
        kind = _kind(step)
        value = step[kind]
        try:
            if kind == "goto":
                page.goto(base + "/" + str(value or "").lstrip("/"), wait_until="load")
                page.wait_for_timeout(int(step.get("settle_ms", 250)))
            elif kind == "click":
                page.locator(str(value)).first.click()
                page.wait_for_timeout(int(step.get("settle_ms", 200)))
            elif kind == "fill":
                page.locator(str(value)).first.fill(str(step.get("text", "")))
            elif kind == "press":
                page.locator(str(step.get("on") or "body")).first.press(str(value))
                page.wait_for_timeout(int(step.get("settle_ms", 200)))
            elif kind == "select":
                page.locator(str(value)).first.select_option(str(step.get("option", "")))
            elif kind == "scroll":
                page.evaluate("(y) => window.scrollTo(0, y)", int(value or 0))
                page.wait_for_timeout(150)
            elif kind == "wait":
                page.wait_for_timeout(min(10000, int(value or 0)))
            elif kind == "checkpoint":
                route = route_of(page.url)
                name = str(value or f"cp{n}")
                entry = {"name": name, "route": route, "width": int(width), "key": checkpoint_key(route, width, name), "file": ""}
                if shoot is not None:
                    entry["file"] = shoot(route, int(width), name)
                shots.append(entry)
            elif kind == "expect_text":
                got = _visible_text(page, str(step.get("in") or "body"))
                ok = str(value).casefold() in got.casefold()
                checks.append({"step": n, "kind": kind, "ok": ok, "detail": "" if ok else f"not shown: {str(value)[:80]}"})
            elif kind == "expect_no_text":
                got = _visible_text(page, str(step.get("in") or "body"))
                ok = str(value).casefold() not in got.casefold()
                checks.append({"step": n, "kind": kind, "ok": ok, "detail": "" if ok else f"still shown: {str(value)[:80]}"})
            elif kind == "expect_url":
                route = route_of(page.url)
                ok = route.rstrip("/") == ("/" + str(value).lstrip("/")).rstrip("/")
                checks.append({"step": n, "kind": kind, "ok": ok, "detail": "" if ok else f"at {route[:120]}"})
            elif kind == "expect_count":
                got = page.locator(str(value)).count()
                ok = got == int(step.get("n", 0))
                checks.append({"step": n, "kind": kind, "ok": ok, "detail": "" if ok else f"{got} found, {step.get('n')} expected"})
            elif kind == "expect_value":
                got = page.locator(str(value)).first.input_value()
                ok = got == str(step.get("value", ""))
                checks.append({"step": n, "kind": kind, "ok": ok, "detail": "" if ok else f"value {got[:80]!r}"})
        except Exception as exc:  # noqa: BLE001 - whatever the page does, the case records it and stops
            error = f"step {n} ({kind}): {type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
            checks.append({"step": n, "kind": kind, "ok": False, "detail": error})
            break
    return {"id": case["id"], "checks": checks, "checkpoints": shots, "error": error}


def score_case(case_id: str, checks: list[dict[str, Any]], checkpoints: list[dict[str, Any]], threshold: float = THRESHOLD) -> dict[str, Any]:
    """A case passes when every check passed and every checkpoint has a score of at least ``threshold``.

    ``checkpoints`` carry the ``score`` of their pairing (None when the candidate had no picture for it: that fails)."""
    functional_ok = all(c.get("ok") for c in checks)
    scores = [c.get("score") for c in checkpoints]
    visual_ok = all(s is not None and s >= threshold for s in scores)
    known = [s for s in scores if s is not None]
    return {"id": case_id, "passed": bool(functional_ok and visual_ok), "functional_ok": functional_ok, "visual_ok": visual_ok,
            "checks": len(checks), "checks_passed": sum(1 for c in checks if c.get("ok")),
            "checkpoints": len(scores), "min_score": round(min(known), 5) if known else None,
            "mean_score": round(sum(known) / len(known), 5) if known else None}


def pair_checkpoints(reference: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Optional[dict[str, Any]]]]:
    """Each reference checkpoint with the candidate's checkpoint of the same key (or None)."""
    by_key = {c["key"]: c for c in candidate}
    return [(r, by_key.get(r["key"])) for r in reference]


def score_site(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """The site's score: the share of its cases that pass, with the means behind it."""
    n = len(cases)
    passed = sum(1 for c in cases if c.get("passed"))
    means = [c["mean_score"] for c in cases if c.get("mean_score") is not None]
    checks = sum(c.get("checks", 0) for c in cases)
    return {"cases": n, "passed": passed, "score": round(passed / n, 4) if n else 0.0,
            "visual_mean": round(sum(means) / len(means), 4) if means else None,
            "functional_rate": round(sum(c.get("checks_passed", 0) for c in cases) / checks, 4) if checks else None}
