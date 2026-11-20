"""Renderers for the eyes facet: turn one piece (a file, a folder or a URL) into images an agent can open.

Each ``look_<kind>`` function writes PNG views into ``out`` and returns ``{"views": [...], "facts": {...},
"resolved": {...}, "warnings": [...]}``. ``resolved`` holds the exact parameters used (seconds, camera,
pages, widths...) so the same look can be repeated on a later revision of the piece. Nothing here touches the
piece: folders and slide decks are read where they are or copied to a temporary folder first.

Local programs (Blender, ffmpeg, LibreOffice, Godot) are found through ``HOARD_HUB_<NAME>`` environment variables,
the PATH and their usual install folders.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

KINDS = ("image", "video", "mesh", "web", "document", "game")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
MESH_EXT = {".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply", ".blend", ".3mf", ".usd", ".usdz", ".usdc", ".dae"}
WEB_EXT = {".html", ".htm"}
OFFICE_EXT = {".pptx", ".ppt", ".odp", ".docx", ".doc", ".odt", ".rtf", ".xlsx", ".xls", ".ods", ".key"}
DOC_EXT = {".pdf"} | OFFICE_EXT

#: Anatomical views of a model, named after the subject's own side ("right" shows its right profile): camera
#: direction from the model centre in the frame where the front faces -Y and Z is up (what Blender's glTF importer
#: gives for a glTF model facing +Z), so the subject's right is -X.
MESH_VIEWS = {
    "front": (0.0, -1.0), "front_right": (-0.7071, -0.7071), "right": (-1.0, 0.0), "back_right": (-0.7071, 0.7071),
    "back": (0.0, 1.0), "back_left": (0.7071, 0.7071), "left": (1.0, 0.0), "front_left": (0.7071, -0.7071),
    "top": None, "bottom": None,
}
DEFAULT_MESH_VIEWS = ("front", "front_right", "right", "back", "left", "front_left")
FRONT_AXIS_TURNS = {"-y": 0.0, "-x": 90.0, "+y": 180.0, "+x": -90.0}


def mesh_facing(view: Optional[str]) -> str:
    """How a model view faces the camera: ``front`` and the three-quarter fronts show the subject's left on the
    image's right, ``back`` ones on the image's left; profiles, top and bottom cannot tell left from right."""
    direction = MESH_VIEWS.get(view or "")
    if not direction:
        return "unknown"
    return "front" if direction[1] < -0.5 else "back" if direction[1] > 0.5 else "profile"
DEFAULT_WIDTHS = (390, 1024, 1440)
VIEWPORT_HEIGHTS = {390: 844, 1024: 768, 1440: 900}


class EyesError(RuntimeError):
    """A piece that cannot be looked at, with the reason an agent can act on."""


# -- pieces --------------------------------------------------------------------------------------------------------

def is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", source or "", re.I))


def detect_kind(source: str) -> str:
    if is_url(source):
        return "web"
    path = Path(source)
    if path.is_dir():
        if (path / "project.godot").is_file():
            return "game"
        if (path / "index.html").is_file():
            return "web"
        raise EyesError(f"Folder has neither project.godot nor index.html: {source}")
    ext = path.suffix.lower()
    if ext == "project.godot" or path.name == "project.godot":
        return "game"
    for kind, exts in (("image", IMAGE_EXT), ("video", VIDEO_EXT), ("mesh", MESH_EXT), ("web", WEB_EXT),
                       ("document", DOC_EXT)):
        if ext in exts:
            return kind
    if ext == ".zip":
        with zipfile.ZipFile(path) as zf:
            if any(n.lower().endswith((".html", ".htm")) for n in zf.namelist()):
                return "web"
    raise EyesError(f"Unknown piece type for {path.name}; pass kind")


def fingerprint(source: str, limit: int = 1 << 30) -> Optional[str]:
    """SHA-256 of a file, or of a folder's relative paths and contents (up to ``limit`` bytes); None for URLs."""
    if is_url(source):
        return None
    path = Path(source)
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    read = 0
    for item in sorted(p for p in path.rglob("*") if p.is_file() and ".godot" not in p.relative_to(path).parts):
        digest.update(item.relative_to(path).as_posix().encode("utf-8") + b"|")
        with item.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                read += len(block)
                if read > limit:
                    return None
                digest.update(block)
    return digest.hexdigest()


# -- local programs ------------------------------------------------------------------------------------------------

def _version_key(path: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", path)[:3]) or (0,)


def find_program(name: str) -> Optional[str]:
    env = os.environ.get(f"HOARD_HUB_{name.upper()}")
    if env and Path(env).is_file():
        return env
    found = shutil.which(name) or shutil.which(f"{name}.exe")
    if found:
        return found
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
    patterns = {
        "blender": ["Blender Foundation/Blender */blender.exe"],
        "soffice": ["LibreOffice/program/soffice.exe"],
        "godot": ["Godot*/Godot*_console.exe", "Godot*/Godot*.exe"],
        "ffmpeg": ["ffmpeg/bin/ffmpeg.exe"], "ffprobe": ["ffmpeg/bin/ffprobe.exe"],
    }.get(name, [])
    hits = [hit for root in roots if root for pattern in patterns for hit in glob.glob(os.path.join(root, pattern))]
    return max(hits, key=_version_key) if hits else None


def _need(name: str) -> str:
    found = find_program(name)
    if not found:
        raise EyesError(f"{name} not found: install it or set HOARD_HUB_{name.upper()}")
    return found


def _run(cmd: list[str], timeout: float, **kwargs: Any) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise EyesError(f"{Path(cmd[0]).name} did not finish within {timeout:.0f}s") from exc


# -- images --------------------------------------------------------------------------------------------------------

def _image_lib():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - Pillow ships with the hub
        raise EyesError("Pillow is required to look at images") from exc
    return Image, ImageDraw, ImageFont


def _font(size: int):
    _, _, ImageFont = _image_lib()
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _save_fit(img, path: Path, longest: int) -> dict[str, Any]:
    Image, _, _ = _image_lib()
    img = img.convert("RGBA") if img.mode in ("P", "LA") else img
    if max(img.size) > longest:
        scale = longest / max(img.size)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    img.save(path)
    return {"width": img.width, "height": img.height}


def _box(spec: Any, size: tuple[int, int]) -> tuple[int, int, int, int]:
    if not (isinstance(spec, (list, tuple)) and len(spec) == 4 and all(isinstance(v, (int, float)) for v in spec)):
        raise EyesError("A crop box is [x, y, width, height], as fractions of the image (0-1) or in pixels")
    x, y, w, h = (float(v) for v in spec)
    if max(x, y, w, h) <= 1.0:
        x, w = x * size[0], w * size[0]
        y, h = y * size[1], h * size[1]
    left, top = max(0, round(x)), max(0, round(y))
    right, bottom = min(size[0], round(x + w)), min(size[1], round(y + h))
    if right - left < 2 or bottom - top < 2:
        raise EyesError(f"Crop box {list(spec)} falls outside the image")
    return left, top, right, bottom


def crop_views(views: list[dict[str, Any]], crops: list[dict[str, Any]], out: Path) -> list[dict[str, Any]]:
    """Cut the named zones out of every view (or the views a crop names) and enlarge them."""
    Image, ImageDraw, _ = _image_lib()
    added: list[dict[str, Any]] = []
    for index, crop in enumerate(crops or []):
        name = re.sub(r"[^a-z0-9_-]+", "-", str(crop.get("name") or f"zona-{index + 1}").lower()).strip("-") or f"zona-{index + 1}"
        wanted = crop.get("views")
        for view in views:
            if wanted and view["name"] not in wanted:
                continue
            with Image.open(view["path"]) as img:
                box = _box(crop.get("box"), img.size)
                part = img.crop(box)
                zoom = int(crop.get("zoom") or 768)
                if max(part.size) < zoom:
                    scale = zoom / max(part.size)
                    part = part.resize((round(part.width * scale), round(part.height * scale)), Image.LANCZOS)
                target = out / f"{view['name']}--{name}.png"
                part.save(target)
            added.append({**{k: v for k, v in view.items() if k not in ("path", "width", "height", "name")},
                          "name": f"{view['name']}--{name}", "path": str(target), "of": view["name"],
                          "zone": name, "box_px": list(box), "width": part.width, "height": part.height})
    return added


def contact_sheet(views: list[dict[str, Any]], target: Path, *, cell: int = 420, columns: Optional[int] = None,
                  title: str = "") -> Optional[str]:
    """One image with every view and its label, so a single file shows the whole look."""
    if not views:
        return None
    Image, ImageDraw, _ = _image_lib()
    columns = columns or min(4, len(views))
    rows = -(-len(views) // columns)
    with Image.open(views[0]["path"]) as first:          # cells take the views' shape (wide video, tall page)
        ratio = min(2.0, max(0.5, first.height / max(1, first.width)))
    cell_w, cell_h = cell, round(cell * ratio)
    label_h, head = 30, (34 if title else 0)
    sheet = Image.new("RGB", (columns * cell_w, head + rows * (cell_h + label_h)), (28, 30, 34))
    draw = ImageDraw.Draw(sheet)
    font = _font(18)
    if title:
        draw.text((10, 7), title[:120], fill=(235, 235, 235), font=font)
    for i, view in enumerate(views):
        with Image.open(view["path"]) as img:
            img = img.convert("RGBA")
            img.thumbnail((cell_w - 8, cell_h - 8), Image.LANCZOS)
            x, y = (i % columns) * cell_w, head + (i // columns) * (cell_h + label_h)
            back = Image.new("RGBA", img.size, (60, 62, 68, 255))
            back.alpha_composite(img)
            sheet.paste(back.convert("RGB"), (x + (cell_w - img.width) // 2, y + (cell_h - img.height) // 2))
        draw.text((x + 8, y + cell_h + 4), str(view.get("label") or view["name"])[:46], fill=(220, 220, 220), font=font)
    sheet.save(target)
    return str(target)


def compare_sheet(pairs: list[tuple[dict[str, Any], dict[str, Any]]], target: Path, *, left: str, right: str,
                  cell: int = 480) -> str:
    """Before and after, one row per view of the same name."""
    Image, ImageDraw, _ = _image_lib()
    label_h, head = 28, 34
    sheet = Image.new("RGB", (2 * cell, head + len(pairs) * (cell + label_h)), (28, 30, 34))
    draw = ImageDraw.Draw(sheet)
    font = _font(18)
    draw.text((10, 7), f"← {left}", fill=(235, 235, 235), font=font)
    draw.text((cell + 10, 7), f"→ {right}", fill=(235, 235, 235), font=font)
    for row, pair in enumerate(pairs):
        y = head + row * (cell + label_h)
        for col, view in enumerate(pair):
            with Image.open(view["path"]) as img:
                img = img.convert("RGB")
                img.thumbnail((cell - 8, cell - 8), Image.LANCZOS)
                sheet.paste(img, (col * cell + (cell - img.width) // 2, y + (cell - img.height) // 2))
        draw.text((8, y + cell + 4), str(pair[1].get("label") or pair[1]["name"])[:60], fill=(220, 220, 220), font=font)
    sheet.save(target)
    return str(target)


def look_image(source: str, out: Path, opts: dict[str, Any]) -> dict[str, Any]:
    Image, ImageDraw, _ = _image_lib()
    with Image.open(source) as img:
        facts = {"width": img.width, "height": img.height, "mode": img.mode, "format": img.format}
        size = _save_fit(img.copy(), out / "image.png", int(opts.get("size") or 1600))
    views = [{"name": "image", "label": f"{facts['width']}x{facts['height']}", "path": str(out / "image.png"), **size}]
    return {"views": views, "facts": facts, "resolved": {"size": int(opts.get("size") or 1600)}}


# -- video ---------------------------------------------------------------------------------------------------------

def _probe(source: str) -> dict[str, Any]:
    proc = _run([_need("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", source], 60)
    if proc.returncode != 0:
        raise EyesError(f"ffprobe could not read the video: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    data = json.loads(proc.stdout or b"{}")
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if stream is None:
        raise EyesError("No video stream in the file")
    num, _, den = str(stream.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0)
    return {"duration_s": round(duration, 3), "fps": round(fps, 3), "width": stream.get("width"),
            "height": stream.get("height"), "codec": stream.get("codec_name"),
            "audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}


def look_video(source: str, out: Path, opts: dict[str, Any]) -> dict[str, Any]:
    facts = _probe(source)
    duration = facts["duration_s"]
    last = max(0.0, duration - (1.0 / facts["fps"] if facts["fps"] else 0.05))
    seconds = opts.get("seconds")
    if seconds:
        seconds = [min(max(0.0, float(s)), last) for s in seconds]
    else:
        count = max(1, min(int(opts.get("frames") or 8), 48))
        seconds = [round(last * i / max(1, count - 1), 3) for i in range(count)] if count > 1 else [0.0]
    width = int(opts.get("size") or 960)
    ffmpeg = _need("ffmpeg")
    views, warnings = [], []
    for second in seconds:
        name = f"t{second:07.3f}s".replace(".", "_")
        target = out / f"{name}.png"
        proc = _run([ffmpeg, "-v", "error", "-y", "-ss", f"{second:.3f}", "-i", source, "-frames:v", "1",
                     "-vf", f"scale='min({width},iw)':-2", str(target)], 120)
        if proc.returncode != 0 or not target.is_file():
            warnings.append(f"No frame at {second:.3f}s: {proc.stderr.decode('utf-8', 'replace')[:160]}")
            continue
        views.append({"name": name, "label": f"{second:.2f} s", "second": second, "path": str(target)})
    return {"views": views, "facts": facts, "resolved": {"seconds": seconds, "size": width}, "warnings": warnings}


# -- meshes (Blender) ----------------------------------------------------------------------------------------------

def look_mesh(source: str, out: Path, opts: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
    names = list(opts.get("views") or DEFAULT_MESH_VIEWS)
    unknown = [n for n in names if n not in MESH_VIEWS]
    if unknown:
        raise EyesError(f"Unknown mesh views {unknown}; use {sorted(MESH_VIEWS)}")
    material = opts.get("material") or "both"
    if material not in ("neutral", "color", "both"):
        raise EyesError("material is neutral, color or both")
    front_axis = opts.get("front_axis")
    if front_axis is not None and (not isinstance(front_axis, str) or front_axis not in FRONT_AXIS_TURNS
                                   or opts.get("turn") is not None):
        raise EyesError("Use one front_axis (-y, -x, +y, +x) or turn / Indica un eje frontal o un giro")
    turn = FRONT_AXIS_TURNS[front_axis] if front_axis else float(opts.get("turn") or 0.0)
    spec = {
        "source": str(Path(source).resolve()), "out": str(out.resolve()), "views": names,
        "materials": ["neutral", "color"] if material == "both" else [material],
        "size": int(opts.get("size") or 640), "turn": turn,
        "up": opts.get("up") or "z", "pose": opts.get("pose") or None, "camera": opts.get("camera") or None,
        "engine": opts.get("engine") or "workbench",
    }
    blender = _need("blender")
    source_is_3mf = Path(source).suffix.lower() == ".3mf"
    with tempfile.TemporaryDirectory(prefix="hub-eyes-mesh-") as scratch:
        if source_is_3mf:
            from .eyes_3mf import to_glb

            target = Path(scratch) / "piece.glb"
            unit = to_glb(source, target)
            spec["source"] = str(target)
        spec_path = Path(scratch) / "mesh-spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        script = Path(__file__).with_name("eyes_blender.py")
        proc = _run([blender, "-b", "--factory-startup", "--python", str(script), "--", str(spec_path)], timeout)
    text = proc.stdout.decode("utf-8", "replace")
    line = next((l for l in reversed(text.splitlines()) if l.startswith("EYES_RESULT ")), None)
    if line is None:
        tail = (proc.stderr.decode("utf-8", "replace") or text)[-600:]
        raise EyesError(f"Blender did not render the model (exit {proc.returncode}): {tail}")
    result = json.loads(line[len("EYES_RESULT "):])
    if not result.get("ok"):
        raise EyesError(result.get("error") or "Blender failed")
    if front_axis:
        result["resolved"]["front_axis"] = front_axis
    if source_is_3mf:
        result["facts"].update(source_format="3mf", source_unit=unit, geometry_only=True)
        result["warnings"].append("3MF preview uses build geometry. Print colours and materials are unavailable. "
                                  "/ La vista 3MF muestra la geometría de impresión sin colores ni materiales.")
    return {k: result.get(k) for k in ("views", "facts", "resolved", "warnings")}


# -- web pages (Playwright) ----------------------------------------------------------------------------------------

class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass


class _Served:
    """A folder served on 127.0.0.1 for as long as the look lasts (pages with relative links and fetches)."""

    def __init__(self, folder: Path) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(folder)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _web_root(source: str, scratch: Path) -> tuple[Path, str]:
    path = Path(source)
    if path.is_dir():
        return path, "index.html"
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            zf.extractall(scratch / "site")
        pages = sorted((p for p in (scratch / "site").rglob("*.htm*")), key=lambda p: (p.name != "index.html", len(p.parts)))
        if not pages:
            raise EyesError("The ZIP has no HTML page")
        return pages[0].parent, pages[0].name
    return path.parent, path.name


def _browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as first:  # noqa: BLE001 - the bundled browser may be missing; use an installed one
        candidates = [os.environ.get("HOARD_HUB_BROWSER_EXE", "")]
        for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            candidates += [os.path.join(root, "BraveSoftware/Brave-Browser/Application/brave.exe"),
                           os.path.join(root, "Google/Chrome/Application/chrome.exe"),
                           os.path.join(root, "Microsoft/Edge/Application/msedge.exe")]
        for exe in candidates:
            if exe and os.path.isfile(exe):
                return playwright.chromium.launch(headless=True, executable_path=exe)
        raise EyesError(f"No headless browser: {first}") from first


def _step(page: Any, step: dict[str, Any]) -> str:
    if "click" in step:
        page.click(step["click"], timeout=8000)
        return f"click {step['click']}"
    if "fill" in step:
        selector, text = step["fill"]
        page.fill(selector, str(text), timeout=8000)
        return f"fill {selector}"
    if "press" in step:
        page.keyboard.press(str(step["press"]))
        return f"press {step['press']}"
    if "hover" in step:
        page.hover(step["hover"], timeout=8000)
        return f"hover {step['hover']}"
    if "scroll" in step:
        page.mouse.wheel(0, float(step["scroll"]))
        return f"scroll {step['scroll']}"
    if "select" in step:
        selector, value = step["select"]
        page.select_option(selector, str(value), timeout=8000)
        return f"select {selector}"
    if "wait" in step:
        page.wait_for_timeout(min(float(step["wait"]), 15000))
        return f"wait {step['wait']}"
    raise EyesError(f"Unknown interaction step {step}; use click, fill, press, hover, scroll, select or wait")


def look_web(source: str, out: Path, opts: dict[str, Any]) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise EyesError("Playwright is not installed in the hub's Python") from exc
    widths = [int(w) for w in (opts.get("widths") or DEFAULT_WIDTHS)]
    steps = list(opts.get("interaction") or [])
    full_page = bool(opts.get("full_page", False))
    views, warnings, errors = [], [], []
    served = None
    with tempfile.TemporaryDirectory(prefix="hub-eyes-web-") as tmp:
        if is_url(source):
            url = source
        else:
            root, entry = _web_root(source, Path(tmp))
            served = _Served(root)
            url = served.url + entry
        result: dict[str, Any] = {}

        def run() -> None:  # Playwright's sync API refuses to run inside an asyncio loop: give it its own thread
            try:
                with sync_playwright() as p:
                    browser = _browser(p)
                    try:
                        for width in widths:
                            page = browser.new_page(viewport={"width": width, "height": VIEWPORT_HEIGHTS.get(width, 900)})
                            page.on("console", lambda m, w=width: errors.append(f"{w}px console {m.type}: {m.text}"[:300])
                                    if m.type == "error" else None)
                            page.on("pageerror", lambda e, w=width: errors.append(f"{w}px page error: {e}"[:300]))
                            page.goto(url, wait_until="networkidle", timeout=30000)
                            page.wait_for_timeout(int(opts.get("settle_ms") or 400))
                            target = out / f"w{width}.png"
                            page.screenshot(path=str(target), full_page=full_page)
                            views.append({"name": f"w{width}", "label": f"{width} px", "width_px": width, "path": str(target)})
                            if steps:
                                done = []
                                for step in steps:
                                    done.append(_step(page, step))
                                page.wait_for_timeout(int(opts.get("settle_ms") or 400))
                                target = out / f"w{width}-after.png"
                                page.screenshot(path=str(target), full_page=full_page)
                                views.append({"name": f"w{width}-after", "label": f"{width} px · after",
                                              "width_px": width, "steps": done, "path": str(target)})
                            if width == widths[0]:
                                result["title"] = page.title()
                            page.close()
                    finally:
                        browser.close()
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(float(opts.get("timeout_s") or 180))
        if served:
            served.close()
        if worker.is_alive():
            raise EyesError("The browser did not finish in time")
        if "error" in result:
            err = result["error"]
            raise err if isinstance(err, EyesError) else EyesError(f"Browser: {err}")
    return {"views": views, "facts": {"url": url if is_url(source) else None, "title": result.get("title"),
                                      "console_errors": errors[:40]},
            "resolved": {"widths": widths, "interaction": steps, "full_page": full_page}, "warnings": warnings}


# -- documents, PDF and slides -------------------------------------------------------------------------------------

def _to_pdf(source: str, scratch: Path) -> Path:
    soffice = _need("soffice")
    profile = (scratch / "lo-profile").resolve().as_uri()
    proc = _run([soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", str(scratch), source], 240)
    pdf = scratch / (Path(source).stem + ".pdf")
    if not pdf.is_file():
        raise EyesError(f"LibreOffice could not convert the document: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return pdf


def look_document(source: str, out: Path, opts: dict[str, Any]) -> dict[str, Any]:
    try:
        import pymupdf as fitz  # noqa: N813
    except ImportError:
        try:
            import fitz  # type: ignore[no-redef]
        except ImportError as exc:
            raise EyesError("PyMuPDF is required to render documents") from exc
    width = int(opts.get("size") or 1280)
    with tempfile.TemporaryDirectory(prefix="hub-eyes-doc-") as tmp:
        pdf = Path(source) if Path(source).suffix.lower() == ".pdf" else _to_pdf(source, Path(tmp))
        views = []
        with fitz.open(str(pdf)) as doc:
            count = doc.page_count
            pages = [int(p) for p in (opts.get("pages") or range(1, min(count, int(opts.get("max_pages") or 24)) + 1))]
            for number in pages:
                if not 1 <= number <= count:
                    continue
                page = doc[number - 1]
                zoom = width / max(1.0, page.rect.width)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                target = out / f"page-{number:03d}.png"
                pix.save(str(target))
                text = " ".join(page.get_text().split())
                views.append({"name": f"page-{number:03d}", "label": f"p. {number}", "page": number,
                              "path": str(target), "text": text[:500]})
            facts = {"pages": count, "converted_from": None if pdf == Path(source) else Path(source).suffix.lower(),
                     "page_size_pt": [round(doc[0].rect.width), round(doc[0].rect.height)] if count else None}
    return {"views": views, "facts": facts, "resolved": {"pages": pages, "size": width}}


# -- playable scenes (Godot) ---------------------------------------------------------------------------------------

GODOT_DRIVER = r'''
extends SceneTree

var spec := {}

func _initialize() -> void:
    spec = JSON.parse_string(FileAccess.get_file_as_string(OS.get_environment("HUB_EYES_SPEC")))
    var scene_path: String = spec.get("scene", "")
    if scene_path == "":
        scene_path = ProjectSettings.get_setting("application/run/main_scene", "")
    var packed = load(scene_path)
    if packed == null:
        printerr("EYES_FAIL no scene " + scene_path)
        quit(2)
        return
    root.add_child.call_deferred(packed.instantiate())
    _drive.call_deferred()

func _press(key_name: String, pressed: bool) -> void:
    var event := InputEventKey.new()
    event.keycode = OS.find_keycode_from_string(key_name)
    event.physical_keycode = event.keycode
    event.pressed = pressed
    Input.parse_input_event(event)

func _shot(name: String) -> void:
    await RenderingServer.frame_post_draw
    var image := root.get_texture().get_image()
    image.save_png(spec["out"].path_join(name + ".png"))
    print("EYES_SHOT " + name)

func _drive() -> void:
    for i in range(int(spec.get("warmup_frames", 30))):
        await process_frame
    await _shot("start")
    var n := 0
    for step in spec.get("steps", []):
        n += 1
        if step.has("key"):
            _press(step["key"], true)
            await process_frame
            _press(step["key"], false)
        if step.has("action"):
            Input.action_press(step["action"])
            await process_frame
            Input.action_release(step["action"])
        if step.has("wait"):
            await create_timer(float(step["wait"]) / 1000.0).timeout
        if step.get("shot", false):
            await _shot("step-%02d" % n)
    await create_timer(float(spec.get("settle_ms", 600)) / 1000.0).timeout
    await _shot("end")
    print("EYES_DONE")
    quit(0)
'''


def look_game(source: str, out: Path, opts: dict[str, Any], timeout: float = 240.0) -> dict[str, Any]:
    project = Path(source)
    if project.is_file():
        project = project.parent
    if not (project / "project.godot").is_file():
        raise EyesError("A playable scene is a Godot project folder (with project.godot)")
    godot = _need("godot")
    steps = list(opts.get("inputs") or [])
    width, height = (int(v) for v in (opts.get("resolution") or (960, 660)))
    with tempfile.TemporaryDirectory(prefix="hub-eyes-game-") as tmp:
        copy = Path(tmp) / "project"
        shutil.copytree(project, copy)       # Godot writes its cache into the project: never into the piece
        driver = Path(tmp) / "eyes_driver.gd"
        driver.write_text(GODOT_DRIVER, encoding="utf-8")
        spec = {"out": out.resolve().as_posix(), "steps": steps, "scene": opts.get("scene") or "",
                "settle_ms": int(opts.get("settle_ms") or 600), "warmup_frames": int(opts.get("warmup_frames") or 30)}
        spec_path = Path(tmp) / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        env = {**os.environ, "HUB_EYES_SPEC": str(spec_path)}
        cmd = [godot, "--path", str(copy), "--script", str(driver), "--resolution", f"{width}x{height}",
               "--position", "-32000,-32000", "--audio-driver", "Dummy", "--", "--hub-eyes"]
        if not (copy / ".godot" / "imported").is_dir():
            _run([godot, "--headless", "--path", str(copy), "--import"], timeout, env=env)
        proc = _run(cmd, timeout, env=env)
        log = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    shots = re.findall(r"EYES_SHOT (\S+)", log)
    if "EYES_DONE" not in log:
        raise EyesError(f"Godot did not finish the input sequence (exit {proc.returncode}): {log[-600:]}")
    views = [{"name": name, "label": name, "path": str(out / f"{name}.png")} for name in shots]
    errors = [l.strip() for l in log.splitlines() if "ERROR" in l or "SCRIPT ERROR" in l][:20]
    return {"views": views, "facts": {"engine": "godot", "errors": errors, "log_tail": log[-800:]},
            "resolved": {"inputs": steps, "resolution": [width, height], "scene": opts.get("scene") or ""}}


RENDERERS: dict[str, Callable[[str, Path, dict[str, Any]], dict[str, Any]]] = {
    "image": look_image, "video": look_video, "mesh": look_mesh, "web": look_web, "document": look_document,
    "game": look_game,
}


def look(kind: str, source: str, out: Path, opts: dict[str, Any]) -> dict[str, Any]:
    if kind not in RENDERERS:
        raise EyesError(f"Unknown kind {kind}; use {', '.join(KINDS)}")
    started = time.monotonic()
    result = RENDERERS[kind](source, out, opts)
    result.setdefault("warnings", [])
    result["warnings"] = list(result.get("warnings") or [])
    views = list(result.get("views") or [])
    if opts.get("crops"):
        views += crop_views(views, opts["crops"], out)
        result["resolved"]["crops"] = opts["crops"]
    result["views"] = views
    result["render_ms"] = int((time.monotonic() - started) * 1000)
    return result

