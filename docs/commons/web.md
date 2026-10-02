# Web: URLs, outbound safety, fetching, reading pages, feeds, change detection, search

Everything the family's apps did to reach the web and read what came back, once. Python modules live in
`hoard_link/web/`, the Node twin (no npm dependencies, Node 18+) in `js/hoard-commons/web.js` (ESM). Standard library
only at import time; `httpx` and `playwright` are imported when first used. Rules shared by all commons are in
`docs/COMMONS.md`.

Tests: `tests/commons/test_web_*.py`. Python and Node are checked against the same vectors in `tests/vectors/web_*.json`
(`urls`, `safety`, `blocks`, `htmltext`, `meta`, `feeds`, `watch`, `robots`, `fetch`) and against each other's constant
tables. Nothing needs the network: `httpx.MockTransport`, fake resolvers, and a local `http.server` on `127.0.0.1` with
the `operator_local` profile. One test drives a real headless browser and skips when none is installed.

| Module | Purpose | Node twin (`web.js`) |
|---|---|---|
| `urls` | one normaliser, one tracking list, redirect unwrapping, registrable domain | `normalizeUrl`, `urlKey`, `unwrapRedirect`, `cleanUrl`, `hostOf`, `registrableDomain`, `urljoin` |
| `safety` | SSRF policy, three profiles, DNS pinning | `checkUrl`, `classifyIp`, `resolvePublic`, `pinnedLookup` |
| `blocks` | anti-bot / login-wall detection | `detectBlock`, `blockHint`, `applyBlock` |
| `htmltext` | HTML to text / Markdown, quality gate, noise-free hashes | `htmlToText`, `quality`, `contentHash`, `normaliseForHash`, `excerpt` |
| `meta` | page metadata, JSON-LD, feed links, favicons | `pageMeta`, `jsonldBlocks`, `jsonldNodes`, `discoverFeeds`, `faviconCandidates` |
| `feeds` | RSS / Atom / RDF, GitHub feeds, news RSS | `parseFeed`, `githubFeed`, `toIsoUtc` |
| `watch` | change detection for pages and feeds (no network, no storage) | `checkPage`, `checkFeed`, `diffLines` |
| `robots` | robots.txt rules (wildcards) and cache | `RobotsRules`, `RobotsCache`, `robotsAllowed` |
| `fetch` | the polite fetcher, JSON API client | `webGet`, `politeJson`, `decodeBody` |
| `browser` | optional real Chromium rung, one family profile | - |
| `search` | engine parsers, rank fusion, engine runner | - |

## Safety profiles (`hoard_link.web.safety`)

| Profile | May reach |
|---|---|
| `PUBLIC` (default) | globally routable unicast addresses only |
| `OPERATOR_LOCAL` | public, loopback, private (RFC 1918 / unique-local) and CGNAT (100.64.0.0/10): things the operator configured |
| `INTERNAL` | loopback only: the hub talking to apps on the same machine |

Always refused, in every profile: schemes other than http(s), credentials in the URL, control characters, backslashes,
cloud metadata hosts and addresses (169.254.169.254, fd00:ec2::254 ...), obfuscated IPv4 (`2130706433`, `0x7f.1`,
octal). IPv4-mapped, 6to4, NAT64 and IPv4-compatible IPv6 addresses are judged by the IPv4 inside; Teredo is refused.
The networks are explicit tables (not `ipaddress` properties, whose answers changed between Python versions), the same
in both languages; a test compares them. A name is resolved once, every answer must be allowed, and the connection goes
to the checked address (`pinned_transport` for httpx, `pinnedLookup` for `node:http`), so a hostile DNS server cannot
answer differently the second time. Redirects are checked hop by hop.

## Python API

```python
# urls
normalize_url(url, *, strip_www=False, drop_params=(), keep_params=(), strip_ref=False, ...) -> str   # "" when not http(s)
url_key(url, **opts) -> str
unwrap_redirect(url, *, max_hops=3) -> str
clean_url(url, *, mail=False, keep_fragment=False, max_len=0) -> str
host_of(url) -> str
registrable_domain(host, extra_suffixes=()) -> str | None
is_tracking_param(name, *, extra=()) -> bool

# safety
check_url(url, profile=PUBLIC, resolver=None, *, max_len=...) -> str | None      # None = allowed, else the reason
resolve_public(url, profile=PUBLIC, resolver=None, *, max_len=...) -> list[str]   # raises PolicyError
classify_ip(addr, profile=PUBLIC) -> str | None
pinned_transport(ip, *, verify=True, http2=False) -> httpx transport
parse_loose_ipv4(host) -> IPv4Address | None

# blocks
detect_block(status, text, headers, url) -> str    # cloudflare|akamai|datadome|perimeterx|captcha|login|http_403|http_429|http_5xx|""
block_hint(reason) -> str
apply_block(fetch_result, reason) -> fetch_result

# htmltext
readable(html, *, drop_chrome=True) -> (title, text)
to_markdown(html, base_url="") -> {markdown, title, links, headings, tables}
markdown_to_text(md) -> str
quality(text, *, min_words=40) -> str              # "" = usable, else why not
normalise_for_hash(text) -> str ; content_hash(text) -> str ; excerpt(text, max_chars=300) -> str
parse_html(html) -> Node                           # find / find_all / iter / get / classes

# meta
page_meta(html, base_url="") -> dict               # title description canonical lang site_name author published image favicon keywords og twitter properties
jsonld_blocks(html) -> (payloads, errors) ; jsonld_nodes(blocks, types=None, *, skip=...) -> Iterator[dict]
load_jsonld(raw) -> (payload | None, error)
discover_feeds(html, base_url="") -> list[{url, title, type}] ; favicon_candidates(html, base_url="", *, ico_first=True) -> list[str]

# feeds
parse_feed(xml, base_url="", *, max_items=200) -> {format, title, link, description, items} | None
github_feed(url, what="releases") -> {url, title} | None ; parse_news_rss(xml, engine="gnews") -> list[hit] ; to_iso_utc(value) -> str

# watch
check_page(fetch, prev=None) -> (finding | None, state)           # state: hash, text, etag, last_modified, error
check_feed(feed, seen=None, baseline=False) -> (new_items, seen)
check_feed_result(fetch, seen=None, baseline=False, ...) -> (items, seen, error)
diff_lines(old, new) -> (added, removed) ; diff_summary(added, removed) -> str

# robots
RobotsRules(text).allowed(agent, path) / .crawl_delay(agent) / .sitemaps
RobotsCache(fetch_text, *, store=None, clock=time.time, ttl_s=86400, unreachable_retry_s=600, agent="HoardLink").check(url) -> (allowed, note)

# fetch
Fetcher(*, user_agent, profile=PUBLIC, state=None, transport=None, clock, sleep, resolver, browser=None, browser_profile_dir=None,
        offline=False, cache_dir=None, cache_ttl_s=0.0, stale_if_error=True, min_interval_s=..., timeout_s=20.0, max_bytes=3 MiB,
        max_redirects=5, retries=1, backoff_s=0.6, accept_language, block_cooldown_s, robots_agent, robots_store, pin=None)
Fetcher.get(url, *, tier="auto", headers=None, params=None, accept="html"|"json"|"any", etag="", last_modified="", respect_robots=True,
            min_interval_s=None, timeout=None, max_bytes=None, retries=None, cache_ttl_s=None, profile=None, allowed_mime=None) -> FetchResult
Fetcher.get_json(url, **kw) -> (FetchResult, data) ; host_status() ; clear_block(host) ; reset_host(host) ; open_for_human(url) ; close()
FetchResult: url final_url status ok headers content_type text body etag last_modified not_modified truncated blocked block_reason
             error error_kind note redirects elapsed_s ... ; to_dict(with_text=False)
JsonApiClient(base_url="", user_agent="", min_interval_s=0.0, ttl=21600, offline=False, retries=1, cache_dir=None, ...).get / .get_json
decode_body(body, content_type="") -> str ; classify_error(error, host="", timeout=0.0) -> (kind, detail) ; ERROR_KINDS

# browser (playwright optional)
BrowserRung(profile_dir, *, idle_s=90, headless=True, channel=..., locale="en-US", settle_s=8, profile=PUBLIC, resolver=None, ...)
  .available() -> bool ; .unavailable_reason() -> str ; .fetch(url, settle_s=None, *, timeout_s=25) -> FetchResult
  .fetch_window(url) ; .open_for_human(url, *, timeout_s=900) ; .capture_json(url, match, *, timeout_s=45, offscreen=False) -> (data, error)
  .screenshot(url, path, width=1280, *, full_page=True) ; .browser_session(...) ; .close()
find_chromium() -> str | None ; system_browsers() -> list[str] ; playwright_installed() -> bool ; channel_order(platform=None)

# search
WebSearch(fetcher, *, searxng_url=None, brave_key=None, lang="es", region="ES", intervals=None, clock=time.time)
  .search(query, limit=10, *, freshness_days=None, engines=None, news=False) -> (hits, {engine: error})   # hit: url title snippet engine rank published
  .available_engines(news=False) -> list[str]
rrf_merge(rankings, k=60, weights=None) ; parse_ddg_html / parse_bing_html / parse_searxng_json / parse_brave_json ; unwrap_ddg / unwrap_bing ; is_blocked_page
```

## Node API (`js/hoard-commons/web.js`)

Same names in camelCase; results are camelCase objects (`finalUrl`, `errorKind`, `notModified`, `blockReason`). Option
objects accept camelCase or snake_case keys.

```js
normalizeUrl(url, opts) / urlKey / unwrapRedirect / cleanUrl / hostOf / registrableDomain / urljoin
checkUrl(url, {profile, lookup, hosts, maxLen}) -> Promise<string|null>       // lookup(host, port) -> [ip]; hosts = {name: [ip]} test seam
resolvePublic(url, opts) -> Promise<string[]>   // throws PolicyError (kind "policy" | "dns")
pinnedLookup(addrs) -> lookup function for node:http(s) (honours family and all)
webGet(url, {profile, headers, userAgent, accept, timeoutMs=20000, maxBytes=3 MiB, maxRedirects=5, retries=1, backoffMs=400,
             minIntervalMs=0, etag, lastModified, respectRobots=false, agent, rejectUnauthorized=true}) -> Promise<result>   // never throws
politeJson(url, {minIntervalMs=1000, timeoutMs=10000, maxBytes, profile, headers}) -> Promise<{ok, status, data, headers} | {ok:false, code, error, status}>
   // code: policy | http | timeout | network | too-large | not-json
robotsAllowed(url, {agent, profile, fetchText, clock, ttlMs, cache}) -> Promise<{allowed, note}> ; class RobotsCache ; RobotsRules
detectBlock / blockHint / applyBlock ; htmlToText(html, {dropChrome}) -> {title, text} ; quality / contentHash / normaliseForHash / excerpt
pageMeta / jsonldBlocks / jsonldNodes / discoverFeeds / faviconCandidates / parseFeed / githubFeed / toIsoUtc
checkPage(fetch, prev) -> [finding|null, state] ; checkFeed(feed, seen, baseline) -> [items, seen] ; diffLines / diffSummary
decodeBody(bytes, contentType) ; classifyError(error, host, timeoutMs)
```

Not in the Node twin: `to_markdown`, `search`, `browser` (a Node app that needs them asks the hub's `fam_web`).

## What each module replaces

| Module | Copies it replaces |
|---|---|
| `urls` | Tantalus, Links, JobHunters and Faustus tracking lists and normalisers (they disagreed on `ref`, `www.`, `utm_*` case); Tantalus `unwrap_redirect`; two `registrable_domain` tables |
| `safety` | Tantalus `safety.check_url`, Faustus `reach/ssrf.py`, Links `fetch.js::assertPublic`, the ad hoc `localhost` string checks of about ten apps |
| `blocks` | Tantalus `fetch/blocks.py` and the keyword guesses of Phileas, Cook and JobHunters |
| `htmltext` | Tantalus `extract/text.py`, `info.readable_text` / `quality_gate`; Faustus `html_markdown.py`; Babels `html_to_markdown.py`; regex strippers of Cook and JobHunters |
| `meta` | Tantalus `extract/jsonld.py` and `meta.py`; Faustus `_extract_meta`, `favicon_routes`; Vitruvius `_meta_from_html`; Cook's JSON-LD loader; Links `metaContent`; Writers' meta script |
| `feeds` | Tantalus `info.parse_feed`, `search.parse_news_rss`; Links `watches.js` feed code; Faustus `reach/rss.py` |
| `watch` | Tantalus information sentry, Links page watches (which treated a Cloudflare page as a change) |
| `robots` | `urllib.robotparser` uses (no wildcards) in Tantalus and Faustus |
| `fetch` | Tantalus `fetch/`, Faustus `reach/http.py`, Phileas, Cook, JobHunters, Links `fetch.js` and the pacing of each; Python `JsonApiClient` replaces the small per-API clients |
| `browser` | Tantalus `fetch/browser.py`, Phileas `carriers/browser.py`, Cicero's Chromium finder, the launch chain in Vitruvius |
| `search` | Tantalus `search.py`, Faustus' separate rank fusion |

## Migration notes

* Replace a private `normalize_url` with `hoard_link.web.urls.normalize_url`. It returns `""` (not `None`) for anything that
  is not an http(s) URL. `ref` is kept unless the host is in `REF_TRACKING_HOSTS` or you pass `strip_ref=True`. Mail-only
  parameters (`e`, `cid`, `goal`) are dropped with `clean_url(..., mail=True)` only.
* Replace `check_url` copies with `safety.check_url(url, profile)`. With no resolver it uses the system resolver, so it does
  real DNS; tests pass a resolver or use `Fetcher(resolver=...)`.
* Build one `Fetcher` per process (or use the hub's `fam_web`) and share its `state`: the per-host interval and block
  cooldown only help when every caller goes through the same object. `JsonFileHostState` shares it between processes.
* Anywhere a page was a "change" because it said "checking your browser", use `watch.check_page`: a blocked page never
  becomes a finding and never replaces the stored state.
* `WebSearch.search` returns `(hits, errors)` with dict hits; an engine that fails shows up in `errors` and the others
  still answer.
* `jsonld_blocks` returns `(payloads, errors)`; `jsonld_nodes` is a generator in Python and an array in Node.
* Behind an HTTP proxy configured in the environment, `Fetcher(pin=None)` turns pinning off (a proxy resolves names
  itself); the policy check at each hop still applies. In Node, `webGet` connects directly and ignores proxy variables.
* In Node `webGet` does not read robots.txt unless `respectRobots: true`; the Python `Fetcher` does by default.

## Behaviour worth knowing

* A fetch of an unsafe URL makes no request at all and returns `error_kind="policy"`; a name that does not resolve is
  `dns`.
* A blocked page (anti-bot interstitial, login wall, 403/429 on HTML) comes back `ok=False, blocked=True` with a reason and
  starts a per-host cooldown (30 minutes, or `Retry-After` capped at one hour). `accept="json"` does not treat a 403 as a
  block. `tier="auto"` retries blocked pages in the browser rung when one is configured; nothing tries to defeat a
  challenge.
* `HTML` accept refuses non-text content types (`error_kind="content"`); `accept="any"` returns raw bytes in `body`.
* Compressed bodies are cut at the byte cap while decoding (a 20 MB gzip bomb stops at `max_bytes`, `truncated=True`).
* Atom `updated` and `published` are independent; feeds that declare entities are refused; stray `&nbsp;` and bare `&` are
  repaired once.
* JSON-LD survives `<!-- -->`, `//<![CDATA[`, trailing commas (as one real recipe site emits them), raw control characters and
  HTML-escaped quotes. `<meta>` attribute order does not matter.
