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
//   const a = await family.chat({ messages: [{ role: "user", content: "..." }], json: true });  // the local model, via the hub
//   res.json({ service: "links-hoard", hoard_link: family.healthBlock() });
//
// Where the hub is: HOARD_HUB_URL, else the `url` file the hub writes in
// HOARD_HUB_DATA_DIR or in a sibling `HoardLink/data/`, else :8810.
// Events are hints: when the hub is down they are dropped and counted,
// never thrown. HOARD_EVENTS=0 turns emission off.
//
// Models: chat() and linkStatus() use the hub's own Hoard Link (0.6) over
// HTTP, so a Node app gets the same model resolution, GPU lease and
// reasoning effort as the Python apps. They never throw: "no model",
// "timed out" and "hub down" come back as { ok: false, error }.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

export const FAMILY_VERSION = "0.8.2";
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
    return { status: null, data: { error: String(e && e.message || e) }, aborted: ctl.signal.aborted };
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

function toBase64(img) {
  if (typeof img === "string") return img;
  return Buffer.from(img).toString("base64");   // Buffer / Uint8Array / ArrayBuffer
}

/**
 * Ask the local model through the hub (POST /api/link/chat).
 * `messages`: [{role: "system"|"user"|"assistant", content}]. `capability`: "llm" or "vision"
 * (vision needs `images`: base64 strings, Buffers or Uint8Arrays, attached to the last user message).
 * `json`: true, or a JSON Schema object, to also get the parsed answer in `result.json`.
 * `effort`: "off"|"low"|"medium"|"high"|"max". Never throws. Returns
 * `{ ok, text, json, model, provider, ms, error, detail }`; on failure `ok` is false and `error` is
 * `no_model` (nothing can serve it), `timeout`, `hub_down` or `http_<code>` (`code` has the hub's own word,
 * e.g. gpu_busy, bad_request, backend_error). `timeoutMs` is how long the hub may take (queue and model);
 * `graceMs` is the extra time before this client gives up on a hub that stopped answering. The evidence for any answer is the text you sent: keep it.
 */
export async function chat({ messages, capability = "llm", images, json, effort, maxTokens, temperature, timeoutMs = 300000, graceMs = 15000 } = {}) {
  const body = { capability, messages: messages || [] };
  if (images && images.length) body.images = images.map(toBase64);
  if (json !== undefined && json !== null && json !== false) body.json = json;
  if (effort) body.effort = effort;
  if (maxTokens) body.max_tokens = maxTokens;
  if (temperature !== undefined && temperature !== null) body.temperature = temperature;
  body.timeout_s = timeoutMs / 1000;
  // The hub answers 504 at timeout_s; the abort is only the net for a hub that stopped answering at all.
  const { status: st, data, aborted } = await post("/api/link/chat", body, timeoutMs + graceMs);
  if (st === null) {
    return aborted ? { ok: false, error: "timeout", detail: `no answer from the hub within ${Math.round(timeoutMs / 1000)}s`, text: "", json: null, model: null, provider: null }
                   : { ok: false, error: "hub_down", detail: `hub not reachable at ${findHubUrl()}`, text: "", json: null, model: null, provider: null };
  }
  const d = data && typeof data === "object" ? data : {};
  if (st === 200 && d.ok) {
    return { ok: true, text: d.text ?? "", json: d.json ?? null, model: d.model ?? null, provider: d.provider ?? null,
             ms: d.ms ?? null, usage: d.usage ?? null, ...(d.json_error ? { json_error: d.json_error } : {}), error: null, detail: null };
  }
  const code = d.error || "";
  const error = code === "no_model" || code === "timeout" ? code : `http_${st}`;
  const detail = st === 401 ? `the hub refused this app's token (${state.tokenFile || "no token file"})` : (d.detail || code || "");
  return { ok: false, error, code, detail, text: "", json: null, model: null, provider: null };
}

/** Which model serves llm / vision / embed / tts now: `{ ok, llm: {available, model, provider, reason}, ... }`, or `{ ok: false, error: "hub_down" }`. */
export async function linkStatus({ force = false, timeoutMs = 30000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(findHubUrl() + "/api/link/status" + (force ? "?force=1" : ""), { headers: headers(), signal: ctl.signal });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (res.ok && data && typeof data === "object") return data;
    return { ok: false, error: `http_${res.status}`, detail: (data && (data.detail || data.error)) || "" };
  } catch (e) {
    return { ok: false, error: ctl.signal.aborted ? "timeout" : "hub_down", detail: String(e && e.message || e) };
  } finally {
    clearTimeout(timer);
  }
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

// ---------------------------------------------------------------- since
// Time windows as people and assistants say them, the same words as
// hoard_link/since.py: an ISO date, an age ("2h", "7d", "hace 3 días",
// "2 weeks ago"), a word ("hoy", "ayer", "esta mañana", "esta semana",
// "la semana pasada", "este mes", "última hora" and their English forms)
// or epoch seconds. Returns an ISO timestamp (a bare YYYY-MM-DD stays as
// written: it compares correctly with full timestamps), null for an empty
// value; anything else throws an Error with status 400 naming the forms.

const SINCE_UNIT_MS = {
  s: 1_000, sec: 1_000, secs: 1_000, second: 1_000, seconds: 1_000, seg: 1_000, segundo: 1_000, segundos: 1_000,
  m: 60_000, min: 60_000, mins: 60_000, minute: 60_000, minutes: 60_000, minuto: 60_000, minutos: 60_000,
  h: 3_600_000, hr: 3_600_000, hrs: 3_600_000, hour: 3_600_000, hours: 3_600_000, hora: 3_600_000, horas: 3_600_000,
  d: 86_400_000, day: 86_400_000, days: 86_400_000, dia: 86_400_000, dias: 86_400_000,
  w: 604_800_000, week: 604_800_000, weeks: 604_800_000, semana: 604_800_000, semanas: 604_800_000,
  mo: 2_592_000_000, month: 2_592_000_000, months: 2_592_000_000, mes: 2_592_000_000, meses: 2_592_000_000,
  y: 31_536_000_000, year: 31_536_000_000, years: 31_536_000_000, ano: 31_536_000_000, anos: 31_536_000_000,
};

export const SINCE_HELP =
  'since accepts epoch seconds, an ISO date or time ("2026-09-25", "2026-09-25T10:30"), an age ("30m", "2h", "3d", "2w", "1mo", "hace 2 horas", "2 hours ago"), or a word: hoy/today, ayer/yesterday, esta mañana/this morning, esta semana/this week, la semana pasada/last week, este mes/this month, el mes pasado/last month, última hora/last hour.';

function sinceFold(text) {
  return String(text).trim().toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/\s+/g, " ");
}

function sinceMidnight(ms) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d;
}

export function resolveSince(input, nowMs = Date.now()) {
  if (input === undefined || input === null || String(input).trim() === "") return null;
  if (typeof input === "number" && Number.isFinite(input)) return new Date(input * 1000).toISOString();
  const raw = String(input).trim();
  if (/^\d{9,}(\.\d+)?$/.test(raw)) return new Date(Number(raw) * 1000).toISOString();
  if (/^\d{4}-\d{2}-\d{2}([T ][\d:.]+(Z|[+-]\d{2}:?\d{2})?)?$/i.test(raw)) {
    if (raw.length === 10) return raw;
    const t = Date.parse(raw.replace(" ", "T"));
    if (!Number.isNaN(t)) return new Date(t).toISOString();
  }
  const s = sinceFold(raw);
  const day = sinceMidnight(nowMs);
  const shift = (ms) => new Date(day.getTime() + ms);
  const monday = () => { const d = new Date(day); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d; };
  const first = () => { const d = new Date(day); d.setDate(1); return d; };
  const words = {
    hoy: () => day, today: () => day,
    ayer: () => shift(-86_400_000), yesterday: () => shift(-86_400_000),
    "esta manana": () => shift(6 * 3_600_000), "this morning": () => shift(6 * 3_600_000),
    "esta semana": monday, "this week": monday,
    "este mes": first, "this month": first,
    "la semana pasada": () => new Date(nowMs - 604_800_000), "last week": () => new Date(nowMs - 604_800_000),
    "el mes pasado": () => new Date(nowMs - 2_592_000_000), "last month": () => new Date(nowMs - 2_592_000_000),
  };
  if (words[s]) return words[s]().toISOString();
  const one = s.match(/^(?:la\s+|el\s+|the\s+)?(?:ultima|ultimo|last|past)\s+([a-z]+)$/);
  if (one && SINCE_UNIT_MS[one[1]]) return new Date(nowMs - SINCE_UNIT_MS[one[1]]).toISOString();
  const m = s.match(/^(?:hace\s+|last\s+)?(\d+(?:[.,]\d+)?)\s*([a-z]+)(?:\s+ago)?$/);
  if (m && SINCE_UNIT_MS[m[2]]) return new Date(nowMs - Number(m[1].replace(",", ".")) * SINCE_UNIT_MS[m[2]]).toISOString();
  throw Object.assign(new Error(SINCE_HELP), { status: 400 });
}

// ---------------------------------------------------------------- notify
// Tell the person through the hub (facet "notify"): Windows toast, ntfy, Telegram or mail, chosen by
// priority and sphere, with quiet hours, duplicate and rate limits. Keep your own channel code only as
// the fallback for when the hub is unreachable (notify() then resolves {ok: false, error: "hub unreachable"}).
//
//   const res = await family.notify("Payment failed", "Netflix 12.99 EUR", { priority: "high", url, group: "payment", dedupeKey: `pay:${id}` });
//   if (!res.ok && res.error === "hub unreachable") ownToast(...);
//
// Part of hoard-link.js: relies on post(), headers() and findHubUrl() above; never throws.

const NOTIFY_CACHE_MS = 30_000;
const notifyAvail = new Map();   // hub url -> { at, ok }

/** Ask the hub to notify the person. Resolves to the hub's answer {ok, id, held, channels, delivered…}. */
export async function notify(title, body = "", { priority = "normal", url = "", group = "", dedupeKey = "", sphere = null, timeoutMs = 5000 } = {}) {
  const payload = { title: String(title ?? ""), body: String(body ?? ""), priority: String(priority || "normal") };
  if (url) payload.url = String(url);
  if (group) payload.group = String(group);
  if (dedupeKey) payload.dedupe_key = String(dedupeKey);
  if (sphere) payload.sphere = String(sphere);
  const { status: st, data } = await post("/api/notify", payload, timeoutMs);
  if (st === null) { notifyAvail.delete(findHubUrl()); return { ok: false, error: "hub unreachable" }; }
  if (data && typeof data === "object") {
    if (data.ok === undefined) data.ok = st >= 200 && st < 300;
    if (data.status === undefined) data.status = st;
    if (st === 401) data.error = `the hub refused this app's token (${state.tokenFile || "no token file"})`;
    return data;
  }
  return st >= 200 && st < 300 ? { ok: true, status: st } : { ok: false, status: st, error: `HTTP ${st}` };
}

/** True when a hub with the notification facet answers. Cached for 30 seconds. */
export async function hubAvailable(timeoutMs = 1000) {
  const base = findHubUrl();
  const hit = notifyAvail.get(base);
  if (hit && Date.now() - hit.at < NOTIFY_CACHE_MS) return hit.ok;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let ok = false;
  try {
    const res = await fetch(base + "/api/notify?limit=1", { headers: headers(), signal: ctl.signal });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    ok = res.status === 200 && Boolean(data && data.ok === true);
  } catch { ok = false; } finally { clearTimeout(timer); }
  notifyAvail.set(base, { at: Date.now(), ok });
  return ok;
}

// ---- channels and the router: the Node twins of hoard_link/notify_channels.py (toast) and fam_notify.Router ----------------------------
// A Node app that wants the Windows toast as its own fallback uses buildToastPs1/showToast; ledger-style "via" switching is notifyRouter.
//
//   const router = family.notifyRouter({ via: () => getSetting("notify.via", "auto"), ownSend: (title, body, o) => family.showToast(title, body, o.url), app: "ledger" });
//   const res = await router.send("Payment failed", "Netflix 12.99 EUR", { priority: "high", url, group: "payment", dedupeKey: `pay:${id}` });
//   // { via: "hub" | "own", ok, why, held?, id?, hub?, own? }

export const POWERSHELL_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe";
const TOAST_TIMEOUT_MS = 20_000;
const cutChars = (v, n) => Array.from(String(v ?? "")).slice(0, n).join("");

/** Escape for XML text and attribute values; also drops the control characters XML 1.0 forbids. */
export function xmlEscape(text) {
  return String(text ?? "").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}

function urlScheme(u) {
  const m = /^([A-Za-z][A-Za-z0-9+.\-]*):/.exec(String(u ?? "").trim());
  return m ? m[1].toLowerCase() : "";
}

/** The URL when it is http(s), else "". */
export function httpUrl(url) {
  const u = String(url ?? "").trim();
  return ["http", "https"].includes(urlScheme(u)) ? u : "";
}

/** PowerShell that shows one toast through Windows.UI.Notifications. Byte-for-byte the output of notify_channels.build_toast_ps1. */
export function buildToastPs1(title, body = "", url = null, { appName = null, appId = POWERSHELL_APP_ID } = {}) {
  const launch = httpUrl(url);
  const attrs = launch ? ` activationType="protocol" launch="${xmlEscape(launch)}"` : "";
  const attribution = appName ? `<text placement="attribution">${xmlEscape(cutChars(appName, 60))}</text>` : "";
  const xml = `<toast${attrs}><visual><binding template="ToastGeneric"><text>${xmlEscape(cutChars(title, 120))}</text>`
    + `<text>${xmlEscape(cutChars(body, 300))}</text>${attribution}</binding></visual></toast>`;
  return "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
    + "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
    + `$xml = @'\n${xml}\n'@\n`
    + "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
    + "$doc.LoadXml($xml)\n"
    + "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)\n"
    + `[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('${appId}').Show($toast)\n`;
}

function spawnRun({ command, args, timeoutMs }) {
  return new Promise((resolve) => {
    let child;
    try { child = spawn(command, args, { windowsHide: true, stdio: "ignore" }); } catch (e) { resolve({ code: null, error: (e && e.code) || "spawn" }); return; }
    let settled = false;
    const finish = (r) => { if (settled) return; settled = true; clearTimeout(timer); resolve(r); };
    const timer = setTimeout(() => { try { child.kill(); } catch { /* gone */ } finish({ code: null, error: "timeout" }); }, timeoutMs);
    child.on("error", (e) => finish({ code: null, error: (e && e.code) || "spawn" }));
    child.on("close", (code) => finish({ code }));
  });
}

/** Run a PowerShell script from a temporary .ps1 (UTF-8 with BOM) without a console window. Resolves { ok } or { ok: false, error }. */
export async function runPs1(script, { runner = null, timeoutMs = TOAST_TIMEOUT_MS } = {}) {
  const file = path.join(os.tmpdir(), `hoard-toast-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.ps1`);
  try {
    fs.writeFileSync(file, "﻿" + script, "utf8");
    const done = await (runner || spawnRun)({ command: "powershell", args: ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", file], timeoutMs });
    if (done && done.code === 0) return { ok: true };
    return { ok: false, error: done && done.error ? String(done.error) : `powershell exit ${done ? done.code : "?"}` };
  } catch (e) {
    return { ok: false, error: String((e && e.code) || (e && e.name) || "error") };
  } finally {
    try { fs.rmSync(file, { force: true }); } catch { /* ignore */ }
  }
}

/** Show one Windows toast and wait for PowerShell. Off Windows: { ok: false, unsupported: true, error: "unsupported" }. Never rejects. */
export async function showToast(title, body = "", url = null, { appName = null, runner = null, platform = process.platform, timeoutMs = TOAST_TIMEOUT_MS } = {}) {
  if (!String(platform).startsWith("win")) return { ok: false, unsupported: true, error: "unsupported" };
  return runPs1(buildToastPs1(title, body, url, { appName }), { runner, timeoutMs });
}

/** The auto | hub | own switch in front of the hub's notification facet (twin of fam_notify.Router).
 *  via: "auto" | "hub" | "own", or a (maybe async) function returning one; ownSend(title, body, { priority, url, group, dedupeKey, sphere })
 *  is the app's own delivery (resolves an object with ok/error, a boolean, an error string, or nothing); hub: { notify, hubAvailable }
 *  (default: this module). A message the hub HELD (quiet hours, duplicate, digest, rate, disabled) counts as delivered and is not repeated
 *  through ownSend. */
export function notifyRouter({ via = "auto", ownSend = null, app = "", hub = null } = {}) {
  const api = () => hub || { notify, hubAvailable };
  const setting = async () => {
    try {
      const v = String(typeof via === "function" ? await via() : via ?? "auto").trim().toLowerCase();
      return ["auto", "hub", "own"].includes(v) ? v : "auto";
    } catch { return "auto"; }
  };
  const hubUp = async () => { try { return Boolean(await api().hubAvailable()); } catch { return false; } };
  const viaHub = async (title, body, o) => {
    let answer;
    try {
      answer = await api().notify(title, body, { priority: o.priority || "normal", url: o.url || "", group: o.group || "", dedupeKey: o.dedupeKey || "", sphere: o.sphere || null });
    } catch (e) { return { via: "hub", ok: false, why: `hub notify: ${(e && e.name) || "error"}` }; }
    if (!answer || typeof answer !== "object") return { via: "hub", ok: false, why: "hub notify: unexpected answer" };
    if (answer.ok) {
      const held = String(answer.held || "");
      return { via: "hub", ok: true, why: held ? `held: ${held}` : "", held, id: answer.id, hub: answer };
    }
    return { via: "hub", ok: false, held: "", hub: answer, why: String(answer.error || `hub notify failed (${answer.status})`).slice(0, 200) };
  };
  const viaOwn = async (title, body, o) => {
    if (typeof ownSend !== "function") return { via: "own", ok: false, why: "no own channel configured" };
    let raw;
    try { raw = await ownSend(title, body, { priority: o.priority || "normal", url: o.url, group: o.group, dedupeKey: o.dedupeKey, sphere: o.sphere }); }
    catch (e) { return { via: "own", ok: false, why: String((e && e.name) || "error") }; }
    if (raw && typeof raw === "object") {
      const ok = "ok" in raw ? Boolean(raw.ok) : !raw.error;
      return { via: "own", ok, why: ok ? "" : String(raw.error || "failed").slice(0, 200), own: raw };
    }
    if (typeof raw === "string") return { via: "own", ok: !raw, why: raw.slice(0, 200) };
    if (raw === false) return { via: "own", ok: false, why: "failed" };
    return { via: "own", ok: true, why: "" };
  };
  return {
    app,
    via: setting,
    async viaStatus() {
      const s = await setting();
      const up = s !== "own" ? await hubUp() : false;
      return { setting: s, effective: s === "hub" || (s === "auto" && up) ? "hub" : "own", hub_available: up };
    },
    /** { via, ok, why, held?, id?, hub?, own? }. Never rejects. */
    async send(title, body = "", o = {}) {
      const s = await setting();
      if (s !== "own" && (s === "hub" || (await hubUp()))) {
        const res = await viaHub(title, body, o);
        if (res.ok || s === "hub") return res;
      }
      return viaOwn(title, body, o);
    },
  };
}

// mail.js — the app side of the hub's mail gateway, from a Node app (part of hoard-link.js; the coordinator merges
// js/parts/*.js into it, so `state`, `post`, `headers`, `findHubUrl`, `fs` and `path` below are the ones defined at the top of
// that file). The twin of hoard_link/fam_mail.py.
//
// The hub reads the inbox once for the whole family (facet "mailgate"). An app registers what it is interested in, asks for the
// messages that match and says which ones it took, so they stop showing up in the person's "sin dueño" tray. Keep the app's own
// mail helper as the fallback for when the hub, or its gateway, is not there.
//
//   family.configure({ app: "ledger", dataDir: DATA_DIR });
//   if (await family.mailAvailable()) {
//     await family.mailRegisterInterest({ subject_terms: ["factura", "recibo"], from_domains: ["amazon.es"], has_attachment: true });
//     const page = await family.mailMessages({ sinceId: lastSeen });          // { ok, messages, last_id }; resume from last_id
//     for (const m of page.messages) {
//       // m.subject, m.text, m.attachments[i].path …
//       await family.mailClaim([m.id], "payment", "hoard://ledger/tx/12");
//     }
//     lastSeen = page.last_id;
//   } else { /* the app's own helper */ }
//
// Nothing here rejects: when the hub cannot be reached the answer is { ok: false, error: "hub unreachable" }. The hub only returns
// mail of the spheres the app is allowed in.

const MAIL_CACHE_MS = 30_000;
const mailAvail = new Map();   // hub url + token -> { at, ok }

async function mailGet(p, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(findHubUrl() + p, { headers: headers(), signal: ctl.signal });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    return { status: res.status, data };
  } catch (e) {
    return { status: null, data: { error: String((e && e.message) || e) } };
  } finally {
    clearTimeout(timer);
  }
}

function mailAnswer(st, data) {
  if (st === null) return { ok: false, error: "hub unreachable" };
  if (!data || typeof data !== "object") return { ok: false, status: st, error: `HTTP ${st}` };
  if (data.ok === undefined) data.ok = st >= 200 && st < 300;
  if (data.status === undefined) data.status = st;
  if (st === 401) data.error = `the hub refused this app's token (${state.tokenFile || "no token file"})`;
  return data;
}

/** True when the hub is up AND its mail gateway is on and has read the inbox at least once (and recently). Cached 30 s. */
export async function mailAvailable(timeoutMs = 1000) {
  const key = findHubUrl() + "|" + (headers().Authorization || "");
  const hit = mailAvail.get(key);
  if (hit && Date.now() - hit.at < MAIL_CACHE_MS) return hit.ok;
  const { status: st, data } = await mailGet("/api/mail/status", timeoutMs);
  let ok = false;
  if (st === 200 && data && data.ready) {
    const interval = Number(data.interval_min || 0);
    ok = data.fresh_s === null || data.fresh_s === undefined || interval === 0 || Number(data.fresh_s) <= interval * 180 + 900;   // a stalled hub pass is "not available"
  }
  mailAvail.set(key, { at: Date.now(), ok });
  return ok;
}

/** Drop the 30-second cache (after changing the hub, or in tests). */
export function mailForgetAvailability() { mailAvail.clear(); }

/** Tell the hub which mail this app wants: { subject_terms, from_domains, from_addresses, text_terms, regex, has_attachment }.
 *  A message matches when ANY non-empty criterion matches (case-insensitive, accents folded). Optional extras (0.8): `exclude` (a spec
 *  with the same keys: a message that matches it is dropped), `all_of` (a list of specs that must ALL match) and `category`
 *  (promo | social | security | dev | other, a string or a list). `sphere` limits it to one sphere. */
export async function mailRegisterInterest(spec, { sphere = null, timeoutMs = 10000 } = {}) {
  const body = { spec: spec || {} };
  if (sphere) body.sphere = String(sphere);
  const { status: st, data } = await post("/api/mail/interests", body, timeoutMs);
  return mailAnswer(st, data);
}

function mailKafkaShape(m) {
  const name = String(m.from_name || ""), addr = String(m.from_addr || "");
  const ts = m.date_ts || null;
  m.from = name && addr ? `${name} <${addr}>` : (addr || name);
  m.from_address = addr;
  m.ts = ts;
  m.date = ts ? new Date(ts * 1000).toUTCString() : "";
  m.account = m.source || "";
  m.from_self = Array.isArray(m.reasons) && m.reasons.includes("own mail");
  if (m.text === undefined) m.text = "";
  if (!Array.isArray(m.links)) m.links = [];
  if (!Array.isArray(m.attachments)) m.attachments = [];
  return m;
}

function mailFieldsParam(fields) {
  const items = Array.isArray(fields) ? fields : String(fields || "").split(/[,\s]+/);
  const names = [...new Set(items.map((f) => String(f).trim().toLowerCase()).filter((f) => ["html", "images", "headers", "all"].includes(f)))];
  return names.includes("all") ? "html,images,headers" : names.join(",");
}

/** The messages the hub stored with id > sinceId (oldest first) for this app's spheres; with interest: true only those that
 *  match the interest it registered. { ok, messages, last_id }: pass last_id as the next sinceId. Each message carries the
 *  gateway's keys (id, source, sphere, from_addr, from_name, to, subject, snippet, priority, text, links,
 *  attachments[{name, mime, size, sha, path, url}]) AND the Kafka helper's (message_id, subject, from, from_address, date, ts,
 *  text, links, attachments with the local path). `fields` (array or comma string: html, images, headers, all) asks for what the default
 *  answer leaves out: the raw HTML part (only for mail with structured markup), images [{alt, src}] and headers {list_unsubscribe,
 *  one_click, gmail_category, message_id}. */
export async function mailMessages({ sinceId = 0, limit = 100, full = true, interest = true, timeoutMs = 20000, fields = null } = {}) {
  const wanted = mailFieldsParam(fields);
  const q = `since_id=${Math.trunc(sinceId)}&limit=${Math.trunc(limit)}&kind=mail&interest=${interest ? 1 : 0}${full ? "&full=1" : ""}${wanted ? `&fields=${wanted}` : ""}`;
  const { status: st, data } = await mailGet(`/api/mail/messages?${q}`, timeoutMs);
  const res = mailAnswer(st, data);
  if (res.ok) {
    res.messages = (res.messages || []).filter((m) => m && typeof m === "object").map(mailKafkaShape);
    if (wanted) {
      for (const m of res.messages) {
        if (wanted.includes("html") && m.html === undefined) m.html = "";
        if (wanted.includes("images") && m.images === undefined) m.images = [];
        if (wanted.includes("headers") && m.headers === undefined) m.headers = {};
      }
    }
    if (res.last_id === undefined) res.last_id = sinceId;
  } else {
    if (!res.messages) res.messages = [];
    if (res.last_id === undefined) res.last_id = sinceId;
  }
  return res;
}

/** Record "this mail is mine" (kind e.g. payment / document / shipment; ref the hoard:// uri of what the app made from it).
 *  A claimed message leaves the "needs you" and "sin dueño" lists. */
export async function mailClaim(ids, kind, ref, { timeoutMs = 10000 } = {}) {
  const { status: st, data } = await post("/api/mail/claim", { ids: (ids || []).map(Number), kind: String(kind || ""), ref: String(ref || "") }, timeoutMs);
  return mailAnswer(st, data);
}

/** Copy one attachment (an element of a message's attachments) into destDir; resolves to the new path, or "" when it cannot be had.
 *  Uses the local file when the hub's path is readable from here, else downloads it through the hub. */
export async function mailCopyAttachment(att, destDir, { timeoutMs = 30000 } = {}) {
  try { fs.mkdirSync(destDir, { recursive: true }); } catch { return ""; }
  const src = String((att && att.path) || "");
  const stem = String((att && att.sha) || "") || path.parse(src).name || "attachment";
  const ext = (path.extname(src) || path.extname(String((att && att.name) || "")) || ".bin").toLowerCase();
  const dest = path.join(destDir, stem + ext);
  if (fs.existsSync(dest)) return dest;
  try {
    if (src && fs.existsSync(src)) { fs.copyFileSync(src, dest); return dest; }
    const url = String((att && att.url) || "");
    if (!url) return "";
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const res = await fetch(url.startsWith("/") ? findHubUrl() + url : url, { headers: headers(), signal: ctl.signal });
      if (!res.ok) return "";
      fs.writeFileSync(dest + ".part", Buffer.from(await res.arrayBuffer()));
      fs.renameSync(dest + ".part", dest);
      return dest;
    } finally { clearTimeout(timer); }
  } catch {
    try { fs.rmSync(dest + ".part", { force: true }); } catch { /* ignore */ }
    return "";
  }
}

// ---- Faustus discovery, the helper runner and the router: Node twins of fam_mail.faustus_dir / FaustusHelper / MailRouter ------------------
// The Python mail helper (hoard_link/mail_helper.py) runs with Faustus's own Python. A Node app that vendors that file gives
// spawnHelperRunner its path; one that keeps its own spawn passes any `runHelper(action, payload, timeoutMs) -> answer`.
//
//   const router = family.famMailRouter({
//     runHelper: family.spawnHelperRunner({ helperPath: HELPER, setting: () => getSetting("mail.faustus_dir") }),
//     sourceGetter: () => getSetting("mail.source", "auto"), interest: { subject_terms: TERMS }, claimKind: "payment",
//     watermarkGet: () => Number(getSetting("mail.hub_since_id", 0)), watermarkSet: (v) => setSetting("mail.hub_since_id", v),
//   });
//   const rows = await router.scan({ since_days: 30, limit: 60, skip: knownIds, subject_terms: TERMS });
//   ...file them...; router.commit(); await router.claim(rows, "hoard://ledger/tx/12");

const FAUSTUS_ENV = ["FAUSTUS_DIR", "HOARD_FAUSTUS_DIR", "HOARD_HUB_FAUSTUS_DIR"];
const FAUSTUS_PYTHON_ENV = ["FAUSTUS_PYTHON", "HOARD_FAUSTUS_PYTHON", "HOARD_HUB_FAUSTUS_PYTHON"];
export const PYTHON_CANDIDATES = ["venv/Scripts/python.exe", ".venv/Scripts/python.exe", "venv/bin/python", ".venv/bin/python"];
const COMMON_FAUSTUS_PATHS = ["D:\\LocalAI\\faustus", "C:\\LocalAI\\faustus", "~/LocalAI/faustus", "~/faustus"];

const isFile = (p) => { try { return fs.statSync(p).isFile(); } catch { return false; } };
const nonEmpty = (v) => v !== undefined && v !== null && String(v).trim() !== "";
const expandHome = (p) => (String(p).startsWith("~") ? path.join(os.homedir(), String(p).slice(1)) : String(p));
const callValue = (v) => { try { return typeof v === "function" ? v() : v; } catch { return null; } };

/** The Faustus folder (the one with mcp_servers/email_server.py) or null. First hit wins: `setting` (a path or a function), the
 *  environment (FAUSTUS_DIR, HOARD_FAUSTUS_DIR, HOARD_HUB_FAUSTUS_DIR), a `faustus` folder next to this file, the app that vendors it or any
 *  folder above (five levels), the usual places (D:\LocalAI\faustus, ~/faustus ...), then `extra`. Sync; the hub's own hint is faustusHubHint(). */
export function faustusDir(setting = null, { env = process.env, extra = [], searchParents = true, commonPaths = COMMON_FAUSTUS_PATHS } = {}) {
  const candidates = [callValue(setting), ...FAUSTUS_ENV.map((k) => env[k])].filter(nonEmpty).map(expandHome);
  let dir = path.dirname(fileURLToPath(import.meta.url));
  for (let i = 0; searchParents && i < 5; i += 1) {
    dir = path.dirname(dir);
    candidates.push(path.join(dir, "faustus"), path.join(dir, "Faustus"));
  }
  candidates.push(...commonPaths.map(expandHome), ...extra.filter(nonEmpty).map(expandHome));
  for (const c of candidates) if (isFile(path.join(c, "mcp_servers", "email_server.py"))) return path.resolve(c);
  return null;
}

/** What the hub's mail gateway says is Faustus's folder (GET /api/mail/status), or "" . */
export async function faustusHubHint(timeoutMs = 1000) {
  const { status: st, data } = await mailGet("/api/mail/status", timeoutMs);
  return st === 200 && data && data.faustus_dir ? String(data.faustus_dir) : "";
}

/** Faustus's own interpreter: its venv / .venv (Windows or POSIX layout), else `setting`, else FAUSTUS_PYTHON ... when that file exists. */
export function faustusPython(root, setting = null, { env = process.env } = {}) {
  if (root) for (const rel of PYTHON_CANDIDATES) { const full = path.join(String(root), rel); if (isFile(full)) return full; }
  for (const c of [callValue(setting), ...FAUSTUS_PYTHON_ENV.map((k) => env[k])]) if (nonEmpty(c) && isFile(String(c))) return String(c);
  return null;
}

/** A runHelper(action, payload, timeoutMs) that runs the Python mail helper with Faustus's Python (no console window). Never rejects:
 *  { ok: false, error } when Faustus or its Python is missing, the helper times out or answers nothing. Options: helperPath (the vendored
 *  mail_helper.py), setting (Faustus's folder), owner, python, env, envDropPrefixes (the app's own secrets), spawnFn (tests). */
export function spawnHelperRunner({ helperPath, setting = null, owner = null, python = null, env = process.env, envDropPrefixes = [], discovery = {}, spawnFn = spawn } = {}) {
  return async function runHelper(action, payload = {}, timeoutMs = 180_000) {
    const root = faustusDir(setting, { ...discovery, env });
    if (!root) return { ok: false, error: "Faustus folder not found (set it in the app's mail settings, or FAUSTUS_DIR)" };
    const py = faustusPython(root, python, { env });
    if (!py) return { ok: false, error: "Faustus has no venv with Python" };
    const request = { ...payload, action: String(action) };
    const who = String(callValue(owner) || "").trim();
    if (who && !request.owner) request.owner = who;
    const childEnv = Object.fromEntries(Object.entries(env).filter(([k]) => !envDropPrefixes.some((p) => k.startsWith(p))));
    childEnv.PYTHONIOENCODING = "utf-8";
    return new Promise((resolve) => {
      let child;
      try { child = spawnFn(py, [helperPath, root], { cwd: root, env: childEnv, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] }); }
      catch (e) { resolve({ ok: false, error: `mail helper: ${(e && e.code) || "spawn"}` }); return; }
      let out = "";
      let settled = false;
      const finish = (r) => { if (settled) return; settled = true; clearTimeout(timer); resolve(r); };
      const timer = setTimeout(() => { try { child.kill(); } catch { /* gone */ } finish({ ok: false, error: "the mail read took too long" }); }, timeoutMs);
      child.stdout.on("data", (c) => { out += c.toString("utf8"); });
      child.on("error", (e) => finish({ ok: false, error: `mail helper: ${(e && e.code) || "spawn"}` }));
      child.on("close", (code) => {
        const lines = out.split(/\r?\n/).filter((l) => l.trim().startsWith("{"));
        let answer = null;
        try { answer = lines.length ? JSON.parse(lines[lines.length - 1]) : null; } catch { answer = null; }
        finish(answer && typeof answer === "object" && "ok" in answer ? answer : { ok: false, error: `mail helper exit ${code}` });
      });
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(request));
    });
  };
}

const MAIL_SOURCES = ["auto", "hub", "faustus"];
const ROUTER_KEYS = new Set(["limit", "max", "deep", "fields", "order", "sinceDays", "senderDomains", "skipOwn"]);
const snakeKey = (k) => String(k).replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
const mailDomain = (a) => { const s = String(a ?? "").trim().toLowerCase(); return s.includes("@") ? s.slice(s.lastIndexOf("@") + 1).replace(/[ >]+$/g, "") : ""; };

/** The mail source of a Node app: the hub's gateway when it is ready, the Faustus helper otherwise (twin of fam_mail.MailRouter; same
 *  paging, claim and watermark semantics). Options: runHelper(action, payload, timeoutMs), helperAvailable() (default: runHelper given),
 *  sourceGetter() (auto | hub | faustus), interest (a spec or a function of the criteria), watermarkGet / watermarkSet, claimKind, page (100),
 *  maxPages (6), registerEveryS (21600), sphere, hub ({ mailAvailable, mailRegisterInterest, mailMessages, mailClaim }; default: this module),
 *  clock() in seconds, deepUsesHelper, autoClaim. Criteria for scan: since_days (30), limit (100), skip, query, deep, fields, sender_domains,
 *  skip_own, order; the rest goes to the helper as it is (camelCase keys become snake_case). Nothing here rejects. */
export function famMailRouter({ runHelper = null, helperAvailable = null, sourceGetter = null, interest = null, watermarkGet = null, watermarkSet = null,
  claimKind = "", page = 100, maxPages = 6, registerEveryS = 21600, sphere = null, hub = null, clock = () => Date.now() / 1000,
  deepUsesHelper = false, autoClaim = false } = {}) {
  const api = () => hub || { mailAvailable, mailRegisterInterest, mailMessages, mailClaim };
  const pageSize = Math.max(1, Math.trunc(page));
  const pages = Math.max(1, Math.trunc(maxPages));
  let wmMem = 0;
  let registeredAt = 0;
  let registeredSig = "";
  let pending = null;
  const router = { lastSource: "", lastError: "", lastMeta: {} };

  const mode = async () => {
    let v;
    try { v = String((await callValue(sourceGetter)) ?? "auto").trim().toLowerCase(); } catch { return "auto"; }
    if (v === "own" || v === "helper") v = "faustus";
    return MAIL_SOURCES.includes(v) ? v : "auto";
  };
  const hubUp = async () => { try { return Boolean(await api().mailAvailable()); } catch { return false; } };
  const helperUp = async () => {
    try { return typeof helperAvailable === "function" ? Boolean(await helperAvailable()) : typeof runHelper === "function"; } catch { return false; }
  };
  const sourceNow = async ({ deep = false } = {}) => {
    const m = await mode();
    if (m === "faustus") return "faustus";
    if (m === "hub") return "hub";
    if (deep && deepUsesHelper && (await helperUp())) return "faustus";
    return (await hubUp()) ? "hub" : "faustus";
  };
  const watermark = async () => { try { return Math.trunc(Number(await (watermarkGet ? watermarkGet() : wmMem)) || 0); } catch { return 0; } };

  async function ensureInterest(criteria = {}, { force = false } = {}) {
    let spec;
    try { spec = typeof interest === "function" ? await interest(criteria) : interest; } catch { spec = null; }
    if (!spec || typeof spec !== "object" || !Object.keys(spec).length) return { ok: true, skipped: "no interest" };
    const sig = JSON.stringify(spec) + "|" + String(sphere || "");
    if (!force && sig === registeredSig && clock() - registeredAt < registerEveryS) return { ok: true, cached: true };
    let answer;
    try { answer = sphere ? await api().mailRegisterInterest(spec, { sphere }) : await api().mailRegisterInterest(spec); }
    catch (e) { return { ok: false, error: `hub interest: ${(e && e.name) || "error"}` }; }
    if (answer && answer.ok) { registeredAt = clock(); registeredSig = sig; return { ok: true }; }
    return { ok: false, error: String((answer && answer.error) || "the hub refused the interest") };
  }

  const pick = (c, camel, snake, dflt) => (c[camel] !== undefined ? c[camel] : c[snake] !== undefined ? c[snake] : dflt);
  const sortRows = (rows, order) => (order === "asc" || order === "desc"
    ? rows.sort((a, b) => ((a.ts || 0) - (b.ts || 0)) * (order === "desc" ? -1 : 1)) : rows);

  async function scanHelper(c, fallbackFrom = "") {
    const limit = Math.trunc(Number(pick(c, "limit", "max", 100)) || 100);
    const payload = {};
    for (const [k, v] of Object.entries(c)) if (!ROUTER_KEYS.has(k) && !ROUTER_KEYS.has(snakeKey(k)) && k !== "order") payload[snakeKey(k)] = v;
    for (const [camel, snake] of [["sinceDays", "since_days"], ["senderDomains", "sender_domains"], ["skipOwn", "skip_own"]]) {
      const v = c[camel] !== undefined ? c[camel] : c[snake];
      if (v !== undefined) payload[snake] = v;
    }
    payload.max = limit;
    if (payload.since_days === undefined) payload.since_days = 30;
    let res;
    try { res = typeof runHelper === "function" ? await runHelper("scan", payload, 180_000) : { ok: false, error: "no mail helper configured" }; }
    catch (e) { res = { ok: false, error: `mail helper: ${(e && e.name) || "error"}` }; }
    if (!res || typeof res !== "object" || !res.ok) {
      return { ok: false, source: "faustus", error: String((res && res.error) || "the mail helper failed"), messages: [], more: false, pending_since: null, fallback_from: fallbackFrom };
    }
    const rows = sortRows((res.messages || []).filter((m) => m && typeof m === "object"), c.order);
    return { ok: true, source: "faustus", error: "", messages: rows, more: false, pending_since: null, accounts: res.accounts || [], read: rows.length, fallback_from: fallbackFrom };
  }

  async function scanHub(c) {
    const fail = (error) => ({ ok: false, source: "hub", error, messages: [], more: false, pending_since: null });
    const reg = await ensureInterest(c);
    if (!reg.ok) return fail(String(reg.error || "hub interest failed").slice(0, 200));
    const limit = Math.max(1, Math.trunc(Number(pick(c, "limit", "max", 100)) || 100));
    const deep = Boolean(c.deep);
    const days = Number(pick(c, "sinceDays", "since_days", 30)) || 30;
    const known = new Set((c.skip || []).map(String));
    const words = String(c.query || "").toLowerCase().split(/\s+/).filter(Boolean);
    const domains = (pick(c, "senderDomains", "sender_domains", []) || []).map((d) => String(d).toLowerCase().replace(/^@/, "")).filter(Boolean);
    const skipOwn = Boolean(pick(c, "skipOwn", "skip_own", false));
    const wm = deep ? 0 : await watermark();
    const cutoff = deep || wm === 0 ? clock() - days * 86400 : 0;
    const out = [];
    let last = wm; let read = 0; let gotAny = false; let more = false;
    for (let i = 0; i < pages; i += 1) {
      const opts = { sinceId: last, limit: pageSize, full: true };
      if (c.fields) opts.fields = c.fields;
      const pg = await api().mailMessages(opts);
      if (!pg || !pg.ok) { if (!read) return fail(String((pg && pg.error) || "the hub did not answer")); break; }
      const rows = (pg.messages || []).filter((m) => m && typeof m === "object");
      if (!rows.length) { last = Math.max(last, Number(pg.last_id) || last); break; }
      gotAny = true; read += rows.length;
      let full = false;
      for (const m of rows) {
        last = Math.max(last, Number(m.id) || 0);
        if (known.has(String(m.message_id || ""))) continue;
        if (cutoff && m.ts && Number(m.ts) < cutoff) continue;
        if (skipOwn && m.from_self) continue;
        if (domains.length) { const dom = mailDomain(m.from_address || m.from_addr); if (!domains.some((d) => dom === d || dom.endsWith(`.${d}`))) continue; }
        if (words.length) {
          const hay = ["subject", "text", "from_address", "from_name"].map((k) => String(m[k] ?? "")).join(" ").toLowerCase();
          if (!words.every((w) => hay.includes(w))) continue;
        }
        m.hub_id = m.id;
        if (m.account === undefined || m.account === "") m.account = m.source || "hub";
        out.push(m);
        if (out.length >= limit) { full = true; break; }
      }
      if (full) { more = true; break; }
      more = rows.length >= pageSize;
      if (!more) break;
    }
    sortRows(out, c.order);
    const pend = deep ? null : (gotAny || last !== wm ? last : null);
    return { ok: true, source: "hub", error: "", messages: out, more, pending_since: pend, read, accounts: [{ account: "hub gateway", matches: out.length }] };
  }

  async function claim(messages, ref = "", kind = null) {
    const rows = (Array.isArray(messages) ? messages : [messages]).filter((m) => m && typeof m === "object");
    const groups = new Map();
    for (const m of rows) {
      if (m.hub_id === undefined || m.hub_id === null || m.hub_id === "") continue;
      const n = Number(m.hub_id);
      if (!Number.isFinite(n)) continue;
      let r;
      try { r = String(typeof ref === "function" ? ref(m) : ref || ""); } catch { continue; }
      if (!groups.has(r)) groups.set(r, []);
      groups.get(r).push(Math.trunc(n));
    }
    let claimed = 0;
    for (const [r, ids] of groups) {
      try { const res = await api().mailClaim(ids, String(kind || claimKind), r); claimed += Number(res && res.claimed) || 0; } catch { /* a hint */ }
    }
    return { ok: true, claimed, groups: groups.size };
  }

  async function scanEx(criteria = {}) {
    const c = { ...criteria };
    const use = await sourceNow({ deep: Boolean(c.deep) });
    let answer;
    if (use === "hub") {
      answer = await scanHub(c);
      if (!answer.ok && (await mode()) === "auto") answer = await scanHelper(c, answer.error || "");
    } else {
      answer = await scanHelper(c);
    }
    pending = answer.pending_since === undefined ? null : answer.pending_since;
    router.lastSource = answer.ok ? answer.source : "";
    router.lastError = answer.ok ? "" : String(answer.error || "");
    const { messages: _m, ...meta } = answer;
    router.lastMeta = meta;
    if (answer.ok && autoClaim && answer.source === "hub") await claim(answer.messages);
    return answer;
  }

  return Object.assign(router, {
    mode, hubUp, helperUp, sourceNow, watermark, ensureInterest, scanEx, claim,
    forgetInterest() { registeredAt = 0; registeredSig = ""; },
    async scan(criteria = {}) { return [...((await scanEx(criteria)).messages || [])]; },
    async commit() {
      const p = pending; pending = null;
      if (p === null || p === undefined) return;
      if (watermarkSet) await watermarkSet(p); else wmMem = Math.trunc(Number(p));
    },
    async status() {
      const m = await mode();
      const up = m !== "faustus" ? await hubUp() : false;
      return { setting: m, effective: await sourceNow(), hub_available: up, helper_available: await helperUp(), interest_registered: Boolean(registeredSig),
        hub_since_id: await watermark(), last_source: router.lastSource, last_error: router.lastError };
    },
  });
}

// agenda.js — the agenda contract, from a Node (Express) app: GET /api/family/agenda (part of hoard-link.js; the
// coordinator merges js/parts/*.js into it, so `state` and `readText` below are the ones defined at the top of that
// file). The twin of hoard_link/fam_agenda.py.
//
// The hub's Today view and the family calendar (.ics) ask every running app what is coming up for the person. The app
// answers with a provider: one function returning plain objects for a date range.
//
//   family.configure({ app: "kafka", dataDir: DATA_DIR });
//   family.installAgenda(app, async (from, to, sphere) => [      // from/to: "YYYY-MM-DD" strings; sphere: "" for everything
//     { id: "kafka:deadline:41", title: "Renew the lease", start: "2026-10-05", kind: "deadline", priority: "high",
//       url: "http://127.0.0.1:5200/#/deadlines/41" },
//   ]);
//
// Call it before the app's SPA catch-all route (Express matches routes in order). The bearer token is the app's own
// (state.tokenFile, read on every request). A provider that throws gives { ok: false, error, items: [] }, never a 500.
// Item keys: id, title, start (YYYY-MM-DD or an ISO date-time), end, all_day, kind (deadline, delivery, birthday,
// followup, maintenance, release, review, cards, incident, publish, renewal, exam, other), priority (low, normal, high,
// urgent), url, detail, sphere. A Date object works for start/end (it is written as a UTC date-time).

export const AGENDA_PATH = "/api/family/agenda";
const AGENDA_KINDS = ["deadline", "delivery", "birthday", "followup", "maintenance", "release", "review", "cards", "incident", "publish", "renewal", "exam", "other"];
const AGENDA_PRIORITIES = ["low", "normal", "high", "urgent"];
const AGENDA_MAX_ITEMS = 500;
const AGENDA_MAX_SPAN_DAYS = 400;

const agendaPad = (n) => String(n).padStart(2, "0");
const agendaIsoDay = (d) => `${d.getUTCFullYear()}-${agendaPad(d.getUTCMonth() + 1)}-${agendaPad(d.getUTCDate())}`;
const agendaClip = (v, n) => String(v ?? "").split(/\s+/).filter(Boolean).join(" ").slice(0, n);

function agendaValidDay(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!m) return null;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return agendaIsoDay(d) === text ? text : null;
}

/** A date, a Date or an ISO string → { day: "YYYY-MM-DD", time: "HH:MM:SS+02:00" | "HH:MM:SS" | null, ms } or null. */
export function agendaParseWhen(value) {
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return null;
    const iso = value.toISOString();
    return { day: iso.slice(0, 10), time: iso.slice(11, 19) + "+00:00", ms: value.getTime() };
  }
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!text) return null;
  const day = agendaValidDay(text);
  if (day) return { day, time: null, ms: Date.parse(day + "T00:00:00Z") };
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|z|[+-]\d{2}:?\d{2})?$/.exec(text);
  if (!m || !agendaValidDay(m[1]) || +m[2] > 23 || +m[3] > 59 || +(m[4] || 0) > 59) return null;
  let zone = m[5] || "";
  if (zone === "Z" || zone === "z") zone = "+00:00";
  else if (zone && !zone.includes(":")) zone = zone.slice(0, 3) + ":" + zone.slice(3);
  const time = `${m[2]}:${m[3]}:${m[4] || "00"}${zone}`;
  return { day: m[1], time, ms: Date.parse(`${m[1]}T${time}`) };
}

/** One provider item → the contract's shape, or null when it cannot be shown (no title, no usable start). */
export function agendaNormalizeItem(raw, { app = "", defaultSphere = "" } = {}) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const title = agendaClip(raw.title, 200);
  const start = agendaParseWhen(raw.start);
  if (!title || !start) return null;
  const allDay = start.time === null ? true : (typeof raw.all_day === "boolean" ? raw.all_day : false);
  const outStart = allDay ? start.day : `${start.day}T${start.time}`;
  let outEnd = null;
  const end = agendaParseWhen(raw.end);
  if (end) {
    if (allDay) { if (end.day >= start.day) outEnd = end.day; }
    else if (end.time !== null && end.ms >= start.ms) outEnd = `${end.day}T${end.time}`;
  }
  const kind = String(raw.kind ?? "").trim().toLowerCase();
  const priority = String(raw.priority ?? "").trim().toLowerCase();
  const url = String(raw.url ?? "").trim();
  let id = agendaClip(raw.id, 160).replace(/ /g, "_");
  const outKind = AGENDA_KINDS.includes(kind) ? kind : "other";
  if (!id) {
    let h = 0;
    for (const ch of `${title}|${outStart}`) h = (Math.imul(h, 31) + ch.codePointAt(0)) >>> 0;
    id = `${app || "app"}:${outKind}:${h.toString(16).padStart(8, "0")}`;
  } else if (!id.includes(":") && app) id = `${app}:${id}`;
  const item = {
    id, title, start: outStart, all_day: allDay, kind: outKind,
    priority: AGENDA_PRIORITIES.includes(priority) ? priority : "normal",
    url: /^(https?|hoard):\/\/\S+$/i.test(url) ? url.slice(0, 500) : "",
    detail: agendaClip(raw.detail, 300),
    sphere: agendaClip(raw.sphere, 40).toLowerCase() || defaultSphere,
  };
  if (outEnd) item.end = outEnd;
  return item;
}

export function agendaNormalizeItems(result, opts = {}) {
  const list = Array.isArray(result) ? result : (result && Array.isArray(result.items) ? result.items : []);
  const out = [];
  const seen = new Set();
  for (const raw of list) {
    const item = agendaNormalizeItem(raw, opts);
    if (!item || seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
    if (out.length >= AGENDA_MAX_ITEMS) break;
  }
  return out;
}

/** The window an agenda request asks for (defaults today-7 … today+60; bad values fall back; swapped when inverted; capped). */
export function agendaRange(from, to, now = new Date()) {
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  const a = agendaParseWhen(from);
  const b = agendaParseWhen(to);
  let start = a ? Date.parse(a.day + "T00:00:00Z") : today - 7 * 86400000;
  let end = b ? Date.parse(b.day + "T00:00:00Z") : today + 60 * 86400000;
  if (end < start) [start, end] = [end, start];
  if ((end - start) / 86400000 > AGENDA_MAX_SPAN_DAYS) end = start + AGENDA_MAX_SPAN_DAYS * 86400000;
  return [agendaIsoDay(new Date(start)), agendaIsoDay(new Date(end))];
}

/** Run a provider for a request and return the response body. Never throws. */
export async function agendaAnswer(provider, from, to, sphere = "", now = new Date()) {
  const [f, t] = agendaRange(from, to, now);
  const sph = agendaClip(sphere, 40).toLowerCase();
  try {
    const result = await provider(f, t, sph);
    return { ok: true, items: agendaNormalizeItems(result, { app: state.app || "" }), from: f, to: t, sphere: sph };
  } catch (e) {
    return { ok: false, error: `${(e && e.name) || "Error"}: ${(e && e.message) || e}`.slice(0, 300), items: [], from: f, to: t, sphere: sph };
  }
}

function agendaTokenOk(given, tokenFile) {
  const expected = readText(tokenFile || state.tokenFile);
  if (!given || !expected || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

/** Express: add GET /api/family/agenda. The caller must send this app's own bearer token. Returns { installed, path }. */
export function installAgenda(app, provider, { path = AGENDA_PATH, tokenFile = null } = {}) {
  app.get(path, async (req, res) => {
    const auth = String((req.headers && req.headers.authorization) || "");
    const given = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
    if (!agendaTokenOk(given, tokenFile)) return res.status(401).json({ ok: false, error: "a family bearer token is required", items: [] });
    const q = req.query || {};
    const one = (v) => (Array.isArray(v) ? v[0] : v);
    return res.json(await agendaAnswer(provider, one(q.from), one(q.to), one(q.sphere) || ""));
  });
  return { installed: true, path };
}

// refs.js — references between apps, from a Node app (part of hoard-link.js; the coordinator merges js/parts/*.js
// into it, so `post`, `headers` and `findHubUrl` below are the ones defined at the top of that file).
//
//   family.refsLink("hoard://ledger/tx/12", "hoard://kafka/document/7", "purchase", { fromLabel: "Amazon 23.90 EUR" });
//   const g = await family.refsAround("hoard://ledger/tx/12");     // { ok, nodes, edges }
//
// Both resolve (never reject): `{ ok: false, error: "hub unreachable" }` when the hub is down. The hub accepts a link
// only when one end is a record of the calling app (its bearer token says who it is).

async function refsGet(p, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(findHubUrl() + p, { headers: headers(), signal: ctl.signal });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    return { status: res.status, data };
  } catch (e) {
    return { status: null, data: { error: String((e && e.message) || e) } };
  } finally {
    clearTimeout(timer);
  }
}

function refsAnswer({ status, data }) {
  if (status === null) return { ok: false, error: "hub unreachable" };
  if (data && typeof data === "object") { if (data.ok === undefined) data.ok = status >= 200 && status < 300; return data; }
  return { ok: status >= 200 && status < 300, status };
}

/** Record `from --rel--> to` in the hub. Idempotent. */
export async function refsLink(from, to, rel = "related", { fromLabel = "", toLabel = "", note = "", timeoutMs = 5000 } = {}) {
  return refsAnswer(await post("/api/refs", { from, to, rel, from_label: fromLabel, to_label: toLabel, note }, timeoutMs));
}

/** The records linked to `uri` within `depth` hops: { ok, nodes: [{uri, app, kind, id, label, app_url}], edges }. */
export async function refsAround(uri, depth = 1, { timeoutMs = 5000 } = {}) {
  return refsAnswer(await refsGet(`/api/refs?uri=${encodeURIComponent(uri)}&depth=${Number(depth) || 1}`, timeoutMs));
}

/** Remove a link this app owns. */
export async function refsUnlink(from, to, rel = "", { timeoutMs = 5000 } = {}) {
  return refsAnswer(await post("/api/refs/remove", { from, to, ...(rel ? { rel } : {}) }, timeoutMs));
}
