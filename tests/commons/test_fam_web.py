"""The app-side web client (``hoard_link.fam_web`` and its Node twin ``js/hoard-commons/fam-web.js``): payloads, the error
shapes, the 30 s availability cache, the hub-first/local-second fallback, and a round trip through a real hub. The "hub" is a
local fake that records requests; the "web" is a local ``http.server`` (nothing leaves this machine)."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hoard_link import fam_web, family
from hoard_link.web import safety
from hoard_link.web.fetch import Fetcher
from tests.hub.conftest import free_port, write_manifest
from tests.hub.test_web_facet import ARTICLE, FEED, Site

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "app-token-xyz"


class FakeHub:
    """Serves /api/web/* the way the hub's facet answers; records every request."""

    def __init__(self, *, old_hub: bool = False, enabled: bool = True):
        self.requests: list[dict] = []
        self.old_hub = old_hub
        self.enabled = enabled
        self.fetch_answer = None          # a callable(body) -> (status, payload) overriding the default
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
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self._send(401, {"ok": False, "error": "a family bearer token is required"})
                if self.path == "/api/web/status":
                    return self._send(200, {"ok": True, "enabled": outer.enabled})
                if self.path == "/api/web/hosts":
                    return self._send(200, {"ok": True, "hosts": [{"host": "x.test", "blocked_now": False}], "blocked": 0})
                self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"method": "POST", "path": self.path, "auth": self.headers.get("Authorization"), "json": body})
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self._send(401, {"ok": False, "error": "a family bearer token is required"})
                if not body.get("url") and not body.get("query"):
                    return self._send(400, {"ok": False, "error": "url is required"})
                if self.path == "/api/web/fetch":
                    if outer.fetch_answer:
                        return self._send(*outer.fetch_answer(body))
                    url = body["url"]
                    if url.endswith("/missing"):
                        return self._send(200, {"ok": False, "url": url, "status": 404, "tier": "http", "error": "HTTP 404", "error_kind": "http"})
                    if url.endswith("/pic.png"):
                        return self._send(200, {"ok": True, "url": url, "status": 200, "tier": "http", "text": "",
                                                "body_b64": base64.b64encode(b"\x89PNGdata").decode(), "body_size": 8})
                    return self._send(200, {"ok": True, "url": url, "status": 200, "tier": "http", "text": "<p>hello</p>", "from_cache": False,
                                            "extract": {"kind": body.get("extract"), "text": "hello"} if body.get("extract") else None})
                if self.path == "/api/web/fetch_file":
                    return self._send(200, {"ok": True, "path": "/d/f.pdf", "sha256": "ab", "content_type": "application/pdf", "size": 3})
                if self.path == "/api/web/search":
                    if body.get("query") == "boom":
                        return self._send(200, {"ok": False, "query": "boom", "hits": [], "errors": {"ddg": "blocked"}, "error": "ddg: blocked"})
                    return self._send(200, {"ok": True, "query": body["query"], "hits": [{"url": "https://a.test/", "title": "A"}], "errors": {}})
                if self.path == "/api/web/preview":
                    return self._send(200, {"ok": True, "title": "T", "favicons": []})
                if self.path == "/api/web/open":
                    return self._send(200, {"ok": True, "started": True, "via": "default_browser"})
                self._send(404, {"ok": False, "error": "not found"})

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def gets(self):
        return [r for r in self.requests if r["method"] == "GET"]

    @property
    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


@pytest.fixture(autouse=True)
def _isolated_family_state():
    saved = dict(family._state)
    fam_web._reset_cache()
    fam_web._local["fetcher"] = None
    yield
    family._state.clear()
    family._state.update(saved)
    fam_web._reset_cache()
    fam_web._local["fetcher"] = None


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


@pytest.fixture
def site():
    s = Site()
    s.html("/article", ARTICLE)
    s.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"}, FEED)
    s.routes["/pic.png"] = (200, {"Content-Type": "image/png"}, b"\x89PNG\r\n\x1a\nzz")
    yield s
    s.close()


def local_site_fetcher() -> Fetcher:
    """A local fetcher that may reach 127.0.0.1 (what the app would have used without the hub)."""
    return Fetcher(profile=safety.OPERATOR_LOCAL, pin=True, min_interval_s=0, retries=0)


# ---- availability -------------------------------------------------------------------------------------------------------------

def test_available_is_cached_for_thirty_seconds(fake_hub, monkeypatch):
    assert fam_web.available() is True and fam_web.available() is True and fam_web.available(timeout=0.1) is True
    assert len(fake_hub.gets) == 1 and fake_hub.gets[0]["path"] == "/api/web/status" and fake_hub.gets[0]["auth"] == "Bearer " + TOKEN
    now = time.monotonic()
    monkeypatch.setattr(fam_web.time, "monotonic", lambda: now + 29)
    assert fam_web.available() is True and len(fake_hub.gets) == 1
    monkeypatch.setattr(fam_web.time, "monotonic", lambda: now + 31)
    assert fam_web.available() is True and len(fake_hub.gets) == 2


def test_available_is_false_without_the_service_or_the_token_or_the_hub(tmp_path):
    for hub in (FakeHub(old_hub=True), FakeHub(enabled=False)):
        try:
            _configure(tmp_path, hub.url)
            assert fam_web.available() is False
        finally:
            hub.close()
        fam_web._reset_cache()
    ok = FakeHub()
    try:
        _configure(tmp_path, ok.url, token="wrong")
        assert fam_web.available() is False                     # the hub refuses this app's token
    finally:
        ok.close()
    fam_web._reset_cache()
    _configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    t0 = time.monotonic()
    assert fam_web.available(timeout=0.5) is False and time.monotonic() - t0 < 5


# ---- the calls ----------------------------------------------------------------------------------------------------------------

def test_fetch_sends_the_options_it_was_given_and_nothing_else(fake_hub):
    res = fam_web.fetch("https://a.test/p")
    assert res["ok"] and res["status"] == 200 and res["text"] == "<p>hello</p>"
    req = fake_hub.posts[0]
    assert req["path"] == "/api/web/fetch" and req["auth"] == "Bearer " + TOKEN
    assert req["json"] == {"url": "https://a.test/p", "tier": "auto", "accept": "html", "respect_robots": True, "timeout": 30}
    fam_web.fetch("https://a.test/p", tier="http", accept="json", etag='"v1"', last_modified="Tue, 01 Oct 2026 10:00:00 GMT",
                  respect_robots=False, max_bytes=1000, timeout=5, extract="readable", cache_ttl_s=60, fresh=True)
    assert fake_hub.posts[1]["json"] == {"url": "https://a.test/p", "tier": "http", "accept": "json", "respect_robots": False, "timeout": 5,
                                         "etag": '"v1"', "last_modified": "Tue, 01 Oct 2026 10:00:00 GMT", "max_bytes": 1000,
                                         "extract": "readable", "cache_ttl_s": 60.0, "fresh": True}
    assert fam_web.fetch("https://a.test/p", extract="meta")["extract"]["kind"] == "meta"


def test_fetch_decodes_a_binary_body_and_passes_failures_through(fake_hub):
    res = fam_web.fetch("https://a.test/pic.png", accept="any")
    assert res["body"] == b"\x89PNGdata" and "body_b64" not in res and res["body_size"] == 8
    res = fam_web.fetch("https://a.test/missing")
    assert res["ok"] is False and res["status"] == 404 and res["error_kind"] == "http"            # the upstream status stays
    res = fam_web.fetch("")
    assert res["ok"] is False and res["status"] == 400 and res["error"] == "url is required"      # a hub-level refusal says its status


def test_other_calls_and_their_payloads(fake_hub):
    assert fam_web.fetch_file("https://a.test/f.pdf")["path"] == "/d/f.pdf"
    assert fake_hub.posts[-1]["json"] == {"url": "https://a.test/f.pdf", "max_bytes": 50_000_000, "timeout": 120}
    fam_web.fetch_file("https://a.test/f.pdf", dest_dir="/tmp/out", max_bytes=10)
    assert fake_hub.posts[-1]["json"]["dest_dir"] == "/tmp/out" and fake_hub.posts[-1]["json"]["max_bytes"] == 10
    res = fam_web.search("tea", limit=3, freshness_days=7, engines=["ddg"], news=True)
    assert res["hits"][0]["title"] == "A"
    assert fake_hub.posts[-1]["json"] == {"query": "tea", "limit": 3, "news": True, "freshness_days": 7, "engines": ["ddg"]}
    fam_web.search("tea")
    assert fake_hub.posts[-1]["json"] == {"query": "tea", "limit": 10, "news": False}
    failed = fam_web.search("boom")
    assert failed["ok"] is False and failed["errors"] == {"ddg": "blocked"} and "status" not in failed
    assert fam_web.preview("https://a.test/x")["title"] == "T" and fake_hub.posts[-1]["path"] == "/api/web/preview"
    assert fam_web.host_status()["hosts"][0]["host"] == "x.test" and fake_hub.gets[-1]["path"] == "/api/web/hosts"
    assert fam_web.open_for_human("https://a.test/solve") == {"ok": True, "started": True, "via": "default_browser"}


def test_nothing_raises_when_the_hub_is_unreachable_or_refuses(tmp_path):
    _configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    gone = {"ok": False, "error": "hub unreachable"}
    assert fam_web.fetch("https://a.test/", timeout=0.5) == gone
    assert fam_web.fetch_file("https://a.test/", timeout=0.5) == gone
    assert fam_web.search("x", timeout=0.5) == gone
    assert fam_web.preview("https://a.test/", timeout=0.5) == gone
    assert fam_web.host_status() == gone and fam_web.open_for_human("https://a.test/") == gone
    hub = FakeHub()
    try:
        _configure(tmp_path, hub.url, token="wrong")
        res = fam_web.fetch("https://a.test/")
        assert res["ok"] is False and res["status"] == 401 and "mcp-token" in res["error"] and "wrong" not in json.dumps(res)
        for body in (b"<html>oops</html>", b""):
            hub.fetch_answer = lambda b, raw=body: (500, None)
            _configure(tmp_path, hub.url)
            res = fam_web.fetch("https://a.test/")
            assert res["ok"] is False and res["status"] == 500
    finally:
        hub.close()


def test_a_failed_call_forgets_the_availability(fake_hub):
    assert fam_web.available() is True
    fake_hub.close()
    assert fam_web.fetch("https://a.test/", timeout=0.5)["error"] == "hub unreachable"
    assert family._hub() not in fam_web._cache


# ---- hub first, local second ------------------------------------------------------------------------------------------------------

def test_fetch_or_local_uses_the_hub_when_it_answers(fake_hub, site):
    res = fam_web.fetch_or_local("https://a.test/p", extract="readable")
    assert res["via"] == "hub" and res["ok"] and res["extract"]["kind"] == "readable" and len(fake_hub.posts) == 1
    missing = fam_web.fetch_or_local("https://a.test/missing")
    assert missing["via"] == "hub" and missing["status"] == 404 and len(fake_hub.posts) == 2        # a page that failed is not retried locally
    assert site.hits == []


def test_fetch_or_local_falls_back_to_a_local_fetcher_when_there_is_no_hub(tmp_path, site):
    _configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    local = local_site_fetcher()
    res = fam_web.fetch_or_local(site.base + "/article", local_fetcher=local, extract="readable", timeout=5)
    assert res["via"] == "local" and res["ok"] and res["status"] == 200 and "Boil the water" in res["text"]
    assert res["extract"]["title"] == "Tea at home" and "body" not in res
    feed = fam_web.fetch_or_local(site.base + "/feed.xml", local_fetcher=local, extract="feed")
    assert [i["title"] for i in feed["extract"]["items"]] == ["First", "Second"]
    pic = fam_web.fetch_or_local(site.base + "/pic.png", local_fetcher=local, accept="any")
    assert pic["ok"] and pic["body"].startswith(b"\x89PNG") and pic["body_size"] == len(pic["body"])
    notfound = fam_web.fetch_or_local(site.base + "/nothing", local_fetcher=local)
    assert notfound["ok"] is False and notfound["status"] == 404 and notfound["via"] == "local"
    meta = fam_web.fetch_or_local(site.base + "/article", local_fetcher=local, extract="meta")
    assert meta["extract"]["description"] == "How to brew tea." and meta["extract"]["favicons"]


def test_the_default_local_fetcher_is_built_lazily_and_applies_the_public_policy(tmp_path, site):
    _configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    assert fam_web._local["fetcher"] is None
    res = fam_web.fetch_or_local(site.base + "/article")
    assert fam_web._local["fetcher"] is not None and res["via"] == "local"
    assert res["ok"] is False and res["error_kind"] == "policy" and site.hits == []               # loopback is not a public address
    assert fam_web.local_fetcher() is fam_web.local_fetcher()


def test_fetch_or_local_falls_back_when_the_hub_refuses_the_token_or_is_off(tmp_path, fake_hub, site):
    local = local_site_fetcher()
    _configure(tmp_path, fake_hub.url, token="wrong")
    res = fam_web.fetch_or_local(site.base + "/article", local_fetcher=local)
    assert res["via"] == "local" and res["ok"]
    # the hub answered available, then went away (or was switched off) in the middle of the call
    _configure(tmp_path, fake_hub.url)
    fam_web._reset_cache()
    fake_hub.fetch_answer = lambda body: (503, {"ok": False, "error": "the web service is turned off"})
    res = fam_web.fetch_or_local(site.base + "/article", local_fetcher=local)
    assert res["via"] == "local" and res["ok"]
    # an UPSTREAM 503 (a fetch result) is the page's answer, not the hub's
    fam_web._reset_cache()
    fake_hub.fetch_answer = lambda body: (200, {"ok": False, "url": body["url"], "status": 503, "tier": "http", "error": "HTTP 503"})
    res = fam_web.fetch_or_local(site.base + "/article", local_fetcher=local)
    assert res["via"] == "hub" and res["status"] == 503


def test_fetch_or_local_never_raises(tmp_path):
    _configure(tmp_path, f"http://127.0.0.1:{free_port()}")

    class Broken:
        def get(self, *a, **kw):
            raise RuntimeError("no httpx here")

    res = fam_web.fetch_or_local("https://a.test/", local_fetcher=Broken())
    assert res["ok"] is False and res["via"] == "local" and "RuntimeError" in res["error"]


def test_extract_payload_shapes_and_cap():
    assert fam_web.extract_payload("readable", ARTICLE, "http://x.test/a")["title"] == "Tea at home"
    r = fam_web.extract_payload("readable", ARTICLE, "http://x.test/a", cap=50)
    assert r["text_truncated"] is True and len(r["text"].encode("utf-8")) <= 50
    assert fam_web.extract_payload("markdown", ARTICLE)["headings"][0]["text"] == "Tea at home"
    assert fam_web.extract_payload("jsonld", ARTICLE)["blocks"][0]["headline"] == "Tea at home"
    assert fam_web.extract_payload("feed", FEED)["items"][0]["title"] == "First"
    assert "not an RSS" in fam_web.extract_payload("feed", ARTICLE)["error"]
    assert fam_web.extract_payload("nope", ARTICLE)["error"] == "unknown extract"


# ---- a full round trip through a real hub ------------------------------------------------------------------------------------------

@pytest.fixture
def real_hub(tmp_path):
    from hoard_link.hub.config import HubConfig
    from hoard_link.hub.core import Hub
    from hoard_link.hub.server import make_server

    root = tmp_path / "apps"
    folder = write_manifest(root / "Fam's Hoard", "fam", free_port(), service="fam-hoard")
    (folder / "data").mkdir()
    (folder / "data" / "mcp-token").write_text("fam-app-token", encoding="utf-8")
    hub = Hub(HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(root)], icon_dirs=[],
                        faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False,
                        web={"default_min_interval_s": 0, "browser": "off"}))
    web = hub.facet("web")
    web.app_profile = safety.OPERATOR_LOCAL                 # 127.0.0.1 stands in for a public host
    web.fetcher_options = {"pin": True, "retries": 0}
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, str(folder / "data" / "mcp-token")
    server.shutdown()
    hub.close()


def test_round_trip_through_the_real_hub(real_hub, site):
    hub, token_file = real_hub
    family.configure("fam", token_file=token_file, hub=hub.config.url)
    assert fam_web.available() is True
    res = fam_web.fetch(site.base + "/article", extract="readable")
    assert res["ok"] and res["extract"]["title"] == "Tea at home" and res["tier"] == "http"
    again = fam_web.fetch(site.base + "/article")
    assert again["from_cache"] is True and site.count("/article") == 1
    assert fam_web.fetch(site.base + "/article", fresh=True)["from_cache"] is False
    pic = fam_web.fetch(site.base + "/pic.png", accept="any")
    assert pic["ok"] and pic["body"].startswith(b"\x89PNG")
    missing = fam_web.fetch(site.base + "/nothing")
    assert missing["ok"] is False and missing["status"] == 404
    prev = fam_web.preview(site.base + "/article")
    assert prev["ok"] and prev["title"] == "Tea at home"
    hosts = fam_web.host_status()
    assert hosts["ok"] and hosts["hosts"][0]["host"] == "127.0.0.1" and hosts["hosts"][0]["last_caller"] == "fam"
    got = fam_web.fetch_file(site.base + "/pic.png")
    assert got["ok"] and Path(got["path"]).parent.name == "fam" and Path(got["path"]).read_bytes().startswith(b"\x89PNG")
    via = fam_web.fetch_or_local(site.base + "/article")
    assert via["via"] == "hub" and via["ok"]
    assert fam_web.fetch("")["status"] == 400
    other = fam_web.fetch(site.base + "/article", respect_robots=False)               # a token never names another caller
    assert other["ok"]
    hub.facet("web").db.hosts.update("127.0.0.1", blocked_until_ts=time.time() + 300, block_reason="http_429")
    blocked = fam_web.fetch(site.base + "/other", fresh=True)
    assert blocked["ok"] is False and blocked["error_kind"] == "blocked" and blocked["blocked_until_ts"] > time.time()


# ---- the Node twin -------------------------------------------------------------------------------------------------------------------------

NODE_SCRIPT = r"""
import * as family from %(link)s;
import * as w from %(fam)s;
import { webGet } from %(web)s;
const cfg = JSON.parse(process.argv[2]);
family.configure({ app: "fake", tokenFile: cfg.tokenFile, hub: cfg.hub });
const out = {};
out.available = await w.webAvailable();
out.fetch = await w.webFetch("https://a.test/p", { extract: "readable", cacheTtlS: 60, fresh: true, timeoutMs: 5000 });
out.pic = await w.webFetch("https://a.test/pic.png", { accept: "any" });
out.missing = await w.webFetch("https://a.test/missing");
out.empty = await w.webFetch("");
out.file = await w.webFetchFile("https://a.test/f.pdf", { destDir: "/tmp/out" });
out.search = await w.webSearch("tea", { limit: 3, freshnessDays: 7, engines: ["ddg"], news: true });
out.preview = await w.webPreview("https://a.test/x");
out.hosts = await w.webHostStatus();
out.open = await w.webOpen("https://a.test/solve");
out.viaHub = await w.webFetchOrLocal("https://a.test/p", { extract: "readable" });
out.pic = { ...out.pic, body: out.pic.body ? Array.from(out.pic.body) : null };
if (cfg.site) {
  const localGet = (u, o) => webGet(u, { ...o, profile: "operator_local" });
  w.webForgetAvailability();
  family.configure({ app: "fake", tokenFile: cfg.tokenFile, hub: "http://127.0.0.1:1" });
  out.gone = await w.webFetch(cfg.site + "/article", { timeoutMs: 1000 });
  out.avail2 = await w.webAvailable(300);
  out.local = await w.webFetchOrLocal(cfg.site + "/article", { localGet, extract: "readable", timeoutMs: 5000 });
  out.localMeta = await w.webFetchOrLocal(cfg.site + "/article", { localGet, extract: "meta" });
  out.localFeed = await w.webFetchOrLocal(cfg.site + "/feed.xml", { localGet, extract: "feed" });
  out.localMd = await w.webFetchOrLocal(cfg.site + "/article", { localGet, extract: "markdown" });
  out.localPic = await w.webFetchOrLocal(cfg.site + "/pic.png", { localGet, accept: "any" });
  out.localPicBody = out.localPic.body ? Array.from(out.localPic.body).slice(0, 4) : null;
  delete out.localPic.body;
  out.localMissing = await w.webFetchOrLocal(cfg.site + "/nothing", { localGet });
  out.localBrowser = await w.webFetchOrLocal(cfg.site + "/article", { localGet, tier: "browser" });
  out.localBroken = await w.webFetchOrLocal(cfg.site + "/article", { localGet: async () => { throw new Error("kaput"); } });
}
process.stdout.write(JSON.stringify(out));
"""


def _run_node(tmp_path, cfg: dict) -> dict:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed")
    script = tmp_path / "fam_web_run.mjs"
    uri = lambda p: json.dumps((ROOT / "js" / p).resolve().as_uri())  # noqa: E731
    script.write_text(NODE_SCRIPT % {"link": uri("hoard-link.js"), "fam": uri("hoard-commons/fam-web.js"),
                                     "web": uri("hoard-commons/web.js")}, encoding="utf-8")
    proc = subprocess.run([exe, str(script), json.dumps(cfg)], capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def test_node_twin_against_a_fake_hub(tmp_path, fake_hub, site):
    out = _run_node(tmp_path, {"tokenFile": str(tmp_path / "mcp-token"), "hub": fake_hub.url, "site": site.base})
    assert out["available"] is True
    assert out["fetch"]["ok"] and out["fetch"]["extract"]["kind"] == "readable"
    first = next(r["json"] for r in fake_hub.posts if r["path"] == "/api/web/fetch")
    assert first == {"url": "https://a.test/p", "tier": "auto", "accept": "html", "respect_robots": True,
                                                        "timeout": 5, "extract": "readable", "cache_ttl_s": 60, "fresh": True}
    assert all(r["auth"] == "Bearer " + TOKEN for r in fake_hub.requests)
    assert out["pic"]["body"][:4] == [0x89, 0x50, 0x4E, 0x47] and "body_b64" not in out["pic"]
    assert out["missing"]["ok"] is False and out["missing"]["status"] == 404
    assert out["empty"]["ok"] is False and out["empty"]["status"] == 400
    assert out["file"]["path"] == "/d/f.pdf"
    file_req = next(r for r in fake_hub.posts if r["path"] == "/api/web/fetch_file")["json"]
    assert file_req == {"url": "https://a.test/f.pdf", "max_bytes": 50_000_000, "timeout": 120, "dest_dir": "/tmp/out"}
    search_req = next(r for r in fake_hub.posts if r["path"] == "/api/web/search")["json"]
    assert search_req == {"query": "tea", "limit": 3, "news": True, "freshness_days": 7, "engines": ["ddg"]}
    assert out["search"]["hits"][0]["title"] == "A" and out["preview"]["title"] == "T" and out["hosts"]["hosts"][0]["host"] == "x.test"
    assert out["open"]["started"] is True and out["viaHub"]["via"] == "hub"
    # no hub: the answers keep their shape and the local fetcher serves the page
    assert out["gone"] == {"ok": False, "error": "hub unreachable"} and out["avail2"] is False
    loc = out["local"]
    assert loc["via"] == "local" and loc["ok"] and loc["status"] == 200 and "Boil the water" in loc["text"] and loc["final_url"].endswith("/article")
    assert loc["extract"]["title"] == "Tea at home" and loc["content_type"].startswith("text/html")
    assert out["localMeta"]["extract"]["description"] == "How to brew tea." and out["localMeta"]["extract"]["site_name"] is not None
    assert [i["title"] for i in out["localFeed"]["extract"]["items"]] == ["First", "Second"]
    assert "needs the hub" in out["localMd"]["extract"]["error"]
    assert out["localPic"]["ok"] and out["localPicBody"] == [0x89, 0x50, 0x4E, 0x47] and out["localPic"]["body_size"] == 10
    assert out["localMissing"]["ok"] is False and out["localMissing"]["status"] == 404 and out["localMissing"]["via"] == "local"
    assert out["localBrowser"]["error_kind"] == "unavailable" and out["localBrowser"]["via"] == "local"
    assert out["localBroken"]["ok"] is False and "kaput" in out["localBroken"]["error"] and out["localBroken"]["via"] == "local"
    assert site.count("/article") >= 3


def test_node_twin_against_a_real_hub(tmp_path, real_hub, site):
    hub, token_file = real_hub
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = tmp_path / "real.mjs"
    uri = lambda p: json.dumps((ROOT / "js" / p).resolve().as_uri())  # noqa: E731
    script.write_text(
        f"import * as family from {uri('hoard-link.js')};\nimport * as w from {uri('hoard-commons/fam-web.js')};\n"
        f"const c = JSON.parse(process.argv[2]);\nfamily.configure({{ app: 'fam', tokenFile: c.t, hub: c.h }});\n"
        "const r = await w.webFetch(c.s + '/article', { extract: 'readable' });\n"
        "const f = await w.webFetchFile(c.s + '/pic.png');\n"
        "const h = await w.webHostStatus();\n"
        "const bad = await w.webFetch('');\n"
        "process.stdout.write(JSON.stringify({ avail: await w.webAvailable(), title: r.extract.title, tier: r.tier, file: f.path, host: h.hosts[0], bad: bad.status }));\n",
        encoding="utf-8")
    proc = subprocess.run([shutil.which("node"), str(script), json.dumps({"t": token_file, "h": hub.config.url, "s": site.base})],
                          capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout)
    assert got["avail"] is True and got["bad"] == 400
    assert got["title"] == "Tea at home" and got["tier"] == "http" and Path(got["file"]).parent.name == "fam"
    assert got["host"]["host"] == "127.0.0.1" and got["host"]["last_caller"] == "fam"
