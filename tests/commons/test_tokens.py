"""hoard_link.tokens: a stable token, atomic creation, bearer checks (shared vectors with the Node twin)."""

from __future__ import annotations

import os
import threading

import pytest

from hoard_link import tokens
from tests.commons.jsrun import load_vectors, normalise, python_call, run_js

CASES = load_vectors("tokens")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{str(c['args'])[:40]}")
def test_check_bearer_vectors_python(case):
    assert normalise(python_call(tokens, case)) == case["expect"]


def test_check_bearer_vectors_node():
    got = run_js("server.js", CASES)
    bad = [(c["args"], g, c["expect"]) for c, g in zip(CASES, got) if g != c["expect"]]
    assert not bad, bad


def test_creates_then_returns_the_same_token(tmp_path):
    p = tmp_path / "data" / "mcp-token"
    first = tokens.read_or_create_token(p)
    assert len(first) >= 32 and p.read_text() == first
    assert tokens.read_or_create_token(p) == first
    assert tokens.read_or_create_token(p) == first                    # "restarts" never rotate it


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_token_file_is_private(tmp_path):
    p = tmp_path / "mcp-token"
    tokens.read_or_create_token(p)
    assert (p.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("content", ["", "short", "   \n", "has whitespace inside the token that is long enough to pass", "x" * 31])
def test_bad_file_is_replaced(tmp_path, content):
    p = tmp_path / "mcp-token"
    p.write_text(content)
    new = tokens.read_or_create_token(p)
    assert len(new) >= 32 and " " not in new and p.read_text() == new


def test_exactly_min_len_is_kept(tmp_path):
    p = tmp_path / "t"
    p.write_text("a" * 32 + "\n")
    assert tokens.read_or_create_token(p) == "a" * 32


def test_bom_and_newline_are_tolerated(tmp_path):
    p = tmp_path / "t"
    p.write_bytes(b"\xef\xbb\xbf" + b"b" * 40 + b"\r\n")
    assert tokens.read_or_create_token(p) == "b" * 40


def test_longer_min_len(tmp_path):
    p = tmp_path / "t"
    assert len(tokens.read_or_create_token(p, min_len=64)) >= 64
    assert len(tokens.new_token()) >= 32


def test_unreadable_path_is_replaced(tmp_path):
    p = tmp_path / "t"
    p.mkdir()                          # a directory where the file should be: unreadable
    with pytest.raises(OSError):       # cannot be replaced either, but it must not crash with a bare traceback of another type
        tokens.read_or_create_token(p)


def test_concurrent_creation_agrees_on_one_token(tmp_path):
    p = tmp_path / "mcp-token"
    out, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        out.append(tokens.read_or_create_token(p))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 1 and p.read_text() == out[0]
    assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []


def test_url_roundtrip(tmp_path):
    p = tmp_path / "data" / "url"
    assert tokens.read_url(p) is None
    tokens.write_url(p, " http://127.0.0.1:5200 \n")
    assert p.read_text() == "http://127.0.0.1:5200"
    assert tokens.read_url(p) == "http://127.0.0.1:5200"
    p.write_text("   ")
    assert tokens.read_url(p) is None
