"""feeds, watch, robots and decode_body: Python and the Node twin agree on tests/vectors/web_{feeds,watch,robots,fetch}.json,
plus the behaviours that only exist in Python (news RSS, the robots cache, state machines over several checks)."""

from __future__ import annotations

import json

import pytest

from hoard_link.web import feeds, robots, watch
from tests.commons.jsrun import load_vectors
from tests.commons.web_helpers import article, case_id, js_results, matches, py_call

SETS = {name: load_vectors(name) for name in ("web_feeds", "web_watch", "web_robots", "web_fetch")}
ALL = [(name, case) for name, cases in SETS.items() for case in cases]


@pytest.mark.parametrize("name,case", ALL, ids=lambda v: v if isinstance(v, str) else case_id(v))
def test_python(name, case):
    assert matches(case, py_call(case))


@pytest.mark.parametrize("name", list(SETS))
def test_node_twin(name):
    cases = SETS[name]
    got = js_results("web.js", cases)
    bad = [(c["fn"], str(c["args"])[:120], g, c["expect"]) for c, g in zip(cases, got) if not matches(c, g)]
    assert not bad, json.dumps(bad, ensure_ascii=False, indent=1)[:4000]


# ---- feeds ------------------------------------------------------------------------------------

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>
<entry><id>1</id><title>Only updated</title><link href="http://a.example/1"/><updated>2026-09-03T08:15:00Z</updated></entry>
<entry><id>2</id><title>Both</title><link href="http://a.example/2"/><published>2026-09-01T00:00:00Z</published><updated>2026-09-02T00:00:00Z</updated></entry></feed>"""


def test_atom_updated_is_not_dropped_when_published_is_missing():
    """The old reader tested ``published or updated`` on Elements: a text-only element is falsy, so the date vanished."""
    items = feeds.parse_feed(ATOM)["items"]
    assert items[0]["updated"] == "2026-09-03T08:15:00Z" and items[0]["date"] == "2026-09-03T08:15:00Z" and items[0]["published"] == ""
    assert items[1]["published"] == "2026-09-01T00:00:00Z" and items[1]["updated"] == "2026-09-02T00:00:00Z"


def test_declared_entities_are_refused():
    bomb = '<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><rss><channel><title>&b;</title></channel></rss>'
    assert feeds.parse_feed(bomb) is None
    assert feeds.parse_feed(bomb.encode("utf-8")) is None


def test_parse_feed_accepts_bytes_and_a_bom():
    xml = '<rss><channel><title>Ü</title><item><title>a</title><link>http://a.example/1</link></item></channel></rss>'
    assert feeds.parse_feed(("﻿" + xml).encode("utf-8"))["title"] == "Ü"


GNEWS = """<rss><channel><title>q</title>
<item><title>Big story - The Paper</title><link>https://news.google.com/rss/articles/abc</link><pubDate>Thu, 01 Oct 2026 08:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Big story&lt;/a&gt;&amp;nbsp;&amp;nbsp;The Paper</description><source url="https://thepaper.example">The Paper</source></item>
<item><title>No link</title></item><item><title>Second - Daily</title><link>https://news.google.com/rss/articles/def</link><source url="https://daily.example">Daily</source></item></channel></rss>"""
BING = """<rss><channel><item><title>Bing story</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=1&amp;url=https%3a%2f%2fsite.example%2fa%3fid%3d1&amp;c=2</link>
<description>Snippet text</description><pubDate>Fri, 02 Oct 2026 09:30:00 GMT</pubDate></item></channel></rss>"""


def test_parse_news_rss_for_both_engines():
    hits = feeds.parse_news_rss(GNEWS, "gnews")
    assert [h["rank"] for h in hits] == [1, 3] and hits[0]["engine"] == "gnews"
    assert hits[0]["published"] == "2026-10-01T08:00:00Z" and "[https://thepaper.example]" in hits[0]["snippet"]
    bing = feeds.parse_news_rss(BING, "bingnews")
    assert bing[0]["url"] == "https://site.example/a?id=1" and bing[0]["snippet"] == "Snippet text"
    assert feeds.parse_news_rss("not xml") == []


# ---- watch: state over several checks ---------------------------------------------------------

def _fetch(html, **kw):
    d = {"url": "https://shop.example/p", "final_url": "https://shop.example/p", "status": 200, "ok": True, "text": html, "headers": {}}
    d.update(kw)
    return d


CLOUDFLARE = ("<html><head><title>Just a moment...</title></head><body><p>Checking your browser before accessing shop.example.</p>"
              "<script src='/cdn-cgi/challenge-platform/h/b/x'></script></body></html>")


def test_a_cloudflare_page_is_never_a_change_and_never_replaces_the_state():
    page = article(80, "Offers", "<ul><li>Alpha 10 EUR</li><li>Beta 20 EUR</li></ul>")
    finding, state = watch.check_page(_fetch(page), None)
    assert finding is None and state["hash"]                                   # baseline
    finding, after = watch.check_page(_fetch(CLOUDFLARE, status=200, ok=True), state)
    assert finding is None and after["hash"] == state["hash"] and after["text"] == state["text"]
    assert after["error"] == "blocked: Cloudflare challenge page"
    finding, again = watch.check_page(_fetch(page), after)                       # the real page comes back: still no change
    assert finding is None and again["hash"] == state["hash"] and again["error"] == ""


def test_a_cloudflare_page_is_not_even_a_baseline():
    finding, state = watch.check_page(_fetch(CLOUDFLARE), None)
    assert finding is None and state["hash"] == "" and state["text"] == ""


def test_a_real_change_after_a_block_is_still_reported():
    a = article(80, "Offers", "<ul><li>Alpha 10 EUR</li></ul>")
    b = article(80, "Offers", "<ul><li>Alpha 12 EUR</li></ul>")
    _, s1 = watch.check_page(_fetch(a), None)
    _, s2 = watch.check_page(_fetch(CLOUDFLARE), s1)
    finding, s3 = watch.check_page(_fetch(b), s2)
    assert finding and finding["added"] == ["Alpha 12 EUR"] and finding["removed"] == ["Alpha 10 EUR"]
    assert watch.check_page(_fetch(b), s3)[0] is None


def test_check_page_accepts_a_fetch_result_object():
    from hoard_link.web.fetch import FetchResult
    page = article(80, "Offers", "<p>x</p>")
    fr = FetchResult(url="https://shop.example/p", final_url="https://shop.example/p", status=200, text=page, ok=True, etag="e1")
    finding, state = watch.check_page(fr, None)
    assert finding is None and state["etag"] == "e1"
    fr.not_modified, fr.text = True, ""
    assert watch.check_page(fr, state) == (None, state)


def test_feed_checks_report_only_new_ids_and_remember_the_rest():
    items = [{"id": str(i), "title": f"t{i}", "link": f"http://a.example/{i}"} for i in range(1, 4)]
    new, seen = watch.check_feed({"items": items}, [], baseline=True)
    assert new == [] and seen == ["1", "2", "3"]
    items.insert(0, {"id": "4", "title": "t4", "link": "http://a.example/4"})
    new, seen = watch.check_feed({"items": items}, seen)
    assert [i["id"] for i in new] == ["4"] and seen[0] == "4" and len(seen) == 4


def test_feed_seen_list_is_capped():
    items = [{"id": f"n{i}"} for i in range(30)]
    new, seen = watch.check_feed({"items": items}, [f"old{i}" for i in range(500)])
    assert len(new) == watch.MAX_FEED_FINDINGS and len(seen) == watch.MAX_SEEN


def test_check_feed_result_covers_failure_modes():
    ok = {"ok": True, "text": '<rss><channel><item><title>a</title><link>http://a.example/1</link></item></channel></rss>', "final_url": "http://a.example/f"}
    items, seen, error = watch.check_feed_result(ok, [], baseline=False)
    assert len(items) == 1 and not error
    assert watch.check_feed_result({"not_modified": True}, ["x"]) == ([], ["x"], "")
    assert watch.check_feed_result({"blocked": True, "block_reason": "cloudflare"}, ["x"])[2].startswith("blocked:")
    assert watch.check_feed_result({"ok": True, "text": "<html></html>"}, ["x"])[2] == "not a feed or empty"
    assert watch.check_feed_result({"ok": False, "error": "boom"}, ["x"])[2] == "boom"


# ---- robots cache -----------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _cache(responses, clock=None, **kw):
    calls = []

    def fetch_text(url):
        calls.append(url)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        return item

    return robots.RobotsCache(fetch_text, clock=clock or Clock(), **kw), calls


def test_robots_cache_fetches_once_per_origin_and_ttl():
    clock = Clock()
    cache, calls = _cache([(200, "User-agent: *\nDisallow: /private\n", "")], clock)
    assert cache.check("https://a.example/ok") == (True, "")
    assert cache.check("https://a.example/private/x") == (False, "")
    assert cache.check("https://a.example:8443/private/x") == (False, "") and len(calls) == 2     # a different origin is another file
    assert calls[0] == "https://a.example/robots.txt"
    n = len(calls)
    cache.check("https://a.example/again")
    assert len(calls) == n
    clock.now += robots.ROBOTS_TTL_S + 1
    cache.check("https://a.example/again")
    assert len(calls) == n + 1


def test_robots_4xx_means_no_robots_and_5xx_means_try_again_later():
    clock = Clock()
    cache, calls = _cache([(404, "", "")], clock)
    assert cache.check("https://a.example/x") == (True, "") and cache.check("https://a.example/y") == (True, "") and len(calls) == 1
    cache, calls = _cache([(503, "", "HTTP 503"), (200, "User-agent: *\nDisallow: /\n", "")], clock)
    allowed, note = cache.check("https://b.example/x")
    assert allowed and "unreachable" in note
    assert cache.check("https://b.example/x")[0] is True and len(calls) == 1          # still inside the retry window
    clock.now += 11 * 60
    assert cache.check("https://b.example/x") == (False, "") and len(calls) == 2


def test_robots_429_and_network_errors_are_not_treated_as_no_robots():
    cache, _ = _cache([(429, "", "")])
    assert "unreachable" in cache.check("https://c.example/x")[1]
    cache, _ = _cache([(0, "", "Timeout: no answer")])
    assert "Timeout" in cache.check("https://d.example/x")[1]


def test_robots_store_is_pluggable_and_survives_a_new_cache():
    store: dict = {}
    clock = Clock()
    cache, _ = _cache([(200, "User-agent: *\nDisallow: /x\n", "")], clock, store=store)
    cache.check("https://e.example/y")
    cache2, calls2 = _cache([(500, "", "boom")], clock, store=store)
    assert cache2.check("https://e.example/x") == (False, "") and calls2 == []


def test_robots_crawl_delay_and_raw_and_forget():
    cache, calls = _cache([(200, "User-agent: *\nCrawl-delay: 4\n", "")])
    assert cache.crawl_delay("https://f.example/") == 4.0
    assert "Crawl-delay" in cache.raw("https://f.example/")
    cache.forget("https://f.example/")
    assert cache.raw("https://f.example/") is None
    cache.check("https://f.example/")
    assert len(calls) == 2


def test_robots_pattern_matching_is_not_a_prefix_only_match():
    rules = robots.RobotsRules("User-agent: *\nDisallow: /*.pdf$\nDisallow: /a/*/b\n")
    assert not rules.allowed("x", "/files/report.pdf") and rules.allowed("x", "/files/report.pdf?x=1")
    assert not rules.allowed("x", "/a/zzz/b") and rules.allowed("x", "/a/zzz/c")
    assert rules.sitemaps == []
    assert robots.RobotsRules("Sitemap: https://x/s.xml\n").sitemaps == ["https://x/s.xml"]
