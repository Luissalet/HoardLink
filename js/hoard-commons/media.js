// media.js — the Node twin of hoard_link/media/bins.py + subs.py, lifted from Links Hoard's server/media-tools.js, media.js (the pure
// parts), media-queue.js and transcript.js (ESM, no dependencies, Node 18+).
//
//   import { resolveTool, runProcess, buildYtdlpArgs, explainFailure, MediaQueue, subtitleText } from "./hoard-commons/media.js";
//
// Same behaviour as Links so Links can switch by import. What changed: the discovery order gained HOARD_<NAME> variables and
// $HOARD_HOME/bin (see resolveTool), killTree is awaited, yt-dlp gets --ignore-config and the JavaScript-runtime options,
// explainFailure no longer mistakes a network error for a login problem, normalizeMediaUrl refuses private addresses, messages
// are English (setLanguage("es") restores Links' Spanish ones), and the subtitle parsers/writers of the Python twin are here.
// The pure functions are checked against tests/vectors/media_subs.json and media_ytdlp.json by both languages.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const isWin = () => process.platform === "win32";

// ---------------------------------------------------------------------------
// messages (English by default; setLanguage("es") gives Links' original Spanish)
// ---------------------------------------------------------------------------

let LANG = "en";
export function setLanguage(lang) { LANG = lang === "es" ? "es" : "en"; return LANG; }
export const getLanguage = () => LANG;

const MSG = {
  en: {
    cancelled: "Download cancelled.",
    notFound: (cmd) => `The program "${cmd}" was not found.`,
    cannotRun: (cmd, why) => `Could not run ${cmd}: ${why}`,
    installFfmpeg: (how) => `Install ffmpeg (${how}) or set HOARD_FFMPEG to its path.`,
    installTool: (label, cmd, env) => `Install ${label} with: ${cmd} (or set ${env} to its path).`,
    installOther: (label, env) => `Install ${label} or set ${env} to its path.`,
    unknownTool: (tool) => `Unknown tool: ${tool}`,
    noPython: (label, hint) => `There is no Python to install ${label}. ${hint}`,
    updateFailed: (label, out) => `${label} could not be updated. ${out}`.trim(),
    updateNotHere: (tool) => `${tool} is not updated from here.`,
    detail: "Detail:",
    noFfmpeg: (hint) => `ffmpeg is missing; it is needed to merge or convert audio and video. ${hint}`,
    noVideo: "The post has no video (it is a photo or a carousel).",
    unsupported: "This address is not supported (unsupported URL). Check that the link goes to a specific video, audio or post.",
    forbidden: "The platform refused the download (403). This is usually an outdated yt-dlp: update it, or, if the video is private or age-restricted, sign in to Firefox or Chrome.",
    login: "This content needs a signed-in account (private account, age restriction or a platform limit). Sign in to Firefox or Chrome and try again, or set a cookies file in the settings. If it still fails, update yt-dlp.",
    outdated: "yt-dlp is probably out of date (platforms change often). Update it, or run: python -m pip install -U yt-dlp.",
    unavailable: "The content is not available: it is private, deleted or blocked in your country.",
    network: "Could not reach the site. Check your connection and try again.",
    generic: (tool, line, code) => `${tool} failed${line ? `: ${line}` : code != null ? ` (code ${code})` : ""}.`,
    downloading: "Downloading…",
    item: (i, n) => `Item ${i} of ${n}…`,
    pp: {
      merge: "Merging audio and video…", extractAudio: "Extracting the audio (MP3)…", metadata: "Writing metadata…",
      remux: "Repackaging the video…", move: "Saving the file…", other: "Processing…",
    },
    urlMissing: "The URL is missing.",
    urlInvalid: (u) => `"${u}" is not a valid URL.`,
    urlNotHttp: (u) => `"${u}" is not a valid http(s) URL.`,
    urlPrivate: (u) => `"${u}" points at this machine or a private network; only public pages can be downloaded.`,
  },
  es: {
    cancelled: "Descarga cancelada.",
    notFound: (cmd) => `No se encontró el programa «${cmd}».`,
    cannotRun: (cmd, why) => `No se pudo ejecutar ${cmd}: ${why}`,
    installFfmpeg: (how) => `Instala ffmpeg (${how}) o indica su ruta en HOARD_FFMPEG.`,
    installTool: (label, cmd, env) => `Instala ${label} con: ${cmd} (o indica su ruta en ${env}).`,
    installOther: (label, env) => `Instala ${label} o indica su ruta en ${env}.`,
    unknownTool: (tool) => `Herramienta desconocida: ${tool}`,
    noPython: (label, hint) => `No hay Python para instalar ${label}. ${hint}`,
    updateFailed: (label, out) => `No se pudo actualizar ${label}. ${out}`.trim(),
    updateNotHere: (tool) => `${tool} no se actualiza desde aquí.`,
    detail: "Detalle:",
    noFfmpeg: (hint) => `Falta ffmpeg, necesario para unir o convertir audio y vídeo. ${hint}`,
    noVideo: "La publicación no contiene vídeo (es una foto o un carrusel).",
    unsupported: "Esta dirección no es compatible (URL no admitida). Comprueba que el enlace lleva a un vídeo, un audio o una publicación concreta.",
    forbidden: "La plataforma rechazó la descarga (403). Suele deberse a un yt-dlp desactualizado: actualízalo (Descargas → Herramientas → Actualizar) o, si el vídeo es privado o con restricción de edad, inicia sesión en Firefox o Chrome.",
    login: "Este contenido necesita iniciar sesión (cuenta privada, restricción de edad o límite de la plataforma). Inicia sesión en Firefox o Chrome y vuelve a intentarlo, o indica un archivo de cookies en Ajustes. Si sigue fallando, actualiza yt-dlp (Descargas → Herramientas → Actualizar).",
    outdated: "Es probable que yt-dlp esté desactualizado (las plataformas cambian a menudo). Actualízalo desde Descargas → Herramientas → Actualizar, o con: python -m pip install -U yt-dlp.",
    unavailable: "El contenido no está disponible: es privado, se ha borrado o está bloqueado en tu país.",
    network: "No se pudo conectar con el sitio. Comprueba tu conexión e inténtalo de nuevo.",
    generic: (tool, line, code) => `${tool} falló${line ? `: ${line}` : code != null ? ` (código ${code})` : ""}.`,
    downloading: "Descargando…",
    item: (i, n) => `Elemento ${i} de ${n}…`,
    pp: {
      merge: "Uniendo audio y vídeo…", extractAudio: "Extrayendo el audio (MP3)…", metadata: "Escribiendo metadatos…",
      remux: "Reempaquetando el vídeo…", move: "Guardando el archivo…", other: "Procesando…",
    },
    urlMissing: "Falta la URL.",
    urlInvalid: (u) => `«${u}» no es una URL válida.`,
    urlNotHttp: (u) => `«${u}» no es una URL http(s) válida.`,
    urlPrivate: (u) => `«${u}» apunta a este equipo o a una red privada; solo se pueden descargar páginas públicas.`,
  },
};
const T = (lang) => MSG[lang || LANG] || MSG.en;

export class MediaError extends Error {
  constructor(message, { code = "MEDIA", status = 400, hint = "", detail = "" } = {}) {
    super(message);
    this.code = code;
    this.status = status;
    this.hint = hint;
    this.detail = detail;
  }
}

export const cancelledError = () => new MediaError(T().cancelled, { code: "CANCELLED", status: 409 });

// ---------------------------------------------------------------------------
// command specs
// ---------------------------------------------------------------------------

/** "node:/x/fake.js" | "/x/fake.js" | "/usr/bin/yt-dlp" | "python -m yt_dlp" → { cmd, args }. */
export function parseCommandSpec(spec) {
  let text = String(spec || "").trim();
  if (!text) return null;
  if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) text = text.slice(1, -1).trim();
  if (!text) return null;
  if (/^node:/i.test(text)) return { cmd: process.execPath, args: [text.slice(5)] };
  if (/\.(?:m?js|cjs)$/i.test(text)) return { cmd: process.execPath, args: [text] };
  const mod = text.match(/^(\S+)\s+-m\s+([\w.]+)$/);
  if (mod) return { cmd: mod[1], args: ["-m", mod[2]] };
  return { cmd: text, args: [] };
}

/** Looks for an executable called `name` in the PATH directories (.exe/.com only on Windows: .cmd shims cannot be spawned safely). */
export function which(name, { pathDirs, platform = process.platform } = {}) {
  const dirs = pathDirs || String(process.env.PATH || process.env.Path || "").split(path.delimiter).filter(Boolean);
  const exts = platform === "win32" ? [".exe", ".com"] : [""];
  for (const dir of dirs) {
    for (const ext of exts) {
      const candidate = path.join(dir, name + ext);
      try {
        if (!fs.statSync(candidate).isFile()) continue;
        if (platform !== "win32") fs.accessSync(candidate, fs.constants.X_OK);
        return candidate;
      } catch { /* next */ }
    }
  }
  return null;
}

/** $HOARD_HOME (default ~/.hoard), the folder the whole family shares. */
export function hoardHome(env = process.env) {
  const h = env.HOARD_HOME;
  if (h && h.trim()) return path.resolve(h.trim().replace(/^~(?=$|[\\/])/, os.homedir()));
  return path.join(os.homedir(), ".hoard");
}
/** $HOARD_HOME/bin — where downloaded programs live and every app looks first. */
export const binDir = (env = process.env) => path.join(hoardHome(env), "bin");

// ---------------------------------------------------------------------------
// running processes
// ---------------------------------------------------------------------------

const baseEnv = (extra) => ({ ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", ...(extra || {}) });

const isAlive = (pid) => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; } };
async function waitGone(pid, ms) {
  const end = Date.now() + ms;
  while (isAlive(pid)) {
    if (Date.now() >= end) return false;
    await new Promise((r) => setTimeout(r, 25));
  }
  return true;
}

/**
 * Kill a process and everything it started (yt-dlp spawns ffmpeg); resolves true once it is gone. `graceMs > 0` asks politely first
 * (taskkill /T, SIGTERM to the group) and forces after the grace period; the default is the immediate forced kill Links used.
 */
export async function killTree(pid, { graceMs = 0, force = true } = {}) {
  if (!pid) return true;
  if (isWin()) {
    const run = (args) => new Promise((resolve) => {
      try {
        const k = spawn("taskkill", args, { windowsHide: true, stdio: "ignore" });
        k.on("error", () => resolve());
        k.on("close", () => resolve());
      } catch { resolve(); }
    });
    if (graceMs > 0) {
      await run(["/PID", String(pid), "/T"]);
      if (await waitGone(pid, graceMs)) return true;
      if (!force) return false;
    }
    await run(["/PID", String(pid), "/T", "/F"]);
    return waitGone(pid, 2000);
  }
  const send = (sig) => {
    try { process.kill(-pid, sig); return true; } catch {
      try { process.kill(pid, sig); return true; } catch { return false; }
    }
  };
  if (graceMs > 0) {
    send("SIGTERM");
    if (await waitGone(pid, graceMs)) return true;
    if (!force) return false;
  }
  send("SIGKILL");
  return waitGone(pid, 2000);
}

function lineSplitter(onLine) {
  let buffer = "";
  return {
    push(chunk) {
      buffer += chunk;
      const parts = buffer.split(/\r\n|\n|\r/);
      buffer = parts.pop();
      for (const line of parts) if (line) onLine(line);
    },
    end() { if (buffer) onLine(buffer); buffer = ""; },
  };
}

/**
 * Run a command to completion. Resolves { code, stdout, stderr } (output tails only, 64 KB each) — it never rejects on a non-zero
 * exit. Rejects with MediaError(BINARY_MISSING) when the program cannot be started and with MediaError(CANCELLED) when `signal`
 * aborts (the whole process tree is killed first and awaited, so the next job never starts while the old one lingers).
 * `lowPriority` (default true) starts it below normal priority; `windowsHide` is always on.
 */
export function runProcess(command, args = [], { signal, onStdoutLine, onStderrLine, env, cwd, timeoutMs, onSpawn, lowPriority = true } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(cancelledError());
    let child;
    try {
      child = spawn(command.cmd, [...(command.args || []), ...args], {
        windowsHide: true,
        detached: !isWin(),
        stdio: ["ignore", "pipe", "pipe"],
        env: baseEnv(env),
        cwd,
      });
    } catch (error) {
      return reject(new MediaError(T().cannotRun(command.cmd, error.message), { code: "BINARY_MISSING" }));
    }
    if (lowPriority) { try { if (child.pid) os.setPriority(child.pid, os.constants.priority.PRIORITY_BELOW_NORMAL); } catch { /* best effort */ } }
    onSpawn?.(child);
    let stdout = "";
    let stderr = "";
    let settled = false;
    const tail = (text, chunk) => (text + chunk).slice(-65536);
    const out = lineSplitter((l) => onStdoutLine?.(l));
    const err = lineSplitter((l) => onStderrLine?.(l));
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (d) => { stdout = tail(stdout, d); out.push(d); });
    child.stderr.on("data", (d) => { stderr = tail(stderr, d); err.push(d); });

    let timer = null;
    let aborted = false;
    let force = null;
    const finish = (fn) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(force);
      signal?.removeEventListener("abort", onAbort);
      fn();
    };
    const onAbort = () => {
      aborted = true;
      // If the process tree refuses to die, do not hold the queue forever.
      force = setTimeout(() => finish(() => reject(cancelledError())), 10_000);
      force.unref?.();
      killTree(child.pid).catch(() => {});
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    if (timeoutMs) {
      timer = setTimeout(() => { killTree(child.pid).catch(() => {}); }, timeoutMs);
      timer.unref?.();
    }
    child.on("error", (error) => finish(() => reject(error.code === "ENOENT"
      ? new MediaError(T().notFound(command.cmd), { code: "BINARY_MISSING" })
      : new MediaError(T().cannotRun(command.cmd, error.message), { code: "BINARY_MISSING" }))));
    child.on("close", (code) => {
      out.end(); err.end();
      finish(() => (aborted || signal?.aborted ? reject(cancelledError()) : resolve({ code, stdout, stderr })));
    });
  });
}

/** Like runProcess but never rejects: failures come back as { code: null, error }. */
export async function runCapture(command, args, options = {}) {
  try {
    return await runProcess(command, args, { timeoutMs: 20_000, ...options });
  } catch (error) {
    return { code: null, stdout: "", stderr: "", error };
  }
}

// ---------------------------------------------------------------------------
// finding the tools
// ---------------------------------------------------------------------------

export const INSTALL_COMMAND = "python -m pip install -U yt-dlp gallery-dl";

// `legacy`: the per-app variables the old copies read (still honoured, after HOARD_<NAME>).
const DEFS = {
  ytdlp: { label: "yt-dlp", bin: "yt-dlp", module: "yt_dlp", pip: "yt-dlp", versionArgs: ["--version"], legacy: ["LINKS_YTDLP", "COOKHOARD_YTDLP"] },
  gallerydl: { label: "gallery-dl", bin: "gallery-dl", module: "gallery_dl", pip: "gallery-dl", versionArgs: ["--version"], legacy: ["LINKS_GALLERYDL"] },
  ffmpeg: { label: "ffmpeg", bin: "ffmpeg", versionArgs: ["-version"], legacy: ["LINKS_FFMPEG", "LUMIERE_FFMPEG", "COOKHOARD_FFMPEG", "PROSPERO_FFMPEG", "IMAGEIO_FFMPEG_EXE"] },
  ffprobe: { label: "ffprobe", bin: "ffprobe", versionArgs: ["-version"], legacy: ["LUMIERE_FFPROBE", "COOKHOARD_FFPROBE", "PROSPERO_FFPROBE"] },
  piper: { label: "piper", bin: "piper", versionArgs: ["--help"], anyExit: true, legacy: ["PROSPERO_PIPER"] },
  node: { label: "Node.js", bin: "node", versionArgs: ["--version"], legacy: [] },
};
export const TOOL_NAMES = Object.keys(DEFS);
export const LEGACY_ENV = Object.fromEntries(TOOL_NAMES.map((n) => [n, [...DEFS[n].legacy]]));

const firstLine = (text) => String(text || "").split(/\r?\n/).map((l) => l.trim()).find(Boolean) || "";

function parseVersion(tool, stdout, stderr) {
  const text = stdout || stderr || "";
  const line = firstLine(text);
  if (tool === "ffmpeg" || tool === "ffprobe") return (text.match(/(?:ffmpeg|ffprobe) version (\S+)/i) || [])[1] || line;
  return line.replace(/^Python\s+/i, "");
}

export function installHint(tool, platform = process.platform) {
  const m = T();
  const env = `HOARD_${String(tool).toUpperCase()}`;
  if (tool === "ffmpeg" || tool === "ffprobe") {
    const how = platform === "win32" ? "winget install Gyan.FFmpeg" : platform === "darwin" ? "brew install ffmpeg" : "sudo apt install ffmpeg";
    return m.installFfmpeg(how);
  }
  if (tool === "node") {
    const how = platform === "win32" ? "winget install OpenJS.NodeJS.LTS" : platform === "darwin" ? "brew install node" : "sudo apt install nodejs";
    return m.installOther(`Node.js (${how})`, env);
  }
  if (tool === "piper") return m.installOther("Piper (https://github.com/rhasspy/piper/releases)", env);
  const name = DEFS[tool]?.label || tool;
  return m.installTool(name, INSTALL_COMMAND, env);
}

function pythonSpecs(env) {
  const specs = [];
  if (env.PYTHON && env.PYTHON.trim()) specs.push({ spec: env.PYTHON, how: "env" });
  if (process.platform === "win32") specs.push({ spec: "python", how: "path" }, { spec: "py -3", how: "path" });
  else specs.push({ spec: "python3", how: "path" }, { spec: "python", how: "path" });
  return specs;
}

function pythonCommand(spec) {
  if (spec === "py -3") return { cmd: "py", args: ["-3"] };
  return parseCommandSpec(spec);
}

const cache = new Map();
const TTL_FOUND = 30_000;
const TTL_MISSING = 4_000;

function envKey(env, tool, extra) {
  const names = tool ? [`HOARD_${tool.toUpperCase()}`, ...DEFS[tool].legacy, ...(extra || [])] : [];
  return JSON.stringify([names.map((n) => env[n]), env.PYTHON, env.PATH, env.Path, env.HOARD_HOME, env.LOCALAPPDATA]);
}

export function resetToolsCache() { cache.clear(); }

/** The Python interpreter, or null. */
export async function resolvePython(env = process.env) {
  const key = `python:${envKey(env, null)}`;
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < (hit.value ? TTL_FOUND : TTL_MISSING)) return hit.value;
  let value = null;
  for (const { spec, how } of pythonSpecs(env)) {
    const command = pythonCommand(spec);
    if (!command) continue;
    const r = await runCapture(command, ["--version"], { timeoutMs: 15_000, env });
    if (r.code === 0) {
      value = { command, path: spec, version: firstLine(r.stdout || r.stderr).replace(/^Python\s+/i, ""), how };
      break;
    }
  }
  cache.set(key, { at: Date.now(), value });
  return value;
}

function fileIn(dir, bin) {
  const exe = bin + (isWin() ? ".exe" : "");
  const p = path.join(dir, exe);
  try { if (fs.statSync(p).isFile()) return p; } catch { /* absent */ }
  return null;
}

function candidatesFor(tool, env, { extraDirs = [], siblingDir = null, legacyEnv = [], ffmpegDir = null } = {}) {
  const def = DEFS[tool];
  const list = [];
  const seen = new Set();
  const add = (c) => { const k = `${c.command.cmd}\u0000${(c.command.args || []).join("\u0000")}`; if (!seen.has(k)) { seen.add(k); list.push(c); } };
  for (const name of [`HOARD_${tool.toUpperCase()}`, ...def.legacy, ...legacyEnv]) {
    const value = env[name];
    if (!value || !value.trim()) continue;
    const command = parseCommandSpec(value);
    if (command) add({ how: "env", source: name, command, display: value.trim() });
  }
  if (ffmpegDir) { const p = fileIn(ffmpegDir, def.bin); if (p) add({ how: "sibling", source: ffmpegDir, command: { cmd: p, args: [] }, display: p }); }
  const home = fileIn(binDir(env), def.bin);
  if (home) add({ how: "hoard-bin", source: binDir(env), command: { cmd: home, args: [] }, display: home });
  for (const dir of extraDirs) { const p = dir && fileIn(dir, def.bin); if (p) add({ how: "extra", source: dir, command: { cmd: p, args: [] }, display: p }); }
  if (siblingDir) { const p = fileIn(siblingDir, def.bin); if (p) add({ how: "sibling", source: siblingDir, command: { cmd: p, args: [] }, display: p }); }
  const pathDirs = String(env.PATH ?? env.Path ?? "").split(path.delimiter).filter(Boolean);
  const onPath = which(def.bin, { pathDirs });
  if (onPath) add({ how: "path", source: "", command: { cmd: onPath, args: [] }, display: onPath });
  if (isWin() && env.LOCALAPPDATA) {
    const links = path.join(env.LOCALAPPDATA, "Microsoft", "WinGet", "Links");
    const p = fileIn(links, def.bin);
    if (p) add({ how: "winget", source: links, command: { cmd: p, args: [] }, display: p });
    const apps = path.join(env.LOCALAPPDATA, "Microsoft", "WindowsApps");
    const q = fileIn(apps, def.bin);
    if (q) add({ how: "system", source: apps, command: { cmd: q, args: [] }, display: q });
  }
  if (!isWin()) {
    for (const dir of ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]) {
      const p = fileIn(dir, def.bin);
      if (p) add({ how: "system", source: dir, command: { cmd: p, args: [] }, display: p });
    }
  }
  if (tool === "node") add({ how: "self", source: "", command: { cmd: process.execPath, args: [] }, display: process.execPath });
  return list;
}

/**
 * Find one tool: HOARD_<NAME> → the legacy per-app variables (LINKS_YTDLP, LINKS_FFMPEG, LUMIERE_FFMPEG, COOKHOARD_FFMPEG …, plus
 * `legacyEnv`) → (ffprobe: next to the ffmpeg that was found) → $HOARD_HOME/bin → `extraDirs` / `siblingDir` → PATH → WinGet links (Windows) → usual folders → `python -m yt_dlp` /
 * `gallery_dl` → imageio-ffmpeg (through Python). An override may be a path, a command that runs through Node ("node:/x/fake.js", any
 * .js/.mjs/.cjs) or "python -m module". Every candidate is run with its version flag and skipped unless it exits 0.
 * Returns { tool, found, how, path, version, command, tried, error, hint, source }; cached 30 s found / 4 s missing; `refresh` skips the cache.
 */
export async function resolveTool(tool, { env = process.env, extraDirs = [], siblingDir = null, legacyEnv = [], refresh = false } = {}) {
  const def = DEFS[tool];
  if (!def) throw new Error(T().unknownTool(tool));
  const key = `${tool}:${envKey(env, tool, legacyEnv)}:${JSON.stringify([extraDirs, siblingDir])}`;
  const hit = cache.get(key);
  if (!refresh && hit && Date.now() - hit.at < (hit.value.found ? TTL_FOUND : TTL_MISSING)) return hit.value;

  const tried = [];
  let value = null;
  const attempt = async (c) => {
    const r = await runCapture(c.command, def.versionArgs, { timeoutMs: 20_000, env });
    const ok = !r.error && r.code !== null && (r.code === 0 || def.anyExit);
    if (ok) {
      return { tool, found: true, how: c.how, source: c.source || "", path: c.display, version: parseVersion(tool, r.stdout, r.stderr), command: c.command, tried, error: null, hint: null };
    }
    tried.push({ how: c.how, path: c.display, error: r.error ? r.error.message : (firstLine(r.stderr) || `exited with code ${r.code}`) });
    return null;
  };
  let ffmpegDir = null;
  if (tool === "ffprobe") {
    const ff = await resolveTool("ffmpeg", { env, extraDirs, siblingDir, refresh });
    if (ff.found && !ff.command.args.length && path.isAbsolute(ff.command.cmd)) ffmpegDir = path.dirname(ff.command.cmd);
  }
  for (const c of candidatesFor(tool, env, { extraDirs, siblingDir, legacyEnv, ffmpegDir })) {
    value = await attempt(c);
    if (value) break;
  }
  if (!value && def.module) {
    const python = await resolvePython(env);
    if (python) {
      const command = { cmd: python.command.cmd, args: [...python.command.args, "-m", def.module] };
      value = await attempt({ how: "python-module", command, display: `${python.path} -m ${def.module}` });
    }
  }
  if (!value && tool === "ffmpeg") {
    const python = await resolvePython(env);
    if (python) {
      const r = await runCapture(python.command, ["-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"], { timeoutMs: 20_000, env });
      const exe = firstLine(r.stdout);
      if (r.code === 0 && exe) value = await attempt({ how: "imageio", command: { cmd: exe, args: [] }, display: exe });
    }
  }
  if (!value) {
    const hint = installHint(tool);
    value = { tool, found: false, how: null, source: "", path: null, version: null, command: null, tried, error: hint, hint };
  }
  cache.set(key, { at: Date.now(), value });
  return value;
}

const publicTool = (t) => ({
  found: t.found,
  path: t.path,
  version: t.version,
  how: t.how,
  ...(t.found && t.source ? { source: t.source } : {}),
  ...(t.found ? {} : { hint: t.error }),
  ...(t.tried?.length ? { tried: t.tried } : {}),
});

/** What was found, for a settings page / GET /api/media/tools. */
export async function toolsStatus(options = {}) {
  const names = options.tools || TOOL_NAMES;
  const found = await Promise.all(names.map((n) => resolveTool(n, options)));
  const python = await resolvePython(options.env || process.env);
  return {
    ...Object.fromEntries(names.map((n, i) => [n, publicTool(found[i])])),
    python: python ? { found: true, path: python.path, version: python.version } : { found: false },
    bin_dir: binDir(options.env || process.env),
    install_command: INSTALL_COMMAND,
    platform: process.platform,
  };
}

// ---------------------------------------------------------------------------
// updating
// ---------------------------------------------------------------------------

const tailOf = (text, lines = 6) => String(text || "").trim().split(/\r?\n/).slice(-lines).join("\n").slice(-600);

async function pipInstall(python, pkg) {
  const r = await runCapture(python.command, ["-m", "pip", "install", "-U", "--disable-pip-version-check", pkg], { timeoutMs: 300_000 });
  return { ok: r.code === 0, output: tailOf(r.stdout + "\n" + r.stderr) || (r.error ? r.error.message : "") };
}

const RELEASES = {
  ytdlp: {
    base: "https://github.com/yt-dlp/yt-dlp/releases/latest/download/",
    asset: { win32: "yt-dlp.exe", darwin: "yt-dlp_macos", linux: "yt-dlp_linux" },
    sums: "SHA2-256SUMS",
  },
};

/** Download the standalone yt-dlp release into $HOARD_HOME/bin, checking the published SHA-256 list unless `verify` is false. */
export async function downloadRelease(tool = "ytdlp", { env = process.env, fetchImpl = globalThis.fetch, verify = true } = {}) {
  const rel = RELEASES[tool];
  const asset = rel?.asset[process.platform] || (rel && process.platform !== "win32" && process.platform !== "darwin" ? rel.asset.linux : null);
  if (!rel || !asset) return { ok: false, method: "download", output: `there is no standalone release of ${DEFS[tool]?.label || tool} for this platform` };
  const method = `download ${asset}`;
  try {
    const res = await fetchImpl(rel.base + asset, { redirect: "follow" });
    if (!res.ok) return { ok: false, method, output: `download failed: HTTP ${res.status}` };
    const data = Buffer.from(await res.arrayBuffer());
    const { createHash } = await import("node:crypto");
    const digest = createHash("sha256").update(data).digest("hex");
    if (verify && rel.sums) {
      const sres = await fetchImpl(rel.base + rel.sums, { redirect: "follow" });
      if (!sres.ok) return { ok: false, method, output: `could not fetch the checksum list (HTTP ${sres.status}); pass verify: false to skip the check` };
      const expected = Object.fromEntries((await sres.text()).split(/\r?\n/).map((l) => l.trim().split(/\s+/)).filter((p) => p.length >= 2).map((p) => [p[p.length - 1].replace(/^\*/, ""), p[0].toLowerCase()]));
      if (expected[asset] !== digest) return { ok: false, method, output: `checksum mismatch for ${asset}: expected ${expected[asset]}, got ${digest}` };
    }
    const dir = binDir(env);
    fs.mkdirSync(dir, { recursive: true });
    const target = path.join(dir, DEFS[tool].bin + (isWin() ? ".exe" : ""));
    const part = `${target}.${process.pid}.part`;
    fs.writeFileSync(part, data, { mode: 0o755 });
    for (let attempt = 0; ; attempt++) {
      try { fs.renameSync(part, target); break; } catch (e) {
        if (e.code !== "EPERM" && e.code !== "EBUSY" || attempt >= 5) { try { fs.rmSync(part, { force: true }); } catch { /* gone */ } throw e; }
        await new Promise((r) => setTimeout(r, 400));
      }
    }
    return { ok: true, method, output: `saved ${target} (sha256 ${digest.slice(0, 12)}…)` };
  } catch (error) {
    return { ok: false, method, output: `download failed: ${error.message}` };
  }
}

/**
 * Update yt-dlp and gallery-dl: `-U` for a standalone yt-dlp, `python -m pip install -U` when it runs as a module (or is missing and
 * Python exists), the release download into $HOARD_HOME/bin when there is no Python (yt-dlp only). One entry per tool with the
 * versions before and after.
 */
export async function updateTools({ tools = ["ytdlp", "gallerydl"], env = process.env, siblingDir, extraDirs, fetchImpl, verify = true } = {}) {
  const results = [];
  const options = { env, ...(siblingDir ? { siblingDir } : {}), ...(extraDirs ? { extraDirs } : {}), refresh: true };
  for (const tool of tools) {
    const def = DEFS[tool];
    if (!def || !def.pip) { results.push({ tool, ok: false, error: T().updateNotHere(tool) }); continue; }
    const before = await resolveTool(tool, options);
    const python = await resolvePython(env);
    let method = "";
    let output = "";
    let ok = false;
    if (before.found && before.how !== "python-module" && tool === "ytdlp") {
      method = "self-update (-U)";
      const r = await runCapture(before.command, ["-U"], { timeoutMs: 180_000 });
      output = tailOf(r.stdout + "\n" + r.stderr) || (r.error ? r.error.message : "");
      ok = r.code === 0 && !r.error;
      if (!ok && python && /pip|package manager|installed (?:by|via|from|with)/i.test(output)) {
        method = `${python.path} -m pip install -U ${def.pip}`;
        ({ ok, output } = await pipInstall(python, def.pip));
      }
    } else if (python) {
      method = `${python.path} -m pip install -U ${def.pip}`;
      ({ ok, output } = await pipInstall(python, def.pip));
    } else if (tool === "ytdlp") {
      ({ ok, method, output } = await downloadRelease(tool, { env, fetchImpl, verify }));
    } else {
      results.push({ tool, ok: false, before: before.version, after: before.version, updated: false, error: T().noPython(def.label, installHint(tool)) });
      continue;
    }
    resetToolsCache();
    const after = await resolveTool(tool, options);
    results.push({
      tool, ok, method, before: before.version, after: after.version,
      updated: !!after.version && after.version !== before.version,
      output,
      ...(ok ? {} : { error: T().updateFailed(def.label, output) }),
    });
  }
  return { results };
}

// ---------------------------------------------------------------------------
// platforms, formats, quality, arguments
// ---------------------------------------------------------------------------

const PLATFORMS = [
  ["YouTube", ["youtube.com", "youtu.be", "youtube-nocookie.com"]],
  ["X (Twitter)", ["twitter.com", "x.com", "t.co", "fxtwitter.com", "vxtwitter.com", "fixupx.com"]],
  ["Instagram", ["instagram.com", "instagr.am"]],
  ["TikTok", ["tiktok.com"]],
  ["Audiomack", ["audiomack.com"]],
  ["SoundCloud", ["soundcloud.com", "snd.sc"]],
  ["Vimeo", ["vimeo.com"]],
  ["Twitch", ["twitch.tv"]],
  ["Reddit", ["reddit.com", "redd.it"]],
  ["Facebook", ["facebook.com", "fb.watch", "fb.com"]],
  ["Bilibili", ["bilibili.com", "b23.tv"]],
  ["Dailymotion", ["dailymotion.com", "dai.ly"]],
  ["Bandcamp", ["bandcamp.com"]],
  ["Pinterest", ["pinterest.com", "pin.it"]],
  ["Threads", ["threads.net"]],
];
export const OTHER_PLATFORM = "Other (yt-dlp)";
export const PLATFORM_NAMES = PLATFORMS.map(([name]) => name);
const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/** Label for the site a URL belongs to; unknown sites are still attempted by yt-dlp. `other` renames the fallback label. */
export function detectPlatform(url, { other = OTHER_PLATFORM } = {}) {
  let host = "";
  try { host = new URL(SCHEME.test(String(url).trim()) ? String(url).trim() : `https://${String(url).trim()}`).hostname.toLowerCase(); } catch { return other; }
  for (const [label, domains] of PLATFORMS) {
    if (domains.some((d) => host === d || host.endsWith(`.${d}`))) return label;
  }
  return other;
}

export const isKnownPlatform = (url) => detectPlatform(url) !== OTHER_PLATFORM;

export const FORMATS = ["auto", "video", "audio", "image"];
export const QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"];
export const STATUSES = ["queued", "downloading", "processing", "done", "failed", "cancelled"];
export const ACTIVE_STATUSES = ["queued", "downloading", "processing"];
export const COOKIE_BROWSERS = ["firefox", "chrome", "edge", "brave", "chromium", "vivaldi", "opera"];
export const OUTPUT_TEMPLATE = "%(title).120s [%(id)s].%(ext)s";
export const DEFAULT_MAX_ITEMS = 50;
/** yt-dlp builds from this one on need a JavaScript runtime for YouTube; older ones reject --js-runtimes. */
export const JS_RUNTIME_MIN_VERSION = "2025.11.12";

const VIDEO_EXT = new Set([".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts"]);
const AUDIO_EXT = new Set([".mp3", ".m4a", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac", ".wma"]);
const IMAGE_EXT = new Set([".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif", ".bmp", ".tiff"]);
export const PARTIAL = /(?:\.part(?:-Frag\d+)?|\.ytdl|\.temp(?:\.\w+)?|\.f(?:\d+|hls|dash|http)[\w-]*(?:\.\w+)?(?:\.part)?|\.lh-h264\.mp4|\.hoard-tmp\.mp4)$/i;

export function fileKind(name) {
  const ext = path.extname(String(name)).toLowerCase();
  if (VIDEO_EXT.has(ext)) return "video";
  if (AUDIO_EXT.has(ext)) return "audio";
  if (IMAGE_EXT.has(ext)) return "image";
  return "other";
}

const MIME = {
  ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
  ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac", ".aac": "audio/aac",
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif",
};
export const mimeOf = (file) => MIME[path.extname(file).toLowerCase()] || "application/octet-stream";

/** yt-dlp -f expression: H.264 + AAC first (plays everywhere), then anything. */
export function videoSelector(quality = "best", hasFfmpeg = true) {
  const h = /^\d+$/.test(String(quality)) ? `[height<=${quality}]` : "";
  if (!hasFfmpeg) return `b[ext=mp4]${h}/b${h}/b`;
  return [`bv*[vcodec^=avc1]${h}+ba[ext=m4a]`, `b[ext=mp4]${h}`, `bv*${h}+ba`, `b${h}`, "b"].join("/");
}

/** One cookie attempt as yt-dlp arguments. */
export function cookieArgs(attempt) {
  if (!attempt || attempt.type === "none") return [];
  if (attempt.type === "file") return ["--cookies", attempt.path];
  return ["--cookies-from-browser", attempt.name];
}

/** The ordered cookie attempts for a request ("auto" | "none" | a browser name) and an optional cookies file. */
export function cookieAttempts(request = "auto", { cookiesFile = "", browsers = COOKIE_BROWSERS } = {}) {
  const r = String(request || "auto").trim().toLowerCase();
  if (r === "none") return [{ type: "none" }];
  if (r && r !== "auto") return [{ type: "browser", name: r }];
  if (cookiesFile) return [{ type: "file", path: cookiesFile }];
  return [{ type: "none" }, ...browsers.map((name) => ({ type: "browser", name }))];
}

const versionTuple = (v) => {
  const m = String(v ?? "").match(/(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?/);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4] || 0)] : null;
};
const cmpTuple = (a, b) => { for (let i = 0; i < 4; i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1; return 0; };

/**
 * The options every yt-dlp call starts with: --ignore-config and, when a Node.js binary is known (`node`, default this process's
 * own), --js-runtimes node:<path> --remote-components ejs:github (skipped for builds older than JS_RUNTIME_MIN_VERSION, which would
 * reject them). Pass `node: null` to leave the runtime options out.
 */
export function ytdlpBaseArgs({ node = process.execPath, version = null, ignoreConfig = true } = {}) {
  const args = ignoreConfig ? ["--ignore-config"] : [];
  const vt = versionTuple(version);
  if (node && (vt === null || cmpTuple(vt, versionTuple(JS_RUNTIME_MIN_VERSION)) >= 0)) args.push("--js-runtimes", `node:${node}`, "--remote-components", "ejs:github");
  return args;
}

export function buildYtdlpArgs({ url, format = "video", quality = "best", dir, playlist = false, maxItems = DEFAULT_MAX_ITEMS, cookie = null, hasFfmpeg = true, ffmpegPath = null, extra = [], nodePath, ytdlpVersion = null }) {
  const args = [
    ...ytdlpBaseArgs({ node: nodePath === undefined ? process.execPath : nodePath, version: ytdlpVersion }),
    "--newline", "--no-colors", "--no-warnings", "--progress", "--windows-filenames", "--no-mtime",
    "--retries", "10", "--fragment-retries", "10", "--concurrent-fragments", "4",
    ...(playlist ? ["--yes-playlist", "--playlist-end", String(maxItems)] : ["--no-playlist"]),
    "-P", dir, "-o", OUTPUT_TEMPLATE,
  ];
  if (ffmpegPath) args.push("--ffmpeg-location", ffmpegPath);
  if (format === "audio") {
    args.push("-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0", "--embed-metadata");
  } else {
    args.push("-f", videoSelector(quality, hasFfmpeg));
    if (hasFfmpeg) args.push("--merge-output-format", "mp4");
  }
  args.push(...cookieArgs(cookie));
  args.push(
    "--progress-template", "download:LHP|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress.status)s",
    "--progress-template", "postprocess:LHPP|%(progress.postprocessor)s|%(progress.status)s",
    "--print", "before_dl:LHSEL|%(format_id)s|%(playlist_index)s|%(n_entries)s|%(filename)s",
    "--print", "after_move:LHMETA|%(.{id,title,uploader,channel,upload_date,description,duration,playlist_title,filepath})j",
    ...extra,
    "--", url,
  );
  return args;
}

export function buildGalleryArgs({ url, dir, cookie = null, maxItems = DEFAULT_MAX_ITEMS }) {
  return [...cookieArgs(cookie), "--write-metadata", "--no-mtime", "--range", `1-${maxItems}`, "-D", dir, "--", url];
}

export function buildProbeArgs({ url, playlist = false, cookie = null, nodePath, ytdlpVersion = null }) {
  return [
    ...ytdlpBaseArgs({ node: nodePath === undefined ? process.execPath : nodePath, version: ytdlpVersion }),
    "--dump-single-json", "--no-warnings", "--skip-download", ...(playlist ? ["--flat-playlist"] : ["--no-playlist"]), ...cookieArgs(cookie), "--", url,
  ];
}

/** Age in days of a yt-dlp version string (YYYY.MM.DD[.N]), or null when it is not a date. `today`: "YYYY-MM-DD" or a Date (default now). */
export function ytdlpAgeDays(version, today = new Date()) {
  const m = String(version || "").match(/(\d{4})\.(\d{1,2})\.(\d{1,2})/);
  if (!m) return null;
  const built = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  let now;
  if (today instanceof Date) now = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  else { const d = String(today).match(/(\d{4})-(\d{2})-(\d{2})/); now = d ? Date.UTC(+d[1], +d[2] - 1, +d[3]) : Date.now(); }
  return Math.floor((now - built) / 86_400_000);
}

export const ytdlpStale = (version, days = 45, today = new Date()) => { const age = ytdlpAgeDays(version, today); return age !== null && age > days; };

// ---------------------------------------------------------------------------
// reading yt-dlp output
// ---------------------------------------------------------------------------

const num = (v) => {
  const s = String(v ?? "").trim();
  if (s === "" || s === "NA") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

/** One stdout line of yt-dlp as an event, or null for lines we ignore. */
export function parseYtdlpLine(line) {
  const text = String(line);
  if (text.startsWith("LHP|")) {
    const p = text.split("|");
    return { type: "progress", downloaded: num(p[1]), total: num(p[2]), estimate: num(p[3]), speed: num(p[4]), eta: num(p[5]), status: p[6] || "downloading" };
  }
  if (text.startsWith("LHPP|")) {
    const p = text.split("|");
    return { type: "pp", name: p[1] || "", status: p[2] || "" };
  }
  if (text.startsWith("LHSEL|")) {
    const p = text.split("|");
    return { type: "sel", formatId: p[1] || "", index: num(p[2]), count: num(p[3]), filename: p.slice(4).join("|") };
  }
  if (text.startsWith("LHMETA|")) {
    try { return { type: "meta", data: JSON.parse(text.slice(7)) }; } catch { return null; }
  }
  const already = text.match(/^\[download\]\s+(.+?)\s+has already been downloaded/);
  if (already) return { type: "already", path: already[1] };
  return null;
}

export function formatSpeed(bytesPerSecond) {
  const v0 = Number(bytesPerSecond);
  if (bytesPerSecond === null || bytesPerSecond === undefined || bytesPerSecond === "" || !Number.isFinite(v0) || v0 <= 0) return "";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let v = v0;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  if (v >= 100 || i === 0) return `${Math.floor(v + 0.5)} ${units[i]}`;
  const tenths = Math.floor(v * 10 + 0.5);
  return `${Math.floor(tenths / 10)}.${tenths % 10} ${units[i]}`;
}

export function formatEta(seconds) {
  const v = Number(seconds);
  if (seconds === null || seconds === undefined || seconds === "" || !Number.isFinite(v) || v < 0) return "";
  const s = Math.floor(v + 0.5);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

/** "mm:ss" or "h:mm:ss" (what the UI shows) back to seconds; null when there is none. */
export function etaSeconds(text) {
  const m = /^(?:(\d+):)?(\d{1,2}):(\d{2})$/.exec(String(text || "").trim());
  return m ? Number(m[1] || 0) * 3600 + Number(m[2]) * 60 + Number(m[3]) : null;
}

const ppKey = (name) => ({
  Merger: "merge", FFmpegMerger: "merge", ExtractAudio: "extractAudio", FFmpegExtractAudio: "extractAudio",
  Metadata: "metadata", FFmpegMetadata: "metadata", VideoRemuxer: "remux", FFmpegVideoRemuxer: "remux", MoveFiles: "move",
}[name] || "other");

/**
 * Turns the progress events of one yt-dlp run into the numbers the UI shows. A merged download has two streams (video + audio), a
 * playlist has several items; both are folded into a single 0–99 % bar (100 is set when done).
 */
export class ProgressTracker {
  constructor({ lang } = {}) {
    this.lang = lang;
    this.streams = 1;
    this.finished = 0;
    this.item = 1;
    this.items = 1;
    this.processing = false;
    this.last = 0;
    this.label = T(lang).downloading;
  }

  /** before_dl event: a new item (playlist entry) starts, with its stream count. */
  select(ev) {
    this.streams = Math.max(1, String(ev.formatId || "").split("+").length);
    this.finished = 0;
    this.item = ev.index || 1;
    this.items = ev.count || 1;
    this.processing = false;
    this.label = this.items > 1 ? T(this.lang).item(this.item, this.items) : T(this.lang).downloading;
  }

  /** Returns the patch to store, or null when nothing changes. */
  update(ev) {
    if (ev.type === "progress") {
      const total = ev.total || ev.estimate || 0;
      const pct = total > 0 ? Math.min(1, (ev.downloaded || 0) / total) : 0;
      const within = ev.status === "finished" ? 1 : pct;
      if (ev.status === "finished") this.finished += 1;
      const stream = Math.min(this.streams, ev.status === "finished" ? this.finished : this.finished + within);
      const overall = ((this.item - 1) + stream / this.streams) / this.items;
      this.last = Math.max(this.last, Math.min(99, Math.round(overall * 1000) / 10));
      return { progress: this.last, speed: formatSpeed(ev.speed), eta: formatEta(ev.eta), detail: this.processing ? undefined : this.label };
    }
    if (ev.type === "pp" && ev.status === "started") {
      this.processing = true;
      return { status: "processing", detail: T(this.lang).pp[ppKey(ev.name)], speed: "", eta: "" };
    }
    return null;
  }
}

// ---------------------------------------------------------------------------
// failures
// ---------------------------------------------------------------------------

// Keep these patterns identical to _RX in hoard_link/media/bins.py (the vectors check both).
const RX = {
  ffmpeg: /ffmpeg.*(?:not found|not installed|could not be found)|ffprobe and ffmpeg not found|requires ffmpeg|ffmpeg is required|ffmpeg or avconv/i,
  noVideo: /there is no video in this post|no video could be found|no video formats? found|does not contain (?:a )?video|no video in this (?:post|tweet)/i,
  unsupported: /unsupported url|no suitable extractor|no extractor found/i,
  // connection-level failures: never a sign-in problem (an old regex matched the bare word "age" inside "webpage")
  network: /getaddrinfo|name or service not known|nodename nor servname|temporary failure in name resolution|no address associated|failed to resolve|name resolution|network is unreachable|no route to host|connection (?:reset|refused|aborted|closed)|timed out|\btimeout\b|urlopen error|\bssl\b|certificate verify|remote end closed/i,
  strongLogin: /\bsign ?in\b|\blog ?in\b|\blogged in\b|\bprivate\b|\bage[- ]restricted|confirm your age|members[- ]only|not a bot|\bcookies\b|\bauthenticat/i,
  login: /\blog ?in\b|\bsign ?in\b|\blogged in\b|\bcookies\b|\bauthenticat|\bprivate (?:video|account|post|tweet)|not a bot|rate[- ]limit|empty media response|restricted video|\bage[- ]restricted|confirm your age|members[- ]only|requires? (?:an )?account|\bnsfw\b|protected tweet|tweet is protected|\b40[13]\b|\bforbidden\b|autherror|authrequired|too many requests|\b429\b/i,
  // YouTube and others answer 403 to an old yt-dlp's media requests: an update fixes it far more often than cookies
  blocked: /unable to download video data:? http error 403|requested format is not available|\bsabr\b|po token|signature (?:extraction|decipher)|http error 403/i,
  outdated: /no such option|unrecognized arguments|invalid (?:output )?template|unknown (?:output )?template|unsupported field|nsig extraction failed|unable to extract (?:uploader|video data|\w+ (?:data|info|player))/i,
  unavailable: /video unavailable|this video is (?:not available|unavailable|private)|has been removed|no longer available|been deleted|does not exist|http error 404|\bnot ?found\b|geo[- ]restrict|not available in your country/i,
  weakNetwork: /unable to download (?:webpage|json)/i,
};

/**
 * What went wrong in a failed yt-dlp / gallery-dl run, from its stderr: no_ffmpeg, no_video (a photo post: try gallery-dl), unsupported,
 * network, forbidden (403: usually an outdated yt-dlp), login (cookies needed), outdated, unavailable or unknown. `code` is the exit
 * code (gallery-dl bit 16 means authentication).
 */
export function classifyFailure(tool, stderr, { code = null } = {}) {
  const raw = String(stderr || "");
  if (RX.ffmpeg.test(raw)) return "no_ffmpeg";
  if (RX.noVideo.test(raw)) return "no_video";
  if (RX.unsupported.test(raw)) return "unsupported";
  if (RX.network.test(raw)) return "network";
  if (tool === "yt-dlp" && RX.blocked.test(raw) && !RX.strongLogin.test(raw)) return "forbidden";
  if (RX.login.test(raw) || (tool === "gallery-dl" && code && (code & 16))) return "login";
  if (RX.outdated.test(raw)) return "outdated";
  if (RX.unavailable.test(raw)) return "unavailable";
  if (RX.weakNetwork.test(raw)) return "network";
  return "unknown";
}

const errorLine = (text) => {
  const lines = String(text || "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const marked = lines.filter((l) => /\bERROR\b|\[error\]/i.test(l));
  return (marked.at(-1) || lines.at(-1) || "").replace(/^ERROR:\s*/i, "").slice(0, 240);
};

/**
 * Classify the stderr of a failed run and build a MediaError with a message the user can act on. The error carries the flags Links
 * used (`noVideo`, `unsupported`, `authLike`, `outdatedLike`, `fatal`) and `kind` (see classifyFailure).
 */
export function explainFailure(tool, stderr, { code = null, lang } = {}) {
  const m = T(lang);
  const raw = String(stderr || "");
  const line = errorLine(raw);
  const kind = classifyFailure(tool, raw, { code });
  const flags = { kind };
  let message;
  let status = 400;
  let errCode = "MEDIA";
  switch (kind) {
    case "no_ffmpeg": message = m.noFfmpeg(installHint("ffmpeg")); flags.fatal = true; errCode = "NO_FFMPEG"; break;
    case "no_video": message = m.noVideo; flags.noVideo = true; break;
    case "unsupported": message = m.unsupported; flags.unsupported = true; break;
    case "forbidden": message = m.forbidden; flags.authLike = true; flags.outdatedLike = true; break;
    case "login": message = m.login; flags.authLike = true; break;
    case "outdated": message = m.outdated; flags.outdatedLike = true; break;
    case "unavailable": message = m.unavailable; break;
    case "network": message = m.network; status = 502; break;
    default:
      return Object.assign(new MediaError(m.generic(tool, line, code), { status, detail: line }), flags);
  }
  if (line && !flags.noVideo) message += ` ${m.detail} ${line}`;
  return Object.assign(new MediaError(message, { status, detail: line, code: errCode }), flags);
}

// ---------------------------------------------------------------------------
// codecs: "plays everywhere" (H.264 + AAC/MP3)
// ---------------------------------------------------------------------------

export function parseCodecs(stderr) {
  const text = String(stderr || "");
  let video = null;
  for (const m of text.matchAll(/Stream #\d+:\d+[^\n]*?: Video: ([A-Za-z0-9_]+)([^\n]*)/g)) {
    if (/attached pic/i.test(m[2])) continue;
    video = { codec: m[1].toLowerCase(), rest: m[2] };
    break;
  }
  const audio = text.match(/Stream #\d+:\d+[^\n]*?: Audio: ([A-Za-z0-9_]+)/);
  return { video, audio: audio ? audio[1].toLowerCase() : null };
}

export function needsTranscode({ video, audio }) {
  if (video && (video.codec !== "h264" || /yuv420p(?:10|12)|yuv4[24]{2}p|rgb|gbr/i.test(video.rest))) return true;
  if (audio && !["aac", "mp3"].includes(audio)) return true;
  return false;
}

// ---------------------------------------------------------------------------
// URLs (with an SSRF check)
// ---------------------------------------------------------------------------

const LOCAL_SUFFIXES = [".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa"];
// [network as a 32-bit number, prefix length] — keep identical to _V4_BLOCKED in bins.py
const V4_BLOCKED = [[0, 8], [0x0A000000, 8], [0x64400000, 10], [0x7F000000, 8], [0xA9FE0000, 16], [0xAC100000, 12], [0xC0000000, 24],
  [0xC0A80000, 16], [0xC6120000, 15], [0xE0000000, 4], [0xF0000000, 4]];

const v4Blocked = (n) => V4_BLOCKED.some(([net, bits]) => Math.floor(n / 2 ** (32 - bits)) === Math.floor(net / 2 ** (32 - bits)));

function v6Groups(host) {
  let text = host;
  const dotted = text.match(/^(.*:)(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (dotted) {
    const [a, b, c, d] = dotted.slice(2).map(Number);
    text = `${dotted[1]}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const halves = text.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const fill = halves.length === 2 ? 8 - head.length - tail.length : 0;
  if (fill < 0 || (halves.length === 1 && head.length !== 8)) return null;
  const groups = [...head, ...Array(fill).fill("0"), ...tail].map((g) => (/^[0-9a-f]{1,4}$/i.test(g) ? parseInt(g, 16) : NaN));
  return groups.length === 8 && groups.every(Number.isFinite) ? groups : null;
}

function v6Blocked(g) {
  if (g.slice(0, 7).every((x) => x === 0) && (g[7] === 0 || g[7] === 1)) return true;                  // :: and ::1
  if (g.slice(0, 5).every((x) => x === 0) && g[5] === 0xffff) return v4Blocked(g[6] * 65536 + g[7]);   // ::ffff:a.b.c.d
  if (g[0] === 0x64 && g[1] === 0xff9b && g.slice(2, 6).every((x) => x === 0)) return v4Blocked(g[6] * 65536 + g[7]); // NAT64
  return (g[0] & 0xfe00) === 0xfc00 || (g[0] & 0xffc0) === 0xfe80 || (g[0] & 0xffc0) === 0xfec0 || (g[0] & 0xff00) === 0xff00;
}

function hostIsPrivate(host, isV6) {
  if (host === "localhost" || LOCAL_SUFFIXES.some((s) => host.endsWith(s))) return true;
  if (isV6) { const g = v6Groups(host); return g === null ? true : v6Blocked(g); }
  const m = host.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);   // the URL parser has already turned 2130706433 and 0x7f.1 into dotted quads
  if (m) return v4Blocked(((+m[1] * 256 + +m[2]) * 256 + +m[3]) * 256 + +m[4]);
  return false;
}

/**
 * A clean http(s) URL for a download, or a MediaError. A missing scheme becomes https://. Refused: other schemes, hosts without a dot,
 * localhost and *.local / *.internal names, and IP literals — in any spelling — that are loopback, private (10/8, 172.16/12, 192.168/16),
 * link-local, CGNAT (100.64/10), multicast or otherwise not the public internet. No DNS lookup is made, so a public name that resolves
 * to a private address is not caught here.
 */
export function normalizeMediaUrl(input) {
  const m = T();
  let text = String(input ?? "").trim();
  if (!text) throw new MediaError(m.urlMissing);
  if (!SCHEME.test(text)) text = `https://${text}`;
  let parsed;
  try { parsed = new URL(text); } catch { throw new MediaError(m.urlInvalid(input)); }
  if (!/^https?:$/.test(parsed.protocol)) throw new MediaError(m.urlNotHttp(input));
  let host = parsed.hostname.toLowerCase();
  const isV6 = host.startsWith("[");
  if (isV6) host = host.slice(1, -1);
  if (!host || !(host.includes(".") || isV6)) throw new MediaError(m.urlNotHttp(input));
  if (hostIsPrivate(host, isV6)) throw new MediaError(m.urlPrivate(input));
  return parsed.toString();
}

// ---------------------------------------------------------------------------
// FIFO queue with concurrency one and real cancellation
// ---------------------------------------------------------------------------

/**
 * cancel(id) on a waiting entry removes it without ever running it; on the running one it aborts the entry's AbortSignal and the
 * entry keeps the lane until its runner settles, so the next download never starts while the old process tree is still being killed.
 */
export class MediaQueue {
  constructor() {
    this.pending = []; // { id, run }
    this.active = null; // { id, controller, run }
    this.idleWaiters = [];
  }

  get size() { return this.pending.length + (this.active ? 1 : 0); }
  get activeId() { return this.active?.id || null; }
  get pendingIds() { return this.pending.map((e) => e.id); }
  has(id) { return this.active?.id === id || this.pending.some((e) => e.id === id); }

  /** Add a job; run(signal) must return a promise. Never rejects. */
  enqueue(id, run) {
    if (this.has(id)) return false;
    this.pending.push({ id, run });
    this.#pump();
    return true;
  }

  /** "pending" (removed, never ran), "active" (abort sent) or false. */
  cancel(id) {
    const index = this.pending.findIndex((e) => e.id === id);
    if (index >= 0) { this.pending.splice(index, 1); return "pending"; }
    if (this.active?.id === id) { this.active.controller.abort(); return "active"; }
    return false;
  }

  /** Remove everything waiting and abort the running job. Returns the ids removed from the waiting list. */
  cancelAll() {
    const waiting = this.pending.splice(0).map((e) => e.id);
    this.active?.controller.abort();
    return waiting;
  }

  /** Resolves once nothing is running or waiting. */
  idle(timeoutMs = 0) {
    if (this.size === 0) return Promise.resolve();
    return new Promise((resolve) => {
      let timer = null;
      const done = () => { clearTimeout(timer); resolve(); };
      this.idleWaiters.push(done);
      if (timeoutMs) { timer = setTimeout(done, timeoutMs); timer.unref?.(); }
    });
  }

  #pump() {
    if (this.active) return;
    const next = this.pending.shift();
    if (!next) {
      const waiters = this.idleWaiters;
      this.idleWaiters = [];
      for (const w of waiters) w();
      return;
    }
    const controller = new AbortController();
    this.active = { id: next.id, controller };
    Promise.resolve()
      .then(() => next.run(controller.signal))
      .catch(() => {})
      .finally(() => {
        this.active = null;
        this.#pump();
      });
  }
}

// ---------------------------------------------------------------------------
// subtitles (twin of hoard_link/media/subs.py; cues are { start_s, end_s, text, speaker, words } — the same wire format)
// ---------------------------------------------------------------------------

export const CUE_GAP_SECONDS = 1.2;

const halfUp = (x) => Math.floor(x + 0.5);
const asCue = (c) => ({ start_s: c.start_s ?? null, end_s: c.end_s ?? null, text: String(c.text ?? ""), speaker: String(c.speaker ?? ""), words: [...(c.words || [])] });
const cue = (start_s, end_s, text, speaker = "") => ({ start_s, end_s, text, speaker, words: [] });
const pad = (n, w = 2) => String(n).padStart(w, "0");

function fields(t) {
  const totalMs = halfUp(Math.max(0, Number(t ?? 0)) * 1000);
  const ms = totalMs % 1000;
  const totalS = Math.floor(totalMs / 1000);
  const totalM = Math.floor(totalS / 60);
  return [Math.floor(totalM / 60), totalM % 60, totalS % 60, ms];
}
export function srtTime(t) { const [h, m, s, ms] = fields(t); return `${pad(h)}:${pad(m)}:${pad(s)},${pad(ms, 3)}`; }
export function vttTime(t) { const [h, m, s, ms] = fields(t); return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(ms, 3)}`; }

function span(c, minDuration) {
  const start = Math.max(0, Number(c.start_s ?? 0));
  const end = c.end_s !== null && c.end_s !== undefined ? Number(c.end_s) : start;
  return [start, Math.max(end, start, start + minDuration)];
}
const blockText = (text) => String(text ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim().replace(/\n[ \t]*(?:\n[ \t]*)+/g, "\n");

export function toSrt(cues, { speakers = false, minDurationS = 0 } = {}) {
  const blocks = [];
  for (const raw of cues) {
    const c = asCue(raw);
    let text = blockText(c.text);
    if (!text) continue;
    if (speakers && c.speaker) text = `${c.speaker}: ${text}`;
    const [start, end] = span(c, minDurationS);
    blocks.push(`${blocks.length + 1}\n${srtTime(start)} --> ${srtTime(end)}\n${text}\n`);
  }
  return blocks.join("\n");
}

const vttEscape = (text) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function toVtt(cues, { speakers = false, minDurationS = 0 } = {}) {
  const blocks = [];
  for (const raw of cues) {
    const c = asCue(raw);
    let text = blockText(c.text);
    if (!text) continue;
    text = vttEscape(text);
    if (speakers && c.speaker) text = `<v ${vttEscape(c.speaker).replace(/\n/g, " ")}>${text}`;
    const [start, end] = span(c, minDurationS);
    blocks.push(`${vttTime(start)} --> ${vttTime(end)}\n${text}\n`);
  }
  return blocks.length ? `WEBVTT\n\n${blocks.join("\n")}` : "WEBVTT\n";
}

function lrcTime(t) {
  const cs = halfUp(Math.max(0, Number(t ?? 0)) * 100);
  return `[${pad(Math.floor(cs / 6000))}:${pad(Math.floor(cs / 100) % 60)}.${pad(cs % 100)}]`;
}

export function toLrc(cues, { metadata = null } = {}) {
  const lines = Object.entries(metadata || {}).map(([k, v]) => `[${k}:${v}]`);
  for (const raw of cues) {
    const c = asCue(raw);
    const text = c.text.replace(/\s+/g, " ").trim();
    if (text) lines.push(`${lrcTime(c.start_s)}${text}`);
  }
  return lines.join("\n") + (lines.length ? "\n" : "");
}

function clockLabel(t) {
  const total = Math.max(0, Math.floor(Number(t ?? 0)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function toTxt(cues, { timestamps = false, speakers = true } = {}) {
  const lines = [];
  for (const raw of cues) {
    const c = asCue(raw);
    const text = c.text.replace(/\s+/g, " ").trim();
    if (!text) continue;
    lines.push(`${timestamps ? `[${clockLabel(c.start_s)}] ` : ""}${speakers && c.speaker ? `${c.speaker}: ` : ""}${text}`);
  }
  return lines.join("\n") + (lines.length ? "\n" : "");
}

export const assEscape = (text) => String(text ?? "").replace(/\\/g, "/").replace(/\{/g, "(").replace(/\}/g, ")").replace(/\r/g, "").replace(/\n/g, "\\N");

export function assColor(hex, alpha = 0) {
  if (alpha && typeof alpha === "object") alpha = alpha.alpha ?? 0;
  let h = String(hex ?? "").trim().replace(/^#+/, "");
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  if (!/^[0-9a-fA-F]{6}$/.test(h)) throw new Error(`not a #RRGGBB colour: ${hex}`);
  const a = Math.max(0, Math.min(255, Math.trunc(Number(alpha))));
  return `&H${a.toString(16).padStart(2, "0")}${h.slice(4, 6)}${h.slice(2, 4)}${h.slice(0, 2)}`.toUpperCase();
}

export function assTime(t) {
  const cs = halfUp(Math.max(0, Number(t ?? 0)) * 100);
  const h = Math.floor(cs / 360000);
  const m = Math.floor((cs % 360000) / 6000);
  const s = Math.floor((cs % 6000) / 100);
  return `${h}:${pad(m)}:${pad(s)}.${pad(cs % 100)}`;
}

const CLOCK = /^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$/;
function clock(raw) {
  const m = CLOCK.exec(String(raw).trim());
  if (!m) return null;
  return Number(m[1] ?? 0) * 3600 + Number(m[2]) * 60 + Number(m[3]) + Number(m[4].padEnd(3, "0")) / 1000;
}

const ENTITIES = [["&lt;", "<"], ["&gt;", ">"], ["&quot;", '"'], ["&#39;", "'"], ["&apos;", "'"], ["&nbsp;", " "]];
function decode(text) {
  let out = text
    .replace(/&#x([0-9a-fA-F]+);/g, (_, h) => (parseInt(h, 16) <= 0x10ffff ? String.fromCodePoint(parseInt(h, 16)) : ""))
    .replace(/&#(\d+);/g, (_, d) => (Number(d) <= 0x10ffff ? String.fromCodePoint(Number(d)) : ""));
  for (const [k, v] of ENTITIES) out = out.split(k).join(v);
  return out.split("&amp;").join("&");
}
const cleanLine = (line) => decode(line.replace(/<[^>]+>/g, "")).replace(/\s+/g, " ").trim();

const VOICE = /<v(?:\.[^\s>]+)*\s+([^>]+)>/;
const HEADER = /^(?:WEBVTT|NOTE|STYLE|REGION|Kind:|Language:)/;

function parseText(content, perLine, untimed) {
  const cues = [];
  for (const block of String(content).replace(/\r/g, "").split(/\n{2,}/)) {
    const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.length || /^(?:NOTE|STYLE|REGION)\b/.test(lines[0])) continue;
    const timeAt = lines.findIndex((l) => l.includes("-->"));
    let start = null; let end = null; let speaker = "";
    const texts = [];
    let body;
    if (timeAt >= 0) {
      const at = lines[timeAt].indexOf("-->");
      start = clock(lines[timeAt].slice(0, at));
      end = clock(lines[timeAt].slice(at + 3).trim().split(/\s+/)[0] || "");
      body = lines.slice(timeAt + 1);
    } else {
      if (!untimed) continue;
      body = lines.filter((l) => !HEADER.test(l) && !/^\d+$/.test(l));
    }
    for (const ln of body) {
      const v = VOICE.exec(ln);
      if (v && !speaker) speaker = decode(v[1]).trim();
      const clean = cleanLine(ln);
      if (clean) texts.push(clean);
    }
    if (!texts.length) continue;
    if (perLine) for (const t of texts) cues.push(cue(start, end, t, speaker));
    else cues.push(cue(start, end, texts.join("\n"), speaker));
  }
  return cues;
}

export const parseSrt = (content, { perLine = false } = {}) => parseText(content ?? "", perLine, false);
export const parseVtt = (content, { perLine = false } = {}) => parseText(content ?? "", perLine, false);

const LRC_STAMP = /\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]/g;
const LRC_LINE = /^((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)\s*(.*)$/;

export function parseLrc(content, { lastS = 4.0 } = {}) {
  let offsetMs = 0;
  let entries = [];
  for (const raw of String(content ?? "").replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    const off = /^\[offset:\s*([+-]?\d+)\s*\]$/i.exec(line);
    if (off) { offsetMs = parseInt(off[1], 10); continue; }
    const m = LRC_LINE.exec(line);
    if (!m) continue;
    const text = m[2].replace(/<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>/g, "").replace(/\s+/g, " ").trim();
    for (const s of m[1].matchAll(LRC_STAMP)) {
      const frac = s[3] || "";
      entries.push([Number(s[1]) * 60 + Number(s[2]) + (frac ? Number(frac) / 10 ** frac.length : 0), text]);
    }
  }
  entries = entries.map(([t, text]) => [t - offsetMs / 1000, text]);
  entries.sort((a, b) => a[0] - b[0]);
  const cues = [];
  entries.forEach(([t, text], i) => {
    if (text) cues.push(cue(t, i + 1 < entries.length ? entries[i + 1][0] : t + lastS, text));
  });
  return cues;
}

export function parseJson3(content, { dedupe = false } = {}) {
  let data;
  try { data = JSON.parse(String(content ?? "").trim()); } catch { return []; }
  const events = data && typeof data === "object" && !Array.isArray(data) ? data.events : null;
  const out = [];
  for (const e of Array.isArray(events) ? events : []) {
    if (!e || typeof e !== "object") continue;
    const text = (Array.isArray(e.segs) ? e.segs : []).map((x) => (x && typeof x === "object" ? String(x.utf8 ?? "") : "")).join("").replace(/\s+/g, " ").trim();
    if (!text) continue;
    const start = typeof e.tStartMs === "number" ? e.tStartMs / 1000 : null;
    const end = start !== null && typeof e.dDurationMs === "number" ? start + e.dDurationMs / 1000 : start;
    out.push(cue(start, end, text));
  }
  return dedupe ? dedupeRolling(out) : out;
}

/** Collapse the repeats of rolling auto-captions (each line is shown twice; cues often continue the previous one). */
export function dedupeRolling(cues) {
  const out = [];
  for (const raw of cues) {
    const line = asCue(raw);
    const same = out.slice(-3).find((x) => x.text === line.text);
    if (same) {
      if (line.end_s !== null && (same.end_s === null || line.end_s > same.end_s)) same.end_s = line.end_s;
      continue;
    }
    const prev = out[out.length - 1];
    if (prev !== undefined && line.text.startsWith(prev.text + " ")) {
      prev.text = line.text;
      if (line.end_s !== null) prev.end_s = line.end_s;
      continue;
    }
    out.push(line);
  }
  return out;
}

/** Continuous speech from cues: a newline only where the speaker stopped (a silence longer than gapS) or a sentence ended. */
export function cuesToText(cues, gapS = CUE_GAP_SECONDS) {
  const items = cues.map(asCue);
  let out = "";
  items.forEach((c, i) => {
    if (i > 0) {
      const prev = items[i - 1];
      const gap = prev.end_s !== null && c.start_s !== null ? c.start_s - prev.end_s : 0;
      out += gap > gapS || /[.!?…]$/.test(prev.text) ? "\n" : " ";
    }
    out += c.text;
  });
  return out;
}

/** The lines of a caption file (WebVTT, SRT or json3, detected) with their timing, rolling repeats removed. */
export function subtitleCues(content) {
  const trimmed = String(content ?? "").trim();
  return dedupeRolling(trimmed.startsWith("{") ? parseJson3(trimmed) : parseText(String(content ?? ""), true, true));
}

export const subtitleText = (content, gapS = CUE_GAP_SECONDS) => cuesToText(subtitleCues(content), gapS);

/** Plain text of a WebVTT file (what Links' transcript import stores): same as subtitleText. */
export const vttText = (vtt) => subtitleText(vtt);
