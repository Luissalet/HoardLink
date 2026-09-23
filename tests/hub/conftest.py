"""Fixtures: a temporary family of app folders, a fake app server that
answers ``/api/health`` like the real ones, and a hub over both."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_manifest(folder: Path, app_id: str, port: int, *, name: str | None = None, service: str | None = None,
                   launch: dict | None = None, extra: dict | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    key = app_id.upper() + "_DIR"
    manifest = {
        "schema": 1,
        "id": app_id,
        "name": name or f"{app_id.title()}'s Hoard",
        "purpose": f"The {app_id} app.",
        "capabilities": ["one", "two"],
        "placeholders": [key, "APP_URL"],
        "defaults": {"APP_URL": f"http://127.0.0.1:{port}"},
        "app": {
            "url_default": f"http://127.0.0.1:{port}",
            "ui_url": "{APP_URL}",
            "health": {"path": "/api/health", "expect": {"service": service or f"{app_id}-hoard"}},
            "identify": {"service": [service or f"{app_id}-hoard"], "title": [app_id]},
        },
        "mcp": {"transport": "stdio", "command": "python", "args": ["x.py"], "env": {}, "optional_env": []},
    }
    if launch is not None:
        manifest["app"]["launch_hint"] = launch
    if extra:
        manifest.update(extra)
    (folder / "faustus-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "app-icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return folder


class _FakeApp(BaseHTTPRequestHandler):
    service = "fake-hoard"

    def log_message(self, *a):  # noqa: D102
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"ok": True, "service": self.service}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_app():
    """Yields ``(port, server)`` of a tiny server answering as ``fake-hoard``."""
    port = free_port()
    server = HTTPServer(("127.0.0.1", port), _FakeApp)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, server
    finally:
        server.shutdown()


@pytest.fixture
def family(tmp_path: Path, fake_app):
    port, _ = fake_app
    root = tmp_path / "apps"
    dead_port = free_port()
    script = tmp_path / "serve.py"
    script.write_text(
        "import sys, json\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self,*a): pass\n"
        "    def do_GET(self):\n"
        "        b=json.dumps({'service':'launch-hoard'}).encode(); self.send_response(200)\n"
        "        self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))\n"
        "        self.end_headers(); self.wfile.write(b)\n"
        "HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n",
        encoding="utf-8",
    )
    write_manifest(root / "Fake's Hoard", "fake", port, service="fake-hoard")
    write_manifest(root / "Dead Hoard", "dead", dead_port, service="dead-hoard", launch={
        "kind": "process", "executable": "{MISSING_EXE}", "argv": [], "cwd": "{DEAD_DIR}",
    })
    launch_port = free_port()
    write_manifest(root / "Launch Hoard", "launch", launch_port, service="launch-hoard", launch={
        "kind": "process", "executable": sys.executable, "argv": [str(script), str(launch_port)], "cwd": "{LAUNCH_DIR}",
        "readiness": {"url": "{APP_URL}/api/health", "timeout_s": 15},
    })
    (root / "not-an-app").mkdir()
    (root / "broken").mkdir()
    (root / "broken" / "faustus-plugin.json").write_text("{ not json", encoding="utf-8")
    return {"root": root, "fake_port": port, "dead_port": dead_port, "launch_port": launch_port}


@pytest.fixture
def hub(family, tmp_path: Path):
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(family["root"])], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"])
    h = Hub(cfg)
    yield h
    for app in h.apps:
        if app.launchable:
            h.stop(app.id)
