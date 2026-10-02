"""The app-side notification client (hoard_link.fam_notify and its Node twin in js/hoard-link.js):
payload, error shapes, the 30 s availability cache, and a full round trip through a real hub."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hoard_link import family, fam_notify
from tests.hub.conftest import free_port, write_manifest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "app-token-xyz"


class FakeHub:
    """Serves GET/POST /api/notify like the hub's facet does; records every request."""

    def __init__(self, *, old_hub: bool = False):
        self.requests: list[dict] = []
        self.old_hub = old_hub
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload, raw=None):
                body = raw if raw is not None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.requests.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
                if outer.old_hub:
                    return self._send(404, {"ok": False, "error": "not found"})
                self._send(200, {"ok": True, "notifications": [], "unseen": 0})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"method": "POST", "path": self.path, "auth": self.headers.get("Authorization"), "json": body})
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self._send(401, {"ok": False, "error": "a family bearer token is required"})
                if body.get("title") == "boom":
                    return self._send(500, None, raw=b"<html>oops</html>")
                if body.get("title") == "plain":
                    return self._send(200, None, raw=b"")
                if not body.get("title"):
                    return self._send(400, {"ok": False, "error": "title is required"})
                self._send(200, {"ok": True, "id": 7, "app": "fake", "held": None, "channels": [], "delivered": ["windows"]})

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()

    @property
    def gets(self):
        return [r for r in self.requests if r["method"] == "GET"]

    @property
    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


@pytest.fixture(autouse=True)
def _isolated_family_state():
    saved = dict(family._state)
    fam_notify._reset_cache()
    yield
    family._state.clear()
    family._state.update(saved)
    fam_notify._reset_cache()


def _configure(tmp_path, hub_url, token=TOKEN):
    tf = tmp_path / "mcp-token"
    tf.write_text(token, encoding="utf-8")
    family.configure("fake", token_file=str(tf), hub=hub_url)


@pytest.fixture
def fake_hub(tmp_path):
    h = FakeHub()
    _configure(tmp_path, h.url)
    yield h
    h.close()


# ---- Python client ---------------------------------------------------------------------------------------------

def test_notify_posts_title_body_and_only_the_given_options(fake_hub):
    res = fam_notify.notify("Payment failed", "Netflix 12.99", priority="high", url="http://x.test/p", group="payment",
                            dedupe_key="pay:1", sphere="personal")
    assert res["ok"] is True and res["id"] == 7 and res["delivered"] == ["windows"]
    req = fake_hub.posts[0]
    assert req["path"] == "/api/notify" and req["auth"] == "Bearer " + TOKEN
    assert req["json"] == {"title": "Payment failed", "body": "Netflix 12.99", "priority": "high", "url": "http://x.test/p",
                           "group": "payment", "dedupe_key": "pay:1", "sphere": "personal"}
    fam_notify.notify("Just a title")
    assert fake_hub.posts[1]["json"] == {"title": "Just a title", "body": "", "priority": "normal"}


def test_notify_keeps_the_hubs_error_answers(fake_hub):
    res = fam_notify.notify("")
    assert res["ok"] is False and res["error"] == "title is required" and res["status"] == 400
    res = fam_notify.notify("boom")
    assert res["ok"] is False and res["status"] == 500 and "HTTP 500" in res["error"]
    res = fam_notify.notify("plain")
    assert res["ok"] is True and res["status"] == 200


def test_notify_says_when_the_hub_refuses_the_token(tmp_path, fake_hub):
    _configure(tmp_path, fake_hub.url, token="wrong")
    res = fam_notify.notify("Hi")
    assert res["ok"] is False and res["status"] == 401 and "mcp-token" in res["error"]
    assert "wrong" not in json.dumps(res)


def test_notify_when_the_hub_is_unreachable(tmp_path):
    _configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    t0 = time.monotonic()
    assert fam_notify.notify("Hi", timeout=1.0) == {"ok": False, "error": "hub unreachable"}
    assert time.monotonic() - t0 < 5


def test_hub_available_is_cached_for_thirty_seconds(fake_hub, monkeypatch):
    assert fam_notify.hub_available() is True and fam_notify.hub_available() is True and fam_notify.hub_available(timeout=0.1) is True
    assert len(fake_hub.gets) == 1 and fake_hub.gets[0]["path"] == "/api/notify?limit=1"
    now = time.monotonic()
    monkeypatch.setattr(fam_notify.time, "monotonic", lambda: now + 29)
    assert fam_notify.hub_available() is True and len(fake_hub.gets) == 1
    monkeypatch.setattr(fam_notify.time, "monotonic", lambda: now + 31)
    assert fam_notify.hub_available() is True and len(fake_hub.gets) == 2


def test_hub_available_false_for_an_old_hub_or_no_hub(tmp_path):
    old = FakeHub(old_hub=True)
    try:
        _configure(tmp_path, old.url)
        assert fam_notify.hub_available() is False          # answers, but has no notification facet
    finally:
        old.close()
    fam_notify._reset_cache()
    _configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    t0 = time.monotonic()
    assert fam_notify.hub_available(timeout=0.5) is False
    assert time.monotonic() - t0 < 5


def test_the_cache_is_per_hub_and_a_failed_notify_drops_it(tmp_path, fake_hub):
    assert fam_notify.hub_available() is True
    other = FakeHub(old_hub=True)
    try:
        _configure(tmp_path, other.url)
        assert fam_notify.hub_available() is False           # another hub: its own answer
    finally:
        other.close()
    _configure(tmp_path, fake_hub.url)
    assert fam_notify.hub_available() is True and len(fake_hub.gets) == 1
    fake_hub.close()
    assert fam_notify.notify("gone", timeout=0.5)["error"] == "hub unreachable"
    assert family._hub() not in fam_notify._cache


# ---- a full round trip through a real hub -------------------------------------------------------------------------------

def test_round_trip_through_the_real_hub(tmp_path):
    from hoard_link.hub.config import HubConfig
    from hoard_link.hub.core import Hub
    from hoard_link.hub.server import make_server

    root = tmp_path / "apps"
    folder = write_manifest(root / "Fam's Hoard", "fam", free_port(), service="fam-hoard")
    (folder / "data").mkdir()
    (folder / "data" / "mcp-token").write_text("fam-app-token", encoding="utf-8")
    hub = Hub(HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(root)], icon_dirs=[],
                        faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False))
    notes = hub.facet("notify")
    sent = []
    notes.senders = {"windows": lambda note, cfg: sent.append(note) or {"ok": True}}
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "", "end": "", "days": "daily"}})
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        family.configure("fam", token_file=str(folder / "data" / "mcp-token"), hub=hub.config.url)
        assert fam_notify.hub_available() is True
        res = fam_notify.notify("Backup done", "all good", priority="normal", url="http://127.0.0.1:5200/x", group="job", dedupe_key="b1")
        assert res["ok"] is True and res["app"] == "fam" and res["held"] is None and res["delivered"] == ["windows"]
        assert sent[0]["title"] == "Backup done" and sent[0]["app"] == "fam"
        again = fam_notify.notify("Backup done", dedupe_key="b1")
        assert again["ok"] is True and again["held"] == "duplicate"
        assert fam_notify.notify("")["status"] == 400
        row = notes.history()[-1]
        assert row["app"] == "fam" and row["group"] == "job" and row["dedupe_key"] == "b1"
        family.configure("fam", token_file=str(tmp_path / "no-such-token"), hub=hub.config.url)
        assert fam_notify.notify("No token")["status"] == 401
    finally:
        server.shutdown()
        hub.close()


# ---- the Node twin ------------------------------------------------------------------------------------------------------------

NODE_SCRIPT = r"""
const fam = await import(process.argv[2]);
const out = {};
fam.configure({ app: "fake", tokenFile: process.argv[3], hub: process.argv[4] });
out.available = await fam.hubAvailable();
out.available_again = await fam.hubAvailable();
out.sent = await fam.notify("Payment failed", "Netflix 12.99", { priority: "high", url: "http://x.test/p", group: "payment", dedupeKey: "pay:1", sphere: "personal" });
out.minimal = await fam.notify("Just a title");
out.empty = await fam.notify("");
out.boom = await fam.notify("boom");
fam.configure({ app: "fake", tokenFile: process.argv[3], hub: process.argv[5] });
out.down = await fam.notify("Hi", "", { timeoutMs: 1000 });
out.down_available = await fam.hubAvailable(500);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_twin_matches_the_python_client(tmp_path, fake_hub):
    base = (ROOT / "js" / "hoard-link.js").read_text(encoding="utf-8")
    merged = tmp_path / "merged.mjs"
    merged.write_text(base, encoding="utf-8")
    script = tmp_path / "t.mjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    token_file = tmp_path / "mcp-token"
    token_file.write_text(TOKEN, encoding="utf-8")
    dead = f"http://127.0.0.1:{free_port()}"
    run = subprocess.run(["node", str(script), merged.as_uri(), str(token_file), fake_hub.url, dead], capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["available"] is True and out["available_again"] is True and len(fake_hub.gets) == 1
    assert out["sent"]["ok"] is True and out["sent"]["id"] == 7
    assert fake_hub.posts[0]["json"] == {"title": "Payment failed", "body": "Netflix 12.99", "priority": "high", "url": "http://x.test/p",
                                         "group": "payment", "dedupe_key": "pay:1", "sphere": "personal"}
    assert fake_hub.posts[0]["auth"] == "Bearer " + TOKEN
    assert fake_hub.posts[1]["json"] == {"title": "Just a title", "body": "", "priority": "normal"}
    assert out["empty"]["ok"] is False and out["empty"]["error"] == "title is required" and out["empty"]["status"] == 400
    assert out["boom"]["ok"] is False and out["boom"]["status"] == 500 and "HTTP 500" in out["boom"]["error"]
    assert out["down"] == {"ok": False, "error": "hub unreachable"} and out["down_available"] is False
