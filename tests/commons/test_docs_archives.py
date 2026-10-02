"""hoard_link.docs.archives: safe names, never-overwrite writes, zip-slip guard."""

from __future__ import annotations

import threading

import pytest

from hoard_link.docs import archives as ar


def test_safe_names():
    assert ar.safe_stem("Factura: enero/2026?") == "2026_"                  # directories are dropped, the rest made safe
    assert ar.safe_stem("Factura: enero 2026?") == "Factura_ enero 2026_"
    assert ar.safe_stem("CON") == "_CON" and ar.safe_stem("") == "document" and ar.safe_stem("...") == "document"
    assert ar.safe_stem("", "informe") == "informe"
    assert ar.safe_stem("a.b.c") == "a.b.c" and len(ar.safe_stem("x" * 500)) <= 110
    assert ar.safe_filename("C:\\Users\\Luis\\Informe final?.PDF") == "Informe final_.PDF"
    assert ar.safe_filename("../../etc/passwd") == "passwd" and ar.safe_filename("") == "file" and ar.safe_filename("Canción ñ.mp3") == "Canción ñ.mp3"
    assert ar.safe_filename("nul.txt") == "_nul.txt"


def test_write_unique_never_overwrites(tmp_path):
    a = ar.write_unique(tmp_path, "report.pdf", b"one")
    b = ar.write_unique(tmp_path, "report.pdf", b"two")
    c = ar.write_unique(tmp_path, "report.pdf", b"three")
    assert [p.name for p in (a, b, c)] == ["report.pdf", "report (2).pdf", "report (3).pdf"]
    assert (a.read_bytes(), b.read_bytes(), c.read_bytes()) == (b"one", b"two", b"three")
    d = ar.write_unique(tmp_path / "new" / "deeper", "x", b"?")                  # creates folders, no extension
    assert d.name == "x" and ar.write_unique(tmp_path / "new" / "deeper", "x", b"?").name == "x (2)"
    h = ar.write_unique(tmp_path, ".hidden", b"1")                              # leading dots are not kept (hidden files are not created)
    assert h.name == "hidden" and ar.write_unique(tmp_path, ".hidden", b"2").name == "hidden (2)"
    assert ar.write_unique(tmp_path, "a/b:c.txt", b"x").name == "b_c.txt"        # sanitised


def test_write_unique_under_a_race(tmp_path):
    paths: list = []
    lock = threading.Lock()

    def work(i):
        p = ar.write_unique(tmp_path, "same.txt", f"data{i}".encode())
        with lock:
            paths.append(p)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(24)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({p.name for p in paths}) == 24
    assert sorted(p.read_text() for p in paths) == sorted(f"data{i}" for i in range(24))


def test_unique_path(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert ar.unique_path(tmp_path, "a.txt").name == "a (2).txt" and ar.unique_path(tmp_path, "b.txt").name == "b.txt"


@pytest.mark.parametrize("name", ["../evil.txt", "a/../../evil.txt", "/etc/passwd", "\\windows\\system32", "C:\\evil.txt", "c:/evil", "..\\..\\x",
                                  "a/b/../../../x", "", ".", "./", "x\x00y"])
def test_safe_member_rejects_zip_slip(tmp_path, name):
    with pytest.raises(ValueError):
        ar.safe_member(tmp_path, name)


def test_safe_member_accepts_normal_names(tmp_path):
    base = tmp_path.resolve()
    assert ar.safe_member(tmp_path, "a.txt") == base / "a.txt"
    assert ar.safe_member(tmp_path, "dir/sub/b.txt") == base / "dir" / "sub" / "b.txt"
    assert ar.safe_member(tmp_path, "dir\\sub\\b.txt") == base / "dir" / "sub" / "b.txt"
    assert ar.safe_member(tmp_path, "./a//b.txt") == base / "a" / "b.txt"
    assert ar.safe_member(tmp_path, "dir/") == base / "dir"


def test_safe_member_refuses_a_symlink_that_escapes(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    try:
        (base / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(ValueError):
        ar.safe_member(base, "link/x.txt")


def test_check_zip_is_reexported():
    from hoard_link.docs import readers_lite

    assert ar.check_zip is readers_lite.check_zip and ar.ZipBombError is readers_lite.ZipBombError
