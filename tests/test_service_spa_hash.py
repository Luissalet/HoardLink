"""install_spa: which build files count as hashed (immutable) — Vite's 8-character base64url hash may hold '-' or '_'."""
from pathlib import Path

import pytest

from hoard_link.service import _is_hashed


@pytest.mark.parametrize("name", [
    "index-BsCUjlvX.js",       # plain alphanumeric hash
    "index-DAV-ezrw.js",       # hash with a hyphen inside
    "index-Ab_cD3eF.css",      # hash with an underscore
    "vendor-react--aB3dE4f.js",  # hash starting with a hyphen, name with a hyphen
    "chunk.0123abcd.js",       # dot-separated hash
])
def test_build_files_with_a_hash_are_hashed(name):
    assert _is_hashed(Path("assets") / name)


@pytest.mark.parametrize("name", [
    "index.html",
    "favicon.ico",
    "logo.svg",
    "sw.js",
    "manifest.webmanifest",
    "app-icon.png",
])
def test_plain_files_are_not_hashed(name):
    assert not _is_hashed(Path(name))
