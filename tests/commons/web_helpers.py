"""Shared helpers for the ``test_web_*`` modules: shared vectors for Python and the Node twin, a local HTTP server,
and fixture builders. Everything here is network free (the servers listen on 127.0.0.1 only)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from hoard_link.web import blocks, feeds, fetch, htmltext, meta, robots, safety, urls, watch
import subprocess
import tempfile
from pathlib import Path

from tests.commons.jsrun import JS_DIR, camel, camel_keys, load_vectors, node, normalise, run_js  # noqa: F401


def snake(name: str) -> str:
    out = []
    for ch in name:
        out.append("_" + ch.lower() if ch.isupper() else ch)
    return "".join(out)


def snake_top(value: Any) -> Any:
    return {snake(k): v for k, v in value.items()} if isinstance(value, dict) else value


def snake_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {snake(k): snake_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [snake_deep(v) for v in value]
    return value


def resolver_from(hosts: Optional[dict[str, list[str]]]) -> Optional[Callable[[str, int], list[str]]]:
    if hosts is None:
        return None

    def resolve(host: str, port: int) -> list[str]:
        if host not in hosts:
            raise OSError(f"getaddrinfo ENOTFOUND {host}")
        return hosts[host]
    return resolve


# ---- name -> Python callable (the Python twin of each vector function) ------------------------------

def _check_url(url, profile="public", hosts=None, **kw):
    return safety.check_url(url, profile, resolver_from(hosts), **kw)


def _page_meta(html, base_url=""):
    return meta.page_meta(html, base_url)


def _jsonld_blocks(html):
    payloads, errors = meta.jsonld_blocks(html)
    return [payloads, len(errors)]


def _jsonld_nodes(blocks_, types=None, **kw):
    return list(meta.jsonld_nodes(blocks_, types, **kw))


def _html_to_text(html, drop_chrome=True):
    title, text = htmltext.readable(html, drop_chrome=drop_chrome)
    return {"title": title, "text": text}


def _robots_allowed(text, agent, path):
    return robots.RobotsRules(text).allowed(agent, path)


def _robots_crawl_delay(text, agent):
    return robots.RobotsRules(text).crawl_delay(agent)


def _decode_body(data, content_type=""):
    return fetch.decode_body(bytes(data), content_type)


def _check_page(fetch_result, prev=None):
    finding, state = watch.check_page(fetch_result, prev)
    return [finding, state]


def _check_feed(feed, seen=None, baseline=False):
    items, merged = watch.check_feed(feed, seen, baseline)
    return [items, merged]


PY: dict[str, Callable[..., Any]] = {
    "normalize_url": urls.normalize_url, "url_key": urls.url_key, "unwrap_redirect": urls.unwrap_redirect,
    "clean_url": urls.clean_url, "host_of": urls.host_of, "registrable_domain": urls.registrable_domain,
    "classify_ip": safety.classify_ip, "check_url": _check_url,
    "detect_block": blocks.detect_block, "block_hint": blocks.block_hint,
    "html_to_text": _html_to_text, "quality": htmltext.quality, "chrome_ratio": htmltext.chrome_ratio,
    "normalise_for_hash": htmltext.normalise_for_hash, "content_hash": htmltext.content_hash, "excerpt": htmltext.excerpt,
    "page_meta": _page_meta, "jsonld_blocks": _jsonld_blocks, "jsonld_nodes": _jsonld_nodes,
    "discover_feeds": meta.discover_feeds, "favicon_candidates": meta.favicon_candidates,
    "parse_feed": feeds.parse_feed, "github_feed": feeds.github_feed, "to_iso_utc": feeds.to_iso_utc,
    "check_page": _check_page, "check_feed": _check_feed, "diff_lines": lambda a, b: [list(x) for x in watch.diff_lines(a, b)],
    "robots_rules_allowed": _robots_allowed, "robots_crawl_delay": _robots_crawl_delay,
    "decode_body": _decode_body,
}

# How a JS result is brought back to the Python shape before comparing.
JS_POST: dict[str, Callable[[Any], Any]] = {
    "page_meta": snake_top,
    "check_page": snake_deep,
    "jsonld_blocks": lambda v: [v[0], len(v[1])],
}

# Vectors whose Python twin is async-free but whose JS twin is a Promise (run_js awaits those).


def py_call(case: dict[str, Any]) -> Any:
    fn = PY[case["fn"]]
    return normalise(fn(*case.get("args", []), **(case.get("opts") or {})))


def run_js_stdin(module: str, calls: list[dict[str, Any]], *, timeout: float = 120.0) -> list[Any]:
    """Like ``jsrun.run_js`` but the calls travel on stdin (large fixtures do not fit in an argument)."""
    exe = node()
    mod = (JS_DIR / module).resolve().as_uri()
    prepared = [{"fn": camel(c["fn"]), "args": c.get("args", []), "opts": camel_keys(c.get("opts") or {})} for c in calls]
    script = (
        f"import * as m from {json.dumps(mod)};\n"
        "import fs from 'node:fs';\n"
        "const calls = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "const out = [];\n"
        "for (const c of calls) {\n"
        "  try {\n"
        "    const f = m[c.fn]; if (typeof f !== 'function') throw new Error('no function ' + c.fn);\n"
        "    const args = Object.keys(c.opts).length ? [...c.args, c.opts] : c.args;\n"
        "    let r = f(...args); if (r && typeof r.then === 'function') r = await r;\n"
        "    out.push(r === undefined ? null : r);\n"
        "  } catch (e) { out.push({ __error__: String(e && e.message || e) }); }\n"
        "}\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "run.mjs"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run([exe, str(f)], input=json.dumps(prepared), capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def js_results(module_file: str, cases: list[dict[str, Any]]) -> list[Any]:
    got = run_js_stdin(module_file, cases)
    out = []
    for case, value in zip(cases, got):
        post = JS_POST.get(case["fn"])
        out.append(post(value) if post and not (isinstance(value, dict) and "__error__" in value) else value)
    return out


def matches(case: dict[str, Any], value: Any) -> bool:
    """A vector's ``expect`` is the exact result; ``expect_has`` is a substring of a string result (reason texts that
    legitimately differ between Python versions)."""
    if "expect_has" in case:
        return isinstance(value, str) and case["expect_has"] in value
    return value == case["expect"]


def case_id(case: dict[str, Any]) -> str:
    return f"{case['fn']}:{str(case.get('args', ''))[:40]}"


# ---- a tiny local HTTP server for the fetch, browser and Node network tests ------------------------

class Site:
    """``routes``: path -> ``(status, headers, body_bytes)`` or a callable ``(handler) -> (status, headers, body)``.
    Records every request as ``(method, path, headers)`` in ``self.hits``."""

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.hits: list[tuple[str, str, dict[str, str]]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:       # silence
                pass

            def do_GET(self) -> None:                     # noqa: N802
                site.hits.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}))
                route = site.routes.get(self.path.split("?")[0]) or site.routes.get(self.path)
                if route is None:
                    status, headers, body = 404, {"Content-Type": "text/plain"}, b"not found"
                else:
                    status, headers, body = route(self) if callable(route) else route
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                sent = {k.lower() for k in headers}
                for k, v in headers.items():
                    self.send_header(k, v)
                if "content-length" not in sent:
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if body:
                    self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "Site":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> list[str]:
        return [p for _, p, _ in self.hits]


def article(words: int = 80, heading: str = "A long enough article", extra: str = "") -> str:
    """A fixture page with ``words`` words of prose (enough to pass the quality gate)."""
    lorem = ("the quick brown fox jumps over the lazy dog while the committee reviews the quarterly figures and the "
             "weather stays calm across the valley").split()
    body = " ".join(lorem[i % len(lorem)] for i in range(words))
    return (f"<html><head><title>{heading}</title></head><body><nav><a href='/'>Home</a> <a href='/cart'>Cart</a></nav>"
            f"<main><h1>{heading}</h1><p>{body}.</p>{extra}</main><footer>(c) 2026 Example</footer></body></html>")


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)
