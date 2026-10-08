// docs.js — the Node twin of hoard_link/docs/ (ESM, Node 18+, no npm dependencies).
//
//   import { ftsQuery, ftsLadder, highlight, chunkText, sniff, parseRanges, topk, rrf, cite } from "./hoard-commons/docs.js";
//
// Same rules as the Python modules (textsearch, chunking, sniff, pageranges, vecmath, citations); both are
// checked against tests/vectors/docs_*.json. Options are passed as a trailing object in camelCase
// (`ftsQuery(q, { mode: "or", maxTerms: 8 })`). Differences from Python: offsets are UTF-16 units, so text with
// characters outside the BMP (emoji) can chunk at slightly different places than Python.

import zlib from "node:zlib";
import { fold } from "./text.js";

// ================================================================================== textsearch

export const PREFIX_MIN_CHARS = 3;

export const STOPWORDS = new Set(
  ("de la el los las un una unos unas y o u e en del al a que con por para se su sus lo le les es son no ni como más muy ya " +
    "mi mis tu tus me te sobre acerca dónde donde qué cuál cuáles cómo cuándo quién hay he ha leí escribí dice dijo del este esta esto " +
    "the of an and or in on to is are was were for with that this about where what which how when who did i my").split(" "),
);
export const STOPWORDS_FOLDED = new Set([...STOPWORDS].map((w) => fold(w)));

const WORD = /[\p{L}\p{N}]+/gu;
const ALNUM = /[\p{L}\p{N}]/u;
const len = (s) => Array.from(s).length;

export function isStopword(word) {
  return STOPWORDS_FOLDED.has(fold(word));
}

export function tokens(text, { stop = null } = {}) {
  const words = fold(text).match(WORD) || [];
  if (stop && (stop.size === undefined ? stop.length : stop.size)) {
    const drop = stop instanceof Set ? stop : new Set(stop);
    return words.filter((w) => !drop.has(w));
  }
  return words;
}

export function stem(word) {
  const w = String(word).toLowerCase();
  const n = len(w);
  if (n > 5 && w.endsWith("es")) return Array.from(w).slice(0, -2).join("");
  if (n > 4 && w.endsWith("s")) return Array.from(w).slice(0, -1).join("");
  return w;
}

export function contentWords(q) {
  const words = (q == null ? "" : String(q)).match(WORD) || [];
  const kept = words.filter((w) => !isStopword(w) && len(w) > 1);
  return kept.length ? kept : words;
}

function uniqueTerms(q, maxTerms) {
  const out = [];
  const seen = new Set();
  for (const w of contentWords(q)) {
    const lw = w.toLowerCase();
    if (seen.has(lw)) continue;
    seen.add(lw);
    out.push(lw);
    if (out.length >= maxTerms) break;
  }
  return out;
}

const quote = (t) => '"' + t.replace(/"/g, '""') + '"';

export function ftsQuery(q, { mode = "and", maxTerms = 12 } = {}) {
  if (!["and", "or", "prefix"].includes(mode)) throw new Error(`mode must be 'and', 'or' or 'prefix', not '${mode}'`);
  const terms = uniqueTerms(q, Math.max(1, Math.trunc(maxTerms)));
  if (!terms.length) return "";
  if (mode === "and") return terms.map(quote).join(" AND ");
  const parts = terms.map((t) => (len(t) >= PREFIX_MIN_CHARS ? quote(stem(t)) + "*" : quote(t)));
  return parts.join(mode === "or" ? " OR " : " AND ");
}

export function ftsLadder(q, { maxTerms = 12 } = {}) {
  const ladder = [];
  for (const mode of ["and", "prefix", "or"]) {
    const expr = ftsQuery(q, { mode, maxTerms });
    if (expr && !ladder.includes(expr)) ladder.push(expr);
  }
  return ladder;
}

export function queryTerms(q, { maxTerms = 12 } = {}) {
  return uniqueTerms(q, maxTerms).map((t) => (len(t) >= PREFIX_MIN_CHARS ? fold(stem(t)) : fold(t)));
}

const escHtml = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escRe = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function excerpt(text, terms, windowSize) {
  const folded = fold(text); // same length as text: offsets map straight back
  const positions = [];
  for (const term of terms) {
    const t = fold(term);
    if (!t) continue;
    const re = new RegExp(escRe(t), "g");
    let m;
    while ((m = re.exec(folded)) !== null) {
      const start = m.index;
      if (m[0].length === 0) { re.lastIndex++; continue; }
      if (start > 0 && ALNUM.test(folded[start - 1])) continue; // only word starts
      let end = start + m[0].length;
      while (end < folded.length && ALNUM.test(folded[end])) end++; // mark the whole word
      positions.push([start, end]);
    }
  }
  positions.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let start = positions.length ? Math.max(0, positions[0][0] - Math.floor(windowSize / 3)) : 0;
  let end = Math.min(text.length, start + windowSize);
  if (start > 0) {
    const from = Math.max(0, start - 20);
    const space = text.lastIndexOf(" ", start - 1);
    start = space >= from ? space + 1 : start < 20 ? 0 : start;
  }
  if (end < text.length) {
    const lim = Math.min(text.length, end + 20);
    const space = text.indexOf(" ", end);
    end = space !== -1 && space < lim ? space : end;
  }
  return { start, end, positions };
}

export function highlight(text, terms, { window: windowSize = 160, tag = "mark", escape = true } = {}) {
  const s = text == null ? "" : String(text);
  if (tag && !/^[A-Za-z][A-Za-z0-9]*$/.test(tag)) throw new Error("tag must be a plain element name");
  const { start, end, positions } = excerpt(s, terms, windowSize);
  const esc = escape ? escHtml : (x) => x;
  const pieces = [];
  let cursor = start;
  for (const [a, z] of positions) {
    if (a < start || z > end || a < cursor || !tag) continue;
    pieces.push(esc(s.slice(cursor, a)));
    pieces.push(`<${tag}>${esc(s.slice(a, z))}</${tag}>`);
    cursor = z;
  }
  pieces.push(esc(s.slice(cursor, end)));
  const body = pieces.join("").replace(/\n/g, " ");
  return (start > 0 ? "…" : "") + body + (end < s.length ? "…" : "");
}

export function snippet(text, terms = [], { window: windowSize = 160 } = {}) {
  const s = text == null ? "" : String(text);
  const { start, end } = excerpt(s, terms, windowSize);
  const body = s.slice(start, end).replace(/\s+/g, " ").trim();
  return (start > 0 ? "…" : "") + body + (end < s.length ? "…" : "");
}

// ================================================================================== chunking

export const CHUNK_VERSION = 4;
const BREAKS = /\n\n|\n|(?<=[.!?…;:])\s+|(?<=,)\s+|\s+/g;

function splitPoint(text, start, limit) {
  const win = text.slice(start, limit);
  let best = -1;
  let bestPriority = -1;
  BREAKS.lastIndex = 0;
  let m;
  while ((m = BREAKS.exec(win)) !== null) {
    if (m[0].length === 0) { BREAKS.lastIndex++; continue; }
    const end = m.index + m[0].length;
    if (end < win.length * 0.4) continue; // don't cut too early
    const before = m.index > 0 ? win[m.index - 1] : "";
    let score;
    if (m[0] === "\n\n") score = 5;
    else if (m[0] === "\n") score = 4;
    else if (before && ".!?…;:".includes(before)) score = 3;
    else if (before === ",") score = 2;
    else score = 1;
    if (score >= bestPriority) { bestPriority = score; best = end; }
  }
  return best > 0 ? start + best : limit;
}

function asUnit(u) {
  return {
    kind: u.kind || "section", number: u.number ?? 1, title: u.title || "", text: u.text || "",
    lineStart: u.lineStart ?? u.line_start ?? null,
  };
}

function absorb(main, others, before) {
  const parts = others.map((u) => (u.title ? `${u.title}\n${u.text}` : u.text));
  const text = (before ? [...parts, main.text] : [main.text, ...parts]).join("\n\n");
  return { ...main, text };
}

export function mergeSmallUnits(units, minimum = 200) {
  if (units.length <= 1 || units.some((u) => u.kind === "page" || u.kind === "slide")) return [...units];
  let pending = [];
  const out = [];
  for (let unit of units) {
    if (unit.text.length < minimum) { pending.push(unit); continue; }
    if (pending.length) { unit = absorb(unit, pending, true); pending = []; }
    out.push(unit);
  }
  if (pending.length) {
    if (out.length) out[out.length - 1] = absorb(out[out.length - 1], pending, false);
    else {
      const biggest = pending.reduce((a, b) => (b.text.length > a.text.length ? b : a));
      out.push(absorb(biggest, pending.filter((u) => u !== biggest), true));
    }
  }
  return out;
}

function countNl(text, end) {
  let n = 0;
  for (let i = 0; i < end; i++) if (text.charCodeAt(i) === 10) n++;
  return n;
}

function chunkUnit(unit, unitIndex, firstOrdinal, size, overlap, minTail) {
  const text = unit.text;
  const page = unit.kind === "page" || unit.kind === "slide" ? unit.number : null;
  const chunks = [];
  let start = 0;
  const n = text.length;
  while (start < n) {
    const end = n - start <= size + minTail ? n : splitPoint(text, start, start + size);
    const piece = text.slice(start, end);
    const stripped = piece.trim();
    if (stripped) {
      const lead = piece.length - piece.trimStart().length;
      const cStart = start + lead;
      const cEnd = cStart + stripped.length;
      const line = unit.lineStart != null ? unit.lineStart + countNl(text, cStart) : null;
      chunks.push({ unitIndex, ordinal: firstOrdinal + chunks.length, page, section: unit.title, line, charStart: cStart, charEnd: cEnd, text: stripped });
    }
    if (end >= n) break;
    let nextStart = Math.max(end - overlap, start + 1);
    const boundary = text.slice(0, end).indexOf(" ", Math.max(0, end - overlap));
    if (boundary > start) nextStart = boundary + 1;
    start = nextStart;
  }
  return chunks;
}

function absorbTiny(units, chunks, minChunk) {
  if (chunks.length <= 1) return chunks;
  const result = [];
  for (const chunk of chunks) {
    if (chunk.text.length >= minChunk || !result.length || result[result.length - 1].unitIndex !== chunk.unitIndex) { result.push(chunk); continue; }
    const prev = result[result.length - 1];
    prev.charEnd = chunk.charEnd;
    prev.text = units[prev.unitIndex].text.slice(prev.charStart, prev.charEnd).trim();
  }
  const cleaned = [];
  result.forEach((chunk, i) => {
    const nxt = i + 1 < result.length ? result[i + 1] : null;
    if (chunk.text.length < minChunk && nxt && nxt.unitIndex === chunk.unitIndex) {
      nxt.charStart = chunk.charStart;
      nxt.text = units[nxt.unitIndex].text.slice(nxt.charStart, nxt.charEnd).trim();
      nxt.line = chunk.line;
      return;
    }
    cleaned.push(chunk);
  });
  cleaned.forEach((c, i) => { c.ordinal = i; });
  return cleaned;
}

function clampSizes(size, overlap, minChunk) {
  const s = Math.max(1, Math.trunc(size));
  return [s, Math.max(0, Math.min(Math.trunc(overlap), Math.floor(s / 2))), Math.max(0, Math.min(Math.trunc(minChunk), Math.floor(s / 2)))];
}

export function chunkUnits(units, { size = 900, overlap = 150, minUnit = 200, minChunk = 120, minTail = 200 } = {}) {
  const [sz, ov, mc] = clampSizes(size, overlap, minChunk);
  const prepared = mergeSmallUnits(units.map(asUnit), minUnit);
  const out = [];
  prepared.forEach((unit, index) => out.push(...chunkUnit(unit, index, out.length, sz, ov, minTail)));
  return absorbTiny(prepared, out, mc);
}

export function chunkText(text, { size = 900, overlap = 150, minTail = 200 } = {}) {
  const s = text == null ? "" : String(text);
  return chunkUnits([{ kind: "text", number: 1, title: "", text: s, lineStart: 1 }], { size, overlap, minUnit: 0, minTail });
}

// ================================================================================== sniff

export const MIME = {
  pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
  bmp: "image/bmp", tif: "image/tiff", tiff: "image/tiff", heic: "image/heic", heif: "image/heif", avif: "image/avif",
  svg: "image/svg+xml", ico: "image/x-icon",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  odt: "application/vnd.oasis.opendocument.text", ods: "application/vnd.oasis.opendocument.spreadsheet",
  odp: "application/vnd.oasis.opendocument.presentation", epub: "application/epub+zip", rtf: "application/rtf",
  doc: "application/msword", xls: "application/vnd.ms-excel", ppt: "application/vnd.ms-powerpoint", msg: "application/vnd.ms-outlook",
  mp3: "audio/mpeg", wav: "audio/wav", flac: "audio/flac", ogg: "audio/ogg", opus: "audio/ogg", m4a: "audio/mp4", aac: "audio/aac",
  mid: "audio/midi", weba: "audio/webm",
  mp4: "video/mp4", m4v: "video/mp4", mov: "video/quicktime", webm: "video/webm", mkv: "video/x-matroska", avi: "video/x-msvideo",
  "3gp": "video/3gpp", ogv: "video/ogg",
  html: "text/html", htm: "text/html", xhtml: "application/xhtml+xml", json: "application/json", csv: "text/csv",
  tsv: "text/tab-separated-values", txt: "text/plain", text: "text/plain", md: "text/markdown", markdown: "text/markdown",
  xml: "application/xml", yaml: "text/yaml", yml: "text/yaml", log: "text/plain", css: "text/css", js: "text/javascript",
  py: "text/x-python", eml: "message/rfc822", ics: "text/calendar", vcf: "text/vcard",
  zip: "application/zip", "7z": "application/x-7z-compressed", rar: "application/vnd.rar", gz: "application/gzip",
  tgz: "application/gzip", tar: "application/x-tar", bz2: "application/x-bzip2", xz: "application/x-xz",
  sqlite: "application/vnd.sqlite3", db: "application/vnd.sqlite3", parquet: "application/vnd.apache.parquet",
};
const OCTET = "application/octet-stream";

const TEXT_EXT = new Set(["txt", "text", "md", "markdown", "csv", "tsv", "json", "html", "htm", "xhtml", "xml", "yaml", "yml", "log",
  "css", "js", "py", "eml", "ics", "vcf", "svg", "ini", "toml", "cfg", "rst", "tex", "srt", "vtt", "sql", "sh", "ts"]);
const EXT_KIND = {
  pdf: "pdf", docx: "docx", xlsx: "xlsx", pptx: "pptx", odt: "odt", ods: "ods", odp: "odp", epub: "epub", rtf: "rtf",
  doc: "ole", xls: "ole", ppt: "ole", msg: "ole", html: "html", htm: "html", xhtml: "html", json: "json", csv: "csv",
  tsv: "csv", eml: "eml", zip: "zip", svg: "image", sqlite: "sqlite", parquet: "parquet",
};
for (const e of ["png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif", "ico"]) EXT_KIND[e] = "image";
for (const e of ["mp3", "wav", "flac", "ogg", "opus", "m4a", "aac", "mid", "weba"]) EXT_KIND[e] = "audio";
for (const e of ["mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "ogv"]) EXT_KIND[e] = "video";
for (const e of ["7z", "rar", "gz", "tgz", "tar", "bz2", "xz"]) EXT_KIND[e] = "archive";
for (const e of TEXT_EXT) if (!(e in EXT_KIND)) EXT_KIND[e] = "text";

const ODF = {
  "application/vnd.oasis.opendocument.text": "odt",
  "application/vnd.oasis.opendocument.spreadsheet": "ods",
  "application/vnd.oasis.opendocument.presentation": "odp",
  "application/vnd.oasis.opendocument.graphics": "odg",
};
const HEIC_BRANDS = new Set(["heic", "heix", "hevc", "hevx", "heim", "heis", "hevm", "hevs"]);
const MP4_AUDIO = new Set(["M4A ", "M4B ", "M4P "]);

export function extOf(name) {
  const base = String(name ?? "").replace(/\\/g, "/").split("/").pop();
  if (!base.replace(/^\.+|\.+$/g, "").includes(".")) return "";
  return base.slice(base.lastIndexOf(".") + 1).toLowerCase().slice(0, 8);
}

export function mimeFor(ext) {
  let e = String(ext ?? "");
  if (e.replace(/^\.+|\.+$/g, "").includes(".")) e = e.slice(e.lastIndexOf(".") + 1);
  e = e.replace(/^[. ]+|[. ]+$/g, "").toLowerCase();
  return Object.hasOwn(MIME, e) ? MIME[e] : OCTET;
}

const mk = (kind, ext) => ({ kind, mime: Object.hasOwn(MIME, ext) ? MIME[ext] : OCTET, ext });

function toBuf(data) {
  if (Buffer.isBuffer(data)) return data;
  if (data instanceof Uint8Array) return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
  if (data instanceof ArrayBuffer) return Buffer.from(data);
  return Buffer.from(data || []);
}

const ascii = (buf, a, b) => buf.toString("latin1", a, Math.min(b, buf.length));
const eq = (buf, a, str) => buf.length >= a + str.length && ascii(buf, a, a + str.length) === str;

export function zipMemberNames(data, { limit = 100000 } = {}) {
  const d = toBuf(data);
  if (d.length < 2 || d[0] !== 0x50 || d[1] !== 0x4b) return null;
  const info = zipEntries(d);
  if (info) return info.map((e) => e.name).slice(0, limit);
  const names = [];
  let pos = 0;
  while (names.length < 64) {
    pos = d.indexOf(Buffer.from([0x50, 0x4b, 3, 4]), pos);
    if (pos < 0 || pos > 8 * 1024 * 1024 || pos + 30 > d.length) break;
    const n = d.readUInt16LE(pos + 26);
    const raw = d.subarray(pos + 30, pos + 30 + n);
    if (raw.length === n && n) names.push(raw.toString("utf8"));
    pos += 4;
  }
  return names.length ? names : null;
}

function zipEntries(d) {
  const lo = Math.max(0, d.length - 65557);
  let eocd = -1;
  for (let i = d.length - 22; i >= lo; i--) {
    if (d[i] === 0x50 && d[i + 1] === 0x4b && d[i + 2] === 5 && d[i + 3] === 6) { eocd = i; break; }
  }
  if (eocd < 0) return null;
  const count = d.readUInt16LE(eocd + 10);
  let off = d.readUInt32LE(eocd + 16);
  if (count === 0xffff || off === 0xffffffff || off >= d.length) return null;
  const entries = [];
  for (let k = 0; k < count; k++) {
    if (off + 46 > d.length || d.readUInt32LE(off) !== 0x02014b50) return entries.length ? entries : null;
    const flags = d.readUInt16LE(off + 8);
    const method = d.readUInt16LE(off + 10);
    const csize = d.readUInt32LE(off + 20);
    const n = d.readUInt16LE(off + 28);
    const m = d.readUInt16LE(off + 30);
    const c = d.readUInt16LE(off + 32);
    const local = d.readUInt32LE(off + 42);
    entries.push({ name: d.toString(flags & 0x800 ? "utf8" : "latin1", off + 46, off + 46 + n), method, csize, local });
    off += 46 + n + m + c;
  }
  return entries;
}

function zipMimetype(d) {
  try {
    const e = (zipEntries(d) || []).find((x) => x.name === "mimetype");
    if (e) {
      const start = e.local + 30 + d.readUInt16LE(e.local + 26) + d.readUInt16LE(e.local + 28);
      const raw = d.subarray(start, start + e.csize);
      const body = e.method === 8 ? zlib.inflateRawSync(raw) : raw;
      return body.toString("latin1", 0, 256).trim();
    }
  } catch { /* damaged: names decide */ }
  const m = /^PK\x03\x04[\s\S]{22}[\s\S]{2}[\s\S]{2}mimetype([\s\S]{0,128})/.exec(ascii(d, 0, 400));
  return m ? m[1].replace(/[^\x20-\x7e]/g, "").trim() : "";
}

function classifyZip(d, ext) {
  const names = zipMemberNames(d);
  if (names === null) return mk("zip", "zip");
  const lower = names.map((n) => n.toLowerCase());
  const has = (p) => lower.some((n) => n.startsWith(p));
  if (lower.includes("mimetype")) {
    const mt = zipMimetype(d);
    if (mt.startsWith("application/epub+zip")) return mk("epub", "epub");
    for (const [key, e] of Object.entries(ODF)) {
      if (mt.startsWith(key)) return e !== "odg" ? mk(e, e) : { kind: "zip", mime: key, ext: "odg" };
    }
  }
  if (has("word/") && (lower.includes("[content_types].xml") || lower.includes("word/document.xml"))) return mk("docx", "docx");
  if (has("xl/")) return mk("xlsx", "xlsx");
  if (has("ppt/")) return mk("pptx", "pptx");
  if (lower.includes("meta-inf/container.xml") && (lower.includes("mimetype") || has("oebps/") || lower.some((n) => n.endsWith(".opf")))) return mk("epub", "epub");
  if (lower.includes("content.xml") && lower.includes("meta-inf/manifest.xml")) {
    const e = ["odt", "ods", "odp"].includes(ext) ? ext : "odt";
    return mk(e, e);
  }
  return mk("zip", "zip");
}

function textInfo(sample) {
  if (sample.length >= 3 && sample[0] === 0xef && sample[1] === 0xbb && sample[2] === 0xbf) return "utf-8";
  if (sample.length >= 2 && ((sample[0] === 0xff && sample[1] === 0xfe) || (sample[0] === 0xfe && sample[1] === 0xff))) return "utf-16";
  if (!sample.length) return "utf-8";
  let ctrl = 0;
  for (const b of sample) {
    if (b === 0) return null;
    if (b < 32 && ![9, 10, 12, 13, 27].includes(b)) ctrl++;
  }
  if (ctrl > Math.max(2, Math.floor(sample.length / 50))) return null;
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(sample, { stream: true });
    return "utf-8";
  } catch { return "latin"; }
}

function decodeHead(d, enc, n = 8192) {
  const head = d.subarray(0, n);
  if (enc === "utf-16") {
    if (head[0] === 0xfe) { const sw = Buffer.from(head.subarray(2)); sw.swap16(); return sw.toString("utf16le"); }
    return head.toString("utf16le", 2);
  }
  if (enc === "utf-8") return head.toString("utf8").replace(/^﻿/, "");
  return head.toString("latin1");
}

function classifyText(d, ext, enc) {
  const head = decodeHead(d, enc);
  const low = head.replace(/^\s+/, "").toLowerCase();
  if (low.startsWith("<!doctype html") || low.startsWith("<html") || (low.startsWith("<?xml") && low.slice(0, 400).includes("<html"))) return mk("html", "html");
  if (low.startsWith("<svg") || ((low.startsWith("<?xml") || low.startsWith("<!doctype svg")) && low.slice(0, 600).includes("<svg"))) return mk("image", "svg");
  if (/^(?:received|return-path|message-id|mime-version|delivered-to|x-[a-z-]+):/.test(low) || (low.startsWith("from ") && low.slice(0, 2000).includes("\nsubject:")) ||
      (ext === "eml" && /^(?:from|to|cc|bcc|subject|date|reply-to|sender):/.test(low))) return mk("eml", "eml");
  const stripped = head.replace(/^\s+/, "");
  const txtOther = TEXT_EXT.has(ext) && ext !== "json";
  if (ext === "json" || (["{", "["].includes(stripped.slice(0, 1)) && !txtOther)) {
    if (d.length <= 2000000) {
      try { JSON.parse(decodeHead(d, enc, d.length)); return mk("json", "json"); } catch { /* not json */ }
    } else if (ext === "json") return mk("json", "json");
  }
  if (ext === "csv" || ext === "tsv") return mk("csv", ext);
  if (TEXT_EXT.has(ext) || ext === "md") return mk(["html", "htm", "xhtml"].includes(ext) ? "html" : "text", Object.hasOwn(MIME, ext) ? ext : "txt");
  const lines = head.split(/\r\n|\r|\n/).filter((l) => l.trim()).slice(0, 8);
  if (lines.length >= 3) {
    for (const sep of [",", ";", "\t"]) {
      const counts = new Set(lines.map((l) => l.split(sep).length - 1));
      if (counts.size === 1 && [...counts][0] >= 2) return mk("csv", sep === "\t" ? "tsv" : "csv");
    }
  }
  return mk("text", "txt");
}

export function sniff(name, data) {
  const ext = extOf(name);
  const d = toBuf(data);
  const n = d.length;
  const has = (a, s) => eq(d, a, s);

  if (d.subarray(0, 1024).includes("%PDF-")) return mk("pdf", "pdf");
  if (n >= 8 && d.readUInt32BE(0) === 0x89504e47 && d.readUInt32BE(4) === 0x0d0a1a0a) return mk("image", "png");
  if (n >= 3 && d[0] === 0xff && d[1] === 0xd8 && d[2] === 0xff) return mk("image", "jpg");
  if (has(0, "GIF87a") || has(0, "GIF89a")) return mk("image", "gif");
  if (has(0, "RIFF") && n >= 12) {
    const tag = ascii(d, 8, 12);
    if (tag === "WEBP") return mk("image", "webp");
    if (tag === "WAVE") return mk("audio", "wav");
    if (tag === "AVI ") return mk("video", "avi");
  }
  if (n >= 4 && ((d[0] === 0x49 && d[1] === 0x49 && d[2] === 0x2a && d[3] === 0) || (d[0] === 0x4d && d[1] === 0x4d && d[2] === 0 && d[3] === 0x2a))) return mk("image", "tif");
  if (has(0, "BM") && n >= 30 && [12, 40, 52, 56, 64, 108, 124].includes(d.readUInt32LE(14))) return mk("image", "bmp");
  if (n >= 22 && d[0] === 0 && d[1] === 0 && d[2] === 1 && d[3] === 0 && d[4] && d[5] === 0) return mk("image", "ico");
  if (n >= 12 && has(4, "ftyp")) {
    const brand = ascii(d, 8, 12);
    if (HEIC_BRANDS.has(brand)) return mk("image", "heic");
    if (brand === "mif1" || brand === "msf1") {
      const compat = ascii(d, 16, Math.min(n, 8 + d.readUInt32BE(0)));
      return mk("image", compat.includes("avif") && !compat.includes("heic") ? "avif" : "heic");
    }
    if (brand === "avif" || brand === "avis") return mk("image", "avif");
    if (MP4_AUDIO.has(brand)) return mk("audio", "m4a");
    if (brand === "qt  ") return mk("video", "mov");
    if (brand.startsWith("3gp") || brand.startsWith("3g2")) return mk("video", "3gp");
    return mk("video", "mp4");
  }
  if (n >= 4 && d[0] === 0x1a && d[1] === 0x45 && d[2] === 0xdf && d[3] === 0xa3) return mk("video", d.subarray(0, 64).includes("webm") ? "webm" : "mkv");
  if (has(0, "fLaC")) return mk("audio", "flac");
  if (has(0, "OggS")) {
    if (d.subarray(0, 64).includes("OpusHead")) return mk("audio", "opus");
    if (d.subarray(0, 96).includes("theora")) return mk("video", "ogv");
    return mk("audio", "ogg");
  }
  if (has(0, "ID3")) return mk("audio", "mp3");
  if (has(0, "MThd")) return mk("audio", "mid");
  if (n >= 2 && d[0] === 0xff && (d[1] & 0xe0) === 0xe0 && ["mp3", "mp2", "aac"].includes(ext) && ((d[1] >> 3) & 3) !== 1 && ((d[1] >> 1) & 3) !== 0) return mk("audio", ext === "aac" ? "aac" : "mp3");
  if (n >= 6 && d[0] === 0x37 && d[1] === 0x7a && d[2] === 0xbc && d[3] === 0xaf && d[4] === 0x27 && d[5] === 0x1c) return mk("archive", "7z");
  if (n >= 6 && has(0, "Rar!") && d[4] === 0x1a && d[5] === 0x07) return mk("archive", "rar");
  if (n >= 2 && d[0] === 0x1f && d[1] === 0x8b) return mk("archive", "gz");
  if (has(0, "BZh")) return mk("archive", "bz2");
  if (n >= 6 && d[0] === 0xfd && has(1, "7zXZ") && d[5] === 0) return mk("archive", "xz");
  if (n >= 262 && has(257, "ustar")) return mk("archive", "tar");
  if (n >= 16 && has(0, "SQLite format 3") && d[15] === 0) return mk("sqlite", "sqlite");
  if (has(0, "PAR1") && n >= 8 && ascii(d, n - 4, n) === "PAR1") return mk("parquet", "parquet");
  if (n >= 8 && d.readUInt32BE(0) === 0xd0cf11e0 && d.readUInt32BE(4) === 0xa1b11ae1) {
    const e = ["doc", "xls", "ppt", "msg"].includes(ext) ? ext : "doc";
    return { kind: "ole", mime: MIME[e], ext: e };
  }
  if (has(0, "{\\rtf")) return mk("rtf", "rtf");
  if (n >= 4 && d[0] === 0x50 && d[1] === 0x4b && ((d[2] === 3 && d[3] === 4) || (d[2] === 5 && d[3] === 6) || (d[2] === 7 && d[3] === 8))) return classifyZip(d, ext);

  if (n === 0) {
    const kind = Object.hasOwn(EXT_KIND, ext) ? EXT_KIND[ext] : "unknown";
    return { kind, mime: Object.hasOwn(MIME, ext) ? MIME[ext] : OCTET, ext: ext || "bin" };
  }
  const enc = textInfo(d.subarray(0, 8192));
  if (enc !== null && (enc !== "latin" || TEXT_EXT.has(ext))) return classifyText(d, ext, enc);
  return { kind: "unknown", mime: OCTET, ext: ext || "bin" };
}

// ================================================================================== pageranges

export const EXAMPLES_ES = "Ejemplos: 3, 1-3, 2,5,8-, last, -1 (la última), 3-last, impares, pares, todas.";
export const EXAMPLES_EN = "Examples: 3, 1-3, 2,5,8-, last, -1 (the last page), 3-last, odd, even, all.";
const LAST = ["last", "ultima", "ultimo", "final", "fin", "end"];
const ALL = new Set(["all", "todas", "todo", "todos"]);
const ODD = new Set(["odd", "impar", "impares"]);
const EVEN = new Set(["even", "par", "pares"]);

export class PageRangeError extends Error {
  constructor(messageEs, messageEn, lang = "es") {
    super(lang === "en" ? messageEn : messageEs);
    this.name = "PageRangeError";
    this.messageEs = messageEs;
    this.messageEn = messageEn;
    this.lang = lang;
  }
}

const rangeErr = (lang, es, en) => new PageRangeError(`${es} ${EXAMPLES_ES}`, `${en} ${EXAMPLES_EN}`, lang);

function normRange(text) {
  let t = fold(text == null ? "" : String(text)).trim();
  t = t.replace(/[‒-―−]/g, "-");
  t = t.replace(/\.\.+/g, "-");
  t = t.replace(/(?<=[\dt])\s+(?:a|hasta)\s+(?=[\d-]|last|ultima|final|fin|end)/g, "-");
  t = t.replace(/\s+y\s+/g, ",");
  t = t.replace(/\s*-\s*/g, "-");
  t = t.replace(/\b(?:pagina|paginas|pag|pags|p)\.?\s*(?=\d|-|last)/g, "");
  for (const w of LAST) t = t.replace(new RegExp(`\\b${w}\\b`, "g"), "last");
  return t;
}

function onePage(token, total, original, lang) {
  if (token === "last") return total;
  let n = parseInt(token, 10);
  if (n === 0) throw rangeErr(lang, "La página 0 no existe: las páginas empiezan en 1.", "Page 0 does not exist: pages start at 1.");
  if (n < 0) {
    n = total + n + 1;
    if (n < 1) throw rangeErr(lang, `«${original}» queda antes de la primera página: el documento tiene ${total} página(s).`, `“${original}” is before the first page: the document has ${total} page(s).`);
    return n;
  }
  if (n > total) throw rangeErr(lang, `La página ${n} no existe: el documento tiene ${total} página(s).`, `Page ${n} does not exist: the document has ${total} page(s).`);
  return n;
}

const seq = (a, z, step = 1) => { const out = []; for (let i = a; i <= z; i += step) out.push(i); return out; };

export function parseGroups(text, total, { allowReversed = false, lang = "es" } = {}) {
  if (total < 1) throw rangeErr(lang, "El documento no tiene páginas.", "The document has no pages.");
  const norm = normRange(text);
  if (!norm) throw rangeErr(lang, "Indica las páginas.", "Say which pages.");
  const groups = [];
  for (const token of norm.split(/[,;\s]+/).filter(Boolean)) {
    if (ALL.has(token)) groups.push(seq(1, total));
    else if (ODD.has(token)) groups.push(seq(1, total, 2));
    else if (EVEN.has(token)) groups.push(seq(2, total, 2));
    else if (/^(?:-?\d+|last)$/.test(token)) groups.push([onePage(token, total, token, lang)]);
    else {
      const m = /^(-?\d+|last)-(-?\d+|last)?$/.exec(token);
      if (!m) throw rangeErr(lang, `No entiendo «${token}» como página o rango.`, `“${token}” is not a page or a range.`);
      let first = onePage(m[1], total, token, lang);
      let last = m[2] ? onePage(m[2], total, token, lang) : total;
      if (first > last) {
        if (!allowReversed) throw rangeErr(lang, `El rango «${token}» está al revés: la primera página (${first}) es mayor que la última (${last}).`, `The range “${token}” is backwards: the first page (${first}) is after the last (${last}).`);
        [first, last] = [last, first];
      }
      groups.push(seq(first, last));
    }
  }
  if (!groups.length) throw rangeErr(lang, "Indica las páginas.", "Say which pages.");
  return groups;
}

export function parseRanges(text, total, { defaultAll = false, unique = true, allowReversed = false, lang = "es" } = {}) {
  if (!String(text ?? "").trim() && defaultAll) return seq(1, total);
  let pages = parseGroups(text, total, { allowReversed, lang }).flat();
  if (unique) { const seen = new Set(); pages = pages.filter((p) => (seen.has(p) ? false : (seen.add(p), true))); }
  return pages;
}

export function describeRanges(pages) {
  const items = [...new Set(pages)].sort((a, b) => a - b);
  const out = [];
  let i = 0;
  while (i < items.length) {
    let j = i;
    while (j + 1 < items.length && items[j + 1] === items[j] + 1) j++;
    out.push(i === j ? String(items[i]) : `${items[i]}-${items[j]}`);
    i = j + 1;
  }
  return out.join(",");
}

// ================================================================================== vecmath

const floats = (v) => Array.from(v, Number);

export function dot(a, b) {
  const x = floats(a), y = floats(b);
  let s = 0;
  for (let i = 0; i < Math.min(x.length, y.length); i++) s += x[i] * y[i];
  return s;
}

export function normalize(v) {
  const xs = floats(v);
  let n = 0;
  for (const x of xs) n += x * x;
  n = Math.sqrt(n);
  return n > 0 ? xs.map((x) => x / n) : xs;
}

export function cosine(a, b) {
  const x = floats(a), y = floats(b);
  if (x.length !== y.length || !x.length) return 0;
  let na = 0, nb = 0, d = 0;
  for (let i = 0; i < x.length; i++) { na += x[i] * x[i]; nb += y[i] * y[i]; d += x[i] * y[i]; }
  if (na === 0 || nb === 0) return 0;
  return d / (Math.sqrt(na) * Math.sqrt(nb));
}

export function packVec(v) {
  const xs = floats(v);
  const buf = Buffer.alloc(xs.length * 4);
  xs.forEach((x, i) => buf.writeFloatLE(x, i * 4));
  return buf;
}

export function unpackVec(blob, opts = null) {
  const dim = opts !== null && typeof opts === "object" ? (opts.dim ?? null) : opts;
  const b = toBuf(blob);
  if (b.length % 4 || (dim !== null && b.length !== 4 * dim)) {
    throw new Error(`vector blob of ${b.length} bytes is not ${dim !== null ? dim : "a whole number of"} float32 values`);
  }
  const out = [];
  for (let i = 0; i < b.length; i += 4) out.push(b.readFloatLE(i));
  return out;
}

export function topk(matrix, query, k, opts = {}) {
  const o = typeof opts === "number" ? { minScore: opts } : opts;
  const { minScore = 0, normalized = false } = o;
  const kk = Math.trunc(k);
  if (kk <= 0) return [];
  const q = floats(query);
  let qn = 0;
  for (const x of q) qn += x * x;
  qn = Math.sqrt(qn);
  const scored = [];
  matrix.forEach((row, i) => {
    const r = floats(row);
    if (r.length !== q.length) throw new Error("row length does not match the query length");
    let d = 0, rn = 0;
    for (let j = 0; j < r.length; j++) { d += r[j] * q[j]; rn += r[j] * r[j]; }
    const s = normalized ? d : rn > 0 && qn > 0 ? d / (Math.sqrt(rn) * qn) : 0;
    if (s >= minScore) scored.push([i, s]);
  });
  scored.sort((a, b) => b[1] - a[1] || a[0] - b[0]);
  return scored.slice(0, kk);
}

export function rrf(rankings, { weights = null, k = 60 } = {}) {
  const fused = new Map();
  rankings.forEach((ranking, i) => {
    const w = weights && i < weights.length ? Number(weights[i]) : 1.0;
    ranking.forEach((item, idx) => fused.set(item, (fused.get(item) ?? 0) + w / (k + idx + 1)));
  });
  const order = new Map([...fused.keys()].map((x, n) => [x, n]));
  return [...fused.entries()].sort((a, b) => b[1] - a[1] || order.get(a[0]) - order.get(b[0]));
}

export function minmaxFuse(scored, { weights = null } = {}) {
  const lists = scored.map((l) => [...l]);
  if (!lists.length) return [];
  const ws = weights ? weights.map(Number) : lists.map(() => 1.0 / lists.length);
  const fused = new Map();
  lists.forEach((items, i) => {
    if (!items.length) return;
    const lo = Math.min(...items.map(([, s]) => s));
    const hi = Math.max(...items.map(([, s]) => s));
    const w = i < ws.length ? ws[i] : 0;
    for (const [item, s] of items) {
      const scaled = hi - lo < 1e-12 ? 1.0 : (s - lo) / (hi - lo);
      fused.set(item, (fused.get(item) ?? 0) + w * scaled);
    }
  });
  const order = new Map([...fused.keys()].map((x, n) => [x, n]));
  return [...fused.entries()].sort((a, b) => b[1] - a[1] || order.get(a[0]) - order.get(b[0]));
}

// ================================================================================== citations

export const CITE_KINDS = ["doc", "page", "book", "chat", "link", "mail", "code"];
const WORDS = {
  es: { q: ["«", "»"], page: "p.", chapter: "cap.", line: "l.", turn: "turno", chat: "chat", link: "enlace", mail: "correo" },
  en: { q: ["“", "”"], page: "p.", chapter: "ch.", line: "line", turn: "turn", chat: "chat", link: "link", mail: "mail" },
};
const cs = (v) => (v == null ? "" : v instanceof Date ? v.toISOString() : String(v).trim());

export function cite(kind, title, { page = null, section = null, line = null, date = null, turn = null, site = null, lang = "es" } = {}) {
  if (!CITE_KINDS.includes(kind)) throw new Error(`unknown citation kind '${kind}'; use one of ${CITE_KINDS.join(", ")}`);
  const w = WORDS[String(lang).toLowerCase().startsWith("en") ? "en" : "es"];
  const [lq, rq] = w.q;
  const t = cs(title), sec = cs(section), ln = cs(line), pg = cs(page);
  const quoted = `${lq}${t}${rq}`;
  if (kind === "doc" || kind === "page") {
    if (pg) return `${quoted}, ${w.page} ${pg}` + (sec && kind === "doc" ? ` · ${sec}` : "");
    if (kind === "page") return quoted;
    if (sec) return `${t} § ${sec}`;
    if (ln) return `${t}, ${w.line} ${ln}`;
    return t;
  }
  if (kind === "book") return sec ? `${quoted}, ${w.chapter} ${sec}` : quoted;
  if (kind === "chat") {
    const parts = [cs(date), sec, cs(turn) ? `${w.turn} ${cs(turn)}` : ""].filter(Boolean);
    return `[${w.chat} ${quoted}` + (parts.length ? ` · ${parts.join(" · ")}]` : "]");
  }
  if (kind === "link") { const s = cs(site); return `[${w.link} ${quoted}` + (s ? ` · ${s}]` : "]"); }
  if (kind === "mail") {
    const parts = [cs(site), cs(date)].filter(Boolean);
    return `[${w.mail} ${quoted}` + (parts.length ? ` · ${parts.join(" · ")}]` : "]");
  }
  let out = t;
  if (sec) out += ` § ${sec}`;
  if (ln) out += `:${ln}`;
  return out;
}
