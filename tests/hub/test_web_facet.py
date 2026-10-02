"""The ``web`` facet (the family's one web service): the shared per-host throttle, robots.txt, the block cooldown
(persistent), the response cache with stale-if-error, extraction, downloads, search through a fake SearXNG, previews,
the browser tier without playwright, ``open_for_human``, the policy for apps, the audit and the hub events, the HTTP
routes through the real server and the agent tools. Nothing leaves this machine: the "web" is a local ``http.server``
(apps only reach PUBLIC addresses, so the tests tell the facet to give apps the ``operator_local`` profile)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hoard_link.hub import tools
from hoard_link.hub import web as web_mod
from hoard_link.hub.config import HubConfig
from hoard_link.hub.facets import Reply, Request
from hoard_link.hub.server import make_server
from hoard_link.hub.web import WebDb, WebFacet, web_settings
from hoard_link.web import safety

from .conftest import free_port
from .test_hub_and_server import _http

UI = {"Sec-Fetch-Site": "same-origin"}

ARTICLE = ("<html><head><title>Tea at home</title><meta property='og:title' content='Tea at home'>"
           "<meta name='description' content='How to brew tea.'><meta property='og:image' content='/img/tea.png'>"
           "<link rel='icon' href='/static/tea.ico'><link rel='alternate' type='application/rss+xml' href='/feed.xml' title='Feed'>"
           "<script type='application/ld+json'>{\"@type\": \"Article\", \"headline\": \"Tea at home\"}</script></head>"
           "<body><main><h1>Tea at home</h1>" + "".join(f"<p>Step {i}: boil the water and let the leaves rest for three minutes before drinking. Boil the water slowly.</p>" for i in range(12)) +
           "</main></body></html>")
FEED = ("<?xml version='1.0'?><rss version='2.0'><channel><title>Tea news</title><link>http://x.test/</link>"
        "<description>d</description><item><title>First</title><link>http://x.test/1</link></item>"
        "<item><title>Second</title><link>http://x.test/2</link></item></channel></rss>")


class Site:
    """A scriptable local web server. ``routes[path] = (status, headers, body)`` or a callable(request path) -> that."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.hits: list[tuple[float, str]] = []
        self.agents: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: D102
                pass

            def do_GET(self):  # noqa: N802
                path = self.path
                outer.hits.append((time.monotonic(), path))
                outer.agents.append(self.headers.get("User-Agent", ""))
                route = outer.routes.get(path.split("?")[0])
                if callable(route):
                    route = route(path)
                if route is None:
                    route = (404, {"Content-Type": "text/plain"}, b"nothing here")
                status, headers, body = route
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def html(self, path: str, body: str, status: int = 200) -> None:
        self.routes[path] = (status, {"Content-Type": "text/html; charset=utf-8"}, body)

    def count(self, prefix: str) -> int:
        return sum(1 for _, p in self.hits if p.startswith(prefix))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    s = Site()
    s.html("/article", ARTICLE)
    s.routes["/feed.xml"] = (200, {"Content-Type": "application/rss+xml"}, FEED)
    yield s
    s.close()


@pytest.fixture
def web(hub):
    """The web facet, apps treated as operator_local (so 127.0.0.1 can stand in for a public host), no interval, no browser."""
    f = hub.facet("web")
    assert isinstance(f, WebFacet)
    hub.config.web = {"default_min_interval_s": 0, "browser": "off"}
    f.app_profile = safety.OPERATOR_LOCAL
    f.fetcher_options = {"pin": True, "retries": 0}
    return f


def call(f: WebFacet, path: str, body=None, *, who="fake", method=None, query=None) -> dict[str, Any]:
    req = Request(method=method or ("POST" if body is not None else "GET"), path=path, query=query or {}, body=body or {},
                  caller=lambda: who, agent=lambda: who == "hub")
    res = (f.post if req.method == "POST" else f.get)(req)
    return res.payload if isinstance(res, Reply) else res


def events(hub, type_):
    return list(reversed(hub.events.query(type=type_)))


# ---- fetching --------------------------------------------------------------------------------------------------------

def test_fetch_returns_the_page_the_status_and_the_audit(hub, web, site):
    res = call(web, "/api/web/fetch", {"url": site.base + "/article"})
    assert res["ok"] and res["status"] == 200 and res["tier"] == "http" and "Boil the water" in res["text"]
    assert res["from_cache"] is False and res["text_truncated"] is False and res["text_len"] == len(res["text"])
    assert "body" not in res and "set-cookie" not in res["headers"]
    # the audit has the caller and the host, never the path or the body
    row = call(web, "/api/web/status")["recent"][0]
    assert row["caller"] == "fake" and row["kind"] == "fetch" and row["host"] == "127.0.0.1" and row["status"] == 200
    assert "article" not in json.dumps(row) and "Boil" not in json.dumps(row)
    ev = events(hub, "web.fetch")[-1]
    assert ev["data"]["caller"] == "fake" and ev["data"]["host"] == "127.0.0.1" and ev["data"]["status"] == 200
    assert "Boil" not in json.dumps(ev) and "article" not in json.dumps(ev)
    hosts = call(web, "/api/web/hosts")["hosts"]
    assert hosts[0]["host"] == "127.0.0.1" and hosts[0]["last_caller"] == "fake" and hosts[0]["last_status"] == 200
    assert site.agents[-1].startswith("Mozilla/5.0")            # one desktop-browser string, no app name


def test_user_agent_setting_and_a_wrong_request(hub, web, site):
    hub.config.web = {**hub.config.web, "user_agent": "FamilyFetcher/1.0"}
    assert call(web, "/api/web/fetch", {"url": site.base + "/article"})["ok"]
    assert site.agents[-1] == "FamilyFetcher/1.0"
    for body in ({}, {"url": "  "}, {"url": site.base, "accept": "xml"}, {"url": site.base, "tier": "teleport"},
                 {"url": site.base, "extract": "pdf"}, {"url": site.base, "accept": "json", "extract": "readable"},
                 {"url": site.base, "timeout": "soon"}, {"url": site.base, "profile": "internal"}):
        res = call(web, "/api/web/fetch", body)
        assert res["ok"] is False and res["status"] == 400, body


def test_apps_reach_public_addresses_only_the_hub_may_widen_it(hub, site):
    f = hub.facet("web")
    hub.config.web = {"default_min_interval_s": 0, "browser": "off"}
    f.fetcher_options = {"pin": True, "retries": 0}
    res = call(f, "/api/web/fetch", {"url": site.base + "/article"})              # default policy: public only
    assert res["ok"] is False and res["error_kind"] == "policy" and site.hits == []
    res = call(f, "/api/web/fetch", {"url": site.base + "/article", "profile": "operator_local"}, who="fake")
    assert res["ok"] is False and res["status"] == 403
    res = call(f, "/api/web/fetch", {"url": site.base + "/article", "profile": "operator_local"}, who="ui")
    assert res["ok"] and "Boil the water" in res["text"]
    assert call(f, "/api/web/fetch", {"url": site.base + "/article", "profile": "operator_local", "tier": "window"}, who="fake")["status"] == 403


def test_throttle_spaces_every_callers_requests_to_one_host(web, hub, site):
    hub.config.web = {"default_min_interval_s": 0.4, "browser": "off"}
    out: dict[str, Any] = {}

    def go(who: str, path: str) -> None:
        out[who] = call(web, "/api/web/fetch", {"url": site.base + path, "respect_robots": False}, who=who)

    site.html("/a", ARTICLE)
    site.html("/b", ARTICLE)
    threads = [threading.Thread(target=go, args=("ledger", "/a")), threading.Thread(target=go, args=("tantalus", "/b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert out["ledger"]["ok"] and out["tantalus"]["ok"]
    times = sorted(t for t, p in site.hits if p in ("/a", "/b"))
    assert len(times) == 2 and times[1] - times[0] >= 0.35, times
    callers = {h["last_caller"] for h in call(web, "/api/web/hosts")["hosts"]}
    assert callers <= {"ledger", "tantalus"}


def test_robots_txt_disallow_is_enforced_for_apps_and_hub_settings(hub, web, site):
    site.routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /private\n")
    site.html("/private/page", ARTICLE)
    res = call(web, "/api/web/fetch", {"url": site.base + "/private/page"})
    assert res["ok"] is False and res["error_kind"] == "robots" and site.count("/private") == 0
    # an app cannot switch it off while the hub setting is on; the hub's own page and agent can
    assert call(web, "/api/web/fetch", {"url": site.base + "/private/page", "respect_robots": False})["error_kind"] == "robots"
    assert call(web, "/api/web/fetch", {"url": site.base + "/private/page", "respect_robots": False}, who="hub")["ok"]
    assert call(web, "/api/web/fetch", {"url": site.base + "/article"})["ok"]
    # the operator can turn it off for the family
    hub.config.web = {**hub.config.web, "respect_robots": False}
    assert call(web, "/api/web/fetch", {"url": site.base + "/private/page", "fresh": True})["ok"]
    assert call(web, "/api/web/fetch", {"url": site.base + "/private/page", "respect_robots": True, "fresh": True})["error_kind"] == "robots"


def test_a_429_with_retry_after_blocks_the_host_for_everyone_and_survives_a_restart(hub, web, site):
    site.routes["/limited"] = (429, {"Content-Type": "text/html", "Retry-After": "120"}, "<html><body>slow down please</body></html>")
    res = call(web, "/api/web/fetch", {"url": site.base + "/limited", "respect_robots": False}, who="ledger")
    assert res["ok"] is False and res["blocked"] and res["block_reason"] == "http_429" and site.count("/limited") == 1
    assert 100 < res["blocked_until_ts"] - time.time() <= 121
    # another app asking for ANY page of the host is turned away without a request
    again = call(web, "/api/web/fetch", {"url": site.base + "/article", "respect_robots": False}, who="tantalus")
    assert again["ok"] is False and again["error_kind"] == "blocked" and "minute" in again["error"] and site.count("/article") == 0
    hosts = call(web, "/api/web/hosts")
    row = hosts["hosts"][0]
    assert hosts["blocked"] == 1 and row["blocked_now"] and row["block_reason"] == "http_429" and row["last_caller"] == "ledger"
    # the state is in web.db: a new process sees the same cooldown
    reopened = WebDb(str(Path(hub.config.data_dir) / "web.db"))
    assert reopened.hosts.get("127.0.0.1")["blocked_until_ts"] > time.time() + 100
    reopened.close()
    # only the hub's page or token lifts it
    assert call(web, "/api/web/hosts/clear", {"host": "127.0.0.1"}, who="ledger")["status"] == 403
    cleared = call(web, "/api/web/hosts/clear", {"host": "127.0.0.1"}, who="ui")
    assert cleared["ok"] and call(web, "/api/web/hosts")["blocked"] == 0
    assert call(web, "/api/web/fetch", {"url": site.base + "/article", "respect_robots": False}, who="tantalus")["ok"]
    assert call(web, "/api/web/hosts/clear", {"host": "nowhere.example"}, who="ui")["status"] == 404
    assert call(web, "/api/web/hosts/clear", {"host": ""}, who="ui")["status"] == 400


def test_cache_hit_fresh_and_stale_if_error(web, hub, site):
    url = site.base + "/article"
    first = call(web, "/api/web/fetch", {"url": url})
    second = call(web, "/api/web/fetch", {"url": url}, who="tantalus")
    assert first["from_cache"] is False and second["from_cache"] is True and second["tier"] == "cache"
    assert site.count("/article") == 1                                    # the second app cost no request
    assert call(web, "/api/web/fetch", {"url": url, "fresh": True})["from_cache"] is False and site.count("/article") == 2
    assert call(web, "/api/web/fetch", {"url": url, "cache_ttl_s": 0})["from_cache"] is False and site.count("/article") == 3
    assert call(web, "/api/web/status")["cache"]["entries"] >= 1
    # an expired entry + a failing server: the old copy is served, marked stale
    short = site.base + "/short"
    site.html("/short", ARTICLE)
    assert call(web, "/api/web/fetch", {"url": short, "cache_ttl_s": 0.3})["ok"]
    time.sleep(0.4)
    site.html("/short", "boom", status=500)
    res = call(web, "/api/web/fetch", {"url": short, "cache_ttl_s": 0.3})
    assert res["ok"] and res["stale"] and res["from_cache"] and "Boil the water" in res["text"] and "served from cache" in res["note"]


def test_conditional_get_not_modified(web, site):
    site.routes["/etag"] = lambda p: (200, {"Content-Type": "text/html", "ETag": '"v1"'}, ARTICLE)
    first = call(web, "/api/web/fetch", {"url": site.base + "/etag", "fresh": True})
    assert first["etag"] == '"v1"'
    site.routes["/etag"] = (304, {"ETag": '"v1"'}, b"")
    again = call(web, "/api/web/fetch", {"url": site.base + "/etag", "etag": '"v1"', "fresh": True})
    assert again["ok"] and again["not_modified"] and again["text"] == ""


def test_extract_readable_markdown_meta_jsonld_and_feed(web, site):
    url = site.base + "/article"
    r = call(web, "/api/web/fetch", {"url": url, "extract": "readable"})["extract"]
    assert r["kind"] == "readable" and r["title"] == "Tea at home" and "Boil the water" in r["text"] and r["quality"] == ""
    m = call(web, "/api/web/fetch", {"url": url, "extract": "markdown"})["extract"]
    assert m["kind"] == "markdown" and "# Tea at home" in m["markdown"] and m["headings"][0]["text"] == "Tea at home"
    meta = call(web, "/api/web/fetch", {"url": url, "extract": "meta"})["extract"]
    assert meta["title"] == "Tea at home" and meta["description"] == "How to brew tea."
    assert meta["image"] == url.rsplit("/", 1)[0] + "/img/tea.png" and meta["feeds"][0]["url"].endswith("/feed.xml")
    assert any(u.endswith("/static/tea.ico") for u in meta["favicons"])
    ld = call(web, "/api/web/fetch", {"url": url, "extract": "jsonld"})["extract"]
    assert ld["blocks"] and ld["blocks"][0]["headline"] == "Tea at home" and ld["errors"] == []
    feed = call(web, "/api/web/fetch", {"url": site.base + "/feed.xml", "extract": "feed"})["extract"]
    assert feed["format"] == "rss" and [i["title"] for i in feed["items"]] == ["First", "Second"]
    notfeed = call(web, "/api/web/fetch", {"url": url, "extract": "feed"})
    assert notfeed["ok"] and "not an RSS" in notfeed["extract"]["error"]


def test_binary_body_comes_back_as_base64_only_when_small(web, site):
    site.routes["/pic.png"] = (200, {"Content-Type": "image/png"}, b"\x89PNG\r\n\x1a\nxxxx")
    res = call(web, "/api/web/fetch", {"url": site.base + "/pic.png", "accept": "any"})
    import base64
    assert res["ok"] and base64.b64decode(res["body_b64"]).startswith(b"\x89PNG") and res["body_size"] == 12 and "body" not in res
    assert call(web, "/api/web/fetch", {"url": site.base + "/pic.png"})["error_kind"] == "content"       # accept=html refuses images
    site.routes["/big.bin"] = (200, {"Content-Type": "application/octet-stream"}, b"z" * 40)
    old = web_mod.MAX_B64_BYTES
    web_mod.MAX_B64_BYTES = 10
    try:
        big = call(web, "/api/web/fetch", {"url": site.base + "/big.bin", "accept": "any"})
    finally:
        web_mod.MAX_B64_BYTES = old
    assert big["ok"] and big.get("body_omitted") and "body_b64" not in big and big["body_size"] == 40


def test_text_is_capped_at_two_megabytes(web, site):
    old = web_mod.MAX_TEXT_BYTES
    web_mod.MAX_TEXT_BYTES = 1000
    try:
        site.html("/long", "<html><body><p>" + "palabra " * 600 + "</p></body></html>")
        res = call(web, "/api/web/fetch", {"url": site.base + "/long"})
        assert res["ok"] and res["text_truncated"] and len(res["text"].encode("utf-8")) <= 1000 and res["text_len"] > 1000
    finally:
        web_mod.MAX_TEXT_BYTES = old
    assert web_mod._cap_text("é" * 10, 7) == ("é" * 3, True) and web_mod._cap_text("abc", 7) == ("abc", False)


def test_too_many_requests_waiting_for_one_host_are_turned_away(web, site, monkeypatch):
    monkeypatch.setattr(web_mod, "MAX_QUEUED_PER_HOST", 0)
    res = call(web, "/api/web/fetch", {"url": site.base + "/article"})
    assert res["ok"] is False and res["error_kind"] == "busy" and site.hits == []


# ---- downloads ---------------------------------------------------------------------------------------------------------

def test_fetch_file_saves_under_the_callers_folder_and_reuses_identical_files(hub, web, site):
    site.routes["/files/report.pdf"] = (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 hello")
    site.routes["/dl"] = (200, {"Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="..\\evil/name?.zip"'}, b"PKzip")
    res = call(web, "/api/web/fetch_file", {"url": site.base + "/files/report.pdf"}, who="kafka")
    folder = Path(hub.config.data_dir) / "web" / "files" / "kafka"
    assert res["ok"] and Path(res["path"]) == folder / "report.pdf" and (folder / "report.pdf").read_bytes() == b"%PDF-1.4 hello"
    import hashlib
    assert res["sha256"] == hashlib.sha256(b"%PDF-1.4 hello").hexdigest() and res["size"] == 14 and res["content_type"] == "application/pdf"
    again = call(web, "/api/web/fetch_file", {"url": site.base + "/files/report.pdf"}, who="kafka")
    assert again["path"] == res["path"]                                  # same bytes: the same file
    site.routes["/files/report.pdf"] = (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 other")
    third = call(web, "/api/web/fetch_file", {"url": site.base + "/files/report.pdf"}, who="kafka")
    assert Path(third["path"]).name == "report (2).pdf"
    named = call(web, "/api/web/fetch_file", {"url": site.base + "/dl"}, who="links")
    assert named["ok"] and Path(named["path"]).name == "name_.zip" and Path(named["path"]).parent.name == "links"
    assert [e["data"]["kind"] for e in events(hub, "web.fetch")][-1] == "file"


def test_fetch_file_dest_dir_limits_and_failures(hub, web, site, tmp_path):
    site.routes["/f.bin"] = (200, {"Content-Type": "application/octet-stream"}, b"0123456789" * 10)
    dest = tmp_path / "downloads"
    res = call(web, "/api/web/fetch_file", {"url": site.base + "/f.bin", "dest_dir": str(dest)})
    assert res["ok"] and Path(res["path"]) == dest / "f.bin" and (dest / "f.bin").stat().st_size == 100
    for bad in ("relative/folder", "/", str(Path(hub.config.data_dir))):
        r = call(web, "/api/web/fetch_file", {"url": site.base + "/f.bin", "dest_dir": bad})
        assert r["ok"] is False and r["status"] == 400 and "dest_dir" in r["error"], bad
    big = call(web, "/api/web/fetch_file", {"url": site.base + "/f.bin", "dest_dir": str(dest / "small"), "max_bytes": 40})
    assert big["ok"] is False and "larger" in big["error"] and not (dest / "small").exists()
    gone = call(web, "/api/web/fetch_file", {"url": site.base + "/nope.bin"})
    assert gone["ok"] is False and gone["status"] == 404
    assert call(web, "/api/web/fetch_file", {})["status"] == 400


# ---- search and previews --------------------------------------------------------------------------------------------------

def test_search_through_a_fake_searxng_with_validation_and_audit(hub, web, site):
    queries = []

    def searx(path: str):
        queries.append(path)
        body = {"results": [{"url": "https://example.org/a", "title": "Alpha", "content": "first hit"},
                            {"url": "https://example.org/b", "title": "Beta", "content": "second"},
                            {"url": "http://127.0.0.1:9/private", "title": "Local", "content": "not for the family"}]}
        return 200, {"Content-Type": "application/json"}, json.dumps(body)

    site.routes["/search"] = searx
    hub.config.web = {**hub.config.web, "searxng_url": site.base, "search_intervals": {"searxng": 0}}
    res = call(web, "/api/web/search", {"query": "  tea   brewing ", "limit": 5, "engines": ["searxng"], "freshness_days": 7}, who="tantalus")
    assert res["ok"] and res["query"] == "tea brewing" and res["engines"] == ["searxng"] and res["errors"] == {}
    assert [h["title"] for h in res["hits"]] == ["Alpha", "Beta"]              # the local address is dropped
    assert res["hits"][0]["url"] == "https://example.org/a" and res["hits"][0]["engine"] == "searxng"
    assert "q=tea+brewing" in queries[0] and "time_range=week" in queries[0] and "format=json" in queries[0]
    status = call(web, "/api/web/status")
    assert status["searxng"] is True and "searxng" in status["engines"] and status["news_engines"] == ["gnews", "bingnews"]
    row = next(r for r in status["recent"] if r["kind"] == "search")
    assert row["caller"] == "tantalus" and row["detail"] == "tea brewing"
    ev = events(hub, "web.search")[-1]["data"]
    assert ev["caller"] == "tantalus" and ev["hits"] == 2 and "tea" not in json.dumps(ev)
    # an engine that fails is reported, the call says so
    site.routes["/search"] = (500, {"Content-Type": "text/plain"}, b"nope")
    res = call(web, "/api/web/search", {"query": "other words", "engines": ["searxng"]})
    assert res["ok"] is False and "searxng" in res["errors"] and "searxng" in res["error"]
    for bad in ({}, {"query": "x", "engines": ["altavista"]}, {"query": "x", "limit": 0}, {"query": "x", "engines": 5}):
        assert call(web, "/api/web/search", bad)["status"] == 400, bad


def test_preview_has_meta_and_favicons_and_is_cached_for_a_week(hub, web, site):
    url = site.base + "/article"
    res = call(web, "/api/web/preview", None, query={"url": [url]}, method="GET")
    assert res["ok"] and res["title"] == "Tea at home" and res["description"] == "How to brew tea."
    assert res["image"].endswith("/img/tea.png") and res["favicon"].endswith("/static/tea.ico") and res["favicons"]
    assert res["from_cache"] is False and site.count("/article") == 1
    again = call(web, "/api/web/preview", {"url": url}, who="links")
    assert again["from_cache"] is True and again["title"] == "Tea at home" and site.count("/article") == 1
    # the same page with tracking noise is the same preview
    assert call(web, "/api/web/preview", {"url": url + "?utm_source=x"})["from_cache"] is True
    assert call(web, "/api/web/preview", {"url": url, "fresh": True})["from_cache"] is False and site.count("/article") == 2
    # older than a week and the page is gone: the old preview is served, marked stale
    key = __import__("hashlib").sha256(web_mod.normalize_url(url).encode()).hexdigest()
    web.db.run("UPDATE previews SET ts = ? WHERE key = ?", (time.time() - 8 * 86400, key), commit=True)
    site.html("/article", "<html><body>gone</body></html>", status=404)
    stale = call(web, "/api/web/preview", {"url": url, "cache_ttl_s": 0})
    assert stale["ok"] and stale["stale"] and stale["title"] == "Tea at home"
    missing = call(web, "/api/web/preview", {"url": site.base + "/missing"})
    assert missing["ok"] is False and missing["status"] == 404
    assert call(web, "/api/web/preview", {"url": "ftp://x.test/a"})["status"] == 400
    assert call(web, "/api/web/preview", {})["status"] == 400


# ---- the browser tier ----------------------------------------------------------------------------------------------------------

def test_browser_tier_without_playwright_says_so_and_http_keeps_working(hub, site, monkeypatch):
    f = hub.facet("web")
    hub.config.web = {"default_min_interval_s": 0}                        # browser: auto
    f.app_profile = safety.OPERATOR_LOCAL
    f.fetcher_options = {"pin": True, "retries": 0}
    monkeypatch.setattr("hoard_link.web.browser.playwright_installed", lambda: False)
    for tier in ("browser",):
        res = call(f, "/api/web/fetch", {"url": site.base + "/article", "tier": tier})
        assert res["ok"] is False and res["error_kind"] == "unavailable"
        assert res["error"] == "browser tier unavailable: playwright is not installed in the hub's Python"
    assert site.hits == []
    assert call(f, "/api/web/fetch", {"url": site.base + "/article", "tier": "auto"})["ok"]
    st = call(f, "/api/web/status")["browser"]
    assert st["available"] is False and "playwright is not installed" in st["reason"] and st["mode"] == "auto"
    assert st["profile_dir"].endswith("browser-profile")
    # the audit does not count a refused tier as traffic to the host
    assert call(f, "/api/web/hosts")["hosts"][0]["last_status"] == 200


def test_browser_mode_off_in_hub_json(hub, web, site):
    res = call(web, "/api/web/fetch", {"url": site.base + "/article", "tier": "browser"})
    assert res["ok"] is False and res["error_kind"] == "unavailable" and "turned off" in res["error"]
    assert call(web, "/api/web/status")["browser"] == {"mode": "off", "available": False, "reason": web_mod.BROWSER_OFF,
                                                        "profile_dir": web.profile_dir, "human_window_open": False}


class FakeRung:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.gate = threading.Event()
        self.done = threading.Event()

    def available(self) -> bool:
        return True

    def open_for_human(self, url: str, **kw: Any) -> dict[str, Any]:
        self.opened.append(url)
        self.gate.wait(10)
        self.done.set()
        return {"url": url, "closed_by_user": True}

    def close(self) -> None:
        pass


def test_open_for_human_uses_the_family_profile_and_clears_the_block(hub, site):
    f = hub.facet("web")
    rung = FakeRung()
    hub.config.web = {"default_min_interval_s": 0}
    f.app_profile = safety.OPERATOR_LOCAL
    f.fetcher_options = {"pin": True, "retries": 0, "browser": rung}
    f.db.hosts.update("127.0.0.1", blocked_until_ts=time.time() + 600, block_reason="cloudflare")
    res = call(f, "/api/web/open", {"url": site.base + "/challenge"}, who="tantalus")
    assert res["ok"] and res["started"] and res["via"] == "family_profile"
    deadline = time.time() + 5
    while not rung.opened and time.time() < deadline:
        time.sleep(0.01)
    assert rung.opened == [site.base + "/challenge"]
    assert call(f, "/api/web/status")["browser"]["human_window_open"] is True
    assert call(f, "/api/web/open", {"url": site.base + "/other"})["status"] == 409      # one window at a time
    rung.gate.set()
    assert rung.done.wait(5)
    deadline = time.time() + 5
    while f._human.is_set() and time.time() < deadline:
        time.sleep(0.01)
    assert f.db.hosts.get("127.0.0.1")["blocked_until_ts"] == 0.0                     # solved: the cooldown is gone
    done = events(hub, "web.open.done")[-1]["data"]
    assert done["closed_by_user"] is True and done["caller"] == "tantalus"


def test_open_for_human_without_playwright_opens_the_default_browser(hub, site, monkeypatch):
    f = hub.facet("web")
    hub.config.web = {"default_min_interval_s": 0}
    f.app_profile = safety.OPERATOR_LOCAL
    f.fetcher_options = {"pin": True, "retries": 0}
    opened: list[str] = []
    f.open_default = lambda url: opened.append(url) or True
    monkeypatch.setattr("hoard_link.web.browser.playwright_installed", lambda: False)
    res = call(f, "/api/web/open", {"url": site.base + "/solve"})
    assert res["ok"] and res["via"] == "default_browser" and opened == [site.base + "/solve"]
    assert call(f, "/api/web/open", {"url": "file:///etc/passwd"})["status"] == 400
    assert call(f, "/api/web/open", {"url": "http://user:pw@x.test/"})["status"] == 400
    f.open_default = lambda url: False
    assert call(f, "/api/web/open", {"url": site.base + "/solve"})["ok"] is False


# ---- switching off, settings, config ------------------------------------------------------------------------------------------------

def test_disabled_in_hub_json(hub, web, site):
    hub.config.web = {"enabled": False}
    for path, body in (("/api/web/fetch", {"url": site.base}), ("/api/web/search", {"query": "x"}), ("/api/web/open", {"url": site.base})):
        res = call(web, path, body)
        assert res["status"] == 503 and "turned off" in res["error"]
    assert call(web, "/api/web/hosts")["status"] == 503
    assert call(web, "/api/web/status") == {"ok": True, "enabled": False, "respect_robots": True, "default_min_interval_s": 2.0,
                                            "cache_ttl_s": 300.0, "max_bytes": 3 * 1024 * 1024, "user_agent": web_mod.DEFAULT_USER_AGENT,
                                            "searxng": False, "brave": False}
    assert tools.call(hub, "hub_web_fetch", {"url": site.base})["ok"] is False
    assert site.hits == []


def test_settings_are_lenient_and_typed():
    d = web_settings({"default_min_interval_s": "fast", "cache_ttl_s": -3, "max_bytes": 5, "respect_robots": "no", "browser": "chrome",
                      "enabled": False, "searxng_url": " http://s.local/ ", "brave_api_key": 12, "search_intervals": {"ddg": 3, "bing": "x"}})
    assert d["default_min_interval_s"] == 2.0 and d["cache_ttl_s"] == 300.0 and d["max_bytes"] == 3 * 1024 * 1024
    assert d["respect_robots"] is True and d["browser"] == "auto" and d["enabled"] is False
    assert d["searxng_url"] == "http://s.local/" and d["brave_api_key"] == "" and d["search_intervals"] == {"ddg": 3.0}
    assert web_settings(None)["enabled"] is True and web_settings([])["browser"] == "auto"
    ok = web_settings({"default_min_interval_s": 0.5, "cache_ttl_s": 0, "max_bytes": 2048, "browser": "off"})
    assert ok["default_min_interval_s"] == 0.5 and ok["cache_ttl_s"] == 0.0 and ok["max_bytes"] == 2048 and ok["browser"] == "off"


def test_the_brave_key_is_never_shown_by_the_config_view(tmp_path):
    cfg = HubConfig(data_dir=str(tmp_path / "d"), web={"brave_api_key": "BSA-very-secret-1234", "browser": "off"})
    assert "very-secret" not in json.dumps(cfg.to_dict()) and cfg.to_dict()["web"]["brave_api_key"].endswith("1234")
    path = cfg.save()
    assert "BSA-very-secret-1234" in Path(path).read_text(encoding="utf-8")            # saving keeps the real one
    loaded = HubConfig.load(path, env={"HOARD_HUB_DATA_DIR": str(tmp_path / "d")})
    assert loaded.web["brave_api_key"] == "BSA-very-secret-1234"
    assert HubConfig.load(None, env={"HOARD_HUB_DATA_DIR": str(tmp_path / "none")}).web == {}


def test_robots_store_and_host_state_persist_in_web_db(tmp_path):
    db = WebDb(str(tmp_path / "web.db"))
    db.robots["http://x.test"] = {"text": "User-agent: *\nDisallow: /a", "ts": 5.0}
    db.hosts.update("x.test", min_interval_s=7.5, preferred_tier="browser", ok_count=3)
    db.audit("fake", "fetch", "x.test", status=200, ok=True, tier="http", ms=12)
    db.close()
    db2 = WebDb(str(tmp_path / "web.db"))
    assert db2.robots["http://x.test"] == {"text": "User-agent: *\nDisallow: /a", "ts": 5.0} and len(db2.robots) == 1
    assert db2.robots.get("http://nope.test") is None and list(db2.robots) == ["http://x.test"]
    row = db2.hosts.get("x.test")
    assert row["min_interval_s"] == 7.5 and row["preferred_tier"] == "browser" and row["ok_count"] == 3 and row["blocked_until_ts"] == 0.0
    assert db2.hosts.get("unknown.test")["min_interval_s"] is None and db2.hosts.items()[0][0] == "x.test"
    del db2.robots["http://x.test"]
    assert len(db2.robots) == 0 and db2.recent()[0]["caller"] == "fake"
    db2.close()


# ---- HTTP through the real server --------------------------------------------------------------------------------------------------------

@pytest.fixture
def served(hub, web):
    app = hub.get("fake")
    Path(app.token_file).parent.mkdir(parents=True, exist_ok=True)
    Path(app.token_file).write_text("fake-app-token", encoding="utf-8")
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, hub.config.url, {"Authorization": "Bearer " + hub.token}, {"Authorization": "Bearer fake-app-token"}
    server.shutdown()


def test_http_routes_need_a_family_caller_and_use_its_identity(served, site):
    hub, url, hubh, apph = served
    for path in ("/api/web/status", "/api/web/hosts"):
        assert _http(url + path)[0] == 401
    assert _http(url + "/api/web/fetch", {"url": site.base + "/article"})[0] == 401
    assert _http(url + "/api/web/fetch", {"url": site.base + "/article"}, headers={"Authorization": "Bearer nonsense"})[0] == 401
    status, body = _http(url + "/api/web/fetch", {"url": site.base + "/article", "caller": "ledger"}, headers=apph)
    assert status == 200 and body["ok"] and body["status"] == 200 and "Boil the water" in body["text"]
    assert _http(url + "/api/web/hosts", headers=apph)[1]["hosts"][0]["last_caller"] == "fake"      # a token cannot name another caller
    _http(url + "/api/web/fetch", {"url": site.base + "/article", "caller": "agent-x", "fresh": True}, headers=hubh)
    assert _http(url + "/api/web/hosts", headers=hubh)[1]["hosts"][0]["last_caller"] == "agent-x"    # the hub may label its own
    # a failed fetch is HTTP 200 with the upstream status inside; a bad request is 400
    status, body = _http(url + "/api/web/fetch", {"url": site.base + "/missing"}, headers=apph)
    assert status == 200 and body["ok"] is False and body["status"] == 404
    assert _http(url + "/api/web/fetch", {}, headers=apph)[0] == 400
    assert _http(url + "/api/web/nothing", {}, headers=apph)[0] == 404
    # preview by GET, search by POST, status and clear by who may
    status, body = _http(url + "/api/web/preview?url=" + site.base + "/article", headers=apph)
    assert status == 200 and body["title"] == "Tea at home"
    assert _http(url + "/api/web/hosts/clear", {"host": "127.0.0.1"}, headers=apph)[0] == 403
    assert _http(url + "/api/web/hosts/clear", {"host": "127.0.0.1"}, headers=UI)[0] == 200
    assert _http(url + "/api/web/status", headers=UI)[1]["enabled"] is True
    status, body = _http(url + "/api/web/search", {"query": "x", "engines": ["nope"]}, headers=apph)
    assert status == 400 and "unknown engine" in body["error"]
    assert _http(url + "/api/web/open", {"url": "http://user:pw@x.test/"}, headers=apph)[0] == 400


def test_http_fetch_file_and_the_hub_page_may_use_operator_local(hub, site):
    f = hub.facet("web")
    hub.config.web = {"default_min_interval_s": 0, "browser": "off"}
    f.fetcher_options = {"pin": True, "retries": 0}                      # apps keep the public policy here
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = hub.config.url
        status, body = _http(url + "/api/web/fetch", {"url": site.base + "/article", "profile": "operator_local", "extract": "readable"}, headers=UI)
        assert status == 200 and body["ok"] and body["extract"]["title"] == "Tea at home"
        status, body = _http(url + "/api/web/fetch", {"url": site.base + "/article"}, headers=UI)
        assert status == 200 and body["ok"] is False and body["error_kind"] == "policy"
        status, body = _http(url + "/api/web/preview", {"url": site.base + "/article", "profile": "operator_local"}, headers=UI)
        assert status == 200 and body["ok"] and body["from_cache"] is False
        status, body = _http(url + "/api/web/preview", {"url": site.base + "/article"}, headers=UI)
        assert body["ok"] is False and body["error_kind"] == "policy"           # nothing the operator read is kept for the apps
        site.routes["/f.txt"] = (200, {"Content-Type": "text/plain"}, b"data")
        status, body = _http(url + "/api/web/fetch_file", {"url": site.base + "/f.txt", "profile": "operator_local"}, headers=UI)
        assert status == 200 and body["ok"] and Path(body["path"]).parent.name == "ui"
    finally:
        server.shutdown()


def test_the_ui_script_is_served_and_listed(served):
    hub, url, hubh, apph = served
    body = _http(url + "/api/facets")[1]
    assert next(f for f in body["facets"] if f["id"] == "web")["ui_scripts"] == ["web.js"]
    import urllib.request
    js = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + "/ui/web.js", timeout=10).read().decode("utf-8")
    assert 'H.register("web"' in js and 'placement: "tab"' in js and "Desbloquear" in js


# ---- agent tools -----------------------------------------------------------------------------------------------------------------------------

def test_tool_descriptions_and_catalogue():
    cat = {t["name"]: t for t in WebFacet.tools()}
    assert set(cat) == {"hub_web_fetch", "hub_web_search", "hub_web_preview", "hub_web_hosts"}
    for t in cat.values():
        first = t["description"].split("\n")[0]
        assert len(first) <= 110, (t["name"], len(first))
        assert t["inputSchema"]["type"] == "object"
    assert "descargar página" in cat["hub_web_fetch"]["description"] and "buscar en internet" in cat["hub_web_search"]["description"]
    assert cat["hub_web_fetch"]["annotations"]["readOnlyHint"] is True and "annotations" not in cat["hub_web_hosts"]
    assert {t["name"] for t in tools.all_tools()} >= set(cat)


def test_tools_fetch_search_preview_and_hosts(hub, web, site):
    res = tools.call(hub, "hub_web_fetch", {"url": site.base + "/article", "profile": "operator_local"})
    assert res["ok"] and res["title"] == "Tea at home" and "Boil the water" in res["content"] and "text" not in res
    cut = tools.call(hub, "hub_web_fetch", {"url": site.base + "/article", "profile": "operator_local", "max_chars": 500})
    assert len(cut["content"]) == 500 and cut["content_truncated"] is True
    md = tools.call(hub, "hub_web_fetch", {"url": site.base + "/article", "profile": "operator_local", "extract": "markdown"})
    assert "# Tea at home" in md["content"]
    meta = tools.call(hub, "hub_web_fetch", {"url": site.base + "/article", "profile": "operator_local", "extract": "meta"})
    assert meta["data"]["description"] == "How to brew tea."
    raw = tools.call(hub, "hub_web_fetch", {"url": site.base + "/article", "profile": "operator_local", "extract": "raw"})
    assert "<title>" in raw["content"]
    assert tools.call(hub, "hub_web_fetch", {"url": ""})["ok"] is False
    pv = tools.call(hub, "hub_web_preview", {"url": site.base + "/article"})
    assert pv["ok"] and pv["title"] == "Tea at home" and pv["favicons"]
    web.db.hosts.update("slow.example", blocked_until_ts=time.time() + 500, block_reason="http_429")
    hosts = tools.call(hub, "hub_web_hosts", {})
    assert hosts["ok"] and any(h["host"] == "slow.example" and h["blocked_now"] for h in hosts["hosts"])
    cleared = tools.call(hub, "hub_web_hosts", {"clear_block": "slow.example"})
    assert cleared["ok"] and tools.call(hub, "hub_web_hosts", {})["blocked"] == 0
    assert tools.call(hub, "hub_web_hosts", {"clear_block": "never.seen"})["ok"] is False
    assert [e["data"]["caller"] for e in events(hub, "web.fetch")][0] == "agent"
    site.routes["/search"] = (200, {"Content-Type": "application/json"}, json.dumps({"results": [{"url": "https://example.org/z", "title": "Z", "content": "c"}]}))
    hub.config.web = {**hub.config.web, "searxng_url": site.base, "search_intervals": {"searxng": 0}}
    found = tools.call(hub, "hub_web_search", {"query": "zeta", "engines": ["searxng"]})
    assert found["ok"] and found["hits"][0]["title"] == "Z"
    assert tools.call(hub, "hub_web_search", {"query": ""})["ok"] is False
