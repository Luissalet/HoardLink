// idcheck.js — the Node twin of hoard_link/idcheck.py (ESM, no dependencies).
//
//   import { dniOk, ibanOk, eanOk, scanPii, maskText, identifiers } from "./hoard-commons/idcheck.js";
//
// Same checks and regexes as the Python module; both are checked against tests/vectors/idcheck.json.

export const KINDS = ["DNI", "NIE", "CIF", "IBAN", "CARD", "EMAIL", "PHONE_ES"];

const DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE";
const IBAN_LENGTHS = {
  AD: 24, AT: 20, BE: 16, BG: 22, CH: 21, CY: 28, CZ: 24, DE: 22, DK: 18, EE: 20, ES: 24, FI: 18, FR: 27, GB: 22, GI: 23,
  GR: 27, HR: 21, HU: 28, IE: 22, IS: 26, IT: 27, LI: 21, LT: 20, LU: 20, LV: 21, MC: 27, MT: 31, NL: 18, NO: 15, PL: 28,
  PT: 25, RO: 24, SE: 24, SI: 19, SK: 24, SM: 27,
};

const str = (v) => (v === null || v === undefined ? "" : String(v));
const digitsOf = (v) => str(v).replace(/[ -]/g, "");
const bare = (v) => str(v).replace(/[.\- ]/g, "").toUpperCase();

export function luhnOk(value) {
  const s = digitsOf(value);
  if (s.length < 2 || !/^[0-9]+$/.test(s)) return false;
  let total = 0, flip = false;
  for (let i = s.length - 1; i >= 0; i--) {
    let d = Number(s[i]);
    if (flip) { d *= 2; if (d > 9) d -= 9; }
    total += d;
    flip = !flip;
  }
  return total % 10 === 0;
}

export function ibanOk(value) {
  const s = digitsOf(value).toUpperCase();
  if (!/^[A-Z]{2}[0-9]{2}[A-Z0-9]+$/.test(s)) return false;
  const want = IBAN_LENGTHS[s.slice(0, 2)];
  if ((want !== undefined && s.length !== want) || (want === undefined && !(s.length >= 15 && s.length <= 34))) return false;
  let rem = 0;
  for (const ch of s.slice(4) + s.slice(0, 4)) {
    const expanded = /[A-Z]/.test(ch) ? String(ch.charCodeAt(0) - 55) : ch;
    for (const d of expanded) rem = (rem * 10 + Number(d)) % 97;
  }
  return rem === 1;
}

export function dniOk(value) {
  const m = /^([0-9]{7,8})([A-Z])$/.exec(bare(value));
  return !!m && DNI_LETTERS[Number(m[1]) % 23] === m[2];
}

export function nieOk(value) {
  const m = /^([XYZ])([0-9]{7})([A-Z])$/.exec(bare(value));
  return !!m && DNI_LETTERS[("XYZ".indexOf(m[1]) * 10000000 + Number(m[2])) % 23] === m[3];
}

export function cifOk(value) {
  const m = /^([ABCDEFGHJNPQRSUVW])([0-9]{7})([0-9A-J])$/.exec(bare(value));
  if (!m) return false;
  const [, kind, body, control] = m;
  let total = 0;
  [...body].forEach((ch, i) => {
    let d = Number(ch);
    if (i % 2 === 0) { d *= 2; d = Math.floor(d / 10) + (d % 10); }
    total += d;
  });
  const digit = (10 - (total % 10)) % 10;
  const letter = "JABCDEFGHI"[digit];
  if ("PQRSNW".includes(kind)) return control === letter;
  if ("ABEH".includes(kind)) return control === String(digit);
  return control === String(digit) || control === letter;
}

export function nifOk(value) {
  const s = bare(value);
  if (dniOk(s) || nieOk(s) || cifOk(s)) return true;
  const m = /^[KLM]([0-9]{7})([A-Z])$/.exec(s);
  return !!m && DNI_LETTERS[Number(m[1]) % 23] === m[2];
}

export function eanOk(value) {
  const s = digitsOf(value);
  if (!/^[0-9]+$/.test(s) || ![8, 12, 13, 14].includes(s.length)) return false;
  let total = 0;
  const payload = [...s.slice(0, -1)].reverse();
  payload.forEach((ch, i) => { total += Number(ch) * (i % 2 === 0 ? 3 : 1); });
  return (10 - (total % 10)) % 10 === Number(s[s.length - 1]);
}

export function isbnOk(value) {
  const s = digitsOf(value).toUpperCase();
  if (/^[0-9]{13}$/.test(s)) return (s.startsWith("978") || s.startsWith("979")) && eanOk(s);
  if (/^[0-9]{9}[0-9X]$/.test(s)) {
    let total = 0;
    [...s].forEach((ch, i) => { total += (10 - i) * (ch === "X" ? 10 : Number(ch)); });
    return total % 11 === 0;
  }
  return false;
}

// ---------------------------------------------------------------- product ids
const ASIN_URL = String.raw`/(?:dp|gp/product|gp/aw/d|product-reviews|exec/obidos/asin)/([A-Za-z0-9]{10})(?![A-Za-z0-9])`;
const ASIN_BARE = String.raw`(?<![A-Za-z0-9])(B0[A-Z0-9]{8})(?![A-Za-z0-9])`;
const GTIN_CAND = String.raw`(?<![0-9])[0-9]{8,14}(?![0-9])`;
const ISBN_LABEL = String.raw`isbn(?:-1[03])?[^0-9]{0,8}([0-9][0-9\- ]{8,16}[0-9Xx])`;
const SKU = String.raw`(?:/p/|/product/|/producto/|/ip/|sku=|pid=|id=)([A-Za-z0-9_-]{5,})`;

function unquote(s) {
  try { return decodeURIComponent(s); } catch { return s; }
}

export function asin(text) {
  const s = unquote(str(text));
  let m = new RegExp(ASIN_URL).exec(s);
  if (m) return m[1].toUpperCase();
  m = new RegExp(ASIN_BARE).exec(s);
  return m ? m[1] : null;
}

const uniq = (items) => [...new Set(items)];

export function identifiers(text, url = null) {
  const hay = unquote(`${str(text)}\n${str(url)}`);
  const eans = [...hay.matchAll(new RegExp(GTIN_CAND, "g"))].map((m) => m[0]).filter((c) => [8, 12, 13, 14].includes(c.length) && eanOk(c));
  const isbns = eans.filter((e) => e.length === 13 && isbnOk(e));
  for (const m of hay.matchAll(new RegExp(ISBN_LABEL, "gi"))) {
    const cleaned = digitsOf(m[1]).toUpperCase();
    if (isbnOk(cleaned)) isbns.push(cleaned);
  }
  const asins = [...hay.matchAll(new RegExp(ASIN_URL, "g"))].map((m) => m[1].toUpperCase())
    .concat([...hay.matchAll(new RegExp(ASIN_BARE, "g"))].map((m) => m[1]));
  let skus = [...hay.matchAll(new RegExp(SKU, "gi"))].map((m) => m[1].toUpperCase()).filter((v) => /[0-9]/.test(v));
  const asinSet = new Set(asins);
  skus = skus.filter((v) => !asinSet.has(v));
  return { ean: uniq(eans), asin: uniq(asins), isbn: uniq(isbns), sku: uniq(skus) };
}

// ---------------------------------------------------------------- personal data
const RX = {
  IBAN: String.raw`(?<![A-Za-z0-9])[A-Z]{2}[0-9]{2}(?:[ -]?[A-Z0-9]{4}){3,7}(?:[ -]?[A-Z0-9]{1,4})?(?![A-Za-z0-9])`,
  DNI: String.raw`(?<![A-Za-z0-9.])[0-9]{2}\.?[0-9]{3}\.?[0-9]{3}-?[A-Za-z](?![A-Za-z0-9])`,
  NIE: String.raw`(?<![A-Za-z0-9])[XYZxyz]-?[0-9]{7}-?[A-Za-z](?![A-Za-z0-9])`,
  CIF: String.raw`(?<![A-Za-z0-9])[ABCDEFGHJNPQRSUVW]-?[0-9]{7}-?[0-9A-J](?![A-Za-z0-9])`,
  CARD: String.raw`(?<![A-Za-z0-9])(?:[0-9][ -]?){12,18}[0-9](?![A-Za-z0-9])`,
  EMAIL: String.raw`(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9-])`,
  PHONE_PREFIX: String.raw`(?<![A-Za-z0-9+])(?:\+34|0034)[ .-]?[6-9][0-9]{2}[ .-]?[0-9]{3}[ .-]?[0-9]{3}(?![0-9])`,
  PHONE_LABEL: String.raw`(?:tel[eé]fono|tel\.?|tfno\.?|m[oó]vil|phone|mobile|contacto|whatsapp)(?:\s*[:.]?\s*)((?:\+|00)?[0-9][0-9 .-]{7,16}[0-9])`,
  PHONE_GROUPED: String.raw`(?<![A-Za-z0-9+.-])[6-9][0-9]{2}(?:[ .-][0-9]{3}[ .-][0-9]{3}|[ .-][0-9]{2}[ .-][0-9]{2}[ .-][0-9]{2})(?![A-Za-z0-9-]|[.,][0-9])`,
};

function isEsPhone(raw) {
  let d = raw.replace(/[^0-9]/g, "");
  if (d.startsWith("0034")) d = d.slice(4);
  else if (d.startsWith("34") && d.length === 11) d = d.slice(2);
  return d.length === 9 && "6789".includes(d[0]);
}

function trimIban(raw) {
  const clean = raw.replace(/[ -]/g, "");
  const want = IBAN_LENGTHS[clean.slice(0, 2)];
  if (want === undefined || clean.length <= want) return raw;
  let seen = 0;
  for (let i = 0; i < raw.length; i++) {
    if (raw[i] !== " " && raw[i] !== "-") {
      seen++;
      if (seen === want) return raw.slice(0, i + 1);
    }
  }
  return raw;
}

export function scanPii(text, kinds = null) {
  const s = str(text);
  const want = new Set(kinds === null || kinds === undefined ? KINDS : kinds);
  const raw = [];
  const add = (kind, start, end) => { if (want.has(kind)) raw.push({ kind, start, end, value: s.slice(start, end) }); };
  for (const m of s.matchAll(new RegExp(RX.IBAN, "g"))) {
    const value = trimIban(m[0]);
    if (ibanOk(value)) add("IBAN", m.index, m.index + value.length);
  }
  for (const m of s.matchAll(new RegExp(RX.DNI, "g"))) if (dniOk(m[0])) add("DNI", m.index, m.index + m[0].length);
  for (const m of s.matchAll(new RegExp(RX.NIE, "g"))) if (nieOk(m[0])) add("NIE", m.index, m.index + m[0].length);
  for (const m of s.matchAll(new RegExp(RX.CIF, "g"))) if (cifOk(m[0])) add("CIF", m.index, m.index + m[0].length);
  for (const m of s.matchAll(new RegExp(RX.CARD, "g"))) {
    const digits = m[0].replace(/[^0-9]/g, "");
    if (digits.length >= 13 && digits.length <= 19 && luhnOk(digits)) add("CARD", m.index, m.index + m[0].length);
  }
  for (const m of s.matchAll(new RegExp(RX.EMAIL, "g"))) add("EMAIL", m.index, m.index + m[0].length);
  for (const m of s.matchAll(new RegExp(RX.PHONE_PREFIX, "g"))) add("PHONE_ES", m.index, m.index + m[0].length);
  for (const m of s.matchAll(new RegExp(RX.PHONE_LABEL, "gi"))) {
    if (isEsPhone(m[1])) {
      const start = m.index + m[0].length - m[1].length;
      add("PHONE_ES", start, start + m[1].length);
    }
  }
  for (const m of s.matchAll(new RegExp(RX.PHONE_GROUPED, "g"))) add("PHONE_ES", m.index, m.index + m[0].length);
  raw.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  const out = [];
  for (const h of raw) {
    if (out.length && h.start < out[out.length - 1].end) continue;
    out.push(h);
  }
  return out;
}

export function maskText(text, kinds = null, token = "<{kind}>") {
  const s = str(text);
  let out = s;
  for (const h of scanPii(s, kinds).reverse()) out = out.slice(0, h.start) + token.split("{kind}").join(h.kind) + out.slice(h.end);
  return out;
}

export function maskObj(obj, { kinds = null, token = "<{kind}>" } = {}) {
  if (typeof obj === "string") return maskText(obj, kinds, token);
  if (Array.isArray(obj)) return obj.map((v) => maskObj(v, { kinds, token }));
  if (obj && typeof obj === "object") return Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, maskObj(v, { kinds, token })]));
  return obj;
}
