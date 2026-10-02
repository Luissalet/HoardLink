"""hoard_link.docs.imaging: orientation, EXIF, hashes, thumbnails, shrink-to-limit."""

from __future__ import annotations

import io
import random
import sys

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from hoard_link.docs import imaging as im  # noqa: E402
from hoard_link.errors import Unavailable  # noqa: E402


def _noise(w=96, h=64, seed=1, mode="RGB"):
    rng = random.Random(seed)
    img = Image.new(mode, (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            v = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            px[x, y] = v if mode == "RGB" else v + (rng.randrange(256),)
    return img


def _gradient(w=128, h=96):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 2 % 256, y * 2 % 256, (x + y) % 256)
    return img


def _encode(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _jpeg_with_exif(orientation=1, gps=True, w=60, h=40):
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = "ACME", "Cam 1"
    exif[0x0112] = orientation
    sub = exif.get_ifd(0x8769)
    sub[0x8827] = 200
    sub[0x829D] = 2.8
    sub[0x829A] = 1 / 250
    sub[0x920A] = 35.0
    sub[0x9003] = "2026:03:04 05:06:07"
    sub[0xA434] = "Lens 50"
    if gps:
        g = exif.get_ifd(0x8825)
        g[1], g[2] = "N", (40.0, 25.0, 30.0)
        g[3], g[4] = "W", (3.0, 42.0, 0.0)
    return _encode(_gradient(w, h), "JPEG", exif=exif, quality=90)


# ---- hashes -------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(6))
def test_phash_equals_imagehash(seed):
    imagehash = pytest.importorskip("imagehash")
    img = _noise(80 + seed * 7, 50 + seed * 5, seed) if seed % 2 else _gradient(64 + seed * 9, 64)
    assert im.phash(img) == int(str(imagehash.phash(img)), 16)


def test_phash_is_stable_under_small_changes_and_accepts_bytes():
    base = _gradient(160, 120)
    resized = base.resize((80, 60))
    assert im.hamming(im.phash(base), im.phash(resized)) <= 6
    assert im.hamming(im.phash(base), im.phash(_noise(160, 120, 3))) > 10
    assert im.phash(_encode(base, "PNG")) == im.phash(base)


def test_dhash_hex_and_hamming():
    h = im.dhash(_gradient())
    assert len(h) == 64 and int(h, 16) >= 0 and h == h.lower()
    assert len(im.dhash(_gradient(), size=8)) == 16
    assert im.hamming(h, h) == 0
    assert im.hamming("00", "ff") == 8 and im.hamming(0b1010, 0b0101) == 4 and im.hamming("0f", 0x0F) == 0
    assert im.hamming("00", "0000") == 16                                   # different lengths: everything differs
    assert im.hamming(h, im.dhash(_noise(128, 96, 5))) > 30


def test_content_hash(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"x" * 3_000_000)
    b = tmp_path / "b.bin"
    b.write_bytes(b"x" * 3_000_000)
    assert im.content_hash(a) == im.content_hash(b) and len(im.content_hash(a)) == 64
    assert len(im.content_hash(a, digest=16)) == 32
    import hashlib
    assert im.content_hash(a, "sha256") == hashlib.sha256(a.read_bytes()).hexdigest()
    b.write_bytes(b"y")
    assert im.content_hash(a) != im.content_hash(b)


# ---- open / orientation -------------------------------------------------------------------------

@pytest.mark.parametrize("orientation,size", [(1, (60, 40)), (3, (60, 40)), (6, (40, 60)), (8, (40, 60))])
def test_open_oriented_applies_exif_rotation(orientation, size):
    img = im.open_oriented(_jpeg_with_exif(orientation))
    assert img.size == size and img.mode == "RGB"


def test_open_oriented_modes_and_sources(tmp_path):
    palette = Image.new("P", (8, 8))
    assert im.open_oriented(_encode(palette, "PNG")).mode == "RGB"
    assert im.open_oriented(_encode(Image.new("RGBA", (8, 8), (1, 2, 3, 100)), "PNG")).mode == "RGBA"
    assert im.open_oriented(_encode(Image.new("CMYK", (8, 8)), "JPEG")).mode == "RGB"
    assert im.open_oriented(_encode(Image.new("L", (8, 8)), "PNG")).mode == "L"
    p = tmp_path / "x.png"
    p.write_bytes(_encode(_noise(10, 10), "PNG"))
    assert im.open_oriented(p).size == im.open_oriented(str(p)).size == im.open_oriented(io.BytesIO(p.read_bytes())).size == (10, 10)


def test_open_oriented_errors_and_limits():
    with pytest.raises(ValueError):
        im.open_oriented(b"not an image")
    with pytest.raises(ValueError):
        im.open_oriented(_encode(_noise(), "PNG")[:200])
    with pytest.raises(ValueError, match="pixels"):
        im.open_oriented(_encode(_noise(50, 50), "PNG"), max_pixels=100)
    big = _encode(_gradient(800, 600), "JPEG")
    reduced = im.open_oriented(big, draft=100)
    assert reduced.size[0] >= 100 and reduced.size[0] < 800


def test_flatten_rgb():
    rgba = Image.new("RGBA", (4, 4), (255, 0, 0, 0))
    assert im.flatten_rgb(rgba).getpixel((0, 0)) == (255, 255, 255)
    assert im.flatten_rgb(rgba, (0, 0, 255)).getpixel((0, 0)) == (0, 0, 255)
    solid = Image.new("RGBA", (4, 4), (10, 20, 30, 255))
    assert im.flatten_rgb(solid).getpixel((0, 0)) == (10, 20, 30)
    assert im.flatten_rgb(Image.new("RGB", (2, 2), (5, 5, 5))).mode == "RGB"


# ---- thumbnails ---------------------------------------------------------------------------------

def test_thumbnail_sizes_and_formats():
    src = _encode(_gradient(800, 400), "JPEG")
    for fmt, magic in (("webp", b"WEBP"), ("jpeg", b"\xff\xd8"), ("png", b"\x89PNG")):
        data = im.thumbnail(src, size=200, fmt=fmt)
        out = Image.open(io.BytesIO(data))
        assert max(out.size) == 200 and out.size == (200, 100)
        assert magic in data[:12]
    small = Image.open(io.BytesIO(im.thumbnail(_encode(_gradient(50, 40), "PNG"), size=512)))
    assert small.size == (50, 40)                                            # never upscaled
    rotated = Image.open(io.BytesIO(im.thumbnail(_jpeg_with_exif(6, w=300, h=200), size=100)))
    assert rotated.size == (67, 100)
    flat = Image.open(io.BytesIO(im.thumbnail(_encode(Image.new("RGBA", (30, 30), (0, 0, 0, 0)), "PNG"), size=30, fmt="jpeg")))
    assert flat.mode == "RGB" and flat.getpixel((5, 5))[0] > 240
    with pytest.raises(ValueError):
        im.thumbnail(b"nope")


# ---- exif ----------------------------------------------------------------------------------------

def test_read_exif_values():
    info = im.read_exif(_jpeg_with_exif(6))
    assert info["width"] == 60 and info["height"] == 40 and info["orientation"] == 6
    assert info["make"] == "ACME" and info["model"] == "Cam 1" and info["camera"] == "ACME Cam 1" and info["lens"] == "Lens 50"
    assert info["iso"] == 200 and info["f_number"] == pytest.approx(2.8) and info["focal_length"] == pytest.approx(35.0)
    assert info["exposure"] == "1/250"
    assert info["date"].startswith("2026-03-04T05:06:07")
    assert info["lat"] == pytest.approx(40 + 25 / 60 + 30 / 3600) and info["lon"] == pytest.approx(-(3 + 42 / 60))


def test_read_exif_without_metadata_has_every_key():
    info = im.read_exif(_encode(_noise(), "PNG"))
    assert set(info) >= {"width", "height", "orientation", "date", "lat", "lon", "camera", "make", "model", "lens", "iso", "f_number",
                         "exposure", "focal_length"}
    assert info["width"] == 96 and info["lat"] is None and info["date"] is None and info["camera"] is None
    assert im.read_exif(_jpeg_with_exif(gps=False))["lon"] is None
    with pytest.raises(ValueError):
        im.read_exif(b"junk")


def test_strip_exif_gps_only_keeps_the_rest():
    out = im.strip_exif(_jpeg_with_exif(6), gps_only=True)
    info = im.read_exif(out)
    assert info["lat"] is None and info["lon"] is None
    assert info["make"] == "ACME" and info["iso"] == 200 and info["orientation"] == 6


def test_strip_exif_everything_keeps_orientation_only():
    out = im.strip_exif(_jpeg_with_exif(6))
    info = im.read_exif(out)
    assert info["orientation"] == 6 and info["make"] is None and info["lat"] is None and info["iso"] is None and info["date"] is None
    dropped = im.read_exif(im.strip_exif(_jpeg_with_exif(6), keep_orientation=False))
    assert dropped["orientation"] in (None, 1)
    assert Image.open(io.BytesIO(out)).size == (60, 40)


def test_strip_exif_no_gps_returns_the_original_bytes_and_other_formats():
    plain = _jpeg_with_exif(gps=False)
    assert im.strip_exif(plain, gps_only=True) == plain
    png = _encode(_noise(), "PNG")
    assert Image.open(io.BytesIO(im.strip_exif(png))).size == (96, 64)
    webp = _encode(_noise(), "WEBP")
    assert Image.open(io.BytesIO(im.strip_exif(webp))).format == "WEBP"
    gif = _encode(Image.new("P", (8, 8)), "GIF")
    with pytest.raises(ValueError):
        im.strip_exif(gif)
    assert im.strip_exif(gif, strict=False) == gif
    with pytest.raises(ValueError):
        im.strip_exif(b"junk")


def test_strip_exif_from_a_path(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg_with_exif(1))
    assert im.read_exif(im.strip_exif(p, gps_only=True))["lat"] is None
    assert im.read_exif(p)["lat"] is not None                                # the file itself is untouched


# ---- compress to a limit --------------------------------------------------------------------------

def test_compress_png_ladder():
    big = _encode(_noise(160, 120, 11), "PNG")
    r = im.compress_to_limit(big, len(big) * 2)
    assert r.ok and r.fmt == "png" and r.strategy == "lossless" and Image.open(io.BytesIO(r.data)).size == (160, 120)
    r = im.compress_to_limit(big, len(big) // 3)
    assert r.ok and r.size <= len(big) // 3 and r.strategy.startswith(("quantize", "resize")) and r.size == len(r.data)
    assert not im.compress_to_limit(big, len(big) // 3, lossless_only=True).ok
    tiny = im.compress_to_limit(_encode(_noise(120, 120, 9), "PNG"), 100)
    assert tiny.ok is False and tiny.strategy == "exhausted" and tiny.data


def test_compress_jpeg_and_webp():
    jpg = _encode(_noise(160, 160, 2), "JPEG", quality=98)
    r = im.compress_to_limit(jpg, len(jpg) // 4)
    assert r.ok and r.fmt == "jpeg" and r.size <= len(jpg) // 4 and Image.open(io.BytesIO(r.data)).format == "JPEG"
    assert im.compress_to_limit(jpg, len(jpg) + 1).ok
    assert im.compress_to_limit(jpg, len(jpg) // 4, lossless_only=True).ok is False
    webp = _encode(_noise(64, 64, 4), "WEBP", quality=95)
    r = im.compress_to_limit(webp, len(webp) // 3)
    assert r.ok and r.fmt == "webp" and r.size <= len(webp) // 3 and Image.open(io.BytesIO(r.data)).format == "WEBP"


def test_compress_keeps_exif_and_refuses_other_inputs():
    jpg = _jpeg_with_exif(1, w=60, h=40)
    r = im.compress_to_limit(_encode(_noise(200, 150, 7), "JPEG", quality=98, exif=Image.open(io.BytesIO(jpg)).getexif()), 12_000)
    assert r.ok and im.read_exif(r.data)["make"] == "ACME"
    with pytest.raises(ValueError):
        im.compress_to_limit(_encode(Image.new("P", (8, 8)), "GIF"), 100)
    with pytest.raises(ValueError):
        im.compress_to_limit(b"junk", 100)


def test_compress_animated_is_left_alone():
    frames = [Image.new("RGB", (20, 20), (i * 20, 0, 0)) for i in range(4)]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=50)
    r = im.compress_to_limit(buf.getvalue(), 10)
    assert (r.ok, r.data, r.strategy) == (False, b"", "animated")


# ---- optional dependencies -----------------------------------------------------------------------

def test_register_heif_reports_availability_without_raising():
    assert im.register_heif() in (True, False)
    assert im.register_heif() == im.register_heif()


def test_missing_pillow_is_unavailable_not_importerror(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(Unavailable):
        im.open_oriented(b"x")
    with pytest.raises(Unavailable):
        im.thumbnail(b"x")
