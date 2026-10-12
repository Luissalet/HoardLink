"""install_spa: which build files count as hashed (immutable) — Vite's 8-character base64url hash may hold '-' or '_'."""
from pathlib import Path

import pytest

from hoard_link.service import _is_hashed


@pytest.mark.parametrize("name", [
    "index-BsCUjlvX.js",       # plain alphanumeric hash
    "index-DAV-ezrw.js",       # hash with a hyphen inside
    "index-Ab_cD3eF.css",      # hash with an underscore
    "vendor-react--aB3dE4f.js",  # hash starting with a hyphen, name with a hyphen
    "logo--D9Dnd-Ds.svg",     # actual Vite output for the source file logo-.svg
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


def test_vite_trailing_dash_name_is_served_immutable(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hoard_link.service import install_spa

    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'logo--D9Dnd-Ds.svg').write_text('<svg></svg>')
    (assets / 'app-icon.png').write_bytes(b'plain icon')
    (tmp_path / 'index.html').write_text('<html></html>')
    app = FastAPI()
    install_spa(app, tmp_path)
    with TestClient(app) as client:
        assert 'immutable' in client.get('/assets/logo--D9Dnd-Ds.svg').headers['cache-control']
        assert client.get('/assets/app-icon.png').headers['cache-control'] == 'no-cache'
        assert client.get('/').headers['cache-control'] == 'no-cache'
