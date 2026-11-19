"""The eyes facet: looking at images, videos, models, web pages, documents and app objects, repeating a look on
a new revision, the vision model with a deadline, undo, the HTTP routes and the tool catalogue. Pieces are made
in ``tmp_path``; tests that need a local program (ffmpeg, Blender, a headless browser) skip without it."""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from hoard_link.hub import eyes_render, eyes_vision
from hoard_link.hub.eyes import EyesFacet
from hoard_link.hub.facets import Reply, Request


class FakeLink:
    def __init__(self, answers: dict[str, Any] | None = None, delay: dict[str, float] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers = answers or {}
        self.delay = delay or {}

    def ask(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.chat("test", body)

    def chat(self, app: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.calls.append(body)
        label = body["messages"][0]["content"]
        for key, wait in self.delay.items():
            if key in label:
                time.sleep(wait)
        for key, answer in self.answers.items():
            if key in label:
                return 200, {"ok": True, "json": answer, "model": "fake-vision"}
        return 200, {"ok": True, "json": {"description": "A test picture.", "details": []}, "model": "fake-vision"}


class FakeHub:
    def __init__(self, data: Path) -> None:
        self.config = SimpleNamespace(data_dir=str(data))
        self.link = FakeLink()
        self.apps = {"vulcan": SimpleNamespace(id="vulcan", url="http://127.0.0.1:9")}
        self.app_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.app_result: dict[str, Any] = {}

    def get(self, app_id: str) -> Any:
        return self.apps.get(app_id)

    def call_app(self, app_id: str, tool: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.app_calls.append((app_id, tool, arguments))
        return {"ok": True, "result": self.app_result}


@pytest.fixture()
def eyes(tmp_path: Path) -> EyesFacet:
    return EyesFacet(FakeHub(tmp_path / "data"))


def _picture(path: Path, size=(400, 200)) -> Path:
    img = Image.new("RGB", size, (20, 20, 220))
    img.paste((220, 20, 20), (0, 0, size[0] // 2, size[1]))          # left half red, right half blue
    img.save(path)
    return path


def _request(path: str, body: dict[str, Any] | None = None, *, method: str = "GET", caller: str | None = "ui",
             query: dict[str, list[str]] | None = None) -> Request:
    return Request(method=method, path=path, query=query or {}, body=body or {}, caller=lambda: caller,
                   agent=lambda: False)


# -- images, crops, store, undo --------------------------------------------------------------------------------------

def test_image_look_keeps_views_zones_and_sheet(eyes: EyesFacet, tmp_path: Path) -> None:
    piece = _picture(tmp_path / "lamina.png")
    out = eyes.look({"path": str(piece), "label": "lamina r1",
                     "crops": [{"name": "Mapa", "box": [0.5, 0, 0.5, 1], "zoom": 300}]})
    assert out["ok"] and out["status"] == "done", out
    assert out["piece"]["kind"] == "image" and len(out["piece"]["sha256"]) == 64
    names = [v["name"] for v in out["views"]]
    assert names == ["image", "image--mapa"]
    zone = out["views"][1]
    assert zone["box_px"] == [200, 0, 400, 200] and max(zone["width"], zone["height"]) == 300
    with Image.open(zone["path"]) as img:
        assert img.getpixel((10, 10)) == (20, 20, 220)               # the right half: blue
    assert Path(out["sheet"]["path"]).is_file()
    assert out["undo"] == {"tool": "hub_eyes_forget", "arguments": {"look_id": out["id"]}}
    assert eyes.list_looks({"label": "lamina r1"})["looks"][0]["id"] == out["id"]


def test_forget_and_restore_are_each_others_undo(eyes: EyesFacet, tmp_path: Path) -> None:
    out = eyes.look({"path": str(_picture(tmp_path / "a.png"))})
    gone = eyes.forget(out["undo"]["arguments"])
    assert gone["ok"] and eyes.get_look({"look_id": out["id"]})["ok"] is False
    back = eyes.forget(gone["undo"]["arguments"])
    assert back["ok"] and back["restored"] and eyes.get_look({"look_id": out["id"]})["status"] == "done"
    assert eyes.forget({"look_id": "../../etc"})["ok"] is False


def test_bad_pieces_are_explained(eyes: EyesFacet, tmp_path: Path) -> None:
    assert "No such file" in eyes.look({"path": str(tmp_path / "missing.png")})["error"]
    assert "one piece" in eyes.look({})["error"]
    odd = tmp_path / "notes.xyz"
    odd.write_text("x")
    assert "pass kind" in eyes.look({"path": str(odd)})["error"]
    assert "No look" in eyes.look({"same_as": "e20260101-000000-abcdef"})["error"]


def test_same_as_repeats_zones_and_compares_revisions(eyes: EyesFacet, tmp_path: Path) -> None:
    first = eyes.look({"path": str(_picture(tmp_path / "r1.png")), "crops": [{"name": "z", "box": [0, 0, 0.5, 0.5]}]})
    revision = tmp_path / "r2.png"
    Image.new("RGB", (400, 200), (0, 200, 0)).save(revision)
    second = eyes.look({"same_as": first["id"], "path": str(revision)})
    assert second["status"] == "done" and second["same_as"] == first["id"]
    assert [v["name"] for v in second["views"]] == ["image", "image--z"]       # the zone came with same_as
    assert second["compare"]["pairs"] == 1 and second["compare"]["same_piece"] is False
    assert Path(second["compare"]["path"]).is_file()
    again = eyes.look({"same_as": first["id"]})                               # same piece when none is given
    assert again["piece"]["source"] == first["piece"]["source"] and again["compare"]["same_piece"] is True


# -- vision ----------------------------------------------------------------------------------------------------------

def test_vision_checks_details_and_merges_views(eyes: EyesFacet, tmp_path: Path) -> None:
    eyes.hub.link.answers = {"image": {"description": "Two colour halves.", "details": [
        {"detail": "mitad roja a la izquierda", "status": "present", "note": "visible"}]}}
    out = eyes.look({"path": str(_picture(tmp_path / "p.png")), "keep": ["mitad roja a la izquierda"],
                     "vision_timeout_s": 20})
    v = out["vision"]
    assert v["status"] == "complete" and v["model"] == "fake-vision"
    assert v["keep"][0]["status"] == "present" and "Two colour halves." in v["description"]
    zoned = eyes.look({"path": str(_picture(tmp_path / "q.png")), "crops": [{"box": [0, 0, 0.5, 0.5]}],
                       "describe": True, "ask_views": ["image--zona-1"]})
    assert zoned["vision"]["asked"] == ["image--zona-1"]
    call = eyes.hub.link.calls[0]
    assert call["capability"] == "vision" and call["images"] and "mitad roja" in call["messages"][0]["content"]


def test_vision_deadline_returns_partial_answer(tmp_path: Path) -> None:
    views = []
    for name in ("front", "back"):
        views.append({"name": name, "label": name, "path": str(_picture(tmp_path / f"{name}.png"))})
    link = FakeLink(answers={"front": {"description": "Front.", "details": [{"detail": "tubo", "status": "present"}]}},
                    delay={"back": 3.0})
    started = time.monotonic()
    out = eyes_vision.describe(link.ask, views, kind="mesh", keep=["tubo"], deadline_s=5.0)
    assert time.monotonic() - started < 5.0 + 1.0
    assert out["status"] in ("complete", "partial")
    out = eyes_vision.describe(FakeLink(answers=link.answers, delay={"back": 8.0}).ask, views, kind="mesh",
                               keep=["tubo"], deadline_s=5.0)
    assert out["status"] == "partial" and out["pending"] == ["back"]
    assert out["keep"] == [{"detail": "tubo", "status": "present", "views": [
        {"view": "front", "status": "present", "note": ""}]}]


def test_vision_failure_is_reported_not_raised(tmp_path: Path) -> None:
    views = [{"name": "a", "path": str(_picture(tmp_path / "a.png"))}]
    lost = eyes_vision.merge_keep(["tubo"], [
        {"view": "t0", "details": [{"detail": "tubo", "status": "present"}]},
        {"view": "t8", "details": [{"detail": "tubo", "status": "absent", "note": "otro lado"}]},
        {"view": "t4", "details": [{"detail": "tubo", "status": "unclear"}]}])
    assert lost[0]["status"] == "mixed" and [v["view"] for v in lost[0]["views"]] == ["t0", "t8", "t4"]
    out = eyes_vision.describe(lambda body: (504, {"ok": False, "error": "timeout"}), views, kind="image",
                               deadline_s=5)
    assert out["status"] == "failed" and out["failed"][0]["error"] == "timeout"


def test_pick_spreads_views_and_keeps_ends() -> None:
    views = [{"name": str(i)} for i in range(10)] + [{"name": "zone", "of": "0"}]
    assert [v["name"] for v in eyes_vision.pick(views, 4)] == ["0", "3", "6", "9"]


# -- app objects -----------------------------------------------------------------------------------------------------

def test_app_object_is_seen_through_its_own_view_tool(eyes: EyesFacet, tmp_path: Path) -> None:
    photo = _picture(tmp_path / "photo.png")
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (9, 9, 9)).save(buf, "PNG")
    eyes.hub.app_result = {"ok": True, "sheet_path": str(photo), "count": 2,
                           "_image": {"data": base64.b64encode(buf.getvalue()).decode(), "mime": "image/png"}}
    out = eyes.look({"app": {"app": "vulcan", "tool": "model_render_photos", "arguments": {"id": 7}},
                     "reason": "check the photos"})
    assert out["status"] == "done", out
    assert eyes.hub.app_calls == [("vulcan", "model_render_photos", {"id": 7})]
    assert len(out["views"]) == 2 and out["facts"]["answer"]["count"] == 2
    again = eyes.look({"same_as": out["id"]})
    assert again["status"] == "done" and len(eyes.hub.app_calls) == 2
    assert "Unknown app" in eyes.look({"app": {"app": "nope", "tool": "x"}})["error"]


# -- HTTP ------------------------------------------------------------------------------------------------------------

def test_routes_need_a_caller_and_serve_view_files(eyes: EyesFacet, tmp_path: Path) -> None:
    piece = _picture(tmp_path / "w.png")
    assert eyes.post(_request("/api/eyes/look", {"path": str(piece)}, method="POST", caller=None))["status"] == 401
    out = eyes.post(_request("/api/eyes/look", {"path": str(piece)}, method="POST"))
    listed = eyes.get(_request("/api/eyes/looks", query={"path": [str(piece)]}))
    assert listed["looks"][0]["id"] == out["id"]
    reply = eyes.get(_request(out["views"][0]["url"]))
    assert isinstance(reply, Reply) and reply.body[:4] == b"\x89PNG"
    assert eyes.get(_request(f"/api/eyes/files/{out['id']}/..%2Flook.json"))["status"] == 404
    assert eyes.get(_request("/api/other")) is None


# -- slow work -------------------------------------------------------------------------------------------------------

def test_long_look_returns_running_and_finishes_in_background(eyes: EyesFacet, tmp_path: Path) -> None:
    gate = threading.Event()
    real = eyes_render.look

    def slow(*args: Any, **kwargs: Any) -> Any:
        gate.wait(5)
        return real(*args, **kwargs)

    eyes_render.look, saved = slow, eyes_render.look
    try:
        out = eyes.look({"path": str(_picture(tmp_path / "s.png")), "wait_s": 0.2})
        assert out["status"] == "running" and out["poll"]["tool"] == "hub_eyes_get"
        gate.set()
        for _ in range(100):
            if eyes.get_look({"look_id": out["id"]})["status"] == "done":
                break
            time.sleep(0.05)
        assert eyes.get_look({"look_id": out["id"]})["status"] == "done"
    finally:
        eyes_render.look = saved


# -- kinds that need local programs ----------------------------------------------------------------------------------

def test_detect_kind(tmp_path: Path) -> None:
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<p>x</p>")
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "project.godot").write_text("")
    cases = {"a.mp4": "video", "b.glb": "mesh", "c.pptx": "document", "d.pdf": "document", "e.jpg": "image"}
    for name, kind in cases.items():
        assert eyes_render.detect_kind(str(tmp_path / name)) == kind
    assert eyes_render.detect_kind(str(tmp_path / "site")) == "web"
    assert eyes_render.detect_kind(str(tmp_path / "game")) == "game"
    assert eyes_render.detect_kind("https://example.org/") == "web"


@pytest.mark.skipif(not eyes_render.find_program("ffmpeg"), reason="ffmpeg not installed")
def test_video_frames_at_seconds(eyes: EyesFacet, tmp_path: Path) -> None:
    import subprocess
    clip = tmp_path / "clip.mp4"
    subprocess.run([eyes_render.find_program("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=2:size=160x120:rate=10", "-pix_fmt", "yuv420p", str(clip)], check=True)
    out = eyes.look({"path": str(clip), "seconds": [0, 1.5, 9]})
    assert out["status"] == "done", out
    assert out["facts"]["duration_s"] == pytest.approx(2.0, abs=0.2)
    assert [v["second"] for v in out["views"]] == [0.0, 1.5, pytest.approx(1.9, abs=0.05)]  # clamped to the last frame
    spread = eyes.look({"path": str(clip), "frames": 3})
    assert len(spread["views"]) == 3 and spread["resolved"]["seconds"][0] == 0.0


def test_pdf_pages_with_text(eyes: EyesFacet, tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "deck.pdf"
    doc = fitz.open()
    for n in range(3):
        doc.new_page().insert_text((72, 72), f"Diapositiva {n + 1}")
    doc.save(str(pdf))
    out = eyes.look({"path": str(pdf), "pages": [2, 3, 7], "size": 400})
    assert out["status"] == "done", out
    assert [v["page"] for v in out["views"]] == [2, 3] and "Diapositiva 2" in out["views"][0]["text"]
    assert out["facts"]["pages"] == 3


@pytest.mark.skipif(not eyes_render.find_program("blender"), reason="Blender not installed")
def test_mesh_views_and_repeatable_camera(eyes: EyesFacet, tmp_path: Path) -> None:
    cube = tmp_path / "box.obj"
    cube.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 2\nv 1 0 2\nv 1 1 2\nv 0 1 2\n"
                    "f 1 2 3 4\nf 5 6 7 8\nf 1 2 6 5\nf 2 3 7 6\nf 3 4 8 7\nf 4 1 5 8\n")
    out = eyes.look({"path": str(cube), "views": ["front", "back_left"], "material": "neutral", "size": 160,
                     "wait_s": 300})
    assert out["status"] == "done", out
    assert [v["name"] for v in out["views"]] == ["front-neutral", "back_left-neutral"]
    camera = out["resolved"]["camera"]
    assert camera["projection"] == "ortho" and out["facts"]["faces"] == 6
    bigger = tmp_path / "box2.obj"
    bigger.write_text(cube.read_text().replace(" 2\n", " 3\n"))
    again = eyes.look({"same_as": out["id"], "path": str(bigger), "wait_s": 300})
    assert again["status"] == "done" and again["resolved"]["camera"] == camera     # the same camera, not refitted
    assert again["compare"]["pairs"] == 2


def test_web_widths_and_interaction(eyes: EyesFacet, tmp_path: Path) -> None:
    pytest.importorskip("playwright")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<button id=b onclick=\"document.body.style.background='red'\">x</button>")
    out = eyes.look({"path": str(site), "widths": [390], "interaction": [{"click": "#b"}], "wait_s": 120})
    if out["status"] == "failed" and "browser" in (out.get("error") or "").lower():
        pytest.skip(out["error"])
    assert out["status"] == "done", out
    assert [v["name"] for v in out["views"]] == ["w390", "w390-after"]
    with Image.open(out["views"][1]["path"]) as img:
        assert img.convert("RGB").getpixel((200, 600)) == (255, 0, 0)


# -- catalogue -------------------------------------------------------------------------------------------------------

def test_tools_are_short_and_writes_carry_undo() -> None:
    tools = {t["name"]: t for t in EyesFacet.tools()}
    assert set(tools) == {"hub_eyes_look", "hub_eyes_get", "hub_eyes_list", "hub_eyes_forget"}
    for tool in tools.values():
        assert len(tool["description"].splitlines()[0]) <= 110
    assert not tools["hub_eyes_look"]["annotations"]["readOnlyHint"]
    assert not tools["hub_eyes_forget"]["annotations"]["readOnlyHint"]
    assert tools["hub_eyes_get"]["annotations"]["readOnlyHint"]
    assert set(EyesFacet.tools()[0]["inputSchema"]["properties"]) >= {"path", "url", "app", "same_as", "keep"}
    json.dumps(EyesFacet.tools())


def test_mesh_views_are_named_after_the_subjects_own_side() -> None:
    # Facing the front camera (-Y), the subject's right hand is at -X: the "right" view looks from there.
    assert eyes_render.MESH_VIEWS["right"] == (-1.0, 0.0) and eyes_render.MESH_VIEWS["front_left"][0] > 0
    blender = (Path(eyes_render.__file__).with_name("eyes_blender.py")).read_text(encoding="utf-8")
    for name, direction in eyes_render.MESH_VIEWS.items():
        if direction:
            assert f'"{name}": {direction}' in blender.replace(".0)", ".0)")
