"""hoard_link.web.fetch: the polite, policy-checked fetcher. Network free: ``httpx.MockTransport`` for the logic, a
local ``http.server`` on 127.0.0.1 (``operator_local`` profile) for the real socket path."""

from __future__ import annotations

import gzip
import json
import socket
import ssl
import zlib

import pytest

from hoard_link.web import fetch, safety
from hoard_link.web.fetch import ApiError, FetchResult, Fetcher, JsonApiClient, JsonFileHostState, MemoryHostState
from tests.commons.web_helpers import Site, article, resolver_from

httpx = pytest.importorskip("httpx")

HOSTS = {"example.com": ["93.184.216.34"], "www.example.com": ["93.184.216.34"], "other.example": ["93.184.216.35"],
         "api.example": ["93.184.216.36"], "private.example": ["10.0.0.9"], "shop.example": ["93.184.216.37"]}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make(handler, **kw):
    clock = kw.pop("clock", None) or Clock()
    kw.setdefault("min_interval_s", 0.0)
    kw.setdefault("retries", 0)
    f = Fetcher(transport=httpx.MockTransport(handler), resolver=resolver_from(HOSTS), clock=clock, sleep=clock.sleep, **kw)
    f.clock_ = clock
    return f


def ok_page(request):
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=article(60, "Hello"))


def test_a_plain_page_comes_back_decoded_with_lowercase_headers():
    fr = make(ok_page).get("https://example.com/a?x=1")
    assert fr.ok and fr.status == 200 and fr.tier == "http" and not fr.blocked
    assert "Hello" in fr.text and fr.final_url == "https://example.com/a?x=1" and fr.headers["content-type"].startswith("text/html")
    assert fr.error == "" and fr.error_kind == "" and fr.to_dict()["text_len"] == len(fr.text) and "text" not in fr.to_dict()


def test_params_are_merged_into_the_url():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return ok_page(request)

    make(handler).get("https://example.com/s?a=1", params={"b": "x y", "c": None})
    assert seen[-1] == "https://example.com/s?a=1&b=x+y"


# ---- policy -----------------------------------------------------------------------------------

@pytest.mark.parametrize("url,needle", [
    ("file:///etc/passwd", "scheme"), ("http://127.0.0.1/", "loopback"), ("http://10.0.0.5/", "private"),
    ("http://169.254.169.254/latest/", "metadata"), ("http://100.64.0.1/", "CGNAT"), ("http://2130706433/", "obfuscated"),
    ("http://private.example/", "10.0.0.9"), ("http://localhost/", "single-label"), ("http://user:pw@example.com/", "credentials"),
])
def test_unsafe_urls_never_reach_the_transport(url, needle):
    calls = []
    f = make(lambda r: calls.append(r) or ok_page(r))
    fr = f.get(url)
    assert not fr.ok and fr.error_kind == "policy" and needle in fr.error and calls == []


def test_unresolvable_host_is_a_dns_error_not_a_policy_error():
    fr = make(ok_page).get("https://nowhere.example/")
    assert fr.error_kind == "dns" and not fr.ok


@pytest.mark.parametrize("target,needle", [
    ("http://169.254.169.254/latest/meta-data/", "metadata"), ("file:///etc/passwd", "scheme"), ("http://10.0.0.5/admin", "private"),
    ("http://127.0.0.1:22/", "loopback"), ("http://private.example/x", "10.0.0.9"), ("http://2130706433/", "obfuscated"),
    ("ftp://example.com/x", "scheme"),
])
def test_redirects_are_checked_at_every_hop(target, needle):
    hit = []

    def handler(request):
        hit.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": target})

    fr = make(handler).get("https://example.com/start")
    assert not fr.ok and fr.error_kind == "policy" and fr.error.startswith("redirect refused:") and needle in fr.error
    assert hit == ["https://example.com/robots.txt", "https://example.com/start"]          # the target was never requested


def test_redirect_chain_is_followed_and_recorded_up_to_the_limit():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        n = int(request.url.path.strip("/") or 0)
        if n < 3:
            return httpx.Response(301, headers={"location": f"/{n + 1}"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article(60))

    fr = make(handler).get("https://example.com/0")
    assert fr.ok and fr.final_url == "https://example.com/3" and fr.redirects == ["https://example.com/1", "https://example.com/2", "https://example.com/3"]

    def endless(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(302, headers={"location": "/loop"})

    fr = make(endless, max_redirects=3).get("https://example.com/loop")
    assert not fr.ok and fr.error_kind == "redirects" and len(fr.redirects) == 4


def test_credential_headers_do_not_follow_a_cross_host_redirect():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization"), request.headers.get("cookie")))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "example.com" and request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        if request.url.host == "example.com" and request.url.path == "/b":
            return httpx.Response(302, headers={"location": "https://other.example/c"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article(60))

    fr = make(handler).get("https://example.com/a", headers={"Authorization": "Bearer s3cret", "Cookie": "sid=1"}, respect_robots=False)
    assert fr.ok
    assert seen[0] == ("example.com", "Bearer s3cret", "sid=1") and seen[1] == ("example.com", "Bearer s3cret", "sid=1")
    assert seen[2] == ("other.example", None, None)


# ---- robots -----------------------------------------------------------------------------------

def test_robots_disallow_is_honoured_and_fetched_once():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nDisallow: /private\nCrawl-delay: 3\n")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article(60))

    f = make(handler)
    blocked = f.get("https://example.com/private/x")
    assert not blocked.ok and blocked.error_kind == "robots" and "private/x" not in paths
    assert f.get("https://example.com/public").ok and f.get("https://example.com/public2").ok
    assert paths.count("/robots.txt") == 1
    assert f.get("https://example.com/private/x", respect_robots=False).ok


def test_robots_crawl_delay_sets_the_pace():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nCrawl-delay: 7\n")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article(60))

    f = make(handler)
    f.get("https://example.com/a")
    f.get("https://example.com/b")
    assert any(abs(s - 7) < 0.5 for s in f.clock_.sleeps)


def test_robots_unreachable_allows_with_a_note():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article(60))

    fr = make(handler).get("https://example.com/a")
    assert fr.ok and "robots.txt unreachable" in fr.note


def test_json_requests_skip_robots():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"a": 1})

    fr, data = make(handler).get_json("https://api.example/v1/x")
    assert data == {"a": 1} and fr.ok and paths == ["/v1/x"]


# ---- politeness, blocks, cooldown -------------------------------------------------------------

def test_min_interval_spaces_requests_to_one_host():
    f = make(ok_page, min_interval_s=5.0)
    f.get("https://example.com/a")
    f.get("https://example.com/b")
    f.get("https://other.example/c")
    assert len(f.clock_.sleeps) == 1 and 4.9 < f.clock_.sleeps[0] <= 5.0


def test_per_host_interval_override_and_status():
    f = make(ok_page, min_interval_s=0.0)
    f.set_min_interval("example.com", 9)
    f.get("https://example.com/a")
    f.get("https://example.com/b")
    assert f.clock_.sleeps and 8.9 < f.clock_.sleeps[0] <= 9
    row = next(r for r in f.host_status() if r["host"] == "example.com")
    assert row["ok_count"] == 2 and row["min_interval_s"] == 9 and row["blocked_now"] is False
    f.set_min_interval("example.com", None)
    assert f.state.get("example.com")["min_interval_s"] is None
    f.reset_host("example.com")
    assert f.state.get("example.com")["preferred_tier"] == ""


def test_429_starts_a_cooldown_that_honours_retry_after_and_is_capped():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(429, headers={"retry-after": "120", "content-type": "text/plain"}, text="slow down")

    f = make(handler)
    fr = f.get("https://example.com/a")
    assert fr.blocked and fr.block_reason == "http_429" and fr.error_kind == "blocked" and not fr.ok
    n = len(calls)
    again = f.get("https://example.com/b")
    assert again.blocked and again.error_kind == "blocked" and "not retrying" in again.error and len(calls) == n    # no request made
    f.clock_.now += 121
    f.get("https://example.com/c")
    assert len(calls) == n + 1                                                                                       # the cooldown ended


def test_retry_after_is_capped_at_one_hour():
    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(429, headers={"retry-after": "999999"}, text="x")

    f = make(handler)
    f.get("https://example.com/a")
    row = f.state.get("example.com")
    assert 3590 <= row["blocked_until_ts"] - f.clock_.now <= 3600


def test_clear_block_lifts_the_cooldown():
    state = {"blocked": True}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if state["blocked"]:
            return httpx.Response(403, text="forbidden")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article(60))

    f = make(handler)
    assert f.get("https://example.com/a").blocked
    state["blocked"] = False
    assert f.get("https://example.com/a").error_kind == "blocked"          # cooling down
    f.clear_block("example.com")
    assert f.get("https://example.com/a").ok


def test_a_challenge_page_served_with_200_is_a_block_not_a_document():
    page = "<html><head><title>Just a moment...</title></head><body>Checking your browser <script src='/cdn-cgi/challenge-platform/x'></script></body></html>"

    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, headers={"content-type": "text/html"}, text=page)

    fr = make(handler).get("https://example.com/a")
    assert fr.blocked and fr.block_reason == "cloudflare" and not fr.ok and fr.error_kind == "blocked"


def test_5xx_is_an_outage_not_a_block_and_is_retried():
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(1)
        return httpx.Response(503, text="down")

    f = make(handler, retries=2, backoff_s=0.5)
    fr = f.get("https://example.com/a")
    assert not fr.ok and not fr.blocked and fr.block_reason == "http_5xx" and fr.error_kind == "http" and len(calls) == 3
    assert f.clock_.sleeps == [0.5, 1.0]


def test_network_errors_are_retried_then_classified():
    attempts = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=article(60))

    fr = make(handler, retries=1).get("https://example.com/a")
    assert fr.ok and len(attempts) == 2
    for exc, kind in [(httpx.ConnectTimeout("t"), "timeout"), (httpx.ReadError("r"), "reset"), (httpx.ConnectError("c"), "network")]:
        def failing(request, exc=exc):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            raise exc
        fr = make(failing, retries=0).get("https://example.com/a")
        assert not fr.ok and fr.error_kind == kind and fr.status == 0


def test_classify_error_recognises_the_usual_suspects():
    assert fetch.classify_error(socket.gaierror(-2, "Name or service not known"), "x.example")[0] == "dns"
    assert fetch.classify_error(ssl.SSLCertVerificationError("bad cert"))[0] == "tls"
    assert fetch.classify_error(TimeoutError("t"), timeout=5)[0] == "timeout"
    assert fetch.classify_error(ConnectionRefusedError())[0] == "refused"
    assert fetch.classify_error(ConnectionResetError())[0] == "reset"
    assert fetch.classify_error(ValueError("x"))[0] == "network"


# ---- bodies -----------------------------------------------------------------------------------

def test_conditional_get_answers_not_modified():
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append((request.headers.get("if-none-match"), request.headers.get("if-modified-since")))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return httpx.Response(200, headers={"content-type": "text/html", "etag": '"v1"', "last-modified": "Wed, 30 Sep 2026 10:00:00 GMT"}, text=article(60))

    f = make(handler)
    first = f.get("https://example.com/a")
    assert first.ok and first.etag == '"v1"' and first.last_modified
    second = f.get("https://example.com/a", etag=first.etag, last_modified=first.last_modified)
    assert second.ok and second.not_modified and second.text == "" and seen[-1] == ('"v1"', first.last_modified)


def test_the_body_is_capped_and_flagged():
    big = httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"x" * 50_000))
    f = make(lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else big, max_bytes=1000)
    fr = f.get("https://example.com/a")
    assert fr.truncated and len(fr.text) == 1000 and fr.ok


def test_a_compression_bomb_never_inflates_past_the_cap():
    bomb = zlib.compress(b"\0" * 60_000_000, 9)
    assert len(bomb) < 100_000

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "deflate"}, stream=httpx.ByteStream(bomb))

    fr = make(handler, max_bytes=100_000).get("https://example.com/a")
    assert fr.truncated and len(fr.text) <= 100_000


def test_gzip_bodies_are_decoded():
    payload = gzip.compress(article(60, "Zipped").encode("utf-8"))

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"}, stream=httpx.ByteStream(payload))

    fr = make(handler).get("https://example.com/a")
    assert fr.ok and "Zipped" in fr.text


def test_charset_comes_from_the_header_then_the_meta_tag_then_cp1252():
    cases = [
        ({"content-type": "text/html; charset=iso-8859-1"}, "<p>caf\xe9</p>".encode("latin-1"), "café"),
        ({"content-type": "text/html"}, b'<meta charset="windows-1252"><p>caf\xe9 \x80</p>', "café €"),
        ({"content-type": "text/html"}, b"<p>caf\xe9 mislabelled</p>", "café mislabelled"),
        ({"content-type": "text/html; charset=utf-8"}, "<p>café €</p>".encode("utf-8"), "café €"),
    ]
    for headers, body, want in cases:
        f = make(lambda r, h=headers, b=body: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(200, headers=h, stream=httpx.ByteStream(b)))
        fr = f.get("https://example.com/a")
        assert want in fr.text, (headers, fr.text)


def test_binary_and_unwanted_content_types_are_content_errors():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG....")

    f = make(handler)
    assert f.get("https://example.com/a.png").error_kind == "content"
    raw = f.get("https://example.com/a.png", accept="any")
    assert raw.ok and raw.body.startswith(b"\x89PNG")
    fr = make(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text="<p>x</p>")).get("https://example.com/", allowed_mime=["application/json"], respect_robots=False)
    assert fr.error_kind == "content"


def test_get_json_parses_and_reports_invalid_json():
    def handler(request):
        if request.url.path == "/good":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"k": [1, 2]})
        return httpx.Response(200, headers={"content-type": "application/json"}, text="{oops")

    f = make(handler)
    assert f.get_json("https://api.example/good")[1] == {"k": [1, 2]}
    fr, data = f.get_json("https://api.example/bad")
    assert data is None and not fr.ok and fr.error_kind == "content" and "invalid JSON" in fr.error


def test_an_api_refusing_a_key_is_an_error_not_an_anti_bot_wall():
    f = make(lambda r: httpx.Response(401, headers={"content-type": "application/json"}, json={"error": "bad key"}))
    fr, data = f.get_json("https://api.example/v1")
    assert not fr.ok and fr.status == 401 and not fr.blocked and fr.error_kind == "http" and data is None


# ---- cache ------------------------------------------------------------------------------------

def test_disk_cache_serves_fresh_entries_revalidates_old_ones_and_survives_failures(tmp_path):
    mode = {"fail": False, "etag": True}
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(request.headers.get("if-none-match"))
        if mode["fail"]:
            return httpx.Response(500, text="boom")
        if mode["etag"] and request.headers.get("if-none-match") == '"e1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"content-type": "text/html", "etag": '"e1"'}, text=article(60, "Cached"))

    f = make(handler, cache_dir=tmp_path, cache_ttl_s=100)
    first = f.get("https://example.com/a")
    assert first.ok and not first.from_cache and len(calls) == 1
    second = f.get("https://example.com/a")
    assert second.from_cache and second.tier == "cache" and len(calls) == 1 and f.cache_stats()["entries"] == 1
    f.clock_.now += 101
    third = f.get("https://example.com/a")                       # expired: revalidated with the stored etag, 304 refreshes the entry
    assert third.ok and third.from_cache and calls[-1] == '"e1"' and "revalidated" in third.note
    f.clock_.now += 101
    mode["fail"] = True
    stale = f.get("https://example.com/a", retries=0)
    assert stale.ok and stale.stale and stale.from_cache and "served from cache after a failure" in stale.note
    assert f.clear_cache() == 1 and f.cache_stats()["entries"] == 0


def test_policy_and_robots_refusals_are_never_masked_by_the_cache(tmp_path):
    f = make(ok_page, cache_dir=tmp_path, cache_ttl_s=100)
    f.get("https://example.com/a")
    f.clock_.now += 500
    assert f.get("http://127.0.0.1/a").error_kind == "policy"


def test_offline_mode_answers_from_the_cache_only(tmp_path):
    online = make(ok_page, cache_dir=tmp_path, cache_ttl_s=100)
    online.get("https://example.com/a")
    calls = []
    offline = make(lambda r: calls.append(r) or ok_page(r), cache_dir=tmp_path, cache_ttl_s=100, offline=True)
    hit = offline.get("https://example.com/a")
    assert hit.ok and hit.from_cache
    miss = offline.get("https://example.com/never-fetched")
    assert not miss.ok and miss.error_kind == "offline" and calls == []


# ---- host state -------------------------------------------------------------------------------

def test_json_file_host_state_is_shared_between_instances(tmp_path):
    path = tmp_path / "hosts.json"
    a = JsonFileHostState(path)
    a.update("example.com", blocked_until_ts=5e9, block_reason="cloudflare", min_interval_s=7.0)
    b = JsonFileHostState(path)
    assert b.get("example.com")["block_reason"] == "cloudflare" and b.get("example.com")["min_interval_s"] == 7.0
    assert b.get("unknown.example")["ok_count"] == 0 and dict(b.items())["example.com"]["blocked_until_ts"] == 5e9
    json.loads(path.read_text(encoding="utf-8"))
    f = make(ok_page, state=b)
    fr = f.get("https://example.com/a")
    assert fr.blocked and fr.block_reason == "cloudflare"          # the cooldown another process recorded is respected


def test_memory_host_state_defaults():
    s = MemoryHostState()
    assert s.get("x")["last_fetch_ts"] == 0.0 and s.get("x")["preferred_tier"] == ""
    s.update("x", ok_count=3)
    assert s.get("x")["ok_count"] == 3 and list(s.items())[0][0] == "x"


# ---- the real socket path: local server, operator_local profile --------------------------------

def test_local_server_is_reachable_only_under_the_operator_profile():
    with Site({"/hello": (200, {"Content-Type": "text/html; charset=utf-8"}, article(60, "Local hello"))}) as site:
        url = f"{site.base}/hello"
        assert Fetcher(min_interval_s=0, retries=0).get(url).error_kind == "policy"
        op = Fetcher(profile=safety.OPERATOR_LOCAL, pin=True, min_interval_s=0, retries=0)
        fr = op.get(url, respect_robots=False)
        assert fr.ok and "Local hello" in fr.text and fr.headers["content-type"].startswith("text/html")
        assert Fetcher(profile=safety.INTERNAL, pin=True, min_interval_s=0, retries=0).get(url, respect_robots=False).ok
        op.close()


def test_local_server_hostname_is_pinned_and_keeps_the_host_header():
    with Site({"/h": (200, {"Content-Type": "text/html"}, article(60))}) as site:
        f = Fetcher(profile=safety.OPERATOR_LOCAL, pin=True, min_interval_s=0, retries=0, resolver=resolver_from({"app.internal-test.example": ["127.0.0.1"]}))
        fr = f.get(f"http://app.internal-test.example:{site.port}/h", respect_robots=False)
        assert fr.ok and site.hits[0][2]["host"] == f"app.internal-test.example:{site.port}"
        f.close()


def test_local_server_redirects_into_forbidden_places_are_refused():
    for target, needle in [("http://169.254.169.254/latest/meta-data/", "metadata"), ("file:///etc/passwd", "scheme")]:
        with Site({"/go": (302, {"Location": target}, b"")}) as site:
            f = Fetcher(profile=safety.OPERATOR_LOCAL, pin=True, min_interval_s=0, retries=0)
            fr = f.get(f"{site.base}/go", respect_robots=False)
            assert not fr.ok and fr.error_kind == "policy" and needle in fr.error and fr.error.startswith("redirect refused")
            f.close()


def test_local_server_redirect_to_a_local_page_is_followed_under_operator_local():
    with Site({"/a": (302, {"Location": "/b"}, b""), "/b": (200, {"Content-Type": "text/html"}, article(60, "Landed"))}) as site:
        f = Fetcher(profile=safety.OPERATOR_LOCAL, pin=True, min_interval_s=0, retries=0)
        fr = f.get(f"{site.base}/a", respect_robots=False)
        assert fr.ok and "Landed" in fr.text and fr.final_url.endswith("/b")
        f.close()


def test_local_server_connection_refused_is_classified():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    fr = Fetcher(profile=safety.OPERATOR_LOCAL, pin=True, min_interval_s=0, retries=0).get(f"http://127.0.0.1:{port}/", respect_robots=False)
    assert not fr.ok and fr.error_kind == "refused"


def test_local_server_streaming_cap_over_a_real_socket():
    with Site({"/big": (200, {"Content-Type": "text/plain"}, b"y" * 400_000)}) as site:
        f = Fetcher(profile=safety.OPERATOR_LOCAL, pin=True, min_interval_s=0, retries=0, max_bytes=50_000)
        fr = f.get(f"{site.base}/big", respect_robots=False)
        assert fr.truncated and len(fr.text) == 50_000
        f.close()


# ---- JsonApiClient ----------------------------------------------------------------------------

def api(handler, **kw):
    clock = kw.pop("clock", None) or Clock()
    c = JsonApiClient("https://api.example/v1", "Test/1.0", transport=httpx.MockTransport(handler), resolver=resolver_from(HOSTS),
                      clock=clock, sleep=clock.sleep, retries=0, **kw)
    c.clock_ = clock
    return c


def test_json_api_client_gets_json_with_params_and_user_agent():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["user-agent"]))
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"items": [1]})

    c = api(handler)
    assert c.get_json("items", {"page": 2}) == {"items": [1]}
    assert seen == [("https://api.example/v1/items?page=2", "Test/1.0")]
    assert c.get("https://api.example/other").status_code == 200


def test_json_api_client_caches_and_serves_stale_and_goes_offline(tmp_path):
    state = {"fail": False}
    calls = []

    def handler(request):
        calls.append(1)
        if state["fail"]:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"n": len(calls)})

    c = api(handler, cache_dir=tmp_path, ttl=60)
    assert c.get_json("x") == {"n": 1} and c.get_json("x") == {"n": 1} and len(calls) == 1 and (c.hits, c.misses) == (1, 1)
    c.clock_.now += 61
    state["fail"] = True
    assert c.get("x").cached and c.get_json("x") == {"n": 1}                     # stale-if-error
    off = api(handler, cache_dir=tmp_path, ttl=60, offline=True, clock=c.clock_)
    assert off.get_json("x") == {"n": 1}
    with pytest.raises(ApiError) as err:
        off.get_json("never")
    assert err.value.kind == "offline"


def test_json_api_client_error_kinds():
    def make_client(status, headers=None, text="x"):
        return api(lambda r: httpx.Response(status, headers=headers or {}, text=text))

    with pytest.raises(ApiError) as err:
        make_client(429, {"retry-after": "30"}).get("x")
    assert err.value.kind == "rate_limited" and err.value.retry_after == "30" and err.value.status == 429
    with pytest.raises(ApiError) as err:
        make_client(503).get("x")
    assert err.value.kind == "server_error"
    with pytest.raises(ApiError) as err:
        api(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))).get("x")
    assert err.value.kind == "unreachable"
    with pytest.raises(ApiError) as err:
        api(lambda r: httpx.Response(200, text="x")).get("http://127.0.0.1/x")
    assert err.value.kind == "policy"
    with pytest.raises(ApiError) as err:
        make_client(200, {"content-type": "application/json"}, "not json").get_json("x")
    assert err.value.kind == "server_error" and "not JSON" in str(err.value)
    assert make_client(404, {"content-type": "application/json"}, "{}").get("x").status_code == 404


def test_json_api_client_paces_calls():
    c = api(lambda r: httpx.Response(200, headers={"content-type": "application/json"}, json={}), min_interval_s=2.0)
    c.get("a", use_cache=False)
    c.get("b", use_cache=False)
    assert c.clock_.sleeps and 1.9 < c.clock_.sleeps[0] <= 2.0
