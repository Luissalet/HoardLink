"""hoard_link.web.urls and its Node twin (js/hoard-commons/web.js) agree on tests/vectors/web_urls.json."""

from __future__ import annotations

import json
import subprocess

import pytest

from hoard_link.web import urls
from tests.commons.jsrun import JS_DIR, load_vectors, node
from tests.commons.web_helpers import case_id, js_results, matches, py_call

CASES = load_vectors("web_urls")


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_python(case):
    assert matches(case, py_call(case))


def test_node_twin():
    got = js_results("web.js", CASES)
    bad = [(c["fn"], c["args"], c.get("opts"), g, c["expect"]) for c, g in zip(CASES, got) if not matches(c, g)]
    assert not bad, bad


def test_tables_are_identical_in_both_languages():
    exe = node()
    script = ("import * as m from %s;"
              "process.stdout.write(JSON.stringify({tracking: m.TRACKING_PARAMS, prefixes: m.TRACKING_PREFIXES, mail: m.MAIL_TRACKING_PARAMS,"
              "ref: m.REF_TRACKING_HOSTS, redirect: m.REDIRECT_KEYS, suffixes: m.PUBLIC_SUFFIXES}));" % json.dumps((JS_DIR / "web.js").resolve().as_uri()))
    out = subprocess.run([exe, "--input-type=module", "-e", script], capture_output=True, text=True, check=True).stdout
    js = json.loads(out)
    assert sorted(js["tracking"]) == sorted(urls.TRACKING_PARAMS)
    assert js["prefixes"] == list(urls.TRACKING_PREFIXES)
    assert sorted(js["mail"]) == sorted(urls.MAIL_TRACKING_PARAMS)
    assert js["ref"] == list(urls.REF_TRACKING_HOSTS)
    assert js["redirect"] == list(urls.REDIRECT_KEYS)
    assert sorted(js["suffixes"]) == sorted(urls.PUBLIC_SUFFIXES)


# ---- regressions and behaviours worth pinning down in Python only ---------------------------------

def test_ref_is_kept_where_it_selects_content():
    assert urls.normalize_url("https://github.com/o/r/blob/main/a.py?ref=feature").endswith("?ref=feature")
    assert "ref=" not in urls.normalize_url("https://www.producthunt.com/posts/x?ref=homepage")
    assert "ref=" not in urls.normalize_url("https://example.com/a?ref=nav", strip_ref=True)


def test_union_tracking_list_covers_every_app_list():
    for name in ("fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "twclid", "_hsenc", "mkt_tok", "vero_id",
                 "utm_anything", "ref_src", "spm", "trackingid", "refid"):
        assert urls.is_tracking_param(name), name
    assert not urls.is_tracking_param("page") and not urls.is_tracking_param("q") and not urls.is_tracking_param("e")


def test_site_rules_accept_custom_rules():
    rules = {"shop.example": lambda host, parts: "https://shop.example/item/" + parts.query.split("=")[1]}
    assert urls.normalize_url("https://shop.example/x?id=42", site_rules=rules) == "https://shop.example/item/42"


def test_split_url_round_trip_pieces():
    p = urls.split_url("HTTP://u:p@Host.Example:8080/a/b?x=1#f")
    assert (p.scheme, p.userinfo, p.host, p.port, p.path, p.query, p.fragment) == ("http", "u:p", "Host.Example", "8080", "/a/b", "x=1", "f")
    assert urls.split_url("mailto:a@b.c") is None and urls.split_url("http://[::1/") is None


def test_normalised_urls_are_idempotent():
    for case in CASES:
        if case["fn"] == "normalize_url" and case["expect"] and not case.get("opts"):
            assert urls.normalize_url(case["expect"], site_rules=False) == case["expect"] or case["expect"].startswith("https://www.linkedin.com/jobs/view/")

