// dates.js — the Node twin of hoard_link/dates.py (ESM, no dependencies).
//
//   import { parseDate, findDates, parseDue, addMonths, daysBetween } from "./hoard-commons/dates.js";
//
// Dates travel as "YYYY-MM-DD" strings (Python uses date objects). Same rules and regexes as the Python module;
// both are checked against tests/vectors/dates.json.

import { fold } from "./text.js";

export const MONTHS = {
  es: [["enero", "ene"], ["febrero", "feb"], ["marzo", "mar"], ["abril", "abr"], ["mayo", "may"], ["junio", "jun"],
    ["julio", "jul"], ["agosto", "ago"], ["septiembre", "setiembre", "sept", "sep", "set"], ["octubre", "oct"],
    ["noviembre", "nov"], ["diciembre", "dic"]],
  en: [["january", "jan"], ["february", "feb"], ["march", "mar"], ["april", "apr"], ["may"], ["june", "jun"],
    ["july", "jul"], ["august", "aug"], ["september", "sept", "sep"], ["october", "oct"], ["november", "nov"], ["december", "dec"]],
  fr: [["janvier", "janv"], ["fevrier", "fevr"], ["mars"], ["avril", "avr"], ["mai"], ["juin"], ["juillet", "juil"],
    ["aout"], ["septembre", "sept"], ["octobre"], ["novembre"], ["decembre"]],
  pt: [["janeiro"], ["fevereiro", "fev"], ["marco"], ["abril"], ["maio"], ["junho"], ["julho"], ["agosto"],
    ["setembro", "set"], ["outubro", "out"], ["novembro"], ["dezembro", "dez"]],
  it: [["gennaio", "gen"], ["febbraio"], ["marzo"], ["aprile"], ["maggio", "mag"], ["giugno", "giu"], ["luglio", "lug"],
    ["agosto"], ["settembre", "set"], ["ottobre", "ott"], ["novembre"], ["dicembre", "dic"]],
  de: [["januar"], ["februar"], ["marz", "mrz"], ["april"], ["mai"], ["juni"], ["juli"], ["august"],
    ["september"], ["oktober", "okt"], ["november"], ["dezember", "dez"]],
};
export const MONTH_NAMES = {
  es: ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
  en: ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
  fr: ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
  pt: ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"],
  it: ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"],
  de: ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
};
export const WEEKDAYS = {
  es: [["lunes", "lun"], ["martes", "mar"], ["miercoles", "mie"], ["jueves", "jue"], ["viernes", "vie"], ["sabado", "sab"], ["domingo", "dom"]],
  en: [["monday", "mon"], ["tuesday", "tue"], ["wednesday", "wed"], ["thursday", "thu"], ["friday", "fri"], ["saturday", "sat"], ["sunday", "sun"]],
  fr: [["lundi"], ["mardi"], ["mercredi"], ["jeudi"], ["vendredi"], ["samedi"], ["dimanche"]],
  pt: [["segunda", "segunda-feira"], ["terca", "terca-feira"], ["quarta", "quarta-feira"], ["quinta", "quinta-feira"],
    ["sexta", "sexta-feira"], ["sabado"], ["domingo"]],
  it: [["lunedi"], ["martedi"], ["mercoledi"], ["giovedi"], ["venerdi"], ["sabato"], ["domenica"]],
  de: [["montag"], ["dienstag"], ["mittwoch"], ["donnerstag"], ["freitag"], ["samstag", "sonnabend"], ["sonntag"]],
};
export const WEEKDAY_NAMES = {
  es: ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
  en: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
  fr: ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
  pt: ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"],
  it: ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"],
  de: ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
};
export const ROLES = ["due", "expires", "issued", "delivery", "departure", "return", "purchase", "renewal"];

const MONTH_LOOKUP = {};
for (const months of Object.values(MONTHS)) months.forEach((aliases, i) => aliases.forEach((a) => { MONTH_LOOKUP[a] = i + 1; }));
const WEEKDAY_LOOKUP = {};
for (const days of Object.values(WEEKDAYS)) days.forEach((aliases, i) => aliases.forEach((a) => { WEEKDAY_LOOKUP[a] = i; }));

const foldKey = (word) => (word === null || word === undefined ? "" : fold(word, { keepLength: false }).trim().replace(/\.$/, ""));
export const monthNumber = (word) => MONTH_LOOKUP[foldKey(word)] ?? null;
export const weekdayNumber = (word) => WEEKDAY_LOOKUP[foldKey(word)] ?? null;

const byLenThenAlpha = (a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0);
const MONTH_ALT = Object.keys(MONTH_LOOKUP).sort(byLenThenAlpha).join("|");
const WEEKDAY_FULL = Object.keys(WEEKDAY_LOOKUP).filter((a) => a.length >= 5).sort(byLenThenAlpha).join("|");

// ---------------------------------------------------------------- ISO date arithmetic (strings)
const pad = (n, w = 2) => String(n).padStart(w, "0");
const MS_DAY = 86400000;

function makeDate(y, m, d) {
  if (!(y >= 1900 && y <= 2100)) return null;
  const t = new Date(Date.UTC(y, m - 1, d));
  if (t.getUTCFullYear() !== y || t.getUTCMonth() !== m - 1 || t.getUTCDate() !== d) return null;
  return `${pad(y, 4)}-${pad(m)}-${pad(d)}`;
}

/** Normalised "YYYY-MM-DD" of the first ISO date of a string (or of a Date), else null. */
export function parseIso(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : `${pad(value.getFullYear(), 4)}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
  const m = /^\s*([0-9]{4})-([0-9]{2})-([0-9]{2})/.exec(String(value ?? ""));
  if (!m) return null;
  const y = Number(m[1]), mo = Number(m[2]), d = Number(m[3]);
  const t = new Date(Date.UTC(y, mo - 1, d));
  if (t.getUTCFullYear() !== y || t.getUTCMonth() !== mo - 1 || t.getUTCDate() !== d) return null;
  return `${pad(y, 4)}-${pad(mo)}-${pad(d)}`;
}
export const isoDay = (value) => parseIso(value) ?? "";

const ms = (iso) => { const [y, m, d] = iso.split("-").map(Number); return Date.UTC(y, m - 1, d); };
const fromMs = (t) => { const d = new Date(t); return `${pad(d.getUTCFullYear(), 4)}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`; };
const need = (value) => { const d = parseIso(value); if (d === null) throw new Error(`not a date: ${value}`); return d; };
const weekdayOf = (iso) => (new Date(ms(iso)).getUTCDay() + 6) % 7;      // Monday = 0

export const addDays = (value, days) => fromMs(ms(need(value)) + Math.trunc(days) * MS_DAY);

export function addMonths(value, months) {
  const [y, m, d] = need(value).split("-").map(Number);
  const index = y * 12 + (m - 1) + Math.trunc(months);
  const year = Math.floor(index / 12), month = (index % 12 + 12) % 12 + 1;
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return `${pad(year, 4)}-${pad(month)}-${pad(Math.min(d, last))}`;
}

export function daysBetween(a, b) {
  const da = parseIso(a), db = parseIso(b);
  return da === null || db === null ? null : Math.round((ms(db) - ms(da)) / MS_DAY);
}

export function nextWeekday(value, weekday, { strict = true } = {}) {
  const d = need(value);
  let ahead = (((Math.trunc(weekday) - weekdayOf(d)) % 7) + 7) % 7;
  if (ahead === 0 && strict) ahead = 7;
  return addDays(d, ahead);
}

function todayIso(today) {
  const d = today !== undefined && today !== null ? parseIso(today) : null;
  return d ?? parseIso(new Date());
}

export function resolveYear(day, month, today, prefer = "nearest") {
  const t = todayIso(today);
  const ty = Number(t.slice(0, 4));
  const cands = [ty - 1, ty, ty + 1].map((y) => makeDate(y, month, day)).filter(Boolean);
  if (!cands.length) return null;
  const score = (c) => {
    const diff = daysBetween(t, c);
    if (prefer === "future") return diff >= -10 ? diff : 10000 - diff;
    if (prefer === "past") return diff <= 45 ? -diff : 10000 + diff;
    if (prefer === "next") return diff >= 0 ? diff : 10000 - diff;
    return Math.abs(diff);
  };
  let best = cands[0];
  for (const c of cands.slice(1)) if (score(c) < score(best)) best = c;
  return best;
}

// ---------------------------------------------------------------- scanning (the same regexes as Python)
const RX_ISO = String.raw`(?<![0-9/.-])((?:19|20)[0-9]{2})([-/.])([0-9]{1,2})\2([0-9]{1,2})(?![0-9])`;
const RX_NUM = String.raw`(?<![0-9/.-])([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{4}|[0-9]{2})(?![0-9]|[./-][0-9])`;
const RX_DMY = String.raw`(?<![0-9])([0-9]{1,2})(?:st|nd|rd|th|o|º|°)?\.?\s*(?:de\s+|of\s+)?(` + MONTH_ALT + String.raw`)(?![a-z])\.?(?:,?\s*(?:del?\s+|of\s+)?([0-9]{4})(?![0-9]))?`;
const RX_MDY = String.raw`(?<![a-z0-9])(` + MONTH_ALT + String.raw`)(?![a-z])\.?\s+([0-9]{1,2})(?:st|nd|rd|th)?(?![0-9a-z])(?:,?\s*([0-9]{4})(?![0-9]))?`;
const RX_DM = String.raw`(?<![0-9/.-])([0-9]{1,2})/([0-9]{1,2})(?![0-9/])`;

const ROLE_SRC = [
  ["due", String.raw`fecha\s+(?:de\s+|limite\s+de\s+)?(?:vencimiento|pago|cargo|cobro|adeudo|domiciliacion)|fecha\s+limite|vencimiento|` +
    String.raw`vence(?:\s+el)?|pagar\s+antes\s+del?|pago\s+antes\s+del?|antes\s+del|` +
    String.raw`se\s+(?:cargara|cobrara|adeudara|pasara\s+al\s+cobro)(?:\s+(?:en\s+su\s+cuenta\s+)?el)?|` +
    String.raw`proximo\s+(?:cobro|cargo|pago|recibo)|siguiente\s+(?:cobro|cargo|pago|recibo)|due\s+date|payment\s+due|due\s+on|` +
    String.raw`pay\s+by|next\s+(?:billing(?:\s+date)?|payment|charge)|billing\s+date|(?:pagar|pago|abonar)\s+hasta(?:\s+el)?`],
  ["expires", String.raw`valid[oa]\s+hasta|vigente\s+hasta|fecha\s+de\s+(?:caducidad|expiracion|validez)|caducidad|caduca(?:\s+el)?|` +
    String.raw`validez|valid\s+until|valid\s+through|expires?(?:\s+on)?|expiry(?:\s+date)?|expiration(?:\s+date)?|use\s+by|` +
    String.raw`best\s+before|consumir\s+preferentemente\s+antes\s+del?|hasta\s+el`],
  ["issued", String.raw`fecha\s+(?:de\s+)?(?:la\s+)?(?:factura|emision|expedicion|edicion|documento|recibo)|fecha\s+factura|` +
    String.raw`invoice\s+date|date\s+of\s+issue|issue\s+date|issued(?:\s+on)?|emitid[oa](?:\s+el)?|expedid[oa](?:\s+el)?|fecha|date`],
  ["delivery", String.raw`fecha\s+(?:estimada\s+|prevista\s+)?de\s+entrega|entrega\s+(?:estimada|prevista)|entrega\s+entre|` +
    String.raw`entregad[oa]\s+el|se\s+entregara(?:\s+el)?|llegara(?:\s+el)?|llega(?:\s+el)?|llegada\s+estimada|` +
    String.raw`recibiras(?:\s+el)?|recibelo(?:\s+el)?|delivered(?:\s+on)?|delivery\s+date|(?:estimated|expected)\s+delivery|` +
    String.raw`arrives?(?:\s+on)?|arriving(?:\s+on)?|entrega`],
  ["departure", String.raw`fecha\s+de\s+(?:salida|ida|viaje|inicio)|salida|salimos|sale(?:\s+el)?|departure(?:\s+date)?|departs?(?:\s+on)?|` +
    String.raw`departing|outbound|ida|vuelo|check-?in|despega|embarque|start\s+date`],
  ["return", String.raw`fecha\s+de\s+(?:regreso|vuelta)|regreso|vuelta|return(?:\s+date)?|returns?(?:\s+on)?|inbound|check-?out`],
  ["purchase", String.raw`fecha\s+de\s+(?:la\s+)?(?:compra|venta|pedido|operacion|transaccion)|fecha\s+compra|order\s+date|` +
    String.raw`date\s+of\s+(?:purchase|order)|purchase\s+date|comprad[oa]\s+el|pedido\s+realizado\s+el|pedido\s+del|compra\s+del|` +
    String.raw`ordered\s+on|purchased\s+on`],
  ["renewal", String.raw`proxima\s+renovacion|fecha\s+de\s+renovacion|renovacion|se\s+renovara(?:\s+(?:automaticamente\s+)?el)?|` +
    String.raw`renews?(?:\s+(?:automatically\s+)?on)?|renewal\s+date|next\s+renewal|prorroga`],
];
const ROLE_RX = ROLE_SRC.map(([role, src]) => [role, new RegExp(String.raw`(?<![a-z])(?:` + src + String.raw`)(?![a-z])`, "g")]);
const FUTURE_ROLES = new Set(["due", "expires", "delivery", "departure", "return", "renewal"]);
const PAST_ROLES = new Set(["issued", "purchase"]);
const LABEL_WINDOW = 80;
const LABEL_GAP = 24;

function scan(folded, { dayfirst, loose }) {
  const raw = [];
  for (const m of folded.matchAll(new RegExp(RX_ISO, "g"))) {
    if (makeDate(Number(m[1]), Number(m[3]), Number(m[4]))) raw.push([m.index, m.index + m[0].length, Number(m[1]), Number(m[3]), Number(m[4])]);
  }
  for (const m of folded.matchAll(new RegExp(RX_NUM, "g"))) {
    const a = Number(m[1]), b = Number(m[3]);
    let [day, month] = dayfirst ? [a, b] : [b, a];
    if (month > 12 && day <= 12) [day, month] = [month, day];
    const year = m[4].length === 2 ? 2000 + Number(m[4]) : Number(m[4]);
    if (makeDate(year, month, day)) raw.push([m.index, m.index + m[0].length, year, month, day]);
  }
  for (const m of folded.matchAll(new RegExp(RX_DMY, "g"))) {
    const month = MONTH_LOOKUP[m[2]], day = Number(m[1]);
    if (m[3]) { if (makeDate(Number(m[3]), month, day)) raw.push([m.index, m.index + m[0].length, Number(m[3]), month, day]); }
    else if (day >= 1 && day <= 31) raw.push([m.index, m.index + m[0].length, null, month, day]);
  }
  for (const m of folded.matchAll(new RegExp(RX_MDY, "g"))) {
    const month = MONTH_LOOKUP[m[1]], day = Number(m[2]);
    if (m[3]) { if (makeDate(Number(m[3]), month, day)) raw.push([m.index, m.index + m[0].length, Number(m[3]), month, day]); }
    else if (day >= 1 && day <= 31) raw.push([m.index, m.index + m[0].length, null, month, day]);
  }
  if (loose) {
    for (const m of folded.matchAll(new RegExp(RX_DM, "g"))) {
      const a = Number(m[1]), b = Number(m[2]);
      let [day, month] = dayfirst ? [a, b] : [b, a];
      if (month > 12 && day <= 12) [day, month] = [month, day];
      if (month >= 1 && month <= 12 && day >= 1 && day <= 31) raw.push([m.index, m.index + m[0].length, null, month, day]);
    }
  }
  raw.sort((x, y) => x[0] - y[0] || (y[1] - y[0]) - (x[1] - x[0]));
  const out = [];
  for (const r of raw) {
    if (out.length && r[0] < out[out.length - 1][1]) continue;
    out.push(r);
  }
  return out;
}

function labelRole(context) {
  let best = [-1, 0, ""];
  for (const [role, rx] of ROLE_RX) {
    rx.lastIndex = 0;
    for (const m of context.matchAll(rx)) {
      const end = m.index + m[0].length, len = m[0].length;
      if (end > best[0] || (end === best[0] && len > best[1])) best = [end, len, role];
    }
  }
  return best[2] && context.length - best[0] <= LABEL_GAP ? best[2] : "";
}

const dayfirstOf = (dayfirst, lang) => (dayfirst !== null && dayfirst !== undefined ? !!dayfirst : !["en-us", "us"].includes(String(lang || "es").toLowerCase()));

export function findDates(text, { today = null, lang = "es", dayfirst = true, loose = false } = {}) {
  const s = text === null || text === undefined ? "" : String(text);
  const folded = fold(s);
  const t = todayIso(today);
  const first = dayfirstOf(dayfirst, lang);
  const hits = [];
  let prevEnd = 0;
  for (const [start, end, year, month, day] of scan(folded, { dayfirst: first, loose })) {
    const lineStart = folded.lastIndexOf("\n", start - 1) + 1;
    const segStart = Math.max(lineStart, start - LABEL_WINDOW, prevEnd);
    const role = labelRole(folded.slice(segStart, start));
    let resolved;
    if (year === null) {
      const prefer = FUTURE_ROLES.has(role) ? "future" : PAST_ROLES.has(role) ? "past" : "nearest";
      resolved = resolveYear(day, month, t, prefer);
    } else resolved = makeDate(year, month, day);
    prevEnd = end;
    if (resolved === null) continue;
    hits.push({ date: resolved, start, end, role, text: s.slice(start, end) });
  }
  return hits;
}

export function parseDate(text, { today = null, lang = "es", dayfirst = true, prefer = "nearest" } = {}) {
  const s = text === null || text === undefined ? "" : String(text);
  const t = todayIso(today);
  for (const [, , year, month, day] of scan(fold(s), { dayfirst: dayfirstOf(dayfirst, lang), loose: true })) {
    const found = year !== null ? makeDate(year, month, day) : resolveYear(day, month, t, prefer);
    if (found) return found;
  }
  return null;
}

// ---------------------------------------------------------------- spoken due dates
const NUMBER_WORDS = { un: 1, una: 1, uno: 1, a: 1, an: 1, one: 1, dos: 2, two: 2, tres: 3, three: 3, cuatro: 4, four: 4, cinco: 5, five: 5,
  seis: 6, six: 6, siete: 7, seven: 7, ocho: 8, eight: 8, nueve: 9, nine: 9, diez: 10, ten: 10, quince: 15, veinte: 20, treinta: 30 };
const RX_MORNING = /\b(?:por|de|en|esta|la|a la)\s+(?:la\s+)?manana\b/g;
const RX_IN = /\b(?:en|dentro de|in)\s+([0-9]+|[a-z]+)\s+(dias?|days?|semanas?|weeks?|meses|mes|months?)\b/;
const RX_WEEKDAY = new RegExp(String.raw`\b(` + WEEKDAY_FULL + String.raw`)\b`);
const RX_EOM = /\b(?:a\s+)?(?:fin|final|finales)\s+de(?:l)?\s+mes\b|\bend\s+of\s+(?:the\s+)?month\b/;
const RX_DAYNUM = /\b(?:el\s+)?dia\s+([0-9]{1,2})\b|\bel\s+([0-9]{1,2})\b(?!\s*(?:de\b|\/|-))/;
const RX_NEXT_WEEK = /\b(?:la\s+)?semana\s+que\s+viene\b|\b(?:la\s+)?proxima\s+semana\b|\bnext\s+week\b/;
const RX_NEXT_MONTH = /\b(?:el\s+)?mes\s+que\s+viene\b|\b(?:el\s+)?proximo\s+mes\b|\bnext\s+month\b/;

export function parseDue(text, { today = null, lang = "es", vague = false } = {}) {
  if (text === null || text === undefined || !String(text).trim()) return null;
  const t = todayIso(today);
  const words = fold(String(text), { keepLength: false }).trim();
  const found = parseDate(words, { today: t, prefer: "next", dayfirst: true });
  if (found) return found;
  const cleaned = words.replace(RX_MORNING, " ");
  let m = RX_IN.exec(cleaned);
  if (m) {
    const token = m[1];
    const amount = /^[0-9]+$/.test(token) ? Number(token) : NUMBER_WORDS[token];
    if (amount !== undefined && amount > 0 && amount <= 366) {
      const unit = m[2];
      if (/^(dia|day)/.test(unit)) return addDays(t, amount);
      if (/^(semana|week)/.test(unit)) return addDays(t, amount * 7);
      return addMonths(t, amount);
    }
  }
  if (/\bpasado manana\b|\bday after tomorrow\b/.test(cleaned)) return addDays(t, 2);
  if (/\bmanana\b|\btomorrow\b/.test(cleaned)) return addDays(t, 1);
  if (/\bhoy\b|\btoday\b|\besta noche\b|\btonight\b/.test(cleaned)) return t;
  if (vague) {
    if (RX_NEXT_WEEK.test(cleaned)) return nextWeekday(t, 0);
    if (RX_NEXT_MONTH.test(cleaned)) return addMonths(`${t.slice(0, 8)}01`, 1);
  }
  m = RX_WEEKDAY.exec(cleaned);
  if (m) return nextWeekday(t, WEEKDAY_LOOKUP[m[1]]);
  m = RX_DAYNUM.exec(cleaned);
  if (m) {
    const day = Number(m[1] ?? m[2]);
    if (day >= 1 && day <= 31) {
      for (const offset of [0, 1]) {
        const first = addMonths(`${t.slice(0, 8)}01`, offset);
        const cand = makeDate(Number(first.slice(0, 4)), Number(first.slice(5, 7)), day);
        if (cand && cand >= t) return cand;
      }
    }
  }
  if (RX_EOM.test(cleaned)) return addDays(addMonths(`${t.slice(0, 8)}01`, 1), -1);
  return null;
}
