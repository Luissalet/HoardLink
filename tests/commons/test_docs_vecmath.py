"""hoard_link.docs.vecmath: BLOB format compatibility, top-k with and without numpy, rank fusion."""

from __future__ import annotations

import math
import random
import struct
import sys
from array import array

import pytest

from hoard_link.docs import vecmath as vm


def test_pack_is_float32_little_endian_and_matches_array_f():
    v = [0.5, -1.25, 2.0, 0.0, 3.14159]
    blob = vm.pack_vec(v)
    assert blob == struct.pack("<5f", *v)
    assert blob == array("f", v).tobytes()                 # Hypatia's BLOBs (little-endian machines)
    assert len(blob) == 20
    back = vm.unpack_vec(blob)
    assert back == [struct.unpack("<f", struct.pack("<f", x))[0] for x in v]
    assert vm.unpack_vec(b"") == [] and vm.pack_vec([]) == b""


def test_borges_numpy_blobs_are_readable():
    np = pytest.importorskip("numpy")
    m = np.random.default_rng(1).standard_normal((4, 16)).astype(np.float32)
    for row in m:
        blob = row.tobytes()                                # what Borges stores
        assert vm.unpack_vec(blob) == row.tolist()
        assert vm.pack_vec(row) == blob
        assert vm.pack_vec(row.tolist()) == blob


def test_unpack_checks_length():
    with pytest.raises(ValueError):
        vm.unpack_vec(b"\x00\x00\x00")
    with pytest.raises(ValueError):
        vm.unpack_vec(vm.pack_vec([1.0, 2.0]), dim=3)
    assert vm.unpack_vec(vm.pack_vec([1.0, 2.0]), dim=2) == [1.0, 2.0]


def test_normalize_and_cosine():
    assert vm.normalize([3, 4]) == [0.6, 0.8]
    assert vm.normalize([0, 0, 0]) == [0, 0, 0]
    assert math.isclose(math.sqrt(sum(x * x for x in vm.normalize([1, 2, 3, 4]))), 1.0)
    assert vm.cosine([1, 0], [0, 1]) == 0.0
    assert math.isclose(vm.cosine([1, 2, 3], [2, 4, 6]), 1.0)
    assert math.isclose(vm.cosine([1, 2], [-1, -2]), -1.0)
    assert vm.cosine([0, 0], [1, 1]) == 0.0 and vm.cosine([1, 2], [1, 2, 3]) == 0.0 and vm.cosine([], []) == 0.0
    assert vm.dot([1, 2, 3], [4, 5, 6]) == 32


def _data(n=300, d=24, seed=3):
    rng = random.Random(seed)
    m = [[rng.gauss(0, 1) for _ in range(d)] for _ in range(n)]
    q = [rng.gauss(0, 1) for _ in range(d)]
    return m, q


def _reference(m, q, k, min_score=0.0):
    scored = sorted(((vm.cosine(r, q), i) for i, r in enumerate(m)), key=lambda t: (-t[0], t[1]))
    return [(i, s) for s, i in scored if s >= min_score][:k]


@pytest.mark.parametrize("numpy_on", [True, False])
def test_topk_matches_brute_force(numpy_on, monkeypatch):
    if numpy_on:
        pytest.importorskip("numpy")
    else:
        monkeypatch.setitem(sys.modules, "numpy", None)        # makes `import numpy` raise ImportError
    m, q = _data()
    for k, ms in [(5, 0.0), (1, 0.0), (50, 0.1), (500, -1.0), (10, 0.99)]:
        got = vm.topk(m, q, k, ms)
        ref = _reference(m, q, k, ms)
        assert [i for i, _ in got] == [i for i, _ in ref]
        assert all(math.isclose(a[1], b[1], abs_tol=1e-5) for a, b in zip(got, ref))
    assert vm.topk(m, q, 0) == [] and vm.topk([], q, 3) == []


@pytest.mark.parametrize("numpy_on", [True, False])
def test_topk_normalized_and_zero_rows(numpy_on, monkeypatch):
    if not numpy_on:
        monkeypatch.setitem(sys.modules, "numpy", None)
    else:
        pytest.importorskip("numpy")
    m = [vm.normalize(r) for r in _data(50, 8)[0]]
    q = vm.normalize(_data(50, 8)[1])
    a = vm.topk(m, q, 5, normalized=True)
    b = vm.topk(m, q, 5)
    assert [i for i, _ in a] == [i for i, _ in b]
    assert vm.topk([[0, 0], [1, 0]], [1, 0], 5, -1.0) == [(1, 1.0), (0, 0.0)]


def test_topk_numpy_matrix_and_errors():
    np = pytest.importorskip("numpy")
    m = np.array([[1, 0], [0, 1], [1, 1]], dtype=np.float32)
    assert [i for i, _ in vm.topk(m, [1, 0], 2)] == [0, 2]
    with pytest.raises(ValueError):
        vm.topk(m, [1, 0, 0], 2)


def test_topk_ties_prefer_the_lower_index_without_numpy(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    assert vm.topk([[1, 0], [1, 0], [1, 0]], [1, 0], 2) == [(0, 1.0), (1, 1.0)]


def test_rrf():
    fused = vm.rrf([["a", "b", "c"], ["b", "c", "d"]])
    assert [x for x, _ in fused] == ["b", "c", "a", "d"]
    assert math.isclose(dict(fused)["b"], 1 / 62 + 1 / 61)
    heavy = vm.rrf([["a", "b"], ["b", "a"]], weights=[1.0, 3.0])
    assert [x for x, _ in heavy] == ["b", "a"]
    assert vm.rrf([]) == [] and vm.rrf([[], []]) == []
    assert vm.rrf([[1, 2], [1, 2]], k=0)[0] == (1, 2.0)
    assert [x for x, _ in vm.rrf([["x"], ["y"]])] == ["x", "y"]                  # tie: first seen first


def test_minmax_fuse():
    fused = dict(vm.minmax_fuse([[("a", 10.0), ("b", 5.0), ("c", 0.0)], [("b", 0.9), ("d", 0.1)]]))
    assert math.isclose(fused["a"], 0.5) and math.isclose(fused["b"], 0.25 + 0.5) and fused["c"] == 0.0 and math.isclose(fused["d"], 0.0)
    flat = vm.minmax_fuse([[("a", 1.0), ("b", 1.0)]], weights=[1.0])
    assert dict(flat) == {"a": 1.0, "b": 1.0}
    assert vm.minmax_fuse([]) == [] and vm.minmax_fuse([[], [("a", 1)]]) == [("a", 0.5)]
