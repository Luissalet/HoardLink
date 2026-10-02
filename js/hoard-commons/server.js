// server.js — the Node twin of hoard_link/{atomic,tokens,ids,net,appconfig,sqlkit,waiting}.py for the Express apps
// (Links, Ledger, People, JobHunter, Cook). ESM, no npm dependencies; the database needs Node 22.5+ (node:sqlite).
//
//   import { writeJsonAtomic, readOrCreateToken, openDatabase, newId, startBackground } from "./hoard-commons/server.js";
//
// Same rules as the Python modules (see docs/commons/plumbing.md); the functions with the same behaviour are checked
// against tests/vectors/*.json. Everything here is synchronous unless it touches the network.

import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import http from "node:http";
import crypto from "node:crypto";
import { createRequire } from "node:module";

// ------------------------------------------------------------------ atomic files

const RETRY_CODES = new Set(["EPERM", "EBUSY", "EACCES"]);

/** Block the thread for `ms` milliseconds (the apps' file code is synchronous). */
export function sleepSync(ms) {
  if (ms > 0) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** fs.renameSync that survives Windows sharing violations (EPERM / EBUSY / EACCES while another handle holds the
 * file): up to `retries` attempts with a backoff of min(250, delayMs * (1 + i)) ms; the temp file is removed when
 * every attempt fails. `rename` and `sleep` are injectable for tests. */
export function replaceWithRetry(src, dst, { retries = 40, delayMs = 50, rename = fs.renameSync, sleep = sleepSync } = {}) {
  let last = null;
  for (let i = 0; i < Math.max(1, retries); i++) {
    try { rename(src, dst); return; } catch (error) {
      if (!RETRY_CODES.has(error?.code)) throw error;
      last = error;
    }
    if (i + 1 < retries) sleep(Math.min(250, delayMs * (1 + i)));
  }
  try { fs.unlinkSync(src); } catch { /* already gone */ }
  throw last;
}

export function tmpPathFor(file) {
  const dir = path.dirname(file);
  return path.join(dir, `${path.basename(file)}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`);
}

/** Write bytes (a string is UTF-8 encoded) to a sibling temp file, fsync, and replace `file` with it. */
export function writeFileAtomic(file, data, { fsync = true, mode, retries, delayMs } = {}) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = tmpPathFor(file);
  try {
    const fd = fs.openSync(tmp, "wx", mode ?? 0o666);
    try {
      fs.writeFileSync(fd, data);
      if (fsync) fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    if (mode !== undefined) { try { fs.chmodSync(tmp, mode); } catch { /* not supported here */ } }
    replaceWithRetry(tmp, file, { retries, delayMs });
  } catch (error) {
    try { fs.unlinkSync(tmp); } catch { /* nothing to clean */ }
    throw error;
  }
}

export const writeTextAtomic = (file, text, options = {}) => writeFileAtomic(file, String(text), options);

/** Serialise first, then write: an object that cannot be serialised leaves the existing file alone. */
export function writeJsonAtomic(file, obj, { indent = 2, fsync = true, retries, delayMs } = {}) {
  const text = JSON.stringify(obj, null, indent) + "\n";
  writeFileAtomic(file, text, { fsync, retries, delayMs });
}

/** The parsed JSON file, or `def` when it is missing, empty or corrupt. Tolerates a BOM. */
export function readJson(file, def = null) {
  try {
    const raw = fs.readFileSync(file, "utf8").replace(/^﻿/, "");
    return raw.trim() ? JSON.parse(raw) : def;
  } catch { return def; }
}

// ------------------------------------------------------------------ tokens

/** The token stored at `file`; a new one (atomic, mode 0600) when it is missing, unreadable or shorter than `minLen`.
 * Stable across restarts: never regenerates a good token. */
export function readToken(file, minLen = 32) {
  try {
    const value = fs.readFileSync(file, "utf8").replace(/^﻿/, "").trim();
    return value.length >= minLen && !/\s/.test(value) ? value : null;
  } catch { return null; }
}

export function readOrCreateToken(file, minLen = 32) {
  const existing = readToken(file, minLen);
  if (existing) return existing;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const token = crypto.randomBytes(Math.max(32, Math.ceil((minLen * 3) / 4) + 1)).toString("base64url");
  if (fs.existsSync(file)) {
    const again = readToken(file, minLen);   // somebody created it between our read and now
    if (again) return again;
  } else {
    const tmp = tmpPathFor(file);
    try {
      writeFileAtomic(tmp, token, { mode: 0o600 });
      try { fs.linkSync(tmp, file); return token; } catch (error) {
        if (error?.code === "EEXIST") { const again = readToken(file, minLen); if (again) return again; }
      }
    } finally { try { fs.unlinkSync(tmp); } catch { /* gone */ } }
  }
  writeFileAtomic(file, token, { mode: 0o600 });
  return token;
}

export const writeUrl = (file, url) => writeFileAtomic(file, String(url).trim(), { fsync: false });

export function readUrl(file) {
  try { return fs.readFileSync(file, "utf8").replace(/^﻿/, "").trim() || null; } catch { return null; }
}

/** True when `header` is `Bearer <token>` (scheme case-insensitive, constant-time compare). An empty token never matches. */
export function checkBearer(header, token) {
  if (!token || !header) return false;
  const trimmed = String(header).trim();
  const space = trimmed.indexOf(" ");
  if (space < 0 || trimmed.slice(0, space).toLowerCase() !== "bearer") return false;
  const candidate = trimmed.slice(space + 1).trim();
  if (!candidate) return false;
  const a = Buffer.from(candidate, "utf8");
  const b = Buffer.from(String(token), "utf8");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// ------------------------------------------------------------------ ids

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const DECODE = new Map([...CROCKFORD].map((c, i) => [c, i]));
const RAND_MAX = (1n << 80n) - 1n;
const ULID_RE = /^[0-7][0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{25}$/;
let lastMs = -1;
let lastRand = 0n;

function encode(number, length) {
  let n = BigInt(number);
  let out = "";
  for (let i = 0; i < length; i++) { out = CROCKFORD[Number(n % 32n)] + out; n /= 32n; }
  return out;
}

const random80 = () => BigInt("0x" + crypto.randomBytes(10).toString("hex"));

/** A 26-character ULID. Without `now` (seconds since the epoch) it is strictly increasing within the process even
 * when the clock does not advance; with `now` the time is exactly that and the process state is not touched. */
export function newUlid(now) {
  if (now !== undefined && now !== null) {
    const ms = Math.max(0, Math.min(2 ** 48 - 1, Math.floor(now * 1000)));
    return encode(ms, 10) + encode(random80(), 16);
  }
  let ms = Date.now();
  let rand;
  if (ms <= lastMs) {
    ms = lastMs;
    rand = lastRand + 1n;
    if (rand > RAND_MAX) { ms += 1; rand = random80() >> 1n; }
  } else {
    rand = random80() >> 1n;
  }
  lastMs = ms; lastRand = rand;
  return encode(ms, 10) + encode(rand, 16);
}

export function newId(prefix = "", { sep = "_" } = {}) {
  const ulid = newUlid();
  return prefix ? `${prefix}${sep}${ulid}` : ulid;
}

export const shortId = (n = 8) => crypto.randomBytes(Math.ceil(Math.max(1, n) / 2)).toString("hex").slice(0, Math.max(1, n));

export const isUlid = (value) => typeof value === "string" && ULID_RE.test(value);

/** The creation time (seconds) in a ULID or `<prefix><sep><ULID>`; null for anything else. */
export function idTime(value) {
  if (typeof value !== "string" || value.length < 26) return null;
  const tail = value.slice(-26);
  if (value.length > 26 && /[0-9A-Za-z]/.test(value[value.length - 27])) return null;
  if (!isUlid(tail)) return null;
  let ms = 0;
  for (const ch of tail.slice(0, 10).toUpperCase()) ms = ms * 32 + DECODE.get(ch);
  return ms / 1000;
}

// ------------------------------------------------------------------ environment

const FALSE_WORDS = new Set(["0", "false", "no", "off"]);
const TRUE_WORDS = new Set(["1", "true", "yes", "on"]);

function opts(def) {
  return def !== null && typeof def === "object" && !Array.isArray(def) ? def : { default: def };
}

/** First of `names` (a string or an array) that is set to a non-blank value, else `default`. Second argument: the
 * default or `{ default, env }`. */
export function envStr(names, def) {
  const o = opts(def);
  const env = o.env ?? process.env;
  for (const name of Array.isArray(names) ? names : [names]) {
    const v = env[name];
    if (v !== undefined && String(v).trim()) return String(v).trim();
  }
  return o.default ?? null;
}

export function envInt(names, def) {
  const o = opts(def);
  for (const name of Array.isArray(names) ? names : [names]) {
    const raw = envStr(name, { env: o.env });
    if (raw === null) continue;
    const n = Number(raw);
    if (!Number.isFinite(n) || !Number.isInteger(n)) continue;
    let v = n;
    if (o.minimum !== undefined) v = Math.max(o.minimum, v);
    if (o.maximum !== undefined) v = Math.min(o.maximum, v);
    return v;
  }
  return o.default ?? null;
}

/** `0/false/no/off` are false, `1/true/yes/on` true; unset, blank or anything else gives the default. */
export function envFlag(name, def = false, env = process.env) {
  const o = def !== null && typeof def === "object" ? def : { default: def, env };
  const raw = envStr(name, { env: o.env ?? env });
  if (raw === null) return Boolean(o.default);
  const word = raw.toLowerCase();
  if (FALSE_WORDS.has(word)) return false;
  if (TRUE_WORDS.has(word)) return true;
  return Boolean(o.default);
}

/** `$<PREFIX>_DATA_DIR` or `<root>/data`. */
export function resolveDataDir(prefix, root, env = process.env) {
  const raw = envStr(`${String(prefix).toUpperCase()}_DATA_DIR`, { env });
  return raw ? path.resolve(raw) : path.join(root, "data");
}

// ------------------------------------------------------------------ ports and "already running"

export function validPort(value, fallback) {
  const port = Number(value);
  return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : fallback;
}

/** True when nothing listens on host:port (a bind probe; EADDRINUSE and EACCES mean no). */
export function canListen(port, host = "127.0.0.1") {
  if (validPort(port, 0) === 0) return Promise.resolve(false);
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.once("error", (error) => {
      if (error.code === "EADDRINUSE" || error.code === "EACCES") resolve(false);
      else reject(error);
    });
    probe.listen({ port, host, exclusive: true }, () => probe.close(() => resolve(true)));
  });
}

export async function findAvailablePort(preferred, { span = 20, host = "127.0.0.1" } = {}) {
  const first = validPort(preferred, 0);
  if (!first) throw new Error(`Not a port: ${preferred}`);
  const last = Math.min(65535, first + Math.max(0, span));
  for (let port = first; port <= last; port++) if (await canListen(port, host)) return port;
  throw new Error(`No free port between ${first} and ${last}.`);
}

export async function freePort(host = "127.0.0.1") {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, host, () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
  });
}

function getJson(url, timeoutMs) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: timeoutMs, agent: false }, (res) => {
      if (res.statusCode !== 200) { res.resume(); resolve(null); return; }
      const chunks = [];
      let size = 0;
      res.on("data", (c) => { size += c.length; if (size > 1_000_000) { req.destroy(); resolve(null); } else chunks.push(c); });
      res.on("end", () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); } catch { resolve(null); } });
      res.on("error", () => resolve(null));
    });
    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.on("error", () => resolve(null));
  });
}

/** True when the port is taken AND GET /api/health there answers { service }: another instance of this app is serving. */
export async function alreadyRunning(service, port, { host = "127.0.0.1", timeoutMs = 1000 } = {}) {
  if (await canListen(port, host)) return false;
  const data = await getJson(`http://${host.includes(":") ? `[${host}]` : host}:${port}/api/health`, timeoutMs);
  return Boolean(data) && typeof data === "object" && data.service === service;
}

// ------------------------------------------------------------------ waiting for jobs

export const MAX_WAIT_S = 150;
export const DONE_STATES = ["done", "error", "cancelled", "interrupted", "failed"];

export function clampWait(waitS) {
  const v = Number(waitS);
  if (waitS === null || waitS === undefined || waitS === "" || !Number.isFinite(v) || v < 0) return 0;
  return Math.min(v, MAX_WAIT_S);
}

/** Poll `getJob()` (sync or async) until its state is one of `doneStates` or clampWait(waitS) seconds pass; returns a
 * copy of the last state with `waited_s`, and `still_running: true` when it gave up. */
export async function waitFor(getJob, waitS, { poll = 0.25, doneStates = DONE_STATES, sleep = (ms) => new Promise((r) => setTimeout(r, ms)) } = {}) {
  const limit = clampWait(waitS);
  const start = Date.now();
  for (;;) {
    const job = await getJob();
    const waited = (Date.now() - start) / 1000;
    if (job === null || job === undefined) return { state: "missing", waited_s: Math.round(waited * 100) / 100 };
    const out = { ...job, waited_s: Math.round(waited * 100) / 100 };
    if (doneStates.includes(job.state ?? job.status)) return out;
    if (waited >= limit) return { ...out, still_running: true };
    await sleep(Math.max(0, Math.min(poll, limit - waited)) * 1000);
  }
}

// ------------------------------------------------------------------ background timers

/** A periodic background pass (Links / People `background.js`). `envFlag` is the name of an environment variable that
 * turns it off with 0/false/no/off. Passes never overlap, a failing `tick` is logged and the loop goes on, the timers
 * do not keep the process alive. Returns `{ name, enabled, stop() }`. */
export function startBackground({ name = "background", intervalMs, firstDelayMs = 0, tick, envFlag: flag, env = process.env, log = console.error } = {}) {
  if (typeof tick !== "function") throw new TypeError("startBackground needs a tick function");
  if (flag && !envFlagOn(flag, env)) return { name, enabled: false, stop() {} };
  let busy = false;
  const run = async () => {
    if (busy) return;
    busy = true;
    try { await tick(); } catch (error) { log(`${name}: ${error?.message ?? error}`); } finally { busy = false; }
  };
  const first = setTimeout(run, Math.max(0, firstDelayMs));
  const loop = setInterval(run, Math.max(1, intervalMs));
  first.unref?.(); loop.unref?.();
  return { name, enabled: true, stop() { clearTimeout(first); clearInterval(loop); } };
}

const envFlagOn = (flag, env) => envFlag(flag, true, env);

// ------------------------------------------------------------------ SQLite (node:sqlite)


function loadSqlite() {
  try {
    return createRequire(import.meta.url)("node:sqlite");
  } catch (error) {
    throw new Error(`openDatabase needs Node 22.5 or newer (node:sqlite is not available in ${process.version}): ${error.message}`);
  }
}

/** True when this SQLite build has FTS5. */
export function checkFts5(db) {
  const raw = db.raw ?? db;
  try {
    raw.exec("CREATE VIRTUAL TABLE IF NOT EXISTS temp.__hl_fts5_probe USING fts5(x)");
    raw.exec("DROP TABLE IF EXISTS temp.__hl_fts5_probe");
    return true;
  } catch { return false; }
}

/** Open a SQLite file (WAL, foreign keys, busy_timeout) and apply `migrations` (SQL strings or `(raw) => void`
 * functions) in order, each in its own transaction together with its `schema_version` row. Returns
 * `{ raw, run, get, all, exec, tx, schemaVersion, getSetting, setSetting, backupTo, isOpen, close }`; `tx(fn)` is
 * re-entrant (a nested call is a SAVEPOINT) and `fn` must be synchronous. */
export function openDatabase(file, { migrations = [], busyTimeoutMs = 15000, wal = true, foreignKeys = true, synchronous = "NORMAL" } = {}) {
  const { DatabaseSync } = loadSqlite();
  if (file !== ":memory:") fs.mkdirSync(path.dirname(file), { recursive: true });
  const raw = new DatabaseSync(file);
  let open = true;
  let depth = 0;
  let sp = 0;
  const sync = String(synchronous).toUpperCase();
  if (!["OFF", "NORMAL", "FULL", "EXTRA", "0", "1", "2", "3"].includes(sync)) throw new Error(`bad synchronous: ${synchronous}`);
  raw.exec(`PRAGMA busy_timeout = ${Math.max(0, Math.floor(busyTimeoutMs))}`);
  if (wal && file !== ":memory:") {
    // SQLite skips the busy handler for this pragma: retry while another process holds a fresh file
    const deadline = Date.now() + Math.max(1000, busyTimeoutMs);
    for (let delay = 10; ; delay = Math.min(200, delay * 2)) {
      try {
        const cur = raw.prepare("PRAGMA journal_mode").get();
        if (!cur || String(Object.values(cur)[0]).toUpperCase() !== "WAL") raw.exec("PRAGMA journal_mode = WAL");
        break;
      } catch (e) {
        if (!/locked|busy/i.test(String(e && e.message)) || Date.now() >= deadline) throw e;
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, delay);
      }
    }
  }
  raw.exec(`PRAGMA synchronous = ${sync}`);
  raw.exec(`PRAGMA foreign_keys = ${foreignKeys ? "ON" : "OFF"}`);

  const db = {
    raw,
    exec: (sql) => raw.exec(sql),
    run: (sql, ...params) => raw.prepare(sql).run(...params),
    get: (sql, ...params) => raw.prepare(sql).get(...params),
    all: (sql, ...params) => raw.prepare(sql).all(...params),
    isOpen: () => open,
    tx(fn) {
      const outermost = depth === 0;
      const name = `hl_sp_${++sp}`;
      raw.exec(outermost ? "BEGIN IMMEDIATE" : `SAVEPOINT ${name}`);
      depth++;
      try {
        const out = fn(raw);
        if (out && typeof out.then === "function") throw new TypeError("tx(fn): fn must be synchronous (it returned a promise)");
        raw.exec(outermost ? "COMMIT" : `RELEASE ${name}`);
        return out;
      } catch (error) {
        try { raw.exec(outermost ? "ROLLBACK" : `ROLLBACK TO ${name}; RELEASE ${name}`); } catch { /* the transaction is already gone */ }
        throw error;
      } finally { depth--; }
    },
    schemaVersion() {
      const has = raw.prepare("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").get();
      if (!has) return 0;
      return Number(raw.prepare("SELECT MAX(version) AS v FROM schema_version").get()?.v ?? 0);
    },
    getSetting(key, fallback = null) {
      raw.exec("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
      const row = raw.prepare("SELECT value FROM settings WHERE key = ?").get(key);
      if (!row) return fallback;
      try { return JSON.parse(row.value); } catch { return row.value; }
    },
    setSetting(key, value) {
      raw.exec("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
      raw.prepare("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").run(key, JSON.stringify(value));
      return value;
    },
    /** An online copy (VACUUM INTO a temp file, then moved into place). */
    backupTo(dest) {
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      const tmp = tmpPathFor(dest);
      try {
        raw.prepare("VACUUM INTO ?").run(tmp);
        replaceWithRetry(tmp, dest);
      } catch (error) { try { fs.unlinkSync(tmp); } catch { /* none */ } throw error; }
      return dest;
    },
    close() {
      if (!open) return;
      open = false;
      try { raw.exec("PRAGMA wal_checkpoint(TRUNCATE)"); } catch { /* read-only or not WAL */ }
      raw.close();
    },
  };

  try {
    raw.exec("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT)");
    const hasApplied = raw.prepare("PRAGMA table_info(schema_version)").all().some((c) => c.name === "applied_at");
    for (let i = 0; i < migrations.length; i++) {
      const index = i + 1;
      if (index <= db.schemaVersion()) continue;
      db.tx(() => {
        if (db.schemaVersion() >= index) return;       // another process got there first
        const step = migrations[i];
        if (typeof step === "function") step(raw); else raw.exec(step);
        if (hasApplied) raw.prepare("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)").run(index, new Date().toISOString());
        else raw.prepare("INSERT INTO schema_version (version) VALUES (?)").run(index);
      });
    }
  } catch (error) {
    try { raw.close(); } catch { /* ignore */ }
    open = false;
    throw error;
  }
  return db;
}

