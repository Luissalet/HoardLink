"""hoard_link.atomic: atomic writes, Windows-style retries, tolerant reads, update under a lock."""

from __future__ import annotations

import json
import os
import threading

import pytest

from hoard_link import atomic


def tmp_files(folder):
    return [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_json_roundtrip_unicode_and_parents(tmp_path):
    target = tmp_path / "a" / "b" / "state.json"
    atomic.write_json_atomic(target, {"name": "Ñandú ✓", "n": [1, 2]})
    assert target.read_text(encoding="utf-8").endswith("\n")
    assert "Ñandú ✓" in target.read_text(encoding="utf-8")          # ensure_ascii=False
    assert atomic.read_json(target) == {"name": "Ñandú ✓", "n": [1, 2]}
    assert tmp_files(target.parent) == []


def test_text_keeps_newlines_as_bytes(tmp_path):
    p = tmp_path / "x.txt"
    atomic.write_text_atomic(p, "a\nb\n")
    assert p.read_bytes() == b"a\nb\n"
    atomic.write_text_atomic(p, "é", encoding="latin-1")
    assert p.read_bytes() == b"\xe9"


def test_json_options_and_extra_types(tmp_path):
    p = tmp_path / "o.json"
    atomic.write_json_atomic(p, {"b": 1, "a": {3, 1}, "p": tmp_path}, indent=None, sort_keys=True, fsync=False)
    data = json.loads(p.read_text())
    assert list(data) == ["a", "b", "p"] and data["a"] == [1, 3] and data["p"] == str(tmp_path)


def test_unserialisable_object_leaves_file_alone(tmp_path):
    p = tmp_path / "keep.json"
    atomic.write_json_atomic(p, {"ok": True})
    with pytest.raises(TypeError):
        atomic.write_json_atomic(p, {"bad": object()})
    assert atomic.read_json(p) == {"ok": True}
    assert tmp_files(tmp_path) == []


def test_read_json_tolerates_missing_empty_corrupt_bom(tmp_path):
    assert atomic.read_json(tmp_path / "nope.json") is None
    assert atomic.read_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}
    (tmp_path / "empty.json").write_text("  \n")
    assert atomic.read_json(tmp_path / "empty.json", []) == []
    (tmp_path / "bad.json").write_text('{"a": ')
    assert atomic.read_json(tmp_path / "bad.json", "dflt") == "dflt"
    (tmp_path / "bom.json").write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
    assert atomic.read_json(tmp_path / "bom.json") == {"a": 1}
    (tmp_path / "bin.json").write_bytes(b"\xff\xfe\x00bad")
    assert atomic.read_json(tmp_path / "bin.json", 7) == 7
    assert atomic.read_json(tmp_path, 8) == 8                       # a directory


def test_tmp_name_shape(tmp_path):
    name = atomic.tmp_path_for(tmp_path / "data.json").name
    parts = name.split(".")
    assert parts[0] == "data" and parts[1] == "json" and parts[2] == str(os.getpid()) and parts[-1] == "tmp"
    assert parts[3] == str(threading.get_ident()) and len(parts[4]) == 8


# ------------------------------------------------------------------ replace_with_retry

def make_replace(failures):
    """Raises the given exceptions in order, then succeeds."""
    calls = {"n": 0, "args": []}

    def replace(src, dst):
        calls["n"] += 1
        calls["args"].append((src, dst))
        if failures:
            raise failures.pop(0)
    return replace, calls


def winerr(code):
    e = OSError("locked")           # a plain OSError: errno 13 would already make it a PermissionError
    e.winerror = code
    return e


def test_retry_succeeds_after_sharing_violations_with_backoff():
    sleeps = []
    replace, calls = make_replace([PermissionError("x"), winerr(32), winerr(5), winerr(33)])
    atomic.replace_with_retry("src", "dst", replace=replace, sleep=sleeps.append)
    assert calls["n"] == 5
    assert sleeps == pytest.approx([0.05, 0.1, 0.15, 0.2])


def test_retry_backoff_is_capped():
    sleeps = []
    replace, _ = make_replace([PermissionError()] * 10)
    atomic.replace_with_retry("a", "b", replace=replace, sleep=sleeps.append, attempts=40)
    assert max(sleeps) == 0.25 and sleeps[:5] == pytest.approx([0.05, 0.1, 0.15, 0.2, 0.25])


def test_other_oserror_is_raised_at_once():
    sleeps = []
    err = OSError(2, "no such file")
    replace, calls = make_replace([err])
    with pytest.raises(OSError) as info:
        atomic.replace_with_retry("a", "b", replace=replace, sleep=sleeps.append)
    assert info.value is err and calls["n"] == 1 and sleeps == []
    err2 = winerr(1234)                                           # a winerror that is not a sharing violation
    replace, calls = make_replace([err2])
    with pytest.raises(OSError):
        atomic.replace_with_retry("a", "b", replace=replace, sleep=sleeps.append)
    assert calls["n"] == 1


def test_exhausted_retries_remove_the_temp_file_and_raise_the_last_error(tmp_path):
    src = tmp_path / "x.tmp"
    src.write_text("data")
    sleeps = []
    last = PermissionError("final")
    replace, calls = make_replace([PermissionError("a"), PermissionError("b"), last])
    with pytest.raises(PermissionError) as info:
        atomic.replace_with_retry(src, tmp_path / "dst", attempts=3, replace=replace, sleep=sleeps.append)
    assert info.value is last and calls["n"] == 3 and len(sleeps) == 2
    assert not src.exists()


def test_write_goes_through_the_retry(tmp_path, monkeypatch):
    seen = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        seen["n"] += 1
        if seen["n"] < 3:
            raise PermissionError("held by the indexer")
        return real(src, dst)
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
    monkeypatch.setattr(atomic.os, "replace", flaky)
    atomic.write_text_atomic(tmp_path / "f.txt", "ok")
    assert (tmp_path / "f.txt").read_text() == "ok" and seen["n"] == 3
    assert tmp_files(tmp_path) == []


def test_failure_removes_tmp_file(tmp_path, monkeypatch):
    def boom(src, dst, **kw):
        raise OSError(28, "disk full")
    monkeypatch.setattr(atomic, "replace_with_retry", boom)
    with pytest.raises(OSError):
        atomic.write_bytes_atomic(tmp_path / "f.bin", b"x")
    assert tmp_files(tmp_path) == [] and not (tmp_path / "f.bin").exists()


def test_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    p = tmp_path / "f.txt"
    atomic.write_text_atomic(p, "old")
    monkeypatch.setattr(atomic, "replace_with_retry", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        atomic.write_text_atomic(p, "new")
    assert p.read_text() == "old" and tmp_files(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_mode_is_applied(tmp_path):
    p = tmp_path / "secret"
    atomic.write_bytes_atomic(p, b"x", mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600


def test_concurrent_writers_never_leave_a_partial_file(tmp_path):
    p = tmp_path / "shared.json"
    errors = []

    def writer(i):
        try:
            for j in range(20):
                atomic.write_json_atomic(p, {"writer": i, "j": j, "pad": "x" * 2000}, fsync=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        for _ in range(100):
            data = atomic.read_json(p, None)
            assert data is None or data["pad"] == "x" * 2000

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + [threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert tmp_files(tmp_path) == []


# ------------------------------------------------------------------ update_json

def test_update_json_is_serialised_per_file(tmp_path):
    p = tmp_path / "counter.json"

    def bump(d):
        d["n"] += 1

    def work():
        for _ in range(25):
            atomic.update_json(p, bump, {"n": 0}, fsync=False)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert atomic.read_json(p)["n"] == 200


def test_update_json_returns_value_and_uses_return_over_mutation(tmp_path):
    p = tmp_path / "l.json"
    assert atomic.update_json(p, lambda d: d + [1], []) == [1]
    assert atomic.update_json(p, lambda d: d.append(2), []) == [1, 2]
    assert atomic.read_json(p) == [1, 2]


def test_update_json_default_is_copied_and_errors_write_nothing(tmp_path):
    default = {"items": []}
    p = tmp_path / "d.json"
    atomic.update_json(p, lambda d: d["items"].append(1), default)
    assert default == {"items": []}

    def fail(d):
        d["items"].append(2)
        raise RuntimeError("no")
    with pytest.raises(RuntimeError):
        atomic.update_json(p, fail, default)
    assert atomic.read_json(p) == {"items": [1]}


def test_update_json_recovers_from_corrupt_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{broken")
    assert atomic.update_json(p, lambda d: d.update(a=1), {}) == {"a": 1}
