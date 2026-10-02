// ics.js — the Node twin of hoard_link/ics.py (ESM; uses node:crypto for the fallback UID).
//
//   import { buildIcs, parseIcs, icsEscape, foldLine } from "./hoard-commons/ics.js";
//
// iCalendar (RFC 5545) export and a small parser. Event keys are the snake_case ones of the Python module
// (uid, title, start, end, all_day, end_exclusive, tz, description, location, url, categories, rrule, status, priority,
// transp, alarms); both implementations are checked against tests/vectors/ics.json. See the Python docstring for the
// full table. `start` / `end` are ISO text (or a Date, which counts as an instant).

import { createHash } from "node:crypto";

export const DEFAULT_PRODID = "-//Hoard//Family//EN";

/** Escape a TEXT value: backslash, semicolon, comma and line breaks. */
export function icsEscape(text) {
  const s = text == null ? "" : String(text);
  return s.replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r\n|\r|\n/g, "\\n");
}

export function icsUnescape(text) {
  return (text == null ? "" : String(text)).replace(/\\([nN,;\\])/g, (_, c) => (c === "n" || c === "N" ? "\n" : c));
}

const octets = (s) => Buffer.byteLength(s, "utf8");

/** Fold a content line at `limit` octets (never inside a character); pieces joined with CRLF + space. */
export function foldLine(line, limit = 75) {
  if (octets(line) <= limit) return line;
  const out = [];
  let cur = "";
  let size = 0;
  for (const ch of line) {
    const n = octets(ch);
    if (size + n > (out.length ? limit - 1 : limit)) {
      out.push(cur);
      cur = ch;
      size = n;
    } else {
      cur += ch;
      size += n;
    }
  }
  out.push(cur);
  return out.join("\r\n ");
}

// ---------------------------------------------------------------------------------------------------- time values
const WHEN = /^\s*([0-9]{4})-?([0-9]{2})-?([0-9]{2})(?:[T ]([0-9]{2}):?([0-9]{2})(?::?([0-9]{2}))?(?:[.,][0-9]+)?)?\s*(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?\s*$/i;
const pad = (n, w = 2) => String(n).padStart(w, "0");

function validDay(y, mo, d) {
  const t = new Date(Date.UTC(y, mo - 1, d));
  return t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d;
}

function fromEpoch(ms, k) {
  const t = new Date(ms);
  return { k, y: t.getUTCFullYear(), mo: t.getUTCMonth() + 1, d: t.getUTCDate(), h: t.getUTCHours(), mi: t.getUTCMinutes(), s: t.getUTCSeconds() };
}

const epoch = (w) => Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi, w.s);

function when(value) {
  if (value == null || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : fromEpoch(value.getTime(), "utc");
  const m = WHEN.exec(String(value));
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  if (!validDay(y, mo, d)) return null;
  if (m[4] === undefined) return { k: "date", y, mo, d, h: 0, mi: 0, s: 0 };
  const h = +m[4], mi = +m[5], s = +(m[6] || 0);
  if (h > 23 || mi > 59 || s > 59) return null;
  const zone = m[7];
  if (!zone) return { k: "local", y, mo, d, h, mi, s };
  let ms = Date.UTC(y, mo - 1, d, h, mi, s);
  if (zone.toUpperCase() !== "Z") {
    const sign = zone[0] === "-" ? -1 : 1;
    const digits = zone.slice(1).replace(":", "");
    ms -= sign * (+digits.slice(0, 2) * 60 + +(digits.slice(2, 4) || 0)) * 60000;
  }
  return fromEpoch(ms, "utc");
}

function add(w, { days = 0, seconds = 0 } = {}) {
  if (w.k === "date") return fromEpoch(Date.UTC(w.y, w.mo - 1, w.d + days + Math.floor(seconds / 86400)), "date");
  return fromEpoch(epoch(w) + days * 86400000 + seconds * 1000, w.k);
}

const ymd = (w) => `${pad(w.y, 4)}${pad(w.mo)}${pad(w.d)}`;
const compact = (w) => `${ymd(w)}T${pad(w.h)}${pad(w.mi)}${pad(w.s)}${w.k === "utc" ? "Z" : ""}`;
function iso(w) {
  const day = `${pad(w.y, 4)}-${pad(w.mo)}-${pad(w.d)}`;
  return w.k === "date" ? day : `${day}T${pad(w.h)}:${pad(w.mi)}:${pad(w.s)}${w.k === "utc" ? "Z" : ""}`;
}

function stamp(now) {
  const w = (now != null ? when(now) : null) || when(new Date());
  return compact({ ...w, k: "utc" });
}

const present = (v) => v !== undefined && v !== null && String(v).trim() !== "";
function text(ev, ...names) {
  for (const n of names) if (present(ev[n])) return String(ev[n]);
  return "";
}

const trigger = (minutes) => `${minutes > 0 ? "-" : ""}PT${Math.abs(minutes)}M`;

function alarmLines(ev, title) {
  let raw = ev.alarms;
  if (raw == null) raw = [];
  else if (!Array.isArray(raw)) raw = [raw];
  const out = [];
  for (const a of raw) {
    let label = title || "Reminder";
    let trig;
    if (a && typeof a === "object") {
      label = text(a, "text", "description") || label;
      const at = a.at ? when(a.at) : null;
      if (at && at.k !== "date") trig = "TRIGGER;VALUE=DATE-TIME:" + compact({ ...at, k: "utc" });
      else if (a.minutes != null) trig = "TRIGGER:" + trigger(Math.trunc(Number(a.minutes)));
      else continue;
    } else {
      const n = Math.trunc(Number(a));
      if (a === null || a === "" || Number.isNaN(n)) continue;
      trig = "TRIGGER:" + trigger(n);
    }
    out.push("BEGIN:VALARM", "ACTION:DISPLAY", `DESCRIPTION:${icsEscape(label)}`, trig, "END:VALARM");
  }
  return out;
}

function eventLines(ev, dtstamp, calTz) {
  const title = text(ev, "title", "summary");
  const start = when(present(ev.start) ? ev.start : ev.dtstart);
  if (!start) return [];
  let end = when(present(ev.end) ? ev.end : ev.dtend);
  const allDay = Boolean(ev.all_day || ev.allDay) || start.k === "date";
  const tz = text(ev, "tz", "tzid") || calTz;
  const lines = ["BEGIN:VEVENT"];
  let dtstart, dtend = "", key;
  if (allDay) {
    const s = { ...start, k: "date" };
    let lastExcl;
    if (!end) lastExcl = add(s, { days: 1 });
    else {
      end = { ...end, k: "date" };
      lastExcl = ev.end_exclusive || ev.endExclusive ? end : add(end, { days: 1 });
      if (ymd(lastExcl) <= ymd(s)) lastExcl = add(s, { days: 1 });
    }
    dtstart = `DTSTART;VALUE=DATE:${ymd(s)}`;
    dtend = `DTEND;VALUE=DATE:${ymd(lastExcl)}`;
    key = `${ymd(s)}|${ymd(lastExcl)}`;
  } else {
    dtstart = `DTSTART${start.k === "local" && tz ? `;TZID=${tz}` : ""}:${compact(start)}`;
    key = compact(start) + "|";
    if (end) {
      if (end.k === "date") end = { ...end, k: start.k };
      dtend = `DTEND${end.k === "local" && tz ? `;TZID=${tz}` : ""}:${compact(end)}`;
      key += compact(end);
    }
  }
  let uid = text(ev, "uid", "id").trim();
  if (!uid) uid = createHash("sha1").update([title, key, text(ev, "location")].join("\x1f"), "utf8").digest("hex").slice(0, 20);
  if (!uid.includes("@")) uid += "@hoard";
  lines.push(`UID:${uid}`, `DTSTAMP:${dtstamp}`, dtstart);
  if (dtend) lines.push(dtend);
  const rrule = text(ev, "rrule").trim();
  if (rrule) lines.push("RRULE:" + rrule.replace(/^RRULE:/i, ""));
  lines.push(`SUMMARY:${icsEscape(title)}`);
  const desc = text(ev, "description", "detail", "notes");
  if (desc) lines.push(`DESCRIPTION:${icsEscape(desc)}`);
  const loc = text(ev, "location");
  if (loc) lines.push(`LOCATION:${icsEscape(loc)}`);
  const url = text(ev, "url").replace(/[\r\n]/g, "");
  if (url) lines.push(`URL:${url}`);
  let cats = ev.categories;
  if (typeof cats === "string") cats = cats.split(",").map((c) => c.trim());
  cats = (cats || []).map(String).filter((c) => c.trim());
  if (cats.length) lines.push("CATEGORIES:" + cats.map(icsEscape).join(","));
  const status = text(ev, "status").toUpperCase();
  if (["CONFIRMED", "TENTATIVE", "CANCELLED"].includes(status)) lines.push(`STATUS:${status}`);
  if (ev.priority != null && /^-?[0-9]+$/.test(String(ev.priority).trim()) && Number(ev.priority) >= 0 && Number(ev.priority) <= 9) {
    lines.push(`PRIORITY:${parseInt(ev.priority, 10)}`);
  }
  const transp = text(ev, "transp").toUpperCase();
  if (transp === "OPAQUE" || transp === "TRANSPARENT") lines.push(`TRANSP:${transp}`);
  lines.push(...alarmLines(ev, title), "END:VEVENT");
  return lines;
}

/**
 * The calendar text (CRLF line ends, folded at 75 octets, ending with a CRLF). `name` -> X-WR-CALNAME, `tz` ->
 * X-WR-TIMEZONE and the TZID of local times, `now` -> every DTSTAMP (default: the current time).
 */
export function buildIcs(events, { name = null, prodid = DEFAULT_PRODID, tz = null, now = null } = {}) {
  const dtstamp = stamp(now);
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", `PRODID:${prodid}`, "CALSCALE:GREGORIAN", "METHOD:PUBLISH"];
  if (name) lines.push(`X-WR-CALNAME:${icsEscape(name)}`);
  if (tz) lines.push(`X-WR-TIMEZONE:${tz}`);
  for (const ev of events || []) lines.push(...eventLines(ev, dtstamp, tz || ""));
  lines.push("END:VCALENDAR");
  return lines.map((l) => foldLine(l)).join("\r\n") + "\r\n";
}

// ---------------------------------------------------------------------------------------------------- parsing
const DURATION = /^([+-])?P(?:([0-9]+)W)?(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)S)?)?$/i;

function durationSeconds(value) {
  const m = DURATION.exec(value.trim());
  if (!m || ![2, 3, 4, 5, 6].some((i) => m[i])) return null;
  const [w, d, h, mi, s] = [2, 3, 4, 5, 6].map((i) => +(m[i] || 0));
  const total = ((w * 7 + d) * 24 + h) * 3600 + mi * 60 + s;
  return m[1] === "-" ? -total : total;
}

function unfold(textValue) {
  const out = [];
  for (const raw of textValue.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n")) {
    if ((raw[0] === " " || raw[0] === "\t") && out.length) out[out.length - 1] += raw.slice(1);
    else if (raw !== "") out.push(raw);
  }
  return out;
}

function splitLine(line) {
  let inQ = false;
  let cut = -1;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') inQ = !inQ;
    else if (ch === ":" && !inQ) { cut = i; break; }
  }
  if (cut < 0) return [line.toUpperCase(), {}, ""];
  const head = line.slice(0, cut);
  const value = line.slice(cut + 1);
  const parts = head.split(/;(?=(?:[^"]*"[^"]*")*[^"]*$)/);
  const params = {};
  for (const p of parts.slice(1)) {
    const i = p.indexOf("=");
    if (i >= 0) params[p.slice(0, i).toUpperCase()] = p.slice(i + 1).replace(/^"|"$/g, "");
  }
  return [parts[0].toUpperCase(), params, value];
}

function whenProp(value, params) {
  const w = when(value.trim());
  if (!w) return null;
  return (params.VALUE || "").toUpperCase() === "DATE" ? { ...w, k: "date" } : w;
}

function parseAlarm(props) {
  for (const [name, params, value] of props) {
    if (name !== "TRIGGER") continue;
    if ((params.VALUE || "").toUpperCase() === "DATE-TIME") {
      const w = when(value.trim());
      return w ? { at: iso({ ...w, k: "utc" }) } : null;
    }
    const sec = durationSeconds(value);
    if (sec !== null) return -Math.floor(sec / 60) || 0;
  }
  return null;
}

function eventFrom(props, alarms) {
  const first = {};
  const cats = [];
  for (const [name, params, value] of props) {
    if (name === "CATEGORIES") {
      for (const c of value.split(/(?<!\\),/)) if (c.trim()) cats.push(icsUnescape(c).trim());
    } else if (!(name in first)) first[name] = [params, value];
  }
  const txt = (n) => (n in first ? icsUnescape(first[n][1]).trim() : "");
  const start = "DTSTART" in first ? whenProp(first.DTSTART[1], first.DTSTART[0]) : null;
  let end = "DTEND" in first ? whenProp(first.DTEND[1], first.DTEND[0]) : null;
  const allDay = Boolean(start && start.k === "date");
  if (!end && start && "DURATION" in first) {
    const sec = durationSeconds(first.DURATION[1]);
    if (sec !== null) end = add(start, { seconds: sec });
  }
  return {
    uid: txt("UID"), summary: txt("SUMMARY"), description: txt("DESCRIPTION"), location: txt("LOCATION"),
    start: start ? iso(start) : "", end: end ? iso(end) : "", all_day: allDay, end_exclusive: allDay,
    tzid: "DTSTART" in first ? first.DTSTART[0].TZID || "" : "", rrule: "RRULE" in first ? first.RRULE[1].trim() : "",
    url: "URL" in first ? first.URL[1].trim() : "", status: txt("STATUS").toUpperCase(), categories: cats, alarms: [...alarms],
  };
}

/**
 * The VEVENTs of an iCalendar text as dicts with the fixed keys uid, summary, description, location, start, end,
 * all_day, end_exclusive, tzid, rrule, url, status, categories, alarms (see the Python docstring).
 */
export function parseIcs(textValue) {
  const events = [];
  const stack = [];
  let cur = null;
  let alarm = null;
  let alarms = [];
  for (const line of unfold(textValue || "")) {
    const [name, params, value] = splitLine(line);
    if (name === "BEGIN") {
      const comp = value.trim().toUpperCase();
      stack.push(comp);
      if (comp === "VEVENT") { cur = []; alarms = []; }
      else if (comp === "VALARM" && cur !== null) alarm = [];
      continue;
    }
    if (name === "END") {
      const comp = value.trim().toUpperCase();
      if (stack.length && stack[stack.length - 1] === comp) stack.pop();
      if (comp === "VALARM" && alarm !== null && cur !== null) {
        const a = parseAlarm(alarm);
        if (a !== null) alarms.push(a);
        alarm = null;
      } else if (comp === "VEVENT" && cur !== null) {
        events.push(eventFrom(cur, alarms));
        cur = null;
      }
      continue;
    }
    if (alarm !== null) alarm.push([name, params, value]);
    else if (cur !== null && stack.length && stack[stack.length - 1] === "VEVENT") cur.push([name, params, value]);
  }
  return events;
}
