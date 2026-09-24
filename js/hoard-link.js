// hoard-link.js — the family client for Node apps (ESM, no dependencies).
//
// The Node counterpart of `hoard_link.family` (Python): emit events to the
// hub's bus, call other apps' tools through the hub with this app's own
// token, and report the family block in /api/health. Copy this file into
// the app (server/hoard-link.js) — it is vendored like the Python package,
// and `scripts/sync_vendored.py` in the HoardLink repository refreshes
// every copy.
//
//   import * as family from "./hoard-link.js";
//   family.configure({ app: "links", dataDir: DATA_DIR });
//   family.emit("links.watch.new", { id, title, url });          // fire and forget
//   const r = await family.call("hypatia", "cards_suggest", { text });
//   res.json({ service: "links-hoard", hoard_link: family.healthBlock() });
//
// Where the hub is: HOARD_HUB_URL, else the `url` file the hub writes in
// HOARD_HUB_DATA_DIR or in a sibling `HoardLink/data/`, else :8810.
// Events are hints: when the hub is down they are dropped and counted,
// never thrown. HOARD_EVENTS=0 turns emission off.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const FAMILY_VERSION = "0.4.0";
const DEFAULT_URL = "http://127.0.0.1:8810";

const state = { app: "", tokenFile: "", hub: null, enabled: true, sent: 0, dropped: 0, lastError: "" };

function readText(p) {
  try { return fs.readFileSync(p, "utf8").replace(/^﻿/, "").trim(); } catch { return ""; }
}

function findHubUrl() {
  if (state.hub) return state.hub.replace(/\/+$/, "");
  const env = (process.env.HOARD_HUB_URL || "").trim();
  if (env) return env.replace(/\/+$/, "");
  const candidates = [];
  if (process.env.HOARD_HUB_DATA_DIR) candidates.push(path.join(process.env.HOARD_HUB_DATA_DIR, "url"));
  if (process.env.HOARD_LINK_DIR) candidates.push(path.join(process.env.HOARD_LINK_DIR, "data", "url"));
  // this file lives in <app>/server/ (vendored) or <HoardLink>/js/ (the source)
  const here = path.dirname(fileURLToPath(import.meta.url));
  for (const up of [path.resolve(here, "..", ".."), path.resolve(here, "..")]) {
    for (const name of ["HoardLink", "Hoard Link", "hoard-link"]) candidates.push(path.join(up, name, "data", "url"));
  }
  for (const c of candidates) {
    const text = readText(c);
    if (text.startsWith("http")) return text.replace(/\/+$/, "");
  }
  return DEFAULT_URL;
}

export function configure({ app, dataDir, tokenFile, hub, enabled } = {}) {
  state.app = String(app || process.env.HOARD_APP_ID || "").trim();
  state.tokenFile = String(tokenFile || process.env.HOARD_TOKEN_FILE || (dataDir ? path.join(dataDir, "mcp-token") : ""));
  state.hub = hub || null;
  const envOff = ["0", "false", "no", "off"].includes(String(process.env.HOARD_EVENTS ?? "1").trim().toLowerCase());
  state.enabled = enabled === undefined ? !envOff : Boolean(enabled);
  return status();
}

export function status() { return { ...state, hub: findHubUrl() }; }

function headers() {
  const tok = readText(state.tokenFile);
  return { "Content-Type": "application/json", Accept: "application/json", ...(tok ? { Authorization: `Bearer ${tok}` } : {}) };
}

async function post(p, body, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(findHubUrl() + p, { method: "POST", headers: headers(), body: JSON.stringify(body), signal: ctl.signal });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    return { status: res.status, data };
  } catch (e) {
    return { status: null, data: { error: String(e && e.message || e) } };
  } finally {
    clearTimeout(timer);
  }
}

/** Post an event. Never throws; returns a promise you may ignore. */
export function emit(type, data = {}, { block = false, timeoutMs = 3000 } = {}) {
  if (!state.enabled || !state.app) return Promise.resolve(false);
  const p = post("/api/events", { type: String(type), source: state.app, data: data || {} }, timeoutMs).then(({ status: st, data: d }) => {
    if (st === 200) { state.sent += 1; return true; }
    state.dropped += 1;
    state.lastError = (d && d.error) || `HTTP ${st}`;
    return false;
  });
  if (!block) p.catch(() => {});
  return p;
}

/** Call a tool of another app through the hub. Same shape as the hub's proxy. */
export async function call(app, tool, args = {}, { timeoutMs = 120000 } = {}) {
  const { status: st, data } = await post(`/api/apps/${encodeURIComponent(app)}/call`, { tool, arguments: args || {}, timeout_s: timeoutMs / 1000 }, timeoutMs + 5000);
  if (st === null) return { ok: false, app, tool, status: null, error: `hub not reachable at ${findHubUrl()}` };
  if (data && typeof data === "object") {
    if (data.status === undefined) data.status = st;
    if (st === 401) data.error = `the hub refused this app's token (${state.tokenFile || "no token file"})`;
    return data;
  }
  return { ok: st >= 200 && st < 300, app, tool, status: st, result: data };
}

/** One agent.call event per /api/agent/call — the audit trail. */
export function recordCall(tool, ok, ms, { caller = "", error = "" } = {}) {
  const data = { tool: String(tool), ok: Boolean(ok) };
  if (ms !== undefined && ms !== null) data.ms = Math.round(ms);
  if (caller) data.caller = String(caller).slice(0, 80);
  if (error) data.error = String(error).slice(0, 200);
  return emit("agent.call", data);
}

/** Add as `hoard_link: family.healthBlock()` in /api/health. */
export function healthBlock() {
  return { version: FAMILY_VERSION, family: FAMILY_VERSION, events: Boolean(state.enabled && state.app), app: state.app || null, hub: findHubUrl() };
}

/** Express helper: wraps the app's /api/agent/call handler so every call is recorded. */
export function recordAgentRoute(handler) {
  return async (req, res, next) => {
    const t0 = Date.now();
    const name = req.body && typeof req.body.name === "string" ? req.body.name : (req.body && req.body.tool) || "?";
    const caller = (req.body && req.body.caller) || "";
    const end = res.end;
    res.end = function patchedEnd(...a) {
      res.end = end;
      const ok = res.statusCode >= 200 && res.statusCode < 300;
      if (name !== "?" || !ok) recordCall(name, ok, Date.now() - t0, { caller, error: ok ? "" : `HTTP ${res.statusCode}` });
      return end.apply(this, a);
    };
    try { return await handler(req, res, next); } catch (e) { return next ? next(e) : undefined; }
  };
}
