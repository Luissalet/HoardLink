"""The mail gateway (facet ``mailgate``): ONE pass over the inbox for the whole family.

Ledger, Tantalus, Phileas, Kafka and JobHunter each used to run their own helper with targeted IMAP searches.
Here the hub does one incremental pass (``mail_helper.py`` run with Faustus's own Python, so the password never
leaves Faustus), stores what it reads in ``<data>/mail.db`` and lets every app ask for the messages that match the
interest it registered. Apps keep their own helper as a fallback for when the hub is not there.

* A message belongs to a *sphere* (``spheres`` facet: personal / work / …). An app only ever sees messages of a
  sphere whose ``apps`` allows it: work mail never reaches Ledger.
* Each message gets a priority (``spheres.classify``: attention / normal / low), the list of apps whose interest it
  matches, and is announced on the bus as ``mail.received`` (ids and a 120-character subject, never the text).
* "Needs you" = priority attention, not dismissed, not claimed. "Sin dueño" = nobody claimed it (promo, social,
  security, dev, other).
* ``<data>/mail.json``: ``{enabled, interval_min, retention_days, owner, max_per_pass, since_days}``. ``enabled``
  is **false** until the person turns it on: a false value means the hub never reads mail (sending notification
  mails through ``send_mail`` does not read anything and keeps working).

The ``messages`` table also holds chat messages (``kind = 'chat'``, filled by the ``chats`` facet through
``store.insert_message``).

0.8 (the commons): a stored message may also carry ``html`` (the raw HTML part, at most 240000 characters, kept only for mail with
structured markup: schema.org / JSON-LD), ``images`` (``[{alt, src}]``) and ``headers`` (``{list_unsubscribe, one_click,
gmail_category, message_id}``). They are returned only when the caller asks (``fields=html,images,headers`` on
``GET /api/mail/messages[/<id>]``), so the default answer is the one it always was. An interest spec may also carry ``exclude``,
``all_of`` and ``category`` (see :func:`normalize_spec` / :func:`match_interest`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

from .. import fam_mail, mail_helper as _mail_helper
from .config import REPO_DIR  # noqa: F401 - kept for callers that import it from here
from .facets import Facet, Reply, Request

logger = logging.getLogger("hoard_hub.mailgate")

HELPER = Path(__file__).resolve().parent.parent / "mail_helper.py"        # the one helper of the whole family (hoard_link/mail_helper.py)
HELPER_TIMEOUT_S = 180
STATUS_TTL_S = 300.0
PRIORITIES = ("attention", "normal", "low")
DEFAULT_CONFIG: dict[str, Any] = {"enabled": False, "interval_min": 10, "retention_days": 60, "owner": "", "max_per_pass": 300,
                                  "since_days": 14}
_SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_HTML = 240_000
MAX_IMAGES = 40
FIELDS = ("html", "images", "headers")
CATEGORIES = ("promo", "social", "security", "dev", "other")
_CATEGORY_ALIASES = {"promotion": "promo", "promotions": "promo", "promos": "promo", "marketing": "promo", "newsletter": "promo",
                     "forums": "social", "updates": "other"}
_SPEC_LISTS = ("subject_terms", "from_domains", "from_addresses", "text_terms")


# ---------------------------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------------------------
def fold(text: Any) -> str:
    """Lower-case and without accents: the comparison form of every term."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def domain_of(address: str) -> str:
    a = str(address or "").strip().lower()
    if "<" in a and ">" in a:
        a = a[a.rfind("<") + 1:a.rfind(">")]
    return a.rsplit("@", 1)[1].strip(" >") if "@" in a else ""


def _clean_list(value: Any, *, limit: int = 60, width: int = 120) -> list[str]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\n;]", value)]
    out: list[str] = []
    for item in value or []:
        s = str(item).strip()[:width]
        if s and s not in out:
            out.append(s)
    return out[:limit]


def _normalize_any(spec: dict[str, Any]) -> dict[str, Any]:
    """The any-of criteria of one (sub-)spec: ``subject_terms, from_domains, from_addresses, text_terms, regex, has_attachment``."""
    regex = str(spec.get("regex") or "").strip()[:500]
    if regex:
        try:
            re.compile(regex)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    return {"subject_terms": _clean_list(spec.get("subject_terms")), "from_domains": _clean_list(spec.get("from_domains")),
            "from_addresses": _clean_list(spec.get("from_addresses")), "text_terms": _clean_list(spec.get("text_terms")),
            "regex": regex, "has_attachment": bool(spec.get("has_attachment"))}


def _normalize_categories(value: Any) -> list[str]:
    out: list[str] = []
    for item in _clean_list(value, limit=10, width=40):
        name = _CATEGORY_ALIASES.get(fold(item).strip(), fold(item).strip())
        if name not in CATEGORIES:
            raise ValueError(f"unknown category {item!r} (use {', '.join(CATEGORIES)})")
        if name not in out:
            out.append(name)
    return out


def _has_any(spec: dict[str, Any]) -> bool:
    return bool(any(spec.get(k) for k in _SPEC_LISTS) or spec.get("regex") or spec.get("has_attachment"))


def normalize_spec(spec: Any, _nested: bool = False) -> dict[str, Any]:
    """The interest an app registers: ``subject_terms, from_domains, from_addresses, text_terms, regex, has_attachment`` (a message
    matches when ANY of them does), plus three optional extras that only appear in the result when they are used:

    * ``exclude``: a spec with the same keys (and ``category``); a message that matches it is dropped;
    * ``all_of``: a list of specs (any-of rule each, ``exclude`` and ``category`` allowed, no further ``all_of``) that must ALL match;
      together with the plain criteria both must hold;
    * ``category``: ``promo | social | security | dev | other`` (string or list): only messages :func:`guess_category` puts there.
    """
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    out = _normalize_any(spec)
    cats = _normalize_categories(spec.get("category"))
    if cats:
        out["category"] = cats
    raw_ex = spec.get("exclude")
    if raw_ex not in (None, "", {}, []):
        if not isinstance(raw_ex, dict):
            raise ValueError("exclude must be an object")
        ex = _normalize_any(raw_ex)
        ex_cats = _normalize_categories(raw_ex.get("category"))
        if _has_any(ex) or ex_cats:
            out["exclude"] = {**ex, **({"category": ex_cats} if ex_cats else {})}
    raw_all = spec.get("all_of")
    if raw_all not in (None, "", [], {}):
        if _nested:
            raise ValueError("all_of cannot be nested")
        if not isinstance(raw_all, list):
            raise ValueError("all_of must be a list of specs")
        subs = []
        for sub in raw_all[:10]:
            norm = normalize_spec(sub, _nested=True)
            if _has_any(norm) or norm.get("category"):
                subs.append(norm)
        if subs:
            out["all_of"] = subs
    return out


def _matches_any(spec: dict[str, Any], msg: dict[str, Any]) -> bool:
    """True when ANY non-empty criterion of ``spec`` matches (case-insensitive, accents folded)."""
    sender = str(msg.get("from_addr") or msg.get("from_address") or "").strip().lower()
    sender_dom = domain_of(sender)
    subject = str(msg.get("subject") or "")
    text = str(msg.get("text") or "")
    f_subject, f_text = fold(subject), fold(text)
    for term in spec.get("subject_terms") or []:
        t = fold(term).strip()
        if t and t in f_subject:
            return True
    for term in spec.get("text_terms") or []:
        t = fold(term).strip()
        if t and (t in f_text or t in f_subject):
            return True
    for dom in spec.get("from_domains") or []:
        d = str(dom).strip().lower().lstrip("@")
        if d and sender_dom and (sender_dom == d or sender_dom.endswith("." + d)):
            return True
    for addr in spec.get("from_addresses") or []:
        a = str(addr).strip().lower()
        if a and a == sender:
            return True
    pattern = str(spec.get("regex") or "")
    if pattern:
        try:
            rx = re.compile(pattern, re.I)
            hay = subject + "\n" + text[:30000]
            if rx.search(hay) or rx.search(fold(hay)):
                return True
        except re.error:
            pass
    if spec.get("has_attachment") and (msg.get("attachments") or []):
        return True
    return False


def _as_categories(value: Any) -> list[str]:
    return [str(c) for c in (value if isinstance(value, (list, tuple)) else [value] if value else []) if c]


def match_interest(spec: dict[str, Any], msg: dict[str, Any]) -> bool:
    """Does ``msg`` match the interest ``spec``?

    * the plain criteria (``subject_terms, text_terms, from_domains, from_addresses, regex, has_attachment``): ANY non-empty one matches
      (case-insensitive, accents folded);
    * ``all_of`` (list of sub-specs): every sub-spec must match; with plain criteria too, both must hold;
    * ``category``: the message's :func:`guess_category` must be one of them (alone, it selects those categories);
    * ``exclude`` (same keys as a spec, and ``category``): a match drops the message whatever else matched.

    ``msg``: ``from_addr`` (or ``from_address``), ``from_name``, ``subject``, ``text``, ``attachments``.
    """
    ex = spec.get("exclude")
    if isinstance(ex, dict) and ex:
        ex_cats = _as_categories(ex.get("category"))
        if _matches_any(ex, msg) or (ex_cats and guess_category(msg) in ex_cats):
            return False
    cats = _as_categories(spec.get("category"))
    if cats and guess_category(msg) not in cats:
        return False
    subs = [x for x in (spec.get("all_of") or []) if isinstance(x, dict) and x]
    plain = _has_any(spec)
    if not plain and not subs:
        return bool(cats)
    if plain and not _matches_any(spec, msg):
        return False
    return all(match_interest(x, msg) for x in subs)


_CATEGORY_RULES: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = [
    ("security",
     re.compile(r"security|seguridad|accounts?\.google|noreply@.*(verif|auth)|no-reply@.*(verif|auth)"),
     re.compile(r"security|seguridad|verif|c[oó]digo (de )?(verific|acceso|seguridad)|verification code|one[- ]time|\botp\b|password|contrase[nñ]a|"
                r"sign[- ]?in|log[- ]?in|inicio de sesi[oó]n|2fa|two[- ]factor|suspicious|sospechos|nuevo dispositivo|new device|acceso no")),
    ("dev",
     re.compile(r"github|gitlab|bitbucket|npmjs|pypi|vercel|netlify|cloudflare|docker|sentry|stackoverflow|jetbrains|atlassian|jira|linear\.app|"
                r"huggingface|amazonaws|aws\.amazon|digitalocean|heroku|render\.com|readthedocs|codecov|circleci|travis"),
     re.compile(r"pull request|merge request|\bcommit|build (failed|passed|succeeded)|deploy|\bissue\b|workflow run|\bci\b|pipeline|dependabot|"
                r"release v?\d|\bversion\b.*released|security advisory")),
    ("social",
     re.compile(r"facebook|linkedin|twitter|(^|[.@])x\.com|instagram|tiktok|youtube|pinterest|reddit|discord|twitch|telegram|whatsapp|snapchat|"
                r"mastodon|tumblr|quora|medium\.com|substack|meetup|threads\.net|bsky"),
     re.compile(r"te ha (etiquetado|mencionado|seguido)|new follower|nuevo seguidor|connection request|solicitud de conexi[oó]n|"
                r"te ha enviado un mensaje|mentioned you|tagged you|commented on|ha comentado|invitaci[oó]n a conectar")),
    ("promo",
     re.compile(r"newsletter|marketing|promo|offers?@|deals?@|ofertas?@|boletin|mailing|campaign|news@|hola@|hello@|shop@|store@|info@"),
     re.compile(r"oferta|descuento|rebajas|\d+ ?% ?(dto|off|de descuento)|black friday|cyber monday|newsletter|bolet[ií]n|novedades|\bsale\b|"
                r"\bdeals?\b|coupon|cup[oó]n|promo|gratis|free shipping|env[ií]o gratis|[uú]ltimas unidades|unsubscribe|darte de baja|"
                r"no te lo pierdas|don't miss|last chance|[uú]ltima oportunidad|solo hoy|only today")),
]


def guess_category(msg: dict[str, Any]) -> str:
    """promo / social / security / dev / other, by sender and subject (cheap heuristics for the "sin dueño" tray)."""
    sender = fold(str(msg.get("from_addr") or msg.get("from_address") or "") + " " + str(msg.get("from_name") or ""))
    subject = fold(msg.get("subject"))
    for name, sender_rx, subject_rx in _CATEGORY_RULES:
        if sender_rx.search(sender) or subject_rx.search(subject):
            return name
    return "other"


def _snippet(text: str, width: int = 240) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:width]


def _loads(raw: Any, default: Any) -> Any:
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return v if v is not None else default


# ---------------------------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'mail', source TEXT NOT NULL DEFAULT '', sphere TEXT NOT NULL DEFAULT 'personal',
  folder TEXT NOT NULL DEFAULT '', uid INTEGER NOT NULL DEFAULT 0, message_id TEXT NOT NULL UNIQUE, thread TEXT NOT NULL DEFAULT '',
  date_ts REAL NOT NULL DEFAULT 0, from_addr TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '', to_json TEXT NOT NULL DEFAULT '[]',
  subject TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', links_json TEXT NOT NULL DEFAULT '[]',
  attachments_json TEXT NOT NULL DEFAULT '[]', fetched_ts REAL NOT NULL DEFAULT 0, priority TEXT NOT NULL DEFAULT 'normal',
  reasons_json TEXT NOT NULL DEFAULT '[]', interests_json TEXT NOT NULL DEFAULT '[]', dismissed INTEGER NOT NULL DEFAULT 0,
  search_text TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS messages_date ON messages(date_ts);
CREATE INDEX IF NOT EXISTS messages_kind ON messages(kind, sphere, priority);
CREATE TABLE IF NOT EXISTS claims(mail_id INTEGER NOT NULL, app TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL DEFAULT 0, UNIQUE(mail_id, app));
CREATE TABLE IF NOT EXISTS interests(app TEXT PRIMARY KEY, sphere TEXT NOT NULL DEFAULT '', spec_json TEXT NOT NULL DEFAULT '{}',
  updated_ts REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT 'null');
"""


#: Columns added after the first release of the table (0.8): applied to an existing ``mail.db`` without touching its rows.
_MIGRATIONS = (("html", "TEXT NOT NULL DEFAULT ''"), ("images_json", "TEXT NOT NULL DEFAULT '[]'"),
               ("headers_json", "TEXT NOT NULL DEFAULT '{}'"))
#: Every column but ``html`` (raw HTML parts are big: they are only read when a caller asks for them).
_COLS = ("id, kind, source, sphere, folder, uid, message_id, thread, date_ts, from_addr, from_name, to_json, subject, snippet, text, "
         "links_json, attachments_json, fetched_ts, priority, reasons_json, interests_json, dismissed, search_text, images_json, headers_json")


def _migrate(db: sqlite3.Connection) -> None:
    have = {r[1] for r in db.execute("PRAGMA table_info(messages)").fetchall()}
    for name, decl in _MIGRATIONS:
        if name not in have:
            try:
                db.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:          # another process added it first
                pass
    db.commit()


def clean_images(value: Any) -> list[dict[str, str]]:
    """``[{alt, src}]`` (at most 40, strings only) from whatever a helper sent."""
    out: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            alt, src = str(item.get("alt") or "")[:160], str(item.get("src") or "")[:500]
            if alt or src:
                out.append({"alt": alt, "src": src})
        if len(out) >= MAX_IMAGES:
            break
    return out


def clean_headers(value: Any) -> dict[str, Any]:
    """``{list_unsubscribe, one_click, gmail_category, message_id}``: only these keys, only these types."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, width in (("list_unsubscribe", 800), ("gmail_category", 40), ("message_id", 400)):
        if value.get(key):
            out[key] = str(value[key])[:width]
    if value.get("one_click"):
        out["one_click"] = True
    return out


def _like(token: str) -> str:
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class MailStore:
    """``<data>/mail.db``: messages (mail and chat), claims, interests and the watermarks."""

    def __init__(self, path: str, *, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._db.executescript(_SCHEMA)
            _migrate(self._db)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # -- writing -----------------------------------------------------------
    def insert_message(self, *, kind: str = "mail", source: str = "", sphere: str = "personal", folder: str = "", uid: int = 0,
                       message_id: str = "", thread: str = "", date_ts: Optional[float] = None, from_addr: str = "",
                       from_name: str = "", to: Any = None, subject: str = "", snippet: str = "", text: str = "",
                       links: Any = None, attachments: Any = None, priority: str = "normal", reasons: Any = None,
                       interests: Any = None, fetched_ts: Optional[float] = None, html: str = "", images: Any = None,
                       headers: Any = None) -> dict[str, Any]:
        """Store one message. ``{"id", "created"}``; a ``message_id`` already stored is not stored twice (``created`` False).
        ``html`` (cut at 240000 characters), ``images`` and ``headers`` are the 0.8 extras."""
        now = float(fetched_ts or self.clock())
        mid = str(message_id or "").strip()[:400] or f"local:{uuid.uuid4().hex}"
        text = str(text or "")
        if priority not in PRIORITIES:
            priority = "normal"
        snippet = str(snippet or "") or _snippet(text)
        search = fold(" ".join([str(subject), str(from_name), str(from_addr), snippet, text[:4000]]))
        row = (kind if kind in ("mail", "chat") else "mail", str(source)[:120], str(sphere or "personal")[:60], str(folder)[:300], int(uid or 0), mid,
               str(thread or "")[:400], float(date_ts or now), str(from_addr)[:300], str(from_name)[:200],
               json.dumps(to if to is not None else [], ensure_ascii=False), str(subject)[:600], snippet[:400], text,
               json.dumps(links or [], ensure_ascii=False), json.dumps(attachments or [], ensure_ascii=False), now, priority,
               json.dumps(list(reasons or []), ensure_ascii=False), json.dumps(list(interests or []), ensure_ascii=False), search,
               str(html or "")[:MAX_HTML], json.dumps(clean_images(images), ensure_ascii=False),
               json.dumps(clean_headers(headers), ensure_ascii=False))
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO messages(kind, source, sphere, folder, uid, message_id, thread, date_ts, from_addr, from_name, to_json,"
                " subject, snippet, text, links_json, attachments_json, fetched_ts, priority, reasons_json, interests_json, dismissed, search_text,"
                " html, images_json, headers_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)", row)
            self._db.commit()
            if cur.rowcount:
                return {"id": int(cur.lastrowid), "created": True}
            existing = self._db.execute("SELECT id FROM messages WHERE message_id = ?", (mid,)).fetchone()
        return {"id": int(existing["id"]) if existing else 0, "created": False}

    def set_interests_of(self, mail_id: int, apps: list[str]) -> None:
        with self._lock:
            self._db.execute("UPDATE messages SET interests_json = ? WHERE id = ?", (json.dumps(apps), int(mail_id)))
            self._db.commit()

    def dismiss(self, ids: Iterable[int], undo: bool = False) -> int:
        ids = [int(i) for i in ids if str(i).lstrip("-").isdigit()]
        if not ids:
            return 0
        with self._lock:
            cur = self._db.execute(f"UPDATE messages SET dismissed = ? WHERE id IN ({','.join('?' * len(ids))})", [0 if undo else 1, *ids])
            self._db.commit()
            return cur.rowcount

    def claim(self, app: str, ids: Iterable[int], kind: str, ref: str) -> int:
        n = 0
        with self._lock:
            for i in ids:
                if not str(i).lstrip("-").isdigit():
                    continue
                exists = self._db.execute("SELECT 1 FROM messages WHERE id = ?", (int(i),)).fetchone()
                if not exists:
                    continue
                self._db.execute("INSERT OR REPLACE INTO claims(mail_id, app, kind, ref, ts) VALUES(?,?,?,?,?)",
                                 (int(i), str(app)[:60], str(kind or "")[:60], str(ref or "")[:400], self.clock()))
                n += 1
            self._db.commit()
        return n

    def set_interest(self, app: str, sphere: str, spec: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO interests(app, sphere, spec_json, updated_ts) VALUES(?,?,?,?)",
                             (app, sphere or "", json.dumps(spec, ensure_ascii=False), self.clock()))
            self._db.commit()

    def remove_interest(self, app: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM interests WHERE app = ?", (app,))
            self._db.commit()
            return bool(cur.rowcount)

    def interests(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM interests ORDER BY app").fetchall()
        return [{"app": r["app"], "sphere": r["sphere"], "spec": _loads(r["spec_json"], {}), "updated_ts": r["updated_ts"]} for r in rows]

    # -- state -------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return _loads(row["value"], default) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO state(key, value) VALUES(?, ?)", (key, json.dumps(value, ensure_ascii=False, default=str)))
            self._db.commit()

    # -- reading -----------------------------------------------------------
    def query(self, *, kind: Optional[str] = None, spheres: Optional[Iterable[str]] = None, source: Optional[str] = None,
              folder: Optional[str] = None, since_id: Optional[int] = None, q: str = "", days: Optional[float] = None,
              interest_app: Optional[str] = None, unclaimed: bool = False, attention: bool = False,
              hide_dismissed: bool = False, limit: int = 50, order: str = "desc", ids: Optional[Iterable[int]] = None,
              with_html: bool = False) -> list[dict[str, Any]]:
        """Raw rows as dicts (JSON columns parsed), each with its ``claims``. ``html`` is only selected with ``with_html``."""
        where, params = ["1=1"], []
        if kind:
            where.append("kind = ?"); params.append(kind)
        if spheres is not None:
            sp = list(spheres)
            if not sp:
                return []
            where.append(f"sphere IN ({','.join('?' * len(sp))})"); params += sp
        if source:
            where.append("source = ?"); params.append(source)
        if folder:
            where.append("folder = ?"); params.append(folder)
        if since_id is not None:
            where.append("id > ?"); params.append(int(since_id))
        if ids is not None:
            idl = [int(i) for i in ids]
            if not idl:
                return []
            where.append(f"id IN ({','.join('?' * len(idl))})"); params += idl
        for token in fold(q).split():
            where.append("search_text LIKE ? ESCAPE '\\'"); params.append(_like(token))
        if days:
            where.append("date_ts >= ?"); params.append(self.clock() - float(days) * 86400)
        if interest_app:
            where.append("instr(interests_json, ?) > 0"); params.append(json.dumps(interest_app))
        if unclaimed or attention:
            where.append("NOT EXISTS (SELECT 1 FROM claims c WHERE c.mail_id = messages.id)")
        if attention:
            where.append("priority = 'attention'")
        if hide_dismissed or attention or unclaimed:
            where.append("dismissed = 0")
        sql = f"SELECT {_COLS}{', html' if with_html else ''} FROM messages WHERE {' AND '.join(where)} ORDER BY " + \
              ("id ASC" if order == "asc" else "date_ts DESC, id DESC") + " LIMIT ?"
        params.append(max(1, min(int(limit or 50), 2000)))
        with self._lock:
            rows = [dict(r) for r in self._db.execute(sql, params).fetchall()]
            claims: dict[int, list[dict[str, Any]]] = {}
            if rows:
                idl = [r["id"] for r in rows]
                for c in self._db.execute(f"SELECT * FROM claims WHERE mail_id IN ({','.join('?' * len(idl))})", idl).fetchall():
                    claims.setdefault(c["mail_id"], []).append({"app": c["app"], "kind": c["kind"], "ref": c["ref"], "ts": c["ts"]})
        for r in rows:
            r["claims"] = claims.get(r["id"], [])
            r["to_raw"] = _loads(r.pop("to_json"), [])
            r["links"] = _loads(r.pop("links_json"), [])
            r["attachments"] = _loads(r.pop("attachments_json"), [])
            r["reasons"] = _loads(r.pop("reasons_json"), [])
            r["interests"] = _loads(r.pop("interests_json"), [])
            r["images"] = _loads(r.pop("images_json", None), [])
            r["headers"] = _loads(r.pop("headers_json", None), {})
            r.pop("search_text", None)
            r["dismissed"] = bool(r["dismissed"])
        return rows

    def get(self, mail_id: int, with_html: bool = False) -> Optional[dict[str, Any]]:
        rows = self.query(ids=[int(mail_id)], limit=1, with_html=with_html)
        return rows[0] if rows else None

    def distinct_spheres(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT sphere FROM messages").fetchall()]

    def counts(self) -> dict[str, int]:
        with self._lock:
            c = self._db
            total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            mail = c.execute("SELECT COUNT(*) FROM messages WHERE kind='mail'").fetchone()[0]
            att = c.execute("SELECT COUNT(*) FROM messages WHERE priority='attention' AND dismissed=0 AND kind='mail' AND NOT EXISTS "
                            "(SELECT 1 FROM claims WHERE mail_id = messages.id)").fetchone()[0]
            unc = c.execute("SELECT COUNT(*) FROM messages WHERE dismissed=0 AND kind='mail' AND NOT EXISTS "
                            "(SELECT 1 FROM claims WHERE mail_id = messages.id)").fetchone()[0]
        return {"total": total, "mail": mail, "chat": total - mail, "attention": att, "unclaimed": unc}

    def message_ids_for_sha(self, sha: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id, sphere, attachments_json FROM messages WHERE attachments_json LIKE ?", (f"%{sha}%",)).fetchall()
        out = []
        for r in rows:
            for a in _loads(r["attachments_json"], []):
                if a.get("sha") == sha:
                    out.append({"id": r["id"], "sphere": r["sphere"], "attachment": a})
        return out

    # -- retention ---------------------------------------------------------
    def prune(self, retention_days: float, attachments_dir: str = "") -> dict[str, int]:
        """Drop ``text``, snippet, links, html, images and attachment files of messages older than ``retention_days``; headers stay."""
        cutoff = self.clock() - float(retention_days) * 86400
        pruned = 0
        with self._lock:
            rows = self._db.execute("SELECT id, subject, from_name, from_addr, attachments_json FROM messages "
                                    "WHERE fetched_ts < ? AND (text != '' OR snippet != '' OR links_json != '[]' OR html != '' OR images_json != '[]' "
                                    "OR attachments_json LIKE '%\"path\"%')",
                                    (cutoff,)).fetchall()
            for r in rows:
                atts = [{**{k: v for k, v in a.items() if k != "path"}, "pruned": True} for a in _loads(r["attachments_json"], [])]
                search = fold(" ".join([r["subject"], r["from_name"], r["from_addr"]]))
                self._db.execute("UPDATE messages SET text = '', snippet = '', links_json = '[]', html = '', images_json = '[]', attachments_json = ?, "
                                 "search_text = ? WHERE id = ?",
                                 (json.dumps(atts, ensure_ascii=False), search, r["id"]))
                pruned += 1
            self._db.commit()
            keep: set[str] = set()
            for r in self._db.execute("SELECT attachments_json FROM messages WHERE attachments_json LIKE '%\"path\"%'").fetchall():
                for a in _loads(r["attachments_json"], []):
                    if a.get("path") and a.get("sha"):
                        keep.add(a["sha"])
        removed = 0
        if attachments_dir and os.path.isdir(attachments_dir):
            for name in os.listdir(attachments_dir):
                stem = name.rsplit(".", 1)[0]
                if _SHA.match(stem) and stem not in keep:
                    try:
                        os.remove(os.path.join(attachments_dir, name))
                        removed += 1
                    except OSError:
                        pass
        return {"pruned": pruned, "files_removed": removed}


# ---------------------------------------------------------------------------------------------
# the facet
# ---------------------------------------------------------------------------------------------
def parse_fields(value: Any) -> list[str]:
    """``html``, ``images``, ``headers`` (``all`` = the three) from a comma string or a list; anything else is ignored."""
    items = re.split(r"[,\s]+", value) if isinstance(value, str) else list(value or [])
    names = [f for f in dict.fromkeys(str(i).strip().lower() for i in items) if f in FIELDS or f == "all"]
    return list(FIELDS) if "all" in names else names


def _bad(status: int, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": error, **extra}


def allowed_spheres(hub: Any, store: MailStore, caller: Optional[str]) -> Optional[set[str]]:
    """The spheres ``caller`` may read: None = all (the page, the hub, the assistant), else a set (maybe empty)."""
    if caller in ("ui", "hub", "hub-tool"):
        return None
    sph = hub.facet("spheres") if hasattr(hub, "facet") else None
    fn = getattr(sph, "app_allowed", None)
    if fn is None:
        return None
    out: set[str] = set()
    known = set(store.distinct_spheres())
    lister = getattr(sph, "list", None)
    if lister is not None:
        try:
            known |= {str(s.get("id")) for s in lister() if isinstance(s, dict)}
        except Exception:  # noqa: BLE001
            pass
    for s in known:
        try:
            if fn(s, caller):
                out.add(s)
        except Exception:  # noqa: BLE001
            continue
    return out


class MailGate(Facet):
    id = "mailgate"
    ui_scripts = ("mail.js",)

    def __init__(self, hub: Any, *, runner: Optional[Callable[..., Any]] = None, clock: Callable[[], float] = time.time):
        super().__init__(hub)
        self.runner = runner or subprocess.run
        self.clock = clock
        self.data_dir = hub.config.data_dir
        self.config_path = os.path.join(self.data_dir, "mail.json")
        self.attachments_dir = os.path.join(self.data_dir, "mail", "attachments")
        self.background = True            # tests switch the pass thread off
        self.first_delay_s = 20.0
        self._store: Optional[MailStore] = None
        self._store_lock = threading.Lock()
        self._pass_lock = threading.Lock()
        self._status_cache: Optional[tuple[float, str, dict[str, Any]]] = None
        self._accounts_seen: list[dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self.next_pass_ts: Optional[float] = None
        self.running = False

    # -- store / config ------------------------------------------------------
    @property
    def store(self) -> MailStore:
        with self._store_lock:
            if self._store is None:
                self._store = MailStore(os.path.join(self.data_dir, "mail.db"), clock=self.clock)
            return self._store

    def config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        try:
            with open(self.config_path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                cfg.update(self._validate(raw, partial=True))
        except (OSError, ValueError):
            pass
        return cfg

    @staticmethod
    def _validate(raw: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        for key, lo, hi in (("interval_min", 0, 1440), ("retention_days", 1, 3650), ("max_per_pass", 1, 2000), ("since_days", 1, 90)):
            if key in raw:
                try:
                    out[key] = max(lo, min(hi, int(raw[key])))
                except (TypeError, ValueError):
                    if not partial:
                        raise ValueError(f"{key} must be a number")
        if "owner" in raw:
            out["owner"] = str(raw["owner"] or "").strip()[:80]
        return out

    def set_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config()
        cfg.update(self._validate(patch))
        tmp = self.config_path + ".tmp"
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.config_path)
        self._status_cache = None
        self._ensure_thread()
        return cfg

    # -- spheres ---------------------------------------------------------------
    def _spheres(self) -> Any:
        return self.hub.facet("spheres") if hasattr(self.hub, "facet") else None

    def _sphere_for_account(self, account: str, account_id: str = "", address: str = "") -> str:
        fn = getattr(self._spheres(), "sphere_of_account", None)
        if fn is None:
            return "personal"
        try:
            default = fn("\x00no-such-account", "")
            for cand in (account, account_id, address):
                if cand:
                    found = fn(str(cand), address or "")
                    if found and found != default:
                        return str(found)
            return str(default or "personal")
        except Exception:  # noqa: BLE001
            logger.exception("sphere_of_account failed")
            return "personal"

    def _classify(self, sphere: str, **kw: Any) -> dict[str, Any]:
        fn = getattr(self._spheres(), "classify", None)
        if fn is None:
            return {"priority": "normal", "reasons": []}
        try:
            res = fn(sphere, **kw)
            pr = str((res or {}).get("priority") or "normal")
            return {"priority": pr if pr in PRIORITIES else "normal", "reasons": [str(r) for r in (res or {}).get("reasons") or []][:8]}
        except Exception:  # noqa: BLE001
            logger.exception("classify failed")
            return {"priority": "normal", "reasons": []}

    def app_allowed(self, sphere: str, app: str) -> bool:
        if app in ("ui", "hub", "hub-tool"):
            return True
        fn = getattr(self._spheres(), "app_allowed", None)
        if fn is None:
            return True
        try:
            return bool(fn(sphere, app))
        except Exception:  # noqa: BLE001
            return True

    # -- the helper ------------------------------------------------------------
    def faustus_dir(self) -> Optional[Path]:
        """The Faustus folder: the hub's ``faustus_dir`` setting, then the shared discovery (``fam_mail.faustus_dir``: environment,
        sibling folders, the usual places). The hub does not ask itself, so ``ask_hub`` is off."""
        return fam_mail.faustus_dir(getattr(self.hub.config, "faustus_dir", None), ask_hub=False)

    def python_of(self, root: Path) -> Optional[str]:
        return fam_mail.faustus_python(root, getattr(self.hub.config, "faustus_python", None))

    def call_helper(self, request: dict[str, Any], timeout: float = HELPER_TIMEOUT_S) -> dict[str, Any]:
        root = self.faustus_dir()
        if root is None:
            return {"ok": False, "error": "Faustus folder not found (hub.json faustus_dir, HOARD_FAUSTUS_DIR or FAUSTUS_DIR)"}
        python = self.python_of(root)
        if python is None:
            return {"ok": False, "error": "Faustus has no venv with Python"}
        owner = self.config().get("owner")
        if owner and not request.get("owner"):
            request = {**request, "owner": owner}
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            done = self.runner([python, str(HELPER), str(root)], input=json.dumps(request, ensure_ascii=False), capture_output=True,
                               text=True, encoding="utf-8", timeout=timeout, cwd=str(root), env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "the mail read took too long"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"mail helper: {type(exc).__name__}"}
        lines = [ln for ln in (getattr(done, "stdout", "") or "").splitlines() if ln.strip().startswith("{")]
        try:
            answer = json.loads(lines[-1]) if lines else {}
        except ValueError:
            answer = {}
        if not isinstance(answer, dict) or "ok" not in answer:
            return {"ok": False, "error": f"mail helper exit {getattr(done, 'returncode', '?')}"}
        return answer

    def helper_status(self, refresh: bool = False) -> dict[str, Any]:
        key = str(self.faustus_dir() or "") + "|" + str(self.config().get("owner"))
        cached = self._status_cache
        if cached and not refresh and cached[1] == key and self.clock() - cached[0] < (STATUS_TTL_S if cached[2].get("ok") else 30):
            return cached[2]
        answer = self.call_helper({"action": "status"}, timeout=60)
        self._status_cache = (self.clock(), key, answer)
        if answer.get("ok"):
            self._accounts_seen = list(answer.get("accounts") or [])
            try:
                self.store.set_state("accounts", self._accounts_seen)
            except Exception:  # noqa: BLE001
                pass
        return answer

    def send_mail(self, subject: str, text: str, to: Optional[list[str]] = None, html: str = "") -> dict[str, Any]:
        """A notification mail through the account configured in Faustus (default recipient: the account's own address).

        Sending does not read any mail, so it works whether or not the gateway is ``enabled``."""
        req: dict[str, Any] = {"action": "send", "subject": str(subject or "Hoard Hub")[:200], "text": str(text or ""),
                               "from_name": "Hoard Hub"}
        if html:
            req["html"] = str(html)
        if to:
            req["to"] = [str(t) for t in to] if not isinstance(to, str) else [to]
        return self.call_helper(req, timeout=60)

    # -- ingest --------------------------------------------------------------------
    def _interests_for(self, sphere: str, msg: dict[str, Any]) -> list[str]:
        out = []
        for it in self.store.interests():
            if it["sphere"] and it["sphere"] != sphere:
                continue
            if not self.app_allowed(sphere, it["app"]):
                continue
            if match_interest(it["spec"], msg):
                out.append(it["app"])
        return out

    def ingest(self, rec: dict[str, Any], *, sphere: Optional[str] = None, source: Optional[str] = None,
               emit: bool = True) -> dict[str, Any]:
        """Classify and store one mail record in the helper's shape (``message_id, subject, from_name, from_address, date_ts,
        text, links, attachments, to, cc, in_reply_to, references, account, from_self``). Also used by the ``chats``
        facet for the Outlook inbox. ``{"id", "created", "sphere", "priority", "interests"}``."""
        account = str(source or rec.get("account") or "")
        address = str(rec.get("from_address") or rec.get("from_addr") or "")
        name = str(rec.get("from_name") or "")
        subject = str(rec.get("subject") or "")
        text = str(rec.get("text") or "")
        sph = sphere or self._sphere_for_account(account, str(rec.get("account_id") or ""), str(rec.get("account_address") or ""))
        cls = self._classify(sph, sender=(f"{name} <{address}>" if name and address else address or name), subject=subject, text=text,
                             mentions_me=False, direct=False)
        priority, reasons = cls["priority"], list(cls["reasons"])
        if rec.get("from_self"):
            priority, reasons = "low", reasons + ["own mail"]
        msg = {"from_addr": address, "from_name": name, "subject": subject, "text": text, "attachments": rec.get("attachments") or []}
        raw_html = str(rec.get("html") or "")
        html = raw_html[:MAX_HTML] if _mail_helper.has_markup(raw_html) else ""         # the gateway only keeps HTML that carries markup
        interests = self._interests_for(sph, msg)
        refs = rec.get("references") or []
        thread = (refs[0] if refs else "") or str(rec.get("in_reply_to") or "") or str(rec.get("message_id") or "")
        res = self.store.insert_message(
            kind="mail", source=account, sphere=sph, folder=str(rec.get("folder") or ""), uid=int(rec.get("uid") or 0),
            message_id=str(rec.get("message_id") or "") or f"nomid:{account}:{rec.get('folder')}:{rec.get('uid')}", thread=thread,
            date_ts=rec.get("date_ts") or rec.get("ts"), from_addr=address, from_name=name,
            to={"to": rec.get("to") or [], "cc": rec.get("cc") or []}, subject=subject, text=text, links=rec.get("links"),
            attachments=rec.get("attachments"), priority=priority, reasons=reasons, interests=interests,
            html=html, images=rec.get("images"), headers=rec.get("headers"))
        out = {**res, "sphere": sph, "priority": priority, "interests": interests}
        if res["created"] and emit:
            try:
                self.hub.events.emit("mail.received", {"mail_id": res["id"], "sphere": sph, "source": account, "from_domain": domain_of(address),
                                                       "subject": subject[:120], "priority": priority, "interests": interests}, source="hub")
            except Exception:  # noqa: BLE001
                logger.exception("mail.received emit failed")
        return out

    def reindex_interests(self, app: Optional[str] = None, *, limit: int = 5000) -> int:
        """Recompute which apps each stored message matches (after an app registers or changes its interest)."""
        specs = {i["app"]: i for i in self.store.interests()}
        if not specs:
            return 0
        n = 0
        for row in self.store.query(kind="mail", limit=limit):
            apps = [a for a in row["interests"] if a != app] if app else []
            for a, it in specs.items():
                if app and a != app:
                    continue
                if it["sphere"] and it["sphere"] != row["sphere"]:
                    continue
                if not self.app_allowed(row["sphere"], a):
                    continue
                if match_interest(it["spec"], row):
                    apps.append(a)
            apps = sorted(set(apps))
            if apps != sorted(set(row["interests"])):
                self.store.set_interests_of(row["id"], apps)
                n += 1
        return n

    # -- the pass --------------------------------------------------------------------
    def _watermarks(self) -> dict[str, Any]:
        wm = self.store.get_state("watermarks", {})
        return wm if isinstance(wm, dict) else {}

    def run_pass(self, *, force: bool = False) -> dict[str, Any]:
        """One incremental read of the inbox. ``force`` runs even when the gateway is disabled (never done by the background thread)."""
        cfg = self.config()
        if not cfg["enabled"] and not force:
            return {"ok": False, "skipped": "disabled", "error": "the mail gateway is disabled (turn it on in the Mail tab)"}
        if not self._pass_lock.acquire(blocking=False):
            return {"ok": False, "error": "a pass is already running"}
        self.running = True
        started = self.clock()
        try:
            wm = self._watermarks()
            since = {acct: {f: int((v or {}).get("uid") or 0) for f, v in folders.items()} for acct, folders in wm.items()}
            validity = {acct: {f: (v or {}).get("validity") for f, v in folders.items()} for acct, folders in wm.items()}
            os.makedirs(self.attachments_dir, exist_ok=True)
            answer = self.call_helper({"action": "fetch", "since": since, "validity": validity, "since_days": cfg["since_days"],
                                       "max": cfg["max_per_pass"], "attachments_dir": self.attachments_dir})
            new = seen = 0
            if answer.get("ok"):
                for rec in answer.get("messages") or []:
                    seen += 1
                    try:
                        if self.ingest(rec)["created"]:
                            new += 1
                    except Exception:  # noqa: BLE001 — one odd message must not stop the pass
                        logger.exception("ingest failed")
                for info in answer.get("accounts") or []:
                    acct = str(info.get("account") or "")
                    for folder, fi in (info.get("folders") or {}).items():
                        cur = wm.setdefault(acct, {}).setdefault(folder, {"uid": 0, "validity": None})
                        top = int(fi.get("last_uid") or 0)
                        if fi.get("validity") and cur.get("validity") and int(fi["validity"]) != int(cur["validity"]):
                            cur["uid"] = 0                    # renumbered folder: the helper already started over
                        cur["uid"] = max(int(cur.get("uid") or 0), top)
                        cur["validity"] = fi.get("validity") or cur.get("validity")
                for rec in answer.get("messages") or []:     # a watermark never stays below a message we stored
                    acct, folder, uid = str(rec.get("account") or ""), str(rec.get("folder") or ""), int(rec.get("uid") or 0)
                    if acct and folder and uid:
                        cur = wm.setdefault(acct, {}).setdefault(folder, {"uid": 0, "validity": None})
                        cur["uid"] = max(int(cur.get("uid") or 0), uid)
                self.store.set_state("watermarks", wm)
                if answer.get("accounts"):
                    self._accounts_seen = [{"account": i.get("account"), "address": i.get("address")} for i in answer["accounts"]]
                    self.store.set_state("accounts", self._accounts_seen)
            result = {"ok": bool(answer.get("ok")), "ts": started, "new": new, "seen": seen, "error": str(answer.get("error") or ""),
                      "took_s": round(self.clock() - started, 2),
                      "remaining": sum(int(f.get("remaining") or 0) for i in answer.get("accounts") or [] for f in (i.get("folders") or {}).values())}
            self.store.set_state("last_pass", result)
            if result["ok"]:
                self.store.set_state("last_ok_ts", started)
            try:
                result["pruned"] = self.store.prune(cfg["retention_days"], self.attachments_dir)
            except Exception:  # noqa: BLE001
                logger.exception("prune failed")
            return result
        finally:
            self.running = False
            self._pass_lock.release()

    # -- thread --------------------------------------------------------------------
    def _want_thread(self) -> bool:
        cfg = self.config()
        return bool(self.background and cfg["enabled"] and cfg["interval_min"] > 0)

    def _ensure_thread(self) -> None:
        alive = self._thread is not None and self._thread.is_alive()
        if self._want_thread() and not alive:
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, args=(self._stop,), name="hoard-hub-mailgate", daemon=True)
            self._thread.start()
        elif not self._want_thread() and alive and self._stop is not None:
            self._stop.set()
            self.next_pass_ts = None

    def _loop(self, stop: threading.Event) -> None:
        wait = self.first_delay_s
        while True:
            self.next_pass_ts = self.clock() + wait
            if stop.wait(wait):
                return
            cfg = self.config()
            if not cfg["enabled"] or cfg["interval_min"] <= 0:
                return
            try:
                self.run_pass()
            except Exception:  # noqa: BLE001
                logger.exception("mail pass failed")
            wait = max(30.0, cfg["interval_min"] * 60.0)

    def start(self) -> None:
        self._ensure_thread()

    def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        if self._store is not None:
            self._store.close()
            self._store = None

    # -- views ---------------------------------------------------------------------
    def _view(self, row: dict[str, Any], *, full: bool = False, category: bool = False, fields: Iterable[str] = ()) -> dict[str, Any]:
        to = row.get("to_raw")
        to_list = to.get("to", []) if isinstance(to, dict) else (to or [])
        cc_list = to.get("cc", []) if isinstance(to, dict) else []
        v = {"id": row["id"], "kind": row["kind"], "source": row["source"], "sphere": row["sphere"], "folder": row["folder"],
             "uid": row["uid"], "message_id": row["message_id"], "thread": row["thread"], "date_ts": row["date_ts"],
             "from_addr": row["from_addr"], "from_name": row["from_name"], "from_domain": domain_of(row["from_addr"]),
             "to": to_list, "cc": cc_list, "subject": row["subject"], "snippet": row["snippet"], "priority": row["priority"],
             "reasons": row["reasons"], "interests": row["interests"], "claims": row["claims"], "dismissed": row["dismissed"],
             "fetched_ts": row["fetched_ts"], "n_attachments": len(row["attachments"])}
        if category:
            v["category"] = guess_category(row)
        if full:
            v["text"] = row["text"]
            v["links"] = row["links"]
            v["attachments"] = [{**a, **({"url": f"/api/mail/attachments/{a['sha']}"} if a.get("sha") else {})} for a in row["attachments"]]
        for f in fields:                                   # 0.8: only what the caller asked for
            if f == "html":
                v["html"] = row.get("html", "")
            elif f == "images":
                v["images"] = row.get("images") or []
            elif f == "headers":
                v["headers"] = row.get("headers") or {}
        return v

    # -- python API for other facets ---------------------------------------------------
    def attention(self, sphere: Optional[str] = None, days: float = 7, limit: int = 50, kind: Optional[str] = "mail") -> list[dict[str, Any]]:
        """"Needs you": priority attention, not dismissed, not claimed. ``sphere`` None = every sphere."""
        rows = self.store.query(kind=kind, spheres=[sphere] if sphere else None, days=days, attention=True, limit=limit)
        return [self._view(r) for r in rows]

    def unclaimed(self, sphere: Optional[str] = None, days: float = 7, limit: int = 50) -> list[dict[str, Any]]:
        """The "sin dueño" tray: nobody claimed it, newest first, each with a guessed ``category``."""
        rows = self.store.query(kind="mail", spheres=[sphere] if sphere else None, days=days, unclaimed=True, limit=limit)
        return [self._view(r, category=True) for r in rows]

    def search(self, q: str, sphere: Optional[str] = None, days: float = 30, limit: int = 20, kind: Optional[str] = None) -> list[dict[str, Any]]:
        rows = self.store.query(kind=kind, spheres=[sphere] if sphere else None, q=q, days=days, limit=limit)
        return [self._view(r) for r in rows]

    def get_message(self, mail_id: int, *, full: bool = True, fields: Iterable[str] = ()) -> Optional[dict[str, Any]]:
        fields = parse_fields(fields)
        row = self.store.get(mail_id, with_html="html" in fields)
        return self._view(row, full=full, category=True, fields=fields) if row else None

    def status(self, refresh: bool = False) -> dict[str, Any]:
        cfg = self.config()
        root = self.faustus_dir()
        configured = bool(root and self.python_of(root))
        if refresh and configured:
            self.helper_status(refresh=True)           # only on request: it starts Faustus's Python
        accounts = self._accounts_seen or self.store.get_state("accounts", []) or []
        wm = self._watermarks()
        out_accounts = []
        for a in accounts:
            name = str(a.get("account") or "")
            out_accounts.append({"account": name, "address": a.get("address") or "", "id": a.get("id") or "",
                                 "sphere": self._sphere_for_account(name, str(a.get("id") or ""), str(a.get("address") or "")),
                                 "last_uid": {f: (v or {}).get("uid") for f, v in (wm.get(name) or {}).items()}})
        last = self.store.get_state("last_pass", None)
        last_ok = self.store.get_state("last_ok_ts", None)
        ready = bool(cfg["enabled"] and configured and last_ok)
        return {"ok": True, "enabled": cfg["enabled"], "ready": ready, "configured": configured, "faustus_dir": str(root or ""),
                "interval_min": cfg["interval_min"], "retention_days": cfg["retention_days"], "owner": cfg["owner"],
                "accounts": out_accounts, "last_pass": last, "last_ok_ts": last_ok,
                "fresh_s": round(self.clock() - last_ok, 1) if last_ok else None,
                "next_pass_ts": self.next_pass_ts if cfg["enabled"] else None, "running": self.running,
                "counts": self.store.counts(), "ts": self.clock()}

    # -- HTTP ---------------------------------------------------------------------------
    def _who(self, req: Request) -> Any:
        try:
            return req.caller()
        except Exception:  # noqa: BLE001
            return None

    def get(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/mail/") and p != "/api/mail":
            return None
        who = self._who(req)
        if who is None:
            return _bad(401, "a family bearer token (or the hub page) is required")
        allowed = allowed_spheres(self.hub, self.store, who)
        priv = allowed is None
        sphere_q = (req.q("sphere") or "").strip()
        if sphere_q in ("all", "*"):
            sphere_q = ""
        if sphere_q and allowed is not None and sphere_q not in allowed:
            return _bad(403, f"app '{who}' is not allowed in sphere '{sphere_q}'")
        spheres = [sphere_q] if sphere_q else (sorted(allowed) if allowed is not None else None)

        if p == "/api/mail/status":
            return self.status(refresh=req.q_bool("refresh") and priv)
        if p == "/api/mail/config":
            return {"ok": True, "config": self.config(), "defaults": DEFAULT_CONFIG} if priv else _bad(403, "ui or hub only")
        if p == "/api/mail/interests":
            rows = self.store.interests()
            return {"ok": True, "interests": rows if priv else [r for r in rows if r["app"] == who]}
        if p == "/api/mail/messages":
            days = req.q("days")
            has_since = req.q("since_id") is not None
            # An app sees the messages that match the interest it registered (interest=0 widens that to its spheres);
            # the page and the hub see everything unless they ask for one app's view (interest=1&app=<id>).
            interest_app = None
            if req.q_bool("interest", default=not priv):
                interest_app = who if not priv else (req.q("app") or None)
            kind = (req.q("kind") or ("any" if priv else "mail")).lower()
            order = (req.q("order") or ("asc" if has_since else "desc")).lower()
            fields = parse_fields(req.q("fields") or "")
            rows = self.store.query(kind=None if kind == "any" else kind, spheres=spheres, source=req.q("source") or None,
                                    folder=req.q("folder") or None, since_id=req.q_int("since_id") if has_since else None,
                                    q=req.q("q") or "", days=float(days) if days else None, interest_app=interest_app,
                                    unclaimed=req.q_bool("unclaimed"), hide_dismissed=req.q_bool("hide_dismissed"),
                                    limit=req.q_int("limit", 50), order="asc" if order == "asc" else "desc", with_html="html" in fields)
            full = req.q_bool("full")
            views = [self._view(r, full=full, category=req.q_bool("unclaimed"), fields=fields) for r in rows]
            last = max([v["id"] for v in views], default=req.q_int("since_id"))
            return {"ok": True, "count": len(views), "last_id": last, "messages": views}
        m = re.fullmatch(r"/api/mail/messages/(\d+)", p)
        if m:
            fields = parse_fields(req.q("fields") or "")
            row = self.store.get(int(m.group(1)), with_html="html" in fields)
            if row is None or (allowed is not None and row["sphere"] not in allowed):
                return _bad(404, "no such message")
            return {"ok": True, "message": self._view(row, full=True, category=True, fields=fields)}
        m = re.fullmatch(r"/api/mail/attachments/([0-9a-f]{64})", p)
        if m:
            return self._attachment(m.group(1), allowed)
        if p == "/api/mail/attention":
            rows = self.store.query(kind=req.q("kind") or "mail", spheres=spheres, days=float(req.q("days") or 7), attention=True,
                                    limit=req.q_int("limit", 50))
            return {"ok": True, "messages": [self._view(r) for r in rows]}
        if p == "/api/mail/unclaimed":
            rows = self.store.query(kind="mail", spheres=spheres, days=float(req.q("days") or 7), unclaimed=True, limit=req.q_int("limit", 50))
            return {"ok": True, "messages": [self._view(r, category=True) for r in rows]}
        return None

    def _attachment(self, sha: str, allowed: Optional[set[str]]) -> Any:
        holders = self.store.message_ids_for_sha(sha)
        if allowed is not None:
            holders = [h for h in holders if h["sphere"] in allowed]
        if not holders:
            return _bad(404, "no such attachment")
        found = None
        if os.path.isdir(self.attachments_dir):
            for name in os.listdir(self.attachments_dir):
                if name.rsplit(".", 1)[0] == sha:
                    found = os.path.join(self.attachments_dir, name)
                    break
        if not found or not os.path.isfile(found):
            return _bad(404, "attachment bytes are gone (retention)")
        with open(found, "rb") as fh:
            body = fh.read()
        label = str(holders[0]["attachment"].get("name") or os.path.basename(found))
        ctype = mimetypes.guess_type(found)[0] or "application/octet-stream"
        return Reply(body=body, content_type=ctype, headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(label)}",
                                                              "X-Content-Type-Options": "nosniff"})

    def post(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/mail/"):
            return None
        who = self._who(req)
        if who is None:
            return _bad(401, "a family bearer token (or the hub page) is required")
        priv = who in ("ui", "hub")
        body = req.body if isinstance(req.body, dict) else {}
        if p == "/api/mail/config":
            if not priv:
                return _bad(403, "ui or hub only")
            try:
                cfg = self.set_config(body.get("config") if isinstance(body.get("config"), dict) else body)
            except ValueError as exc:
                return _bad(400, str(exc))
            return {"ok": True, "config": cfg}
        if p == "/api/mail/fetch":
            res = self.run_pass()
            return res if res.get("ok") else {**res, "ok": False, "status": 409 if res.get("skipped") else 502}
        if p == "/api/mail/interests":
            app = str(body.get("app") or "") if priv else who
            if not app or app in ("ui", "hub"):
                return _bad(400, "app required (an app token registers its own interest)")
            try:
                spec = normalize_spec(body.get("spec") if body.get("spec") is not None else {})
            except ValueError as exc:
                return _bad(400, str(exc))
            self.store.set_interest(app, str(body.get("sphere") or ""), spec)
            n = self.reindex_interests(app)
            return {"ok": True, "app": app, "spec": spec, "sphere": str(body.get("sphere") or ""), "reindexed": n}
        if p == "/api/mail/interests/remove":
            app = str(body.get("app") or "") if priv else who
            removed = self.store.remove_interest(app)
            self.reindex_interests(app)
            return {"ok": True, "removed": removed}
        if p in ("/api/mail/claim", "/api/mail/dismiss"):
            ids = [int(i) for i in (body.get("ids") or []) if str(i).lstrip("-").isdigit()][:500]
            allowed = allowed_spheres(self.hub, self.store, who)
            if allowed is not None:
                ok_ids = [r["id"] for r in self.store.query(ids=ids, limit=len(ids) or 1) if r["sphere"] in allowed]
            else:
                ok_ids = ids
            if p == "/api/mail/claim":
                app = str(body.get("app") or "") if priv else who
                if not app or app in ("ui",):
                    return _bad(400, "app required")
                n = self.store.claim(app, ok_ids, str(body.get("kind") or ""), str(body.get("ref") or ""))
                return {"ok": True, "claimed": n, "skipped": len(ids) - n}
            if not priv:
                return _bad(403, "ui or hub only")
            return {"ok": True, "dismissed": self.store.dismiss(ok_ids, undo=bool(body.get("undo")))}
        return None

    # -- tools -----------------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        sphere = {"type": "string", "description": "Sphere id (personal, work…); empty = all."}
        return [
            {"name": "hub_mail_status",
             "description": "Mail gateway state: accounts, last pass, counts / estado del correo. Keywords: mail, email.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_mail_search",
             "description": "Search stored mail by words, sender or subject / buscar en el correo. Keywords: email, remitente.\n"
                            "Returns id, date, from, subject, snippet, sphere, priority. Read one with hub_mail_get.",
             "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}, "sphere": sphere,
                                                              "days": {"type": "number", "default": 30}, "limit": {"type": "integer", "default": 20}},
                             "required": ["q"], "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_mail_get",
             "description": "Read one stored mail: text, links, attachments / leer un correo. Keywords: email, abrir.",
             "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"], "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_mail_attention",
             "description": "Mail that needs your attention (VIP, keywords) / correo que necesita atención. Keywords: urgente.",
             "inputSchema": {"type": "object", "properties": {"sphere": sphere, "days": {"type": "number", "default": 7},
                                                              "limit": {"type": "integer", "default": 20}}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_mail_unclaimed",
             "description": "Mail no app has claimed, by category / correo sin dueño. Keywords: promo, social, seguridad.",
             "inputSchema": {"type": "object", "properties": {"sphere": sphere, "days": {"type": "number", "default": 7},
                                                              "limit": {"type": "integer", "default": 20}}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_mail_fetch",
             "description": "Read the inbox now (one pass) / leer el correo ahora. Keywords: fetch, revisar correo.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        ]

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        def sph(a: dict[str, Any]) -> Optional[str]:
            s = str(a.get("sphere") or "").strip()
            return None if s in ("", "all", "*") else s

        def status(_: dict[str, Any]) -> Any:
            return self.status()

        def search(a: dict[str, Any]) -> Any:
            rows = self.search(str(a.get("q") or ""), sph(a), float(a.get("days") or 30), int(a.get("limit") or 20), kind="mail")
            return {"ok": True, "count": len(rows), "messages": rows}

        def get_(a: dict[str, Any]) -> Any:
            try:
                msg = self.get_message(int(a.get("id")))
            except (TypeError, ValueError):
                return {"ok": False, "error": "id required"}
            if msg is None:
                return {"ok": False, "error": "no such message"}
            if len(msg.get("text") or "") > 8000:
                msg["text"] = msg["text"][:8000]
                msg["truncated"] = True
            return {"ok": True, "message": msg}

        def attention(a: dict[str, Any]) -> Any:
            rows = self.attention(sph(a), float(a.get("days") or 7), int(a.get("limit") or 20))
            return {"ok": True, "count": len(rows), "messages": rows}

        def unclaimed(a: dict[str, Any]) -> Any:
            rows = self.unclaimed(sph(a), float(a.get("days") or 7), int(a.get("limit") or 20))
            return {"ok": True, "count": len(rows), "messages": rows}

        def fetch(_: dict[str, Any]) -> Any:
            return self.run_pass()

        return {"hub_mail_status": status, "hub_mail_search": search, "hub_mail_get": get_, "hub_mail_attention": attention,
                "hub_mail_unclaimed": unclaimed, "hub_mail_fetch": fetch}

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "ui_scripts": list(self.ui_scripts)}
