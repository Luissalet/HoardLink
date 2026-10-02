// fam-web.js — the app side of the hub's web service, from a Node app (ESM, no dependencies). The twin of
// hoard_link/fam_web.py. Copy it with the other files of `hoard-commons/` (the vendored layout is
// server/hoard-link.js + server/hoard-commons/*.js; here js/hoard-link.js + js/hoard-commons/*.js: the same relative path).
//
// The hub (facet "web") owns ONE polite fetcher for the whole family: per-host spacing that holds across apps, one robots.txt
// cache, block cooldowns that survive restarts, a shared response cache, one optional browser profile, web search and link
// previews. An app asks the hub instead of fetching on its own, and keeps its own fetcher (`webGet` of ./web.js) as the fallback:
//
//   import * as family from "../hoard-link.js";
//   import { webFetchOrLocal, webSearch, webPreview } from "./hoard-commons/fam-web.js";
//   family.configure({ app: "links", dataDir: DATA_DIR });                // the app's token is how the hub knows who asks
//   const r = await webFetchOrLocal(url, { extract: "readable" });         // { ok, status, text, extract, via: "hub" | "local", ... }
//
// Answers are the hub's JSON as it is (snake_case keys, like the mail functions of hoard-link.js). Nothing here throws: no hub is
// `{ ok: false, error: "hub unreachable" }`, a refused token adds `status: 401`, a page that failed is
// `{ ok: false, status: <upstream>, error, error_kind }`. A body fetched with `accept: "any"` arrives decoded in `body`
// (a Buffer, up to 5 MB; bigger ones come with `body_omitted`: use webFetchFile).

import fs from "node:fs";
import * as family from "../hoard-link.js";
import { webGet, htmlToText, quality, excerpt, pageMeta, jsonldBlocks, discoverFeeds, faviconCandidates, parseFeed } from "./web.js";

export const WEB_CACHE_MS = 30_000;
export const WEB_EXTRACTS = Object.freeze(["readable", "markdown", "meta", "jsonld", "feed"]);
const QUEUE_GRACE_MS = 35_000;
const avail = new Map();   // hub url -> { at, ok }

function readText(p) {
  try { return fs.readFileSync(p, "utf8").replace(/^﻿/, "").trim(); } catch { return ""; }
}

function hubUrl() { return String(family.status().hub || "").replace(/\/+$/, ""); }

function headers() {
  const tok = readText(family.status().tokenFile || "");
  return { "Content-Type": "application/json", Accept: "application/json", ...(tok ? { Authorization: `Bearer ${tok}` } : {}) };
}

async function request(method, path, body, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(hubUrl() + path, { method, headers: headers(), body: body === undefined ? undefined : JSON.stringify(body), signal: ctl.signal });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    return { status: res.status, data };
  } catch {
    return { status: null, data: null };
  } finally {
    clearTimeout(timer);
  }
}

function answer(st, data) {
  if (st === null) { avail.delete(hubUrl()); return { ok: false, error: "hub unreachable" }; }
  if (!data || typeof data !== "object") return { ok: st >= 200 && st < 300, status: st, ...(st >= 200 && st < 300 ? {} : { error: `HTTP ${st}` }) };
  if (data.ok === undefined) data.ok = st >= 200 && st < 300;
  if (st === 401) {
    data.error = `the hub refused this app's token (${family.status().tokenFile || "no token file"})`;
    if (data.status === undefined) data.status = 401;
  } else if (st >= 400 && !data.ok && data.tier === undefined && data.status === undefined) {
    data.status = st;       // a hub-level refusal (400, 403, 503...), not a fetch result
  }
  return data;
}

/** True when the hub is up with the web service on and accepts this app's token. Cached 30 s. */
export async function webAvailable(timeoutMs = 1000) {
  const key = hubUrl();
  const hit = avail.get(key);
  if (hit && Date.now() - hit.at < WEB_CACHE_MS) return hit.ok;
  const { status: st, data } = await request("GET", "/api/web/status", undefined, timeoutMs);
  const ok = st === 200 && !!data && data.ok === true && data.enabled !== false;
  avail.set(key, { at: Date.now(), ok });
  return ok;
}

/** Drop the 30-second cache (after changing the hub, or in tests). */
export function webForgetAvailability() { avail.clear(); }

/**
 * Read one URL through the hub. `tier`: auto | http | browser. `accept`: html | json | any (binary: `body` Buffer). `etag` /
 * `lastModified` make it a conditional GET (`not_modified`). `extract`: readable | markdown | meta | jsonld | feed adds an `extract`
 * object. `cacheTtlS` > 0 sets how old a cached copy may be (0 = the hub's own default); `fresh: true` skips the cache.
 * `respectRobots` can only ask for MORE politeness: the hub's setting rules.
 */
export async function webFetch(url, { tier = "auto", accept = "html", etag = "", lastModified = "", respectRobots = true, maxBytes = null,
  timeoutMs = 30000, extract = null, cacheTtlS = 0, fresh = false } = {}) {
  const payload = { url: String(url || ""), tier, accept, respect_robots: Boolean(respectRobots), timeout: timeoutMs / 1000 };
  if (etag) payload.etag = etag;
  if (lastModified) payload.last_modified = lastModified;
  if (maxBytes) payload.max_bytes = maxBytes;
  if (extract) payload.extract = extract;
  if (cacheTtlS && cacheTtlS > 0) payload.cache_ttl_s = Number(cacheTtlS);
  if (fresh) payload.fresh = true;
  const { status: st, data } = await request("POST", "/api/web/fetch", payload, timeoutMs + QUEUE_GRACE_MS);
  const res = answer(st, data);
  if (res.body_b64) { res.body = Buffer.from(res.body_b64, "base64"); delete res.body_b64; }
  return res;
}

/** Download a file through the hub: { ok, path, sha256, content_type, size, filename }; saved in the hub's data/web/files/<app>/ unless `destDir`. */
export async function webFetchFile(url, { destDir = null, maxBytes = 50_000_000, timeoutMs = 120000 } = {}) {
  const payload = { url: String(url || ""), max_bytes: maxBytes, timeout: timeoutMs / 1000 };
  if (destDir) payload.dest_dir = String(destDir);
  const { status: st, data } = await request("POST", "/api/web/fetch_file", payload, timeoutMs + QUEUE_GRACE_MS);
  return answer(st, data);
}

/** Search the web: { ok, hits: [{url, title, snippet, engine, rank, published}], errors: {engine: why}, engines }. */
export async function webSearch(query, { limit = 10, freshnessDays = null, engines = null, news = false, timeoutMs = 90000 } = {}) {
  const payload = { query: String(query || ""), limit, news: Boolean(news) };
  if (freshnessDays) payload.freshness_days = freshnessDays;
  if (engines && engines.length) payload.engines = engines;
  const { status: st, data } = await request("POST", "/api/web/search", payload, timeoutMs);
  return answer(st, data);
}

/** A link card: { ok, title, description, image, favicon, favicons, site_name, canonical, ... }; the hub keeps it 7 days. */
export async function webPreview(url, { timeoutMs = 40000 } = {}) {
  const { status: st, data } = await request("POST", "/api/web/preview", { url: String(url || "") }, timeoutMs);
  return answer(st, data);
}

/** The hub's per-host state: { ok, hosts: [{host, last_status, blocked_now, blocked_until_ts, block_reason, min_interval_s, preferred_tier, last_caller}], blocked }. */
export async function webHostStatus({ timeoutMs = 5000 } = {}) {
  const { status: st, data } = await request("GET", "/api/web/hosts", undefined, timeoutMs);
  return answer(st, data);
}

/** Ask the hub to open the page where a person can solve a challenge or log in. Returns at once: { ok, started, via }. */
export async function webOpen(url, { timeoutMs = 10000 } = {}) {
  const { status: st, data } = await request("POST", "/api/web/open", { url: String(url || "") }, timeoutMs);
  return answer(st, data);
}

// ---------------------------------------------------------------------------------------------- hub first, local second
const snakeKey = (k) => k.replace(/[A-Z]/g, (c) => "_" + c.toLowerCase());
const snakeTop = (o) => Object.fromEntries(Object.entries(o).map(([k, v]) => [snakeKey(k), v]));

/** The extract object built locally (the Node twin has no Markdown converter: ask the hub for `markdown`). */
export function webExtractLocal(kind, text, baseUrl = "") {
  try {
    if (kind === "readable") {
      const { title, text: body } = htmlToText(text);
      return { kind, title, text: body, text_truncated: false, quality: quality(body), excerpt: excerpt(body) };
    }
    if (kind === "meta") return { kind, ...snakeTop(pageMeta(text, baseUrl)), feeds: discoverFeeds(text, baseUrl), favicons: faviconCandidates(text, baseUrl) };
    if (kind === "jsonld") { const [blocks, errors] = jsonldBlocks(text); return { kind, blocks, errors }; }
    if (kind === "feed") {
      const feed = parseFeed(text, baseUrl);
      return feed ? { kind, ...feed } : { kind, error: "the document is not an RSS, Atom or RDF feed" };
    }
    if (kind === "markdown") return { kind, error: "markdown needs the hub (the Node twin has no HTML-to-Markdown converter)" };
  } catch (e) {
    return { kind, error: `${(e && e.name) || "Error"}: ${(e && e.message) || e}`.slice(0, 200) };
  }
  return { kind, error: "unknown extract" };
}

function hubGone(res) {
  if (res.error === "hub unreachable") return true;
  return res.ok === false && res.tier === undefined && (res.status === 401 || res.status === 503);
}

/**
 * webFetch through the hub when it is there, else through `localGet` (default: `webGet` of ./web.js, which honours robots.txt only
 * with respectRobots, and paces per host inside this process only). Same options as webFetch; the answer has the same shape plus
 * `via`: "hub" or "local". A page that failed on the hub (404, blocked, robots) is NOT retried locally.
 */
export async function webFetchOrLocal(url, { localGet = null, ...opts } = {}) {
  if (await webAvailable()) {
    const res = await webFetch(url, opts);
    if (!hubGone(res)) { res.via = "hub"; return res; }
    avail.delete(hubUrl());
  }
  const get = localGet || webGet;
  const { tier = "auto", accept = "html", etag = "", lastModified = "", respectRobots = true, maxBytes = null, timeoutMs = 30000, extract = null } = opts;
  if (tier === "browser" || tier === "window") {
    return { ok: false, url: String(url || ""), error: "the browser tier needs the hub", error_kind: "unavailable", via: "local" };
  }
  let fr;
  try {
    fr = await get(String(url || ""), { accept, etag, lastModified, respectRobots, timeoutMs, ...(maxBytes ? { maxBytes } : {}) });
  } catch (e) {
    return { ok: false, url: String(url || ""), error: `${(e && e.name) || "Error"}: ${(e && e.message) || e}`.slice(0, 300), error_kind: "network", via: "local" };
  }
  const { body, ...rest } = fr;
  const res = {};
  for (const [k, v] of Object.entries(rest)) res[snakeKey(k)] = v;
  res.text_truncated = false;
  if (body) { res.body = body; res.body_size = body.length; }
  if (extract && WEB_EXTRACTS.includes(extract) && res.ok && !res.not_modified && res.text) res.extract = webExtractLocal(extract, res.text, res.final_url || res.url);
  res.via = "local";
  return res;
}
