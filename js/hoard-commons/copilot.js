// copilot.js — the Node twin of hoard_link/copilot.py's install_copilot for the Express Hoards: the Faustus copilot relay
// (/api/copilot/info, panel.js, proxy), the launcher (/api/copilot/launcher.js) and the launcher tag added to the app's
// HTML. The conversation, model, memory and tools live in Faustus; the app keeps no loop and no history, and the Faustus
// token never reaches the browser.
//
//   import { installCopilot } from "./hoard-commons/copilot.js";
//   installCopilot(app, { appId: "links", appName: "Links' Hoard", dataDir: DATA_DIR });   // before installSpa
//
// installSpa (express.js) does this on its own for every app that called family.configure, so most apps need no code.
// ESM, Node 18+, no npm dependencies.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { fileURLToPath } from "node:url";

export const LAUNCHER_PATH = "/api/copilot/launcher.js";
// What an app's session guard must let through (the page loads the launcher before its cookie exists) and the
// anti-CSRF header the launcher sends with /info and /proxy (same values as hoard_link/copilot.py).
export const COPILOT_PUBLIC_PATHS = Object.freeze([LAUNCHER_PATH]);
export const COPILOT_REQUEST_HEADER = Object.freeze(["X-Hoard-Request", "1"]);
const LAUNCHER_TAG = '<script type="module" src="/api/copilot/launcher.js"></script>';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ALLOWED_PREFIXES = ["/api/hoard-copilot/", "/api/chat/", "/api/session/", "/api/sessions/"];
const ALLOWED_EXACT = ["/api/chat_stream", "/api/session", "/api/sessions", "/api/upload"];
const FORWARD_REQUEST = ["x-faustus-client-version", "x-odysseus-run-id"];
const FORWARD_RESPONSE = ["content-type", "x-odysseus-run-id", "x-faustus-idempotent-replay", "x-faustus-api-version"];

let launcherCache = null;
/** The browser launcher (generated from hoard_link/copilot_launcher.py by scripts/sync_vendored.py). */
export function launcherJs() {
  if (launcherCache === null) launcherCache = fs.readFileSync(path.join(HERE, "copilot-launcher.js"), "utf8");
  return launcherCache;
}

/** `html` (string or Buffer) with the launcher tag before `</body>` (or at the end); unchanged when already there. */
export function injectLauncher(html) {
  const text = Buffer.isBuffer(html) ? html.toString("utf8") : String(html);
  if (text.includes(LAUNCHER_PATH)) return text;
  const at = text.toLowerCase().lastIndexOf("</body>");
  return at < 0 ? text + LAUNCHER_TAG : text.slice(0, at) + LAUNCHER_TAG + text.slice(at);
}

/** True when `app` already serves the copilot (installCopilot ran, or the app mounted its own /api/copilot/info). */
export function hasCopilot(app) {
  if (app?.locals?.hoardCopilot) return true;
  let stack = app?._router?.stack;                    // Express 4 (its app.router getter throws)
  if (!stack) { try { stack = app?.router?.stack; } catch { stack = null; } }   // Express 5
  stack = stack || [];
  return stack.some((layer) => layer?.route?.path === "/api/copilot/info");
}

function readJsonFile(file) {
  try { return JSON.parse(fs.readFileSync(file, "utf8").replace(/^﻿/, "")); } catch { return null; }
}

function envValue(env, name) {
  const value = env[name];
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

/** Faustus endpoints and token, same precedence as the Python LinkConfig: explicit options, then
 *  `<APP>_FAUSTUS_URL` / `HOARD_FAUSTUS_URL` / `HOARD_FAUSTUS_TOKEN`, then the optional backend.json, then the defaults. */
export function resolveFaustus({ appId = "app", faustusUrls = null, token = null, configPath = null, env = process.env } = {}) {
  const raw = (configPath && readJsonFile(configPath)) || {};
  const section = raw && typeof raw.faustus === "object" && raw.faustus ? raw.faustus : {};
  const clean = (url) => String(url).trim().replace(/\/+$/, "");
  let urls = faustusUrls && faustusUrls.length ? faustusUrls.map(clean) : null;
  if (!urls) {
    const fromEnv = envValue(env, appId.toUpperCase().replace(/-/g, "_") + "_FAUSTUS_URL") || envValue(env, "HOARD_FAUSTUS_URL");
    urls = fromEnv ? [clean(fromEnv)] : section.url ? [clean(section.url)] : ["http://127.0.0.1:7000", "http://127.0.0.1:7001"];
  }
  const secret = token ?? (envValue(env, "HOARD_FAUSTUS_TOKEN") || section.token || "");
  return { urls, token: secret || "" };
}

function spaceId(dataDir) {
  const file = path.join(dataDir, "copilot-space-id");
  fs.mkdirSync(dataDir, { recursive: true });
  try { fs.writeFileSync(file, crypto.randomUUID(), { flag: "wx", encoding: "utf8" }); }
  catch (error) { if (error.code !== "EEXIST") throw error; }
  return fs.readFileSync(file, "utf8").trim();
}

/** Put `origin` into the `context` / `hoard_context` field of a multipart body; everything else stays byte for byte. */
export function multipartContext(body, contentType, origin) {
  const match = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || "");
  if (!/^multipart\/form-data/i.test(contentType || "") || !match) return body;
  const boundary = Buffer.from("--" + (match[1] || match[2]).trim());
  const marks = [];
  for (let at = body.indexOf(boundary); at >= 0; at = body.indexOf(boundary, at + boundary.length)) {
    // A MIME delimiter occupies a complete line. Boundary substrings inside
    // an uploaded file, including JSON examples, are file bytes.
    if (at !== 0 && (body[at - 2] !== 13 || body[at - 1] !== 10)) continue;
    const tail = at + boundary.length;
    const end = body[tail] === 45 && body[tail + 1] === 45 ? tail + 2 : tail;
    if (end !== body.length && (body[end] !== 13 || body[end + 1] !== 10)) continue;
    marks.push(at);
  }
  const pieces = [];
  let cursor = 0;
  for (let i = 0; i + 1 < marks.length; i += 1) {
    const start = body.indexOf("\r\n", marks[i]) + 2;
    const end = marks[i + 1] - 2;                                  // the CRLF before the next boundary
    const split = body.indexOf("\r\n\r\n", start);
    if (start < 2 || split < 0 || split > end) continue;
    const head = body.subarray(start, split).toString("latin1");
    const disposition = /(?:^|\r\n)Content-Disposition:([^\r\n]*)/i.exec(head);
    const name = disposition && /(?:^|;)\s*name\s*=\s*(?:"([^"]*)"|([^;\s]+))/i.exec(disposition[1]);
    if (!name || !["context", "hoard_context"].includes(name[1] ?? name[2])) continue;
    let context;
    try { context = JSON.parse(body.subarray(split + 4, end).toString("utf8")); } catch { continue; }
    if (!context || typeof context !== "object" || Array.isArray(context)) continue;
    context.origin = { ...(context.origin || {}), ...origin };
    pieces.push(body.subarray(cursor, split + 4), Buffer.from(JSON.stringify(context), "utf8"));
    cursor = end;
  }
  if (!pieces.length) return body;
  pieces.push(body.subarray(cursor));
  return Buffer.concat(pieces);
}

async function readBody(req) {
  if (req.body !== undefined && req.body !== null && typeof req.body === "object" && !Buffer.isBuffer(req.body)) return req.body;
  if (Buffer.isBuffer(req.body)) return JSON.parse(req.body.toString("utf8"));
  if (typeof req.body === "string") return JSON.parse(req.body);
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "null");
}

/** The copilot's four Express handlers for one app (no routes registered): `info`, `panel`, `launcher`, `proxy`. */
export function buildCopilot({ appId, appName = "", dataDir, faustusUrls = null, token = null, configPath = null,
  inject = true, fetch: transport = globalThis.fetch, env = process.env } = {}) {
  if (!appId || !dataDir) throw new Error("installCopilot needs { appId, dataDir }");
  const faustus = resolveFaustus({ appId, faustusUrls, token, configPath, env });
  const auth = faustus.token ? { Authorization: "Bearer " + faustus.token } : {};
  const space = spaceId(dataDir);
  const origin = { app_id: appId, data_space_id: space };
  let selected = null;

  async function locate() {
    if (selected) return selected;
    for (const base of faustus.urls) {
      try {
        const response = await transport(base + "/api/hoard-copilot/capabilities", { headers: auth, signal: AbortSignal.timeout(3000) });
        if (response.ok && (await response.json())?.contract_version) return (selected = base);
      } catch { /* next endpoint */ }
    }
    const error = new Error("Faustus copilot is unavailable on the configured endpoints");
    error.status = 503;
    throw error;
  }

  async function relay(res, req, route, { method = "GET", body = undefined, contentType = "application/json", headers = {} } = {}) {
    const base = await locate();
    const controller = new AbortController();
    res.on("close", () => { if (!res.writableFinished) controller.abort(); });
    let upstream;
    try {
      upstream = await transport(base + route, { method, body, signal: controller.signal,
        headers: { ...auth, ...headers, "Content-Type": contentType } });
    } catch (cause) {
      selected = null;
      const error = new Error("Faustus copilot connection failed", { cause });
      error.status = 503;
      throw error;
    }
    res.status(upstream.status);
    for (const name of FORWARD_RESPONSE) {
      const value = upstream.headers.get(name);
      if (value) res.setHeader(name, value);
    }
    if (String(upstream.headers.get("content-type") || "").includes("text/event-stream")) {
      res.setHeader("Cache-Control", "no-cache");
      res.setHeader("X-Accel-Buffering", "no");
    }
    res.flushHeaders?.();
    if (!upstream.body) return res.end();
    const stream = Readable.fromWeb(upstream.body);
    stream.on("error", () => res.destroy());
    stream.pipe(res);
  }

  const fail = (res, error) => {
    if (res.headersSent) return res.destroy();
    res.status(error.status || 400).json({ error: error.status ? error.message : "Invalid copilot transport request",
      code: error.status === 503 ? "copilot_unavailable" : "bad_request" });
  };

  const handlers = {};
  handlers.info = async (req, res) => {
    try {
      const base = await locate();
      res.json({ app_id: appId, app_name: appName || appId, data_space_id: space, panel_url: "/api/copilot/panel.js",
        server: "/", tool_server_id: appId, settings_url: base + "/studio/settings" });
    } catch (error) { fail(res, error); }
  };
  handlers.panel = async (req, res) => {
    try { await relay(res, req, "/api/hoard-copilot/panel.js"); } catch (error) { fail(res, error); }
  };
  handlers.launcher = (req, res) => {
    res.setHeader("Cache-Control", "no-cache");
    res.type("application/javascript").send(launcherJs());
  };
  handlers.proxy = async (req, res) => {
    try {
      const envelope = await readBody(req);
      const target = envelope?.path;
      if (typeof target !== "string" || !target.startsWith("/") || target.startsWith("//")) throw new Error("Unknown copilot operation");
      const route = new URL(target, "http://relay.invalid").pathname;
      if (!ALLOWED_PREFIXES.some((prefix) => route.startsWith(prefix)) && !ALLOWED_EXACT.includes(route)) throw new Error("Unknown copilot operation");
      let body;
      let contentType = envelope.content_type || "application/json";
      if (typeof envelope.raw_body === "string") {
        body = multipartContext(Buffer.from(envelope.raw_body, "base64"), contentType, origin);
      } else if ("body" in envelope) {
        const payload = envelope.body;
        if (payload && typeof payload === "object" && !Array.isArray(payload)) {
          for (const field of ["context", "hoard_context"]) {
            if (payload[field] && typeof payload[field] === "object") payload[field].origin = { ...(payload[field].origin || {}), ...origin };
          }
        }
        body = JSON.stringify(payload);
        contentType = "application/json";
      }
      const headers = {};
      for (const [name, value] of Object.entries(envelope.headers || {})) {
        if (FORWARD_REQUEST.includes(name.toLowerCase()) && typeof value === "string") headers[name] = value;
      }
      await relay(res, req, target, { method: String(envelope.method || "GET").toUpperCase(), body, contentType, headers });
    } catch (error) { fail(res, error); }
  };
  return { handlers, info: { app_id: appId, launcher: LAUNCHER_PATH, inject: Boolean(inject), data_space_id: space } };
}

function register(app, pick) {
  app.get("/api/copilot/info", (req, res, next) => pick("info", req, res, next));
  app.get("/api/copilot/panel.js", (req, res, next) => pick("panel", req, res, next));
  app.get(LAUNCHER_PATH, (req, res, next) => pick("launcher", req, res, next));
  app.post("/api/copilot/proxy", (req, res, next) => pick("proxy", req, res, next));
}

/** Mount the copilot on an Express `app`. Call it before any SPA catch-all (installSpa does it for you).
 *  `inject` adds the launcher to the HTML installSpa serves; apps that mount their own panel pass `inject: false`. */
export function installCopilot(app, options = {}) {
  if (hasCopilot(app)) return app.locals.hoardCopilot || { app_id: options.appId, launcher: LAUNCHER_PATH, inject: false, existing: true };
  const { handlers, info } = buildCopilot(options);
  app.locals ||= {};
  register(app, (name, req, res) => handlers[name](req, res));
  app.locals.hoardCopilot = info;
  app.locals.hoardCopilotReady = async () => info;
  return info;
}

function findManifest(start) {
  let dir = path.resolve(start);
  for (let i = 0; i < 4; i += 1) {
    const file = path.join(dir, "faustus-plugin.json");
    if (fs.existsSync(file)) return readJsonFile(file);
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

/** installSpa's automatic copilot: on unless `HOARD_COPILOT=0`, skipped when the app already has one, and only for an
 *  app that told the family who it is (`family.configure({ app, dataDir })`). The routes are registered now (so they
 *  sit before the SPA catch-all) and resolved on the first request, after the app's startup ran `family.configure`;
 *  an unconfigured app answers them with its ordinary 404. `required` makes an unconfigured app an error instead. */
export function autoCopilot(app, { distDir = process.cwd(), required = false, env = process.env, family = null, fetch = undefined } = {}) {
  if (envValue(env, "HOARD_COPILOT") === "0" && !required) return false;
  if (hasCopilot(app)) return false;
  app.locals ||= {};
  let pending = null;
  const resolve = () => (pending ||= (async () => {
    let status = null;
    try {
      const link = family || (await import("../hoard-link.js"));
      status = typeof link.status === "function" ? link.status() : null;
    } catch { status = null; }
    if (!status?.app || !(status.dataDir || status.tokenFile)) {
      if (required) throw new Error("installSpa(copilot: true) needs family.configure({ app, dataDir }) first");
      return null;
    }
    const manifest = findManifest(distDir);
    const appName = (manifest && (manifest.name || manifest.title)) || status.app;
    const built = buildCopilot({ appId: String(status.app), appName, env, ...(fetch ? { fetch } : {}),
      dataDir: status.dataDir || path.dirname(status.tokenFile) });
    app.locals.hoardCopilot = built.info;
    return built;
  })());
  register(app, async (name, req, res, next) => {
    let built;
    try { built = await resolve(); } catch (error) { return next(error); }
    if (!built) return next();
    return built.handlers[name](req, res);
  });
  app.locals.hoardCopilotReady = async () => (await resolve().catch(() => null))?.info || null;
  return true;
}
