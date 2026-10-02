// text.js — the Node twin of hoard_link/text.py (ESM, no dependencies).
//
//   import { fold, slugify, safeFilename, normalizeEmail } from "./hoard-commons/text.js";
//
// Same rules as the Python module; both are checked against tests/vectors/text.json.

import fs from "node:fs";
import crypto from "node:crypto";

const MN = /\p{Mn}/u;

export function foldChar(ch) {
  let base = ch.normalize("NFD").slice(0, 1) || ch;
  // a surrogate pair is one character for Python; keep it whole
  if (ch.length === 2 && ch.codePointAt(0) > 0xffff) base = ch;
  if (MN.test(base)) base = ch;
  const low = base.toLowerCase();
  return [...low].length === 1 ? low : base;
}

export function fold(text, { keepLength = true } = {}) {
  const s = text == null ? "" : String(text);
  if (keepLength) return Array.from(s, foldChar).join("");
  return s.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().replace(/ß/g, "ss");
}

export function slugify(text, { maxLen = 80, sep = "-", fallback = "" } = {}) {
  let s = fold(text, { keepLength: false }).normalize("NFKD").replace(/[^\x00-\x7f]/g, "");
  const esc = sep.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  s = s.replace(/[^a-z0-9]+/g, sep).replace(new RegExp(`^(${esc})+|(${esc})+$`, "g"), "");
  if (maxLen && s.length > maxLen) {
    let cut = s.slice(0, maxLen);
    if (cut.includes(sep) && s.slice(maxLen, maxLen + 1) !== sep) {
      const head = cut.slice(0, cut.lastIndexOf(sep));
      if (head.length >= Math.floor(maxLen / 2)) cut = head;
    }
    s = cut.replace(new RegExp(`^(${esc})+|(${esc})+$`, "g"), "");
  }
  return s || fallback;
}

const RESERVED = new Set(["con", "prn", "aux", "nul", ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`), ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`)]);

export function safeFilename(name, { maxLen = 120, fallback = "file", keepExtension = true } = {}) {
  let s = (name == null ? "" : String(name)).normalize("NFC");
  s = s.replace(/[<>:"/\\|?*\x00-\x1f]+/g, "_");
  s = s.replace(/\s+/g, " ").trim().replace(/^\.+|\.+$/g, "").trim();
  let stem = s, ext = "";
  if (keepExtension) {
    const m = /^(.+?)(\.[A-Za-z0-9]{1,11})$/.exec(s);
    if (m && !m[1].endsWith(".") ) { stem = m[1]; ext = m[2]; }
    // Python's splitext: a name that starts with a dot has no extension
    if (s.startsWith(".") && s.indexOf(".", 1) === -1) { stem = s; ext = ""; }
  }
  if (RESERVED.has(stem.split(".")[0].toLowerCase())) stem = "_" + stem;
  const room = Math.max(1, maxLen - ext.length);
  stem = stem.slice(0, room).replace(/[ .]+$/, "");
  const out = (stem || fallback) + ext;
  return out.replace(/^[. ]+|[. ]+$/g, "") ? out : fallback;
}

export function clampText(text, limit, { ellipsis = "…" } = {}) {
  const s = (text == null ? "" : String(text)).replace(/\s+/g, " ").trim();
  if (limit <= 0 || s.length <= limit) return s;
  let cut = s.slice(0, Math.max(0, limit - ellipsis.length));
  const space = cut.lastIndexOf(" ");
  if (space >= cut.length * 0.6) cut = cut.slice(0, space);
  return cut.replace(/[ ,;:.\-]+$/, "") + ellipsis;
}

export function sha256Text(text) {
  return crypto.createHash("sha256").update(text == null ? "" : String(text), "utf8").digest("hex");
}

export function sha256File(file) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash("sha256");
    fs.createReadStream(file).on("data", (b) => h.update(b)).on("end", () => resolve(h.digest("hex"))).on("error", reject);
  });
}

export function nowIso() {
  const d = new Date();
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${sign}${pad(off / 60)}:${pad(off % 60)}`;
}

const GMAIL = new Set(["gmail.com", "googlemail.com"]);

export function normalizeEmail(addr, { stripTag = true } = {}) {
  let s = addr == null ? "" : String(addr).trim();
  const m = /<([^<>]+)>/.exec(s);
  if (m) s = m[1];
  s = s.trim().replace(/^<+|>+$/g, "").trim().toLowerCase();
  if (s.startsWith("mailto:")) s = s.slice(7);
  if ((s.match(/@/g) || []).length !== 1) return "";
  let [local, domain] = s.split("@");
  domain = domain.replace(/^\.+|\.+$/g, "");
  if (!local || !domain.includes(".")) return "";
  if (stripTag && local.includes("+")) local = local.split("+")[0];
  if (GMAIL.has(domain)) { local = local.replace(/\./g, ""); domain = "gmail.com"; }
  return local ? `${local}@${domain}` : "";
}

const CC = { ES: "34", FR: "33", PT: "351", IT: "39", DE: "49", GB: "44", UK: "44", US: "1", MX: "52", AR: "54", NL: "31", BE: "32", IE: "353", CH: "41" };

export function normalizePhone(num, region = "ES") {
  const s = num == null ? "" : String(num).trim();
  if (!s) return "";
  const plus = s.startsWith("+") || s.startsWith("00");
  let digits = s.replace(/\D/g, "");
  if (s.startsWith("00")) digits = digits.slice(2);
  if (!digits || digits.length < 6) return "";
  if (plus) return "+" + digits;
  const cc = CC[(region || "").toUpperCase()] || "";
  if (cc === "34" && digits.length === 11 && digits.startsWith("34")) return "+" + digits;
  if (cc) return cc !== "39" ? "+" + cc + digits.replace(/^0+/, "") : "+" + cc + digits;
  return digits;
}

export function tokens(text) {
  return fold(text, { keepLength: false }).match(/[a-z0-9]+/g) || [];
}

export function nameSimilarity(a, b) {
  const ta = tokens(a), tb = tokens(b);
  if (!ta.length || !tb.length) return 0;
  if (ta.join(" ") === tb.join(" ") || [...ta].sort().join(" ") === [...tb].sort().join(" ")) return 1;
  const sa = new Set(ta), sb = new Set(tb);
  let hits = [...sa].filter((x) => sb.has(x)).length;
  const onlyB = [...sb].filter((y) => !sa.has(y));
  for (const x of sa) if (!sb.has(x) && x.length === 1 && onlyB.some((y) => y.startsWith(x))) hits += 0.5;
  const union = new Set([...sa, ...sb]).size;
  return union ? Math.round(Math.min(1, hits / union) * 10000) / 10000 : 0;
}

export function domainOf(value) {
  let s = value == null ? "" : String(value).trim().toLowerCase();
  if (!s) return "";
  if (s.includes("@") && !s.includes("://")) s = s.slice(s.lastIndexOf("@") + 1);
  else {
    s = s.replace(/^[a-z][a-z0-9+.-]*:\/\//, "");
    s = s.split("/")[0].split("?")[0].split("#")[0];
    s = s.slice(s.lastIndexOf("@") + 1);
    if (s.startsWith("[")) return s.split("]")[0].replace(/^\[/, "");
    s = s.split(":")[0];
  }
  s = s.replace(/^\.+|[.>]+$/g, "").trim();
  return s.startsWith("www.") ? s.slice(4) : s;
}
