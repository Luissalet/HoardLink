// money.js — the Node twin of hoard_link/money.py (ESM, no dependencies).
//
//   import { parseAmount, parseCents, findPrices, formatMoney, splitShares, settle } from "./hoard-commons/money.js";
//
// Same rules as the Python module (see its docstring for how "1.234" and "2,099" are read); both are checked against
// tests/vectors/money.json. parseAmount returns a Number rounded to two decimals (half up) or null; use parseCents for
// exact integer work. Python's Decimal results are plain Numbers here.

import { fold } from "./text.js";

export const CURRENCIES = {
  EUR: { symbol: "€", decimals: 2, name: "Euro", dot: false, aliases: ["€", "eur", "euro", "euros"] },
  USD: { symbol: "$", decimals: 2, name: "US dollar", dot: true, aliases: ["us$", "$", "usd", "dolar", "dolares", "dólar", "dólares", "dollar", "dollars"] },
  GBP: { symbol: "£", decimals: 2, name: "Pound sterling", dot: true, aliases: ["£", "gbp"] },
  CHF: { symbol: "CHF", decimals: 2, name: "Swiss franc", dot: true, aliases: ["chf", "sfr"] },
  SEK: { symbol: "SEK", decimals: 2, name: "Swedish krona", dot: false, aliases: ["sek"] },
  DKK: { symbol: "DKK", decimals: 2, name: "Danish krone", dot: false, aliases: ["dkk"] },
  NOK: { symbol: "NOK", decimals: 2, name: "Norwegian krone", dot: false, aliases: ["nok"] },
  PLN: { symbol: "zł", decimals: 2, name: "Polish zloty", dot: false, aliases: ["pln", "zł"] },
  CZK: { symbol: "Kč", decimals: 2, name: "Czech koruna", dot: false, aliases: ["czk", "kč"] },
  JPY: { symbol: "¥", decimals: 0, name: "Japanese yen", dot: true, aliases: ["jpy", "¥"] },
  MXN: { symbol: "MX$", decimals: 2, name: "Mexican peso", dot: true, aliases: ["mxn", "mx$"] },
  CAD: { symbol: "CA$", decimals: 2, name: "Canadian dollar", dot: true, aliases: ["cad", "ca$", "c$"] },
  AUD: { symbol: "A$", decimals: 2, name: "Australian dollar", dot: true, aliases: ["aud", "au$", "a$"] },
  BRL: { symbol: "R$", decimals: 2, name: "Brazilian real", dot: false, aliases: ["brl", "r$"] },
  CNY: { symbol: "CN¥", decimals: 2, name: "Chinese yuan", dot: true, aliases: ["cny", "rmb"] },
  INR: { symbol: "₹", decimals: 2, name: "Indian rupee", dot: true, aliases: ["inr", "₹"] },
  ARS: { symbol: "AR$", decimals: 2, name: "Argentine peso", dot: false, aliases: ["ars"] },
  HUF: { symbol: "Ft", decimals: 2, name: "Hungarian forint", dot: false, aliases: ["huf"] },
  RON: { symbol: "lei", decimals: 2, name: "Romanian leu", dot: false, aliases: ["ron"] },
  TRY: { symbol: "₺", decimals: 2, name: "Turkish lira", dot: false, aliases: ["₺"] },
};

const ALIAS_TO_CODE = {};
for (const [code, cur] of Object.entries(CURRENCIES)) for (const alias of cur.aliases) ALIAS_TO_CODE[alias.toLowerCase()] = code;

const escapeRx = (s) => s.replace(/[.*+?^${}()|[\]\\\-]/g, "\\$&");
const isAlpha = (ch) => /\p{L}/u.test(ch);

function aliasPattern() {
  const alts = Object.keys(ALIAS_TO_CODE).sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0)).map((alias) => {
    let body = escapeRx(alias);
    if (isAlpha(alias[0])) body = "(?<![A-Za-z])" + body;
    if (isAlpha(alias[alias.length - 1])) body += "(?![A-Za-z])";
    return body;
  });
  return "(?:" + alts.join("|") + ")";
}
const CUR = aliasPattern();
const CUR_RX = new RegExp(CUR, "gi");

export function currencyOf(marker) {
  return ALIAS_TO_CODE[String(marker ?? "").trim().toLowerCase()] || "";
}

// ---------------------------------------------------------------- exact decimals (digit strings, BigInt)

/** {neg, int, frac} digit strings of a finite Number (shortest round-trip form), or null. */
function numberParts(x) {
  if (!Number.isFinite(x)) return null;
  let s = String(x);
  if (/e/i.test(s)) s = x.toFixed(20).replace(/0+$/, "").replace(/\.$/, "");
  const neg = s.startsWith("-");
  if (neg) s = s.slice(1);
  const [int, frac = ""] = s.split(".");
  return { neg, int, frac };
}

/** Integer units of 10^-places of {neg,int,frac}, rounded half up (away from zero), as BigInt. */
function scaled(parts, places) {
  const frac = (parts.frac + "0".repeat(places + 1)).slice(0, places + 1);
  let n = BigInt((parts.int || "0") + frac.slice(0, places));
  if (frac.charCodeAt(places) - 48 >= 5) n += 1n;
  return parts.neg ? -n : n;
}

// ---------------------------------------------------------------- parsing

function stripCurrency(text) {
  let found = "";
  const cleaned = text.replace(CUR_RX, (m) => {
    if (!found) found = ALIAS_TO_CODE[m.toLowerCase()] || "";
    return "";
  });
  return [cleaned, found];
}

function dotContext(currency, lang) {
  const cur = CURRENCIES[String(currency || "").toUpperCase()];
  if (cur) return !!cur.dot;
  return String(lang || "es").toLowerCase().startsWith("en");
}

function numberPartsOfText(raw, decimal, dotCtx) {
  let s = raw.replace(/[\s'’]/g, "");
  if (!s) return null;
  let neg = false;
  if (s.startsWith("(") && s.endsWith(")")) { neg = true; s = s.slice(1, -1); }
  if (s.startsWith("+")) s = s.slice(1);
  if (s.startsWith("-") || s.startsWith("−") || s.startsWith("–")) { neg = !neg; s = s.slice(1); }
  else if (s.endsWith("-")) { neg = !neg; s = s.slice(0, -1); }
  if (!/^[0-9.,]+$/.test(s) || !/[0-9]/.test(s)) return null;

  let dec = null, thousands = null;
  if (decimal === "," || decimal === ".") {
    dec = decimal;
    thousands = decimal === "," ? "." : ",";
  } else {
    const hasComma = s.includes(","), hasDot = s.includes(".");
    if (hasComma && hasDot) {
      dec = s.lastIndexOf(",") > s.lastIndexOf(".") ? "," : ".";
      thousands = dec === "," ? "." : ",";
    } else if (hasComma || hasDot) {
      const sep = hasComma ? "," : ".";
      const parts = s.split(sep);
      if (parts.length > 2) thousands = sep;
      else {
        const [head, tail] = parts;
        if (tail.length === 3 && head.length >= 1 && head.length <= 3 && !head.startsWith("0")) {
          if (sep === "." && dotCtx) dec = ".";
          else thousands = sep;
        } else dec = sep;
      }
    }
  }
  let intRaw = s, frac = "";
  if (dec) {
    const parts = s.split(dec);
    if (parts.length > 2) return null;
    intRaw = parts[0];
    frac = parts.length === 2 ? parts[1] : "";
  }
  if (thousands && intRaw.includes(thousands)) {
    const groups = intRaw.split(thousands);
    if (!(groups[0].length >= 1 && groups[0].length <= 3) || groups.slice(1).some((g) => g.length !== 3)) return null;
    intRaw = groups.join("");
  }
  if (!/^[0-9]*$/.test(intRaw) || !/^[0-9]*$/.test(frac)) return null;
  if (!intRaw && !frac) return null;
  return { neg, int: intRaw || "0", frac };
}

function partsOf(text, { decimal = null, currencyHint = null, lang = "es" } = {}) {
  if (text === null || text === undefined || typeof text === "boolean") return null;
  if (typeof text === "number") return numberParts(text);
  if (typeof text !== "string") return null;
  const [cleaned, cur] = stripCurrency(text);
  return numberPartsOfText(cleaned, decimal, dotContext(cur || currencyHint || "", lang));
}

/** Integer cents (half up) of an amount string or number, or null. */
export function parseCents(text, opts = {}) {
  const parts = partsOf(text, opts);
  if (!parts) return null;
  const cents = scaled(parts, 2);
  if (cents > BigInt(Number.MAX_SAFE_INTEGER) || cents < -BigInt(Number.MAX_SAFE_INTEGER)) return null;
  return Number(cents);
}

/** The amount as a Number rounded to two decimals (half up), or null. */
export function parseAmount(text, opts = {}) {
  const cents = parseCents(text, opts);
  return cents === null ? null : cents / 100;
}

export function toCents(value) {
  return parseCents(value);
}

export function fromCents(cents) {
  return Number(cents) / 100;
}

export function detectDecimal(samples) {
  let comma = 0, dot = 0;
  for (const raw of samples) {
    const v = String(raw ?? "").replace(CUR_RX, "").replace(/\s/g, "");
    if (/,[0-9]{1,2}$/.test(v)) comma++;
    if (/\.[0-9]{1,2}$/.test(v)) dot++;
    if (/\.[0-9]{3},[0-9]+$/.test(v)) comma++;
    if (/,[0-9]{3}\.[0-9]+$/.test(v)) dot++;
  }
  if (comma === 0 && dot === 0) return null;
  return comma >= dot ? "," : ".";
}

// ---------------------------------------------------------------- prices in text

const NUM = "[0-9]{1,3}(?:[ \\u00a0\\u202f.,'][0-9]{3})+(?:[.,][0-9]{1,2})?(?![0-9])|[0-9]+(?:[.,][0-9]{1,2})?(?![0-9])";
const SIGN = "(?:(?<![0-9A-Za-z])([-−])[ ]?)?";
const SUFFIX_SRC = SIGN + "(?<![0-9.,])(" + NUM + ")[ \\u00a0]?(" + CUR + ")(?![0-9])";
const PREFIX_SRC = SIGN + "(" + CUR + ")[ \\u00a0]?([-−])?(?<![0-9.,])(" + NUM + ")";

const LABELS = ["importe total a pagar", "total a pagar", "importe a pagar", "importe a cargar", "importe total", "total factura",
  "total pedido", "precio total", "precio final", "amount due", "total due", "total amount", "subtotal", "total",
  "importe", "precio", "price", "amount", "cuota", "pvp", "prima", "iva", "vat", "tax", "envio", "shipping",
  "descuento", "discount", "cargo", "pagado", "paid"];
const LABEL_RX = new RegExp("(" + [...LABELS].sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0)).map(escapeRx).join("|") + ")[^a-z0-9]{0,12}$");

function labelBefore(text, start) {
  const lineStart = text.lastIndexOf("\n", start - 1) + 1;
  const segment = fold(text.slice(Math.max(lineStart, start - 60), start));
  const m = LABEL_RX.exec(segment);
  return m ? m[1] : "";
}

export function findPrices(text, { labelledOnly = false, lang = "es" } = {}) {
  const s = text === null || text === undefined ? "" : String(text);
  const raw = [];
  for (const m of s.matchAll(new RegExp(SUFFIX_SRC, "gi"))) raw.push([m.index, m.index + m[0].length, m[1] || "", m[2], m[3]]);
  for (const m of s.matchAll(new RegExp(PREFIX_SRC, "gi"))) raw.push([m.index, m.index + m[0].length, m[1] || m[3] || "", m[4], m[2]]);
  raw.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const out = [];
  let lastEnd = -1;
  for (const [start, end, sign, number, marker] of raw) {
    if (start < lastEnd) continue;
    const code = ALIAS_TO_CODE[marker.toLowerCase()] || "";
    let amount = parseAmount(number, { currencyHint: code, lang });
    if (amount === null) continue;
    if (sign) amount = -amount;
    const label = labelBefore(s, start);
    if (labelledOnly && !label) continue;
    out.push({ amount, currency: code, start, end, label });
    lastEnd = end;
  }
  return out;
}

// ---------------------------------------------------------------- formatting

const STYLE = { es: [".", ",", false], en: [",", ".", true], fr: [" ", ",", false] };
const styleOf = (lang) => STYLE[String(lang || "es").toLowerCase().split("-")[0]] || STYLE.es;
const SYMBOL_FIRST_SHORT = new Set(["€", "$", "£", "¥", "₹", "₺"]);

export function formatMoney(value, currency = "EUR", lang = "es", { trimZeroCents = false, grouping = true, nbsp = false } = {}) {
  const parts = partsOf(value);
  if (!parts) return "";
  const code = String(currency || "EUR").toUpperCase();
  const cur = CURRENCIES[code];
  const decimals = cur ? cur.decimals : 2;
  const symbol = cur ? cur.symbol : code;
  const units = scaled(parts, decimals);                      // exact, half up
  const neg = units < 0n;
  const abs = neg ? -units : units;
  const asNumber = Number(abs) / 10 ** decimals;
  // digits come from Intl (en-US), then the marks are mapped to the language's
  const text = new Intl.NumberFormat("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: grouping }).format(asNumber);
  const [group, decMark, symbolFirst] = styleOf(lang);
  let [intPart, frac = ""] = text.split(".");
  intPart = intPart.replace(/,/g, group);
  if (trimZeroCents && frac && !/[1-9]/.test(frac)) frac = "";
  const number = intPart + (frac ? decMark + frac : "");
  const gap = nbsp ? " " : " ";
  let out;
  if (symbolFirst) out = SYMBOL_FIRST_SHORT.has(symbol) ? symbol + number : symbol + gap + number;
  else out = number + gap + symbol;
  return neg ? "-" + out : out;
}

export function formatCents(cents, currency = "EUR", lang = "es", opts = {}) {
  if (cents === null || cents === undefined) return "";
  return formatMoney(Number(cents) / 100, currency, lang, opts);
}

// ---------------------------------------------------------------- shares and settling

export function splitShares(totalCents, weights) {
  const isMap = !Array.isArray(weights) && weights !== null && typeof weights === "object";
  const keys = isMap ? Object.keys(weights) : weights.map((_, i) => i);
  const ws = keys.map((k) => Number(weights[k]));
  if (ws.some((w) => w < 0) || !ws.some((w) => w > 0)) throw new Error("a split needs at least one positive weight and no negative ones");
  let total = Math.trunc(Number(totalCents));
  const sign = total < 0 ? -1 : 1;
  total = Math.abs(total);
  const whole = ws.filter((w) => w > 0).reduce((a, b) => a + b, 0);
  const raw = ws.map((w) => (w > 0 ? (total * w) / whole : 0));
  const parts = raw.map((v) => Math.floor(v + 1e-9));
  const left = total - parts.reduce((a, b) => a + b, 0);
  const ranked = ws.map((w, i) => i).filter((i) => ws[i] > 0).sort((a, b) => (raw[b] - parts[b]) - (raw[a] - parts[a]) || a - b);
  for (const i of ranked.slice(0, Math.max(0, left))) parts[i] += 1;
  const signed = parts.map((p) => (p === 0 ? 0 : sign * p));
  if (!isMap) return signed;
  const out = {};
  keys.forEach((k, i) => { out[k] = signed[i]; });
  return out;
}

export const EXACT_LIMIT = 16;

const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

export function settle(balances) {
  const entries = balances instanceof Map ? [...balances.entries()] : Object.entries(balances);
  const net = {};
  for (const [k, v] of entries) if (Math.trunc(Number(v)) !== 0) net[k] = Math.trunc(Number(v));
  const ids = Object.keys(net).sort(cmpStr);
  if (!ids.length) return [];
  const groups = ids.length <= EXACT_LIMIT ? exactGroups(ids, net) : [ids];
  const out = [];
  const byAmount = (x, y) => y[0] - x[0] || cmpStr(x[1], y[1]);
  for (const group of groups) {
    const debtors = group.filter((i) => net[i] < 0).map((i) => [-net[i], i]).sort(byAmount);
    const creditors = group.filter((i) => net[i] > 0).map((i) => [net[i], i]).sort(byAmount);
    while (debtors.length && creditors.length) {
      const amount = Math.min(debtors[0][0], creditors[0][0]);
      out.push({ from: debtors[0][1], to: creditors[0][1], cents: amount });
      debtors[0][0] -= amount;
      creditors[0][0] -= amount;
      if (debtors[0][0] === 0) debtors.shift();
      if (creditors.length && creditors[0][0] === 0) creditors.shift();
      debtors.sort(byAmount);
      creditors.sort(byAmount);
    }
  }
  return out;
}

function exactGroups(ids, net) {
  const n = ids.length;
  const size = 1 << n;
  const total = new Array(size).fill(0);
  for (let mask = 1; mask < size; mask++) {
    const low = 31 - Math.clz32(mask & -mask);
    total[mask] = total[mask & (mask - 1)] + net[ids[low]];
  }
  const best = new Array(size).fill(0);
  const pick = new Array(size).fill(0);
  for (let mask = 1; mask < size; mask++) {
    let top = -1, choice = 0;
    for (let i = 0; i < n; i++) {
      if ((mask >> i) & 1) {
        const value = best[mask ^ (1 << i)];
        if (value > top) { top = value; choice = i; }
      }
    }
    best[mask] = top + (total[mask] === 0 ? 1 : 0);
    pick[mask] = choice;
  }
  const order = [];
  let mask = size - 1;
  while (mask) {
    const i = pick[mask];
    order.push(i);
    mask ^= 1 << i;
  }
  order.reverse();
  const groups = [];
  let current = [];
  let running = 0;
  for (const i of order) {
    current.push(ids[i]);
    running += net[ids[i]];
    if (running === 0) { groups.push(current); current = []; }
  }
  if (current.length) groups.push(current);
  return groups;
}
