// express.js — the Node twin of hoard_link/{guard,agentkit,service,bridge}.py for the Express apps (Links, Ledger, People,
// JobHunter, Cook). ESM, Node 18+, **no npm dependencies**: Express and the MCP SDK are passed in by the app.
//
//   import { createGuard, makeAgentRoutes, installSpa, installErrorHandlers, runServer, createBridge } from "./hoard-commons/express.js";
//
//   Guard     LOCAL_HOSTS DEV_ORIGINS hostOf portOf parseAllowedHosts isAllowedHost checkRequest createGuard
//   Agent     capResult makeAgentRoutes
//   Service   installSpa installErrorHandlers runServer
//   Bridge    createBridge
//
// The guard and capResult behave exactly like the Python modules (tests/vectors/guard.json and agentkit.json run against both).

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { checkBearer } from "./server.js";

// ------------------------------------------------------------------------------------------------ guard

export const LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]"];
export const DEV_ORIGINS = [5173, 5174, 4173].flatMap((port) => [`http://localhost:${port}`, `http://127.0.0.1:${port}`]);
const FRAME_DESTS = new Set(["iframe", "frame", "embed", "object"]);
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

const MSG_HOST = "Only local access is allowed.";
const MSG_ORIGIN = "Origin not allowed.";
const MSG_SITE = "Cross-site requests are not allowed.";
const MSG_FORM = "Form submissions are not allowed.";

function authority(value) {
  let text = String(value ?? "").trim().toLowerCase();
  const scheme = text.indexOf("://");
  if (scheme !== -1) text = text.slice(scheme + 3);
  return text.split("/")[0];
}

/** Host part of a Host header, Origin or URL: no scheme, no path, no port, lowercase; an IPv6 literal keeps its brackets. */
export function hostOf(value) {
  const host = authority(value);
  if (host.startsWith("[")) {
    const end = host.indexOf("]");
    return end === -1 ? host : host.slice(0, end + 1);
  }
  return host.split(":")[0];
}

/** The explicit port of a Host header, Origin or URL, or null. */
export function portOf(value) {
  const text = authority(value);
  let rest;
  if (text.startsWith("[")) {
    const end = text.indexOf("]");
    rest = end === -1 ? "" : text.slice(end + 1);
  } else {
    const i = text.indexOf(":");
    rest = i === -1 ? "" : text.slice(i);
  }
  if (!rest.startsWith(":")) return null;
  const digits = rest.slice(1);
  if (!/^[0-9]+$/.test(digits)) return null;
  const n = Number(digits);
  return n >= 1 && n <= 65535 ? n : null;
}

/** Parse "a.example, *.ts.net, nas.local:8443" (or an array of entries) into a clean list of patterns. */
export function parseAllowedHosts(raw) {
  if (raw === null || raw === undefined) return [];
  const entries = typeof raw === "string" ? raw.split(",") : Array.from(raw, String);
  const out = [];
  for (const entry of entries.map((e) => e.trim()).filter(Boolean)) {
    const wildcard = entry.startsWith("*.");
    const body = wildcard ? entry.slice(2) : entry;
    const name = hostOf(body);
    const port = portOf(body);
    if (!name) continue;
    const pattern = (wildcard ? "*." : "") + name + (port ? `:${port}` : "");
    if (!out.includes(pattern)) out.push(pattern);
  }
  return out;
}

function nameMatches(name, pattern) {
  if (pattern.startsWith("*.")) {
    const suffix = pattern.slice(1);
    return name.endsWith(suffix) && name.length > suffix.length;
  }
  return name === pattern;
}

function patternMatches(name, explicit, pattern) {
  let pin = null;
  if (pattern.startsWith("[")) {
    pin = portOf(pattern);
    pattern = hostOf(pattern);
  } else if (pattern.includes(":")) {
    const i = pattern.lastIndexOf(":");
    const digits = pattern.slice(i + 1);
    pin = /^[0-9]+$/.test(digits) ? Number(digits) : null;
    pattern = pattern.slice(0, i);
  }
  if (!nameMatches(name, pattern)) return false;
  if (pin === null) return true;
  return explicit === pin || (explicit === null && (pin === 80 || pin === 443));
}

/** Is `host` (a Host header such as "localhost:5190", or a bare hostname) acceptable? With `strictPorts` a port named by `host`
 * must equal `port` (the app's own). */
export function isAllowedHost(host, port = null, allowed = [], { strictPorts = false } = {}) {
  if (!host) return false;
  const name = hostOf(host);
  if (!name) return false;
  const explicit = portOf(host);
  if (strictPorts && explicit !== null && port && explicit !== Number(port)) return false;
  if (LOCAL_HOSTS.includes(name)) return true;
  return allowed.some((pattern) => patternMatches(name, explicit, pattern));
}

function originOk(origin, port, allowed, devOrigins, strictPorts) {
  if (devOrigins.includes(origin)) return true;
  const name = hostOf(origin);
  if (strictPorts && LOCAL_HOSTS.includes(name)) {
    const explicit = portOf(origin);
    const schemeDefault = origin.trim().toLowerCase().startsWith("https://") ? 443 : 80;
    return Boolean(port) && (explicit ?? schemeDefault) === Number(port);
  }
  return isAllowedHost(origin, port, allowed);
}

/** null when the request may proceed, otherwise [status, message] (always 403). `headers` has lowercase names (Node's req.headers). */
export function checkRequest(method, headers, port = null, allowed = [], { devOrigins = DEV_ORIGINS, strictPorts = false } = {}) {
  const allowedList = Array.from(allowed);
  if (!isAllowedHost(headers.host, port, allowedList, { strictPorts })) return [403, MSG_HOST];
  const origin = headers.origin;
  if (origin && !originOk(origin, port, allowedList, Array.from(devOrigins), strictPorts)) return [403, MSG_ORIGIN];
  const site = headers["sec-fetch-site"];
  const mode = headers["sec-fetch-mode"];
  const dest = headers["sec-fetch-dest"];
  if (site === "cross-site" && (mode !== "navigate" || FRAME_DESTS.has(dest))) return [403, MSG_SITE];
  if (mode === "navigate" && !SAFE_METHODS.has(String(method).toUpperCase())) return [403, MSG_FORM];
  return null;
}

/** Express middleware: 403 { error } when checkRequest() rejects. `port` (a number) or `portGetter` (a function: the port is
 * only known after the port search); `allowedHosts` a list or a comma-separated string (e.g. process.env.LINKS_ALLOWED_HOSTS). */
export function createGuard({ port = null, portGetter = null, allowedHosts = [], devOrigins = DEV_ORIGINS, strictPorts = false } = {}) {
  const allowed = parseAllowedHosts(allowedHosts);
  return function guard(req, res, next) {
    const current = portGetter ? Number(portGetter()) || null : port;
    const verdict = checkRequest(req.method, req.headers, current, allowed, { devOrigins, strictPorts });
    if (verdict) return res.status(verdict[0]).json({ error: verdict[1] });
    next();
  };
}

// ------------------------------------------------------------------------------------------------ agent routes

const sizeOf = (value) => Buffer.byteLength(JSON.stringify(value, (k, v) => (typeof v === "bigint" ? String(v) : v)) ?? "null", "utf8");

/** Keep a tool result under ~`limit` bytes of JSON (same rules as hoard_link.agentkit.cap_result): the largest top-level list is
 * halved until it fits, one huge string is cut, and a "truncated" block says what was cut. A list result becomes { result: [...] }. */
export function capResult(data, limit = 20000) {
  if (Array.isArray(data)) {
    const wrapped = { result: data };
    const capped = capResult(wrapped, limit);
    return capped === wrapped ? data : capped;
  }
  if (data === null || typeof data !== "object" || sizeOf(data) <= limit) return data;
  const out = { ...data };
  const truncated = {};
  for (let i = 0; i < 40; i++) {
    if (sizeOf(out) <= limit - 300) break;
    const lists = Object.entries(out).filter(([, v]) => Array.isArray(v) && v.length > 1);
    if (!lists.length) break;
    let best = lists[0];
    for (const entry of lists) if (sizeOf(entry[1]) > sizeOf(best[1])) best = entry;
    const [key, value] = best;
    if (!(key in truncated)) truncated[key] = value.length;
    out[key] = value.slice(0, Math.max(1, Math.floor(value.length / 2)));
  }
  if (sizeOf(out) > limit - 300) {
    const strings = Object.entries(out).filter(([, v]) => typeof v === "string" && v.length > 200);
    if (strings.length) {
      let best = strings[0];
      for (const entry of strings) if (entry[1].length > best[1].length) best = entry;
      const [key, value] = best;
      if (!(key in truncated)) truncated[key] = value.length;
      out[key] = value.slice(0, Math.max(200, Math.floor(limit / 2))) + "…";
    }
  }
  out.truncated = { reason: `result capped at ~${Math.floor(limit / 1000)} KB`, original_lengths: truncated, hint: "Use limit or narrower filters to see the rest." };
  return out;
}

const issuesOf = (error) => (Array.isArray(error?.issues) ? error.issues : []).map((i) => ({
  loc: (Array.isArray(i.path) ? i.path.join(".") : String(i.path ?? "")) || "input",
  msg: String(i.message ?? "invalid"),
}));

/** Shape any thrown error as { status, body } for a JSON response: zod issues -> 400 invalid_arguments (+ issues), `status` /
 * `code` / `hint` / `details` / `candidates` of the error are kept, anything else is a 500 "internal". */
export function errorBody(error) {
  const issues = issuesOf(error);
  if (issues.length) return { status: 400, body: { error: issues.map((i) => `${i.loc}: ${i.msg}`).join("; "), code: "invalid_arguments", issues } };
  const status = Number.isInteger(error?.status) && error.status >= 400 && error.status < 600 ? error.status : 500;
  const body = { error: status === 500 && !error?.expose ? `Internal error: ${error?.name ?? "Error"}: ${String(error?.message ?? error).slice(0, 200)}` : String(error?.message ?? error) };
  if (status === 500 && !error?.expose) body.code = "internal";
  for (const key of ["code", "hint", "details", "candidates"]) if (error?.[key] !== undefined) body[key] = error[key];
  return { status, body };
}

/** The two routes the MCP bridge talks to. Returns { catalog(), tools, call, install(app) }; `install` mounts them on an Express app
 * (after express.json()). `tools` is the app's list (`{ name, description, annotations, schema, timeoutMs?, run? }`); `z` (zod) or
 * `toJsonSchema(tool)` turns a tool's schema into JSON Schema; `callTool(name, args)` runs one (default: parse with `tool.schema`
 * and call `tool.run(args)`); `token` / `tokenGetter` is the Bearer token; `recordCall` is family.recordCall; results are capped
 * at `capLimit` bytes (20000) unless the tool's result has a `__uncapped` marker. */
export function makeAgentRoutes({ app = "", tools = [], callTool = null, z = null, toJsonSchema = null, token = "", tokenGetter = null,
  instructions = "", capLimit = 20000, recordCall = null } = {}) {
  const index = new Map(tools.map((t) => [t.name, t]));
  const catalog = () => tools.map((t) => {
    let schema = { type: "object", properties: {} };
    if (toJsonSchema) schema = toJsonSchema(t);
    else if (z && t.schema && typeof z.toJSONSchema === "function") schema = z.toJSONSchema(t.schema, { io: "input" });
    else if (t.inputSchema) schema = t.inputSchema;
    const entry = { name: t.name, description: t.description, annotations: t.annotations ?? {}, inputSchema: schema };
    if (t.timeoutMs) entry["x-timeout-s"] = t.timeoutMs / 1000;
    return entry;
  });
  const run = callTool ?? (async (name, args) => {
    const tool = index.get(name);
    if (!tool) throw Object.assign(new Error(`Unknown tool: ${name}`), { status: 404, code: "unknown_tool", expose: true });
    const parsed = tool.schema && typeof tool.schema.parse === "function" ? tool.schema.parse(args ?? {}) : (args ?? {});
    return tool.run(parsed);
  });
  const currentToken = () => (tokenGetter ? tokenGetter() : token);

  const toolsRoute = (req, res) => res.json({ instructions, tools: catalog(), app });
  const callRoute = async (req, res) => {
    if (!checkBearer(req.headers.authorization, currentToken())) return res.status(401).json({ error: "Invalid MCP token.", code: "unauthorized" });
    const { name, arguments: args, caller } = req.body || {};
    if (typeof name !== "string" || !name) return res.status(400).json({ error: "The tool name is missing.", code: "invalid_arguments" });
    const t0 = Date.now();
    let ok = false;
    let failure = "";
    try {
      let result = await run(name, args ?? {});
      if (result === undefined || result === null || typeof result !== "object" || Array.isArray(result)) result = { result: result ?? null };
      ok = true;
      return res.json(capResult(result, capLimit));
    } catch (error) {
      const { status, body } = errorBody(error);
      failure = body.error;
      return res.status(status).json(body);
    } finally {
      if (recordCall) {
        try { recordCall(name, ok, Date.now() - t0, { caller: caller || "", error: failure }); } catch { /* the audit trail never fails a call */ }
      }
    }
  };
  return {
    catalog, tools: toolsRoute, call: callRoute,
    install(target) {
      target.get("/api/agent/tools", toolsRoute);
      target.post("/api/agent/call", callRoute);
    },
  };
}

// ------------------------------------------------------------------------------------------------ SPA and errors

const STATIC_EXTENSIONS = new Set([".js", ".mjs", ".css", ".json", ".map", ".svg", ".webmanifest", ".wasm", ".woff", ".woff2", ".ttf", ".png",
  ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".txt", ".mp3", ".mp4", ".webm", ".pdf", ".xml", ".avif"]);

/** Serve a built single-page app (the Vite `dist`): `express` is the app's own `express` import. /api/* that nothing matched is a
 * JSON 404; hashed /assets/ files are immutable, everything else no-cache (no stale index.html); a missing asset is a 404, not an
 * HTML page; client-side routes get index.html; an unbuilt folder answers 503. Call it after the API routes. */
export function installSpa(app, distDir, { express = null, apiPrefix = "/api" } = {}) {
  if (!express || typeof express.static !== "function") throw new Error("installSpa needs the app's express module: installSpa(app, dist, { express })");
  const prefix = "/" + apiPrefix.replace(/^\/+|\/+$/g, "");
  const escaped = prefix.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  app.all(new RegExp(`^${escaped}(/.*)?$`), (req, res) => res.status(404).json({ error: "Not found.", code: "not_found" }));
  const index = path.join(distDir, "index.html");
  app.use(express.static(distDir, {
    index: false,
    dotfiles: "ignore",
    setHeaders(res, file) {
      const hashed = /[-.][A-Za-z0-9_]{8,}\.[a-z0-9]+$/.test(file) && file.split(path.sep).includes("assets");
      res.setHeader("Cache-Control", hashed ? "public, max-age=31536000, immutable" : "no-cache");
    },
  }));
  app.get(/^\/assets\/.*/, (req, res) => res.status(404).json({ error: "Not found.", code: "not_found" }));
  app.get(/.*/, (req, res) => {
    if (STATIC_EXTENSIONS.has(path.extname(req.path).toLowerCase())) return res.status(404).json({ error: "Not found.", code: "not_found" });
    if (!fs.existsSync(index)) return res.status(503).json({ error: "The client is not built yet: run `npm install && npm run build`.", code: "not_built" });
    res.setHeader("Cache-Control", "no-cache");
    return res.sendFile(path.resolve(index));
  });
}

/** One JSON error envelope for the whole app ({ error, code?, hint?, details?, issues? }); install it last. */
export function installErrorHandlers(app, { log = console } = {}) {
  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, next) => {
    if (res.headersSent) return next(err);
    if (err?.type === "entity.parse.failed") return res.status(400).json({ error: "Invalid JSON.", code: "invalid_json" });
    if (err?.type === "entity.too.large") return res.status(413).json({ error: "The request is too large.", code: "too_large" });
    const { status, body } = errorBody(err);
    if (status >= 500) log.error?.(`Unhandled error on ${req.method} ${req.path}:`, err);
    return res.status(status).json(body);
  });
}

/** Listen and shut down cleanly (what Links' index.js did): SIGINT / SIGTERM stop taking requests, `onShutdown()` closes the
 * app's own things while the database is still open, the server closes, the process exits 0; after `forceMs` (15 s) it exits anyway.
 * `createApp` returns the Express app (or `{ app }`). Resolves { server, port, shutdown } once listening; rejects when it cannot
 * listen (the caller prints and sets the exit code). `exit` and `signals` are injectable for tests. */
export async function runServer({ service = "app", createApp, port, host = "127.0.0.1", onShutdown = null, forceMs = 15000, log = console,
  exit = (code) => process.exit(code), signals = true } = {}) {
  const made = await createApp();
  const app = made && typeof made.listen !== "function" && made.app ? made.app : made;
  const server = await new Promise((resolve, reject) => {
    const s = app.listen(port, host, () => resolve(s));
    s.once("error", reject);
  });
  const actual = server.address()?.port ?? port;
  let closing = null;
  const shutdown = (signal = "shutdown") => {
    if (closing) return closing;
    closing = (async () => {
      log.log?.(`Closing ${service} (${signal})…`);
      const force = setTimeout(() => exit(0), forceMs);
      force.unref?.();
      try { await onShutdown?.(signal); } catch (error) { log.error?.(`${service}: shutdown hook failed: ${error?.message ?? error}`); }
      server.closeIdleConnections?.();
      await new Promise((resolve) => server.close(resolve));
      clearTimeout(force);
      exit(0);
    })();
    return closing;
  };
  if (signals) {
    process.on("SIGINT", () => void shutdown("SIGINT"));
    process.on("SIGTERM", () => void shutdown("SIGTERM"));
  }
  return { server, port: actual, shutdown };
}

// ------------------------------------------------------------------------------------------------ MCP bridge

const LOCAL_URL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);
const OFFLINE_CODES = new Set(["ECONNREFUSED", "ENOTFOUND", "EHOSTUNREACH", "ENETUNREACH"]);
const FORWARDED = ["code", "hint", "issues", "details", "candidates", "key", "params"];

/** POST JSON to <base><pathname> with node:http (fetch gives up on a response that takes over five minutes to start). Resolves
 * { status, ok, body, raw }; rejects with an Error whose `.code` is the socket code (ECONNREFUSED, ETIMEDOUT...) and `.connected`
 * says whether the connection was established (so the request may have been processed). */
export function postJson(base, pathname, payload, { token = "", timeoutMs = 90000 } = {}) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(payload));
    let connected = false;
    const req = http.request(new URL(pathname, base), {
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": data.length, ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        const raw = Buffer.concat(chunks).toString("utf8");
        let body = null;
        try { body = JSON.parse(raw); } catch { body = null; }
        resolve({ status: res.statusCode, ok: res.statusCode >= 200 && res.statusCode < 300, body, raw: raw.slice(0, 300) });
      });
      res.on("error", (e) => reject(Object.assign(e, { connected: true })));
    });
    req.on("socket", (socket) => { if (socket.connecting) socket.once("connect", () => { connected = true; }); else connected = true; });
    req.setTimeout(timeoutMs, () => req.destroy(Object.assign(new Error(`The app did not answer within ${Math.round(timeoutMs / 1000)} s.`), { code: "ETIMEDOUT" })));
    req.on("error", (e) => reject(Object.assign(e, { connected })));
    req.end(data);
  });
}

function readLocalUrl({ baseUrl, portFile, urlFile }) {
  if (typeof baseUrl === "function") return baseUrl();
  if (baseUrl) return baseUrl;
  const file = urlFile || portFile;
  let value = "";
  if (file) { try { value = fs.readFileSync(file, "utf8").trim(); } catch { value = ""; } }
  if (/^https?:\/\//i.test(value)) return value;
  if (/^[0-9]+$/.test(value)) return `http://127.0.0.1:${value}`;
  return "";
}

/** The MCP stdio bridge of a Node app (Links' mcp.js + bridge-call.js and Ledger's outcome_unknown, once). The MCP SDK classes are
 * injected: `createBridge({ app, service, version, McpServer, StdioServerTransport, tools, instructions, baseUrl | urlFile | portFile,
 * token | tokenFile, callTimeoutMs, defaultTimeoutMs, heartbeatMs, messages })`. Each tool (`{ name, description, schema, annotations,
 * timeoutMs? }`, the schema being what registerTool takes, i.e. a zod shape or object) becomes an MCP tool that proxies to POST
 * /api/agent/call. It returns { server, handlers, call, start() }; `start()` connects the stdio transport.
 *
 * * Per-call timeout: `callTimeoutMs(name, args, tool)` or `tool.timeoutMs` or `defaultTimeoutMs` (90 s); calls longer than
 *   `heartbeatMs` (10 s) send progress notifications while the client asked for them.
 * * A tool that is not readOnly whose request was sent (connected) but got no complete answer returns `outcome_unknown`.
 * * The app's error envelope (code / hint / issues / details / candidates) is forwarded; 401 and a missing token are said plainly. */
export function createBridge({ app = "", service = "", version = "0.0.0", McpServer, StdioServerTransport, tools = [], instructions = "", baseUrl = "",
  urlFile = "", portFile = "", defaultPort = 0, token = "", tokenFile = "", callTimeoutMs = null, defaultTimeoutMs = 90000, heartbeatMs = 10000,
  messages = {}, z = null } = {}) {
  if (typeof McpServer !== "function") throw new Error("createBridge needs McpServer (from @modelcontextprotocol/sdk/server/mcp.js)");
  const title = messages.title || service || app;
  const text = {
    offline: `Open ${title} so the assistant can reach your data.`,
    noToken: `${title} is running, but this bridge has no access token${tokenFile ? ` at ${tokenFile}` : ""}.`,
    tokenRefused: `${title} refused this bridge's token${tokenFile ? ` (${tokenFile})` : ""}: it belongs to another data folder.`,
    outcomeUnknown: "No answer was received. The change may have been applied: read the current state before repeating it.",
    ...messages,
  };
  const resolveBase = () => {
    const url = readLocalUrl({ baseUrl, urlFile, portFile }) || (defaultPort ? `http://127.0.0.1:${defaultPort}` : "");
    if (!url) throw Object.assign(new Error("no URL"), { code: "ECONNREFUSED" });
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" || !LOCAL_URL_HOSTS.has(parsed.hostname)) throw new Error("The MCP bridge only connects to the local server.");
    return url;
  };
  if (typeof baseUrl === "string" && baseUrl) resolveBase();      // fail at start for a non-local fixed URL
  const readToken = () => {
    const given = typeof token === "function" ? token() : token;
    if (given) return String(given).trim();
    return fs.readFileSync(tokenFile, "utf8").trim();
  };
  const timeoutFor = (tool, args) => {
    const custom = callTimeoutMs ? Number(callTimeoutMs(tool.name, args, tool)) : NaN;
    return Number.isFinite(custom) && custom > 0 ? custom : (tool.timeoutMs || defaultTimeoutMs);
  };
  const reply = (body, isError = false) => ({ ...(isError ? { isError: true } : {}), content: [{ type: "text", text: JSON.stringify(body) }] });

  async function call(tool, args, extra) {
    const timeoutMs = timeoutFor(tool, args);
    const progressToken = extra?._meta?.progressToken;
    let beats = 0;
    const heartbeat = progressToken === undefined || !extra?.sendNotification ? null : setInterval(() => {
      extra.sendNotification({ method: "notifications/progress", params: { progressToken, progress: ++beats, message: `${tool.name} is still running…` } }).catch(() => {});
    }, heartbeatMs);
    heartbeat?.unref?.();
    try {
      let tokenValue;
      try { tokenValue = readToken(); } catch (error) {
        if (error.code === "ENOENT") return reply({ error: text.noToken, code: "no_token" }, true);
        throw error;
      }
      const response = await postJson(resolveBase(), "/api/agent/call", { name: tool.name, arguments: args }, { token: tokenValue, timeoutMs });
      const body = response.body;
      if (response.status === 401) return reply({ error: text.tokenRefused, code: "token_refused" }, true);
      if (!response.ok) {
        const out = { error: (body && body.error) || (body === null && response.raw.trim()) || `Error ${response.status}` };
        for (const key of FORWARDED) if (body && body[key] !== undefined) out[key] = body[key];
        return reply(out, true);
      }
      if (body === null) return reply({ error: `The app answered with something that is not JSON (HTTP ${response.status}).`, code: "bad_response" }, true);
      return { content: [{ type: "text", text: JSON.stringify(body) }] };
    } catch (error) {
      const offline = error.code === "ENOENT" || OFFLINE_CODES.has(error.code) || error.message === "fetch failed";
      if (!offline && error.connected && !tool.annotations?.readOnlyHint) {
        return reply({ error: text.outcomeUnknown, code: "outcome_unknown", status: "outcome_unknown", outcome_unknown: true, reconcile_action: "read_current_state_before_retry" }, true);
      }
      return reply({ error: offline ? text.offline : error.message, ...(offline ? { code: "not_running" } : {}) }, true);
    } finally {
      if (heartbeat) clearInterval(heartbeat);
    }
  }

  const server = new McpServer({ name: service || app, version }, { instructions });
  const handlers = new Map();
  for (const tool of tools) {
    let schema = tool.schema;
    if (schema === undefined && tool.inputSchema && z && typeof z.fromJSONSchema === "function") schema = z.fromJSONSchema(tool.inputSchema);
    const handler = (args, extra) => call(tool, args ?? {}, extra);
    handlers.set(tool.name, handler);
    server.registerTool(tool.name, { description: tool.description, ...(schema !== undefined ? { inputSchema: schema } : {}), annotations: tool.annotations }, handler);
  }
  return {
    server, handlers, call: (name, args, extra) => call(tools.find((t) => t.name === name), args ?? {}, extra),
    async start() {
      if (typeof StdioServerTransport !== "function") throw new Error("createBridge needs StdioServerTransport");
      await server.connect(new StdioServerTransport());
    },
  };
}
