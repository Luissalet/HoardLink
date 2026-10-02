"""The Node twin's network half (webGet, robotsAllowed, politeJson, checkUrl, pinnedLookup) against a local
http.server on 127.0.0.1 with the operator_local profile. Name resolution goes through the ``hosts`` test seam."""

from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
import textwrap
import zlib
from pathlib import Path

import pytest

from tests.commons.jsrun import JS_DIR, node
from tests.commons.web_helpers import Site, article

pytestmark = pytest.mark.skipif(not __import__("shutil").which("node"), reason="node is not installed")

WEB = (JS_DIR / "web.js").resolve().as_uri()


def run(body: str, data: dict | None = None, timeout: float = 60.0):
    """Run an ES module snippet with ``w`` (web.js) and ``D`` (the JSON data) in scope; the snippet ``return``s the result."""
    script = (f"import * as w from {json.dumps(WEB)};\nimport fs from 'node:fs';\n"
              "const D = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
              "const main = async () => {\n" + textwrap.indent(textwrap.dedent(body), "  ") + "\n};\n"
              "const out = await main();\nprocess.stdout.write(JSON.stringify(out === undefined ? null : out));\n")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "run.mjs"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run([node(), str(f)], input=json.dumps(data or {}), capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


LOCAL = {"profile": "operator_local"}


def get(url: str, **opts):
    return run("return await w.webGet(D.url, D.opts);", {"url": url, "opts": {**LOCAL, "retries": 0, **opts}})


# ---- webGet ----------------------------------------------------------------------------------

def test_plain_get_decodes_and_reports():
    page = article(80, "Hello")
    with Site({"/p": (200, {"Content-Type": "text/html; charset=utf-8", "ETag": '"v1"'}, page)}) as s:
        r = get(s.base + "/p")
        assert r["ok"] and r["status"] == 200 and "Hello" in r["text"] and r["etag"] == '"v1"' and not r["blocked"]
        assert r["errorKind"] == "" and r["finalUrl"] == s.base + "/p"
        assert s.hits[0][2]["user-agent"].startswith("Mozilla/5.0") and "gzip" in s.hits[0][2]["accept-encoding"]


def test_public_profile_refuses_loopback_without_a_request():
    with Site({"/p": (200, {}, "x")}) as s:
        r = run("return await w.webGet(D.url, {retries: 0});", {"url": s.base + "/p"})
        assert not r["ok"] and r["errorKind"] == "policy" and s.hits == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://127.0.0.1/x", "http://user:pw@127.0.0.1/x", "http://169.254.169.254/latest/meta-data",
                                 "http://0x7f.1/", "http://2130706433/", "http://[::ffff:169.254.169.254]/"])
def test_dangerous_urls_are_refused_under_operator_local(url):
    r = get(url)
    assert not r["ok"] and r["errorKind"] == "policy" and r["status"] == 0


def test_redirects_are_checked_hop_by_hop():
    with Site({"/meta": (302, {"Location": "http://169.254.169.254/latest/meta-data"}, ""),
               "/file": (302, {"Location": "file:///etc/passwd"}, ""),
               "/loop": (302, {"Location": "/loop"}, ""),
               "/ok": (302, {"Location": "/final"}, ""), "/final": (200, {"Content-Type": "text/plain"}, "done")}) as s:
        r = get(s.base + "/meta")
        assert not r["ok"] and r["errorKind"] == "policy" and r["error"].startswith("redirect refused") and r["finalUrl"].startswith("http://169.254.169.254")
        r = get(s.base + "/file")
        assert not r["ok"] and r["errorKind"] == "policy" and r["error"].startswith("redirect refused")
        r = get(s.base + "/loop", maxRedirects=3)
        assert r["errorKind"] == "redirects" and len(r["redirects"]) == 4
        r = get(s.base + "/ok", accept="any")
        assert r["ok"] and r["text"] == "done" and r["redirects"] == [s.base + "/final"] and r["finalUrl"] == s.base + "/final"


def test_a_redirect_to_a_non_allowed_address_is_refused_under_the_internal_profile():
    with Site({"/r": (302, {"Location": "http://10.0.0.5/admin"}, "")}) as s:
        r = run("return await w.webGet(D.url, D.opts);", {"url": s.base + "/r", "opts": {"profile": "internal", "retries": 0}})
        assert r["errorKind"] == "policy" and r["error"].startswith("redirect refused") and r["finalUrl"] == "http://10.0.0.5/admin"


def test_sensitive_headers_are_dropped_on_a_cross_host_redirect_only():
    with Site({"/same": (302, {"Location": "/echo"}, ""), "/echo": (200, {"Content-Type": "text/plain"}, "ok"),
               "/cross": lambda h: (302, {"Location": f"http://other.test:{h.server.server_address[1]}/echo"}, "")}) as s:
        hosts = {"127.0.0.1": ["127.0.0.1"], "other.test": ["127.0.0.1"]}
        headers = {"Authorization": "Bearer secret", "Cookie": "a=b", "X-Trace": "keep"}
        get(s.base + "/same", headers=headers, hosts=hosts)
        same = s.hits[-1][2]
        assert same.get("authorization") == "Bearer secret" and same.get("cookie") == "a=b"
        s.hits.clear()
        r = get(s.base + "/cross", headers=headers, hosts=hosts)
        assert r["ok"]
        last = s.hits[-1][2]
        assert "authorization" not in last and "cookie" not in last and last.get("x-trace") == "keep"
        assert last["host"].startswith("other.test:")


def test_body_cap_truncates_and_marks():
    with Site({"/big": (200, {"Content-Type": "text/plain"}, b"x" * 50_000)}) as s:
        r = get(s.base + "/big", maxBytes=1000, accept="any")
        assert r["truncated"] is True and len(r["text"]) <= 1000


@pytest.mark.parametrize("enc", ["gzip", "deflate"])
def test_compressed_bodies_are_decoded(enc):
    raw = ("<html><body>" + "café " * 30 + "</body></html>").encode("utf-8")
    body = gzip.compress(raw) if enc == "gzip" else zlib.compress(raw)
    with Site({"/z": (200, {"Content-Type": "text/html; charset=utf-8", "Content-Encoding": enc}, body)}) as s:
        r = get(s.base + "/z")
        assert r["ok"] and "café café" in r["text"]


def test_a_decompression_bomb_is_cut_at_the_cap():
    bomb = gzip.compress(b"a" * 20_000_000)
    with Site({"/bomb": (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, bomb)}) as s:
        r = get(s.base + "/bomb", maxBytes=100_000, accept="any")
        assert r["truncated"] is True and len(r["text"]) <= 100_000


def test_brotli_is_decoded_when_node_can_do_it():
    import shutil
    if not shutil.which("node"):
        pytest.skip("node")
    can = run("const zlib = await import('node:zlib'); return typeof zlib.brotliCompressSync === 'function' ? zlib.brotliCompressSync(Buffer.from('<html><body>brotli ok ok ok ok ok ok</body></html>')).toString('base64') : null;")
    if not can:
        pytest.skip("no brotli in this node")
    import base64
    with Site({"/b": (200, {"Content-Type": "text/html", "Content-Encoding": "br"}, base64.b64decode(can))}) as s:
        assert "brotli ok" in get(s.base + "/b")["text"]


def test_conditional_get_and_304():
    def route(h):
        if h.headers.get("If-None-Match") == '"v1"':
            return 304, {"ETag": '"v1"'}, b""
        return 200, {"Content-Type": "text/html", "ETag": '"v1"'}, article(60)
    with Site({"/c": route}) as s:
        first = get(s.base + "/c")
        again = get(s.base + "/c", etag=first["etag"])
        assert again["ok"] and again["notModified"] is True and again["text"] == "" and again["status"] == 304


def test_charset_from_header_and_from_a_meta_tag():
    latin = "<html><body><p>caf\xe9 cr\xe8me</p></body></html>".encode("latin-1")
    cp = "<html><body><p>“quoted”</p></body></html>".encode("cp1252")
    with Site({"/h": (200, {"Content-Type": "text/html; charset=iso-8859-1"}, latin),
               "/m": (200, {"Content-Type": "text/html"}, b'<html><head><meta charset="windows-1252"></head><body>' + "“quoted”".encode("cp1252") + b"</body></html>"),
               "/n": (200, {"Content-Type": "text/html"}, cp)}) as s:
        assert "café crème" in get(s.base + "/h")["text"]
        assert "“quoted”" in get(s.base + "/m")["text"]


def test_http_errors_and_block_pages():
    cf = ("<html><head><title>Just a moment...</title></head><body><p>Checking your browser before accessing</p>"
          "<script src='/cdn-cgi/challenge-platform/h/b'></script></body></html>")
    with Site({"/404": (404, {"Content-Type": "text/html"}, "<html><body>gone</body></html>"),
               "/429": (429, {"Content-Type": "text/html", "Retry-After": "7"}, "slow down"),
               "/cf": (200, {"Content-Type": "text/html"}, cf)}) as s:
        r = get(s.base + "/404")
        assert not r["ok"] and r["status"] == 404 and r["errorKind"] == "http"
        r = get(s.base + "/429")
        assert not r["ok"] and r["blocked"] and r["blockReason"]
        r = get(s.base + "/cf")
        assert not r["ok"] and r["blocked"] and "Cloudflare" in r["error"]


def test_json_accept_does_not_treat_a_403_as_a_block():
    with Site({"/j": (403, {"Content-Type": "application/json"}, '{"message":"rate limited"}')}) as s:
        r = get(s.base + "/j", accept="json")
        assert not r["ok"] and r["status"] == 403 and not r["blocked"]


def test_wrong_content_type_for_html_is_an_error():
    with Site({"/pdf": (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 binary")}) as s:
        r = get(s.base + "/pdf")
        assert not r["ok"] and r["errorKind"] == "content"
        r = get(s.base + "/pdf", accept="any")
        assert r["ok"] and r["body"]


def test_5xx_is_retried_and_connection_refused_is_classified():
    state = {"n": 0}

    def flaky(h):
        state["n"] += 1
        return (503, {"Content-Type": "text/plain"}, "busy") if state["n"] == 1 else (200, {"Content-Type": "text/plain"}, "fine")
    with Site({"/f": flaky}) as s:
        r = get(s.base + "/f", retries=1, backoffMs=10, accept="any")
        assert r["ok"] and r["text"] == "fine" and state["n"] == 2
    with Site({}) as dead:
        base = dead.base
    r = get(base + "/x", retries=0)
    assert r["errorKind"] in ("refused", "network") and not r["ok"]


def test_timeout_is_reported_as_timeout():
    import time

    def slow(h):
        time.sleep(1.5)
        return 200, {}, "late"
    with Site({"/s": slow}) as s:
        r = get(s.base + "/s", timeoutMs=300)
        assert r["errorKind"] == "timeout" and not r["ok"]


def test_per_host_interval_spaces_requests():
    with Site({"/a": (200, {"Content-Type": "text/plain"}, "a")}) as s:
        res = run("""
            const t = Date.now();
            await Promise.all([w.webGet(D.url, D.opts), w.webGet(D.url, D.opts), w.webGet(D.url, D.opts)]);
            return Date.now() - t;
        """, {"url": s.base + "/a", "opts": {**LOCAL, "retries": 0, "minIntervalMs": 250, "accept": "any"}})
        assert res >= 480 and len(s.hits) == 3


def test_robots_are_honoured_when_asked():
    with Site({"/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /private\n"),
               "/private/x": (200, {"Content-Type": "text/plain"}, "secret"), "/open": (200, {"Content-Type": "text/plain"}, "open")}) as s:
        out = run("""
            const a = await w.webGet(D.base + '/private/x', D.opts);
            const b = await w.webGet(D.base + '/open', D.opts);
            return [a, b];
        """, {"base": s.base, "opts": {**LOCAL, "retries": 0, "respectRobots": True, "accept": "any"}})
        assert out[0]["errorKind"] == "robots" and not out[0]["ok"] and out[1]["ok"]
        assert "/private/x" not in s.paths()


# ---- robots, politeJson, checkUrl, pinnedLookup ---------------------------------------------

def test_robots_allowed_with_an_injected_fetcher_and_a_clock():
    out = run("""
        let now = 1000, calls = 0;
        const cache = new w.RobotsCache({
          fetchText: async (u) => { calls++; return calls === 1 ? {status: 503, text: '', error: 'HTTP 503'} : {status: 200, text: 'User-agent: *\\nDisallow: /x\\n'}; },
          clock: () => now });
        const r = [];
        r.push(await cache.check('https://a.example/x'));
        r.push(await cache.check('https://a.example/x'));
        now += 11 * 60 * 1000;
        r.push(await cache.check('https://a.example/x'));
        r.push(await cache.check('https://a.example/y'));
        r.push(await w.robotsAllowed('https://b.example/x', {fetchText: async () => ({status: 404, text: ''})}));
        return [r, calls];
    """)
    r, calls = out
    assert r[0]["allowed"] and "unreachable" in r[0]["note"] and r[1]["allowed"] and calls == 2
    assert r[2] == {"allowed": False, "note": ""} and r[3]["allowed"] is True and r[4] == {"allowed": True, "note": ""}


def test_robots_cache_really_fetches_robots_txt_over_http():
    with Site({"/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: HoardLink\nDisallow: /no\n")}) as s:
        out = run("""
            const c = new w.RobotsCache({profile: 'operator_local'});
            return [await c.check(D.base + '/no/way'), await c.check(D.base + '/yes')];
        """, {"base": s.base})
        assert out[0]["allowed"] is False and out[1]["allowed"] is True and s.paths().count("/robots.txt") == 1


def test_polite_json_codes():
    with Site({"/ok": (200, {"Content-Type": "application/json"}, '{"a": [1, 2]}'),
               "/html": (200, {"Content-Type": "text/html"}, "<html>no</html>"),
               "/500": (500, {"Content-Type": "text/plain", "Retry-After": "3"}, "oops"),
               "/redir": (302, {"Location": "/ok"}, ""),
               "/big": (200, {"Content-Type": "application/json"}, b'{"x":"' + b"a" * 5000 + b'"}')}) as s:
        out = run("""
            const o = {profile: 'operator_local', minIntervalMs: 0};
            return {
              ok: await w.politeJson(D.b + '/ok', o), nj: await w.politeJson(D.b + '/html', o), http: await w.politeJson(D.b + '/500', o),
              redir: await w.politeJson(D.b + '/redir', o), big: await w.politeJson(D.b + '/big', {...o, maxBytes: 100}),
              pol: await w.politeJson('file:///etc/passwd', o), pub: await w.politeJson(D.b + '/ok', {minIntervalMs: 0}),
              refused: await w.politeJson(D.dead + '/x', o),
            };
        """, {"b": s.base, "dead": "http://127.0.0.1:9"})
        assert out["ok"]["ok"] and out["ok"]["data"] == {"a": [1, 2]} and out["ok"]["status"] == 200
        assert out["nj"]["code"] == "not-json" and out["http"]["code"] == "http" and out["http"]["status"] == 500
        assert out["redir"]["code"] == "http" and "not followed" in out["redir"]["error"]
        assert out["big"]["code"] == "too-large"
        assert out["pol"]["code"] == "policy" and out["pub"]["code"] == "policy"
        assert out["refused"]["code"] == "network"


def test_polite_json_paces_a_host():
    with Site({"/ok": (200, {"Content-Type": "application/json"}, "{}")}) as s:
        elapsed = run("""
            const t = Date.now(), o = {profile: 'operator_local', minIntervalMs: 300};
            await Promise.all([w.politeJson(D.u, o), w.politeJson(D.u, o), w.politeJson(D.u, o)]);
            return Date.now() - t;
        """, {"u": s.base + "/ok"})
        assert elapsed >= 550 and len(s.hits) == 3


def test_check_url_with_the_hosts_seam_and_profiles():
    out = run("""
        const hosts = {'pub.test': ['93.184.216.34'], 'lan.test': ['192.168.1.20'], 'cg.test': ['100.64.0.9'], 'meta.test': ['169.254.169.254'],
                       'mixed.test': ['93.184.216.34', '10.0.0.1'], 'lo.test': ['127.0.0.1']};
        const r = {};
        for (const [name, url] of Object.entries({pub: 'http://pub.test/', lan: 'http://lan.test/', cg: 'http://cg.test/', meta: 'http://meta.test/',
                                                  mixed: 'http://mixed.test/', lo: 'http://lo.test/', nx: 'http://nx.test/'})) {
          r[name] = {};
          for (const profile of ['public', 'operator_local', 'internal']) {
            r[name][profile] = (await w.checkUrl(url, {profile, hosts})) || 'ok';
          }
        }
        return r;
    """)
    assert out["pub"]["public"] == "ok" and out["pub"]["operator_local"] == "ok" and out["pub"]["internal"] != "ok"
    assert out["lan"]["public"] != "ok" and out["lan"]["operator_local"] == "ok"
    assert out["cg"]["public"] != "ok" and out["cg"]["operator_local"] == "ok"                    # CGNAT is the operator's own network
    assert all(v != "ok" for v in out["meta"].values())
    assert out["mixed"]["public"] != "ok"                                                         # one private answer poisons the name
    assert out["lo"]["operator_local"] == "ok" and out["lo"]["internal"] == "ok" and out["lo"]["public"] != "ok"
    assert all(v != "ok" for v in out["nx"].values())


def test_pinned_lookup_answers_only_with_the_vetted_addresses():
    out = run("""
        const lookup = w.pinnedLookup(['127.0.0.1', '::1']);
        const one = await new Promise((res) => lookup('anything.example', {}, (e, a, f) => res([e && e.message, a, f])));
        const all = await new Promise((res) => lookup('anything.example', {all: true}, (e, a) => res([e && e.message, a])));
        const v6 = await new Promise((res) => lookup('anything.example', {family: 6}, (e, a, f) => res([e && e.message, a, f])));
        return {one, all, v6};
    """)
    assert out["one"][1] == "127.0.0.1" and out["one"][2] == 4
    assert [a["address"] for a in out["all"][1]] == ["127.0.0.1", "::1"]
    assert out["v6"][1] == "::1" and out["v6"][2] == 6


def test_pinned_lookup_refuses_a_family_it_does_not_hold():
    out = run("""
        const lookup = w.pinnedLookup(['127.0.0.1']);
        return await new Promise((res) => lookup('x.example', {family: 6}, (e) => res(e && e.code)));
    """)
    assert out == "ENOTFOUND"
