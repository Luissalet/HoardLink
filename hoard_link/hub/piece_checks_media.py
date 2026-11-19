"""Image measurements and timecoded visual continuity samples."""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
import subprocess

from .. import proc

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def _dimensions(report, actual, requirements):
    expected = requirements.get("size_px")
    report.add("resolution", None if expected is None else actual == list(expected),
               "Compare pixel size with the declared size.", "Coteja la resolución con la declarada.",
               measured=actual, expected=expected)


def _neutral_border(image):
    from PIL import Image

    rgba = image.convert("RGBA").resize((64, 64), Image.Resampling.BOX)
    border = {(x, y) for x in range(64) for y in range(64) if min(x, y, 63 - x, 63 - y) < 3}
    pixels = [rgba.getpixel(point) for point in border]
    return sum(a <= 16 or max(r, g, b) - min(r, g, b) <= 24 for r, g, b, a in pixels) / len(pixels)


def check_image(path, requirements, report):
    from PIL import Image

    files = sorted(file for file in path.iterdir() if file.suffix.lower() in IMAGE_SUFFIXES) if path.is_dir() else [path]
    if not files:
        raise ValueError("No supported images in the piece")
    measures = []
    for file in files:
        with Image.open(file) as image:
            image.load()
            measures.append({"file": file.name, "size_px": list(image.size),
                             "alpha": "A" in image.getbands() or "transparency" in image.info,
                             "neutral_border_ratio": round(_neutral_border(image), 4)})
    report.measures["images"] = measures
    expected = requirements.get("size_px")
    if len(measures) == 1:
        _dimensions(report, measures[0]["size_px"], requirements)
    else:
        report.add("resolution", None if expected is None else all(row["size_px"] == list(expected) for row in measures),
                   "Compare every image with the declared size.", "Coteja cada imagen con la resolución declarada.",
                   expected=expected)
    if requirements.get("alpha") is not None:
        expected_alpha = requirements["alpha"]
        report.add("alpha", all(row["alpha"] == expected_alpha for row in measures),
                   "Check the alpha channel.", "Comprueba el canal alfa.", expected=expected_alpha)
    if requirements.get("neutral_background", requirements.get("sheet", False)):
        threshold = float(requirements.get("neutral_ratio", 0.90))
        report.add("neutral-background", all(row["neutral_border_ratio"] >= threshold for row in measures),
                   "Measure neutral or transparent pixels along the border.",
                   "Mide el fondo neutro o transparente en el borde.", minimum=threshold)
    if "views" in requirements or requirements.get("sheet"):
        expected_views = requirements.get("views")
        boxes = requirements.get("view_boxes")
        if boxes is not None:
            if len(files) != 1 or not boxes:
                raise ValueError("view_boxes needs a single sheet and at least one box")
            with Image.open(files[0]) as image:
                crops = [_crop(image, box) for box in boxes]
                extrema = [crop.convert("L").getextrema() for crop in crops]
                actual = sum(high - low >= 16 for low, high in extrema)
        else:
            actual = len(files) if path.is_dir() else None
        report.add("views", None if actual is None or expected_views is None else actual == expected_views,
                   "Count view files or non-blank declared regions.",
                   "Cuenta los ficheros de vistas o las regiones declaradas con contenido.",
                   measured=actual, expected=expected_views)


def _crop(image, box):
    if box is None:
        return image
    left, top, right, bottom = map(float, box)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("A region is [left, top, right, bottom] in the 0–1 interval")
    width, height = image.size
    return image.crop((math.floor(left * width), math.floor(top * height),
                       math.ceil(right * width), math.ceil(bottom * height)))


def _command(name, arguments, timeout=15, *, binary=False):
    executable = proc.find_exe(name)
    if not executable:
        raise RuntimeError(f"{name} is not available")
    result = proc.run([executable, *arguments], timeout=timeout, text=not binary)
    if result.returncode:
        detail = result.stderr.decode(errors="replace") if binary else result.stderr
        raise RuntimeError(proc.tail_lines(detail))
    return result.stdout


def _video_info(path):
    raw = _command("ffprobe", ["-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height,duration,avg_frame_rate:format=duration", "-of", "json", str(path)])
    data = json.loads(raw)
    stream = data["streams"][0]
    num, den = map(int, stream["avg_frame_rate"].split("/"))
    return [stream["width"], stream["height"]], float(stream.get("duration", data["format"]["duration"])), num / den


def _frame(path, seconds):
    from PIL import Image

    data = _command("ffmpeg", ["-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.6f}",
                               "-threads", "2", "-i", str(path), "-frames:v", "1", "-vf", "scale=384:-2",
                               "-threads", "1", "-f", "image2pipe", "-c:v", "png", "-"], binary=True)
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


def _signature(image):
    """Small colour/texture histogram for a registered region, entirely CPU and without learned weights."""
    import numpy as np
    from PIL import Image

    image = image.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
    rgb = np.asarray(image, dtype=np.float32) / 255
    luminance = np.asarray(image.convert("L"), dtype=np.float32) / 255
    dy, dx = np.gradient(luminance)
    angle = np.arctan2(dy, dx) % np.pi
    histograms = [np.histogram(channel, bins=16, range=(0, 1))[0] for channel in rgb.transpose(2, 0, 1)]
    histograms.append(np.histogram(angle, weights=np.hypot(dx, dy), bins=16, range=(0, np.pi))[0])
    return [np.sqrt(hist / hist.sum()) if hist.sum() else np.zeros(16) for hist in histograms]


def _similarity(first, second):
    import numpy as np

    return round(float(np.mean([np.dot(a, b) if a.any() or b.any() else 1.0 for a, b in zip(first, second)])), 5)


def _shots(requirements, duration, fps):
    shots = requirements.get("shots")
    if shots is None:
        return [{"id": "1", "start_s": 0, "end_s": duration,
                 "references": requirements.get("references", []), "region": requirements.get("region")}]
    cursor = 0.0
    for shot in shots:
        start, end = float(shot["start_s"]), float(shot["end_s"])
        if abs(start - cursor) > 0.5 / fps or not start < end <= duration + 0.5 / fps:
            raise ValueError("Shots must cover the clip in order without gaps or overlaps")
        cursor = end
    if not shots or abs(cursor - duration) > 0.5 / fps:
        raise ValueError("Shots must cover the complete clip")
    return shots


def check_video(path, requirements, report):
    from PIL import Image

    size, duration, fps = _video_info(path)
    if not (math.isfinite(duration) and duration > 0 and math.isfinite(fps) and fps > 0):
        raise ValueError("Video duration and frame rate must be positive")
    report.measures.update(size_px=size, duration_s=duration, fps=fps)
    _dimensions(report, size, requirements)
    expected = requirements.get("duration_s")
    tolerance = float(requirements.get("duration_tolerance_s", 1 / fps))
    report.add("duration", None if expected is None else abs(duration - float(expected)) <= tolerance + 1e-6,
               "Compare video duration with the declared target.", "Coteja la duración con la declarada.",
               measured=duration, expected=expected, tolerance=tolerance)
    threshold, interval = requirements.get("min_similarity"), float(requirements.get("interval_s", 1))
    if (threshold is not None and not 0 <= float(threshold) <= 1) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("Use a threshold between zero and one and a positive sampling interval")
    report.measures.update(method="colour-gradient-histogram-v1", min_similarity=threshold, interval_s=interval)
    all_samples = []
    for index, shot in enumerate(_shots(requirements, duration, fps), 1):
        label = str(shot.get("id", index))
        references = []
        for ref in shot.get("references", []):
            with Image.open(Path(ref["path"]).expanduser()) as image:
                references.append(_signature(_crop(image, ref.get("region"))))
        samples = []
        all_samples.append({"shot": label, "samples": samples})
        if threshold is None or not references:
            report.add("visual-continuity", None, "Supply reference regions and a similarity threshold.",
                       "Indica regiones de referencia y un umbral de similitud.", shot=label)
            continue
        start, end = float(shot["start_s"]), min(float(shot["end_s"]), duration)
        # Do not sample the next shot at a cut or seek beyond the last decodable frame.
        last = max(start, end - 1 / fps)
        count = math.floor((last - start) / interval) + 1
        if count > 60:
            raise ValueError("Choose a sampling interval yielding no more than 60 samples per shot")
        times = [start + offset * interval for offset in range(count)]
        if last - times[-1] > 0.5 / fps:
            times.append(last)
        for seconds in times:
            signature = _signature(_crop(_frame(path, seconds), shot.get("region")))
            score = max(_similarity(reference, signature) for reference in references)
            samples.append({"time_s": round(seconds, 4), "similarity": score})
        failed = [sample for sample in samples if sample["similarity"] < float(threshold)]
        report.add("visual-continuity", not failed, "Compare visual similarity in the declared region.",
                   "Coteja la similitud visual en la región declarada.", shot=label, details=failed)
    report.measures["shots"] = all_samples
