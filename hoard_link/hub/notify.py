"""Notifications facet ("Hermes", 0.7): one place decides how the person is told.

Apps call :meth:`NotifyFacet.send` (``POST /api/notify``) instead of carrying their own toast / ntfy /
Telegram / mail code (they keep it as the fallback for when the hub is unreachable). The hub

* picks the sphere of the notification (the caller's, else the first sphere that allows the app),
* routes by priority through the sphere's ``notify`` table (``digest`` = keep it for the daily digest),
* holds what must not disturb: quiet hours of that sphere (everything but ``urgent``), duplicates
  (same app + dedupe key, or title, inside ``dedupe_window_s``) and apps that talk too much
  (``rate_per_app`` pushes per hour, then one summary push),
* delivers through the channels ``windows`` (toast), ``ntfy``, ``telegram`` and ``email``
  (through the mail gateway facet, or plain SMTP),
* stores every outcome in ``<data>/notify.db`` and emits ``notify.sent`` / ``notify.held``.

Config: ``<data>/notify.json``. Secrets (ntfy token, Telegram bot token, SMTP password) live there and
are never returned unmasked (``••••last4``). Every channel sender is injectable
(``NotifyFacet.senders = {"windows": fn, ...}``; ``fn(note, channel_config) -> dict|bool|str|None``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from .. import notify_channels as nc
from ..notify_channels import (  # noqa: F401 - re-exported: the hub's tests and tools import them from here
    HTTP_TIMEOUT_S, POWERSHELL_APP_ID, TOAST_TIMEOUT_S, build_toast_ps1, clean_url, http_json as _http_json, scrub as _scrub, xml_escape)
from .facets import Facet, Request

logger = logging.getLogger("hoard_hub.notify")

CHANNELS = ("windows", "ntfy", "telegram", "email")
PRIORITIES = ("low", "normal", "high", "urgent")
MASK = "••••"
DEFAULT_ROUTES = {"urgent": ["windows", "telegram"], "high": ["windows"], "normal": ["windows"], "low": ["digest"]}
KEEP_ROWS = 5000
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "dedupe_window_s": 6 * 3600,
    "rate_per_app": 20,
    "channels": {
        "windows": {"enabled": True},
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "token": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": "", "api_base": "https://api.telegram.org"},
        "email": {"enabled": False, "via": "faustus", "to": [], "host": "", "port": 465, "user": "",
                  "password": "", "tls": True, "from": ""},
    },
}
SECRETS = {"ntfy": ("token",), "telegram": ("bot_token",), "email": ("password",)}

_TEXT = {
    "es": {"rate_title": "{app}: demasiados avisos",
           "rate_body": "Los siguientes de esta hora se retienen; los verás en la pestaña Avisos.",
           "test_title": "Prueba de avisos", "test_body": "Si lees esto, este canal funciona."},
    "en": {"rate_title": "{app}: too many notifications",
           "rate_body": "Further ones this hour are held; see the Notifications tab.",
           "test_title": "Notification test", "test_body": "If you can read this, this channel works."},
}


# -- small helpers ---------------------------------------------------------------------------------------

def mask(secret: Any) -> str:
    """``••••last4`` for a stored secret, empty when none is set."""
    s = str(secret or "")
    if not s:
        return ""
    return MASK + (s[-4:] if len(s) >= 8 else "")


def is_masked(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MASK)


def _norm_result(channel: str, value: Any) -> dict[str, Any]:
    """Whatever a sender returned → ``{"channel", "ok", "error"?, ...}``."""
    if isinstance(value, dict):
        out = dict(value)
        out["ok"] = bool(out.get("ok", not out.get("error")))
    elif isinstance(value, str):
        out = {"ok": not value, **({"error": value} if value else {})}
    elif value is False:
        out = {"ok": False, "error": "failed"}
    else:
        out = {"ok": True}
    out["channel"] = channel
    return out


def _parse_ts(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ValueError(f"bad time: {value!r} (epoch seconds or an ISO date)")


def _merge_config(base: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Stored config over the defaults (lenient: wrong types fall back to the default)."""
    out = copy.deepcopy(base)
    if not isinstance(raw, dict):
        return out
    for key in ("enabled",):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    for key, lo, hi in (("dedupe_window_s", 0, 30 * 86400), ("rate_per_app", 1, 10000)):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            out[key] = int(v)
    chans = raw.get("channels")
    if isinstance(chans, dict):
        for ch, vals in chans.items():
            if ch in out["channels"] and isinstance(vals, dict):
                for k, v in vals.items():
                    if k in out["channels"][ch] and type(v) is type(out["channels"][ch][k]):
                        out["channels"][ch][k] = v
    return out


# -- the facet ---------------------------------------------------------------------------------------------

class NotifyFacet(Facet):
    id = "notify"
    ui_scripts = ("notify.js",)

    #: Channel senders that replace the built-in ones: ``{"windows": fn}``; ``fn(note, channel_config)``.
    senders: dict[str, Callable[[dict[str, Any], dict[str, Any]], Any]] = {}

    def __init__(self, hub: Any):
        super().__init__(hub)
        data_dir = hub.config.data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.config_path = os.path.join(data_dir, "notify.json")
        self.db_path = os.path.join(data_dir, "notify.db")
        self.platform = sys.platform
        self.toast_async = True                      # the toast runs on its own thread; tests set False
        self.powershell_runner: Callable[..., Any] = subprocess.run
        self.smtp_factory: Callable[[dict[str, Any]], Any] = self._default_smtp
        self.http: Callable[..., tuple[Optional[int], Any]] = _http_json
        self._cfg_lock = threading.RLock()
        self._cfg: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._cfg_sig: Optional[tuple[int, int]] = None
        self._send_lock = threading.RLock()          # decide + record is atomic; delivery is outside it
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
            "app TEXT NOT NULL, sphere TEXT NOT NULL, priority TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, "
            "url TEXT NOT NULL, grp TEXT NOT NULL, dedupe_key TEXT NOT NULL, tags_json TEXT NOT NULL, "
            "icon_app TEXT NOT NULL, channels_json TEXT NOT NULL, held TEXT, seen INTEGER NOT NULL DEFAULT 0)")
        self._db.execute("CREATE INDEX IF NOT EXISTS notif_app_ts ON notifications(app, ts)")
        self._db.execute("CREATE INDEX IF NOT EXISTS notif_ts ON notifications(ts)")
        self._db.commit()
        self._load_config(force=True)

    def close(self) -> None:
        with self._db_lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    # -- config ----------------------------------------------------------------------------------------------
    def _sig(self) -> Optional[tuple[int, int]]:
        try:
            st = os.stat(self.config_path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load_config(self, force: bool = False) -> None:
        with self._cfg_lock:
            sig = self._sig()
            if not force and sig == self._cfg_sig:
                return
            self._cfg_sig = sig
            if sig is None:
                self._cfg = copy.deepcopy(DEFAULT_CONFIG)
                return
            try:
                with open(self.config_path, "r", encoding="utf-8-sig") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                logger.warning("notify.json is unreadable; using the defaults until it is saved again")
                raw = None
            self._cfg = _merge_config(DEFAULT_CONFIG, raw)

    def config(self) -> dict[str, Any]:
        """The full config including secrets (internal use; never send this over HTTP)."""
        with self._cfg_lock:
            self._load_config()
            return copy.deepcopy(self._cfg)

    def config_view(self) -> dict[str, Any]:
        """The config as HTTP/UI/tools see it: secrets masked, plus what each channel can do here."""
        cfg = self.config()
        for ch, fields in SECRETS.items():
            for f in fields:
                cfg["channels"][ch][f] = mask(cfg["channels"][ch][f])
        return {"ok": True, **cfg, "status": self.channel_status(), "routing": self.routing(),
                "priorities": list(PRIORITIES), "channel_names": list(CHANNELS)}

    def channel_status(self) -> dict[str, dict[str, Any]]:
        cfg = self.config()["channels"]
        mg = self.hub.facet("mailgate")
        out: dict[str, dict[str, Any]] = {}
        out["windows"] = {"enabled": cfg["windows"]["enabled"], "supported": self._is_windows(),
                          "configured": self._is_windows(), "detail": "Windows toast" if self._is_windows() else "unsupported here"}
        n = cfg["ntfy"]
        out["ntfy"] = {"enabled": n["enabled"], "supported": True, "configured": bool(n["topic"]),
                       "detail": (n["server"] + "/" + n["topic"]) if n["topic"] else "missing topic"}
        t = cfg["telegram"]
        out["telegram"] = {"enabled": t["enabled"], "supported": True, "configured": bool(t["bot_token"] and t["chat_id"]),
                           "detail": "bot and chat set" if t["bot_token"] and t["chat_id"] else "missing bot token or chat id"}
        e = cfg["email"]
        if e["via"] == "smtp":
            ok = bool(e["host"] and e["to"])
            detail = f"SMTP {e['host']}:{e['port']}" if ok else "missing SMTP host or recipient"
        else:
            ok = callable(getattr(mg, "send_mail", None))
            detail = "through the mail gateway" if ok else "mail gateway not available"
        out["email"] = {"enabled": e["enabled"], "supported": True, "configured": ok, "detail": detail, "via": e["via"]}
        return out

    def routing(self) -> dict[str, Any]:
        """Per sphere, the channel list of each priority (edited in the spheres dialog)."""
        sp = self.hub.facet("spheres")
        if sp is None:
            return {"personal": {"name": {"es": "Personal", "en": "Personal"}, "notify": copy.deepcopy(DEFAULT_ROUTES)}}
        return {s["id"]: {"name": s["name"], "color": s.get("color"), "notify": s["notify"]} for s in sp.list()}

    def update_config(self, patch: Any) -> dict[str, Any]:
        """Merge a partial config. A masked or empty secret keeps the stored one, ``null`` clears it."""
        if not isinstance(patch, dict):
            return {"ok": False, "status": 400, "error": "config must be an object"}
        errors: list[str] = []
        with self._cfg_lock:
            self._load_config()
            cfg = copy.deepcopy(self._cfg)
            if "enabled" in patch:
                if isinstance(patch["enabled"], bool):
                    cfg["enabled"] = patch["enabled"]
                else:
                    errors.append("enabled: must be true or false")
            for key, lo, hi in (("dedupe_window_s", 0, 30 * 86400), ("rate_per_app", 1, 10000)):
                if key in patch:
                    v = patch[key]
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                        cfg[key] = int(v)
                    else:
                        errors.append(f"{key}: a number between {lo} and {hi}")
            chans = patch.get("channels", {})
            if chans is not None and not isinstance(chans, dict):
                errors.append("channels: must be an object")
                chans = {}
            for ch, vals in (chans or {}).items():
                if ch not in CHANNELS:
                    errors.append(f"channels.{ch}: unknown channel (use {', '.join(CHANNELS)})")
                    continue
                if not isinstance(vals, dict):
                    errors.append(f"channels.{ch}: must be an object")
                    continue
                vals = dict(vals)
                if ch == "email" and isinstance(vals.get("smtp"), dict):
                    vals = {**vals.pop("smtp"), **vals}
                self._apply_channel(ch, vals, cfg["channels"][ch], errors)
            if errors:
                return {"ok": False, "status": 400, "error": "; ".join(errors), "errors": errors}
            self._cfg = cfg
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            tmp = self.config_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.config_path)
            self._cfg_sig = self._sig()
        return self.config_view()

    @staticmethod
    def _apply_channel(ch: str, vals: dict[str, Any], dest: dict[str, Any], errors: list[str]) -> None:
        for key, v in vals.items():
            if key not in dest:
                continue
            path = f"channels.{ch}.{key}"
            if key in SECRETS.get(ch, ()):
                if v is None:
                    dest[key] = ""
                elif isinstance(v, str):
                    if v and not is_masked(v):
                        dest[key] = v.strip()
                else:
                    errors.append(f"{path}: must be text")
            elif key == "enabled" or key == "tls":
                if isinstance(v, bool):
                    dest[key] = v
                else:
                    errors.append(f"{path}: must be true or false")
            elif key == "port":
                if isinstance(v, (int, str)) and not isinstance(v, bool) and str(v).strip().isdigit() and 0 < int(v) < 65536:
                    dest[key] = int(v)
                else:
                    errors.append(f"{path}: a port number")
            elif key == "to":
                items = re.split(r"[,;\s]+", v.strip()) if isinstance(v, str) else v
                if isinstance(items, list):
                    dest[key] = [str(a).strip() for a in items if str(a).strip()][:20]
                else:
                    errors.append(f"{path}: must be a list of addresses")
            elif key == "via":
                if v in ("faustus", "smtp"):
                    dest[key] = v
                else:
                    errors.append(f"{path}: faustus or smtp")
            elif key in ("server", "api_base"):
                s = str(v or "").strip().rstrip("/")
                if urlsplit(s).scheme in ("http", "https") and urlsplit(s).hostname:
                    dest[key] = s
                else:
                    errors.append(f"{path}: an http(s) URL")
            elif key == "topic":
                s = str(v or "").strip()
                if not s or _TOPIC.match(s):
                    dest[key] = s
                else:
                    errors.append(f"{path}: letters, digits, '-' and '_' only")
            elif isinstance(v, str):
                dest[key] = v.strip()[:300]
            else:
                errors.append(f"{path}: must be text")

    # -- store -----------------------------------------------------------------------------------------------
    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        try:
            tags = json.loads(r[9])
        except ValueError:
            tags = []
        try:
            channels = json.loads(r[12])
        except ValueError:
            channels = []
        return {"id": int(r[0]), "ts": float(r[1]), "app": r[2], "sphere": r[3], "priority": r[4], "title": r[5],
                "body": r[6], "url": r[7], "group": r[8], "dedupe_key": r[10], "tags": tags,
                "icon_app": r[11], "channels": channels, "held": r[13], "seen": bool(r[14])}

    _COLS = "id, ts, app, sphere, priority, title, body, url, grp, tags_json, dedupe_key, icon_app, channels_json, held, seen"

    def _insert(self, ts: float, app: str, sphere: str, priority: str, title: str, body: str, url: str, group: str,
                dedupe_key: str, tags: list[str], icon_app: str, held: Optional[str]) -> int:
        with self._db_lock:
            cur = self._db.execute(
                "INSERT INTO notifications (ts, app, sphere, priority, title, body, url, grp, dedupe_key, tags_json, "
                "icon_app, channels_json, held, seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (ts, app, sphere, priority, title, body, url, group, dedupe_key,
                 json.dumps(tags, ensure_ascii=False), icon_app, "[]", held))
            nid = int(cur.lastrowid)
            if nid % 200 == 0:
                self._db.execute("DELETE FROM notifications WHERE id <= ?", (nid - KEEP_ROWS,))
            self._db.commit()
            return nid

    def _set_channels(self, nid: int, results: list[dict[str, Any]]) -> None:
        with self._db_lock:
            self._db.execute("UPDATE notifications SET channels_json = ? WHERE id = ?",
                             (json.dumps(results, ensure_ascii=False, default=str), int(nid)))
            self._db.commit()

    def _late_result(self, nid: int, channel: str, result: dict[str, Any]) -> None:
        """A sender that finished after ``send`` returned (the toast thread) records its outcome here."""
        with self._db_lock:
            try:
                r = self._db.execute("SELECT channels_json FROM notifications WHERE id = ?", (int(nid),)).fetchone()
                if not r:
                    return
                rows = json.loads(r[0])
                rows = [x for x in rows if x.get("channel") != channel] + [_norm_result(channel, result)]
                self._db.execute("UPDATE notifications SET channels_json = ? WHERE id = ?",
                                 (json.dumps(rows, ensure_ascii=False, default=str), int(nid)))
                self._db.commit()
            except Exception:  # noqa: BLE001
                logger.debug("late result lost", exc_info=True)

    def get_notification(self, nid: int) -> Optional[dict[str, Any]]:
        with self._db_lock:
            r = self._db.execute(f"SELECT {self._COLS} FROM notifications WHERE id = ?", (int(nid),)).fetchone()
        return self._row(r) if r else None

    def history(self, limit: int = 50, *, app: Optional[str] = None, sphere: Optional[str] = None,
                since: Any = None, since_id: int = 0, held: Optional[str] = None, unseen: bool = False,
                q: Optional[str] = None, group: Optional[str] = None) -> list[dict[str, Any]]:
        """Newest first. ``held``: a reason, ``any`` (held for any reason) or ``none`` (delivered)."""
        where, params = ["1=1"], []
        if app:
            where.append("app = ?"); params.append(str(app))
        if sphere:
            where.append("sphere = ?"); params.append(str(sphere))
        ts = _parse_ts(since)
        if ts is not None:
            where.append("ts >= ?"); params.append(ts)
        if since_id:
            where.append("id > ?"); params.append(int(since_id))
        if group:
            where.append("grp = ?"); params.append(str(group))
        if held == "any":
            where.append("held IS NOT NULL")
        elif held == "none":
            where.append("held IS NULL")
        elif held:
            where.append("held = ?"); params.append(str(held))
        if unseen:
            where.append("seen = 0")
        if q:
            where.append("(title LIKE ? OR body LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
        limit = max(1, min(int(limit or 50), 500))
        with self._db_lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM notifications WHERE {' AND '.join(where)} "
                                    "ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._row(r) for r in rows]

    def unseen_count(self) -> int:
        with self._db_lock:
            return int(self._db.execute("SELECT COUNT(*) FROM notifications WHERE seen = 0 AND "
                                        "(held IS NULL OR held NOT IN ('duplicate', 'rate'))").fetchone()[0])

    def mark_read(self, ids: Optional[list[int]] = None) -> int:
        """Mark notifications as seen (``None`` = all). Returns how many changed."""
        with self._db_lock:
            if ids is None:
                cur = self._db.execute("UPDATE notifications SET seen = 1 WHERE seen = 0")
            else:
                clean = [int(i) for i in ids if str(i).lstrip("-").isdigit()][:1000]
                if not clean:
                    return 0
                cur = self._db.execute(f"UPDATE notifications SET seen = 1 WHERE seen = 0 AND id IN ({','.join('?' * len(clean))})", clean)
            self._db.commit()
            return int(cur.rowcount or 0)

    # -- sending ---------------------------------------------------------------------------------------------
    def _lang(self) -> str:
        lang = str(getattr(self.hub.config, "language", "auto") or "auto").lower()[:2]
        if lang in ("es", "en"):
            return lang
        env = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
        return "es" if env.startswith("es") else "en"

    def _is_windows(self) -> bool:
        return str(self.platform).startswith("win")

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        try:
            self.hub.events.emit(type_, data, source="hub")
        except Exception:  # noqa: BLE001
            logger.debug("could not emit %s", type_, exc_info=True)

    def _clock(self, now: Any) -> tuple[float, datetime]:
        if isinstance(now, datetime):
            return now.timestamp(), now
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            return float(now), datetime.fromtimestamp(float(now))
        t = time.time()
        return t, datetime.fromtimestamp(t)

    def send(self, title: str, body: str = "", *, app: str = "hub", sphere: Optional[str] = None,
             priority: str = "normal", url: str = "", group: str = "", dedupe_key: str = "",
             tags: Iterable[str] = (), icon_app: Optional[str] = None, now: Any = None,
             channels_override: Optional[list[str]] = None) -> dict[str, Any]:
        """Tell the person. Returns ``{ok, id, app, sphere, priority, held, channels: [per-channel result], delivered}``.

        ``held`` is None when it was pushed, else ``disabled|duplicate|quiet|digest|rate``.
        ``channels_override`` (the digest) names the channels directly and skips routing, quiet hours
        and the rate limit (duplicates are only checked when ``dedupe_key`` is given). ``now`` (epoch
        seconds or a datetime) is for tests."""
        title = str(title or "").strip()[:200]
        if not title:
            return {"ok": False, "status": 400, "error": "title is required"}
        body = str(body or "").strip()[:2000]
        app = (str(app or "hub").strip() or "hub")[:60]
        priority = str(priority or "normal").lower()
        if priority not in PRIORITIES:
            priority = "normal"
        url = clean_url(url)
        group = str(group or "").strip()[:60]
        dedupe_key = str(dedupe_key or "").strip()[:200]
        tag_list = [str(t).strip()[:40] for t in (tags or ()) if str(t).strip()][:8]
        icon = str(icon_app or app)[:60]
        ts, now_dt = self._clock(now)
        spheres = self.hub.facet("spheres")
        sphere_id = str(sphere or "").strip().lower() or (spheres.sphere_of_app(app) if spheres else "personal")
        sph = spheres.get(sphere_id) if spheres else None
        cfg = self.config()
        override = None
        if channels_override is not None:
            override = [c for c in dict.fromkeys(str(c).strip().lower() for c in channels_override) if c in CHANNELS]
        route = list(((sph or {}).get("notify") or DEFAULT_ROUTES).get(priority) or [])

        held: Optional[str] = None
        channels: list[str] = []
        digest_item = False
        summary: Optional[tuple[int, dict[str, Any]]] = None
        with self._send_lock:
            if not cfg["enabled"]:
                held = "disabled"
            elif override is not None:
                channels = override
                if dedupe_key and self._is_duplicate(app, dedupe_key, title, ts, cfg["dedupe_window_s"]):
                    held, channels = "duplicate", []
            else:
                if self._is_duplicate(app, dedupe_key, title, ts, cfg["dedupe_window_s"]):
                    held = "duplicate"
                else:
                    channels = [c for c in route if c in CHANNELS]
                    wants_digest = "digest" in route
                    if spheres is not None and priority != "urgent" and self._quiet(spheres, sph, sphere_id, now_dt):
                        held, digest_item, channels = "quiet", True, []
                    elif wants_digest and not channels:
                        held, digest_item = "digest", True
                    elif self._pushed_last_hour(app, ts) >= cfg["rate_per_app"] and channels:
                        held = "rate"
                        if not self._has_summary(app, ts):
                            lang = _TEXT[self._lang()]
                            sid = self._insert(ts, app, sphere_id, "normal", lang["rate_title"].format(app=app), lang["rate_body"],
                                               "", "rate-summary", "", [], icon, None)
                            summary = (sid, {"id": sid, "app": app, "sphere": sphere_id, "priority": "normal",
                                             "title": lang["rate_title"].format(app=app), "body": lang["rate_body"],
                                             "url": "", "group": "rate-summary", "tags": [], "ts": ts, "channels": list(channels)})
                    elif wants_digest:
                        digest_item = True            # pushed AND kept for the digest
            nid = self._insert(ts, app, sphere_id, priority, title, body, url, group, dedupe_key, tag_list, icon, held)

        base = {"id": nid, "app": app, "sphere": sphere_id, "priority": priority}
        if summary is not None:
            self._dispatch(summary[1], summary[1]["channels"], cfg)
        if held:
            self._emit("notify.held", {**base, "reason": held, "title": title})
            if digest_item:
                self._emit_digest_item(title, url, app, group, sphere_id)
            out = {"ok": True, **base, "held": held, "channels": [], "delivered": []}
            if held == "duplicate":
                dup = self._find_duplicate(app, dedupe_key, title, ts, cfg["dedupe_window_s"], exclude=nid)
                if dup:
                    out["duplicate_of"] = dup
            return out
        note = {**base, "title": title, "body": body, "url": url, "group": group, "tags": tag_list, "ts": ts}
        results = self._dispatch(note, channels, cfg)
        delivered = [r["channel"] for r in results if r.get("ok")]
        self._emit("notify.sent", {**base, "title": title, "channels": [r["channel"] for r in results], "delivered": delivered})
        if digest_item:
            self._emit_digest_item(title, url, app, group, sphere_id)
        return {"ok": True, **base, "held": None, "channels": results, "delivered": delivered}

    def _emit_digest_item(self, title: str, url: str, app: str, group: str, sphere: str) -> None:
        self._emit("digest.item", {"title": title, "url": url, "watch": app, "kind": group or "notify", "sphere": sphere})

    def _dedupe_where(self, app: str, key: str, title: str, since: float, exclude: Optional[int] = None) -> tuple[str, list[Any]]:
        sql = "app = ? AND ts >= ? AND (held IS NULL OR held != 'duplicate') AND grp != 'rate-summary'"
        params: list[Any] = [app, since]
        if key:
            sql += " AND dedupe_key = ?"; params.append(key)
        else:
            sql += " AND dedupe_key = '' AND title = ?"; params.append(title)
        if exclude is not None:
            sql += " AND id != ?"; params.append(int(exclude))
        return sql, params

    def _is_duplicate(self, app: str, key: str, title: str, ts: float, window: float) -> bool:
        if window <= 0:
            return False
        sql, params = self._dedupe_where(app, key, title, ts - window)
        with self._db_lock:
            return self._db.execute(f"SELECT 1 FROM notifications WHERE {sql} LIMIT 1", params).fetchone() is not None

    def _find_duplicate(self, app: str, key: str, title: str, ts: float, window: float, exclude: int) -> Optional[int]:
        sql, params = self._dedupe_where(app, key, title, ts - window, exclude)
        with self._db_lock:
            r = self._db.execute(f"SELECT id FROM notifications WHERE {sql} ORDER BY id LIMIT 1", params).fetchone()
        return int(r[0]) if r else None

    def _pushed_last_hour(self, app: str, ts: float) -> int:
        with self._db_lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM notifications WHERE app = ? AND ts > ? AND held IS NULL AND grp NOT IN ('rate-summary', 'test')",
                (app, ts - 3600)).fetchone()[0])

    def _has_summary(self, app: str, ts: float) -> bool:
        with self._db_lock:
            return self._db.execute("SELECT 1 FROM notifications WHERE app = ? AND grp = 'rate-summary' AND ts > ? LIMIT 1",
                                    (app, ts - 3600)).fetchone() is not None

    def _dispatch(self, note: dict[str, Any], channels: list[str], cfg: dict[str, Any],
                  force: bool = False) -> list[dict[str, Any]]:
        """Deliver to every channel (in parallel), store the results, return them."""
        if not channels:
            self._set_channels(note["id"], [])
            return []
        if len(channels) == 1:
            results = [self._deliver(channels[0], note, cfg, force)]
        else:
            with ThreadPoolExecutor(max_workers=len(channels)) as pool:
                results = list(pool.map(lambda c: self._deliver(c, note, cfg, force), channels))
        self._set_channels(note["id"], results)
        return results

    @staticmethod
    def _quiet(spheres: Any, sph: Optional[dict[str, Any]], sphere_id: str, now_dt: datetime) -> bool:
        """Is ``now_dt`` inside the sphere's quiet hours? The window arithmetic is ``notify_channels.in_quiet_hours`` (the hub holds
        everything but urgent); a spheres facet that does not expose its window falls back to its own check."""
        q = (sph or {}).get("quiet_hours")
        if isinstance(q, dict):
            try:
                from .spheres import parse_days
                return nc.in_quiet_hours(now_dt, q.get("start"), q.get("end"), allow_high=False, days=parse_days(q.get("days", "daily")))
            except Exception:  # noqa: BLE001
                pass
        return bool(spheres.in_quiet_hours(sphere_id, now_dt))

    def _sender(self, channel: str) -> Callable[[dict[str, Any], dict[str, Any]], Any]:
        custom = self.senders.get(channel)
        if custom is not None:
            return custom
        return {"windows": self._send_windows, "ntfy": self._send_ntfy, "telegram": self._send_telegram,
                "email": self._send_email}[channel]

    def _deliver(self, channel: str, note: dict[str, Any], cfg: dict[str, Any], force: bool = False) -> dict[str, Any]:
        ccfg = cfg["channels"].get(channel) or {}
        if channel not in CHANNELS:
            return {"channel": channel, "ok": False, "error": "unknown channel"}
        if not force and not ccfg.get("enabled"):
            return {"channel": channel, "ok": False, "skipped": True, "error": "disabled"}
        t0 = time.monotonic()
        try:
            res = _norm_result(channel, self._sender(channel)(note, ccfg))
        except Exception as exc:  # noqa: BLE001 - a channel must never raise out of send()
            logger.info("notify channel %s failed: %s", channel, type(exc).__name__)
            res = {"channel": channel, "ok": False, "error": type(exc).__name__}
        res["ms"] = int((time.monotonic() - t0) * 1000)
        return res

    # -- channels ----------------------------------------------------------------------------------------------
    def _send_windows(self, note: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        if not self._is_windows():
            return {"ok": False, "unsupported": True, "error": "unsupported"}
        script = build_toast_ps1(note["title"], note.get("body", ""), note.get("url", ""))
        if not self.toast_async:
            return self._run_toast(script)
        nid = note.get("id")

        def run() -> None:
            res = self._run_toast(script)
            if not res.get("ok") and nid:
                self._late_result(int(nid), "windows", res)

        threading.Thread(target=run, name="hoard-hub-toast", daemon=True).start()
        return {"ok": True, "async": True}

    def _run_toast(self, script: str) -> dict[str, Any]:
        return nc.run_ps1(script, runner=self.powershell_runner, timeout=TOAST_TIMEOUT_S)

    def _send_ntfy(self, note: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        return nc.send_ntfy(cfg.get("server") or "https://ntfy.sh", cfg.get("topic") or "", note["title"], note.get("body") or "",
                            priority=note.get("priority", "normal"), url=note.get("url"), token=cfg.get("token") or None,
                            tags=note.get("tags") or [], http=self.http)

    def _send_telegram(self, note: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        text = nc.telegram_text(note["title"], note.get("body") or "", note.get("url"))
        return nc.send_telegram(cfg.get("bot_token") or "", cfg.get("chat_id") or "", text,
                                api_base=str(cfg.get("api_base") or nc.TELEGRAM_API), http=self.http)

    def telegram_discover_chat_id(self) -> dict[str, Any]:
        """Find the chat id after the person has written to the bot (getUpdates with the stored token)."""
        cfg = self.config()["channels"]["telegram"]
        token = cfg.get("bot_token") or ""
        if not token:
            return {"ok": False, "status": 400, "error": "save the bot token first"}
        res = nc.telegram_discover_chat_id(token, api_base=str(cfg.get("api_base") or nc.TELEGRAM_API), http=self.http)
        if res.get("ok"):
            return {"ok": True, "chat_id": res["chat_id"], "name": res["name"]}
        return {"ok": False, "error": res["error"]}

    @staticmethod
    def _email_parts(note: dict[str, Any]) -> tuple[str, str, str]:
        return nc.email_parts(note["title"], note.get("body") or "", note.get("url"))

    def _send_email(self, note: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        subject, text, html_body = self._email_parts(note)
        if cfg.get("via") == "smtp":
            return self._send_smtp(subject, text, html_body, cfg)
        mg = self.hub.facet("mailgate")
        fn = getattr(mg, "send_mail", None)
        if not callable(fn):
            return {"ok": False, "error": "mail gateway not available"}
        res = fn(subject, text, to=list(cfg.get("to") or []) or None, html=html_body)
        if isinstance(res, dict):
            return {"ok": bool(res.get("ok")), **({"error": str(res.get("error"))[:200]} if res.get("error") else {})}
        return {"ok": bool(res)}

    @staticmethod
    def _default_smtp(cfg: dict[str, Any]) -> Any:
        return nc.default_smtp(cfg)

    def _send_smtp(self, subject: str, text: str, html_body: str, cfg: dict[str, Any]) -> dict[str, Any]:
        return nc.send_smtp(cfg, [a for a in cfg.get("to") or [] if a], subject, text, html=html_body,
                            smtp_factory=self.smtp_factory, default_from="hoard-hub@localhost")

    def test_channel(self, channel: str) -> dict[str, Any]:
        """Send a sample through one channel now, ignoring its enabled flag, routing and quiet hours."""
        channel = str(channel or "").strip().lower()
        if channel not in CHANNELS:
            return {"ok": False, "status": 400, "error": f"unknown channel (use {', '.join(CHANNELS)})"}
        cfg = self.config()
        lang = _TEXT[self._lang()]
        ts = time.time()
        sphere = (self.hub.facet("spheres").active() if self.hub.facet("spheres") else "personal")
        nid = self._insert(ts, "hub", sphere, "normal", lang["test_title"], lang["test_body"], "", "test", "", [], "hub", None)
        note = {"id": nid, "app": "hub", "sphere": sphere, "priority": "normal", "title": lang["test_title"],
                "body": lang["test_body"], "url": "", "group": "test", "tags": [], "ts": ts}
        results = self._dispatch(note, [channel], cfg, force=True)
        return {"ok": True, "id": nid, "channel": channel, "result": results[0], "delivered": bool(results[0].get("ok"))}

    # -- HTTP --------------------------------------------------------------------------------------------------
    def get(self, req: Request) -> Optional[Any]:
        if req.path == "/api/notify":
            try:
                rows = self.history(req.q_int("limit", 50), app=req.q("app") or None, sphere=req.q("sphere") or None,
                                    since=req.q("since"), since_id=req.q_int("since_id", 0), held=req.q("held") or None,
                                    unseen=req.q_bool("unseen"), q=req.q("q") or None, group=req.q("group") or None)
            except ValueError as exc:
                return {"ok": False, "status": 400, "error": str(exc)}
            return {"ok": True, "notifications": rows, "unseen": self.unseen_count()}
        if req.path == "/api/notify/config":
            return self.config_view()
        return None

    def post(self, req: Request) -> Optional[Any]:
        if not (req.path == "/api/notify" or req.path.startswith("/api/notify/")):
            return None
        who = req.caller()
        if who is None:
            return {"ok": False, "status": 401, "error": "a family bearer token is required"}
        body = req.body or {}
        admin = who in ("hub", "ui")
        if req.path == "/api/notify":
            claimed = str(body.get("app") or "").strip()
            if not admin and claimed and claimed != who:
                return {"ok": False, "status": 403, "error": "an app may not notify on behalf of another app"}
            app = (claimed or "hub") if admin else who
            override = body.get("channels") if admin and isinstance(body.get("channels"), list) else None
            tags = body.get("tags") if isinstance(body.get("tags"), (list, tuple)) else ()
            return self.send(str(body.get("title") or ""), str(body.get("body") or ""), app=app,
                             sphere=str(body.get("sphere") or "") or None, priority=str(body.get("priority") or "normal"),
                             url=str(body.get("url") or ""), group=str(body.get("group") or ""),
                             dedupe_key=str(body.get("dedupe_key") or ""), tags=tags,
                             icon_app=str(body.get("icon_app") or "") or None, channels_override=override)
        if not admin:
            return {"ok": False, "status": 403, "error": "only the hub's page or the hub token may do this"}
        if req.path == "/api/notify/config":
            return self.update_config(body)
        if req.path == "/api/notify/test":
            return self.test_channel(str(body.get("channel") or ""))
        if req.path == "/api/notify/read":
            if body.get("all"):
                return {"ok": True, "marked": self.mark_read(None), "unseen": self.unseen_count()}
            ids = body.get("ids")
            if not isinstance(ids, list):
                return {"ok": False, "status": 400, "error": "send {ids: [...]} or {all: true}"}
            return {"ok": True, "marked": self.mark_read(ids), "unseen": self.unseen_count()}
        if req.path == "/api/notify/telegram/discover":
            return self.telegram_discover_chat_id()
        return None

    # -- tools ---------------------------------------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": "hub_notify",
                "description": "Notify the person (toast, ntfy, Telegram, mail) / avísame, notifícame, mándame un aviso.\n"
                               "The hub routes by priority and sphere, holds it in quiet hours and drops duplicates. "
                               "Keywords: aviso, notificación, recordatorio, alerta, push.",
                "inputSchema": {"type": "object", "properties": {
                    "title": {"type": "string", "description": "Short headline."},
                    "body": {"type": "string", "description": "Optional detail."},
                    "priority": {"type": "string", "enum": list(PRIORITIES), "default": "normal",
                                 "description": "urgent also reaches the person in quiet hours."},
                    "url": {"type": "string", "description": "Optional link opened from the notification."},
                    "sphere": {"type": "string", "description": "Sphere id (default: the one of the sender)."},
                    "group": {"type": "string", "description": "Optional kind, e.g. 'job' or 'reminder'."},
                    "dedupe_key": {"type": "string", "description": "Same key within hours is sent once."},
                    "channels": {"type": "array", "items": {"type": "string", "enum": list(CHANNELS)},
                                 "description": "Force these channels instead of the sphere's routing."}},
                    "required": ["title"], "additionalProperties": False},
            },
            {
                "name": "hub_notify_history",
                "description": "Recent notifications the hub sent or held / avisos recientes, qué te he avisado.\n"
                               "Each: title, app, sphere, priority, channels with results, held reason (quiet, duplicate, "
                               "digest, rate). Keywords: historial de avisos, notificaciones, qué ha pasado.",
                "inputSchema": {"type": "object", "properties": {
                    "limit": {"type": "integer", "default": 20}, "app": {"type": "string"}, "sphere": {"type": "string"},
                    "held": {"type": "string", "description": "A reason, 'any' (held) or 'none' (delivered)."},
                    "unseen": {"type": "boolean", "default": False}, "q": {"type": "string"}},
                    "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
        ]

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        def notify(a: dict[str, Any]) -> dict[str, Any]:
            ch = a.get("channels")
            return self.send(str(a.get("title") or ""), str(a.get("body") or ""), app="hub",
                             sphere=str(a.get("sphere") or "") or None, priority=str(a.get("priority") or "normal"),
                             url=str(a.get("url") or ""), group=str(a.get("group") or ""),
                             dedupe_key=str(a.get("dedupe_key") or ""), channels_override=ch if isinstance(ch, list) else None)

        def hist(a: dict[str, Any]) -> dict[str, Any]:
            try:
                rows = self.history(int(a.get("limit") or 20), app=a.get("app") or None, sphere=a.get("sphere") or None,
                                    held=a.get("held") or None, unseen=bool(a.get("unseen")), q=a.get("q") or None)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "notifications": rows, "unseen": self.unseen_count()}

        return {"hub_notify": notify, "hub_notify_history": hist}
