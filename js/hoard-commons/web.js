// web.js — the Node twin of hoard_link/web (ESM, Node 18+, no npm dependencies).
//
//   import { normalizeUrl, checkUrl, htmlToText, pageMeta, parseFeed, checkPage, webGet, politeJson }
//     from "./hoard-commons/web.js";
//
// Same rules and the same tables as the Python package; both are checked against tests/vectors/web_*.json.
// Functions take options as one trailing object and accept snake_case or camelCase keys; results are camelCase.
//
//   URLs      normalizeUrl urlKey unwrapRedirect cleanUrl hostOf registrableDomain splitUrl urljoin
//   Safety    PUBLIC OPERATOR_LOCAL INTERNAL classifyIp parseIp checkUrl resolvePublic PolicyError
//   Blocks    detectBlock blockHint
//   HTML      parseHtml htmlToText quality normaliseForHash contentHash excerpt unescapeHtml
//   Metadata  pageMeta jsonldBlocks jsonldNodes discoverFeeds faviconCandidates
//   Feeds     parseFeed githubFeed toIsoUtc
//   Watch     checkPage checkFeed diffLines
//   Robots    RobotsRules robotsRulesAllowed robotsAllowed
//   Network   webGet politeJson decodeBody

import crypto from "node:crypto";
import dns from "node:dns";
import http from "node:http";
import https from "node:https";
import zlib from "node:zlib";
import { domainToASCII } from "node:url";
import { clampText } from "./text.js";

// ------------------------------------------------------------------------------------------------ helpers

/** The characters Python's str.isspace() accepts (JS \s differs: it has U+FEFF, lacks \x1c-\x1f and \x85). */
const WS = "\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const WS_RUN = new RegExp(`[${WS}]+`, "g");
const WS_EDGE = new RegExp(`^[${WS}]+|[${WS}]+$`, "g");
const LINE_BREAK = /\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/;

const pyStrip = (s) => String(s).replace(WS_EDGE, "");
const pySplit = (s) => { const t = pyStrip(s); return t ? t.split(WS_RUN) : []; };
const splitLines = (s) => { const t = String(s ?? ""); if (!t) return []; const parts = t.split(LINE_BREAK); if (parts[parts.length - 1] === "") parts.pop(); return parts; };
const str = (v) => (v === null || v === undefined ? "" : String(v));
const cp = (s) => Array.from(s);
const clipText = (text, n) => { const c = cp(text); return c.length <= n ? text : c.slice(0, n - 1).join("").replace(new RegExp(`[${WS}]+$`), "") + "…"; };
const cpSlice = (s, n) => { const c = cp(s); return c.length <= n ? s : c.slice(0, n).join(""); };
const sha256 = (text) => crypto.createHash("sha256").update(Buffer.from(String(text), "utf8")).digest("hex");
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\\/-]/g, "\\$&");

/** Read an option written as camelCase or snake_case. */
function opt(o, camel, dflt) {
  if (!o || typeof o !== "object") return dflt;
  if (o[camel] !== undefined) return o[camel];
  const snake = camel.replace(/[A-Z]/g, (c) => "_" + c.toLowerCase());
  return o[snake] !== undefined ? o[snake] : dflt;
}
const pick = (o, camel, dflt = undefined) => opt(o, camel, dflt);
const camelKey = (k) => k.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase());
const snakeKey = (k) => k.replace(/[A-Z]/g, (c) => "_" + c.toLowerCase());
/** Deep key conversion (for tests and callers that hold snake_case data). */
export function camelKeys(v) { return mapKeys(v, camelKey); }
export function snakeKeys(v) { return mapKeys(v, snakeKey); }
function mapKeys(v, f) {
  if (Array.isArray(v)) return v.map((x) => mapKeys(x, f));
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [f(k), mapKeys(x, f)]));
  return v;
}

// ------------------------------------------------------------------------------------------------ entities
// HTML 4 names, ``&apos;`` and the legacy names that need no ``;`` (what html.unescape accepts), plus the
// numeric rules of html.unescape.
const ENTITIES = {
  "AElig": "\u00c6",
  "Aacute": "\u00c1",
  "Acirc": "\u00c2",
  "Agrave": "\u00c0",
  "Alpha": "\u0391",
  "Aring": "\u00c5",
  "Atilde": "\u00c3",
  "Auml": "\u00c4",
  "Beta": "\u0392",
  "Ccedil": "\u00c7",
  "Chi": "\u03a7",
  "Dagger": "\u2021",
  "Delta": "\u0394",
  "ETH": "\u00d0",
  "Eacute": "\u00c9",
  "Ecirc": "\u00ca",
  "Egrave": "\u00c8",
  "Epsilon": "\u0395",
  "Eta": "\u0397",
  "Euml": "\u00cb",
  "Gamma": "\u0393",
  "Iacute": "\u00cd",
  "Icirc": "\u00ce",
  "Igrave": "\u00cc",
  "Iota": "\u0399",
  "Iuml": "\u00cf",
  "Kappa": "\u039a",
  "Lambda": "\u039b",
  "Mu": "\u039c",
  "Ntilde": "\u00d1",
  "Nu": "\u039d",
  "OElig": "\u0152",
  "Oacute": "\u00d3",
  "Ocirc": "\u00d4",
  "Ograve": "\u00d2",
  "Omega": "\u03a9",
  "Omicron": "\u039f",
  "Oslash": "\u00d8",
  "Otilde": "\u00d5",
  "Ouml": "\u00d6",
  "Phi": "\u03a6",
  "Pi": "\u03a0",
  "Prime": "\u2033",
  "Psi": "\u03a8",
  "Rho": "\u03a1",
  "Scaron": "\u0160",
  "Sigma": "\u03a3",
  "THORN": "\u00de",
  "Tau": "\u03a4",
  "Theta": "\u0398",
  "Uacute": "\u00da",
  "Ucirc": "\u00db",
  "Ugrave": "\u00d9",
  "Upsilon": "\u03a5",
  "Uuml": "\u00dc",
  "Xi": "\u039e",
  "Yacute": "\u00dd",
  "Yuml": "\u0178",
  "Zeta": "\u0396",
  "aacute": "\u00e1",
  "acirc": "\u00e2",
  "acute": "\u00b4",
  "aelig": "\u00e6",
  "agrave": "\u00e0",
  "alefsym": "\u2135",
  "alpha": "\u03b1",
  "amp": "&",
  "and": "\u2227",
  "ang": "\u2220",
  "apos": "'",
  "aring": "\u00e5",
  "asymp": "\u2248",
  "atilde": "\u00e3",
  "auml": "\u00e4",
  "bdquo": "\u201e",
  "beta": "\u03b2",
  "brvbar": "\u00a6",
  "bull": "\u2022",
  "cap": "\u2229",
  "ccedil": "\u00e7",
  "cedil": "\u00b8",
  "cent": "\u00a2",
  "chi": "\u03c7",
  "circ": "\u02c6",
  "clubs": "\u2663",
  "cong": "\u2245",
  "copy": "\u00a9",
  "crarr": "\u21b5",
  "cup": "\u222a",
  "curren": "\u00a4",
  "dArr": "\u21d3",
  "dagger": "\u2020",
  "darr": "\u2193",
  "deg": "\u00b0",
  "delta": "\u03b4",
  "diams": "\u2666",
  "divide": "\u00f7",
  "eacute": "\u00e9",
  "ecirc": "\u00ea",
  "egrave": "\u00e8",
  "empty": "\u2205",
  "emsp": "\u2003",
  "ensp": "\u2002",
  "epsilon": "\u03b5",
  "equiv": "\u2261",
  "eta": "\u03b7",
  "eth": "\u00f0",
  "euml": "\u00eb",
  "euro": "\u20ac",
  "exist": "\u2203",
  "fnof": "\u0192",
  "forall": "\u2200",
  "frac12": "\u00bd",
  "frac14": "\u00bc",
  "frac34": "\u00be",
  "frasl": "\u2044",
  "gamma": "\u03b3",
  "ge": "\u2265",
  "gt": ">",
  "hArr": "\u21d4",
  "harr": "\u2194",
  "hearts": "\u2665",
  "hellip": "\u2026",
  "iacute": "\u00ed",
  "icirc": "\u00ee",
  "iexcl": "\u00a1",
  "igrave": "\u00ec",
  "image": "\u2111",
  "infin": "\u221e",
  "int": "\u222b",
  "iota": "\u03b9",
  "iquest": "\u00bf",
  "isin": "\u2208",
  "iuml": "\u00ef",
  "kappa": "\u03ba",
  "lArr": "\u21d0",
  "lambda": "\u03bb",
  "lang": "\u27e8",
  "laquo": "\u00ab",
  "larr": "\u2190",
  "lceil": "\u2308",
  "ldquo": "\u201c",
  "le": "\u2264",
  "lfloor": "\u230a",
  "lowast": "\u2217",
  "loz": "\u25ca",
  "lrm": "\u200e",
  "lsaquo": "\u2039",
  "lsquo": "\u2018",
  "lt": "<",
  "macr": "\u00af",
  "mdash": "\u2014",
  "micro": "\u00b5",
  "middot": "\u00b7",
  "minus": "\u2212",
  "mu": "\u03bc",
  "nabla": "\u2207",
  "nbsp": "\u00a0",
  "ndash": "\u2013",
  "ne": "\u2260",
  "ni": "\u220b",
  "not": "\u00ac",
  "notin": "\u2209",
  "nsub": "\u2284",
  "ntilde": "\u00f1",
  "nu": "\u03bd",
  "oacute": "\u00f3",
  "ocirc": "\u00f4",
  "oelig": "\u0153",
  "ograve": "\u00f2",
  "oline": "\u203e",
  "omega": "\u03c9",
  "omicron": "\u03bf",
  "oplus": "\u2295",
  "or": "\u2228",
  "ordf": "\u00aa",
  "ordm": "\u00ba",
  "oslash": "\u00f8",
  "otilde": "\u00f5",
  "otimes": "\u2297",
  "ouml": "\u00f6",
  "para": "\u00b6",
  "part": "\u2202",
  "permil": "\u2030",
  "perp": "\u22a5",
  "phi": "\u03c6",
  "pi": "\u03c0",
  "piv": "\u03d6",
  "plusmn": "\u00b1",
  "pound": "\u00a3",
  "prime": "\u2032",
  "prod": "\u220f",
  "prop": "\u221d",
  "psi": "\u03c8",
  "quot": "\u0022",
  "rArr": "\u21d2",
  "radic": "\u221a",
  "rang": "\u27e9",
  "raquo": "\u00bb",
  "rarr": "\u2192",
  "rceil": "\u2309",
  "rdquo": "\u201d",
  "real": "\u211c",
  "reg": "\u00ae",
  "rfloor": "\u230b",
  "rho": "\u03c1",
  "rlm": "\u200f",
  "rsaquo": "\u203a",
  "rsquo": "\u2019",
  "sbquo": "\u201a",
  "scaron": "\u0161",
  "sdot": "\u22c5",
  "sect": "\u00a7",
  "shy": "\u00ad",
  "sigma": "\u03c3",
  "sigmaf": "\u03c2",
  "sim": "\u223c",
  "spades": "\u2660",
  "sub": "\u2282",
  "sube": "\u2286",
  "sum": "\u2211",
  "sup": "\u2283",
  "sup1": "\u00b9",
  "sup2": "\u00b2",
  "sup3": "\u00b3",
  "supe": "\u2287",
  "szlig": "\u00df",
  "tau": "\u03c4",
  "there4": "\u2234",
  "theta": "\u03b8",
  "thetasym": "\u03d1",
  "thinsp": "\u2009",
  "thorn": "\u00fe",
  "tilde": "\u02dc",
  "times": "\u00d7",
  "trade": "\u2122",
  "uArr": "\u21d1",
  "uacute": "\u00fa",
  "uarr": "\u2191",
  "ucirc": "\u00fb",
  "ugrave": "\u00f9",
  "uml": "\u00a8",
  "upsih": "\u03d2",
  "upsilon": "\u03c5",
  "uuml": "\u00fc",
  "weierp": "\u2118",
  "xi": "\u03be",
  "yacute": "\u00fd",
  "yen": "\u00a5",
  "yuml": "\u00ff",
  "zeta": "\u03b6",
  "zwj": "\u200d",
  "zwnj": "\u200c",
  "AMP": "&",
  "COPY": "\u00a9",
  "GT": ">",
  "LT": "<",
  "QUOT": "\u0022",
  "REG": "\u00ae"
};
const ENTITY_LEGACY = new Set(["AElig", "AMP", "Aacute", "Acirc", "Agrave", "Aring", "Atilde", "Auml", "COPY", "Ccedil", "ETH", "Eacute", "Ecirc", "Egrave", "Euml", "GT", "Iacute", "Icirc", "Igrave", "Iuml", "LT", "Ntilde", "Oacute", "Ocirc", "Ograve", "Oslash", "Otilde", "Ouml", "QUOT", "REG", "THORN", "Uacute", "Ucirc", "Ugrave", "Uuml", "Yacute", "aacute", "acirc", "acute", "aelig", "agrave", "amp", "aring", "atilde", "auml", "brvbar", "ccedil", "cedil", "cent", "copy", "curren", "deg", "divide", "eacute", "ecirc", "egrave", "eth", "euml", "frac12", "frac14", "frac34", "gt", "iacute", "icirc", "iexcl", "igrave", "iquest", "iuml", "laquo", "lt", "macr", "micro", "middot", "nbsp", "not", "ntilde", "oacute", "ocirc", "ograve", "ordf", "ordm", "oslash", "otilde", "ouml", "para", "plusmn", "pound", "quot", "raquo", "reg", "sect", "shy", "sup1", "sup2", "sup3", "szlig", "thorn", "times", "uacute", "ucirc", "ugrave", "uml", "uuml", "yacute", "yen", "yuml"]);
const INVALID_CHARREFS = {13: 0xd, 128: 0x20ac, 129: 0x81, 130: 0x201a, 131: 0x192, 132: 0x201e, 133: 0x2026, 134: 0x2020, 135: 0x2021, 136: 0x2c6, 137: 0x2030, 138: 0x160, 139: 0x2039, 140: 0x152, 141: 0x8d, 142: 0x17d, 143: 0x8f, 144: 0x90, 145: 0x2018, 146: 0x2019, 147: 0x201c, 148: 0x201d, 149: 0x2022, 150: 0x2013, 151: 0x2014, 152: 0x2dc, 153: 0x2122, 154: 0x161, 155: 0x203a, 156: 0x153, 157: 0x9d, 158: 0x17e, 159: 0x178};
const PUBLIC_SUFFIX_LIST = "ac.id ac.il ac.in ac.jp ac.kr ac.nz ac.th ac.uk ac.za asn.au asso.fr co.ae co.id co.il co.in co.jp co.ke co.kr co.nz co.th co.tz co.uk co.za com.ar com.au com.bo com.br com.cn com.co com.ec com.eg com.es com.fr com.gt com.hk com.mx com.my com.ng com.pa com.pe com.ph com.pl com.pt com.py com.ru com.sa com.sg com.tr com.ua com.uy com.ve com.vn edu.ar edu.au edu.br edu.cn edu.co edu.es edu.hk edu.it edu.mx edu.my edu.pe edu.ph edu.pl edu.pt edu.sg edu.tr go.id go.jp go.kr go.th gob.ar gob.es gob.mx gob.pe gouv.fr gov.ar gov.au gov.br gov.cn gov.co gov.hk gov.il gov.in gov.it gov.my gov.ph gov.pl gov.pt gov.sg gov.tr gov.uk gov.vn gov.za govt.nz id.au ltd.uk me.uk ne.jp net.ar net.au net.br net.cn net.co net.in net.mx net.nz net.pl net.uk net.za nhs.uk nom.es or.id or.jp or.kr org.ar org.au org.br org.cn org.co org.es org.hk org.il org.in org.mx org.nz org.pe org.ph org.pl org.pt org.sg org.tr org.ua org.uk org.za plc.uk police.uk sch.uk school.nz";
const NAV_WORD_LIST = "account afiliados aviso ayuda blog buscar busqueda b\u00fasqueda careers carrito cart categorias categor\u00edas cesta condiciones contactanos contacto cont\u00e1ctanos cookies cuenta devoluciones empresa envios env\u00edos facebook favoritos help home idioma inicio instagram legal login mapa menu men\u00fa newsletter nosotros novedades ofertas outlet pais pa\u00eds pedidos pinterest politica pol\u00edtica prensa privacidad privacy registrarse registro returns search shipping sign siguenos sitio sobre stores suscribete suscr\u00edbete s\u00edguenos terminos terms tiendas tiktok trabaja twitter t\u00e9rminos whatsapp wishlist youtube";

const CHARREF = /&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)/g;

function numericRef(num) {
  if (num === 0) return "�";
  if (INVALID_CHARREFS[num] !== undefined) return String.fromCodePoint(INVALID_CHARREFS[num]);
  if ((num >= 0xd800 && num <= 0xdfff) || num > 0x10ffff) return "�";
  if ((num >= 1 && num <= 8) || (num >= 0xe && num <= 0x1f) || num === 0x7f || num === 0xb ||
      (num >= 0xfdd0 && num <= 0xfdef) || (num & 0xfffe) === 0xfffe) return "";
  return String.fromCodePoint(num);
}

/** html.unescape: decimal, hex and named references (named: HTML 4 plus the legacy no-semicolon forms). */
export function unescapeHtml(text) {
  const s = str(text);
  if (!s.includes("&")) return s;
  return s.replace(CHARREF, (whole, body) => {
    if (body[0] === "#") {
      const num = body[1] === "x" || body[1] === "X" ? parseInt(body.replace(/;$/, "").slice(2), 16) : parseInt(body.replace(/;$/, "").slice(1), 10);
      return numericRef(num);
    }
    if (body.endsWith(";")) {
      const name = body.slice(0, -1);
      if (Object.prototype.hasOwnProperty.call(ENTITIES, name)) return ENTITIES[name];
    } else if (ENTITY_LEGACY.has(body)) return ENTITIES[body];
    for (let x = body.length - 1; x > 1; x--) {                 // longest legacy prefix: "&ampfoo" -> "&foo"
      const head = body.slice(0, x);
      if (ENTITY_LEGACY.has(head)) return ENTITIES[head] + body.slice(x);
    }
    return whole;
  });
}

// ------------------------------------------------------------------------------------------------ URLs

export const TRACKING_PARAMS = Object.freeze([
  "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url", "ref_",
  "yclid", "twclid", "wbraid", "gbraid", "ttclid", "li_fat_id", "srsltid", "_gl", "_ga",
  "_hsenc", "_hsmi", "vero_id", "vero_conv", "s_cid", "spm", "snr", "ser", "eid", "c2id", "mkt_tok",
  "trackingid", "refid",
]);
export const TRACKING_PREFIXES = Object.freeze(["utm_", "mc_", "_hs", "vero_", "trk"]);
export const MAIL_TRACKING_PARAMS = Object.freeze(["e", "cid", "goal"]);
export const REF_TRACKING_HOSTS = Object.freeze(["producthunt.com", "etsy.com", "medium.com", "indiehackers.com"]);
export const REDIRECT_KEYS = Object.freeze(["url", "u", "redirect", "redirect_url", "redirecturl", "link", "target", "dest",
  "destination", "to", "r", "q"]);
const TRACKING_SET = new Set(TRACKING_PARAMS);
const AMBIGUOUS_REDIRECT_KEYS = new Set(["to", "r", "q"]);
const REDIRECT_PATH = /(?:^|\/)(?:url|redirect|redir|r|l|out|click|clk|away|go|goto|ck|track|tracking|link|exit|ext|jump)(?:\.php|\.aspx?)?(?:\/|$)/;
const DEFAULT_PORTS = { http: "80", https: "443" };
export const PUBLIC_SUFFIXES = Object.freeze(PUBLIC_SUFFIX_LIST.split(" ").sort());
const PUBLIC_SUFFIX_SET = new Set(PUBLIC_SUFFIXES);

const URL_RE = /^([A-Za-z][A-Za-z0-9+.\-]*):\/\/([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#([\s\S]*))?$/;
const BARE_HOST = /^(?:[A-Za-z0-9¡-￿](?:[A-Za-z0-9¡-￿\-]*[A-Za-z0-9¡-￿])?\.)+[A-Za-z¡-￿]{2,}(?::\d+)?(?:[/?#]|$)/;
const DIGITS = /^\d+$/;

/** Percent-decode as UTF-8 (undecodable bytes become U+FFFD), like urllib.parse.unquote. */
export function unquote(s) {
  const text = str(s);
  if (!text.includes("%")) return text;
  return text.replace(/(?:%[0-9a-fA-F]{2})+/g, (run) => {
    const bytes = Uint8Array.from(run.match(/%([0-9a-fA-F]{2})/g).map((h) => parseInt(h.slice(1), 16)));
    return new TextDecoder("utf-8").decode(bytes);
  });
}
const unquotePlus = (s) => unquote(str(s).replace(/\+/g, " "));

/** Split ``scheme://[userinfo@]host[:port]/path?query#fragment``; ``null`` when it is not one. */
export function splitUrl(url) {
  const m = URL_RE.exec(pyStrip(str(url)));
  if (!m) return null;
  let [, scheme, authority, path, query, fragment] = m;
  let userinfo = "";
  const at = authority.lastIndexOf("@");
  if (at >= 0) { userinfo = authority.slice(0, at); authority = authority.slice(at + 1); }
  let host = authority, port = "";
  if (authority.startsWith("[")) {
    const end = authority.indexOf("]");
    if (end < 0) return null;
    host = authority.slice(1, end);
    const rest = authority.slice(end + 1);
    if (rest) {
      if (!rest.startsWith(":") || !(rest.length === 1 || DIGITS.test(rest.slice(1)))) return null;
      port = rest.slice(1);
    }
  } else if (authority.includes(":")) {
    const i = authority.lastIndexOf(":");
    const tail = authority.slice(i + 1);
    host = authority.slice(0, i);
    if (tail === "" || DIGITS.test(tail)) port = tail; else return null;
  }
  return { scheme: scheme.toLowerCase(), userinfo, host, port, path: path || "", query: query || "", fragment: fragment || "", hasQuery: query !== undefined };
}

/** Lowercase, NFKC, no trailing dot, internationalised names in punycode; "" when it cannot be encoded. */
export function cleanHost(host) {
  const h = pyStrip(str(host).normalize("NFKC")).toLowerCase().replace(/\.+$/, "");
  if (!h) return "";
  if (/^[\x00-\x7f]*$/.test(h)) return h;
  return domainToASCII(h) || "";
}

export function isTrackingParam(name, extra = []) {
  const n = unquotePlus(name).toLowerCase();
  return TRACKING_SET.has(n) || TRACKING_PREFIXES.some((p) => n.startsWith(p)) || extra.some((e) => str(e).toLowerCase() === n);
}

const LINKEDIN_VIEW = /\/jobs\/view\/(?:[^/]*-)?(\d+)/;
function linkedinJob(host, parts) {
  let id = (LINKEDIN_VIEW.exec(parts.path) || [])[1];
  if (!id) {
    for (const pair of parts.query.split("&")) {
      const i = pair.indexOf("=");
      const k = i < 0 ? pair : pair.slice(0, i), v = i < 0 ? "" : pair.slice(i + 1);
      if (k === "currentJobId" && DIGITS.test(v)) { id = /^\d+/.exec(v)[0]; break; }
    }
  }
  return id ? `https://www.linkedin.com/jobs/view/${id}` : null;
}
/** host suffix -> rule(host, parts) returning a replacement URL or null. */
export const SITE_RULES = { "linkedin.com": linkedinJob };

function siteRule(host, rules) {
  for (const [suffix, rule] of Object.entries(rules)) if (host === suffix || host.endsWith("." + suffix)) return rule;
  return null;
}
function prepare(url) {
  const s = pyStrip(str(url));
  if (s.startsWith("//")) return "https:" + s;
  if (!s.includes("://") && BARE_HOST.test(s)) return "https://" + s;
  return s;
}
const refStripped = (host, stripRef) => stripRef || REF_TRACKING_HOSTS.some((h) => host === h || host.endsWith("." + h));
const lowerSet = (xs) => new Set([...(xs || [])].map((x) => str(x).toLowerCase()));

function queryTokens(query, host, stripRef, drop, keep) {
  const out = [];
  for (const token of query.split("&")) {
    if (!token) continue;
    const i = token.indexOf("=");
    const name = unquotePlus(i < 0 ? token : token.slice(0, i)).toLowerCase();
    if (keep.has(name)) { out.push(token); continue; }
    if (drop.has(name) || isTrackingParam(name) || (name === "ref" && refStripped(host, stripRef))) continue;
    out.push(token);
  }
  return out;
}
function sortKey(token) { const i = token.indexOf("="); return [unquotePlus(i < 0 ? token : token.slice(0, i)), token]; }

/** Canonical identity of a page; "" when ``url`` is not an http(s) URL. Options: stripWww, dropParams, keepParams,
 *  siteRules (true | false | {suffix: fn}), stripRef, keepFragment, sortParams. */
export function normalizeUrl(url, o = {}) {
  const p = splitUrl(prepare(url));
  if (!p || (p.scheme !== "http" && p.scheme !== "https")) return "";
  let host = cleanHost(p.host);
  if (!host) return "";
  if (p.port && parseInt(p.port, 10) > 65535) return "";
  const stripWww = !!opt(o, "stripWww", false), keepFragment = !!opt(o, "keepFragment", false);
  const sr = opt(o, "siteRules", true);
  const rules = sr === true ? SITE_RULES : (sr || {});
  const rule = siteRule(host, rules);
  const replacement = rule ? rule(host, p) : null;
  if (replacement) return normalizeUrl(replacement, { stripWww, siteRules: false, keepFragment });
  if (stripWww && host.startsWith("www.")) host = host.slice(4);
  let port = p.port ? String(parseInt(p.port, 10)) : "";
  if (port === DEFAULT_PORTS[p.scheme]) port = "";
  const shown = host.includes(":") ? `[${host}]` : host;
  const netloc = shown + (port ? ":" + port : "");
  let path = p.path;
  if (path.length > 1) path = path.replace(/\/+$/, "");
  if (path === "/" || path === "") path = "";
  const tokens = queryTokens(p.query, host, !!opt(o, "stripRef", false), lowerSet(opt(o, "dropParams", [])), lowerSet(opt(o, "keepParams", [])));
  if (opt(o, "sortParams", true)) tokens.sort((a, b) => { const x = sortKey(a), y = sortKey(b); return cmp(x[0], y[0]) || cmp(x[1], y[1]); });
  let out = `${p.scheme}://${netloc}${path}`;
  if (tokens.length) out += "?" + tokens.join("&");
  if (keepFragment && p.fragment) out += "#" + p.fragment;
  return out;
}

/** ``host/path?query`` of the normalised URL, ignoring the scheme and ``www.``. */
export function urlKey(url, o = {}) {
  const norm = normalizeUrl(url, { ...(o || {}), stripWww: true });
  return norm ? norm.split("://").slice(1).join("://") : "";
}

/** Lowercase host of a URL (no port, userinfo or brackets, ``www.`` kept); "" when there is none. */
export function hostOf(url) { const p = splitUrl(prepare(url)); return p ? cleanHost(p.host) : ""; }

function qsPairs(query) {
  const out = [];
  for (const token of query.split("&")) {
    if (!token) continue;
    const i = token.indexOf("=");
    out.push([unquotePlus(i < 0 ? token : token.slice(0, i)), unquotePlus(i < 0 ? "" : token.slice(i + 1))]);
  }
  return out;
}

/** The destination of a tracking redirect that carries it as a parameter (read from the text, never fetched). */
export function unwrapRedirect(url, o = {}) {
  let cur = pyStrip(str(url));
  const hops = Math.max(0, opt(o, "maxHops", 3));
  for (let n = 0; n < hops; n++) {
    const p = splitUrl(cur);
    if (!p || !p.query) break;
    const looks = REDIRECT_PATH.test(p.path.toLowerCase());
    const pairs = qsPairs(p.query);
    let inner = "";
    for (const key of REDIRECT_KEYS) {
      if (AMBIGUOUS_REDIRECT_KEYS.has(key) && !looks) continue;
      for (const [k, v] of pairs) {
        if (k.toLowerCase() === key) {
          const candidate = pyStrip(unquote(v));
          if (/^https?:\/\//i.test(candidate)) { inner = candidate; break; }
        }
      }
      if (inner) break;
    }
    if (!inner) break;
    cur = inner;
  }
  return cur;
}

/** Unwrap a redirect link and drop tracking parameters and the fragment, keeping everything else as written. */
export function cleanUrl(url, o = {}) {
  const cur = unwrapRedirect(url);
  const maxLen = opt(o, "maxLen", 0);
  const cut = (s) => (maxLen ? cpSlice(s, maxLen) : s);
  const p = splitUrl(cur);
  if (!p) return cut(cur);
  const host = cleanHost(p.host);
  const extra = opt(o, "mail", false) ? new Set(MAIL_TRACKING_PARAMS) : new Set();
  const keep = [];
  for (const token of p.query.split("&")) {
    if (!token) continue;
    const i = token.indexOf("=");
    const name = unquotePlus(i < 0 ? token : token.slice(0, i)).toLowerCase();
    if (extra.has(name) || isTrackingParam(name) || (name === "ref" && refStripped(host, false))) continue;
    keep.push(token);
  }
  const authority = (p.userinfo ? p.userinfo + "@" : "") + (p.host.includes(":") ? `[${p.host}]` : p.host) + (p.port ? ":" + p.port : "");
  let out = `${p.scheme}://${authority}${p.path}`;
  if (keep.length) out += "?" + keep.join("&");
  if (opt(o, "keepFragment", false) && p.fragment) out += "#" + p.fragment;
  return cut(out);
}

/** The registrable domain (public suffix plus one label). IP literals and single-label names come back unchanged;
 *  ``null`` for an empty host. ``extraSuffixes`` adds suffixes of your own. */
export function registrableDomain(host, o = {}) {
  const extraList = Array.isArray(o) ? o : opt(o, "extraSuffixes", []);
  const raw = str(host);
  const h = cleanHost(raw.includes("/") ? hostOf(raw) : raw);
  if (!h) return null;
  if (parseIp(h)) return h;
  const labels = h.split(".");
  if (labels.length <= 2) return h;
  const extra = new Set(extraList.map((s) => str(s).toLowerCase().replace(/^\.+|\.+$/g, "")));
  for (const n of [3, 2]) if (labels.length > n && extra.has(labels.slice(-n).join("."))) return labels.slice(-(n + 1)).join(".");
  const two = labels.slice(-2).join(".");
  if (PUBLIC_SUFFIX_SET.has(two) || extra.has(two)) return labels.slice(-3).join(".");
  return two;
}

// ---- RFC 3986 reference resolution, the way urllib.parse.urljoin does it ------------------------

const USES_RELATIVE = new Set(["", "ftp", "http", "gopher", "nntp", "imap", "wais", "file", "https", "shttp", "mms", "prospero", "rtsp", "rtspu", "sftp", "svn", "svn+ssh", "ws", "wss"]);
const USES_NETLOC = new Set(["", "ftp", "http", "gopher", "nntp", "telnet", "imap", "wais", "file", "mms", "https", "shttp", "snews", "prospero", "rtsp", "rtspu", "rsync", "svn", "svn+ssh", "sftp", "nfs", "git", "git+ssh", "ws", "wss"]);
const REF_RE = /^(?:([A-Za-z][A-Za-z0-9+.\-]*):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#([\s\S]*))?$/;

function parseRef(url, dflt = "") {
  const m = REF_RE.exec(url);
  return { scheme: m[1] ? m[1].toLowerCase() : dflt, netloc: m[2] ?? "", path: m[3] ?? "", query: m[4] ?? "", fragment: m[5] ?? "" };
}
function unparse(scheme, netloc, path, query, fragment) {
  let u = path;
  if (netloc || (scheme && USES_NETLOC.has(scheme) && !u.startsWith("//"))) {
    if (u && u[0] !== "/") u = "/" + u;
    u = "//" + (netloc || "") + u;
  }
  if (scheme) u = scheme + ":" + u;
  if (query) u += "?" + query;
  if (fragment) u += "#" + fragment;
  return u;
}

export function urljoin(base, url) {
  base = str(base); url = str(url);
  if (!base) return url;
  if (!url) return base;
  const b = parseRef(base), r = parseRef(url, b.scheme);
  if (r.scheme !== b.scheme || !USES_RELATIVE.has(r.scheme)) return url;
  if (USES_NETLOC.has(r.scheme)) {
    if (r.netloc) return unparse(r.scheme, r.netloc, r.path, r.query, r.fragment);
    r.netloc = b.netloc;
  }
  if (!r.path) return unparse(r.scheme, r.netloc, b.path, r.query || b.query, r.fragment);
  const baseParts = b.path.split("/");
  if (baseParts[baseParts.length - 1] !== "") baseParts.pop();
  const segments = r.path.startsWith("/") ? r.path.split("/") : baseParts.concat(r.path.split("/"));
  const middle = segments.slice(1, -1).filter(Boolean);
  const all = segments.length > 1 ? [segments[0], ...middle, segments[segments.length - 1]] : segments;
  const resolved = [];
  for (const seg of all) {
    if (seg === "..") resolved.pop();
    else if (seg !== ".") resolved.push(seg);
  }
  if (all[all.length - 1] === "." || all[all.length - 1] === "..") resolved.push("");
  return unparse(r.scheme, r.netloc, resolved.join("/") || "/", r.query, r.fragment);
}

// ------------------------------------------------------------------------------------------------ safety
// Profiles, the IP tables and the reason wording are the ones in hoard_link/web/safety.py.

export const PUBLIC = "public";
export const OPERATOR_LOCAL = "operator_local";
export const INTERNAL = "internal";
export const PROFILES = Object.freeze([PUBLIC, OPERATOR_LOCAL, INTERNAL]);
export const UNRESOLVABLE_PREFIX = "unresolvable host";
const MAX_URL_LEN = 2048;

export const METADATA_HOSTS = Object.freeze(["metadata", "metadata.google.internal", "metadata.goog", "instance-data",
  "instance-data.ec2.internal", "metadata.azure.com"]);
export const LOCAL_SUFFIXES = Object.freeze([".localhost", ".local", ".localdomain", ".internal", ".lan", ".home.arpa",
  ".intranet", ".corp", ".home", ".private"]);
export const NETWORKS = Object.freeze({
  v4LinkLocal: ["169.254.0.0/16"], v4Multicast: ["224.0.0.0/4"], v4Private: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
  v4Reserved: ["0.0.0.0/8", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4"],
  v6LinkLocal: ["fe80::/10"], v6Multicast: ["ff00::/8"], v6Private: ["fc00::/7"],
  v6Reserved: ["64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "3fff::/20", "5f00::/16", "fec0::/10"],
  v6GlobalUnicast: ["2000::/3"], cgnat: ["100.64.0.0/10"], nat64: ["64:ff9b::/96"], teredo: ["2001::/32"],
});
export const METADATA_ADDRS = Object.freeze(["169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192", "fd00:ec2::254"]);

const CONTROL = /[\x00-\x20\x7f]/;
const NUMERIC_HOST = /^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$/i;
const HOSTNAME = /^[a-z0-9_]([a-z0-9_\-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_\-]*[a-z0-9_])?)*$/;

export class PolicyError extends Error {
  constructor(reason, url = "", kind = "policy") {
    super(reason);
    this.name = "PolicyError";
    this.reason = reason;
    this.url = url;
    this.kind = kind;
  }
}

// ---- IP parsing (BigInt) -------------------------------------------------------------------------

/** ``{v: 4|6, n: BigInt}`` for an IPv4 (four plain decimals) or IPv6 literal, else ``null``. */
export function parseIp(text) {
  const s = str(text);
  if (/^(?:0|[1-9]\d{0,2})(?:\.(?:0|[1-9]\d{0,2})){3}$/.test(s)) {
    const parts = s.split(".").map(Number);
    if (parts.some((x) => x > 255)) return null;
    return { v: 4, n: BigInt(((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256 + parts[3]) };
  }
  if (!s.includes(":") || /[^0-9a-fA-F:.]/.test(s)) return null;
  let head = s, tail4 = null;
  const lastColon = s.lastIndexOf(":");
  if (s.includes(".")) {
    const v4 = parseIp(s.slice(lastColon + 1));
    if (!v4 || v4.v !== 4) return null;
    tail4 = v4.n;
    head = s.slice(0, lastColon + 1) + "0:0";
  }
  const dbl = head.split("::");
  if (dbl.length > 2) return null;
  const toGroups = (part) => (part === "" ? [] : part.split(":"));
  const left = toGroups(dbl[0]), right = dbl.length === 2 ? toGroups(dbl[1]) : [];
  const all = left.concat(right);
  if (all.some((g) => !/^[0-9a-fA-F]{1,4}$/.test(g))) return null;
  let groups;
  if (dbl.length === 2) {
    if (all.length > 7) return null;
    groups = [...left, ...Array(8 - all.length).fill("0"), ...right];
  } else {
    if (all.length !== 8) return null;
    groups = left;
  }
  let n = 0n;
  for (const g of groups) n = (n << 16n) | BigInt(parseInt(g, 16));
  if (tail4 !== null) n = (n & ~0xffffffffn) | tail4;
  return { v: 6, n };
}

function fmtIp(ip) {
  if (ip.v === 4) return [24n, 16n, 8n, 0n].map((sh) => String((ip.n >> sh) & 255n)).join(".");
  if (ip.n >> 32n === 0xffffn) return "::ffff:" + fmtIp({ v: 4, n: ip.n & 0xffffffffn });
  const g = [];
  for (let i = 7n; i >= 0n; i--) g.push(Number((ip.n >> (i * 16n)) & 0xffffn));
  let best = -1, bestLen = 0;
  for (let i = 0; i < 8;) {
    if (g[i] !== 0) { i++; continue; }
    let j = i;
    while (j < 8 && g[j] === 0) j++;
    if (j - i > bestLen) { best = i; bestLen = j - i; }
    i = j;
  }
  const hex = g.map((x) => x.toString(16));
  if (bestLen < 2) return hex.join(":");
  return hex.slice(0, best).join(":") + "::" + hex.slice(best + bestLen).join(":");
}

const netCache = new Map();
function net(spec) {
  let n = netCache.get(spec);
  if (!n) {
    const [addr, len] = spec.split("/");
    const ip = parseIp(addr);
    const bits = ip.v === 4 ? 32n : 128n;
    const shift = bits - BigInt(len);
    n = { v: ip.v, base: ip.n >> shift, shift };
    netCache.set(spec, n);
  }
  return n;
}
const within = (ip, specs) => specs.some((s) => { const n = net(s); return n.v === ip.v && (ip.n >> n.shift) === n.base; });
const metadataSet = new Set(METADATA_ADDRS.map((a) => { const ip = parseIp(a); return `${ip.v}:${ip.n}`; }));

/** The IPv4 address hidden in an IPv6 one (mapped, compatible, 6to4, NAT64), else the address itself. */
function unwrapIp(ip) {
  if (ip.v !== 6) return ip;
  const n = ip.n;
  if (n >> 32n === 0xffffn) return { v: 4, n: n & 0xffffffffn };
  if (n >> 112n === 0x2002n) return { v: 4, n: (n >> 80n) & 0xffffffffn };
  if (within(ip, NETWORKS.nat64)) return { v: 4, n: n & 0xffffffffn };
  if (n >> 32n === 0n && n > 1n) return { v: 4, n: n & 0xffffffffn };
  return ip;
}

function categoryOf(ip0) {
  if (ip0.v === 6 && within(ip0, NETWORKS.teredo)) return "reserved";
  const ip = unwrapIp(ip0);
  if (metadataSet.has(`${ip.v}:${ip.n}`)) return "metadata";
  const v4 = ip.v === 4;
  if (ip.n === 0n) return "unspecified";
  if ((v4 && ip.n >> 24n === 127n) || (!v4 && ip.n === 1n)) return "loopback";
  if (within(ip, v4 ? NETWORKS.v4LinkLocal : NETWORKS.v6LinkLocal)) return "link-local";
  if (v4 && within(ip, NETWORKS.cgnat)) return "cgnat";
  if (within(ip, v4 ? NETWORKS.v4Multicast : NETWORKS.v6Multicast)) return "multicast";
  if (within(ip, v4 ? NETWORKS.v4Private : NETWORKS.v6Private)) return "private";
  if (within(ip, v4 ? NETWORKS.v4Reserved : NETWORKS.v6Reserved) || (!v4 && !within(ip, NETWORKS.v6GlobalUnicast))) return "reserved";
  return "public";
}

const REASONS = {
  metadata: "a cloud metadata address", unspecified: "the unspecified address", loopback: "a loopback address",
  "link-local": "a link-local address", cgnat: "a shared (CGNAT 100.64.0.0/10) address", multicast: "a multicast address",
  private: "a private address", reserved: "a reserved address",
};
const ALLOWED = {
  [PUBLIC]: new Set(["public"]),
  [OPERATOR_LOCAL]: new Set(["public", "loopback", "private", "cgnat"]),
  [INTERNAL]: new Set(["loopback"]),
};
function checkProfile(profile) {
  if (!PROFILES.includes(profile)) throw new Error(`unknown profile '${profile}' (use one of ${PROFILES.join(", ")})`);
  return profile;
}

/** ``null`` when ``addr`` may be reached under ``profile``, otherwise the reason it may not. */
export function classifyIp(addr, o = {}) {
  const profile = checkProfile(typeof o === "string" ? o : opt(o, "profile", PUBLIC));
  const text = str(addr).trim().replace(/^[\[\]]+|[\[\]]+$/g, "").split("%")[0];
  const ip = parseIp(text);
  if (!ip) return `'${str(addr)}' is not an IP address`;
  const cat = categoryOf(ip);
  if (ALLOWED[profile].has(cat)) return null;
  return `address ${text} is ${REASONS[cat] || cat}` + (profile === PUBLIC ? "" : ` (not allowed for ${profile})`);
}

/** What inet_aton makes of ``host`` (1-4 parts, decimal, 0-octal or 0x hex); ``null`` when it is not such a literal. */
export function parseLooseIpv4(host) {
  const h = pyStrip(str(host)).toLowerCase();
  if (!NUMERIC_HOST.test(h)) return null;
  const nums = [];
  for (const part of h.split(".")) {
    let v;
    if (part.startsWith("0x")) v = BigInt("0x" + (part.slice(2) || "0"));
    else if (part.length > 1 && part.startsWith("0")) { if (/[89]/.test(part)) return null; v = BigInt("0o" + part.slice(1)); }
    else v = BigInt(part);
    nums.push(v);
  }
  const last = nums[nums.length - 1], head = nums.slice(0, -1);
  if (head.some((x) => x > 255n) || last >= 256n ** BigInt(4 - head.length)) return null;
  let value = last;
  head.forEach((x, i) => { value |= x << BigInt(8 * (3 - i)); });
  return fmtIp({ v: 4, n: value });
}

function hostReason(host, profile, original) {
  if (METADATA_HOSTS.includes(host) || host.endsWith(".metadata.google.internal")) return `host ${original} is a cloud metadata name`;
  if (profile === PUBLIC) {
    if (!host.includes(".")) return `host ${original} is a local (single-label) name`;
    if (LOCAL_SUFFIXES.some((s) => host.endsWith(s))) return `host ${original} is a local name`;
  }
  return null;
}

/** urllib.parse.urlsplit's view of the netloc: ``{hostname, port, hasAt}`` or ``null`` when it is malformed. */
function splitNetloc(netloc) {
  if ((netloc.includes("[") && !netloc.includes("]")) || (netloc.includes("]") && !netloc.includes("["))) return null;
  const hostinfo = netloc.slice(netloc.lastIndexOf("@") + 1);
  let hostname, port;
  const open = hostinfo.indexOf("[");
  if (open >= 0) {
    const bracketed = hostinfo.slice(open + 1);
    const close = bracketed.indexOf("]");
    hostname = bracketed.slice(0, close < 0 ? undefined : close);
    const after = close < 0 ? "" : bracketed.slice(close + 1);
    port = after.includes(":") ? after.slice(after.indexOf(":") + 1) : "";
    if (!parseIp(hostname.split("%")[0])) return null;
  } else {
    const c = hostinfo.indexOf(":");
    hostname = c < 0 ? hostinfo : hostinfo.slice(0, c);
    port = c < 0 ? "" : hostinfo.slice(c + 1);
  }
  const z = hostname.indexOf("%");
  hostname = z < 0 ? hostname.toLowerCase() : hostname.slice(0, z).toLowerCase() + hostname.slice(z);
  return { hostname, port: port || null, hasAt: netloc.includes("@") };
}

async function defaultLookup(host) {
  const found = await dns.promises.lookup(host, { all: true, verbatim: true });
  return found.map((f) => f.address);
}

function makeLookup(o) {
  const hosts = opt(o, "hosts", null);
  if (hosts) return async (h) => { if (!(h in hosts)) { const e = new Error("getaddrinfo ENOTFOUND " + h); e.code = "ENOTFOUND"; throw e; } return hosts[h]; };
  return opt(o, "lookup", null) || defaultLookup;
}

/** ``{reason, addrs, kind}``: ``reason`` is ``null`` when the URL is fine. */
export async function evaluateUrl(url, o = {}) {
  const profile = checkProfile(typeof o === "string" ? o : opt(o, "profile", PUBLIC));
  const maxLen = opt(o, "maxLen", MAX_URL_LEN);
  const raw = pyStrip(str(url));
  const bad = (reason, kind = "policy") => ({ reason, addrs: [], kind });
  if (!raw) return bad("empty URL");
  if (maxLen && raw.length > maxLen) return bad(`URL longer than ${maxLen} characters`);
  if (CONTROL.test(raw)) return bad("URL contains control characters or spaces");
  if (raw.split("?")[0].split("#")[0].includes("\\")) return bad("URL contains a backslash");
  const sm = /^([A-Za-z][A-Za-z0-9+.\-]*):/.exec(raw);
  const scheme = sm ? sm[1].toLowerCase() : "";
  const rest = sm ? raw.slice(sm[0].length) : raw;
  let netloc = "";
  if (rest.startsWith("//")) { const m = /^\/\/([^/?#]*)/.exec(rest); netloc = m[1]; }
  const sp = splitNetloc(netloc);
  if (!sp) return bad("malformed URL");
  if (scheme !== "http" && scheme !== "https") return bad(`unsupported scheme '${scheme || "(none)"}': only http and https are allowed`);
  if (!netloc || !sp.hostname) return bad("URL has no host");
  if (sp.hasAt) return bad("URLs with embedded credentials are not allowed");
  let port = scheme === "https" ? 443 : 80;
  if (sp.port !== null) {
    if (!/^[0-9]+$/.test(sp.port) || parseInt(sp.port, 10) > 65535) return bad("invalid port");
    port = parseInt(sp.port, 10);
  }
  if (port === 0) return bad("invalid port");
  const original = sp.hostname;
  let host = pyStrip(original.normalize("NFKC")).toLowerCase().replace(/\.+$/, "");
  if (!host) return bad("URL has no host");

  const literal = parseIp(host.split("%")[0]);
  if (literal) {
    const reason = classifyIp(fmtIp(literal), { profile });
    return { reason, addrs: reason ? [] : [fmtIp(literal)], kind: "policy" };
  }
  if (NUMERIC_HOST.test(host)) {
    const loose = parseLooseIpv4(host);
    return bad(`host ${original} is an obfuscated IP address` + (loose ? ` (${loose})` : ""));
  }
  if (!/^[\x00-\x7f]*$/.test(host)) {
    const ascii = domainToASCII(host);
    if (!ascii) return bad(`host ${original} is not a valid name`);
    host = ascii;
  }
  if (!HOSTNAME.test(host) || host.length > 253) return bad(`host ${original} is not a valid name`);
  const hr = hostReason(host, profile, original);
  if (hr) return bad(hr);
  if (profile !== PUBLIC && (host === "localhost" || host.endsWith(".localhost"))) return { reason: null, addrs: ["127.0.0.1", "::1"], kind: "policy" };
  let found;
  try {
    found = (await makeLookup(o)(host, port)).map(String);
  } catch (error) {
    return bad(`${UNRESOLVABLE_PREFIX} ${original}: ${error && error.message ? error.message : error}`, "dns");
  }
  if (!found.length) return bad(`${UNRESOLVABLE_PREFIX} ${original}`, "dns");
  for (const addr of found) {
    const problem = classifyIp(addr, { profile });
    if (problem) {
      const i = problem.indexOf(" is ");
      return bad(`host ${original} resolves to a non-allowed address (${addr}: ${i < 0 ? problem : problem.slice(i + 4)})`);
    }
  }
  return { reason: null, addrs: found, kind: "policy" };
}

/** ``null`` when ``url`` may be fetched under ``profile``, otherwise the reason it may not. Options: profile,
 *  lookup(host, port) -> [ip] (sync or async), hosts ({name: [ip]}, a test seam), maxLen. */
export async function checkUrl(url, o = {}) { return (await evaluateUrl(url, o)).reason; }

/** The checked addresses of ``url``; throws PolicyError (``kind`` "policy" or "dns") when it is not allowed. */
export async function resolvePublic(url, o = {}) {
  const r = await evaluateUrl(url, o);
  if (r.reason) throw new PolicyError(r.reason, str(url), r.kind);
  return r.addrs;
}

/** A ``lookup`` function for node:http(s) that answers with the already-checked addresses only (connection pinning). */
export function pinnedLookup(addrs) {
  const list = addrs.map((address) => ({ address, family: address.includes(":") ? 6 : 4 }));
  return (hostname, options, cb) => {
    if (typeof options === "function") { cb = options; options = {}; }
    const want = options && (options.family === 4 || options.family === "IPv4" ? 4 : options.family === 6 || options.family === "IPv6" ? 6 : 0);
    const usable = want ? list.filter((a) => a.family === want) : list;
    if (!usable.length) { const e = new Error("getaddrinfo ENOTFOUND " + hostname); e.code = "ENOTFOUND"; e.hostname = hostname; cb(e); return; }
    if (options && options.all) cb(null, usable);
    else cb(null, usable[0].address, usable[0].family);
  };
}

// ------------------------------------------------------------------------------------------------ blocks

export const BLOCK_REASONS = Object.freeze(["cloudflare", "akamai", "datadome", "perimeterx", "captcha", "login", "http_403", "http_429", "http_5xx"]);
export const BROWSER_RETRY_REASONS = Object.freeze(["cloudflare", "akamai", "datadome", "perimeterx", "captcha", "login", "http_403"]);
const SMALL_PAGE = 60000, TINY_PAGE = 12000;
const B_TITLE = /<title[^>]*>([\s\S]*?)<\/title>/i;
const B_TAGS = /<[^>]+>/g;
const LOGIN_PATH = /\/(?:login|log-in|signin|sign-in|sign_in|iniciar-?sesion|acceso|ap\/signin|account\/login|customer\/account\/login)(?:[/?#.]|$)/i;
const LOGIN_TITLE = /^\s*(?:inicia(?:r)?\s+sesi[oó]n|acceso\s+de\s+clientes|identif[ií]cate|sign\s*in|log\s*in|login|iniciar sesi[oó]n en .*)\s*(?:[|\-–—:].*)?$/i;
const CF_TITLE = /just a moment|un momento|attention required!? \| cloudflare|checking your browser|verificando/i;
const CF_MARKERS = ["challenges.cloudflare.com", "challenge-platform", "cf-chl", "cf-turnstile", "__cf_chl", "cdn-cgi/styles/cf.errors"];
const AMAZON_CAPTCHA = ["validatecaptcha", "robot check", "introduce los caracteres que ves", "type the characters you see",
  "enter the characters you see below", "introduce los caracteres que aparecen"];

function pathOf(url) { const m = /^(?:[A-Za-z][A-Za-z0-9+.\-]*:)?(?:\/\/[^/?#]*)?([^?#]*)/.exec(str(url)); return m ? m[1] : ""; }
function blockTitle(text) {
  const m = B_TITLE.exec(text.slice(0, 40000));
  return m ? pyStrip(unescapeHtml(m[1].replace(B_TAGS, ""))) : "";
}
function visibleLen(text) {
  const stripped = text.replace(/<(script|style)\b[\s\S]*?<\/\1>/gi, " ");
  return pyStrip(stripped.replace(B_TAGS, " ").replace(WS_RUN, " ")).length;
}

/** Classify a response: cloudflare, akamai, datadome, perimeterx, captcha, login, http_403, http_429, http_5xx or "". */
export function detectBlock(status, text, headers, url) {
  const hdr = {};
  for (const [k, v] of Object.entries(headers || {})) hdr[String(k).toLowerCase()] = String(v);
  text = str(text);
  status = Number(status) || 0;
  const size = text.length;
  const small = size < SMALL_PAGE;
  const body = small ? unescapeHtml(text.slice(0, SMALL_PAGE)).toLowerCase() : "";
  const title = small ? blockTitle(text) : "";
  const titleL = title.toLowerCase();
  const server = (hdr.server || "").toLowerCase();
  const cookies = ((hdr["set-cookie"] || "") + " " + (hdr.cookie || "")).toLowerCase();
  const any = (list) => list.some((m) => body.includes(m));

  if ((hdr["cf-mitigated"] || "").toLowerCase() === "challenge") return "cloudflare";
  if (small) {
    const cfMarker = body.includes("cloudflare") || any(CF_MARKERS);
    if ((CF_TITLE.test(title) && cfMarker) || ([403, 429, 503].includes(status) && any(CF_MARKERS))) return "cloudflare";
  }
  if (small) {
    if ((titleL.includes("access denied") || body.includes("<h1>access denied</h1>")) &&
        (body.includes("reference #") || body.includes("errors.edgesuite.net") || body.includes("akamai"))) return "akamai";
    if (size < TINY_PAGE && (body.includes("bm-verify") || body.includes("/_sec/verify") || body.includes("triggerinterstitialchallenge"))) return "akamai";
    if ([403, 429].includes(status) && server.includes("akamai") && size < TINY_PAGE) return "akamai";
  }
  if ("x-datadome" in hdr || "x-dd-b" in hdr || cookies.includes("datadome=")) {
    if ([403, 429].includes(status) || size < TINY_PAGE) return "datadome";
  }
  if (small && (body.includes("captcha-delivery.com") || body.includes("geo.captcha-delivery"))) return "datadome";
  if (small && (body.includes('id="px-captcha"') || body.includes("id='px-captcha'") || body.includes("press & hold") ||
      body.includes("pulsa y mantén") || (body.includes("px-cloud.net") && [403, 429].includes(status)))) return "perimeterx";
  if (small && any(AMAZON_CAPTCHA)) return "captcha";
  if (small && /class="[^"]*\b(?:g-recaptcha|h-captcha|cf-turnstile)\b|data-sitekey=/.test(body) && visibleLen(text) < 800) return "captcha";
  if (LOGIN_PATH.test(pathOf(url))) return "login";
  if (small && title && LOGIN_TITLE.test(title)) return "login";
  if (status === 401) return "login";
  if (status === 403) return "http_403";
  if (status === 429) return "http_429";
  if (status >= 500 && status <= 599) return "http_5xx";
  return "";
}

const HINTS = {
  cloudflare: "Cloudflare challenge page", akamai: "Akamai bot-manager page", datadome: "DataDome challenge",
  perimeterx: "PerimeterX / HUMAN challenge", captcha: "CAPTCHA / robot check", login: "login wall",
  http_403: "HTTP 403 forbidden", http_429: "HTTP 429 too many requests", http_5xx: "server error",
};
/** Human-readable explanation of a ``detectBlock`` verdict. */
export function blockHint(reason) { return HINTS[reason] || reason; }

// ------------------------------------------------------------------------------------------------ HTML tokenizer
// A small, forgiving tokenizer with the behaviour of Python's html.parser that matters here: comments,
// declarations and processing instructions are dropped, script and style content is raw text, character
// references are decoded in text and attribute values, attribute names are lowercased.

const TAG_NAME = /^[A-Za-z][^\t\n\r\f />\x00]*/;
const RAW_TEXT = new Set(["script", "style"]);

/** Calls ``h.start(tag, attrs, selfClosing)``, ``h.end(tag)`` and ``h.data(text)``. ``attrs`` maps name -> value. */
function tokenizeHtml(html, h) {
  const s = str(html);
  const n = s.length;
  let i = 0;
  const text = (t) => { if (t) h.data(unescapeHtml(t)); };
  while (i < n) {
    const lt = s.indexOf("<", i);
    if (lt < 0) { text(s.slice(i)); break; }
    if (lt > i) text(s.slice(i, lt));
    i = lt;
    const c1 = s[i + 1];
    if (s.startsWith("<!--", i)) {
      const end = s.indexOf("-->", i + 4);
      i = end < 0 ? n : end + 3;
    } else if (c1 === "!" || c1 === "?") {
      if (s.startsWith("<![CDATA[", i)) { const end = s.indexOf("]]>", i); i = end < 0 ? n : end + 3; }
      else { const end = s.indexOf(">", i); i = end < 0 ? n : end + 1; }
    } else if (c1 === "/" && /[A-Za-z]/.test(s[i + 2] || "")) {
      const m = TAG_NAME.exec(s.slice(i + 2, i + 2 + 256));
      const end = s.indexOf(">", i);
      h.end(m[0].toLowerCase());
      i = end < 0 ? n : end + 1;
    } else if (c1 !== undefined && /[A-Za-z]/.test(c1)) {
      const m = TAG_NAME.exec(s.slice(i + 1, i + 1 + 256));
      const tag = m[0].toLowerCase();
      let j = i + 1 + m[0].length;
      const attrs = {};
      let selfClosing = false, closed = false;
      while (j < n) {
        while (j < n && /[\t\n\r\f /]/.test(s[j])) { if (s[j] === "/" && s[j + 1] === ">") { selfClosing = true; } j++; }
        if (j >= n) break;
        if (s[j] === ">") { j++; closed = true; break; }
        let k = j;
        if (s[k] === "=") k++;
        while (k < n && !/[\t\n\r\f />=]/.test(s[k])) k++;
        const name = s.slice(j, k).toLowerCase();
        j = k;
        while (j < n && /[\t\n\r\f]| /.test(s[j])) j++;
        let value = "";
        if (s[j] === "=") {
          j++;
          while (j < n && /[\t\n\r\f ]/.test(s[j])) j++;
          const q = s[j];
          if (q === '"' || q === "'") {
            const end = s.indexOf(q, j + 1);
            value = s.slice(j + 1, end < 0 ? n : end);
            j = end < 0 ? n : end + 1;
          } else {
            let e = j;
            while (e < n && !/[\t\n\r\f >]/.test(s[e])) e++;
            value = s.slice(j, e);
            j = e;
          }
          value = unescapeHtml(value);
        }
        if (name && !(name in attrs)) attrs[name] = value;
        selfClosing = false;
      }
      if (!closed && j >= n) { i = n; break; }
      i = j;
      h.start(tag, attrs, selfClosing);
      if (RAW_TEXT.has(tag) && !selfClosing) {
        const re = new RegExp(`</${tag}(?=[\\t\\n\\r\\f />]|$)`, "i");
        const m2 = re.exec(s.slice(i));
        const stop = m2 ? i + m2.index : n;
        if (stop > i) h.data(s.slice(i, stop));
        i = stop;
      }
    } else {
      text("<");
      i += 1;
    }
  }
}

// ------------------------------------------------------------------------------------------------ DOM

const VOID = new Set("area base br col embed hr img input link meta param source track wbr".split(" "));
const CLOSES_P = new Set(`address article aside blockquote details div dl fieldset figcaption figure footer form h1 h2 h3 h4
h5 h6 header hr main nav ol p pre section table ul`.split(/\s+/));
const IMPLIED_CLOSE = {
  li: [new Set(["li"]), new Set(["ul", "ol", "menu"])],
  dt: [new Set(["dt", "dd"]), new Set(["dl"])],
  dd: [new Set(["dt", "dd"]), new Set(["dl"])],
  tr: [new Set(["tr"]), new Set(["table", "thead", "tbody", "tfoot"])],
  td: [new Set(["td", "th"]), new Set(["tr", "table"])],
  th: [new Set(["td", "th"]), new Set(["tr", "table"])],
  thead: [new Set(["thead", "tbody", "tfoot"]), new Set(["table"])],
  tbody: [new Set(["thead", "tbody", "tfoot"]), new Set(["table"])],
  tfoot: [new Set(["thead", "tbody", "tfoot"]), new Set(["table"])],
  option: [new Set(["option"]), new Set(["select", "datalist"])],
};
const INLINE = new Set(`a abbr b bdi bdo big cite code data del dfn em font i img ins kbd label mark q s samp small span strike
strong sub sup time tt u var wbr br`.split(/\s+/));
const MAX_DEPTH = 200;
const SELF_CLOSING_SVG = new Set(["path", "circle", "rect", "use", "line", "polygon", "polyline", "ellipse", "stop"]);

/** A tiny DOM node: an element (``tag`` is its lowercase name) or a text node (``tag === "#text"``). */
export class Node {
  constructor(tag, attrs = {}, parent = null, text = "") {
    this.tag = tag;
    this.attrs = attrs;
    this.children = [];
    this.parent = parent;
    this.text = text;
    this._classes = null;
  }
  get isText() { return this.tag === "#text"; }
  get classes() {
    if (!this._classes) this._classes = new Set(pySplit((this.attrs.class || "").toLowerCase()));
    return this._classes;
  }
  get(name, dflt = "") { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : dflt; }
  has(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  *iter() {
    const stack = [this];
    while (stack.length) {
      const x = stack.pop();
      yield x;
      for (let i = x.children.length - 1; i >= 0; i--) stack.push(x.children[i]);
    }
  }
  find(tag) { for (const x of this.iter()) if (x.tag === tag) return x; return null; }
  findAll(tag) { return [...this.iter()].filter((x) => x.tag === tag); }
  *ancestors() { let x = this.parent; while (x) { yield x; x = x.parent; } }
}

/** Parse ``html`` into a Node tree (tolerant: unclosed and misnested tags are repaired the usual way). */
export function parseHtml(html) {
  const root = new Node("#document");
  const stack = [root];
  const top = () => stack[stack.length - 1];
  const closeImplied = (tag) => {
    if (CLOSES_P.has(tag)) {
      for (let i = stack.length - 1; i > 0; i--) {
        const name = stack[i].tag;
        if (name === "p") { stack.length = i; break; }
        if (!INLINE.has(name)) break;
      }
    }
    const rule = IMPLIED_CLOSE[tag];
    if (rule) {
      for (let i = stack.length - 1; i > 0; i--) {
        const name = stack[i].tag;
        if (rule[0].has(name)) { stack.length = i; break; }
        if (rule[1].has(name)) break;
      }
    }
  };
  const start = (tag, attrs) => {
    closeImplied(tag);
    const node = new Node(tag, attrs, top());
    top().children.push(node);
    if (!VOID.has(tag) && stack.length < MAX_DEPTH) stack.push(node);
  };
  tokenizeHtml(html, {
    start(tag, attrs, selfClosing) {
      if (selfClosing && (VOID.has(tag) || SELF_CLOSING_SVG.has(tag))) {
        start(tag, attrs);
        if (!VOID.has(tag) && top().tag === tag) stack.pop();
      } else start(tag, attrs);
    },
    end(tag) {
      if (VOID.has(tag)) return;
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].tag === tag) { stack.length = i; return; }
    },
    data(text) {
      if (!text) return;
      const t = top();
      const last = t.children[t.children.length - 1];
      if (last && last.isText) last.text += text;
      else t.children.push(new Node("#text", {}, t, text));
    },
  });
  return root;
}

// ------------------------------------------------------------------------------------------------ readable text

const ALWAYS_DROP = new Set("script style noscript template svg iframe canvas head title meta link dialog object embed base".split(" "));
const CHROME_TAGS = new Set(["nav", "footer", "aside", "form", "header"]);
const NOISE_TOKENS = /cookie|consent|onetrust|cookiebot|gdpr|cmp-|newsletter/i;
const HIDDEN_CLASS = new Set(["hidden", "d-none", "is-hidden", "u-hidden", "hide", "sr-only", "visually-hidden", "is-template"]);
const HIDDEN_STYLE = /display\s*:\s*none|visibility\s*:\s*hidden/i;
const CHROME_ROLES = new Set(["navigation", "banner", "contentinfo"]);
const BLOCK = new Set(`address article aside blockquote body caption dd details div dl dt fieldset figcaption figure footer form
h1 h2 h3 h4 h5 h6 header hr html li main nav ol p pre section summary table tbody tfoot thead tr ul menu
center`.split(/\s+/));
const SEPARATORS = new Set(["td", "th"]);
export const MIN_WORDS = 40;

function hidden(n) {
  if (n.has("hidden") || n.get("aria-hidden").toLowerCase() === "true" || n.get("type").toLowerCase() === "hidden") return true;
  const style = n.get("style");
  if (style && HIDDEN_STYLE.test(style)) return true;
  for (const c of n.classes) if (HIDDEN_CLASS.has(c)) return true;
  return false;
}
function isChrome(n) {
  const name = n.tag;
  if (name === "header") {
    for (const p of n.ancestors()) if (p.tag === "article" || p.tag === "main") return false;
    return true;
  }
  if (CHROME_TAGS.has(name)) return true;
  if (CHROME_ROLES.has(n.get("role").toLowerCase())) return true;
  if (["html", "body", "main", "article"].includes(name)) return false;
  const ident = n.get("id") + " " + n.get("class");
  return !!(pyStrip(ident) && NOISE_TOKENS.test(ident));
}
const collapse = (text) => pyStrip(str(text).replace(/\xa0/g, " ").replace(WS_RUN, " "));
const BREAK = Symbol("break");

function emit(n, dropChrome, out, inPre = false) {
  for (const c of n.children) {
    if (c.isText) {
      if (inPre) c.text.split("\n").forEach((line, i) => { if (i) out.push(BREAK); out.push(line); });
      else out.push(c.text);
      continue;
    }
    const name = c.tag;
    if (ALWAYS_DROP.has(name) || hidden(c) || (dropChrome && isChrome(c))) continue;
    if (name === "br" || name === "hr") { out.push(BREAK); continue; }
    const block = BLOCK.has(name);
    if (block) out.push(BREAK);
    emit(c, dropChrome, out, inPre || name === "pre");
    if (block) out.push(BREAK);
    else if (SEPARATORS.has(name)) out.push(" ");
  }
}
function toLines(out) {
  const lines = [];
  let cur = [];
  const flush = () => {
    const line = collapse(cur.join(""));
    cur = [];
    if (line && (!lines.length || lines[lines.length - 1] !== line)) lines.push(line);
  };
  for (const piece of out) { if (piece === BREAK) flush(); else cur.push(piece); }
  flush();
  return lines;
}
function textOf(node) { let t = ""; for (const x of node.iter()) if (x.isText) t += x.text; return t; }
function titleOf(root) {
  const t = root.find("title");
  if (t) { const title = collapse(textOf(t)); if (title) return cpSlice(title, 300); }
  const h1 = root.find("h1");
  return h1 ? cpSlice(collapse(textOf(h1)), 300) : "";
}
const wordCount = (lines) => lines.reduce((a, l) => a + pySplit(l).length, 0);

function chooseRoot(doc) {
  const body = doc.find("body") || doc;
  for (const n of doc.iter()) if (n.tag === "main" || n.get("role").toLowerCase() === "main") return n;
  const articles = doc.findAll("article");
  return articles.length === 1 ? articles[0] : body;
}

/** ``{title, text}`` — what a person would call the content (scripts, styles, hidden nodes, navigation, footers,
 *  asides, forms and cookie banners dropped; ``main`` or a single ``article`` preferred). ``dropChrome: false`` keeps
 *  every visible word. */
export function htmlToText(html, o = {}) {
  const dropChrome = opt(o, "dropChrome", true);
  const doc = parseHtml(html);
  const title = titleOf(doc);
  const body = doc.find("body") || doc;
  if (!dropChrome) { const out = []; emit(body, false, out); return { title, text: toLines(out).join("\n") }; }
  const root = chooseRoot(doc);
  let out = [];
  emit(root, true, out);
  let lines = toLines(out);
  if (root !== body && wordCount(lines) < MIN_WORDS) { out = []; emit(body, true, out); lines = toLines(out); }
  return { title, text: lines.join("\n") };
}

// ------------------------------------------------------------------------------------------------ quality, hashing

export const NAV_WORDS = Object.freeze(NAV_WORD_LIST.split(" ").sort());
const NAV_SET = new Set(NAV_WORDS);
const BLOCK_PHRASES = ["enable javascript", "captcha", "access denied", "verify you are human", "just a moment", "acceso denegado",
  "activa javascript", "not a robot", "checking your browser", "unusual traffic"];

export function chromeRatio(text) {
  let total = 0, chrome = 0;
  for (const line of splitLines(text)) {
    const words = line.toLowerCase().match(/\p{L}+/gu) || [];
    if (!words.length) continue;
    total += words.length;
    if (words.length <= 4 && words.some((w) => NAV_SET.has(w))) chrome += words.length;
  }
  return total ? chrome / total : 1.0;
}

/** "" when ``text`` is a usable document, else "blocked page", "too short" or "mostly navigation". */
export function quality(text, o = {}) {
  const minWords = typeof o === "number" ? o : opt(o, "minWords", MIN_WORDS);
  const s = str(text);
  const words = pySplit(s);
  if (words.length < 150 && BLOCK_PHRASES.some((p) => s.slice(0, 800).toLowerCase().includes(p))) return "blocked page";
  if (words.length < minWords) return "too short";
  const lines = splitLines(s).filter((l) => pyStrip(l));
  const short = lines.filter((l) => pySplit(l).length <= 3).length;
  if (lines.length && short / lines.length > 0.85 && words.length < 400) return "mostly navigation";
  if (chromeRatio(s) >= 0.6) return "mostly navigation";
  return "";
}

const NB = "(?<![\\p{L}\\p{N}_])";          // Python's \b before a word character
const NA = "(?![\\p{L}\\p{N}_])";           // ... and after one
const CLOCK = new RegExp(`${NB}\\p{Nd}{1,2}:\\p{Nd}{2}(?::\\p{Nd}{2})?(?:[${WS}]?[ap]\\.?m\\.?)?(?!\\p{Nd})`, "giu");
const ISO_STAMP = new RegExp(`${NB}\\p{Nd}{4}-\\p{Nd}{2}-\\p{Nd}{2}[t ]\\p{Nd}{2}:\\p{Nd}{2}(?::\\p{Nd}{2})?(?:[.,]\\p{Nd}+)?(?:z|[+-]\\p{Nd}{2}:?\\p{Nd}{2})?(?!\\p{Nd})`, "giu");
const UNIT = "(?:segundos?|segs?|minutos?|mins?|horas?|hrs?|h|d[ií]as?|semanas?|meses|seconds?|secs?|minutes?|hours?|days?|weeks?|months?)";
const RELATIVE = new RegExp(`${NB}(?:hace|quedan?|faltan?)[${WS}]+\\p{Nd}+[${WS}]+${UNIT}${NA}|${NB}\\p{Nd}+[${WS}]+${UNIT}[${WS}]+(?:ago|left|remaining)${NA}` +
  `|${NB}(?:just now|justo ahora|ahora mismo|hace un momento)${NA}`, "giu");
const TOKEN = new RegExp(`${NB}[0-9a-f]{16,}${NA}`, "giu");
const VOLATILE_LINE = new RegExp(`^(?:©|\\(c\\)|copyright${NA}|hoy es${NA}|actualizado hace${NA}|updated[${WS}]+\\p{Nd}+[${WS}]+\\w+[${WS}]+ago${NA}|` +
  `last updated:?[${WS}]*(?:just now|\\p{Nd}+[${WS}]+\\w+[${WS}]+ago)${NA})`, "iu");

const casefold = (s) => s.toLowerCase().replace(/ß|ẞ/g, "ss").replace(/ς/g, "σ").replace(/ſ/g, "s");

/** Text with the volatile noise removed (clocks, ISO stamps, "5 minutes ago", ``©`` lines, long hex tokens), one
 *  normalised line per line. Every other digit is kept on purpose: prices and quantities are what a sentry must notice. */
export function normaliseForHash(text) {
  const out = [];
  for (const raw of splitLines(text)) {
    let line = casefold(raw.normalize("NFKC"));
    line = line.replace(/​/g, "").replace(/‌/g, "").replace(/﻿/g, "");
    line = pyStrip(line.replace(WS_RUN, " "));
    if (!line || VOLATILE_LINE.test(line)) continue;
    for (const pattern of [ISO_STAMP, RELATIVE, CLOCK, TOKEN]) line = line.replace(pattern, " ");
    line = pyStrip(line.replace(WS_RUN, " "));
    if (line) out.push(line);
  }
  return out.join("\n");
}
/** SHA-256 (hex) of ``normaliseForHash``. */
export function contentHash(text) { return sha256(normaliseForHash(text)); }

/** A short plain excerpt: whitespace collapsed, cut at a word boundary with an ellipsis (text.js ``clampText``). */
export function excerpt(text, maxChars = 300) { return clampText(text, maxChars); }

// ------------------------------------------------------------------------------------------------ metadata, JSON-LD

function scanHtml(html) {
  const s = { lang: "", baseHref: "", title: "", h1: "", metas: [], links: [], scripts: [] };
  let inTitle = false, inH1 = false, titleDone = false, h1Done = false, script = null;
  tokenizeHtml(html, {
    start(tag, a, selfClosing) {
      if (tag === "html") s.lang = s.lang || a.lang || a["xml:lang"] || "";
      else if (tag === "base") s.baseHref = s.baseHref || a.href || "";
      else if (tag === "meta") s.metas.push(a);
      else if (tag === "link") s.links.push(a);
      else if (tag === "title" && !titleDone) inTitle = true;
      else if (tag === "h1" && !h1Done) inH1 = true;
      else if (tag === "script" && /ld\+json/i.test(a.type || "")) script = [];
      if (selfClosing) this.end(tag);
    },
    end(tag) {
      if (tag === "title" && inTitle) { inTitle = false; titleDone = true; }
      else if (tag === "h1" && inH1) { inH1 = false; h1Done = true; }
      else if (tag === "script" && script !== null) { s.scripts.push(script.join("")); script = null; }
    },
    data(text) {
      if (script !== null) script.push(text);
      else if (inTitle) s.title += text;
      else if (inH1) s.h1 += text;
    },
  });
  return s;
}

const clean = (text) => pyStrip(str(text).replace(/\xa0/g, " ").replace(WS_RUN, " "));
function absUrl(href, base) {
  href = pyStrip(str(href));
  if (!href) return "";
  if (/^(?:javascript:|data:|vbscript:)/i.test(href)) return "";
  return base ? urljoin(base, href) : href;
}
const isHttp = (url) => /^https?:\/\//i.test(url);
function baseOf(scan, baseUrl) { return scan.baseHref ? absUrl(scan.baseHref, baseUrl) || baseUrl : baseUrl; }

const CONTROL_CHARS = /[\x00-\x08\x0b\x0c\x0e-\x1f]/g;

function stripTrailingCommas(s) {
  let out = "", inStr = false, esc = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (inStr) {
      out += ch;
      if (esc) esc = false;
      else if (ch === "\\") esc = true;
      else if (ch === '"') inStr = false;
    } else if (ch === '"') { inStr = true; out += ch; }
    else if (ch === ",") {
      let j = i + 1;
      while (j < s.length && " \t\r\n".includes(s[j])) j++;
      if (!(j < s.length && "}]".includes(s[j]))) out += ch;
    } else out += ch;
  }
  return out;
}
/** Python's ``json.loads(strict=False)`` allows raw tabs and newlines inside strings; JSON.parse does not. */
function escapeRawControls(s) {
  let out = "", inStr = false, esc = false;
  for (const ch of s) {
    if (inStr) {
      if (esc) { esc = false; out += ch; }
      else if (ch === "\\") { esc = true; out += ch; }
      else if (ch === '"') { inStr = false; out += ch; }
      else if (ch === "\n") out += "\\n";
      else if (ch === "\r") out += "\\r";
      else if (ch === "\t") out += "\\t";
      else out += ch;
    } else { if (ch === '"') inStr = true; out += ch; }
  }
  return out;
}
function parseJsonLoose(candidate) {
  try { return [JSON.parse(candidate), ""]; } catch (e) {
    try { return [JSON.parse(escapeRawControls(candidate)), ""]; } catch (e2) { return [null, String(e.message || e)]; }
  }
}

/** Parse one JSON-LD payload leniently: ``[value, ""]`` or ``[null, error]``. */
export function loadJsonld(raw) {
  let text = pyStrip(str(raw).replace(/^﻿+/, ""));
  if (!text) return [null, "empty block"];
  text = text.replace(new RegExp(`^[${WS}]*<!--`), "");
  text = pyStrip(text.replace(new RegExp(`-->[${WS}]*$`), ""));
  const m = /^(?:\/\/\s*)?<!\[CDATA\[([\s\S]*?)(?:\/\/\s*)?\]\]>$/.exec(text);
  if (m) text = pyStrip(m[1]);
  text = text.replace(CONTROL_CHARS, " ");
  const attempts = [text, stripTrailingCommas(text)];
  if (text.includes("&quot;") || text.includes("&#34;")) { const u = unescapeHtml(text); attempts.push(u, stripTrailingCommas(u)); }
  let error = "";
  for (const candidate of attempts) {
    const [value, err] = parseJsonLoose(candidate);
    if (!err) return [value, ""];
    error = error || err;
  }
  return [null, error.slice(0, 120)];
}

function blocksFromScripts(scripts) {
  const payloads = [], errors = [];
  scripts.forEach((raw, i) => {
    const [value, error] = loadJsonld(raw);
    if (error) errors.push(`block ${i + 1}: ${error}`); else payloads.push(value);
  });
  return [payloads, errors];
}

/** ``[payloads, errors]``: every ``<script type="application/ld+json">`` that parses, and one message per block that did not. */
export function jsonldBlocks(html) { return blocksFromScripts(scanHtml(html).scripts); }

export const DEFAULT_SKIP = Object.freeze(["review", "reviews", "aggregaterating", "isrelatedto", "issimilarto", "isaccessoryorsparepartfor",
  "isconsumablefor", "mainentityofpage", "potentialaction", "breadcrumb", "publisher", "author"]);
const typeName = (v) => str(v).split("/").pop().split(":").pop().toLowerCase();
function typesOf(node) {
  const raw = node["@type"];
  const values = Array.isArray(raw) ? raw : [raw];
  return new Set(values.filter((v) => v).map(typeName));
}
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** Every typed JSON-LD node (parents before children) as an array, found through ``@graph``, lists, ``ItemList``
 *  elements, ``hasVariant``, ``mainEntity`` and plain nesting. ``types`` filters (case-insensitive, any schema.org prefix);
 *  keys in ``skip`` (default: reviews, related products, publisher, author ...) are not descended into. */
export function jsonldNodes(blocks, types = null, o = {}) {
  if (types && !Array.isArray(types) && typeof types === "object" && !(types instanceof Set)) { o = types; types = null; }
  const wanted = types ? new Set([...types].map(typeName)) : null;
  const skipped = new Set([...(opt(o, "skip", DEFAULT_SKIP))].map((k) => String(k).toLowerCase()));
  const found = [];
  const stack = [...(Array.isArray(blocks) ? blocks : [blocks])].reverse().map((b) => [b, 0]);
  while (stack.length) {
    const [node, depth] = stack.pop();
    if (depth > 14) continue;
    if (Array.isArray(node)) { for (let i = node.length - 1; i >= 0; i--) stack.push([node[i], depth + 1]); continue; }
    if (!isObj(node)) continue;
    const kinds = typesOf(node);
    if (kinds.size && (!wanted || [...kinds].some((k) => wanted.has(k)))) found.push(node);
    const children = [];
    for (const [k, v] of Object.entries(node)) {
      if (skipped.has(k.toLowerCase()) || (k.startsWith("@") && k !== "@graph" && k !== "@list")) continue;
      if (v !== null && typeof v === "object") children.push(v);
    }
    for (let i = children.length - 1; i >= 0; i--) stack.push([children[i], depth + 1]);
  }
  return found;
}

function firstText(value) {
  if (Array.isArray(value)) { for (const v of value) { const t = firstText(v); if (t) return t; } return ""; }
  if (isObj(value)) return firstText(value.name || value["@id"] || value.value || "");
  return value === null || value === undefined ? "" : clean(unescapeHtml(String(value)));
}

const FEED_TYPES = { "application/rss+xml": "rss", "application/atom+xml": "atom", "application/feed+json": "json",
  "application/json": "json", "text/xml": "rss", "application/xml": "rss" };

/** Feeds a page advertises: ``[{url, type, kind, title}]`` in document order, absolute, de-duplicated. */
export function discoverFeeds(html, baseUrl = "") {
  const scan = scanHtml(html);
  const base = baseOf(scan, str(baseUrl));
  const out = [], seen = new Set();
  for (const link of scan.links) {
    const rel = pySplit((link.rel || "").toLowerCase());
    const mime = pyStrip((link.type || "").toLowerCase().split(";")[0]);
    if (!rel.includes("alternate") && !rel.includes("feed")) continue;
    const kind = FEED_TYPES[mime];
    if (!kind) continue;
    const href = link.href || "";
    if (["application/xml", "text/xml", "application/json"].includes(mime) && !/rss|atom|feed|xml/i.test(href + " " + (link.title || ""))) continue;
    const url = absUrl(href, base);
    if (!url || seen.has(url)) continue;
    seen.add(url);
    out.push({ url, type: mime, kind, title: clean(link.title || "") });
  }
  return out;
}

function iconSize(link) { const m = /^(\d+)x(\d+)/.exec((link.sizes || "").toLowerCase()); return m ? parseInt(m[1], 10) * parseInt(m[2], 10) : 0; }

/** Icon URLs to try (absolute http(s), de-duplicated): ``/favicon.ico``, declared icons, then ``apple-touch-icon`` by size.
 *  ``icoFirst: false`` puts the declared icons before ``/favicon.ico``. */
export function faviconCandidates(html, baseUrl = "", o = {}) {
  const icoFirst = opt(o, "icoFirst", true);
  const scan = scanHtml(html);
  const base = baseOf(scan, str(baseUrl));
  const bu = splitUrl(baseUrl);
  const dflt = bu ? `${bu.scheme}://${bu.userinfo ? bu.userinfo + "@" : ""}${bu.host.includes(":") ? `[${bu.host}]` : bu.host}${bu.port ? ":" + bu.port : ""}/favicon.ico` : "";
  const declared = [], touch = [];
  for (const link of scan.links) {
    const rel = pySplit((link.rel || "").toLowerCase());
    if (!link.href) continue;
    const url = absUrl(link.href, base);
    if (!isHttp(url)) continue;
    if (rel.some((r) => r.startsWith("apple-touch-icon"))) touch.push([-iconSize(link), url]);
    else if (rel.includes("icon")) declared.push(url);
  }
  touch.sort((a, b) => a[0] - b[0]);
  const touchUrls = touch.map((t) => t[1]);
  const order = icoFirst ? [dflt, ...declared, ...touchUrls] : [...declared, dflt, ...touchUrls];
  const out = [];
  for (const u of order) if (u && !out.includes(u)) out.push(u);
  return out;
}

const IMAGE_KEYS = ["og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"];
const PUBLISHED_KEYS = ["article:published_time", "og:article:published_time", "datepublished", "date", "pubdate", "publishdate",
  "publish-date", "dc.date", "dc.date.issued", "sailthru.date", "parsely-pub-date", "article:modified_time"];
const AUTHOR_KEYS = ["author", "article:author", "dc.creator", "twitter:creator", "parsely-author"];
const META_ARTICLE_TYPES = ["Article", "NewsArticle", "BlogPosting", "Report", "TechArticle", "ScholarlyArticle", "WebPage", "Recipe", "VideoObject"];

function imageOk(url) { const p = splitUrl(url); const path = (p ? p.path : "").toLowerCase(); return isHttp(url) && !path.endsWith(".svg") && !path.endsWith(".ico"); }

/** Metadata of a page: ``{title, description, canonical, lang, siteName, author, published, image, favicon, keywords,
 *  og, twitter, properties}``. Attribute order does not matter, entities are decoded, ``<base href>`` is honoured;
 *  relative URLs are resolved against ``baseUrl``. Missing values are "" (``keywords`` is []). ``og`` keys are
 *  unprefixed (``image``, ``title`` ...). */
export function pageMeta(html, baseUrl = "") {
  baseUrl = str(baseUrl);
  const scan = scanHtml(html);
  const base = baseOf(scan, baseUrl);
  const named = {}, props = {};
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  for (const m of scan.metas) {
    const content = clean(m.content || "");
    if (!content && "charset" in m) continue;
    for (const [attr, bucket] of [["property", props], ["name", named], ["itemprop", named], ["http-equiv", named]]) {
      const key = pyStrip(m[attr] || "").toLowerCase();
      if (key && content && !has(bucket, key)) bucket[key] = content;
    }
  }
  const both = { ...named, ...props };
  const og = {};
  for (const [k, v] of Object.entries(props)) if (k.startsWith("og:")) og[k.slice(3)] = v;
  for (const [k, v] of Object.entries(named)) if (k.startsWith("og:") && !has(og, k.slice(3))) og[k.slice(3)] = v;
  const twitter = {};
  for (const [k, v] of Object.entries(both)) if (k.startsWith("twitter:")) twitter[k.slice(8)] = v;
  const other = {};
  for (const [k, v] of Object.entries(props)) if (!k.startsWith("og:") && !k.startsWith("twitter:")) other[k] = v;
  const first = (...keys) => { for (const k of keys) if (both[k]) return both[k]; return ""; };

  const title = first("og:title", "twitter:title") || clean(scan.title) || clean(scan.h1);
  let description = first("og:description", "twitter:description", "description");
  let canonical = "";
  for (const link of scan.links) {
    if (pySplit((link.rel || "").toLowerCase()).includes("canonical") && link.href) { canonical = absUrl(link.href, base); break; }
  }
  canonical = canonical || absUrl(first("og:url"), base);
  const lang = clean(scan.lang) || first("og:locale", "content-language");
  const siteName = first("og:site_name", "application-name", "apple-mobile-web-app-title");
  let author = first(...AUTHOR_KEYS);
  let published = first(...PUBLISHED_KEYS);
  const [blocks] = blocksFromScripts(scan.scripts);
  if (blocks.length && (!author || !published || !description)) {
    for (const node of jsonldNodes(blocks, META_ARTICLE_TYPES, { skip: [] })) {
      author = author || firstText(node.author);
      published = published || firstText(node.datePublished);
      description = description || firstText(node.description);
      if (author && published) break;
    }
  }
  let image = "";
  for (const key of IMAGE_KEYS) {
    const candidate = absUrl(both[key] || "", base);
    if (candidate && imageOk(candidate)) { image = candidate; break; }
  }
  if (!image) {
    for (const link of scan.links) {
      if (pySplit((link.rel || "").toLowerCase()).includes("image_src")) {
        const candidate = absUrl(link.href || "", base);
        if (candidate && imageOk(candidate)) { image = candidate; break; }
      }
    }
  }
  const icons = faviconCandidates(html, baseUrl, { icoFirst: false });
  const declaredIcon = icons.find((u) => !u.endsWith("/favicon.ico")) || "";
  const favicon = declaredIcon || icons[0] || "";
  const keywords = (named.keywords || "").split(/[,;]/).map(pyStrip).filter(Boolean);
  return { title, description, canonical, lang, siteName, author, published, image, favicon, keywords, og, twitter, properties: other };
}

// ------------------------------------------------------------------------------------------------ feeds
// A small XML reader (no dependencies): elements, attributes, text, CDATA, the five XML entities and numeric
// references. Mismatched tags, undefined entities and junk after the root make the document unreadable (one repair
// attempt: control characters, HTML entities XML does not know, a bare "&"). Documents that declare entities are refused.

const XML_ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
const NAMED_ENTITY = /&([A-Za-z][A-Za-z0-9]*);/g;
const BARE_AMP = /&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)/g;

class XmlError extends Error {}
class XEl {
  constructor(tag, attrs) { this.raw = tag; this.tag = tag.split(":").pop().toLowerCase(); this.attrs = attrs; this.kids = []; }
  get children() { return this.kids.filter((k) => k instanceof XEl); }
  /** All text below this element, in document order (ElementTree's itertext). */
  itertext() { return this.kids.map((k) => (k instanceof XEl ? k.itertext() : k)).join(""); }
  /** Text before the first child element (ElementTree's ``.text``). */
  get text() { const t = []; for (const k of this.kids) { if (k instanceof XEl) break; t.push(k); } return t.join(""); }
  *iter() { yield this; for (const k of this.kids) if (k instanceof XEl) yield* k.iter(); }
}

function xmlDecode(text) {
  return text.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);/g, (whole, body) => {
    if (body[0] === "#") {
      const n = body[1] === "x" ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      if (!(n === 9 || n === 10 || n === 13 || (n >= 32 && n <= 0xd7ff) || (n >= 0xe000 && n <= 0xfffd) || (n >= 0x10000 && n <= 0x10ffff))) throw new XmlError("bad reference");
      return String.fromCodePoint(n);
    }
    if (body in XML_ENTITIES) return XML_ENTITIES[body];
    throw new XmlError("undefined entity " + body);
  });
}

function parseXmlStrict(text) {
  if (CONTROL_CHARS.test(text)) { CONTROL_CHARS.lastIndex = 0; throw new XmlError("invalid character"); }
  CONTROL_CHARS.lastIndex = 0;
  const n = text.length;
  let i = 0, root = null;
  const stack = [];
  const add = (s) => { if (stack.length) stack[stack.length - 1].kids.push(s); else if (pyStrip(s)) throw new XmlError("junk"); };
  while (i < n) {
    const lt = text.indexOf("<", i);
    if (lt < 0) { add(xmlDecode(text.slice(i))); break; }
    if (lt > i) add(xmlDecode(text.slice(i, lt)));
    i = lt;
    if (text.startsWith("<!--", i)) { const e = text.indexOf("-->", i + 4); if (e < 0) throw new XmlError("comment"); i = e + 3; }
    else if (text.startsWith("<![CDATA[", i)) {
      const e = text.indexOf("]]>", i); if (e < 0) throw new XmlError("cdata");
      if (!stack.length) throw new XmlError("junk");
      stack[stack.length - 1].kids.push(text.slice(i + 9, e)); i = e + 3;
    } else if (text.startsWith("<?", i)) { const e = text.indexOf("?>", i); if (e < 0) throw new XmlError("pi"); i = e + 2; }
    else if (text.startsWith("<!", i)) {
      let depth = 0, j = i + 2;
      for (; j < n; j++) { if (text[j] === "[") depth++; else if (text[j] === "]") depth--; else if (text[j] === ">" && depth <= 0) break; }
      if (j >= n) throw new XmlError("doctype");
      i = j + 1;
    } else if (text[i + 1] === "/") {
      const e = text.indexOf(">", i); if (e < 0) throw new XmlError("end tag");
      const name = pyStrip(text.slice(i + 2, e));
      const open = stack.pop();
      if (!open || open.raw !== name) throw new XmlError("mismatched tag");
      i = e + 1;
    } else {
      const m = /^<([^\s/>=]+)/.exec(text.slice(i, i + 300));
      if (!m) throw new XmlError("bad tag");
      let j = i + m[0].length;
      const attrs = {};
      let selfClose = false;
      for (;;) {
        while (j < n && /\s/.test(text[j])) j++;
        if (j >= n) throw new XmlError("unterminated tag");
        if (text[j] === ">") { j++; break; }
        if (text[j] === "/" && text[j + 1] === ">") { selfClose = true; j += 2; break; }
        const am = /^([^\s=/>]+)\s*=\s*/.exec(text.slice(j, j + 300));
        if (!am) throw new XmlError("bad attribute");
        j += am[0].length;
        const q = text[j];
        if (q !== '"' && q !== "'") throw new XmlError("unquoted attribute");
        const e = text.indexOf(q, j + 1); if (e < 0) throw new XmlError("attribute");
        attrs[am[1]] = xmlDecode(text.slice(j + 1, e));
        j = e + 1;
      }
      const el = new XEl(m[1], attrs);
      if (stack.length) stack[stack.length - 1].kids.push(el);
      else if (root) throw new XmlError("second root");
      else root = el;
      if (!selfClose) stack.push(el);
      i = j;
    }
  }
  if (stack.length || !root) throw new XmlError("unclosed");
  return root;
}

function parseXml(xml) {
  if (xml === null || xml === undefined) return null;
  let text = typeof xml === "string" ? xml : Buffer.isBuffer(xml) || xml instanceof Uint8Array ? Buffer.from(xml).toString("utf8") : String(xml);
  text = pyStrip(text.replace(/^﻿+/, ""));
  if (!text || /<!ENTITY/i.test(text)) return null;
  try { return parseXmlStrict(text); } catch (e) {
    if (!(e instanceof XmlError)) throw e;
  }
  const repaired = text.replace(CONTROL_CHARS, " ")
    .replace(NAMED_ENTITY, (whole, name) => {
      if (name in XML_ENTITIES) return whole;
      const ch = unescapeHtml(whole);
      return ch === whole ? whole : ch.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    })
    .replace(BARE_AMP, "&amp;");
  try { return parseXmlStrict(repaired); } catch (e) { if (e instanceof XmlError) return null; throw e; }
}

const plain = (text) => pyStrip(unescapeHtml(str(text).replace(/<[^>]+>/g, " ")).replace(/\xa0/g, " ").replace(WS_RUN, " "));
function xText(el, ...names) {
  for (const name of names) {
    for (const child of el.children) {
      if (child.tag === name) { const v = pyStrip(child.itertext()); if (v) return v; }
    }
  }
  return "";
}
function atomLink(el, base) {
  let chosen = "";
  for (const child of el.children) {
    if (child.tag !== "link") continue;
    const href = pyStrip(child.attrs.href || "");
    if (!href) continue;
    const rel = child.attrs.rel || "alternate";
    if (rel === "alternate") return base ? urljoin(base, href) : href;
    chosen = chosen || (base ? urljoin(base, href) : href);
  }
  return chosen;
}

const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
const ZONES = { ut: 0, utc: 0, gmt: 0, z: 0, est: -300, edt: -240, cst: -360, cdt: -300, mst: -420, mdt: -360, pst: -480, pdt: -420 };
const ISO_DATE = /^(\d{4})-?(\d{2})-?(\d{2})(?:[T ](\d{2}):?(\d{2})(?::?(\d{2})(?:[.,]\d+)?)?)?[ ]*(Z|[+-]\d{2}(?::?\d{2})?)?$/i;
const RFC_DATE = /^(?:[A-Za-z]{3,9},?\s+)?(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{2,4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([A-Za-z]{1,5}|[+-]\d{4})?$/;
const pad = (v, n = 2) => String(v).padStart(n, "0");

function isoFrom(y, mo, d, h, mi, s, offMin) {
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return "";
  const t = Date.UTC(y, mo - 1, d, h, mi, s) - offMin * 60000;
  const dt = new Date(t);
  if (isNaN(dt)) return "";
  const check = new Date(Date.UTC(y, mo - 1, d));
  if (check.getUTCDate() !== d) return "";                          // 31 February
  return `${pad(dt.getUTCFullYear(), 4)}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}T${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}:${pad(dt.getUTCSeconds())}Z`;
}

/** An RFC 822 or ISO 8601 date as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC; no zone means UTC); "" when unreadable. */
export function toIsoUtc(value) {
  const text = pyStrip(str(value));
  if (!text) return "";
  let m = ISO_DATE.exec(text);
  if (m) {
    let off = 0;
    const z = m[7];
    if (z && z.toUpperCase() !== "Z") { const sign = z[0] === "-" ? -1 : 1; const digits = z.slice(1).replace(":", ""); off = sign * (parseInt(digits.slice(0, 2), 10) * 60 + (digits.length > 2 ? parseInt(digits.slice(2), 10) : 0)); }
    return isoFrom(+m[1], +m[2], +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0), off);
  }
  m = RFC_DATE.exec(text);
  if (m) {
    const mo = MONTHS[m[2].slice(0, 3).toLowerCase()];
    if (!mo) return "";
    let y = +m[3];
    if (m[3].length <= 2) y += y > 68 ? 1900 : 2000;
    let off = 0;
    const z = m[7];
    if (z) {
      if (/^[+-]\d{4}$/.test(z)) off = (z[0] === "-" ? -1 : 1) * (parseInt(z.slice(1, 3), 10) * 60 + parseInt(z.slice(3), 10));
      else off = ZONES[z.toLowerCase()] || 0;
    }
    return isoFrom(y, mo, +m[1], +m[4], +m[5], +(m[6] || 0), off);
  }
  return "";
}

function feedItem(el, atom, base) {
  let link;
  if (atom) link = atomLink(el, base);
  else {
    link = xText(el, "link");
    if (!link) {
      const guid = el.children.find((c) => c.raw === "guid");
      if (guid && pyStrip(guid.text).toLowerCase().startsWith("http") && (guid.attrs.isPermaLink || "true") !== "false") link = pyStrip(guid.text);
      else link = atomLink(el, base);
    }
    link = base && link ? urljoin(base, link) : link;
  }
  const title = plain(xText(el, "title"));
  const ident = xText(el, "guid", "id") || link || title;
  const summarySrc = xText(el, "summary", "description") || xText(el, "encoded", "content");
  const content = xText(el, "encoded", "content") || (!atom ? xText(el, "description") : "");
  const published = toIsoUtc(xText(el, "pubdate", "published", "date", "issued"));
  const updated = toIsoUtc(xText(el, "updated", "modified"));
  let author = "";
  for (const child of el.children) {
    if (child.tag === "author" || child.tag === "creator") { author = xText(child, "name") || pyStrip(child.itertext()); if (author) break; }
  }
  author = plain(author) || plain(xText(el, "author", "creator"));
  if (!(ident || link)) return null;
  return { id: ident, title: clipText(title, 300), link, published, updated, date: published || updated,
    summary: clipText(plain(summarySrc), 400), content: cpSlice(content, 50000), author: clipText(author, 120) };
}

/** ``{format, title, link, description, items}`` for RSS 2.0, RSS 1.0 (RDF) and Atom, else ``null``. Items:
 *  ``{id, title, link, published, updated, date, summary, content, author}`` (dates ISO 8601 UTC, "" when absent). */
export function parseFeed(xml, baseUrl = "", o = {}) {
  if (baseUrl && typeof baseUrl === "object") { o = baseUrl; baseUrl = ""; }
  baseUrl = str(baseUrl);
  const maxItems = typeof o === "number" ? o : opt(o, "maxItems", 200);
  const root = parseXml(xml);
  if (!root) return null;
  const kind = root.tag;
  let fmt, atom = false, container, link;
  if (kind === "feed") { fmt = "atom"; atom = true; container = root; link = atomLink(root, baseUrl); }
  else if (kind === "rss" || kind === "channel") {
    fmt = "rss";
    container = kind === "rss" ? (root.children.find((c) => c.raw === "channel") || root) : root;
    link = xText(container, "link");
  } else if (kind === "rdf") {
    fmt = "rdf";
    container = root.children.find((c) => c.tag === "channel") || root;
    link = xText(container, "link");
  } else return null;
  const items = [];
  for (const el of root.iter()) {
    if (el.tag === "item" || el.tag === "entry") {
      const parsed = feedItem(el, atom || el.tag === "entry", baseUrl);
      if (parsed) { items.push(parsed); if (items.length >= maxItems) break; }
    }
  }
  return { format: fmt, title: plain(xText(container, "title")), link: baseUrl && link ? urljoin(baseUrl, link) : link,
    description: clipText(plain(xText(container, "description", "subtitle")), 400), items };
}

const GITHUB_RESERVED = new Set(`orgs users topics marketplace sponsors settings features pricing about search explore
notifications pulls issues gist new login join collections trending enterprise customer-stories readme apps`.split(/\s+/));

/** The Atom feed that tracks a GitHub repository: ``{repo, url, what, name}`` or ``null``. A path hint in the URL wins over ``what``. */
export function githubFeed(url, what = "releases") {
  if (typeof what === "object" && what) what = opt(what, "what", "releases");
  const p = splitUrl(pyStrip(str(url)));
  if (!p || (p.scheme !== "http" && p.scheme !== "https") || !["github.com", "www.github.com"].includes(p.host.toLowerCase())) return null;
  const segs = p.path.split("/").filter(Boolean);
  if (segs.length < 2 || GITHUB_RESERVED.has(segs[0].toLowerCase())) return null;
  const owner = segs[0], repo = segs[1].replace(/\.git$/, "");
  if (!repo) return null;
  const rest = segs.slice(2);
  let kind = ["releases", "tags", "commits"].includes(what) ? what : "releases";
  let branch = "";
  if (rest.length && ["commits", "tags", "releases"].includes(rest[0])) {
    kind = rest[0];
    if (kind === "commits" && rest.length > 1) branch = rest.slice(1).join("/").replace(/\.atom$/, "");
  } else if (rest.length && rest[0] === "tree" && kind === "commits" && rest.length > 1) branch = rest.slice(1).join("/");
  return { repo: `${owner}/${repo}`, url: `https://github.com/${owner}/${repo}/${kind}` + (branch ? `/${branch}` : "") + ".atom", what: kind, name: `${owner}/${repo} ${kind}` };
}

// ------------------------------------------------------------------------------------------------ watch

export const MAX_STORED_TEXT = 20000, MAX_SEEN = 400, MAX_FEED_FINDINGS = 20;
const MAX_ADDED_LINES = 8, MAX_LINE_CHARS = 200;
const clipLine = (text, n) => { const t = pySplit(text || "").join(" "); const c = cp(t); return c.length <= n ? t : c.slice(0, n - 1).join("").replace(new RegExp(`[${WS}]+$`), "") + "…"; };
const field = (o, camel, dflt = "") => { const v = pick(o, camel); return v === undefined || v === null ? dflt : v; };

/** ``[added, removed]`` lines of two texts in document order, compared on their normalised form. */
export function diffLines(oldText, newText) {
  const oldLines = splitLines(oldText).filter((l) => normaliseForHash(l));
  const newLines = splitLines(newText).filter((l) => normaliseForHash(l));
  const oldSet = new Set(oldLines.map(normaliseForHash)), newSet = new Set(newLines.map(normaliseForHash));
  return [newLines.filter((l) => !oldSet.has(normaliseForHash(l))), oldLines.filter((l) => !newSet.has(normaliseForHash(l)))];
}
export function diffSummary(added, removed) {
  const out = added.slice(0, MAX_ADDED_LINES).map((l) => `+ ${clipLine(l, MAX_LINE_CHARS)}`);
  if (added.length > MAX_ADDED_LINES) out.push(`+ … ${added.length - MAX_ADDED_LINES} more lines`);
  if (removed.length) out.push(`- ${removed.length} line(s) removed: ${clipLine(removed[0], 120)}`);
  return out.join("\n");
}

/** Decide whether a fetched page changed. ``fetch`` is a webGet result or an object with the same fields (camelCase
 *  or snake_case); ``prev`` is the state returned last time. Returns ``[finding | null, state]`` where state is
 *  ``{hash, text, etag, lastModified, error}``. A blocked or unusable answer never counts as a change and never
 *  replaces the stored state; the first check is a baseline. */
export function checkPage(fetch, prev = null) {
  const p = prev || {};
  const state = { hash: str(field(p, "hash")), text: str(field(p, "text")), etag: str(field(p, "etag")), lastModified: str(field(p, "lastModified")), error: "" };
  const url = str(field(fetch, "finalUrl") || field(fetch, "url"));
  if (field(fetch, "notModified", false) || Number(field(fetch, "status", 0) || 0) === 304) return [null, state];
  const textHtml = str(field(fetch, "text"));
  let reason = field(fetch, "blocked", false) ? str(field(fetch, "blockReason")) : "";
  if (!reason && textHtml) {
    reason = detectBlock(Number(field(fetch, "status", 200) || 200), textHtml, field(fetch, "headers", {}) || {}, url);
    if (reason === "http_5xx") reason = "";
  }
  if (reason) { state.error = `blocked: ${blockHint(reason)}`; return [null, state]; }
  if (!field(fetch, "ok", false)) {
    state.error = str(field(fetch, "error") || (field(fetch, "status") ? `HTTP ${field(fetch, "status")}` : "no answer"));
    return [null, state];
  }
  const { title, text } = htmlToText(textHtml);
  const problem = quality(text);
  if (problem) { state.error = `low quality page (${problem}); no comparison made`; return [null, state]; }
  const newHash = contentHash(text);
  const stored = cpSlice(text, MAX_STORED_TEXT);
  const oldHash = state.hash, oldText = state.text;
  Object.assign(state, { hash: newHash, text: stored, etag: str(field(fetch, "etag")), lastModified: str(field(fetch, "lastModified")) });
  if (!oldHash || newHash === oldHash) return [null, state];
  const [added, removed] = diffLines(oldText, stored);
  if (!added.length && !removed.length) return [null, state];
  const finding = {
    kind: "page_change", url, title: title || url,
    snippet: clipLine(added.slice(0, 3).join(" ") || "content removed", 300),
    added: added.slice(0, 50), removed: removed.slice(0, 50), summary: diffSummary(added, removed),
    contentHash: sha256(url + "\n" + (added.length ? added : removed).map(normaliseForHash).join("\n")).slice(0, 32),
  };
  return [finding, state];
}

const itemKey = (item) => str(item.id || item.link || item.title || "");

/** New items of a parsed feed: ``[newItems, seen]`` (at most 20 new, at most 400 remembered). ``baseline`` reports nothing. */
export function checkFeed(feed, seen = null, baseline = false) {
  if (baseline && typeof baseline === "object") baseline = opt(baseline, "baseline", false);
  const items = Array.isArray(feed) ? feed : (feed && feed.items) || [];
  const old = [...(seen || [])];
  if (!items.length) return [[], old];
  const keys = items.map(itemKey);
  const seenSet = new Set(old), current = new Set(keys);
  const merged = [...keys.filter(Boolean), ...old.filter((k) => !current.has(k))].slice(0, MAX_SEEN);
  if (baseline) return [[], merged];
  const fresh = items.map((it, i) => [it, keys[i]]).filter(([, k]) => k && !seenSet.has(k)).map(([it, k]) => ({ ...it, key: k }));
  return [fresh.slice(0, MAX_FEED_FINDINGS), merged];
}

// ------------------------------------------------------------------------------------------------ robots.txt

function patternRegex(pattern) {
  const anchored = pattern.endsWith("$");
  const body = anchored ? pattern.slice(0, -1) : pattern;
  let rx = "";
  for (const ch of body) rx += ch === "*" ? ".*" : escRe(ch);
  return new RegExp("^" + rx + (anchored ? "$" : ""), "s");
}
const FLOAT_RE = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;

/** A parsed robots.txt (RFC 9309: most specific group, ``*`` and ``$`` wildcards, longest match wins, Allow wins a tie). */
export class RobotsRules {
  constructor(text = "") {
    this.groups = [];
    this.sitemaps = [];
    let agents = [], rules = [], delay = null, inRules = false;
    const close = () => { if (agents.length) this.groups.push([agents, rules, delay]); agents = []; rules = []; delay = null; inRules = false; };
    for (const raw of splitLines(str(text).replace(/^﻿+/, ""))) {
      const hash = raw.indexOf("#");
      const line = pyStrip(hash < 0 ? raw : raw.slice(0, hash));
      const colon = line.indexOf(":");
      if (colon < 0) continue;
      const name = pyStrip(line.slice(0, colon)).toLowerCase(), value = pyStrip(line.slice(colon + 1));
      if (name === "user-agent") {
        if (inRules) close();
        if (value) agents.push(value.toLowerCase());
      } else if (name === "allow" || name === "disallow") {
        if (!agents.length) continue;
        inRules = true;
        if (value) rules.push([name === "allow", value, patternRegex(value)]);
      } else if (name === "crawl-delay") {
        if (agents.length) { inRules = true; if (FLOAT_RE.test(value)) delay = parseFloat(value); }
      } else if (name === "sitemap" && value) this.sitemaps.push(value);
    }
    close();
  }
  _select(agent) {
    const name = str(agent).toLowerCase();
    let best = -1, chosen = [];
    for (const group of this.groups) {
      for (const token of group[0]) {
        if (token !== "*" && name.includes(token) && token.length >= best) {
          if (token.length > best) { chosen = []; best = token.length; }
          chosen.push(group);
          break;
        }
      }
    }
    if (!chosen.length) chosen = this.groups.filter((g) => g[0].includes("*"));
    const rules = chosen.flatMap((g) => g[1]);
    const delays = chosen.map((g) => g[2]).filter((d) => d !== null);
    return [rules, delays.length ? delays[0] : null];
  }
  allowed(agent, path) {
    path = str(path) || "/";
    if (!path.startsWith("/")) path = "/" + path;
    if (path.split("?")[0] === "/robots.txt") return true;
    const [rules] = this._select(agent);
    let best = null;
    const candidates = new Set([path, unquote(path)]);
    for (const [allow, pattern, rx] of rules) {
      if ([...candidates].some((c) => rx.test(c))) {
        const score = [pattern.length, allow ? 1 : 0];
        if (!best || score[0] > best[0] || (score[0] === best[0] && score[1] > best[1])) best = score;
      }
    }
    return best === null ? true : best[1] === 1;
  }
  crawlDelay(agent) { return this._select(agent)[1]; }
}

/** May ``agent`` fetch ``path`` according to the robots.txt ``text``? (The pure rule check; no network.) */
export function robotsRulesAllowed(text, agent, path) { return new RobotsRules(text).allowed(agent, path); }

/** The ``Crawl-delay`` (seconds) a robots.txt asks of ``agent``, or ``null``. */
export function robotsCrawlDelay(text, agent) { return new RobotsRules(text).crawlDelay(agent); }

// ------------------------------------------------------------------------------------------------ network
// webGet and politeJson use node:http(s) with the policy check in front of every hop and the connection pinned to the
// addresses that were checked (the TLS server name and Host header keep the original name).

export const DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36";
export const MAX_BODY_BYTES = 3 * 1024 * 1024;
export const MAX_REDIRECTS = 5;
export const ERROR_KINDS = Object.freeze(["dns", "tls", "timeout", "refused", "reset", "network", "policy", "robots", "offline", "blocked", "http", "content", "redirects"]);
const ACCEPT = {
  html: "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
  json: "application/json, text/plain, */*",
  any: "*/*",
};
const SENSITIVE_HEADERS = new Set(["authorization", "cookie", "proxy-authorization", "x-api-key", "x-auth-token"]);
const REDIRECT_STATUS = new Set([301, 302, 303, 307, 308]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const META_CHARSET = /<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)/i;
const XML_ENCODING = /^\s*<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_\-:.]+)["']/i;

const CP1252_HIGH = [0x20ac, 0, 0x201a, 0x192, 0x201e, 0x2026, 0x2020, 0x2021, 0x2c6, 0x2030, 0x160, 0x2039, 0x152, 0, 0x17d, 0, 0,
  0x2018, 0x2019, 0x201c, 0x201d, 0x2022, 0x2013, 0x2014, 0x2dc, 0x2122, 0x161, 0x203a, 0x153, 0, 0x17e, 0x178];
/** windows-1252 (Node's TextDecoder treats the label as ISO-8859-1). Undefined bytes: ``fatal`` throws, else U+FFFD. */
function decodeCp1252(buf, fatal) {
  let out = "";
  for (const b of buf) {
    if (b >= 0x80 && b <= 0x9f) {
      const c = CP1252_HIGH[b - 0x80];
      if (!c) { if (fatal) throw new RangeError("undefined cp1252 byte"); out += "\ufffd"; } else out += String.fromCharCode(c);
    } else out += String.fromCharCode(b);
  }
  return out;
}
const CP1252_LABELS = new Set(["windows-1252", "cp1252", "x-cp1252"]);
const LATIN1_LABELS = new Set(["iso-8859-1", "latin1", "latin-1", "l1", "iso8859-1", "iso_8859-1", "us-ascii", "ascii"]);
function decodeNamed(buf, name, fatal) {
  const label = name.toLowerCase();
  if (CP1252_LABELS.has(label)) return decodeCp1252(buf, fatal);
  if (LATIN1_LABELS.has(label)) return buf.toString("latin1");
  return new TextDecoder(label, { fatal }).decode(buf);
}

/** Bytes to text, never throwing: a BOM wins, then the ``charset`` of the Content-Type, then ``<meta charset>`` or the
 *  XML prolog, then strict UTF-8, finally windows-1252 (the usual mislabelled legacy page). */
export function decodeBody(body, contentType = "") {
  const buf = Buffer.from(body || []);
  if (buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) return new TextDecoder("utf-8").decode(buf.subarray(3));
  if (buf.length >= 2 && ((buf[0] === 0xff && buf[1] === 0xfe) || (buf[0] === 0xfe && buf[1] === 0xff))) {
    try { return new TextDecoder(buf[0] === 0xff ? "utf-16le" : "utf-16be", { fatal: true }).decode(buf); } catch { /* fall through */ }
  }
  const ctype = str(contentType).toLowerCase();
  const names = [];
  const m = /charset\s*=\s*['"]?([\w\-:.]+)/.exec(ctype);
  if (m) names.push(m[1]);
  if (!ctype.includes("json")) {
    const head = buf.subarray(0, 4096).toString("latin1");
    const sniff = ((ctype.includes("html") || !ctype) ? META_CHARSET.exec(head) : null) || XML_ENCODING.exec(buf.subarray(0, 200).toString("latin1"));
    if (sniff) names.push(sniff[1]);
  }
  for (const name of names) {
    try { return decodeNamed(buf, name, true); } catch {
      if (["utf-8", "utf8"].includes(name.toLowerCase())) return new TextDecoder("utf-8").decode(buf);
    }
  }
  try { return new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch { return decodeCp1252(buf, false); }
}

/** An error from node:net / node:tls / node:http as ``[kind, detail]`` (dns tls timeout refused reset network). */
export function classifyError(error, host = "", timeoutMs = 0) {
  const code = (error && error.code) || "";
  const msg = (error && error.message) || String(error);
  if (error && (error.timedOut || error.name === "TimeoutError" || ["ETIMEDOUT", "ESOCKETTIMEDOUT", "UND_ERR_CONNECT_TIMEOUT"].includes(code))) {
    return ["timeout", timeoutMs ? `Timeout: no answer within ${Math.round(timeoutMs / 1000)} s` : "Timeout: no answer"];
  }
  if (["ENOTFOUND", "EAI_AGAIN", "EAI_FAIL", "EAI_NODATA"].includes(code)) return ["dns", `DNS failure: ${host || "the host"} does not resolve`];
  if (/^(CERT_|ERR_TLS|ERR_SSL|UNABLE_TO_|DEPTH_ZERO|SELF_SIGNED|HOSTNAME_MISMATCH|ERR_CERT)/.test(code) || /certificate|tls|ssl/i.test(msg)) return ["tls", `TLS error: ${msg}`];
  if (code === "ECONNREFUSED") return ["refused", "Connection refused: nothing accepts connections on that port"];
  if (["ECONNRESET", "EPIPE", "ECONNABORTED", "UND_ERR_SOCKET"].includes(code) || /socket hang up/i.test(msg)) return ["reset", "Connection reset by the server"];
  return ["network", `${(error && error.name) || "Error"}: ${msg}`.slice(0, 300)];
}

// One queue per host: requests run one after the other with at least ``minMs`` between the end of one and the start of the next.
const gates = new Map();
function enqueue(host, minMs, job) {
  const g = gates.get(host) || { tail: Promise.resolve(), last: 0 };
  gates.set(host, g);
  const run = g.tail.then(async () => {
    const wait = g.last + minMs - Date.now();
    if (minMs > 0 && wait > 0) await sleep(wait);
    try { return await job(); } finally { g.last = Date.now(); }
  });
  g.tail = run.catch(() => {});
  return run;
}

/** One HTTP request, no redirects, body capped at ``maxBytes`` (gzip, deflate and brotli are decoded with the cap
 *  applied to the decoded size, so a compression bomb never inflates past it). */
function requestOnce(urlStr, { method = "GET", headers = {}, addrs, timeoutMs = 20000, maxBytes = MAX_BODY_BYTES, rejectUnauthorized = true }) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(urlStr); } catch (e) { reject(e); return; }
    const mod = u.protocol === "https:" ? https : http;
    let settled = false, timer = null, req = null;
    const done = (fn, v) => { if (settled) return; settled = true; clearTimeout(timer); fn(v); };
    const options = {
      method, hostname: u.hostname.replace(/^\[|\]$/g, ""), port: u.port || undefined, path: u.pathname + u.search, headers, agent: false,
      lookup: addrs ? pinnedLookup(addrs) : undefined, rejectUnauthorized,
    };
    req = mod.request(options, (res) => {
      let stream = res;
      const enc = str(res.headers["content-encoding"]).toLowerCase().trim();
      if (enc === "gzip" || enc === "x-gzip") stream = res.pipe(zlib.createGunzip());
      else if (enc === "deflate") stream = res.pipe(zlib.createInflate());
      else if (enc === "br") stream = res.pipe(zlib.createBrotliDecompress());
      if (stream !== res) stream.on("error", (e) => finish(e));
      const chunks = [];
      let size = 0, truncated = false, ended = false;
      const finish = (err) => {
        if (ended) return;
        ended = true;
        if (err && !truncated) { done(reject, err); return; }
        done(resolve, { status: res.statusCode || 0, headers: res.headers, body: Buffer.concat(chunks), truncated });
      };
      stream.on("data", (c) => {
        if (ended) return;
        const room = maxBytes - size;
        if (c.length > room) {
          chunks.push(c.subarray(0, room));
          size = maxBytes;
          truncated = true;
          finish();
          res.destroy();
          if (stream !== res) stream.destroy();
        } else { chunks.push(c); size += c.length; }
      });
      stream.on("end", () => finish());
      res.on("error", (e) => finish(e));
      res.on("aborted", () => finish(Object.assign(new Error("connection reset"), { code: "ECONNRESET" })));
    });
    req.on("error", (e) => done(reject, e));
    timer = setTimeout(() => { const e = Object.assign(new Error("timeout"), { timedOut: true }); done(reject, e); req.destroy(); }, timeoutMs);
    req.end();
  });
}

function flatHeaders(raw) {
  const out = {};
  for (const [k, v] of Object.entries(raw || {})) out[k.toLowerCase()] = Array.isArray(v) ? v.join(", ") : String(v);
  return out;
}
function retryAfterMs(value) {
  const v = pyStrip(str(value));
  if (!v) return null;
  if (/^\d+$/.test(v)) return parseInt(v, 10) * 1000;
  const t = Date.parse(v);
  return isNaN(t) ? null : Math.max(0, t - Date.now());
}
function newResult(url) {
  return { url, finalUrl: "", status: 0, headers: {}, contentType: "", text: "", body: null, etag: "", lastModified: "", ok: false,
    notModified: false, truncated: false, fromCache: false, stale: false, elapsedMs: 0, fetchedAt: Date.now() / 1000, tier: "http",
    blocked: false, blockReason: "", error: "", errorKind: "", note: "", redirects: [] };
}
/** Fold a ``detectBlock`` verdict into a result (the JS twin of ``apply_block``). */
export function applyBlock(fr, reason) {
  fr.blockReason = reason;
  fr.blocked = !!reason && reason !== "http_5xx";
  const good = fr.status >= 200 && fr.status < 400;
  fr.ok = !!(good && !reason && fr.text);
  if (reason) {
    fr.error = fr.blocked ? `blocked: ${blockHint(reason)}` : `${blockHint(reason)} (HTTP ${fr.status})`;
    fr.errorKind = fr.blocked ? "blocked" : "http";
  } else if (!good) {
    fr.error = fr.error || `HTTP ${fr.status}`;
    fr.errorKind = fr.errorKind || "http";
  } else if (!fr.text) fr.error = fr.error || "empty response";
  return fr;
}

const TEXTISH = /^(?:text\/|application\/(?:xhtml\+xml|xml|json|ld\+json|feed\+json|rss\+xml|atom\+xml|javascript)|[^;]*\+(?:xml|json))/i;

/** One logical GET with manual, policy-checked, pinned redirects and a body cap; never throws. Options: profile
 *  (public | operator_local | internal), headers, userAgent, accept ("html" | "json" | "any"), timeoutMs (20000),
 *  maxBytes (3 MB), maxRedirects (5), retries (1), backoffMs (400), minIntervalMs (0, per host), etag, lastModified,
 *  respectRobots (false), agent (robots name), lookup / hosts (resolver, test seam), rejectUnauthorized (true).
 *  Result: ``{url, finalUrl, status, headers, contentType, text, body, etag, lastModified, ok, notModified, truncated,
 *  elapsedMs, blocked, blockReason, error, errorKind, note, redirects}``. */
export async function webGet(url, o = {}) {
  const fr = newResult(str(url));
  const began = Date.now();
  const profile = opt(o, "profile", PUBLIC);
  const accept = opt(o, "accept", "html");
  const timeoutMs = opt(o, "timeoutMs", 20000), maxBytes = opt(o, "maxBytes", MAX_BODY_BYTES);
  const maxRedirects = opt(o, "maxRedirects", MAX_REDIRECTS), retries = opt(o, "retries", 1), backoffMs = opt(o, "backoffMs", 400);
  const minIntervalMs = opt(o, "minIntervalMs", 0);
  const policy = { profile, lookup: opt(o, "lookup", null), hosts: opt(o, "hosts", null) };
  const headers = { "user-agent": opt(o, "userAgent", DEFAULT_USER_AGENT), accept: ACCEPT[accept] || ACCEPT.html, "accept-encoding": "gzip, deflate, br" };
  for (const [k, v] of Object.entries(opt(o, "headers", {}) || {})) headers[k.toLowerCase()] = String(v);
  const etag = opt(o, "etag", ""), lastModified = opt(o, "lastModified", "");
  if (etag) headers["if-none-match"] = etag;
  if (lastModified) headers["if-modified-since"] = lastModified;
  let current = fr.url;
  try {
    for (let hop = 0; hop <= maxRedirects; hop++) {
      const ev = await evaluateUrl(current, policy);
      if (ev.reason) {
        fr.error = (hop ? "redirect refused: " : "") + ev.reason;
        fr.errorKind = ev.kind;
        fr.finalUrl = current;
        return fr;
      }
      if (opt(o, "respectRobots", false)) {
        const verdict = await robotsAllowed(current, { agent: opt(o, "agent", DEFAULT_ROBOTS_AGENT), ...policy });
        if (!verdict.allowed) { fr.error = "disallowed by robots.txt"; fr.errorKind = "robots"; fr.finalUrl = current; return fr; }
        if (verdict.note) fr.note = verdict.note;
      }
      const host = hostOf(current);
      let r, attempt = 0;
      for (;;) {
        try {
          r = await enqueue(host, minIntervalMs, () => requestOnce(current, { headers, addrs: ev.addrs, timeoutMs, maxBytes, rejectUnauthorized: opt(o, "rejectUnauthorized", true) }));
        } catch (error) {
          const [kind, detail] = classifyError(error, host, timeoutMs);
          if (attempt < retries && ["timeout", "refused", "reset", "network"].includes(kind)) { attempt++; await sleep(backoffMs * 2 ** (attempt - 1)); continue; }
          fr.error = detail; fr.errorKind = kind; fr.finalUrl = current;
          return fr;
        }
        if (r.status >= 500 && !r.headers["retry-after"] && attempt < retries) { attempt++; await sleep(backoffMs * 2 ** (attempt - 1)); continue; }
        break;
      }
      const location = r.headers.location;
      if (REDIRECT_STATUS.has(r.status) && location) {
        const target = urljoin(current, pyStrip(location));
        fr.redirects.push(target);
        if (hostOf(target) !== hostOf(current)) for (const name of Object.keys(headers)) if (SENSITIVE_HEADERS.has(name)) delete headers[name];
        current = target;
        continue;
      }
      return finishResult(fr, r, current, accept, began);
    }
    fr.error = `too many redirects (more than ${maxRedirects})`;
    fr.errorKind = "redirects";
    fr.finalUrl = current;
    return fr;
  } catch (error) {
    fr.error = `${(error && error.name) || "Error"}: ${(error && error.message) || error}`.slice(0, 300);
    fr.errorKind = "network";
    return fr;
  } finally {
    fr.elapsedMs = Date.now() - began;
  }
}

function finishResult(fr, r, current, accept, began) {
  fr.finalUrl = current;
  fr.status = r.status;
  fr.headers = flatHeaders(r.headers);
  fr.contentType = fr.headers["content-type"] || "";
  fr.etag = fr.headers.etag || "";
  fr.lastModified = fr.headers["last-modified"] || "";
  fr.truncated = r.truncated;
  fr.elapsedMs = Date.now() - began;
  if (r.status === 304) { fr.notModified = true; fr.ok = true; fr.error = fr.errorKind = ""; return fr; }
  const textual = !fr.contentType || TEXTISH.test(fr.contentType);
  if (accept === "any") fr.body = r.body;
  if (textual || accept !== "any") {
    if (!textual && r.body.length && accept === "html") { fr.error = `unexpected content type ${fr.contentType}`; fr.errorKind = "content"; return fr; }
    fr.text = decodeBody(r.body, fr.contentType);
  }
  let reason = fr.text || fr.status ? detectBlock(fr.status, fr.text, fr.headers, fr.finalUrl) : "";
  if (accept === "json" && (reason === "http_403" || reason === "login")) reason = "";
  applyBlock(fr, reason);
  if (accept === "any" && fr.status >= 200 && fr.status < 400 && !reason && fr.body && fr.body.length) { fr.ok = true; fr.error = ""; fr.errorKind = ""; }
  return fr;
}

// ------------------------------------------------------------------------------------------------ robots.txt, networked

export const DEFAULT_ROBOTS_AGENT = "HoardLink";
export const ROBOTS_TTL_MS = 24 * 3600 * 1000;
export const UNREACHABLE_RETRY_MS = 10 * 60 * 1000;

function originOf(url) {
  const p = splitUrl(prepare(url));
  if (!p) return "";
  const host = cleanHost(p.host);
  if (!host) return "";
  const port = p.port && !["80", "443"].includes(String(parseInt(p.port, 10))) ? ":" + parseInt(p.port, 10) : "";
  return `${p.scheme || "https"}://${host.includes(":") ? `[${host}]` : host}${port}`;
}

/** robots.txt per origin with a time to live; ``fetchText(url) -> {status, text, error}`` and ``clock() -> ms`` are injectable. */
export class RobotsCache {
  constructor(o = {}) {
    this.fetchText = opt(o, "fetchText", null);
    this.clock = opt(o, "clock", Date.now);
    this.ttlMs = opt(o, "ttlMs", ROBOTS_TTL_MS);
    this.unreachableRetryMs = opt(o, "unreachableRetryMs", UNREACHABLE_RETRY_MS);
    this.agent = opt(o, "agent", DEFAULT_ROBOTS_AGENT);
    this.policy = { profile: opt(o, "profile", PUBLIC), lookup: opt(o, "lookup", null), hosts: opt(o, "hosts", null) };
    this.parsed = new Map();
    this.unreachable = new Map();
  }
  async _fetch(robotsUrl) {
    if (this.fetchText) return await this.fetchText(robotsUrl);
    const r = await webGet(robotsUrl, { ...this.policy, accept: "any", maxBytes: 512 * 1024, timeoutMs: 8000, retries: 0 });
    return { status: r.status, text: r.status === 200 ? r.text : "", error: r.error };
  }
  /** ``{allowed, note}``; ``note`` is non-empty when robots.txt could not be read (the fetch is allowed). */
  async check(url) {
    const origin = originOf(url);
    if (!origin) return { allowed: true, note: "" };
    const now = this.clock();
    let entry = this.parsed.get(origin);
    let rules = null, note = "";
    if (entry && now - entry.at < this.ttlMs) rules = entry.rules;
    else if ((this.unreachable.get(origin) || 0) > now) return { allowed: true, note: "robots.txt unreachable (assumed allowed)" };
    else {
      const got = await this._fetch(`${origin}/robots.txt`);
      const status = Number(got.status) || 0;
      if (status === 200 && got.text !== null && got.text !== undefined) {
        rules = pyStrip(got.text) ? new RobotsRules(got.text) : null;
        this.parsed.set(origin, { at: now, rules });
      } else if (status >= 400 && status < 500 && status !== 429) this.parsed.set(origin, { at: now, rules: null });
      else {
        this.unreachable.set(origin, now + this.unreachableRetryMs);
        return { allowed: true, note: `robots.txt unreachable (${got.error || "HTTP " + status}); assumed allowed` };
      }
    }
    if (!rules) return { allowed: true, note };
    const p = splitUrl(prepare(url));
    const path = (p.path || "/") + (p.query ? "?" + p.query : "");
    return { allowed: rules.allowed(this.agent, path), note: "" };
  }
  forget(url) { const o = originOf(url); this.parsed.delete(o); this.unreachable.delete(o); }
}

const sharedRobots = new Map();
/** May ``agent`` fetch ``url``? ``{allowed, note}``. Options: agent, profile, lookup / hosts, fetchText, clock, ttlMs, cache
 *  (a RobotsCache; one shared cache per agent otherwise). */
export async function robotsAllowed(url, o = {}) {
  const agent = opt(o, "agent", DEFAULT_ROBOTS_AGENT);
  let cache = opt(o, "cache", null);
  if (!cache) {
    const key = agent + "\n" + (opt(o, "fetchText", null) ? "custom" : "default");
    if (opt(o, "fetchText", null) || !sharedRobots.has(key)) { cache = new RobotsCache({ ...o, agent }); if (!opt(o, "fetchText", null)) sharedRobots.set(key, cache); }
    else cache = sharedRobots.get(key);
  }
  return cache.check(url);
}

// ------------------------------------------------------------------------------------------------ politeJson

/** GET a JSON API politely: one request at a time per host, at least ``minIntervalMs`` (1000) apart, no redirects, a body
 *  cap (3 MB) and a timeout (10 s). Never throws: ``{ok: true, status, data, headers}`` or ``{ok: false, code, error,
 *  status}`` with ``code`` one of ``policy http timeout network too-large not-json``. */
export async function politeJson(url, o = {}) {
  const minMs = opt(o, "minIntervalMs", 1000), timeoutMs = opt(o, "timeoutMs", 10000), maxBytes = opt(o, "maxBytes", MAX_BODY_BYTES);
  const policy = { profile: opt(o, "profile", PUBLIC), lookup: opt(o, "lookup", null), hosts: opt(o, "hosts", null) };
  const ev = await evaluateUrl(url, policy);
  if (ev.reason) return { ok: false, code: "policy", error: ev.reason, status: 0 };
  const headers = { "user-agent": opt(o, "userAgent", DEFAULT_USER_AGENT), accept: "application/json", "accept-encoding": "gzip, deflate, br" };
  for (const [k, v] of Object.entries(opt(o, "headers", {}) || {})) headers[k.toLowerCase()] = String(v);
  let r;
  try {
    r = await enqueue(hostOf(url), minMs, () => requestOnce(str(url), { headers, addrs: ev.addrs, timeoutMs, maxBytes }));
  } catch (error) {
    const [kind, detail] = classifyError(error, hostOf(url), timeoutMs);
    return { ok: false, code: kind === "timeout" ? "timeout" : "network", error: detail, status: 0 };
  }
  const flat = flatHeaders(r.headers);
  if (r.truncated) return { ok: false, code: "too-large", error: `response larger than ${maxBytes} bytes`, status: r.status };
  const text = decodeBody(r.body, flat["content-type"] || "application/json");
  if (r.status < 200 || r.status >= 300) {
    const extra = REDIRECT_STATUS.has(r.status) ? " (redirects are not followed)" : "";
    return { ok: false, code: "http", error: `HTTP ${r.status}${extra}${text ? ": " + clipLine(text, 200) : ""}`, status: r.status, retryAfterMs: retryAfterMs(flat["retry-after"]) };
  }
  try { return { ok: true, status: r.status, data: JSON.parse(text), headers: flat }; }
  catch { return { ok: false, code: "not-json", error: "the answer is not JSON", status: r.status }; }
}
