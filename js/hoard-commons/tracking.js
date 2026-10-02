// tracking.js — the Node twin of hoard_link/tracking.py (ESM, no dependencies).
//
//   import { find, classify, trackingUrl, carrierName, upsValid, s10Valid, cleanUrl } from "./hoard-commons/tracking.js";
//
// Same formats, regexes and confidences as the Python module (ported from Phileas's numbers.py); both are checked
// against tests/vectors/tracking.json. cleanUrl defers to ./web.js (the twin of hoard_link/web/urls.py) when that file
// is present and keeps a small local copy otherwise.

export const CARRIERS = {
  ups: { name: "UPS", url: "https://www.ups.com/track?loc=es_ES&tracknum={n}" },
  dhl: { name: "DHL", url: "https://www.dhl.com/es-es/home/tracking/tracking-parcel.html?submit=1&tracking-id={n}" },
  correos: { name: "Correos", url: "https://www.correos.es/es/es/herramientas/localizador/envios/detalle?tracking-number={n}" },
  correos_express: { name: "Correos Express", url: "https://s.correosexpress.com/SeguimientoSinCP/search?n={n}" },
  seur: { name: "SEUR", url: "https://www.seur.com/livetracking/?segOnlineIdentificador={n}&segOnlineIdioma=es" },
  gls: { name: "GLS", url: "https://gls-group.com/ES/es/seguimiento-envio/?match={n}" },
  mrw: { name: "MRW", url: "https://www.mrw.es/seguimiento_envios/MRW_resultados_consultas.asp?modo=nacional&envio={n}" },
  nacex: { name: "NACEX", url: "https://www.nacex.es/seguimientoDetalle.do?agencia_origen=&numero_albaran={n}" },
  ctt: { name: "CTT Express", url: "https://www.cttexpress.com/localizador-de-envios/?sc={n}" },
  inpost: { name: "InPost", url: "https://inpost.es/seguimiento-envio/?number={n}" },
  fedex: { name: "FedEx", url: "https://www.fedex.com/fedextrack/?trknbr={n}" },
  tnt: { name: "TNT", url: "https://www.tnt.com/express/es_es/site/herramientas-envio/seguimiento.html?searchType=con&cons={n}" },
  yunexpress: { name: "YunExpress", url: "https://www.yuntrack.com/Track/Detail/{n}" },
  cainiao: { name: "Cainiao", url: "https://global.cainiao.com/newDetail.htm?mailNoList={n}" },
  postnl: { name: "PostNL", url: "https://jouw.postnl.nl/track-and-trace/{n}" },
  royalmail: { name: "Royal Mail", url: "https://www.royalmail.com/track-your-item#/tracking-results/{n}" },
  deutschepost: { name: "Deutsche Post", url: "https://www.deutschepost.de/de/s/sendungsverfolgung.html?piececode={n}" },
  laposte: { name: "La Poste", url: "https://www.laposte.fr/outils/suivre-vos-envois?code={n}" },
  chinapost: { name: "China Post", url: "https://t.17track.net/es#nums={n}" },
  amazon: { name: "Amazon", url: "" },
  paack: { name: "Paack", url: "https://paack.co/es/tracking?tracking={n}" },
  zeleris: { name: "Zeleris", url: "https://www.zeleris.com/seguimiento_envio.aspx?id_seguimiento={n}" },
  ecoscooting: { name: "Ecoscooting", url: "https://www.ecoscooting.com/tracking/{n}" },
  dpd: { name: "DPD", url: "https://www.dpd.com/es/es/seguimiento/?parcelNumber={n}" },
  other: { name: "", url: "https://t.17track.net/es#nums={n}" },
};

export const CARRIER_WORDS = [
  ["correos_express", String.raw`correos\s*express`], ["correos", String.raw`\bcorreos\b`], ["ups", String.raw`\bUPS\b`], ["dhl", String.raw`\bDHL\b`],
  ["seur", String.raw`\bSEUR\b`], ["gls", String.raw`\bGLS\b`], ["mrw", String.raw`\bMRW\b`], ["nacex", String.raw`\bNACEX\b`],
  ["ctt", String.raw`\bCTT(?:\s*Express)?\b`], ["inpost", String.raw`\bInPost\b|\bMondial\s+Relay\b`], ["fedex", String.raw`\bFed\s?Ex\b`],
  ["tnt", String.raw`\bTNT\b`], ["yunexpress", String.raw`\bYun\s?Express\b`], ["cainiao", String.raw`\bCainiao\b`], ["postnl", String.raw`\bPostNL\b`],
  ["royalmail", String.raw`\bRoyal\s+Mail\b`], ["deutschepost", String.raw`\bDeutsche\s+Post\b`], ["paack", String.raw`\bPaack\b`],
  ["zeleris", String.raw`\bZeleris\b`], ["amazon", String.raw`\bAmazon\s+Logistics\b`], ["ecoscooting", String.raw`\bEcoscooting\b`],
  ["dpd", String.raw`\bDPD\b`],
];
const CASE_SENSITIVE = new Set(["ups", "dhl", "gls", "mrw", "tnt", "dpd"]);
const CARRIER_WORD_RE = CARRIER_WORDS.map(([cid, rx]) => [cid, rx, CASE_SENSITIVE.has(cid) ? "g" : "gi"]);

const S10_COUNTRY = { ES: "correos", CN: "chinapost", GB: "royalmail", DE: "deutschepost", NL: "postnl", FR: "laposte" };

const LABEL_SRC = String.raw`(?:n[uú]mero\s+de\s+(?:seguimiento|env[ií]o|tracking)|c[oó]digo\s+de\s+(?:seguimiento|env[ií]o|recogida)|` +
  String.raw`tracking\s*(?:number|no\.?|n[º°o]|id|code)?|seguimiento|sendungsnummer|num[eé]ro\s+de\s+suivi|` +
  String.raw`localizador|n\.?\s*[ºo°]\s*de\s+env[ií]o|awb)\s*(?:es|is|:|#|\(.*?\))?\s*[:#]?\s*([A-Z0-9][A-Z0-9\- ]{6,34}[A-Z0-9])`;
const TOKEN_STOP = /\s{2,}|\s(?=[a-záéíóúñ]{2,})/;

export function normalize(number) {
  return (number === null || number === undefined ? "" : String(number)).replace(/[\s-]/g, "").toUpperCase();
}

export function upsValid(n) {
  n = normalize(n);
  if (!/^1Z[0-9A-Z]{16}$/.test(n)) return false;
  let total = 0;
  [...n.slice(2, 17)].forEach((ch, i) => {
    const value = /[0-9]/.test(ch) ? Number(ch) : (ch.charCodeAt(0) - 63) % 10;
    total += i % 2 ? value * 2 : value;
  });
  return /[0-9]/.test(n[17]) ? (10 - (total % 10)) % 10 === Number(n[17]) : false;
}

export function s10Valid(n) {
  n = normalize(n);
  if (!/^[A-Z]{2}[0-9]{9}[A-Z]{2}$/.test(n)) return false;
  const weights = [8, 6, 4, 2, 3, 5, 9, 7];
  const total = weights.reduce((acc, w, i) => acc + Number(n[2 + i]) * w, 0);
  let check = 11 - (total % 11);
  check = check === 10 ? 0 : check === 11 ? 5 : check;
  return check === Number(n[10]);
}

export function classify(number) {
  const n = normalize(number);
  if (upsValid(n)) return ["ups", 98];
  if (/^1Z[0-9A-Z]{16}$/.test(n)) return ["ups", 70];
  if (s10Valid(n)) return [S10_COUNTRY[n.slice(-2)] || "other", 92];
  if (/^(?:JJD[0-9]{15,24}|JVGL[0-9]{8,20}|GM[0-9]{16,22}|00340[0-9]{15}|3S[A-Z]{4}[0-9]{6,})$/.test(n)) return n.startsWith("3S") ? ["postnl", 85] : ["dhl", 88];
  if (/^YT[0-9]{16}$/.test(n)) return ["yunexpress", 92];
  if (/^(?:(?:LP|CN|CAINIAO)[0-9]{12,20}[A-Z]{0,2}|LP[0-9]{14})$/.test(n)) return ["cainiao", 75];
  if (/^TBA[0-9]{9,14}$/.test(n)) return ["amazon", 85];
  if (/^P[A-Z0-9]{2}[A-Z0-9]{14,20}[A-Z]?$/.test(n) && /[0-9]{5}/.test(n) && n.length >= 16) return ["correos", 72];
  return ["", 0];
}

export function plausible(number) {
  const n = normalize(number);
  if (!(n.length >= 8 && n.length <= 35)) return false;
  if (!/[0-9]{4}/.test(n)) return false;
  if (/^[0-9]{9}$/.test(n) && "6789".includes(n[0])) return false;
  if (/^(?:34)?[6789][0-9]{8}$/.test(n)) return false;
  return true;
}

// ---------------------------------------------------------------- links
const URL_PARAMS = ["tracknum", "trackingnumber", "tracking-number", "tracking_number", "tracking-id", "trackingid", "tracking", "trknbr",
  "awb", "piececode", "match", "mailnolist", "nums", "number", "numero", "n", "sc", "envio", "segonlineidentificador",
  "numero_albaran", "id_seguimiento", "code", "cons", "shipmentnumber", "parcelnumber", "barcode", "codigo"];
const URL_HOSTS = [
  ["ups", "ups.com"], ["dhl", "dhl."], ["correos_express", "correosexpress"], ["correos", "correos.es"], ["seur", "seur.com"],
  ["gls", "gls-"], ["mrw", "mrw.es"], ["nacex", "nacex"], ["ctt", "cttexpress"], ["inpost", "inpost"], ["fedex", "fedex.com"],
  ["tnt", "tnt.com"], ["yunexpress", "yuntrack"], ["yunexpress", "yunexpress"], ["cainiao", "cainiao"], ["postnl", "postnl"],
  ["royalmail", "royalmail"], ["deutschepost", "deutschepost"], ["laposte", "laposte"], ["paack", "paack"], ["zeleris", "zeleris"],
  ["other", "17track"], ["other", "parcelsapp"], ["other", "aftership"],
];
const UNWRAP_KEYS = ["U", "u", "url", "redirect", "target", "dest", "q", "link"];

const unquote = (s) => { try { return decodeURIComponent(s); } catch { return s; } };

function splitUrl(url) {
  try {
    const u = new URL(url);
    const exact = new Map();
    for (const [k, v] of u.searchParams) {
      if (v === "") continue;                      // parse_qs drops blank values
      if (!exact.has(k)) exact.set(k, []);
      exact.get(k).push(v);
    }
    return { host: u.hostname.toLowerCase(), path: u.pathname, query: u.search.replace(/^\?/, ""), fragment: u.hash.replace(/^#/, ""), exact };
  } catch {
    return null;
  }
}

export function unwrap(url, depth = 3) {
  url = url === null || url === undefined ? "" : String(url);
  for (let i = 0; i < depth; i++) {
    let inner = "";
    const m = /\/L0\/(https?(?::|%3A).*?)\/[0-9]+\/[0-9A-Za-z-]{10,}/i.exec(url) || /\/L0\/(https?(?::|%3A).*)$/i.exec(url);
    if (m) inner = unquote(m[1]);
    else {
      const parts = splitUrl(url);
      if (parts) {
        for (const key of UNWRAP_KEYS) {
          for (const value of parts.exact.get(key) || []) {
            const v = unquote(value);
            if (/^https?:\/\//i.test(v)) { inner = v; break; }
          }
          if (inner) break;
        }
      }
    }
    if (!inner || inner === url) return url;
    url = inner;
  }
  return url;
}

export function fromUrl(url) {
  url = unwrap(url);
  const parts = splitUrl(url);
  if (!parts) return null;
  const host = parts.host;
  const hostHit = URL_HOSTS.find(([, needle]) => host.includes(needle));
  const carrier = hostHit ? hostHit[0] : "";
  if (!carrier) return null;
  const qs = new Map();
  for (const [k, v] of parts.exact) qs.set(k.toLowerCase(), v);
  const candidates = [];
  for (const key of URL_PARAMS) for (const value of qs.get(key) || []) candidates.push(...value.split(/[,;\s]+/));
  let m = /\/(?:track(?:ing)?|detail|seguimiento|trace|track-and-trace)\/(?:[a-z-]+\/)?([A-Za-z0-9]{8,35})(?:[/?#]|$)/i.exec(parts.path);
  if (m) candidates.push(m[1]);
  m = /nums=([A-Za-z0-9,]+)/.exec(parts.fragment) || /tracking-results\/([A-Za-z0-9]+)/.exec(parts.fragment);
  if (m) candidates.push(...m[1].split(","));
  for (const cand of candidates) {
    const n = normalize(cand);
    if (plausible(n)) {
      const [fmtCarrier] = classify(n);
      const fin = carrier === "other" && fmtCarrier ? fmtCarrier : carrier;
      return { number: n, carrier: fin !== "other" ? fin : (fmtCarrier || ""), confidence: 95, evidence: "url", url, notes: [] };
    }
  }
  return null;
}

// ---------------------------------------------------------------- text
export function carriersMentioned(text) {
  const s = text === null || text === undefined ? "" : String(text);
  const seen = [];
  for (const [cid, rx, flags] of CARRIER_WORD_RE) {
    if (new RegExp(rx, flags.replace("g", "")).test(s) && !seen.includes(cid)) {
      if (cid === "correos" && seen.includes("correos_express")) continue;
      seen.push(cid);
    }
  }
  return seen;
}

function nearestCarrier(text, pos, window = 160) {
  const lo = Math.max(0, pos - window), hi = Math.min(text.length, pos + window);
  const chunk = text.slice(lo, hi);
  let best = "", bestDist = 1e9;
  for (const [cid, rx, flags] of CARRIER_WORD_RE) {
    for (const m of chunk.matchAll(new RegExp(rx, flags))) {
      const dist = Math.abs(lo + m.index - pos);
      if (dist < bestDist) { best = cid; bestDist = dist; }
    }
  }
  return best;
}

const FORMAT_SRC = String.raw`\b(1Z[0-9A-Z]{16}|[A-Z]{2}[0-9]{9}[A-Z]{2}|JJD[0-9]{15,24}|JVGL[0-9]{8,20}|YT[0-9]{16}|TBA[0-9]{9,14}|00340[0-9]{15}|` +
  String.raw`P[A-Z0-9]{2}[A-Z0-9]{14,20}[A-Z]?|LP[0-9]{14})\b`;

export function find(text, links = null) {
  const found = new Map();
  const keep = (item) => {
    const prev = found.get(item.number);
    if (!prev || item.confidence > prev.confidence) {
      if (prev && !item.carrier) item.carrier = prev.carrier;
      found.set(item.number, item);
    } else if (prev && !prev.carrier && item.carrier) prev.carrier = item.carrier;
  };
  for (const link of links || []) {
    const item = fromUrl(String((link && link.url) || ""));
    if (item) keep(item);
  }
  text = text === null || text === undefined ? "" : String(text);
  for (const url of text.match(/https?:\/\/[^\s<>"')\]]+/g) || []) {
    const item = fromUrl(url);
    if (item) keep(item);
  }
  for (const m of text.matchAll(new RegExp(FORMAT_SRC, "g"))) {
    const n = normalize(m[1]);
    let [carrier, conf] = classify(n);
    if (!carrier || conf < 70) continue;
    if (carrier === "correos" && conf < 90 && !/correos|recogida|seguimiento|env[ií]o/i.test(text)) continue;
    const near = nearestCarrier(text, m.index);
    if (near && (carrier === "other" || carrier === "")) carrier = near;
    keep({ number: n, carrier, confidence: conf, evidence: "format", url: "", notes: [] });
  }
  for (const m of text.matchAll(new RegExp(LABEL_SRC, "gi"))) {
    let raw = m[1].split(TOKEN_STOP)[0].trim();
    const first = raw.split(" ")[0];
    if (raw.includes(" ") && (classify(first)[0] || (plausible(first) && normalize(first).length >= 10))) raw = first;
    const n = normalize(raw);
    if (!plausible(n) || /^[A-Z]+$/.test(n)) continue;
    if (/^[0-9]{3}-?[0-9]{7}-?[0-9]{7}$/.test(raw.replace(/ /g, ""))) continue;
    const [carrier, conf] = classify(n);
    const near = nearestCarrier(text, m.index);
    keep({ number: n, carrier: carrier || near, confidence: Math.max(conf, near ? 80 : 65), evidence: "label", url: "", notes: [] });
  }
  return [...found.values()].sort((a, b) => b.confidence - a.confidence);
}

export function trackingUrl(carrier, number) {
  const template = (CARRIERS[String(carrier || "")] || {}).url || "";
  return template && number ? template.replace("{n}", normalize(number)) : "";
}

export function carrierName(carrier) {
  const cid = String(carrier || "");
  return (CARRIERS[cid] || {}).name || cid.toUpperCase();
}

// ---------------------------------------------------------------- mail links
// local copy of hoard_link.web.urls' lists, used only when ./web.js is not there
const TRACK_PREFIXES = ["utm_", "mc_", "_hs", "vero_", "trk"];
const TRACK_EXACT = new Set(["fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url", "ref_", "yclid", "twclid",
  "wbraid", "gbraid", "ttclid", "li_fat_id", "srsltid", "_gl", "_ga", "_hsenc", "_hsmi", "vero_id", "vero_conv", "s_cid", "spm",
  "snr", "ser", "eid", "c2id", "mkt_tok", "trackingid", "refid", "e", "cid", "goal"]);
const LOCAL_REDIRECT_KEYS = ["url", "u", "redirect", "redirect_url", "redirecturl", "link", "target", "dest", "destination"];

let webCleanUrl = null;
try {
  ({ cleanUrl: webCleanUrl } = await import("./web.js"));
} catch {
  webCleanUrl = null;
}

const plus = (s) => unquote(s.replace(/\+/g, " "));

function unwrapAwstrack(url) {
  const m = /\/L0\/(https?(?::|%3A).*?)\/[0-9]+\/[0-9A-Za-z-]{10,}/i.exec(url) || /\/L0\/(https?(?::|%3A).*)$/i.exec(url);
  return m ? unquote(m[1]) : url;
}

export function localCleanUrl(url) {
  const split = (u) => {
    const m = /^([A-Za-z][A-Za-z0-9+.\-]*):\/\/([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/s.exec(u);
    return m ? { scheme: m[1].toLowerCase(), netloc: m[2], path: m[3], query: m[4] || "" } : null;
  };
  for (let i = 0; i < 3; i++) {
    const parts = split(url);
    if (!parts) break;
    let inner = "";
    for (const token of parts.query.split("&")) {
      const eq = token.indexOf("=");
      const key = eq < 0 ? token : token.slice(0, eq), value = eq < 0 ? "" : token.slice(eq + 1);
      if (LOCAL_REDIRECT_KEYS.includes(plus(key).toLowerCase()) && /^https?:\/\//i.test(unquote(value).trim())) { inner = unquote(value).trim(); break; }
    }
    if (!inner) break;
    url = inner;
  }
  const parts = split(url);
  if (!parts) return url;
  const keep = [];
  for (const token of parts.query.split("&")) {
    if (!token) continue;
    const name = plus(token.split("=")[0]).toLowerCase();
    if (TRACK_EXACT.has(name) || TRACK_PREFIXES.some((p) => name.startsWith(p))) continue;
    keep.push(token);
  }
  return `${parts.scheme}://${parts.netloc}${parts.path}` + (keep.length ? "?" + keep.join("&") : "");
}

export function cleanUrl(url) {
  const s = unwrapAwstrack(url === null || url === undefined ? "" : String(url).trim());
  return webCleanUrl ? webCleanUrl(s, { mail: true }) : localCleanUrl(s);
}
