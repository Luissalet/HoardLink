"""Spheres facet (0.7): two lives kept apart, ``personal`` and ``work`` (more can be added).

A sphere decides which mail accounts and chat sources belong to it, who and what
counts as "attention" or "low", when the person must not be disturbed, how a
notification of each priority is delivered, when the daily digest is composed and
which apps may see the sphere's mail. The file is ``<data>/spheres.json``::

    {"active": "personal", "spheres": [{"id": "personal", "name": {"es": ..., "en": ...},
      "color": "#c9a227", "mail_accounts": ["*"], "chat_sources": [], "vip": [], "keywords": [],
      "mute": [], "quiet_hours": {"start": "22:30", "end": "08:00", "days": "daily"},
      "notify": {"urgent": [...], "high": [...], "normal": [...], "low": [...]},
      "digest": {"enabled": true, "at": "08:30", "days": "daily", "summarize": true, "channels": [...]},
      "apps": ["*"]}, ...]}

The facet dispatches nothing to the page by itself: the UI sets ``<html data-sphere>``
and fires a ``sphere`` event on its own bus when the person switches.

Python API (other facets call these): :meth:`active`, :meth:`get`, :meth:`list`,
:meth:`set_active`, :meth:`sphere_of_account`, :meth:`sphere_of_chat_source`,
:meth:`sphere_of_app`, :meth:`classify`, :meth:`in_quiet_hours`, :meth:`app_allowed`.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Any, Callable, Optional

from .facets import Facet, Request

logger = logging.getLogger("hoard_hub.spheres")

#: Channels a sphere may route a notification to (``digest`` = keep for the daily digest).
NOTIFY_CHANNELS = ("windows", "ntfy", "telegram", "email", "digest")
PRIORITIES = ("urgent", "high", "normal", "low")
DEFAULT_SPHERE = "personal"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_COLOR = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DAY_NAMES = {
    "mon": 0, "monday": 0, "lun": 0, "lunes": 0, "tue": 1, "tuesday": 1, "mar": 1, "martes": 1,
    "wed": 2, "wednesday": 2, "mie": 2, "miercoles": 2, "thu": 3, "thursday": 3, "jue": 3, "jueves": 3,
    "fri": 4, "friday": 4, "vie": 4, "viernes": 4, "sat": 5, "saturday": 5, "sab": 5, "sabado": 5,
    "sun": 6, "sunday": 6, "dom": 6, "domingo": 6,
}
_DAY_CANON = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MAX_LIST = 200
_MAX_ITEM = 200
_SCAN_CHARS = 4000


def default_spheres() -> list[dict[str, Any]]:
    return [
        {"id": "personal", "name": {"es": "Personal", "en": "Personal"}, "color": "#c9a227",
         "mail_accounts": ["*"], "chat_sources": [], "vip": [], "keywords": [], "mute": [],
         "quiet_hours": {"start": "22:30", "end": "08:00", "days": "daily"},
         "notify": {"urgent": ["windows", "telegram"], "high": ["windows"], "normal": ["windows"], "low": ["digest"]},
         "digest": {"enabled": True, "at": "08:30", "days": "daily", "summarize": True, "channels": ["windows"]},
         "apps": ["*"]},
        {"id": "work", "name": {"es": "Trabajo", "en": "Work"}, "color": "#3d7bd9",
         "mail_accounts": [], "chat_sources": [], "vip": [],
         "keywords": ["urgente", "urgent", "asap", "deadline", "hoy", "today"], "mute": [],
         "quiet_hours": {"start": "19:00", "end": "08:30", "days": "daily"},
         "notify": {"urgent": ["windows"], "high": ["windows"], "normal": ["digest"], "low": ["digest"]},
         "digest": {"enabled": True, "at": "08:45", "days": "weekdays", "summarize": True, "channels": ["windows"]},
         "apps": []},
    ]


def blank_sphere(sphere_id: str = "") -> dict[str, Any]:
    """The template a new sphere starts from: nothing claimed, no app allowed (privacy first)."""
    return {"id": sphere_id, "name": {"es": sphere_id.title(), "en": sphere_id.title()}, "color": "#8a8f98",
            "mail_accounts": [], "chat_sources": [], "vip": [], "keywords": [], "mute": [],
            "quiet_hours": {"start": "22:30", "end": "08:00", "days": "daily"},
            "notify": {"urgent": ["windows"], "high": ["windows"], "normal": ["windows"], "low": ["digest"]},
            "digest": {"enabled": True, "at": "08:30", "days": "daily", "summarize": True, "channels": ["windows"]},
            "apps": []}


# -- helpers ---------------------------------------------------------------------------------------------

def fold(text: Any) -> str:
    """Lower-case and strip accents: how every match in this module compares text."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _has_word(hay: str, word: str) -> bool:
    """``word`` (already folded) as a whole word or phrase of ``hay`` (already folded)."""
    if not word:
        return False
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", hay) is not None


def address_of(sender: Any) -> str:
    """The lower-case e-mail address inside ``"Name <a@b.c>"`` (or the text itself when it has none)."""
    raw = str(sender or "").strip()
    addr = parseaddr(raw)[1] or raw
    return addr.strip().lower()


def _domain_match(addr: str, domain: str) -> bool:
    dom = addr.rsplit("@", 1)[1] if "@" in addr else addr
    return dom == domain or dom.endswith("." + domain)


def sender_matches(entry: str, sender: str) -> bool:
    """``entry`` is an address, an ``@domain`` or (no ``@``) a word of the sender's display name."""
    e = str(entry or "").strip().lower()
    if not e:
        return False
    addr = address_of(sender)
    if e.startswith("@"):
        return _domain_match(addr, e[1:])
    if "@" in e:
        return addr == e
    return _has_word(fold(sender), fold(e))


def parse_days(spec: Any) -> set[int]:
    """``daily`` / ``weekdays`` / ``weekends`` / a list or comma string of day names → weekday numbers (Mon = 0)."""
    if isinstance(spec, str):
        s = fold(spec).strip()
        if s in ("", "daily", "diario", "todos", "all", "*", "everyday", "cada dia"):
            return set(range(7))
        if s in ("weekdays", "laborables", "weekday", "lunes a viernes"):
            return set(range(5))
        if s in ("weekends", "finde", "finde semana", "weekend", "fin de semana"):
            return {5, 6}
        spec = [p for p in re.split(r"[,;\s]+", s) if p]
    if isinstance(spec, (list, tuple)):
        out: set[int] = set()
        for p in spec:
            k = fold(p).strip()
            if k in _DAY_NAMES:
                out.add(_DAY_NAMES[k])
            elif k in ("weekdays", "laborables"):
                out |= set(range(5))
            elif k in ("weekends", "finde"):
                out |= {5, 6}
            elif k in ("daily", "diario"):
                out |= set(range(7))
        return out
    return set(range(7))


def _canon_days(spec: Any, errors: list[str], field: str, default: Any = "daily") -> Any:
    if spec is None:
        return default
    if isinstance(spec, str):
        s = fold(spec).strip()
        if s in ("daily", "diario", "todos", "all", "*", "everyday", ""):
            return "daily"
        if s in ("weekdays", "laborables"):
            return "weekdays"
        if s in ("weekends", "finde"):
            return "weekends"
    days = parse_days(spec)
    given = spec if isinstance(spec, (list, tuple)) else re.split(r"[,;\s]+", str(spec))
    known = [g for g in given if str(g).strip()]
    if not days or not all(fold(g).strip() in _DAY_NAMES or fold(g).strip() in ("weekdays", "weekends", "daily") for g in known):
        errors.append(f"{field}: days must be daily, weekdays, weekends or a list of mon..sun")
        return default
    if len(days) == 7:
        return "daily"
    return [_DAY_CANON[i] for i in sorted(days)]


def _hhmm(value: Any, errors: list[str], field: str, default: str, allow_empty: bool = False) -> str:
    if value is None:
        return default
    s = str(value).strip()
    if not s and allow_empty:
        return ""
    m = _HHMM.match(s)
    if not m:
        errors.append(f"{field}: expected HH:MM")
        return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on", "si", "sí"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
    return default


def _str_list(value: Any, field: str, errors: list[str], default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = re.split(r"[,\n;]", value)
    if not isinstance(value, (list, tuple)):
        errors.append(f"{field}: must be a list")
        return list(default)
    out: list[str] = []
    seen: set[str] = set()
    for x in value:
        s = str(x).strip()
        if not s or len(s) > _MAX_ITEM or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out[:_MAX_LIST]


def _channels(value: Any, field: str, errors: list[str], default: list[str], allow_digest: bool = True) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value.strip())
    if not isinstance(value, (list, tuple)):
        errors.append(f"{field}: must be a list of channels")
        return list(default)
    out: list[str] = []
    for x in value:
        c = str(x).strip().lower()
        if not c:
            continue
        if c not in NOTIFY_CHANNELS or (c == "digest" and not allow_digest):
            errors.append(f"{field}: unknown channel {c!r} (known: {', '.join(NOTIFY_CHANNELS)})")
            continue
        if c not in out:
            out.append(c)
    return out


def normalize_sphere(raw: Any, base: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], list[str]]:
    """Validate ``raw`` over ``base`` (missing keys keep ``base``'s value). Returns ``(sphere, errors)``;
    a field with an error keeps the base value, so a lenient caller can still use the result."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return blank_sphere("sphere"), ["a sphere must be an object"]
    sid = str(raw.get("id") if raw.get("id") is not None else (base or {}).get("id", "")).strip().lower()
    if not _SLUG.match(sid):
        errors.append("id: use 1-32 characters a-z, 0-9, '-' or '_', starting with a letter or digit")
        sid = sid[:32] if _SLUG.match(sid[:32] or "!") else "sphere"
    b = copy.deepcopy(base) if base else blank_sphere(sid)
    out: dict[str, Any] = {"id": sid}
    # name
    name = raw.get("name", None)
    bname = b.get("name") or {"es": sid.title(), "en": sid.title()}
    if name is None:
        out["name"] = dict(bname)
    else:
        if isinstance(name, str):
            name = {"es": name, "en": name}
        if isinstance(name, dict):
            es = str(name.get("es") or "").strip()[:60]
            en = str(name.get("en") or "").strip()[:60]
            if not es and not en:
                errors.append("name: needs a Spanish or English name")
                out["name"] = dict(bname)
            else:
                out["name"] = {"es": es or en, "en": en or es}
        else:
            errors.append("name: must be text or {es, en}")
            out["name"] = dict(bname)
    # colour
    color = raw.get("color", None)
    if color is None:
        out["color"] = b.get("color") or "#8a8f98"
    elif isinstance(color, str) and _COLOR.match(color.strip()):
        out["color"] = color.strip().lower()
    else:
        errors.append("color: use #rgb or #rrggbb")
        out["color"] = b.get("color") or "#8a8f98"
    for key in ("mail_accounts", "chat_sources", "vip", "keywords", "mute", "apps"):
        out[key] = _str_list(raw.get(key), key, errors, b.get(key) or [])
    # quiet hours
    qh_raw = raw.get("quiet_hours", None)
    bq = b.get("quiet_hours") or {"start": "22:30", "end": "08:00", "days": "daily"}
    if qh_raw is None:
        out["quiet_hours"] = dict(bq)
    elif isinstance(qh_raw, dict):
        out["quiet_hours"] = {
            "start": _hhmm(qh_raw.get("start"), errors, "quiet_hours.start", bq.get("start", ""), allow_empty=True),
            "end": _hhmm(qh_raw.get("end"), errors, "quiet_hours.end", bq.get("end", ""), allow_empty=True),
            "days": _canon_days(qh_raw.get("days"), errors, "quiet_hours.days", bq.get("days", "daily")),
        }
    else:
        errors.append("quiet_hours: must be {start, end, days}")
        out["quiet_hours"] = dict(bq)
    # notify routing
    n_raw = raw.get("notify", None)
    bn = b.get("notify") or {}
    if n_raw is None:
        out["notify"] = {p: list(bn.get(p) or []) for p in PRIORITIES}
    elif isinstance(n_raw, dict):
        out["notify"] = {p: _channels(n_raw.get(p), f"notify.{p}", errors, bn.get(p) or []) for p in PRIORITIES}
        for p in n_raw:
            if p not in PRIORITIES:
                errors.append(f"notify.{p}: unknown priority (use {', '.join(PRIORITIES)})")
    else:
        errors.append("notify: must map urgent/high/normal/low to channel lists")
        out["notify"] = {p: list(bn.get(p) or []) for p in PRIORITIES}
    # digest
    d_raw = raw.get("digest", None)
    bd = b.get("digest") or {}
    if d_raw is None:
        out["digest"] = dict(bd) if bd else blank_sphere(sid)["digest"]
    elif isinstance(d_raw, dict):
        out["digest"] = {
            "enabled": _to_bool(d_raw.get("enabled"), bool(bd.get("enabled", True))),
            "at": _hhmm(d_raw.get("at"), errors, "digest.at", bd.get("at", "08:30")),
            "days": _canon_days(d_raw.get("days"), errors, "digest.days", bd.get("days", "daily")),
            "summarize": _to_bool(d_raw.get("summarize"), bool(bd.get("summarize", True))),
            "channels": _channels(d_raw.get("channels"), "digest.channels", errors, bd.get("channels") or ["windows"],
                                  allow_digest=False),
        }
    else:
        errors.append("digest: must be {enabled, at, days, summarize, channels}")
        out["digest"] = dict(bd) if bd else blank_sphere(sid)["digest"]
    ordered = ("id", "name", "color", "mail_accounts", "chat_sources", "vip", "keywords", "mute",
               "quiet_hours", "notify", "digest", "apps")
    return {k: out[k] for k in ordered}, errors


# -- the facet --------------------------------------------------------------------------------------------

class SpheresFacet(Facet):
    id = "spheres"
    ui_scripts = ("spheres.js",)

    def __init__(self, hub: Any):
        super().__init__(hub)
        self.path = os.path.join(hub.config.data_dir, "spheres.json")
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"active": DEFAULT_SPHERE, "spheres": default_spheres()}
        self._sig: Optional[tuple[int, int]] = None
        self._load(force=True)

    # -- storage ---------------------------------------------------------------------------------------
    def _file_sig(self) -> Optional[tuple[int, int]]:
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self, force: bool = False) -> None:
        with self._lock:
            sig = self._file_sig()
            if not force and sig == self._sig:
                return
            self._sig = sig
            if sig is None:
                self._state = {"active": DEFAULT_SPHERE, "spheres": default_spheres()}
                return
            try:
                with open(self.path, "r", encoding="utf-8-sig") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                logger.warning("spheres.json is unreadable; using the defaults until it is saved again")
                self._state = {"active": DEFAULT_SPHERE, "spheres": default_spheres()}
                return
            spheres: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in (raw.get("spheres") if isinstance(raw, dict) else None) or []:
                if not isinstance(item, dict) or not _SLUG.match(str(item.get("id") or "").strip().lower()):
                    continue
                s, _ = normalize_sphere(item, base=blank_sphere(str(item["id"]).strip().lower()))
                if s["id"] not in seen:
                    seen.add(s["id"])
                    spheres.append(s)
            if DEFAULT_SPHERE not in seen:
                spheres.insert(0, default_spheres()[0])
            active = str((raw.get("active") if isinstance(raw, dict) else "") or DEFAULT_SPHERE)
            if active not in {s["id"] for s in spheres}:
                active = DEFAULT_SPHERE
            self._state = {"active": active, "spheres": spheres}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, self.path)
        self._sig = self._file_sig()

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        try:
            self.hub.events.emit(type_, data, source="hub")
        except Exception:  # noqa: BLE001 - the bus is best effort
            logger.debug("could not emit %s", type_, exc_info=True)

    # -- public API ------------------------------------------------------------------------------------
    def active(self) -> str:
        with self._lock:
            self._load()
            return self._state["active"]

    def get(self, sphere_id: Any = None, *args: Any) -> Optional[dict[str, Any]]:  # type: ignore[override]
        """The sphere with that id (a copy) or None. (``Facet.get(req)`` is the HTTP hook; a Request is routed there.)"""
        if isinstance(sphere_id, Request):
            return self._http_get(sphere_id)
        sid = str(sphere_id or "").strip().lower()
        with self._lock:
            self._load()
            for s in self._state["spheres"]:
                if s["id"] == sid:
                    return copy.deepcopy(s)
        return None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load()
            return copy.deepcopy(self._state["spheres"])

    def ids(self) -> list[str]:
        with self._lock:
            self._load()
            return [s["id"] for s in self._state["spheres"]]

    def set_active(self, sphere_id: str) -> dict[str, Any]:
        sid = str(sphere_id or "").strip().lower()
        with self._lock:
            self._load()
            if sid not in {s["id"] for s in self._state["spheres"]}:
                return {"ok": False, "status": 404, "error": f"unknown sphere: {sphere_id}", "spheres": self.ids()}
            prev = self._state["active"]
            changed = prev != sid
            if changed:
                self._state["active"] = sid
                self._save()
        if changed:
            self._emit("hub.sphere.changed", {"from": prev, "to": sid})
        return {"ok": True, "active": sid, "from": prev, "changed": changed}

    def upsert(self, raw: Any) -> dict[str, Any]:
        """Add or update one sphere (validated; keys left out keep their value)."""
        if not isinstance(raw, dict):
            return {"ok": False, "status": 400, "error": "sphere must be an object"}
        sid = str(raw.get("id") or "").strip().lower()
        with self._lock:
            self._load()
            existing = next((s for s in self._state["spheres"] if s["id"] == sid), None)
            sphere, errors = normalize_sphere(raw, base=existing or blank_sphere(sid))
            if errors:
                return {"ok": False, "status": 400, "error": "; ".join(errors), "errors": errors}
            if existing is None:
                self._state["spheres"].append(sphere)
            else:
                self._state["spheres"] = [sphere if s["id"] == sid else s for s in self._state["spheres"]]
            self._save()
            return {"ok": True, "sphere": copy.deepcopy(sphere), "active": self._state["active"],
                    "spheres": copy.deepcopy(self._state["spheres"])}

    def save_all(self, raw_list: Any) -> dict[str, Any]:
        """Replace the whole list (every entry validated; ``personal`` cannot be dropped)."""
        if not isinstance(raw_list, list) or not raw_list:
            return {"ok": False, "status": 400, "error": "spheres must be a non-empty list"}
        out: list[dict[str, Any]] = []
        errors: list[str] = []
        with self._lock:
            self._load()
            old = {s["id"]: s for s in self._state["spheres"]}
            for item in raw_list:
                sid = str((item or {}).get("id") or "").strip().lower() if isinstance(item, dict) else ""
                s, errs = normalize_sphere(item, base=old.get(sid) or blank_sphere(sid))
                errors += [f"{sid or '?'}: {e}" for e in errs]
                if s["id"] in {x["id"] for x in out}:
                    errors.append(f"{s['id']}: duplicated id")
                out.append(s)
            if errors:
                return {"ok": False, "status": 400, "error": "; ".join(errors), "errors": errors}
            if DEFAULT_SPHERE not in {s["id"] for s in out}:
                out.insert(0, old.get(DEFAULT_SPHERE) or default_spheres()[0])
            prev = self._state["active"]
            self._state["spheres"] = out
            if prev not in {s["id"] for s in out}:
                self._state["active"] = DEFAULT_SPHERE
            now_active = self._state["active"]
            self._save()
            res = {"ok": True, "active": now_active, "spheres": copy.deepcopy(out)}
        if now_active != prev:
            self._emit("hub.sphere.changed", {"from": prev, "to": now_active})
        return res

    def remove(self, sphere_id: str) -> dict[str, Any]:
        sid = str(sphere_id or "").strip().lower()
        if sid == DEFAULT_SPHERE:
            return {"ok": False, "status": 400, "error": "the personal sphere cannot be removed"}
        with self._lock:
            self._load()
            if sid not in {s["id"] for s in self._state["spheres"]}:
                return {"ok": False, "status": 404, "error": f"unknown sphere: {sphere_id}"}
            prev = self._state["active"]
            self._state["spheres"] = [s for s in self._state["spheres"] if s["id"] != sid]
            if prev == sid:
                self._state["active"] = DEFAULT_SPHERE
            now_active = self._state["active"]
            self._save()
            res = {"ok": True, "removed": sid, "active": now_active, "spheres": copy.deepcopy(self._state["spheres"])}
        if now_active != prev:
            self._emit("hub.sphere.changed", {"from": prev, "to": now_active})
        return res

    # -- routing of mail, chats and apps -----------------------------------------------------------------
    def sphere_of_account(self, selector: str, address: str = "") -> str:
        """The sphere that claims a mail account: explicit lists first (matched on the selector or the
        address), then the first sphere with ``"*"``, else ``personal``."""
        keys = {str(selector or "").strip().lower(), str(address or "").strip().lower()} - {""}
        with self._lock:
            self._load()
            spheres = self._state["spheres"]
            if keys:
                for s in spheres:
                    for entry in s["mail_accounts"]:
                        e = entry.strip().lower()
                        if e != "*" and e in keys:
                            return s["id"]
            for s in spheres:
                if any(e.strip() == "*" for e in s["mail_accounts"]):
                    return s["id"]
        return DEFAULT_SPHERE

    def sphere_of_chat_source(self, source_id: str) -> str:
        sid = str(source_id or "").strip().lower()
        with self._lock:
            self._load()
            for s in self._state["spheres"]:
                if sid and any(e.strip().lower() == sid for e in s["chat_sources"]):
                    return s["id"]
            for s in self._state["spheres"]:
                if any(e.strip() == "*" for e in s["chat_sources"]):
                    return s["id"]
        return DEFAULT_SPHERE

    def sphere_of_app(self, app_id: str) -> str:
        """The sphere a notification of ``app_id`` belongs to: the first whose ``apps`` allows it, else the active one."""
        with self._lock:
            self._load()
            for s in self._state["spheres"]:
                if self._allows(s, app_id):
                    return s["id"]
            return self._state["active"]

    @staticmethod
    def _allows(sphere: dict[str, Any], app_id: str) -> bool:
        apps = [str(a).strip().lower() for a in sphere.get("apps") or []]
        return "*" in apps or str(app_id or "").strip().lower() in apps

    def app_allowed(self, sphere_id: str, app_id: str) -> bool:
        """May ``app_id`` see this sphere's mail and chats? (``"*"`` = every app; an unknown sphere allows none.)"""
        s = self.get(sphere_id)
        return bool(s) and self._allows(s, app_id)

    # -- classification -----------------------------------------------------------------------------------
    def classify(self, sphere_id: str, *, sender: str, subject: str = "", text: str = "",
                 mentions_me: bool = False, direct: bool = False) -> dict[str, Any]:
        """``{"priority": "attention"|"normal"|"low", "reasons": [...]}`` for one message.

        A VIP sender is always attention; a muted sender (address, ``@domain`` or a word of the name) is low;
        then keywords, a mention or a direct message make it attention; a muted word makes it low."""
        s = self.get(sphere_id) or blank_sphere(str(sphere_id or ""))
        sender = str(sender or "")
        for entry in s["vip"]:
            if sender_matches(entry, sender):
                return {"priority": "attention", "reasons": [f"vip:{entry}"]}
        mute_senders = [e for e in s["mute"] if e.startswith("@") or "@" in e]
        for entry in mute_senders:
            if sender_matches(entry, sender):
                return {"priority": "low", "reasons": [f"mute:{entry}"]}
        hay = fold(f"{subject}\n{str(text or '')[:_SCAN_CHARS]}")
        reasons: list[str] = []
        for kw in s["keywords"]:
            if _has_word(hay, fold(kw)):
                reasons.append(f"keyword:{kw}")
        if mentions_me:
            reasons.append("mention")
        if direct:
            reasons.append("direct")
        if reasons:
            return {"priority": "attention", "reasons": reasons}
        sender_fold = fold(sender)
        for entry in s["mute"]:
            if entry in mute_senders:
                continue
            w = fold(entry)
            if _has_word(hay, w) or _has_word(sender_fold, w):
                return {"priority": "low", "reasons": [f"mute:{entry}"]}
        return {"priority": "normal", "reasons": []}

    def in_quiet_hours(self, sphere_id: str, now: Optional[datetime] = None) -> bool:
        """Is ``now`` (default: the local time) inside the sphere's quiet hours? A window that crosses
        midnight belongs to the day it starts on; ``start == end`` or an empty time turns it off."""
        s = self.get(sphere_id)
        if not s:
            return False
        q = s["quiet_hours"]
        a, b = _HHMM.match(str(q.get("start") or "")), _HHMM.match(str(q.get("end") or ""))
        if not a or not b:
            return False
        start, end = int(a.group(1)) * 60 + int(a.group(2)), int(b.group(1)) * 60 + int(b.group(2))
        if start == end:
            return False
        now = now or datetime.now()
        minute = now.hour * 60 + now.minute
        days = parse_days(q.get("days", "daily"))
        if start < end:
            return start <= minute < end and now.weekday() in days
        if minute >= start:
            return now.weekday() in days
        if minute < end:
            return (now - timedelta(days=1)).weekday() in days
        return False

    # -- HTTP ------------------------------------------------------------------------------------------------
    def _payload(self) -> dict[str, Any]:
        return {"ok": True, "active": self.active(), "spheres": self.list()}

    def _http_get(self, req: Request) -> Optional[Any]:
        if req.method == "GET" and req.path == "/api/spheres":
            return self._payload()
        return None

    def post(self, req: Request) -> Optional[Any]:
        if not req.path.startswith("/api/spheres"):
            return None
        who = req.caller()
        if who is None:
            return {"ok": False, "status": 401, "error": "a family bearer token is required"}
        body = req.body or {}
        if req.path == "/api/spheres/active":
            return self.set_active(str(body.get("id") or ""))
        if who not in ("ui", "hub"):
            return {"ok": False, "status": 403, "error": "only the hub's page or the hub token may edit spheres"}
        if req.path == "/api/spheres":
            if isinstance(body.get("spheres"), list):
                return self.save_all(body["spheres"])
            if isinstance(body.get("sphere"), dict):
                return self.upsert(body["sphere"])
            return {"ok": False, "status": 400, "error": "send {spheres: [...]} or {sphere: {...}}"}
        if req.path == "/api/spheres/remove":
            return self.remove(str(body.get("id") or ""))
        return None

    # -- tools -----------------------------------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": "hub_spheres",
                "description": "Show the person's spheres (personal, work) and the active one / esferas y cuál está activa.\n"
                               "Each sphere: name, colour, mail accounts, VIP, keywords, quiet hours, routing, digest, apps. "
                               "Keywords: esfera, trabajo, personal, contexto, horas de silencio.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "hub_sphere_set",
                "description": "Switch the active sphere (personal or work) / cambiar de esfera: modo trabajo o personal.\n"
                               "Keywords: pasa a trabajo, modo personal, switch context. id from hub_spheres.",
                "inputSchema": {"type": "object", "properties": {
                    "id": {"type": "string", "description": "Sphere id as listed by hub_spheres (e.g. 'personal', 'work')."}},
                    "required": ["id"], "additionalProperties": False},
            },
        ]

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "hub_spheres": lambda a: self._payload(),
            "hub_sphere_set": lambda a: self.set_active(str(a.get("id") or "")),
        }
