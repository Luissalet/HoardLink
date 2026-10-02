// merchants.js — the Node twin of hoard_link/merchants.py (ESM, no dependencies; reads ./merchants.json).
//
//   import { lookup, merchantSimilar, merchantKey, findOrderRefs, categoryOf } from "./hoard-commons/merchants.js";
//
// Who a name, a web domain or a mail sender is, whether two names are the same shop, and order references. The data
// file is byte-identical to hoard_link/_data/merchants.json; both implementations are checked against
// tests/vectors/merchants_cases.json. See the Python module for the resolution order and the data format.

import { readFileSync } from "node:fs";
import { fold } from "./text.js";
import { upsValid } from "./tracking.js";

export const DATA = JSON.parse(readFileSync(new URL("./merchants.json", import.meta.url), "utf8"));
export const MERCHANTS = DATA.merchants;

const SUFFIXES = new Set(DATA.legal_suffixes);
const STOP = new Set(DATA.stop_tokens);
const BANK_RX = new RegExp(DATA.bank_pattern);
const NOISE = DATA.noise_senders;

const NON_ALNUM = /[^a-z0-9]+/g;
const DIGITS = /^[0-9]+$/;
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Folded, non-alphanumerics collapsed to single spaces, trimmed (no legal suffix handling). */
export function plainKeyRaw(text) {
  return fold(text, { keepLength: false }).replace(NON_ALNUM, " ").trim();
}

/** Stable key of a name: folded, punctuation collapsed, legal suffix removed unless `stripLegal` is false. */
export function plainKey(name, { stripLegal = true } = {}) {
  let s = fold(name, { keepLength: false });
  if (stripLegal) s = s.replace(/\b([a-z])\.\s?(?=[a-z]\.)/g, "$1").replace(/\b([a-z])\./g, "$1");
  const words = s.replace(NON_ALNUM, " ").split(" ").filter(Boolean);
  if (stripLegal) while (words.length > 1 && SUFFIXES.has(words[words.length - 1])) words.pop();
  return words.join(" ");
}

const PREP = MERCHANTS.map((m) => ({
  m,
  patterns: m.patterns.map((p) => new RegExp(p)),
  names: new Set([plainKeyRaw(m.display), ...m.aliases.map(plainKeyRaw)].filter(Boolean)),
  words: m.aliases.filter((a) => a.length >= 5).map((a) => new RegExp("\\b" + esc(plainKeyRaw(a)) + "\\b")),
  domains: m.domains.map((d) => d.toLowerCase()),
  senders: (m.senders || []).map((s) => s.toLowerCase()),
}));

const ADDR = /<([^<>\s]+@[^<>\s]+)>|([^\s<>,;"']+@[^\s<>,;"']+)/;
const HOST = /^(?:[a-z][a-z0-9+.-]*:\/\/)?(?:[^/@\s]*@)?([a-z0-9][a-z0-9.-]*\.[a-z]{2,})(?::[0-9]+)?(?:[/?#].*)?$/;

function hostOf(text) {
  const m = HOST.exec(text.trim().toLowerCase());
  return m ? m[1] : "";
}

function bySender(address) {
  for (const p of PREP) if (p.senders.includes(address)) return p.m;
  return null;
}

function byDomain(host) {
  let best = null;
  let size = 0;
  for (const p of PREP) {
    for (const d of p.domains) {
      if (d.length > size && (host === d || host.endsWith("." + d))) { best = p.m; size = d.length; }
    }
  }
  return best;
}

function copy(m) {
  const out = {};
  for (const [k, v] of Object.entries(m)) out[k] = Array.isArray(v) ? [...v] : v;
  out.key = m.id;
  return out;
}

function resolve(value) {
  if (value == null) return null;
  const text = String(value).trim();
  if (!text) return null;
  const low = text.toLowerCase();
  const found = ADDR.exec(low);
  if (found) {
    const address = (found[1] || found[2]).replace(/^<|>$/g, "");
    const hit = bySender(address) || byDomain(address.split("@").pop());
    if (hit) return hit;
  } else {
    const host = hostOf(low);
    if (host) {
      const hit = byDomain(host);
      if (hit) return hit;
    }
  }
  const key = plainKey(text);
  if (!key) return null;
  for (const p of PREP) if (p.names.has(key)) return p.m;
  const folded = fold(text, { keepLength: false }).split(/\s+/).filter(Boolean).join(" ");
  const spaced = folded.replace(NON_ALNUM, " ").trim();
  for (const p of PREP) {
    if (p.patterns.some((rx) => rx.test(folded)) || p.words.some((rx) => rx.test(spaced))) return p.m;
  }
  return null;
}

/** The merchant a name, a domain / URL or a mail sender belongs to (a copy of its entry plus `key`), or null. */
export function lookup(nameOrDomainOrSender) {
  const hit = resolve(nameOrDomainOrSender);
  return hit ? copy(hit) : null;
}

/** `lookup(value).id` or null. */
export function merchantId(value) {
  const hit = resolve(value);
  return hit ? hit.id : null;
}

/** Stable grouping key: the id of the known merchant, else plainKey (legal suffix removed). */
export function merchantKey(name) {
  return merchantId(name) || plainKey(name);
}

/** Significant lower-case words (3+ chars, no stop word or number), in order of appearance without repeats. */
export function merchantTokens(text) {
  const out = [];
  for (const t of fold(text, { keepLength: false }).replace(NON_ALNUM, " ").split(" ")) {
    if (t.length >= 3 && !STOP.has(t) && !DIGITS.test(t) && !out.includes(t)) out.push(t);
  }
  return out;
}

/** Do two names plausibly denote the same merchant? (known merchants compare by id, else by their significant words) */
export function merchantSimilar(a, b) {
  if (!a || !b) return false;
  const ka = merchantId(a);
  const kb = merchantId(b);
  if (ka && kb) return ka === kb;
  const ta = merchantTokens(a);
  const tb = merchantTokens(b);
  if (!ta.length || !tb.length) return false;
  const sa = new Set(ta);
  const sb = new Set(tb);
  const common = ta.filter((t) => sb.has(t)).length;
  if (common === sa.size || common === sb.size) return true;
  if (common / (sa.size + sb.size - common) >= 0.5) return true;
  // one word glued onto another: "netflix" / "netflixcom"
  return ta.some((t) => tb.some((u) => t !== u && Math.min(t.length, u.length) >= 4 && (u.includes(t) || t.includes(u))));
}

/** Category id of the known merchant, or null. */
export function categoryOf(name) {
  const hit = resolve(name);
  return hit ? hit.category : null;
}

/** Names an app's own categories commonly have for a merchant category, best first. */
export function categoryHints(category) {
  return [...(DATA.category_hints[String(category || "")] || [])];
}

/** The `CARRIERS` key (tracking.js) of a carrier merchant, else null. */
export function carrierOf(name) {
  const hit = resolve(name);
  return hit && hit.carrier ? hit.tracking_id ?? null : null;
}

const flag = (name, f) => { const hit = resolve(name); return Boolean(hit && hit[f]); };
export const isSubscription = (name) => flag(name, "subscription");
export const isCarrier = (name) => flag(name, "carrier");
export const isGateway = (name) => flag(name, "gateway");

/** A bank, card network or fintech. */
export function isBank(name) {
  if (!name) return false;
  return flag(name, "bank") || BANK_RX.test(fold(name, { keepLength: false }).split(/\s+/).filter(Boolean).join(" "));
}

/** A sender whose mail is marketing or a notification, never a receipt. */
export function isNoiseSender(sender) {
  const s = fold(sender, { keepLength: false });
  return NOISE.some((n) => s.includes(n));
}

/** Every known merchant named in a text, in order of appearance: `{id, start, end}`. */
export function findMerchants(text) {
  const folded = fold(text);
  const spans = [];
  for (const p of PREP) {
    let best = null;
    for (const rx of p.patterns) {
      const m = rx.exec(folded);
      if (m && m[0].length > 0 && (best === null || m.index < best[0])) best = [m.index, m.index + m[0].length];
    }
    if (best) spans.push([best[0], best[1], p.m.id]);
  }
  const keep = spans.filter((s) => !spans.some((o) => o !== s && o[0] <= s[0] && s[1] <= o[1] && o[1] - o[0] > s[1] - s[0]));
  keep.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return keep.map(([a, b, id]) => ({ id, start: a, end: b }));
}

// ---------------------------------------------------------------------------------------------- order references
const REF = String.raw`([a-z0-9][a-z0-9./-]{2,34}[a-z0-9])`;
const REF_PATTERNS = [
  /\b([a-z]?[0-9]{2,3}-[0-9]{7}-[0-9]{7})\b/g,
  /\b(gs\.[0-9]{4}-[0-9]{4}-[0-9]{4})\b/g,
  /\b((?:sop|gpa)\.[0-9.-]{8,40})/g,
  new RegExp(String.raw`(?:id de pedido|order id|order #|pedido #|invoice #|factura #|n[º°o]\.? de factura|referencia del pedido)\s*[:#]?\s*` + REF, "g"),
  new RegExp(String.raw`(?:numero|num\.?|n\.?[º°o]\.?)\s*(?:de\s+)?(?:pedido|orden|order|factura|invoice|recibo|receipt)\s*(?:es|is|:|#)?\s*[:#]?\s*` + REF, "g"),
  new RegExp(String.raw`\b(?:pedido|orden|order)\s*(?:n\.?[º°o]\.?|no\.?|num\.?|number|numero|#|:|es|is)\s*[:#]?\s*` + REF, "g"),
  /\b(?:pedido|order)\s+#?([0-9]{5,})\b/g,
  /(?:bestellung|commande)\s*(?:number|no\.?|nr\.?|n[º°o]|#|nummer|numero)?\s*[:#]?\s*([a-z]{0,3}[0-9][a-z0-9-]{4,24})/g,
  new RegExp(String.raw`(?:factura|invoice|recibo|receipt|referencia|reference|ref\.?)\s*(?:no\.?|n[º°o]\.?|#|:)?\s*[:#]?\s*` + REF, "g"),
];
const DATE_LIKE = [/^[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4}$/, /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/];

function validRef(ref) {
  return (ref.match(/[0-9]/g) || []).length >= 3 && !DATE_LIKE.some((rx) => rx.test(ref)) && !upsValid(ref);
}

/** Comparison form of an order reference: upper case, letters and digits only; "" when under four characters are left. */
export function orderKey(ref) {
  const key = fold(ref, { keepLength: false }).replace(NON_ALNUM, "").toUpperCase();
  return key.length >= 4 ? key : "";
}

/** Order, invoice and receipt references in a text, upper case, best evidence first (see the Python docstring). */
export function findOrderRefs(text, { limit = 8 } = {}) {
  const folded = fold(text);
  const out = [];
  const seen = new Set();
  for (const rx of REF_PATTERNS) {
    for (const m of folded.matchAll(rx)) {
      let ref = m[1].replace(/[-/.]+$/, "");
      if (ref.length < 4 || !validRef(ref)) continue;
      ref = ref.toUpperCase().slice(0, 40);
      const k = ref.toLowerCase().replace(NON_ALNUM, "");
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(ref);
      if (out.length >= limit) return out;
    }
  }
  return out;
}

/** The first of findOrderRefs, or "". */
export function findOrderRef(text) {
  const refs = findOrderRefs(text, { limit: 1 });
  return refs.length ? refs[0] : "";
}
