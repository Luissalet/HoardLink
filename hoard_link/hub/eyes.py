"""Eyes: one shared way for agents to look at any piece and describe it.

A *look* turns one piece into images an agent can open: an image (and enlarged zones of it), frames of a video
and a contact sheet, anatomical views of a model in neutral clay and in its own colours, a web page at three
widths before and after an interaction, the pages of a PDF, slide deck or document, or a playable Godot scene
after a sequence of inputs. A piece can also be an object of an app: the hub calls that app's own view tool
(Vulcan's photos, Prospero's contact sheet, Cicero's page render...) and keeps the images it returns.

Optionally the shared vision model (the Sparks) describes the views and checks a list of details to keep, with
a deadline and a partial answer when part of it does not arrive in time.

Every look is kept in ``<data>/eyes/<look_id>/`` with the exact camera, pose, seconds, pages, widths and inputs
it used. ``same_as`` repeats a previous look on a new revision of the piece and adds a side-by-side comparison,
which is how a defect goes back to the previous station reproducibly. ``hub_eyes_forget`` undoes a look.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import eyes_render as render
from . import eyes_vision as vision
from .facets import Facet, Reply, Request

ID_RE = re.compile(r"^e\d{8}-\d{6}-[0-9a-f]{6}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.(png|jpg|jpeg|json)$")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
#: Parameters each kind keeps from ``same_as`` (what makes two looks comparable).
REPEATABLE = {
    "image": ("size", "facing"),
    "video": ("seconds", "size", "facing"),
    "mesh": ("views", "material", "camera", "turn", "front_axis", "up", "pose", "size", "engine"),
    "web": ("widths", "interaction", "full_page", "facing"),
    "document": ("pages", "size", "facing"),
    "game": ("inputs", "resolution", "scene", "facing"),
    "app": ("facing",),
}
OPTION_KEYS = ("crops", "size", "seconds", "frames", "views", "material", "camera", "turn", "front_axis", "up", "pose", "engine",
               "widths", "interaction", "full_page", "settle_ms", "pages", "max_pages", "inputs", "resolution", "scene",
               "facing")


def _tool(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = (), *,
          read: bool = True) -> dict[str, Any]:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": read, "destructiveHint": False}}


def _now_id() -> str:
    return time.strftime("e%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class EyesFacet(Facet):
    id = "eyes"

    def __init__(self, hub: Any) -> None:
        super().__init__(hub)
        self.root = Path(hub.config.data_dir) / "eyes"
        self.trash = self.root / ".forgotten"
        self._lock = threading.Lock()
        self._renders = threading.BoundedSemaphore(2)   # Blender, browsers and Godot are heavy: two at a time

    # -- store -------------------------------------------------------------------------------------------------
    def _dir(self, look_id: str, *, forgotten: bool = False) -> Path:
        if not isinstance(look_id, str) or not ID_RE.match(look_id):
            raise ValueError(f"Not a look id: {look_id!r}")
        return (self.trash if forgotten else self.root) / look_id

    def _read(self, look_id: str) -> Optional[dict[str, Any]]:
        try:
            path = self._dir(look_id) / "look.json"
        except ValueError:
            return None
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, record: dict[str, Any]) -> None:
        with self._lock:
            _write_json(self._dir(record["id"]) / "look.json", record)

    def _url(self, look_id: str, path: str) -> str:
        return f"/api/eyes/files/{look_id}/{Path(path).name}"

    # -- pieces ------------------------------------------------------------------------------------------------
    def _piece(self, args: dict[str, Any]) -> dict[str, Any]:
        source = args.get("path") or args.get("url")
        app = args.get("app")
        if bool(source) == bool(app):
            raise ValueError("Give one piece: path (file or folder), url, or app with its view tool")
        if app:
            if not isinstance(app, dict) or not app.get("app") or not app.get("tool"):
                raise ValueError("app is {app, tool, arguments}: the app id and its own view tool")
            if self.hub.get(str(app["app"])) is None:
                raise ValueError(f"Unknown app: {app['app']}")
            return {"source": None, "kind": "app", "app": {"app": str(app["app"]), "tool": str(app["tool"]),
                                                           "arguments": dict(app.get("arguments") or {})}}
        source = str(source)
        if not render.is_url(source):
            path = Path(source).expanduser()
            if not path.exists():
                raise ValueError(f"No such file or folder: {source}")
            source = str(path.resolve())
        kind = args.get("kind") or render.detect_kind(source)
        if kind not in render.KINDS:
            raise ValueError(f"kind is one of {', '.join(render.KINDS)}")
        return {"source": source, "kind": kind}

    def _app_views(self, piece: dict[str, Any], out: Path, reason: str) -> dict[str, Any]:
        """Call the app's own view tool and keep every image its answer points to."""
        spec = piece["app"]
        result = self.hub.call_app(spec["app"], spec["tool"], spec["arguments"], caller="hub",
                                   reason=reason or "Look at a piece for an agent", timeout=300)
        if not result.get("ok"):
            raise render.EyesError(f"{spec['app']}.{spec['tool']}: {result.get('error') or result.get('status')}")
        found: list[tuple[str, Any]] = []

        def walk(node: Any, key: str = "") -> None:
            if isinstance(node, dict):
                if isinstance(node.get("data"), str) and str(node.get("mime", "")).startswith("image/"):
                    found.append(("b64", node["data"]))
                for k, v in node.items():
                    walk(v, k)
            elif isinstance(node, list):
                for v in node:
                    walk(v, key)
            elif isinstance(node, str):
                low = node.lower().split("?")[0]
                if low.endswith(IMAGE_SUFFIXES):
                    if os.path.isfile(node):
                        found.append(("file", node))
                    elif node.startswith("/") or render.is_url(node):
                        found.append(("url", node))
                elif node.startswith("data:image/") and ";base64," in node:
                    found.append(("b64", node.split(",", 1)[1]))

        walk(result.get("result"))
        app = self.hub.get(spec["app"])
        base = (getattr(app, "url", None) or "").rstrip("/")
        views, warnings, seen = [], [], set()
        for n, (how, value) in enumerate(found, 1):
            if (how, value) in seen:
                continue
            seen.add((how, value))
            target = out / f"app-{n:02d}.png"
            try:
                if how == "file":
                    shutil.copyfile(value, target)
                elif how == "b64":
                    target.write_bytes(base64.b64decode(value))
                else:
                    url = value if render.is_url(value) else base + value
                    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - the app's own local server
                        target.write_bytes(resp.read())
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Could not keep {value[:120] if how != 'b64' else 'an inline image'}: {exc}")
                continue
            views.append({"name": target.stem, "label": f"{spec['app']} {n}", "path": str(target),
                          "from": value if how != "b64" else "inline"})
        if not views:
            warnings.append("The app answered without any image")
        answer = result.get("result")
        summary = {k: v for k, v in answer.items() if isinstance(v, (str, int, float, bool)) and k != "data"} \
            if isinstance(answer, dict) else {}
        return {"views": views, "facts": {"app": spec["app"], "tool": spec["tool"], "answer": summary},
                "resolved": {}, "warnings": warnings}

    # -- looks -------------------------------------------------------------------------------------------------
    def _piece_front(self, sha256: str, up: Optional[str]) -> Optional[dict[str, Any]]:
        """Use the latest successful declaration for these bytes, including after a rename."""
        declarations = []
        for path in self.root.glob("e*/look.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            piece = record.get("piece") or {}
            if (piece.get("sha256") == sha256 and record.get("status") == "done"
                    and piece.get("front_axis_source") == "declared"
                    and (up is None or piece.get("up") == up)):
                declarations.append(record)
        return max(declarations, key=lambda record: (record["created"], record["id"]), default=None)

    def _options(self, args: dict[str, Any], previous: Optional[dict[str, Any]], kind: str) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        if previous:
            resolved = previous.get("resolved") or {}
            opts.update({k: resolved[k] for k in REPEATABLE.get(kind, ()) if resolved.get(k) is not None})
            if resolved.get("crops"):
                opts["crops"] = resolved["crops"]
        if opts.get("front_axis") or args.get("front_axis") is not None:
            opts.pop("turn", None)
        if args.get("turn") is not None:
            opts.pop("front_axis", None)
        opts.update({k: args[k] for k in OPTION_KEYS if args.get(k) is not None})
        return opts

    def _work(self, record: dict[str, Any], opts: dict[str, Any], vision_opts: Optional[dict[str, Any]],
              previous: Optional[dict[str, Any]]) -> None:
        out = self._dir(record["id"])
        piece = record["piece"]
        try:
            with self._renders:
                if piece["kind"] == "app":
                    result = self._app_views(piece, out, record.get("reason") or "")
                    if opts.get("crops"):
                        result["views"] += render.crop_views(result["views"], opts["crops"], out)
                else:
                    result = render.look(piece["kind"], piece["source"], out, opts)
            resolved = result.get("resolved") or {}
            if piece["kind"] != "mesh" and opts.get("facing"):
                resolved["facing"] = opts["facing"]
            views = [{**v, "url": self._url(record["id"], v["path"]), "facing": self._facing(piece["kind"], v, opts)}
                     for v in result["views"]]
            whole = [v for v in views if not v.get("of")]
            columns = None
            if piece["kind"] == "mesh" and (result.get("resolved") or {}).get("material") == "both":
                columns = max(1, len(whole) // 2)
            sheet = render.contact_sheet(whole, out / "sheet.png", columns=columns,
                                         title=f"{record.get('label') or Path(piece.get('source') or piece['kind']).name}"
                                               f" · {piece['kind']}")
            record.update(views=views, facts=result.get("facts") or {}, resolved=resolved,
                          warnings=result.get("warnings") or [], render_ms=result.get("render_ms"),
                          sheet={"path": sheet, "url": self._url(record["id"], sheet)} if sheet else None)
            if previous:
                record["compare"] = self._compare(previous, record, out)
            self._save(record)
            if vision_opts is not None and views:
                record["vision"] = {"status": "running"}
                self._save(record)
                only = vision_opts.pop("only", None)
                asked = [v for v in views if v["name"] in only] if only else views
                record["vision"] = vision.describe(lambda body: self.hub.link.chat("hub-eyes", body), asked or views,
                                                   kind=piece["kind"], **vision_opts)
            record["status"] = "done"
        except render.EyesError as exc:
            record.update(status="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - reported on the look, never lost in a thread
            record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        record["finished"] = time.time()
        self._save(record)

    @staticmethod
    def _facing(kind: str, view: dict[str, Any], opts: dict[str, Any]) -> str:
        """The view's orientation for checking left and right: known from the camera for models (after ``turn``),
        declared by the caller for everything else, ``unknown`` when not declared."""
        if kind == "mesh":
            return render.mesh_facing(view.get("view"))
        facing = str(opts.get("facing") or "unknown")
        return facing if facing in vision.FACINGS else "unknown"

    def _compare(self, previous: dict[str, Any], record: dict[str, Any], out: Path) -> Optional[dict[str, Any]]:
        before = {v["name"]: v for v in previous.get("views") or [] if not v.get("of")}
        pairs = [(before[v["name"]], v) for v in record.get("views") or [] if not v.get("of") and v["name"] in before]
        if not pairs:
            return {"with": previous["id"], "pairs": 0, "note": "No view of the same name to compare"}
        target = out / "compare.png"
        render.compare_sheet(pairs, target, left=f"{previous['id']}", right=f"{record['id']}")
        return {"with": previous["id"], "pairs": len(pairs), "path": str(target), "url": self._url(record["id"], str(target)),
                "same_piece": (previous.get("piece") or {}).get("sha256") == (record.get("piece") or {}).get("sha256")}

    def look(self, args: dict[str, Any]) -> dict[str, Any]:
        previous = None
        if args.get("same_as"):
            previous = self._read(str(args["same_as"]))
            if previous is None:
                return {"ok": False, "error": f"No look {args['same_as']} to repeat"}
        merged = dict(args)
        if previous and not (args.get("path") or args.get("url") or args.get("app")):
            piece_before = previous.get("piece") or {}
            if piece_before.get("app"):
                merged["app"] = piece_before["app"]
            else:
                merged["path" if not render.is_url(piece_before.get("source") or "") else "url"] = piece_before.get("source")
        if previous and not args.get("kind") and (previous.get("piece") or {}).get("kind") not in (None, "app"):
            merged["kind"] = previous["piece"]["kind"]
        try:
            piece = self._piece(merged)
            if piece["kind"] != "app":
                piece["sha256"] = render.fingerprint(piece["source"])
        except (ValueError, render.EyesError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        opts = self._options(merged, previous, piece["kind"])
        if piece["kind"] == "mesh":
            axis = args.get("front_axis")
            if axis is not None:
                if not isinstance(axis, str) or axis not in render.FRONT_AXIS_TURNS or args.get("turn") is not None:
                    return {"ok": False, "error": "Use one front_axis (-y, -x, +y, +x) or turn / "
                                                  "Indica un eje frontal o un giro"}
                piece.update(front_axis=axis, front_axis_source="declared", up=opts.get("up", "z"))
            elif opts.get("front_axis"):
                piece.update(front_axis=opts["front_axis"], front_axis_source="repeated",
                             up=opts.get("up", "z"), front_axis_look=previous["id"])
            elif "turn" not in opts and piece.get("sha256"):
                declaration = self._piece_front(piece["sha256"], opts.get("up"))
                if declaration:
                    opts.update({key: declaration["piece"][key] for key in ("front_axis", "up")})
                    piece.update(front_axis=opts["front_axis"], up=opts["up"],
                                 front_axis_source="saved", front_axis_look=declaration["id"])
                elif Path(piece["source"]).suffix.lower() == ".3mf" and opts.get("up", "z") == "z":
                    from .eyes_3mf import declared_front

                    if embedded := declared_front(piece["source"]):
                        opts.update(front_axis=embedded, up="z")
                        piece.update(front_axis=embedded, front_axis_source="embedded", up="z")
        vision_opts = None
        if args.get("describe") or args.get("keep"):
            vision_opts = {"keep": list(args.get("keep") or []), "language": args.get("language") or "es",
                           "deadline_s": float(args.get("vision_timeout_s") or 90),
                           "max_views": int(args.get("vision_views") or 6), "only": args.get("ask_views")}
        look_id = _now_id()
        self._dir(look_id).mkdir(parents=True, exist_ok=False)
        record = {"id": look_id, "status": "running", "created": time.time(), "agent": args.get("agent"),
                  "label": args.get("label"), "reason": args.get("reason"), "piece": piece,
                  "request": {k: v for k, v in merged.items() if k not in ("agent", "reason")},
                  "same_as": previous["id"] if previous else None}
        self._save(record)
        worker = threading.Thread(target=self._work, args=(record, opts, vision_opts, previous),
                                  name=f"hub-eyes-{look_id}", daemon=True)
        worker.start()
        worker.join(max(0.0, min(float(args.get("wait_s") or 75), 600)))
        return self._answer(self._read(look_id) or record)

    def _answer(self, record: dict[str, Any]) -> dict[str, Any]:
        out = {"ok": record.get("status") != "failed", **record,
               "repeat": {"tool": "hub_eyes_look", "arguments": {"same_as": record["id"]}},
               "undo": {"tool": "hub_eyes_forget", "arguments": {"look_id": record["id"]}}}
        if record.get("status") == "running":
            out["poll"] = {"tool": "hub_eyes_get", "arguments": {"look_id": record["id"]}}
        return out

    def get_look(self, args: dict[str, Any]) -> dict[str, Any]:
        record = self._read(str(args.get("look_id") or ""))
        if record is None:
            return {"ok": False, "error": f"No look {args.get('look_id')}"}
        return self._answer(record)

    def list_looks(self, args: dict[str, Any]) -> dict[str, Any]:
        want_path = args.get("path")
        if want_path and not render.is_url(want_path):
            want_path = str(Path(want_path).expanduser().resolve())
        rows = []
        if self.root.is_dir():
            for folder in self.root.iterdir():
                if not ID_RE.match(folder.name) or not (folder / "look.json").is_file():
                    continue
                try:
                    rec = json.loads((folder / "look.json").read_text(encoding="utf-8"))
                except ValueError:
                    continue
                piece = rec.get("piece") or {}
                if want_path and piece.get("source") != want_path:
                    continue
                if args.get("sha256") and piece.get("sha256") != args["sha256"]:
                    continue
                if args.get("label") and rec.get("label") != args["label"]:
                    continue
                if args.get("kind") and piece.get("kind") != args["kind"]:
                    continue
                rows.append({"id": rec["id"], "status": rec.get("status"), "created": rec.get("created"),
                             "label": rec.get("label"), "agent": rec.get("agent"), "kind": piece.get("kind"),
                             "source": piece.get("source"), "app": piece.get("app"), "sha256": piece.get("sha256"),
                             "front_axis": piece.get("front_axis"),
                             "views": len(rec.get("views") or []), "sheet": (rec.get("sheet") or {}).get("path"),
                             "same_as": rec.get("same_as"), "vision": (rec.get("vision") or {}).get("status")})
        rows.sort(key=lambda r: r.get("created") or 0, reverse=True)
        limit = max(1, min(int(args.get("limit") or 50), 500))
        return {"ok": True, "looks": rows[:limit], "total": len(rows)}

    def forget(self, args: dict[str, Any]) -> dict[str, Any]:
        look_id = str(args.get("look_id") or "")
        restore = bool(args.get("restore"))
        try:
            src, dst = self._dir(look_id, forgotten=restore), self._dir(look_id, forgotten=not restore)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not src.is_dir():
            return {"ok": False, "error": f"No {'forgotten ' if restore else ''}look {look_id}"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return {"ok": True, "look_id": look_id, "restored" if restore else "forgotten": True,
                "undo": {"tool": "hub_eyes_forget", "arguments": {"look_id": look_id, "restore": not restore}}}

    # -- HTTP --------------------------------------------------------------------------------------------------
    def get(self, req: Request) -> Any:
        if not req.path.startswith("/api/eyes/"):
            return None
        if not req.caller():
            return {"ok": False, "status": 401, "error": "authentication required"}
        if req.path == "/api/eyes/looks":
            return self.list_looks({k: req.q(k) for k in ("path", "sha256", "label", "kind", "limit") if req.q(k)})
        match = re.match(r"^/api/eyes/looks/([^/]+)$", req.path)
        if match:
            return self.get_look({"look_id": match.group(1)})
        match = re.match(r"^/api/eyes/files/([^/]+)/([^/]+)$", req.path)
        if match:
            look_id, name = match.groups()
            try:
                path = self._dir(look_id) / name
            except ValueError:
                return {"ok": False, "status": 404, "error": "not found"}
            if not FILE_RE.match(name) or not path.is_file():
                return {"ok": False, "status": 404, "error": "not found"}
            kind = "application/json" if name.endswith(".json") else \
                "image/jpeg" if name.endswith((".jpg", ".jpeg")) else "image/png"
            return Reply(body=path.read_bytes(), content_type=kind)
        return None

    def post(self, req: Request) -> Any:
        if req.path not in ("/api/eyes/look", "/api/eyes/forget"):
            return None
        if not req.caller():
            return {"ok": False, "status": 401, "error": "authentication required"}
        return self.look(req.body) if req.path == "/api/eyes/look" else self.forget(req.body)

    # -- agent tools -------------------------------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        text = {"type": "string", "minLength": 1}
        box = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
        step = {"type": "object", "description": "One of click, fill [selector, text], press, hover, scroll, select "
                                                 "[selector, value], wait (ms)."}
        return [
            _tool("hub_eyes_look",
                  "Look at a piece and get views to open: image, video, 3D model, web, document, slides, game. "
                  "Mirar una pieza.\n"
                  "Gives PNG views plus one contact sheet. image: zones (crops); video: frames at seconds; mesh: "
                  "anatomical views front/back/left/right/three-quarter in neutral clay and colour, pose, turn and "
                  "a repeatable camera; web: 390/1024/1440 px before and after an interaction; document/PDF/PPTX: "
                  "pages; game (Godot folder): screenshots after key inputs; app: that app's own view tool. "
                  "describe or keep asks the Sparks vision model, with a deadline and partial answers. same_as "
                  "repeats a look on a new revision and adds a before/after comparison. Undo: hub_eyes_forget.",
                  {"path": {"type": "string", "description": "File or folder of the piece."},
                   "url": {"type": "string", "description": "Web page address."},
                   "app": {"type": "object", "description": "{app, tool, arguments}: an app object seen through "
                                                            "its own view tool."},
                   "kind": {"type": "string", "enum": list(render.KINDS)},
                   "same_as": {"type": "string", "description": "Look id to repeat (same camera, pose, seconds...)."},
                   "label": {"type": "string", "description": "Piece or revision name to find the look later."},
                   "crops": {"type": "array", "items": {"type": "object", "properties": {
                       "name": {"type": "string"}, "box": box, "zoom": {"type": "integer"},
                       "views": {"type": "array", "items": {"type": "string"}}}, "required": ["box"]},
                       "description": "Zones as [x, y, w, h], fractions 0-1 or pixels."},
                   "seconds": {"type": "array", "items": {"type": "number"}},
                   "frames": {"type": "integer", "minimum": 1, "maximum": 48},
                   "views": {"type": "array", "items": {"type": "string", "enum": list(render.MESH_VIEWS)}},
                   "material": {"type": "string", "enum": ["neutral", "color", "both"]},
                   "turn": {"type": "number", "description": "Degrees counterclockwise from above, to face the front."},
                   "front_axis": {"type": "string", "enum": list(render.FRONT_AXIS_TURNS),
                                  "description": "Subject's front in Blender world axes after applying up. Saved by "
                                                 "piece fingerprint. Eje frontal de la pieza tras aplicar up."},
                   "facing": {"type": "string", "enum": list(vision.FACINGS),
                              "description": "Pictures, videos, pages: how the subject faces the viewer (front: its "
                                             "left is on the image's right), for checking left and right in keep. "
                                             "Models know it from the camera."},
                   "up": {"type": "string", "enum": ["z", "y"]},
                   "pose": {"type": "object", "description": "{action, frame} or {bones: {name: [x, y, z] degrees}}."},
                   "camera": {"type": "object", "description": "projection, center, ortho_scale, distance, "
                                                              "elevation, lens."},
                   "engine": {"type": "string", "enum": ["workbench", "eevee"]},
                   "widths": {"type": "array", "items": {"type": "integer"}},
                   "interaction": {"type": "array", "items": step},
                   "full_page": {"type": "boolean"},
                   "pages": {"type": "array", "items": {"type": "integer", "minimum": 1}},
                   "inputs": {"type": "array", "items": {"type": "object"},
                              "description": "Game steps: {key|action, wait (ms), shot}."},
                   "size": {"type": "integer", "minimum": 128, "maximum": 4096},
                   "describe": {"type": "boolean", "description": "Ask the vision model for a description."},
                   "keep": {"type": "array", "items": text,
                            "description": "Details to keep, checked one by one: present, absent, mixed, doubtful "
                                           "(seen, but its left or right side is wrong or cannot be told) or "
                                           "unclear. Sides are the subject's own unless the detail says the "
                                           "viewer's."},
                   "language": {"type": "string", "enum": ["es", "en"]},
                   "vision_timeout_s": {"type": "number", "minimum": 5, "maximum": 900},
                   "vision_views": {"type": "integer", "minimum": 1, "maximum": 12},
                   "ask_views": {"type": "array", "items": {"type": "string"},
                                 "description": "Only these view names go to the vision model (e.g. w390-after)."},
                   "wait_s": {"type": "number", "minimum": 0, "maximum": 600,
                              "description": "Return after this long; poll hub_eyes_get if still running."},
                   "agent": {"type": "string"}, "reason": {"type": "string"}},
                  read=False),
            _tool("hub_eyes_get", "One look: views, sheet, description, camera and progress. Ver una mirada.",
                  {"look_id": text}, ("look_id",)),
            _tool("hub_eyes_list", "Looks taken of a piece, newest first. Miradas de una pieza.",
                  {"path": {"type": "string"}, "sha256": {"type": "string"}, "label": {"type": "string"},
                   "kind": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
            _tool("hub_eyes_forget", "Forget a look, or restore it with restore=true; returns undo. Olvidar una mirada.",
                  {"look_id": text, "restore": {"type": "boolean"}}, ("look_id",), read=False),
        ]

    def handlers(self) -> dict[str, Any]:
        return {"hub_eyes_look": self.look, "hub_eyes_get": self.get_look, "hub_eyes_list": self.list_looks,
                "hub_eyes_forget": self.forget}
