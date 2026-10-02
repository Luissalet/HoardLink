"""hoard_link.web.browser: the browser rung. Most tests use a fake Playwright (no browser needed); one smoke test drives a
real headless Chromium against a page served from 127.0.0.1 and skips when no browser can be started."""

from __future__ import annotations

import json
import threading
import time

import pytest

from hoard_link.web import browser, safety
from hoard_link.web.browser import BrowserRung, BrowserUnavailable
from hoard_link.web.fetch import Fetcher
from tests.commons.web_helpers import Site, article, resolver_from

HOSTS = {"example.com": ["93.184.216.34"], "shop.example": ["93.184.216.37"]}
CF = "<html><head><title>Just a moment...</title></head><body>Checking your browser <script src='/cdn-cgi/challenge-platform/x'></script></body></html>"


# ---- a fake Playwright ------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {"content-type": "text/html"}


class FakePage:
    def __init__(self, ctx):
        self.ctx = ctx
        self.url = "about:blank"
        self.closed = False

    def goto(self, url, wait_until=None, timeout=None):
        self.ctx.visits.append((url, threading.get_ident()))
        if self.ctx.goto_error:
            raise RuntimeError(self.ctx.goto_error)
        self.url = url
        return FakeResponse(*self.ctx.responses[min(len(self.ctx.visits) - 1, len(self.ctx.responses) - 1)][:2])

    def content(self):
        idx = min(len(self.ctx.visits) - 1 + self.ctx.rechecks, len(self.ctx.bodies) - 1)
        return self.ctx.bodies[idx]

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_timeout(self, ms):
        self.ctx.rechecks += 1

    def close(self):
        self.closed = True


class FakeCtx:
    def __init__(self, bodies, responses, goto_error=""):
        self.bodies, self.responses, self.goto_error = bodies, responses, goto_error
        self.visits: list[tuple[str, int]] = []
        self.rechecks = 0
        self.routes: list = []
        self.closed = False
        self.pages: list = []

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, owner):
        self.owner = owner

    def launch_persistent_context(self, **kwargs):
        self.owner.launches.append(kwargs)
        if kwargs.get("channel") in self.owner.failing_channels:
            raise RuntimeError(f"Executable doesn't exist for channel {kwargs.get('channel')}\nsecond line")
        if self.owner.fail_all:
            raise RuntimeError("no luck")
        self.owner.ctx = FakeCtx(self.owner.bodies, self.owner.responses, self.owner.goto_error)
        return self.owner.ctx


class FakePW:
    def __init__(self, bodies, responses=((200, None),), failing_channels=(), fail_all=False, goto_error=""):
        self.bodies, self.responses, self.failing_channels, self.fail_all, self.goto_error = list(bodies), list(responses), failing_channels, fail_all, goto_error
        self.launches: list[dict] = []
        self.ctx = None
        self.stopped = False
        self.chromium = FakeChromium(self)

    def stop(self):
        self.stopped = True


def rung(tmp_path, pw, **kw):
    kw.setdefault("resolver", resolver_from(HOSTS))
    kw.setdefault("channel", None)
    return BrowserRung(tmp_path / "profile", playwright_factory=lambda: pw, **kw)


def test_fetch_reads_a_page_through_the_browser_and_marks_the_tier(tmp_path):
    pw = FakePW([article(60, "Rendered")])
    r = rung(tmp_path, pw)
    try:
        fr = r.fetch("https://example.com/a")
        assert fr.ok and fr.tier == "browser" and "Rendered" in fr.text and fr.status == 200 and fr.final_url == "https://example.com/a"
        assert pw.launches[0]["headless"] is True and pw.launches[0]["user_data_dir"] == str(tmp_path / "profile")
        assert pw.ctx.pages[0].closed and pw.ctx.routes and pw.ctx.routes[0][0] == "**/*"
    finally:
        r.close()


def test_unsafe_urls_are_refused_before_any_browser_is_started(tmp_path):
    started = []
    r = BrowserRung(tmp_path / "p", playwright_factory=lambda: started.append(1), resolver=resolver_from(HOSTS))
    for url, kind in [("file:///etc/passwd", "policy"), ("http://127.0.0.1/", "policy"), ("data:text/html,<b>x</b>", "policy"),
                      ("javascript:alert(1)", "policy"), ("http://user:pw@example.com/", "policy"), ("http://nowhere.example/", "dns")]:
        fr = r.fetch(url)
        assert not fr.ok and fr.error_kind == kind and fr.tier == "browser", url
    assert started == []
    assert r.screenshot("file:///etc/passwd", tmp_path / "x.png")["ok"] is False
    assert r.capture_json("http://10.0.0.1/", "api")[0] is None
    with pytest.raises(safety.PolicyError):
        r.open_for_human("file:///etc/passwd")


def test_every_subrequest_of_a_page_goes_through_the_policy(tmp_path):
    pw = FakePW([article(60)])
    r = rung(tmp_path, pw)
    try:
        r.fetch("https://example.com/a")
        handler = pw.ctx.routes[0][1]

        class Req:
            def __init__(self, url):
                self.url = url

        class Route:
            def __init__(self, url):
                self.request = Req(url)
                self.action = ""

            def abort(self, reason):
                self.action = "abort:" + reason

            def continue_(self):
                self.action = "continue"

        verdicts = {}
        for url in ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8080/admin", "http://10.0.0.5/", "file:///etc/passwd",
                    "https://example.com/style.css", "data:image/png;base64,AAAA", "https://nowhere.example/x.js"]:
            route = Route(url)
            handler(route)
            verdicts[url] = route.action
        assert verdicts["http://169.254.169.254/latest/meta-data/"] == "abort:blockedbyclient"
        assert verdicts["http://127.0.0.1:8080/admin"] == "abort:blockedbyclient" and verdicts["http://10.0.0.5/"] == "abort:blockedbyclient"
        assert verdicts["file:///etc/passwd"] == "abort:blockedbyclient"
        assert verdicts["https://example.com/style.css"] == "continue" and verdicts["data:image/png;base64,AAAA"] == "continue"
        assert verdicts["https://nowhere.example/x.js"] == "continue"          # an unresolvable name is the browser's business
    finally:
        r.close()


def test_a_challenge_page_is_reported_as_blocked_and_a_self_refreshing_one_is_waited_for(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "PASSIVE_RECHECK_WAIT_S", 0.0)
    pw = FakePW([CF, CF, CF], responses=[(403, None)])
    r = rung(tmp_path, pw)
    try:
        fr = r.fetch("https://example.com/a")
        assert fr.blocked and fr.block_reason == "cloudflare" and not fr.ok and fr.error_kind == "blocked"
    finally:
        r.close()
    pw = FakePW([CF, article(60, "Through")], responses=[(503, None)])
    r = rung(tmp_path / "second", pw)
    try:
        fr = r.fetch("https://example.com/a")
        assert fr.ok and "Through" in fr.text and not fr.blocked
    finally:
        r.close()


def test_browser_channels_fall_back_in_order(tmp_path):
    pw = FakePW([article(60)], failing_channels=("chrome",))
    r = rung(tmp_path, pw, channel=["chrome", None])
    try:
        assert r.fetch("https://example.com/a").ok
        assert [l.get("channel") for l in pw.launches] == ["chrome", None] and r.channel_used == "chromium"
    finally:
        r.close()


def test_when_no_browser_starts_the_error_lists_what_failed(tmp_path):
    pw = FakePW([article(60)], fail_all=True)
    r = rung(tmp_path, pw, channel=["chrome", "msedge"])
    try:
        fr = r.fetch("https://example.com/a")
        assert not fr.ok and fr.error_kind == "network" and "no browser could be started" in fr.error and "chrome" in fr.error and "msedge" in fr.error
        with pytest.raises(BrowserUnavailable):
            browser.launch_context(pw, tmp_path / "x", channel=["chrome"])
    finally:
        r.close()


def test_navigation_errors_come_back_as_results_and_reset_the_browser(tmp_path):
    pw = FakePW([article(60)], goto_error="Timeout 25000ms exceeded.")
    r = rung(tmp_path, pw)
    try:
        fr = r.fetch("https://example.com/a")
        assert not fr.ok and fr.error_kind == "timeout" and "browser error" in fr.error
        assert pw.ctx.closed and pw.stopped                       # reset: the next call starts clean
    finally:
        r.close()


def test_without_playwright_the_rung_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "playwright_installed", lambda: False)
    r = BrowserRung(tmp_path / "p", resolver=resolver_from(HOSTS))
    assert r.available() is False and "pip install playwright" in r.unavailable_reason()
    fr = r.fetch("https://example.com/a")
    assert not fr.ok and fr.error_kind == "network" and "Playwright is not installed" in fr.error


def test_every_call_runs_on_one_worker_thread(tmp_path):
    pw = FakePW([article(60)])
    r = rung(tmp_path, pw)
    try:
        results = []
        threads = [threading.Thread(target=lambda: results.append(r.fetch("https://example.com/a"))) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert len(results) == 4 and all(x.ok for x in results)
        idents = {tid for _, tid in pw.ctx.visits}
        assert len(idents) == 1 and threading.get_ident() not in idents          # one worker, never the caller
    finally:
        r.close()


def test_the_browser_closes_itself_when_idle(tmp_path):
    pw = FakePW([article(60)])
    r = rung(tmp_path, pw, idle_s=0.3)
    try:
        r.fetch("https://example.com/a")
        ctx = pw.ctx
        deadline = time.time() + 5
        while not ctx.closed and time.time() < deadline:
            time.sleep(0.05)
        assert ctx.closed and pw.stopped
        assert r.fetch("https://example.com/b").ok and pw.ctx is not ctx          # and starts again on demand
    finally:
        r.close()


def test_channel_order_per_platform():
    assert browser.channel_order("win32") == ["msedge", "chrome", None]
    assert browser.channel_order("darwin") == ["chrome", "msedge", None]
    assert browser.channel_order("linux") == ["chrome", None, "msedge"]


def test_find_chromium_reads_a_playwright_browsers_folder(tmp_path, monkeypatch):
    exe = tmp_path / "chromium-1234" / "chrome-linux" / "chrome"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    older = tmp_path / "chromium-1000" / "chrome-linux" / "chrome"
    older.parent.mkdir(parents=True)
    older.write_text("#!/bin/sh\n")
    older.chmod(0o755)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert browser.find_chromium() == str(exe)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "nothing-here"))
    assert browser.find_chromium() is None or "nothing-here" not in browser.find_chromium()


def test_launch_context_adds_no_sandbox_as_root_and_tries_the_executable_fallback(tmp_path, monkeypatch):
    import os
    seen = []

    class PW:
        class chromium:                                                   # noqa: N801
            @staticmethod
            def launch_persistent_context(**kw):
                seen.append(kw)
                if "executable_path" not in kw:
                    raise RuntimeError("channel missing")
                return object()

    monkeypatch.setattr(browser, "find_chromium", lambda: "/opt/fake/chrome")
    monkeypatch.setattr(browser, "system_browsers", lambda: [])
    ctx, name = browser.launch_context(PW, tmp_path)
    assert name.startswith("chromium (") and seen[-1]["executable_path"] == "/opt/fake/chrome"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        assert "--no-sandbox" in seen[-1]["args"]


# ---- the rung inside the Fetcher --------------------------------------------------------------

def test_the_fetcher_falls_back_to_the_browser_when_http_is_blocked_and_remembers_it(tmp_path):
    httpx = pytest.importorskip("httpx")

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403, headers={"content-type": "text/html"}, text=CF)

    pw = FakePW([article(60, "Browser got through")])
    r = rung(tmp_path, pw)
    f = Fetcher(transport=httpx.MockTransport(handler), resolver=resolver_from(HOSTS), browser=r, min_interval_s=0, retries=0)
    try:
        fr = f.get("https://shop.example/p")
        assert fr.ok and fr.tier == "browser" and "Browser got through" in fr.text
        assert f.state.get("shop.example")["preferred_tier"] == "browser"
        assert f.get("https://shop.example/q").tier == "browser"              # straight to the browser next time
    finally:
        f.close()
        r.close()


def test_the_fetcher_reports_a_missing_browser_without_failing(tmp_path):
    httpx = pytest.importorskip("httpx")
    f = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(403, text=CF)),
                resolver=resolver_from(HOSTS), browser=False, min_interval_s=0, retries=0)
    fr = f.get("https://shop.example/p")
    assert fr.blocked and fr.tier == "http"
    assert "not configured" in f.get("https://shop.example/p", tier="browser").error


# ---- one real headless browser ----------------------------------------------------------------

PAGE = """<!doctype html><html><head><title>Smoke page</title></head><body><h1 id="h">loading</h1>
<script>document.getElementById('h').textContent = 'rendered by javascript ' + (6 * 7);
fetch('/api/data.json').then(r => r.json()).then(d => { document.body.insertAdjacentHTML('beforeend', '<p>api says ' + d.answer + '</p>'); });</script></body></html>"""


def test_real_browser_smoke(tmp_path):
    if not browser.playwright_installed():
        pytest.skip("playwright is not installed")
    routes = {
        "/": (200, {"Content-Type": "text/html"}, PAGE),
        "/api/data.json": (200, {"Content-Type": "application/json"}, json.dumps({"answer": 42})),
    }
    with Site(routes) as site:
        r = BrowserRung(tmp_path / "profile", profile=safety.OPERATOR_LOCAL, settle_s=3.0, idle_s=30)
        try:
            # file:// is refused by policy before any browser is involved, whatever the profile
            local = tmp_path / "local.html"
            local.write_text("<h1>secret</h1>", encoding="utf-8")
            refused = r.fetch(local.as_uri())
            assert not refused.ok and refused.error_kind == "policy" and "scheme" in refused.error

            fr = r.fetch(site.base + "/")
            if not fr.ok and "no browser could be started" in fr.error:
                pytest.skip("no Chromium could be launched here: " + fr.error[:200])
            assert fr.ok and fr.tier == "browser", fr.error
            assert "rendered by javascript 42" in fr.text and "api says 42" in fr.text     # JavaScript ran and fetch() worked

            data, error = r.capture_json(site.base + "/", "/api/data.json", timeout_s=20)
            assert error == "" and data == {"answer": 42}

            shot = r.screenshot(site.base + "/", tmp_path / "shots" / "page.png", width=800)
            assert shot["ok"] and (tmp_path / "shots" / "page.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

            # the public profile cannot see the same page
            public = BrowserRung(tmp_path / "profile2", profile=safety.PUBLIC)
            assert public.fetch(site.base + "/").error_kind == "policy"
        finally:
            r.close()
