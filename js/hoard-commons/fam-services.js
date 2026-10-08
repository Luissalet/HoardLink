// fam-services.js — the Node twin of hoard_link/{fam_media,fam_docs,fam_embed}.py: the family's media, document and embedding
// services, called through the hub (`call` of ../hoard-link.js). ESM, no npm dependencies, Node 18+.
//
//   import { mediaDownload, transcribe, docsExtract, embedTexts } from "./hoard-commons/fam-services.js";
//
//   const got = await mediaDownload("https://youtu.be/xyz", { format: "audio", sections: [[30, 75]] });
//   if (got.ok) { const t = await transcribe(got.path, { language: "auto", progress: (f) => log(f) }); ... }
//
// The contract (owners, tool names, arguments, result shapes, errors, polling) is docs/commons/services.md. The names are the Python
// ones in camelCase; options are one trailing camelCase object (`timeoutS`, not `timeoutMs`: seconds, like the Python keyword). Nothing
// throws: a failure is { ok: false, error, via, kind } with `via` = the owner app and `kind` = hub_down | app_down | app_missing |
// tool_missing | timeout | auth | tool_error | client_error (a hub that does not answer says "hub unreachable"). There are no local
// fallbacks here (the Python clients have them for speech to text, text to speech, extraction and embeddings).
//
// Long work follows the waiting rule (hoard-commons/server.js MAX_WAIT_S): the first call waits at most 150 s, then the status tool is
// polled with the same cap until `timeoutS`; past it the answer has kind "timeout" and the id, and the work goes on.

import fs from "node:fs";
import path from "node:path";
import { call, status as familyStatus } from "../hoard-link.js";

const serviceContract = JSON.parse(fs.readFileSync(new URL("./family-services.json", import.meta.url), "utf8"));
export const OWNERS = Object.freeze(Object.fromEntries(Object.entries(serviceContract.services).map(([name, spec]) => [name, spec.owner])));
export const HUB_MAX_S = 900;
export const HUB_MARGIN_S = 20;
export const MAX_WAIT_S = 150;
export const MAX_TTS_CHARS = 20000;
const CACHE_MS = 30000;
const UNAVAILABLE = new Set(["hub_down", "app_down", "app_missing", "tool_missing"]);
const DONE_STATES = ["done", "error", "cancelled", "interrupted", "failed"];
const ACTIVE_DOWNLOAD = ["queued", "downloading", "processing"];
const { media: LINKS, stt: FUNES, tts: PROSPERO, docs: KAFKA, embed: BORGES } = OWNERS;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

export function clampWait(waitS) {
  const v = Number(waitS);
  if (!Number.isFinite(v) || v < 0) return 0;
  return Math.min(v, MAX_WAIT_S);
}

function hubTimeout(seconds) {
  let v = Number(seconds);
  if (!Number.isFinite(v)) v = 120;
  return Math.max(1, Math.min(v, HUB_MAX_S));
}

function fail(kind, error, extra = {}) {
  return { ok: false, kind, error, data: null, status: null, ...extra };
}

export const isUnavailable = (res) => !res.ok && UNAVAILABLE.has(res.kind);

function errText(v) {
  if (typeof v === "string") return v;
  if (isObj(v)) for (const k of ["error", "message", "detail"]) if (v[k]) return errText(v[k]);
  return "";
}

function unknownTool(low) {
  return (low.includes("tool") && ["unknown", "not found", "no existe", "desconoc", "no such"].some((w) => low.includes(w))) || ["not found", "http 404", ""].includes(low.replace(/^[ .]+|[ .]+$/g, ""));
}

/** One tool of `owner` through the hub, classified: { ok, data, error, kind, status, ms }. */
export async function callTool(owner, tool, args = {}, { timeoutS = 120 } = {}) {
  const timeout = hubTimeout(timeoutS);
  const t0 = Date.now();
  let raw;
  try {
    raw = await call(owner, tool, args || {}, { timeoutMs: timeout * 1000 });
  } catch (e) {
    return fail("hub_down", "hub unreachable", { detail: String(e && e.message || e) });
  }
  const elapsed = (Date.now() - t0) / 1000;
  if (!isObj(raw)) return fail("tool_error", "unexpected answer from the hub");
  const st = raw.status === undefined ? null : raw.status;
  const ms = raw.ms;
  const error = errText(raw.error);
  if (st === null && !raw.ok) {
    if (elapsed >= timeout * 0.95) return fail("timeout", `timeout after ${Math.round(timeout)}s`, { ms });
    if (error.includes("hub not reachable") || !("contract" in raw)) return fail("hub_down", "hub unreachable", { detail: error });
    return fail("app_down", `${owner} unreachable`, { detail: error, ms });
  }
  if (!raw.ok) {
    const low = error.toLowerCase();
    if (st === 401) return fail("auth", error || "the hub refused this app's token", { status: 401, ms });
    if (st === 404 && low.includes("unknown app")) return fail("app_missing", `${owner} is not registered in the hub`, { status: 404, ms });
    if (st === 404 && unknownTool(low)) return fail("tool_missing", `${owner} has no tool ${tool} (update the app)`, { status: 404, detail: error, ms });
    return fail("tool_error", error || `HTTP ${st}`, { status: st, ms });
  }
  const data = raw.result;
  if (isObj(data) && data.ok === false && !("status" in data)) return fail("tool_error", errText(data) || "the tool failed", { status: st, data, ms });
  return { ok: true, kind: "", error: "", data, status: st, ms };
}

function publicError(res, via) {
  const out = { ok: false, error: String(res.error || "failed"), via, kind: String(res.kind || "tool_error") };
  if (res.detail && res.kind !== "hub_down" && res.kind !== "app_down") out.detail = res.detail;
  if (isObj(res.data)) out.result = res.data;
  return out;
}

const safe = (via, fn) => async (...a) => {
  try { return await fn(...a); } catch (e) { return { ok: false, error: `${e && e.name || "Error"}: ${e && e.message || e}`.slice(0, 300), via, kind: "client_error" }; }
};

const asPath = (p) => (p ? path.resolve(String(p)) : "");
const asRef = (p) => { const s = String(p ?? "").trim(); return /^d_[^\\/]*$/.test(s) ? s : asPath(s); };
const asRefs = (v) => (v === undefined || v === null ? [] : (Array.isArray(v) ? v : [v])).map(asRef);

function cleanArgs(o) {
  const out = {};
  for (const [k, v] of Object.entries(o)) {
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) continue;
    out[k] = v;
  }
  return out;
}

const defaultDone = (d) => { const s = isObj(d) ? d.status : undefined; return s === undefined || s === null || DONE_STATES.includes(String(s).toLowerCase()); };

function fraction(v) {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return Math.max(0, Math.min(1, v > 1 ? v / 100 : v));
}

/** Start a long job and follow it (see the file header). Returns the final callTool answer, or kind "timeout" with { data, jobId }. */
export async function runJob(owner, startTool, startArgs, statusTool, { timeoutS, idField = "job_id", idArg = "job_id", waitArg = "wait_s", statusWaitArg = "", minWait = 0,
  isDone = defaultDone, onData = null } = {}) {
  const deadline = Date.now() + Math.max(1, Number(timeoutS)) * 1000;
  let chunk = Math.max(clampWait(Math.min(MAX_WAIT_S, timeoutS)), minWait);
  let res = await callTool(owner, startTool, { ...startArgs, [waitArg]: chunk }, { timeoutS: chunk + HUB_MARGIN_S });
  for (;;) {
    if (!res.ok) return res;
    const data = res.data;
    if (isObj(data) && onData) { try { onData(data); } catch { /* a progress callback must not break the job */ } }
    if (isDone(data)) return res;
    const jobId = isObj(data) ? data[idField] : null;
    const remaining = (deadline - Date.now()) / 1000;
    if (!jobId || remaining <= 0.5) return { ...fail("timeout", `still running after ${Math.round(timeoutS)}s`, { status: res.status }), data: isObj(data) ? data : null, jobId };
    chunk = Math.max(clampWait(Math.min(MAX_WAIT_S, remaining)), minWait);
    const polled = Date.now();
    res = await callTool(owner, statusTool, { [idArg]: jobId, [statusWaitArg || waitArg]: chunk }, { timeoutS: chunk + HUB_MARGIN_S });
    if (res.ok && !isDone(res.data) && Date.now() - polled < 200) await sleep(Math.min(250, Math.max(0, deadline - Date.now())));
  }
}

// ---- availability --------------------------------------------------------------------------------------------------------------

const availCache = new Map();

/** True when the hub answers and the app that owns `service` ("media", "stt", "tts", "docs", "embed" or an app id) is running. Cached 30 s. */
export async function serviceAvailable(service, timeoutMs = 1000) {
  const owner = OWNERS[String(service || "").toLowerCase()] || String(service || "");
  const st = familyStatus();
  const key = `${st.hub}|${owner}`;
  const hit = availCache.get(key);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.ok;
  let ok = false;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    let token = "";
    try { token = fs.readFileSync(st.tokenFile, "utf8").replace(/^﻿/, "").trim(); } catch { token = ""; }
    const res = await fetch(`${st.hub}/api/apps/${encodeURIComponent(owner)}`, { headers: { Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) }, signal: ctl.signal });
    const body = res.status === 200 ? await res.json().catch(() => null) : null;
    ok = Boolean(body && body.state === "running");
  } catch { ok = false; } finally { clearTimeout(timer); }
  availCache.set(key, { at: Date.now(), ok });
  return ok;
}

export function forgetAvailability() { availCache.clear(); }

// ---- media: download from a link (Links) ----------------------------------------------------------------------------------------

const FORMATS = { auto: "auto", video: "video", audio: "audio", image: "image", mp3: "audio", mp4: "video", photo: "image", photos: "image", images: "image" };

function sectionsOf(value) {
  if (value === undefined || value === null || (Array.isArray(value) && !value.length)) return [null, ""];
  let pairs = value;
  if (!Array.isArray(pairs)) return [null, "sections must be a list of [start_s, end_s] pairs"];
  if (pairs.length === 2 && pairs.every((x) => typeof x === "number")) pairs = [pairs];
  const out = [];
  for (const pair of pairs) {
    if (!Array.isArray(pair) || pair.length < 2) return [null, "sections must be a list of [start_s, end_s] pairs"];
    const start = Number(pair[0]), end = Number(pair[1]);
    if (!(Number.isFinite(start) && Number.isFinite(end) && start >= 0 && start < end)) return [null, `bad section ${JSON.stringify(pair)}: need 0 <= start < end (seconds)`];
    out.push([start, end]);
  }
  return [out, ""];
}

function downloadView(data, error = "") {
  const view = isObj(data) ? { ...data } : {};
  if ("kind" in view) { view.media_kind = view.kind; delete view.kind; }
  let files = (Array.isArray(view.files) ? view.files : []).filter((f) => isObj(f) && f.path).map((f) => ({ ...f }));
  if (!files.length && view.path) { files = [{ path: view.path }]; if (view.status === undefined) view.status = "done"; }   // an owner that answers just { path }
  view.files = files;
  view.path = files.length ? files[0].path : null;
  const st = String(view.status || "");
  view.ok = Boolean(view.ok === undefined ? st === "done" : view.ok) && files.length > 0;
  view.via = LINKS;
  if (ACTIVE_DOWNLOAD.includes(st)) {
    Object.assign(view, { ok: false, still_running: true, kind: "timeout" });
    view.error = String(view.error || error || `still running (${st}); follow it with mediaStatus(id)`);
  } else if (!view.ok) {
    view.kind = "tool_error";
    view.error = String(view.error || error || (st === "done" ? "finished without files" : `download ${st || "failed"}`));
  }
  return view;
}

/** Download the media behind `url` with Links. Options: format (auto|video|audio|image), quality ("best" or a height), destDir, sections
 *  ([[start_s, end_s]]), maxDurationS, maxHeight, saveLink, playlist, maxItems, cookies ("auto" or a browser), wait (default true), timeoutS (150).
 *  Returns Links' view { ok, id, status, path, files: [{path, name, size, kind}], dir, title, ..., via: "links" }. */
export const mediaDownload = safe(LINKS, async (url, { format = "auto", quality = "best", destDir = null, sections = null, maxDurationS = null, maxHeight = null,
  saveLink = false, playlist = false, maxItems = 50, cookies = "auto", wait = true, timeoutS = 150 } = {}) => {
  const u = String(url ?? "").trim();
  if (!u) return { ok: false, error: "url is required", via: LINKS, kind: "client_error" };
  const [secs, bad] = sectionsOf(sections);
  if (bad) return { ok: false, error: bad, via: LINKS, kind: "client_error" };
  const dir = destDir ? asPath(destDir) : null;
  const args = cleanArgs({ url: u, format: FORMATS[String(format || "auto").trim().toLowerCase()] || String(format), quality: String(quality ?? "") || "best", dir, dest_dir: dir,
    sections: secs, max_duration_s: maxDurationS, max_height: maxHeight, save_link: Boolean(saveLink), playlist: Boolean(playlist), max_items: Math.trunc(maxItems), cookies_browser: cookies });
  return runDownload("media_download", args, wait, timeoutS);
});

/** media_download / media_audio_for_asr (same arguments and answer): start, then follow with media_status. */
async function runDownload(tool, args, wait, timeoutS) {
  if (!wait) {
    const r = await callTool(LINKS, tool, { ...args, wait: false }, { timeoutS: 30 });
    return r.ok ? downloadView(r.data) : publicError(r, LINKS);
  }
  const res = await runJob(LINKS, tool, { ...args, wait: true }, "media_status", { timeoutS: Number(timeoutS), idField: "id", idArg: "id", waitArg: "timeout_s",
    statusWaitArg: "wait_s", minWait: 5, isDone: (d) => !(isObj(d) && ACTIVE_DOWNLOAD.includes(String(d.status || ""))) });
  if (res.ok) return downloadView(res.data);
  const out = publicError(res, LINKS);
  if (res.kind === "timeout" && isObj(res.data)) {
    const v = downloadView(res.data, res.error);
    for (const [k, val] of Object.entries(v)) if (!["ok", "error", "via", "kind"].includes(k)) out[k] = val;
    out.still_running = true;
  }
  return out;
}

export const mediaStatus = safe(LINKS, async (id, { waitS = 0 } = {}) => {
  const w = clampWait(waitS);
  const r = await callTool(LINKS, "media_status", { id: String(id), wait_s: Math.trunc(w) }, { timeoutS: w + HUB_MARGIN_S });
  return r.ok ? downloadView(r.data) : publicError(r, LINKS);
});

export const mediaCancel = safe(LINKS, async (id) => {
  const r = await callTool(LINKS, "media_cancel", { id: String(id) }, { timeoutS: 30 });
  return r.ok ? downloadView(r.data) : publicError(r, LINKS);
});

export const mediaInfo = safe(LINKS, async (url) => {
  const args = { url: String(url ?? "").trim() };
  if (!args.url) return { ok: false, error: "url is required", via: LINKS, kind: "client_error" };
  let r = await callTool(LINKS, "media_info", args, { timeoutS: 120 });
  if (!r.ok && r.kind === "tool_missing") {
    r = await callTool(LINKS, "media_probe", args, { timeoutS: 120 });
    if (r.ok && isObj(r.data)) return { ok: true, thumbnail: "", subtitle_langs: [], ...r.data, partial: true, via: LINKS };
  }
  return r.ok ? { ok: true, ...(isObj(r.data) ? r.data : {}), via: LINKS } : publicError(r, LINKS);
});

export const mediaSubtitles = safe(LINKS, async (url, { langs = ["es", "en"] } = {}) => {
  const wanted = (Array.isArray(langs) ? langs : [langs]).map(String);
  const r = await callTool(LINKS, "media_subtitles", { url: String(url ?? "").trim(), langs: wanted }, { timeoutS: 120 });
  if (!r.ok) return publicError(r, LINKS);
  return { ok: true, text: "", cues: [], ...(isObj(r.data) ? r.data : {}), via: LINKS };
});

export const mediaAudioForAsr = safe(LINKS, async (url, { sections = null, timeoutS = 150 } = {}) => {
  const u = String(url ?? "").trim();
  if (!u) return { ok: false, error: "url is required", via: LINKS, kind: "client_error" };
  const [secs, bad] = sectionsOf(sections);
  if (bad) return { ok: false, error: bad, via: LINKS, kind: "client_error" };
  const got = await runDownload("media_audio_for_asr", cleanArgs({ url: u, sections: secs }), true, timeoutS);
  if (got.ok) return got;
  if (got.kind === "tool_missing") return { ...(await mediaDownload(u, { format: "audio", sections: secs, timeoutS })), converted: false };
  return got;
});

export const mediaTools = safe(LINKS, async ({ update = false } = {}) => {
  const r = await callTool(LINKS, "media_tools", { update: Boolean(update) }, { timeoutS: update ? 180 : 30 });
  return r.ok ? { ok: true, ...(isObj(r.data) ? r.data : {}), via: LINKS } : publicError(r, LINKS);
});

// ---- speech to text (Funes) ------------------------------------------------------------------------------------------------------

function normSegment(seg) {
  const s = isObj(seg) ? { ...seg } : { text: String(seg) };
  const start = s.start, end = s.end;
  delete s.start; delete s.end;
  s.start_s = Number(s.start_s ?? start) || 0;
  s.end_s = Number(s.end_s ?? end) || 0;
  s.text = String(s.text ?? "").trim();
  s.words = (Array.isArray(s.words) ? s.words : []).map((w) => {
    const x = isObj(w) ? { ...w } : { word: String(w) };
    const ws = x.start, we = x.end; delete x.start; delete x.end;
    x.start_s = Number(x.start_s ?? ws) || 0;
    x.end_s = Number(x.end_s ?? we) || 0;
    if (x.p === undefined && x.probability !== undefined) { x.p = x.probability; delete x.probability; }
    x.word = String(x.word ?? x.text ?? "");
    return x;
  });
  return s;
}

function transcriptOf(data, via) {
  const segments = (Array.isArray(data.segments) ? data.segments : []).map(normSegment);
  const text = String(data.text || "").trim() || segments.map((s) => s.text).filter(Boolean).join(" ").trim();
  const duration = Number(data.duration_s ?? data.duration) || (segments.length ? segments[segments.length - 1].end_s : 0);
  const out = { ok: true, language: String(data.language || ""), language_probability: Number(data.language_probability) || 0, duration_s: Math.round(duration * 1000) / 1000, text, segments,
    model: String(data.model || ""), device: String(data.device || ""), note: String(data.note || ""), stats: isObj(data.stats) ? data.stats : {}, via };
  if (data.job_id) out.job_id = data.job_id;
  return out;
}

const isDoneOk = (s) => ["done", "completed", "ok"].includes(String(s ?? "done").toLowerCase());

/** Speech to text through Funes. Options: language ("auto"), model, wordTimestamps, initialPrompt, vad, timeoutS (600), progress(fraction).
 *  Returns { ok, language, language_probability, duration_s, text, segments: [{start_s, end_s, text, words}], model, device, note, stats, via: "funes" }. */
export const transcribe = safe(FUNES, async (file, { language = "auto", model = null, wordTimestamps = true, initialPrompt = "", vad = true, timeoutS = 600, progress = null } = {}) => {
  const src = asPath(file);
  if (!src) return { ok: false, error: "path is required", via: FUNES, kind: "client_error" };
  const args = cleanArgs({ path: src, language: String(language || "auto"), model, word_timestamps: Boolean(wordTimestamps), initial_prompt: String(initialPrompt || ""), vad: Boolean(vad) });
  const onData = progress ? (d) => { const f = fraction(d.progress); if (f !== null) progress(f); } : null;
  const res = await runJob(FUNES, "transcribe_file", args, "transcribe_status", { timeoutS: Number(timeoutS), onData });
  if (res.ok) {
    const data = isObj(res.data) ? res.data : {};
    if (isDoneOk(data.status)) { if (progress) { try { progress(1); } catch { /* ignore */ } } return transcriptOf(data, FUNES); }
    return { ...publicError(fail("tool_error", String(data.error || `transcription ${data.status}`), { data }), FUNES), status: data.status, job_id: data.job_id };
  }
  const out = publicError(res, FUNES);
  if (res.kind === "timeout") {
    const job = isObj(res.data) ? res.data : {};
    Object.assign(out, { job_id: res.jobId || job.job_id, status: job.status || "running", still_running: true });
  }
  return out;
});

export const transcribeStatus = safe(FUNES, async (jobId, { waitS = 0 } = {}) => {
  const w = clampWait(waitS);
  const r = await callTool(FUNES, "transcribe_status", { job_id: String(jobId), wait_s: w }, { timeoutS: w + HUB_MARGIN_S });
  if (!r.ok) return publicError(r, FUNES);
  const data = isObj(r.data) ? r.data : {};
  const st = String(data.status ?? "done").toLowerCase();
  if (isDoneOk(st)) return transcriptOf(data, FUNES);
  if (DONE_STATES.includes(st)) return { ok: false, error: String(data.error || `transcription ${st}`), via: FUNES, kind: "tool_error", status: st, job_id: jobId };
  return { ok: false, error: `still running (${st})`, via: FUNES, kind: "timeout", still_running: true, status: st, job_id: jobId, progress: data.progress };
});

export const transcribeCancel = safe(FUNES, async (jobId) => {
  const r = await callTool(FUNES, "transcribe_cancel", { job_id: String(jobId) }, { timeoutS: 30 });
  if (!r.ok) return publicError(r, FUNES);
  return { ok: true, job_id: jobId, status: (isObj(r.data) && r.data.status) || "cancelled", via: FUNES };
});

// ---- text to speech (Prospero) ---------------------------------------------------------------------------------------------------

/** Read `text` aloud with Prospero. Options: voice, engine, lang, speed, timeoutS (300). Returns { ok, path, bytes, engine_id, via: "prospero" }. */
export const speak = safe(PROSPERO, async (text, { voice = null, engine = null, lang = null, speed = null, timeoutS = 300 } = {}) => {
  const body = String(text ?? "").trim();
  if (!body) return { ok: false, error: "empty text", via: PROSPERO, kind: "client_error" };
  if (body.length > MAX_TTS_CHARS) return { ok: false, error: `text too long (${body.length} characters; at most ${MAX_TTS_CHARS} per call: split it)`, via: PROSPERO, kind: "client_error" };
  const r = await callTool(PROSPERO, "voice_tts", cleanArgs({ text: body, voice, engine, lang, speed }), { timeoutS: Number(timeoutS) });
  if (!r.ok) return publicError(r, PROSPERO);
  const d = isObj(r.data) ? r.data : {};
  const p = String(d.path || "");
  if (!p) return { ok: false, error: "Prospero returned no audio path", via: PROSPERO, kind: "tool_error" };
  let size = Number.isInteger(d.bytes) ? d.bytes : 0;
  if (!size) { try { size = fs.statSync(p).size; } catch { size = 0; } }
  return { ok: true, path: p, bytes: size, engine_id: String(d.engine_id || ""), via: PROSPERO };
});

// ---- documents: PDF operations, extraction, OCR (Kafka) ---------------------------------------------------------------------------

function kafkaOk(data) {
  const out = isObj(data) ? { ...data } : { result: data };
  out.ok = true;
  out.via = KAFKA;
  if (Array.isArray(out.outputs) && !("paths" in out)) out.paths = out.outputs.filter((o) => isObj(o) && o.path).map((o) => String(o.path));
  return out;
}

async function kafka(tool, args, timeoutS) {
  const r = await callTool(KAFKA, tool, args, { timeoutS });
  return r.ok ? kafkaOk(r.data) : publicError(r, KAFKA);
}

const dirOf = (v) => (v ? asPath(v) : null);

export const pdfInfo = safe(KAFKA, (file, { password = "", timeoutS = 60 } = {}) => kafka("pdf_info", cleanArgs({ file: asRef(file), password }), timeoutS));

export const pdfMerge = safe(KAFKA, (files, { ranges = null, password = "", output = "", outDir = "", fileResult = false, timeoutS = 300 } = {}) =>
  kafka("pdf_merge", cleanArgs({ files: asRefs(files), ranges: ranges && ranges.length ? ranges : null, password, output: dirOf(output), out_dir: dirOf(outDir), file_result: fileResult || null }), timeoutS));

export const pdfSplit = safe(KAFKA, (file, { mode = "ranges", ranges = "", every = 1, password = "", outDir = "", fileResult = false, timeoutS = 300 } = {}) =>
  kafka("pdf_split", cleanArgs({ file: asRef(file), mode, ranges, every: mode === "every" ? Math.trunc(every) : null, password, out_dir: dirOf(outDir), file_result: fileResult || null }), timeoutS));

export const pdfPages = safe(KAFKA, (file, action, { pages = "", degrees = 90, order = "", password = "", output = "", outDir = "", timeoutS = 300 } = {}) =>
  kafka("pdf_pages", cleanArgs({ action, file: asRef(file), pages, degrees: action === "rotate" ? Math.trunc(degrees) : null, order, password, output: dirOf(output), out_dir: dirOf(outDir) }), timeoutS));

export const pdfCompress = safe(KAFKA, (file, { preset = "ebook", targetMb = null, engine = "auto", password = "", output = "", outDir = "", timeoutS = 600 } = {}) =>
  kafka("pdf_compress", cleanArgs({ file: asRef(file), preset, target_mb: targetMb, engine, password, output: dirOf(output), out_dir: dirOf(outDir) }), timeoutS));

export const pdfWatermark = safe(KAFKA, (file, text, { opacity = 0.3, angle = 45, fontSize = 60, color = "gris", pages = "", password = "", output = "", outDir = "", timeoutS = 300 } = {}) =>
  kafka("pdf_watermark", cleanArgs({ file: asRef(file), text, opacity, angle, font_size: fontSize, color, pages, password, output: dirOf(output), out_dir: dirOf(outDir) }), timeoutS));

export const pdfProtect = safe(KAFKA, (file, action, password, { ownerPassword = "", currentPassword = "", allowPrint = true, allowCopy = true, allowModify = true, output = "", outDir = "", timeoutS = 300 } = {}) =>
  kafka("pdf_protect", cleanArgs({ action, file: asRef(file), password, owner_password: ownerPassword, current_password: currentPassword, allow_print: Boolean(allowPrint), allow_copy: Boolean(allowCopy),
    allow_modify: Boolean(allowModify), output: dirOf(output), out_dir: dirOf(outDir) }), timeoutS));

export const pdfMetadataSet = safe(KAFKA, (file, { title, author, subject, keywords, password = "", output = "", outDir = "", timeoutS = 120 } = {}) => {
  const args = { file: asRef(file) };
  for (const [k, v] of [["title", title], ["author", author], ["subject", subject], ["keywords", keywords]]) if (v !== undefined && v !== null) args[k] = v;
  return kafka("pdf_metadata_set", { ...args, ...cleanArgs({ password, output: dirOf(output), out_dir: dirOf(outDir) }) }, timeoutS);
});

export const pdfToImages = safe(KAFKA, (file, { pages = "", format = "png", dpi = 150, quality = 90, password = "", outDir = "", timeoutS = 600 } = {}) =>
  kafka("pdf_to_images", cleanArgs({ file: asRef(file), pages, format, dpi: Math.trunc(dpi), quality: Math.trunc(quality), password, out_dir: dirOf(outDir) }), timeoutS));

export const imagesToPdf = safe(KAFKA, (images, { pageSize = "A4", marginMm = 10, orientation = "auto", output = "", outDir = "", fileResult = false, timeoutS = 300 } = {}) =>
  kafka("pdf_from_images", cleanArgs({ images: asRefs(images), page_size: pageSize, margin_mm: marginMm, orientation, output: dirOf(output), out_dir: dirOf(outDir), file_result: fileResult || null }), timeoutS));

export const pdfFromOffice = safe(KAFKA, (file, { engine = "auto", output = "", outDir = "", timeoutS = 300 } = {}) =>
  kafka("pdf_from_office", cleanArgs({ file: asRef(file), engine, output: dirOf(output), out_dir: dirOf(outDir) }), timeoutS));

export const imagesCompress = safe(KAFKA, (sources, { limitMb = null, limitKb = null, recursive = true, losslessOnly = false, skipSmall = false, outDir = "", timeLimitS = 70, timeoutS = 300 } = {}) =>
  kafka("images_compress", cleanArgs({ sources: asRefs(sources), limit_mb: limitMb, limit_kb: limitKb, recursive: Boolean(recursive), lossless_only: Boolean(losslessOnly), skip_small: Boolean(skipSmall),
    out_dir: dirOf(outDir), time_limit_s: Number(timeLimitS) }), Math.max(Number(timeoutS), Number(timeLimitS) + 30)));

function extractResult(data, via) {
  const out = isObj(data) ? { ...data } : {};
  const units = (Array.isArray(out.units) ? out.units : []).filter(isObj).map((u) => ({ ...u }));
  const text = typeof out.text === "string" ? out.text : units.map((u) => String(u.text || "")).filter(Boolean).join("\n\n");
  const result = { ...out, kind: String(out.kind || ""), title: String(out.title || ""), text, units, needs_ocr: Boolean(out.needs_ocr), notes: (Array.isArray(out.notes) ? out.notes : []).map(String),
    pages_ocr: Math.trunc(Number(out.pages_ocr) || 0), via, ok: true };
  if (out.error) return { ok: false, error: String(out.error), via, kind: "tool_error", result: { ...result, ok: false } };
  return result;
}

/** The text of any document, scans included (Kafka's doc_extract). Options: ocr (auto|off|force), maxPages (400), lang ("es"), timeoutS (900).
 *  Returns { ok, kind, title, text, units: [{kind, number, title, text}], needs_ocr, notes, pages_ocr, via: "kafka" }. */
export const docsExtract = safe(KAFKA, async (file, { ocr = "auto", maxPages = 400, lang = "es", timeoutS = 900 } = {}) => {
  const src = asPath(file);
  if (!src) return { ok: false, error: "path is required", via: KAFKA, kind: "client_error" };
  const mode = String(ocr || "auto").trim().toLowerCase();
  if (!["auto", "off", "force"].includes(mode)) return { ok: false, error: `ocr must be auto, off or force, not ${JSON.stringify(ocr)}`, via: KAFKA, kind: "client_error" };
  const res = await runJob(KAFKA, "doc_extract", { path: src, ocr: mode, max_pages: Math.trunc(maxPages), lang: String(lang || "es") }, "ocr_status", { timeoutS: Number(timeoutS) });
  if (res.ok) return extractResult(res.data, KAFKA);
  const out = publicError(res, KAFKA);
  if (res.kind === "timeout") Object.assign(out, { job_id: res.jobId, still_running: true });
  return out;
});

export const docsOcrImage = safe(KAFKA, (file, { lang = "es", blocks = false, timeoutS = 120 } = {}) =>
  kafka("ocr_image", cleanArgs({ path: asPath(file), lang, blocks: blocks || null }), timeoutS));

export const docsOcrPdf = safe(KAFKA, async (file, { pages = null, dpi = 200, lang = "es", maxPages = 40, timeoutS = 900 } = {}) => {
  const res = await runJob(KAFKA, "ocr_pdf", cleanArgs({ path: asPath(file), pages, dpi: Math.trunc(dpi), lang, max_pages: Math.trunc(maxPages) }), "ocr_status", { timeoutS: Number(timeoutS) });
  if (res.ok) return kafkaOk(res.data);
  const out = publicError(res, KAFKA);
  if (res.kind === "timeout") Object.assign(out, { job_id: res.jobId, still_running: true });
  return out;
});

export const docsOcrStatus = safe(KAFKA, (jobId = "", { waitS = 0, timeoutS = 30 } = {}) => {
  const w = clampWait(waitS);
  return kafka("ocr_status", cleanArgs({ job_id: jobId, wait_s: jobId ? w : null }), Math.max(Number(timeoutS), w + HUB_MARGIN_S));
});

// ---- embeddings (Borges) -----------------------------------------------------------------------------------------------------------

export const embedStatus = safe(BORGES, async ({ timeoutS = 30 } = {}) => {
  const r = await callTool(BORGES, "embed_status", {}, { timeoutS });
  if (!r.ok) return publicError(r, BORGES);
  const data = isObj(r.data) ? { ...r.data } : {};
  if (data.ready === undefined) data.ready = String(data.state || "") === "ready";
  return { ok: true, ...data, via: BORGES };
});

function validVectors(vectors, count) {
  if (!Array.isArray(vectors) || vectors.length !== count) return null;
  let dim = null;
  const out = [];
  for (const v of vectors) {
    if (!Array.isArray(v) || !v.length) return null;
    if (dim === null) dim = v.length;
    if (v.length !== dim) return null;
    const nums = v.map(Number);
    if (nums.some((x) => !Number.isFinite(x))) return null;
    out.push(nums);
  }
  return out;
}

function unit(v) {
  const n = Math.sqrt(v.reduce((a, x) => a + x * x, 0));
  return n ? v.map((x) => x / n) : v;
}

/** Vectors for `texts` from Borges's embedder, in batches. Options: kind ("document"|"query"), normalize (true), batch (64), timeoutS (120 per batch).
 *  Returns { ok, model, dim, vectors, via: "borges" }; `model` and `dim` identify the vector space (store them with the vectors). */
export const embedTexts = safe(BORGES, async (texts, { kind = "document", normalize = true, batch = 64, timeoutS = 120 } = {}) => {
  if (!Array.isArray(texts)) return { ok: false, error: "texts must be a list of strings", via: BORGES, kind: "client_error" };
  const items = texts.map(String);
  const mode = String(kind || "document").trim().toLowerCase();
  if (!["document", "query"].includes(mode)) return { ok: false, error: `kind must be document or query, not ${JSON.stringify(kind)}`, via: BORGES, kind: "client_error" };
  if (!items.length) return { ok: true, model: "", dim: 0, vectors: [], via: BORGES };
  const size = Math.max(1, Math.min(Math.trunc(batch) || 64, 512));
  const vectors = [];
  let model = "", dim = 0;
  for (let i = 0; i < items.length; i += size) {
    const part = items.slice(i, i + size);
    const r = await callTool(BORGES, "embed_texts", { texts: part, kind: mode, normalize: Boolean(normalize) }, { timeoutS: Number(timeoutS) });
    if (!r.ok) {
      const out = publicError(r, BORGES);
      if (i) out.error = `${out.error} (after ${i} of ${items.length} texts)`;
      return out;
    }
    const data = isObj(r.data) ? r.data : {};
    let got = validVectors(data.vectors, part.length);
    if (!got) return { ok: false, error: "Borges returned a wrong number or shape of vectors", via: BORGES, kind: "tool_error" };
    const thisModel = String(data.model || "");
    if (i && (thisModel !== model || got[0].length !== dim)) return { ok: false, error: `the embedding model changed during the call (${JSON.stringify(model)} -> ${JSON.stringify(thisModel)}); try again`, via: BORGES, kind: "tool_error" };
    model = thisModel; dim = got[0].length;
    if (normalize && data.normalized === false) got = got.map(unit);
    vectors.push(...got);
  }
  return { ok: true, model, dim, vectors, via: BORGES };
});

export const embedQuery = safe(BORGES, async (text, { normalize = true, timeoutS = 60 } = {}) => {
  const res = await embedTexts([String(text ?? "")], { kind: "query", normalize, timeoutS });
  return res.ok ? { ok: true, model: res.model, dim: res.dim, vector: res.vectors[0], via: res.via } : res;
});
