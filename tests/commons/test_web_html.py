"""blocks, htmltext and meta: Python and the Node twin agree on tests/vectors/web_{blocks,htmltext,meta}.json,
plus the Markdown converter and regressions that only exist on the Python side."""

from __future__ import annotations

import json

import pytest

from hoard_link.web import blocks, htmltext, meta
from tests.commons.jsrun import load_vectors
from tests.commons.web_helpers import case_id, js_results, matches, py_call

SETS = {name: load_vectors(name) for name in ("web_blocks", "web_htmltext", "web_meta")}
ALL = [(name, case) for name, cases in SETS.items() for case in cases]


@pytest.mark.parametrize("name,case", ALL, ids=lambda v: v if isinstance(v, str) else case_id(v))
def test_python(name, case):
    assert matches(case, py_call(case))


@pytest.mark.parametrize("name", list(SETS))
def test_node_twin(name):
    cases = SETS[name]
    got = js_results("web.js", cases)
    bad = [(c["fn"], str(c["args"])[:100], g, c["expect"]) for c, g in zip(cases, got) if not matches(c, g)]
    assert not bad, json.dumps(bad, ensure_ascii=False, indent=1)[:4000]


# ---- blocks -----------------------------------------------------------------------------------

def test_scripts_that_merely_mention_captcha_are_not_blocks():
    page = "<html><body>" + "<p>we mention captcha, akamai and datadome in a long product text</p>" * 2000 + "</body></html>"
    assert blocks.detect_block(200, page, {}, "https://shop.example/p") == ""


def test_a_cloudflare_challenge_served_with_status_200_is_still_a_block():
    page = "<html><head><title>Just a moment...</title></head><body>Checking your browser <script src='/cdn-cgi/challenge-platform/x'></script></body></html>"
    assert blocks.detect_block(200, page, {}, "https://example.com/") == "cloudflare"


def test_apply_block_distinguishes_blocks_from_outages():
    fr = type("FR", (), {})()
    fr.status, fr.text, fr.error, fr.error_kind = 429, "slow", "", ""
    blocks.apply_block(fr, blocks.HTTP_429)
    assert fr.blocked and not fr.ok and fr.error_kind == "blocked" and "429" in fr.error
    fr = type("FR", (), {})()
    fr.status, fr.text, fr.error, fr.error_kind = 503, "down", "", ""
    blocks.apply_block(fr, blocks.HTTP_5XX)
    assert not fr.blocked and fr.error_kind == "http"


# ---- htmltext ---------------------------------------------------------------------------------

def test_deeply_nested_markup_does_not_blow_the_stack():
    html = "<div>" * 5000 + "deep" + "</div>" * 5000
    title, text = htmltext.readable(html)
    assert "deep" in text


def test_parse_html_repairs_misnesting():
    root = htmltext.parse_html("<p>a<b>b<p>c</b>d")
    assert [n.tag for n in root.find_all("p")] == ["p", "p"]


def test_markdown_headings_lists_tables_code_and_links():
    html = """<html><head><title>Doc</title></head><body><nav>menu</nav><main><h1>Title</h1><p>Intro with <a href="/rel">a link</a> and <strong>bold</strong> and <code>code</code>.</p>
    <ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul><ol><li>first</li><li>second</li></ol>
    <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
    <pre><code class="language-python">print("hi")\nx = 1</code></pre></main><footer>foot</footer></body></html>"""
    result = htmltext.to_markdown(html, "https://example.com/dir/")
    md = result["markdown"]
    assert "# Title" in md
    assert "[a link](https://example.com/rel)" in md and "**bold**" in md and "`code`" in md
    assert "- one" in md and "  - nested" in md and "1. first" in md and "2. second" in md
    assert "| A | B |" in md and "| 1 | 2 |" in md
    assert "```python" in md and 'print("hi")' in md
    assert "menu" not in md and "foot" not in md
    assert result["tables"] == 1 and any(h["text"] == "Title" for h in result["headings"])
    assert any(l["url"] == "https://example.com/rel" for l in result["links"])


def test_markdown_groups_consecutive_inline_children_into_one_paragraph():
    md = htmltext.to_markdown("<main><div>Hello <b>big</b> <i>world</i>, <a href='http://x.example/'>link</a>.</div></main>")["markdown"]
    assert md.strip() == "Hello **big** *world*, [link](http://x.example/)."


def test_markdown_drops_javascript_links_and_permalinks():
    md = htmltext.to_markdown("<main><h2>Head<a class='headerlink' href='#head'>¶</a></h2><p><a href='javascript:void(0)'>bad</a> ok</p></main>")["markdown"]
    assert "javascript" not in md and "¶" not in md and "## Head" in md


def test_markdown_to_text_strips_the_markup():
    text = htmltext.markdown_to_text("# T\n\n**bold** and [link](http://x/) and `code`\n\n- item\n\n```py\nprint(1)\n```\n")
    assert "bold and link and code" in text and "item" in text and "#" not in text and "**" not in text


def test_hash_ignores_volatile_noise_but_not_prices():
    base = "Product page\nPrice 19,99 EUR\nIn stock"
    noisy = "Product page\nPrice 19,99 EUR\nIn stock\nUpdated 5 minutes ago\n(c) 2026 Shop\n0123456789abcdef0123"
    assert htmltext.content_hash(base) == htmltext.content_hash(noisy)
    assert htmltext.content_hash(base) != htmltext.content_hash(base.replace("19,99", "17,99"))


# ---- meta -------------------------------------------------------------------------------------

def test_meta_attribute_order_does_not_matter():
    a = meta.page_meta('<meta property="og:title" content="Same">')
    b = meta.page_meta('<meta content="Same" property="og:title">')
    assert a["title"] == b["title"] == "Same"


def test_cooks_trailing_comma_json_ld_is_read():
    html = '<script type="application/ld+json">{"@type":"Recipe","name":"Soup","recipeIngredient":["a","b",],}</script>'
    blocks_, errors = meta.jsonld_blocks(html)
    assert errors == [] and blocks_[0]["recipeIngredient"] == ["a", "b"]
    assert [n["name"] for n in meta.jsonld_nodes(blocks_, ["Recipe"])] == ["Soup"]


def test_trailing_comma_inside_a_string_is_left_alone():
    value, error = meta.load_jsonld('{"name": "a,}", "x": [1,2,]}')
    assert error == "" and value == {"name": "a,}", "x": [1, 2]}


def test_unusable_blocks_are_counted_not_raised():
    html = '<script type="application/ld+json">{{{</script><script type="application/ld+json">{"@type":"X"}</script>'
    payloads, errors = meta.jsonld_blocks(html)
    assert payloads == [{"@type": "X"}] and len(errors) == 1 and errors[0].startswith("block 1:")
