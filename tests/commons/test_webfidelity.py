"""Web fidelity scorer: normalized SSIM, regions that differ, cases and the site score."""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from hoard_link import webfidelity as wf  # noqa: E402


def page(blocks=(), size=(200, 160), bg=(255, 255, 255)):
    arr = np.empty((size[1], size[0], 3), dtype="float64")
    arr[...] = bg
    for x, y, w, h, colour in blocks:
        arr[y:y + h, x:x + w] = colour
    return arr


REF = page([(20, 20, 120, 18, (20, 20, 20)), (20, 60, 160, 60, (40, 90, 200)), (20, 130, 60, 16, (200, 40, 40))])


def test_identical_pictures_score_one():
    r = wf.normalized_ssim(REF, REF.copy())
    assert r["score"] == 1.0 and r["ssim"] == 1.0
    assert r["baseline"] < 1.0


def test_blank_candidate_scores_zero_against_a_mostly_white_reference():
    r = wf.normalized_ssim(REF, page())
    assert r["ssim"] > 0.5          # plain SSIM is fooled by the white background ...
    assert r["score"] < 0.05        # ... the normalized score is not


def test_a_moved_block_scores_between():
    moved = page([(20, 20, 120, 18, (20, 20, 20)), (30, 70, 160, 60, (40, 90, 200)), (20, 130, 60, 16, (200, 40, 40))])
    r = wf.normalized_ssim(REF, moved)
    assert 0.05 < r["score"] < wf.THRESHOLD


def test_missing_rows_count_as_different_and_extra_rows_are_ignored():
    short = REF[:100]
    assert wf.normalized_ssim(REF, short)["score"] < 0.9
    taller = np.concatenate([REF, page(size=(200, 40))], axis=0)
    assert wf.normalized_ssim(REF, taller)["score"] == 1.0


def test_flat_reference_keeps_plain_ssim():
    flat = page()
    assert wf.normalized_ssim(flat, flat.copy())["score"] == 1.0


def test_png_bytes_and_paths_are_read(tmp_path):
    from PIL import Image
    path = tmp_path / "ref.png"
    Image.fromarray(REF.astype("uint8")).save(path)
    data = path.read_bytes()
    assert wf.normalized_ssim(str(path), data)["score"] == 1.0


def test_diff_regions_find_the_changed_zone_first():
    cand = REF.copy()
    cand[130:146, 20:80] = (255, 255, 255)             # the red block is gone
    regions = wf.diff_regions(REF, cand, cell=16)
    assert regions, "a changed zone must be reported"
    top = regions[0]
    assert top["x"] <= 20 and top["x"] + top["w"] >= 80
    assert top["y"] <= 130 and top["y"] + top["h"] >= 146
    assert wf.diff_regions(REF, REF.copy()) == []


def test_overlay_draws_on_a_copy_of_the_picture():
    regions = [{"x": 10, "y": 10, "w": 50, "h": 40}]
    img = wf.overlay(REF, regions)
    assert img.size == (200, 160)
    arr = np.asarray(img)
    assert tuple(arr[10, 30]) == (229, 57, 53)


def test_validate_steps():
    assert wf.validate_case({"id": "a", "steps": [{"goto": "/"}, {"checkpoint": "home"}]})["id"] == "a"
    with pytest.raises(ValueError):
        wf.validate_steps([])
    with pytest.raises(ValueError):
        wf.validate_steps([{"goto": "/", "click": "a"}])
    with pytest.raises(ValueError):
        wf.validate_steps([{"teleport": "/"}])


def test_score_case_and_site():
    ok = wf.score_case("a", [{"ok": True}], [{"score": 0.995}, {"score": 1.0}])
    assert ok["passed"] and ok["min_score"] == 0.995
    visual = wf.score_case("b", [{"ok": True}], [{"score": 0.98}])
    assert not visual["passed"] and visual["functional_ok"] and not visual["visual_ok"]
    missing = wf.score_case("c", [], [{"score": None}])
    assert not missing["passed"]
    broken = wf.score_case("d", [{"ok": False}], [{"score": 1.0}])
    assert not broken["passed"] and not broken["functional_ok"]
    site = wf.score_site([ok, visual, missing, broken])
    assert site["cases"] == 4 and site["passed"] == 1 and site["score"] == 0.25
    assert site["functional_rate"] == round(2 / 3, 4)


def test_pairing_by_route_width_and_name():
    ref = [{"key": wf.checkpoint_key("/", 1280, "home")}, {"key": wf.checkpoint_key("/b", 390, "x")}]
    cand = [{"key": wf.checkpoint_key("/", 1280, "home"), "file": "c.png"}]
    pairs = wf.pair_checkpoints(ref, cand)
    assert pairs[0][1]["file"] == "c.png" and pairs[1][1] is None
    assert wf.route_of("http://127.0.0.1:5000/blog/uno?x=1#top") == "/blog/uno?x=1"
    assert wf.route_of("http://127.0.0.1:5000") == "/"


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    def count(self):
        return self.page.dom.get(self.sel, 0) if self.sel != "body" else 1

    def nth(self, _i):
        return self

    @property
    def first(self):
        return self

    def inner_text(self):
        return self.page.text

    def click(self):
        if self.sel == "#missing":
            raise RuntimeError("Timeout 8000ms exceeded")
        self.page.text += " clicked"
        self.page.dom["li"] = self.page.dom.get("li", 0) + 1

    def fill(self, value):
        self.page.values[self.sel] = value

    def input_value(self):
        return self.page.values.get(self.sel, "")


class FakePage:
    def __init__(self):
        self.url, self.text, self.dom, self.values = "about:blank", "", {}, {}

    def set_default_timeout(self, _ms):
        pass

    def goto(self, url, wait_until="load"):
        self.url, self.text = url, "Hola"

    def wait_for_timeout(self, _ms):
        pass

    def locator(self, sel):
        return FakeLocator(self, sel)

    def evaluate(self, _js, _arg=None):
        return None


def test_run_case_records_checks_and_checkpoints():
    shots = []
    case = wf.validate_case({"id": "add", "steps": [
        {"goto": "/"}, {"expect_text": "hola"}, {"fill": "#t", "text": "pan"}, {"expect_value": "#t", "value": "pan"},
        {"click": "button"}, {"expect_count": "li", "n": 1}, {"expect_url": "/"}, {"expect_no_text": "adiós"}, {"checkpoint": "after"}]})
    out = wf.run_case(FakePage(), "http://h:1", case, width=1280, shoot=lambda r, w, n: shots.append((r, w, n)) or f"{n}.png")
    assert out["error"] == "" and all(c["ok"] for c in out["checks"]) and len(out["checks"]) == 5
    assert out["checkpoints"] == [{"name": "after", "route": "/", "width": 1280, "key": "/@1280#after", "file": "after.png"}]
    assert shots == [("/", 1280, "after")]


def test_run_case_stops_at_a_step_that_cannot_run():
    case = wf.validate_case({"id": "x", "steps": [{"goto": "/"}, {"click": "#missing"}, {"checkpoint": "never"}]})
    out = wf.run_case(FakePage(), "http://h:1", case, width=390)
    assert out["error"].startswith("step 2 (click)") and out["checkpoints"] == []
    assert out["checks"][-1]["ok"] is False
