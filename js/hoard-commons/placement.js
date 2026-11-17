// placement.js — where the family's movable apps answer right now (the Node twin of hoard_link/where.py).
//
// The Hub's "Run on" control moves Faustus, Hermes and Prometheus to a Spark so the PC can be switched off. An Express
// Hoard that keeps calling http://127.0.0.1:7000 then reaches nothing. These helpers ask the Hub (GET /api/placement),
// remember the answer for a few seconds and add the placed address after the configured ones, so an app that is off on
// this PC (it refuses at once) is found on the Spark, and nothing changes when the Hub is silent or the app is on the PC.
//
//   import { faustusCandidates } from "./hoard-commons/placement.js";
//   for (const base of await faustusCandidates(["http://127.0.0.1:7000"])) { ... }
//
// ESM, Node 18+, no npm dependencies.

import fs from "node:fs";

export const DEFAULT_HUB_URL = "http://127.0.0.1:8810";
const FRESH_MS = 15_000;      // a good answer is reused this long
const SILENT_MS = 10_000;     // a silent Hub is not asked again for this long (the reserve is used meanwhile)
const TIMEOUT_MS = 1_500;
const cache = new Map();      // hub url -> { until, data, pending }

const clean = (url) => String(url ?? "").trim().replace(/\/+$/, "");

/** The Hub address: explicit, else HOARD_HUB_URL, else backend.json `gpu_lease.hub_url`, else the default. */
export function hubUrl({ hub = "", env = process.env, configPath = null } = {}) {
  if (hub) return clean(hub);
  const fromEnv = [env?.HOARD_HUB_URL, process.env.HOARD_HUB_URL].find((v) => typeof v === "string" && v.trim())?.trim() || "";
  if (fromEnv) return clean(fromEnv);
  if (configPath) {
    try {
      const raw = JSON.parse(fs.readFileSync(configPath, "utf8").replace(/^﻿/, ""));
      const fromFile = raw?.gpu_lease?.hub_url;
      if (typeof fromFile === "string" && fromFile.trim()) return clean(fromFile);
    } catch { /* no config file */ }
  }
  return DEFAULT_HUB_URL;
}

/** The Hub's placement document, or `{}` when it does not answer. */
export async function fetchPlacement(hub, { transport = globalThis.fetch, timeoutMs = TIMEOUT_MS } = {}) {
  try {
    const response = await transport(clean(hub) + "/api/placement", { signal: AbortSignal.timeout(timeoutMs), headers: { Accept: "application/json" } });
    if (!response.ok) return {};
    const data = await response.json();
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch { return {}; }
}

/** The placement document through a short cache (one request in flight at a time per Hub). */
export async function currentPlacement(hub, { transport = globalThis.fetch, now = Date.now() } = {}) {
  const key = clean(hub);
  const saved = cache.get(key);
  if (saved?.data && saved.until > now) return saved.data;
  if (saved?.pending) return saved.pending;
  const pending = fetchPlacement(key, { transport }).then((data) => {
    cache.set(key, { until: Date.now() + (Object.keys(data).length ? FRESH_MS : SILENT_MS), data });
    return data;
  });
  cache.set(key, { ...(saved || {}), pending });
  return pending;
}

/** Forget the remembered answers (after a failed call, or in tests). */
export function forgetPlacement() { cache.clear(); }

/** The address `appId` answers on when the Hub put it on a Spark, else "". Nothing while it is moving. */
export function placedUrl(appId, document) {
  if (!document || document.location !== "sparks" || document.job?.running) return "";
  const url = document.urls?.[appId];
  return typeof url === "string" && /^https?:\/\//.test(url) ? clean(url) : "";
}

/** `configured` followed by the address the Hub placed `appId` on. The configured addresses are tried first: an app that
 *  is off on this PC refuses at once, so the placed address costs nothing extra, and an address somebody pinned on
 *  purpose (a test Faustus, another machine) keeps winning. */
export function withPlacement(appId, configured, document) {
  const urls = (configured || []).map(clean).filter(Boolean);
  const where = placedUrl(appId, document);
  return !where || urls.includes(where) ? urls : [...urls, where];
}

/** Candidate Faustus addresses in the order to try them: the configured ones, then the placed one. */
export async function faustusCandidates(configured, { hub = "", env = process.env, configPath = null, transport = globalThis.fetch } = {}) {
  const document = await currentPlacement(hubUrl({ hub, env, configPath }), { transport });
  return withPlacement("faustus", configured, document);
}

/** Where one movable app answers: the placed address, else `configured`. */
export async function appUrl(appId, configured = "", { hub = "", env = process.env, configPath = null, transport = globalThis.fetch } = {}) {
  const document = await currentPlacement(hubUrl({ hub, env, configPath }), { transport });
  return placedUrl(appId, document) || clean(configured);
}
