"""hoard_link.web.search: parsers, rank fusion and the engine runner (all answers faked; no network)."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, quote

import pytest

from hoard_link.web import search
from hoard_link.web.fetch import Fetcher
from hoard_link.web.search import WebSearch, rrf_merge
from tests.commons.web_helpers import resolver_from

httpx = pytest.importorskip("httpx")

DDG = """<html><body><div id="links">
<div class="result results_links result--ad"><a class="result__a" href="//duckduckgo.com/y.js?ad_provider=x">Ad</a></div>
<div class="result"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.example.org%2Fpage%3Fid%3D1%26utm_source%3Dx&amp;rut=abc">First <b>hit</b></a></h2>
<a class="result__snippet" href="x">Snippet   one with <b>bold</b></a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.example%2F">Second</a><a class="result__snippet">Two</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/settings">Internal</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?rut=nothing">No target</a></div>
</div></body></html>"""

BING_URL = "https://news.example.net/story?id=7"
BING = f"""<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=abc&u=a1{base64.urlsafe_b64encode(BING_URL.encode()).decode().rstrip('=')}&ntb=1">News <b>story</b></a></h2>
<div class="b_caption"><p>Caption <strong>text</strong> here</p></div></li>
<li class="b_algo"><h2><a href="https://direct.example/x">Direct</a></h2><div class="b_caption"><p class="b_lineclamp2">Direct snippet</p></div></li>
<li class="b_algo"><h2><a href="https://www.bing.com/images/search?q=x">Internal</a></h2></li>
<li class="b_ad"><h2><a href="https://ad.example/">Ad</a></h2></li></ol></body></html>"""

SEARX = {"results": [{"url": "https://www.example.org/page?id=1", "title": " Searx  hit ", "content": "from searx", "publishedDate": "2026-09-30T10:00:00"},
                     {"url": "https://third.example/", "title": "Third", "content": ""}, {"title": "no url"}]}
BRAVE = {"web": {"results": [{"url": "https://other.example/", "title": "<strong>Brave</strong> hit", "description": "desc <b>x</b>", "page_age": "2026-09-01T00:00:00"},
                             {"url": "https://fourth.example/", "title": "Fourth", "description": "d"}]}}
GNEWS = """<rss><channel><item><title>Fresh story - Paper</title><link>https://news.google.com/rss/articles/1</link><pubDate>Thu, 01 Oct 2026 08:00:00 GMT</pubDate><source url="https://paper.example">Paper</source></item>
<item><title>Old story - Paper</title><link>https://news.google.com/rss/articles/2</link><pubDate>Mon, 01 Jun 2026 08:00:00 GMT</pubDate><source url="https://paper.example">Paper</source></item></channel></rss>"""


def test_unwrap_helpers():
    assert search.unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fx%3Fy%3D1&rut=z") == "https://a.example/x?y=1"
    assert search.unwrap_ddg("https://duckduckgo.com/l/?rut=1") == ""                       # a redirector without a target
    assert search.unwrap_ddg("https://duckduckgo.com/y.js?ad=1") == "https://duckduckgo.com/y.js?ad=1"
    assert search.unwrap_ddg("https://plain.example/x") == "https://plain.example/x"
    assert search.unwrap_bing("https://www.bing.com/ck/a?u=a1" + base64.urlsafe_b64encode(b"https://t.example/p").decode().rstrip("=")) == "https://t.example/p"
    assert search.unwrap_bing("https://www.bing.com/ck/a?u=zzz") == "" and search.unwrap_bing("https://x.example/") == "https://x.example/"


def test_parse_ddg_html_drops_ads_and_internal_links():
    hits = search.parse_ddg_html(DDG)
    assert [h["url"] for h in hits] == ["https://www.example.org/page?id=1&utm_source=x", "https://other.example/"]
    assert hits[0]["title"] == "First hit" and hits[0]["snippet"] == "Snippet one with bold" and hits[0]["engine"] == "ddg" and hits[1]["rank"] == 2


def test_parse_bing_html_unwraps_redirects_and_skips_internal_results():
    hits = search.parse_bing_html(BING)
    assert [h["url"] for h in hits] == [BING_URL, "https://direct.example/x"]
    assert hits[0]["snippet"] == "Caption text here" and hits[1]["snippet"] == "Direct snippet"


def test_parse_json_engines():
    s = search.parse_searxng_json(SEARX)
    assert [h["url"] for h in s] == ["https://www.example.org/page?id=1", "https://third.example/"] and s[0]["title"] == "Searx hit" and s[0]["published"]
    assert search.parse_searxng_json(json.dumps(SEARX))[0]["engine"] == "searxng" and search.parse_searxng_json("not json") == [] and search.parse_searxng_json(None) == []
    b = search.parse_brave_json(BRAVE)
    assert b[0]["title"] == "Brave hit" and b[0]["snippet"] == "desc x" and b[0]["published"] == "2026-09-01T00:00:00" and search.parse_brave_json({"web": None}) == []


def test_is_blocked_page_needs_no_hits_and_a_signal():
    assert search.is_blocked_page("<div class='anomaly-modal'>x</div>", []) and not search.is_blocked_page("<div class='anomaly-modal'>x</div>", [1])
    assert not search.is_blocked_page("<p>no results found</p>", [])


def test_freshness_maps():
    assert [search.ddg_freshness(d) for d in (None, 1, 7, 30, 400)] == ["", "d", "w", "m", "y"]
    assert [search.bing_freshness(d) for d in (None, 1, 7, 30, 400)] == ["", 'ex1:"ez1"', 'ex1:"ez2"', 'ex1:"ez3"', ""]
    assert [search.brave_freshness(d) for d in (None, 1, 7, 30, 400)] == ["", "pd", "pw", "pm", "py"]
    assert [search.searxng_time_range(d) for d in (None, 1, 7, 30, 400)] == ["", "day", "week", "month", "year"]


# ---- rank fusion ------------------------------------------------------------------------------

def hit(url, engine, rank, title="", snippet="", published=None):
    return {"url": url, "title": title or url, "snippet": snippet, "engine": engine, "rank": rank, "published": published}


def test_rrf_merges_the_same_page_found_by_several_engines():
    a = [hit("https://www.example.org/p?utm_source=x", "ddg", 1, snippet=""), hit("https://only-ddg.example/", "ddg", 2)]
    b = [hit("http://example.org/p/", "bing", 3, snippet="bing snippet"), hit("https://only-bing.example/", "bing", 1)]
    merged = rrf_merge([a, b])
    assert merged[0]["engine"] == "ddg+bing" and merged[0]["snippet"] == "bing snippet" and merged[0]["rank"] == 1
    assert [m["rank"] for m in merged] == [1, 2, 3] and len(merged) == 3
    assert merged[0]["score"] == round(1 / 61 + 1 / 63, 6)


def test_rrf_weights_and_ordering_and_junk():
    a = [hit("https://a.example/", "x", 1)]
    b = [hit("https://b.example/", "y", 1)]
    assert [m["url"] for m in rrf_merge([a, b])] == ["https://a.example/", "https://b.example/"]               # tie: first seen wins
    assert [m["url"] for m in rrf_merge([a, b], weights=[1, 3])][0] == "https://b.example/"
    assert [m["url"] for m in rrf_merge([a, b], weights={"x": 0.1, "y": 1})][0] == "https://b.example/"
    assert rrf_merge([[{"url": ""}, {"url": "not a url"}]]) == [] and rrf_merge([]) == []


# ---- the engine runner ------------------------------------------------------------------------

HOSTS = {"html.duckduckgo.com": ["93.184.216.40"], "www.bing.com": ["93.184.216.41"], "api.search.brave.com": ["93.184.216.42"],
         "news.google.com": ["93.184.216.43"], "searx.lan.example": ["192.168.1.50"]}
NOW = 1_790_000_000.0          # 2026-09-21; the "fresh" story is 10 days old


class Rig:
    def __init__(self, **answers):
        self.answers = answers
        self.requests: list = []

    def handler(self, request):
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        key = {"html.duckduckgo.com": "ddg", "api.search.brave.com": "brave", "news.google.com": "gnews", "searx.lan.example": "searxng"}.get(host)
        if host == "www.bing.com":
            key = "bingnews" if path.startswith("/news") else "bing"
        answer = self.answers.get(key, httpx.Response(500))
        return answer(request) if callable(answer) else answer

    def search(self, **kw):
        sleeps: list = []
        fetcher = Fetcher(transport=httpx.MockTransport(self.handler), resolver=resolver_from(HOSTS), sleep=sleeps.append, retries=0)
        ws = WebSearch(fetcher, clock=lambda: NOW, **kw)
        ws.sleeps = sleeps
        return ws


def html_ok(text):
    return httpx.Response(200, headers={"content-type": "text/html"}, text=text)


def json_ok(data):
    return httpx.Response(200, headers={"content-type": "application/json"}, json=data)


def test_search_fuses_engines_and_reports_failures_per_engine():
    rig = Rig(ddg=html_ok(DDG), bing=html_ok(BING), searxng=json_ok(SEARX), brave=json_ok(BRAVE))
    ws = rig.search(searxng_url="http://searx.lan.example:8080", brave_key="k3y")
    hits, errors = ws.search("example query", 10)
    urls = [h["url"] for h in hits]
    assert errors == {}
    assert urls[0] == "https://www.example.org/page?id=1" and not any("utm_" in u for u in urls)
    top = hits[0]
    assert "+" in top["engine"] and {"ddg", "searxng"} <= set(top["engine"].split("+"))                     # found by both
    assert hits[-1]["rank"] == len(hits) and len({h["url"] for h in hits}) == len(hits)


def test_one_failing_engine_never_takes_the_others_down():
    rig = Rig(ddg=httpx.Response(202, headers={"content-type": "text/html"}, text="<html>please wait</html>"), bing=html_ok(BING))
    hits, errors = rig.search().search("q", 5)
    assert [h["engine"] for h in hits] == ["bing", "bing"] and "ddg" in errors and "bot check" in errors["ddg"]


def test_a_captcha_page_is_reported_not_bypassed():
    rig = Rig(ddg=html_ok("<html><div class='anomaly-modal'>captcha</div></html>"), bing=html_ok("<html><p>nothing</p></html>"))
    hits, errors = rig.search().search("q", 5)
    assert hits == [] and "blocked" in errors["ddg"] and errors["bing"] == "no results"


def test_request_parameters_follow_language_region_and_freshness():
    rig = Rig(ddg=html_ok(DDG), bing=html_ok(BING), searxng=json_ok(SEARX), brave=json_ok(BRAVE))
    rig.search(searxng_url="http://searx.lan.example:8080", brave_key="k3y", lang="es", region="es").search("café madrid", 5, freshness_days=7)
    by_host = {r.url.host: r for r in rig.requests}
    ddg = parse_qs(by_host["html.duckduckgo.com"].url.query.decode())
    assert ddg["q"] == ["café madrid"] and ddg["df"] == ["w"] and ddg["kl"] == ["es-es"]
    bing = parse_qs(by_host["www.bing.com"].url.query.decode())
    assert bing["cc"] == ["ES"] and bing["setlang"] == ["es"] and bing["filters"] == ['ex1:"ez2"']
    brave = by_host["api.search.brave.com"]
    assert brave.headers["x-subscription-token"] == "k3y" and parse_qs(brave.url.query.decode())["freshness"] == ["pw"]
    searx = parse_qs(by_host["searx.lan.example"].url.query.decode())
    assert searx["format"] == ["json"] and searx["time_range"] == ["week"]


def test_searxng_may_live_on_a_private_address_but_result_pages_may_not_point_there():
    results = {"results": [{"url": "https://public.example/a", "title": "ok", "content": "x"}, {"url": "http://192.168.1.1/admin", "title": "bad", "content": "x"},
                           {"url": "http://localhost/x", "title": "bad2", "content": "x"}, {"url": "http://169.254.169.254/", "title": "bad3", "content": ""},
                           {"url": "file:///etc/passwd", "title": "bad4", "content": ""}, {"url": "http://user:pw@public.example/", "title": "bad5", "content": ""}]}
    rig = Rig(searxng=json_ok(results))
    hits, errors = rig.search(searxng_url="http://searx.lan.example:8080").search("q", 10, engines=["searxng"])
    assert [h["url"] for h in hits] == ["https://public.example/a"] and errors == {}


def test_news_search_uses_the_rss_engines_and_the_freshness_filter():
    rig = Rig(gnews=httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=GNEWS))
    hits, errors = rig.search().search("story", 10, news=True, freshness_days=30)
    assert [h["title"] for h in hits] == ["Fresh story - Paper"] and "bingnews" in errors
    q = parse_qs(rig.requests[0].url.query.decode())
    assert q["q"] == ["story when:30d"] and q["hl"] == ["es"] and q["gl"] == ["ES"] and q["ceid"] == ["ES:es"]
    hits, _ = rig.search().search("story", 10, news=True)
    assert len(hits) == 2


def test_engine_selection_and_empty_queries():
    ws = Rig().search()
    assert ws.available_engines() == ["ddg", "bing"] and ws.available_engines(news=True) == ["gnews", "bingnews"]
    assert Rig().search(searxng_url="http://s", brave_key="k").available_engines() == ["searxng", "ddg", "bing", "brave"]
    assert ws.search("   ") == ([], {"query": "empty query"})
    assert ws.search("q", 5, engines=["nonsense"]) == ([], {})


def test_search_requests_are_paced_per_engine_through_the_shared_fetcher():
    rig = Rig(ddg=html_ok(DDG))
    ws = rig.search(intervals={"ddg": 8.0})
    ws.search("one", 5, engines=["ddg"])
    ws.search("two", 5, engines=["ddg"])
    assert any(s > 0 for s in ws.sleeps)
