"""The family's web service — the ``web`` facet (0.8): ONE polite fetcher for every app.

Until 0.8 each app reached the web on its own: two apps could hammer one host at the same time, only one of them read
robots.txt, and every app that needed a real browser kept its own profile. This facet owns a single shared
:class:`hoard_link.web.fetch.Fetcher` and gives the family:

* **one per-host state** in ``<data>/web.db`` (SQLite): the minimum interval, the block cooldown (30 minutes, or the
  server's ``Retry-After``), the tier that worked last time, the robots.txt cache, who asked last and what came back.
  It survives restarts, so a host that blocked the family stays quiet until the cooldown ends;
* **one throttle**: every request to a host, from any app, is serialized and spaced (``default_min_interval_s``,
  ``Crawl-delay`` honoured). One host never sees more than ``MAX_QUEUED_PER_HOST`` requests waiting;
* **one response cache** under ``<data>/web/cache`` with a ttl and stale-if-error;
* **one optional browser** (``hoard_link.web.browser.BrowserRung``) on ONE persistent family profile,
  ``<data>/web/browser-profile``. It needs ``playwright`` in the hub's Python; without it the ``browser`` tier answers
  ``{ok: false, error_kind: "unavailable"}`` and everything else keeps working. ``open_for_human`` opens that profile
  visibly so a person can solve a challenge (or the default browser when there is no playwright);
* **web search** (:class:`hoard_link.web.search.WebSearch`) with per-engine minimum intervals, an optional SearXNG URL
  and an optional Brave key;
* **link previews** (page metadata + favicon candidates, kept 7 days).

Policy: an app may only reach PUBLIC addresses. The ``operator_local`` profile (loopback, private ranges: things the
person configured) is for the hub's own page and the hub's agent. ``respect_robots`` is a hub setting first: an app can
ask for it but cannot switch it off while the setting is on.

Routes (a family bearer token, the hub's own token or the hub page is required; the page is caller ``ui``)::

    POST /api/web/fetch        {url, tier, accept, etag, last_modified, respect_robots, max_bytes, timeout,
                                extract, cache_ttl_s, fresh, profile}  -> FetchResult dict (+ text, body_b64, extract)
    POST /api/web/fetch_file   {url, dest_dir?, max_bytes}             -> {ok, path, sha256, content_type, size}
    POST /api/web/search       {query, limit, freshness_days, engines, news} -> {ok, hits, errors, engines}
    GET|POST /api/web/preview  ?url= / {url}                           -> {ok, title, description, image, favicon...}
    GET  /api/web/hosts                                                -> per-host state
    POST /api/web/hosts/clear  {host, reset?}                          (hub page / hub token only)
    POST /api/web/open         {url}                                   -> {ok, started, via}
    GET  /api/web/status

A fetch that fails is HTTP 200 with ``ok: false`` (the answer carries the upstream ``status``, ``error`` and
``error_kind``: the FetchResult kinds plus ``unavailable`` and ``busy``); HTTP errors mean the request itself was wrong
(400), unauthorised (401/403) or the facet is off (503). Every request is audited (caller, host, status, ms: never the
URL path or the body) and emitted as the hub event ``web.fetch`` / ``web.search``.

Settings (``hub.json`` -> ``web``): ``enabled``, ``user_agent``, ``default_min_interval_s``, ``respect_robots``,
``cache_ttl_s``, ``max_bytes``, ``searxng_url``, ``brave_api_key``, ``browser`` (``auto`` | ``off``). Hidden extras:
``search_intervals`` ({engine: seconds}), ``lang``, ``region``. The hub reads them when it starts (the intervals, user
agent, ttl and limits also when they change in memory); the browser mode is fixed when the first request builds the
fetcher.

Test seams: ``WebFacet.fetcher_options`` (extra ``Fetcher`` arguments, e.g. ``pin``/``retries``/``transport``),
``app_profile`` (the profile apps get: tests set ``operator_local`` so 127.0.0.1 stands in for a public host),
``resolver`` and ``open_default`` (opens a URL in the default browser).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import time
import webbrowser
from collections.abc import MutableMapping
from typing import Any, Callable, Iterator, Optional
from urllib.parse import unquote, urlsplit

from .. import paths as _paths
from ..fam_web import extract_payload
from ..web import browser as _browser
from ..web import meta as _meta
from ..web import safety
from ..web.fetch import DEFAULT_USER_AGENT, FetchResult, Fetcher
from ..web.search import DEFAULT_ENGINES, WebSearch
from ..web.urls import normalize_url
from .facets import Facet, Reply, Request

logger = logging.getLogger("hoard_hub.web")

MAX_TEXT_BYTES = 2 * 1024 * 1024          # "text" in a fetch answer
MAX_B64_BYTES = 5_000_000                 # "body_b64" only below this (accept="any")
MAX_FETCH_BYTES = 16 * 1024 * 1024        # a fetch never reads more than this, whatever max_bytes says
DEFAULT_FILE_BYTES = 50_000_000
MAX_FILE_BYTES = 500_000_000
PREVIEW_TTL_S = 7 * 86400
CACHE_KEEP_S = 7 * 86400                  # cache files older than this are deleted (stale-if-error reaches that far)
AUDIT_KEEP = 5000
MAX_QUEUED_PER_HOST = 20
EXTRACTS = ("readable", "markdown", "meta", "jsonld", "feed")
ACCEPTS = ("html", "json", "any")
TIERS = ("auto", "http", "browser", "window")
BROWSER_MODES = ("auto", "off")
_NO_REQUEST = ("unavailable", "busy", "blocked", "policy", "robots", "offline")     # answers that never reached the host
PLAYWRIGHT_MISSING = "browser tier unavailable: playwright is not installed in the hub's Python"
BROWSER_OFF = "browser tier unavailable: the browser tier is turned off in hub.json (web.browser = off)"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "user_agent": "",                      # "" = the family's one desktop-browser string
    "default_min_interval_s": 2.0,
    "respect_robots": True,
    "cache_ttl_s": 300.0,
    "max_bytes": 3 * 1024 * 1024,
    "searxng_url": "",
    "brave_api_key": "",
    "browser": "auto",
    "search_intervals": {},
    "lang": "es",
    "region": "ES",
}


def web_settings(raw: Any) -> dict[str, Any]:
    """``hub.json``'s ``web`` over the defaults. Lenient: a value of the wrong type or range keeps the default."""
    out = dict(DEFAULT_SETTINGS)
    out["search_intervals"] = {}
    if not isinstance(raw, dict):
        return out

    def num(key: str, lo: float, hi: float) -> None:
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            out[key] = float(v) if key != "max_bytes" else int(v)

    for key in ("enabled", "respect_robots"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    num("default_min_interval_s", 0, 3600)
    num("cache_ttl_s", 0, 30 * 86400)
    num("max_bytes", 1024, MAX_FETCH_BYTES)
    for key in ("user_agent", "searxng_url", "brave_api_key", "lang", "region"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key].strip()
    if raw.get("browser") in BROWSER_MODES:
        out["browser"] = raw["browser"]
    iv = raw.get("search_intervals")
    if isinstance(iv, dict):
        out["search_intervals"] = {str(k): float(v) for k, v in iv.items()
                                  if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 600}
    return out


# ---------------------------------------------------------------------------------------------- the store
_HOST_COLS = {"last_fetch_ts": 0.0, "min_interval_s": None, "blocked_until_ts": 0.0, "block_reason": "", "preferred_tier": "",
              "ok_count": 0, "fail_count": 0, "last_status": 0, "last_caller": "", "last_error": ""}
_HOST_SQL = {"last_fetch_ts": "REAL", "min_interval_s": "REAL", "blocked_until_ts": "REAL", "block_reason": "TEXT",
             "preferred_tier": "TEXT", "ok_count": "INTEGER", "fail_count": "INTEGER", "last_status": "INTEGER",
             "last_caller": "TEXT", "last_error": "TEXT"}


class WebDb:
    """``<data>/web.db``: host state, the robots.txt cache, the audit trail and the preview cache, over one connection."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        cols = ", ".join(f"{k} {t}" for k, t in _HOST_SQL.items())
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, {cols})")
        self._conn.execute("CREATE TABLE IF NOT EXISTS robots (origin TEXT PRIMARY KEY, text TEXT NOT NULL, ts REAL NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                           "caller TEXT NOT NULL, kind TEXT NOT NULL, host TEXT NOT NULL, status INTEGER NOT NULL, "
                           "ok INTEGER NOT NULL, tier TEXT NOT NULL, ms INTEGER NOT NULL, from_cache INTEGER NOT NULL, "
                           "error_kind TEXT NOT NULL, detail TEXT NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS previews (key TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL)")
        self._conn.commit()
        self.hosts = HostState(self)
        self.robots = RobotsStore(self)
        self._audit_writes = 0

    def close(self) -> None:
        with self.lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def run(self, sql: str, args: tuple = (), *, commit: bool = False) -> list[tuple]:
        with self.lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall() if cur.description else []
            if commit:
                self._conn.commit()
            return rows

    # -- audit
    def audit(self, caller: str, kind: str, host: str, *, status: int = 0, ok: bool = False, tier: str = "", ms: int = 0,
              from_cache: bool = False, error_kind: str = "", detail: str = "") -> None:
        self.run("INSERT INTO audit (ts, caller, kind, host, status, ok, tier, ms, from_cache, error_kind, detail) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (time.time(), caller[:60], kind, host[:200], int(status or 0), int(bool(ok)), tier[:20], int(ms or 0),
                  int(bool(from_cache)), error_kind[:20], detail[:160]), commit=True)
        self._audit_writes += 1
        if self._audit_writes % 200 == 0:
            self.prune_audit()

    def prune_audit(self, keep: int = AUDIT_KEEP) -> None:
        self.run("DELETE FROM audit WHERE id <= (SELECT MAX(id) FROM audit) - ?", (keep,), commit=True)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.run("SELECT ts, caller, kind, host, status, ok, tier, ms, from_cache, error_kind, detail FROM audit "
                        "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))
        keys = ("ts", "caller", "kind", "host", "status", "ok", "tier", "ms", "from_cache", "error_kind", "detail")
        return [{**dict(zip(keys, r)), "ok": bool(r[5]), "from_cache": bool(r[8])} for r in rows]

    # -- previews
    def preview_get(self, key: str) -> Optional[tuple[float, dict[str, Any]]]:
        rows = self.run("SELECT ts, payload FROM previews WHERE key=?", (key,))
        if not rows:
            return None
        try:
            return float(rows[0][0]), json.loads(rows[0][1])
        except (TypeError, ValueError):
            return None

    def preview_put(self, key: str, payload: dict[str, Any]) -> None:
        self.run("INSERT OR REPLACE INTO previews (key, ts, payload) VALUES (?,?,?)",
                 (key, time.time(), json.dumps(payload, ensure_ascii=False)), commit=True)

    def prune(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        self.run("DELETE FROM previews WHERE ts < ?", (now - PREVIEW_TTL_S * 2,), commit=True)
        self.prune_audit()


class HostState:
    """The :class:`hoard_link.web.fetch.HostStateStore` of the hub, backed by ``web.db`` (survives restarts)."""

    def __init__(self, db: WebDb):
        self._db = db

    @staticmethod
    def _row(values: tuple) -> dict[str, Any]:
        row = dict(zip(_HOST_COLS, values))
        for key, default in _HOST_COLS.items():
            if row.get(key) is None and default is not None:
                row[key] = default
        return row

    def get(self, host: str) -> dict[str, Any]:
        rows = self._db.run(f"SELECT {', '.join(_HOST_COLS)} FROM hosts WHERE host=?", (host,))
        return self._row(rows[0]) if rows else dict(_HOST_COLS)

    def update(self, host: str, **cols: Any) -> None:
        cols = {k: v for k, v in cols.items() if k in _HOST_COLS}
        with self._db.lock:
            self._db.run("INSERT OR IGNORE INTO hosts (host) VALUES (?)", (host,), commit=not cols)
            if cols:
                sets = ", ".join(f"{k}=?" for k in cols)
                self._db.run(f"UPDATE hosts SET {sets} WHERE host=?", (*cols.values(), host), commit=True)

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self._db.run(f"SELECT host, {', '.join(_HOST_COLS)} FROM hosts ORDER BY host")
        return [(r[0], self._row(r[1:])) for r in rows]


class RobotsStore(MutableMapping):
    """``{origin: {"text", "ts"}}`` in ``web.db`` (what :class:`hoard_link.web.robots.RobotsCache` keeps between runs)."""

    def __init__(self, db: WebDb):
        self._db = db

    def __getitem__(self, key: str) -> dict[str, Any]:
        rows = self._db.run("SELECT text, ts FROM robots WHERE origin=?", (key,))
        if not rows:
            raise KeyError(key)
        return {"text": rows[0][0], "ts": rows[0][1]}

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        self._db.run("INSERT OR REPLACE INTO robots (origin, text, ts) VALUES (?,?,?)",
                     (key, str(value.get("text", "")), float(value.get("ts", time.time()))), commit=True)

    def __delitem__(self, key: str) -> None:
        self._db.run("DELETE FROM robots WHERE origin=?", (key,), commit=True)

    def __iter__(self) -> Iterator[str]:
        return iter([r[0] for r in self._db.run("SELECT origin FROM robots")])

    def __len__(self) -> int:
        return int(self._db.run("SELECT COUNT(*) FROM robots")[0][0])


# ---------------------------------------------------------------------------------------------- helpers
def _cap_text(text: str, cap: Optional[int] = None) -> tuple[str, bool]:
    cap = MAX_TEXT_BYTES if cap is None else cap
    if len(text) * 4 <= cap or len(text.encode("utf-8")) <= cap:
        return text, False
    return text.encode("utf-8")[:cap].decode("utf-8", "ignore"), True


class _Bad(ValueError):
    """A request the caller got wrong (HTTP 400)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _num(body: dict[str, Any], key: str, lo: float, hi: float, default: Optional[float]) -> Optional[float]:
    v = body.get(key)
    if v is None or v == "":
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _Bad(f"{key}: a number between {lo:g} and {hi:g}")
    if not lo <= v <= hi:
        raise _Bad(f"{key}: a number between {lo:g} and {hi:g}")
    return float(v)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "").strip())[:60].strip("._") or "app"


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _file_name(fr: FetchResult) -> str:
    """The file name of a download: Content-Disposition, else the last URL segment, cleaned for Windows."""
    cd = fr.headers.get("content-disposition", "")
    name = ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    if m:
        name = unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            name = m.group(1).strip()
    if not name:
        name = unquote(urlsplit(fr.final_url or fr.url).path.rsplit("/", 1)[-1])
    name = re.split(r"[\\/]", name)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")[:120]
    if not name:
        name = "download"
    stem, ext = os.path.splitext(name)
    if not ext:
        guess = mimetypes.guess_extension((fr.content_type or "").split(";")[0].strip()) if fr.content_type else None
        name += guess or ""
    if stem.lower() in _RESERVED:
        name = "_" + name
    return name


def _write_unique(folder: str, name: str, data: bytes, sha: str) -> str:
    """Write ``data`` into ``folder`` as ``name``; the same bytes already there are reused, other bytes get ``name (2)``."""
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(name)
    for n in range(1, 100):
        path = os.path.join(folder, name if n == 1 else f"{stem} ({n}){ext}")
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() == sha:
                        return path
            except OSError:
                pass
            continue
        tmp = path + f".{os.getpid()}.part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path
    raise OSError("too many files with that name")


# ---------------------------------------------------------------------------------------------- the facet
class WebFacet(Facet):
    id = "web"
    ui_scripts = ("web.js",)

    #: Extra ``Fetcher`` arguments (tests: ``pin``, ``retries``, ``transport``, ``browser``). Copied per instance.
    fetcher_options: dict[str, Any] = {}

    def __init__(self, hub: Any):
        super().__init__(hub)
        data_dir = hub.config.data_dir
        self.root = os.path.join(data_dir, "web")
        self.cache_dir = os.path.join(self.root, "cache")
        self.files_dir = os.path.join(self.root, "files")
        self.profile_dir = os.path.join(self.root, "browser-profile")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.files_dir, exist_ok=True)
        self.db = WebDb(os.path.join(data_dir, "web.db"))
        self.fetcher_options = dict(type(self).fetcher_options)
        self.app_profile = safety.PUBLIC
        self.resolver: Optional[safety.Resolver] = None
        self.open_default: Callable[[str], Any] = webbrowser.open
        self._build_lock = threading.Lock()
        self._fetcher: Optional[Fetcher] = None
        self._search: Optional[WebSearch] = None
        self._browser_mode = "auto"
        self._queued: dict[str, int] = {}
        self._queued_lock = threading.Lock()
        self._human = threading.Event()
        self._closed = False

    # -- settings / parts ------------------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return web_settings(getattr(self.hub.config, "web", None))

    def fetcher(self) -> Fetcher:
        cfg = self.settings()
        with self._build_lock:
            if self._fetcher is None:
                opts: dict[str, Any] = {
                    "user_agent": cfg["user_agent"] or DEFAULT_USER_AGENT, "profile": self.app_profile, "state": self.db.hosts,
                    "cache_dir": self.cache_dir, "cache_ttl_s": cfg["cache_ttl_s"], "stale_if_error": True,
                    "min_interval_s": cfg["default_min_interval_s"], "max_bytes": cfg["max_bytes"],
                    "robots_store": self.db.robots, "resolver": self.resolver}
                self._browser_mode = cfg["browser"]
                if cfg["browser"] == "off":
                    opts["browser"] = False
                else:
                    opts["browser_profile_dir"] = self.profile_dir
                opts.update(self.fetcher_options)
                self._fetcher = Fetcher(**opts)
            f = self._fetcher
            f.user_agent = cfg["user_agent"] or DEFAULT_USER_AGENT
            f.min_interval_s = cfg["default_min_interval_s"]
            f.cache_ttl_s = cfg["cache_ttl_s"]
            f.max_bytes = cfg["max_bytes"]
            if self._search is None:
                self._search = WebSearch(f, lang=cfg["lang"] or "es", region=cfg["region"] or "ES")
            self._search.searxng_url = cfg["searxng_url"].rstrip("/")
            self._search.brave_key = cfg["brave_api_key"]
            self._search.intervals = {**self._search.intervals, **cfg["search_intervals"]}
            return f

    def web_search(self) -> WebSearch:
        self.fetcher()
        assert self._search is not None
        return self._search

    def browser_state(self) -> tuple[bool, str]:
        """``(available, reason)`` of the browser tier."""
        f = self.fetcher()
        if self._browser_mode == "off":
            return False, BROWSER_OFF
        rung = f._rung()
        if rung is None:
            return False, "browser tier unavailable: no browser is configured"
        try:
            if rung.available():
                return True, ""
        except Exception:  # noqa: BLE001
            pass
        if not _browser.playwright_installed():
            return False, PLAYWRIGHT_MISSING
        return False, "browser tier unavailable: " + (getattr(rung, "unavailable_reason", lambda: "")() or "the browser cannot start")

    def start(self) -> None:
        threading.Thread(target=self._janitor, name="hoard-hub-web-janitor", daemon=True).start()

    def _janitor(self) -> None:
        """Forget what is too old to be useful: cache files after a week, old previews, old audit rows."""
        try:
            limit = time.time() - CACHE_KEEP_S
            for sub in os.listdir(self.cache_dir):
                folder = os.path.join(self.cache_dir, sub)
                if not os.path.isdir(folder):
                    continue
                for name in os.listdir(folder):
                    path = os.path.join(folder, name)
                    try:
                        if os.stat(path).st_mtime < limit:
                            os.unlink(path)
                    except OSError:
                        pass
            self.db.prune()
        except Exception:  # noqa: BLE001 - housekeeping must never hurt
            logger.debug("web janitor failed", exc_info=True)

    def close(self) -> None:
        self._closed = True
        with self._build_lock:
            f, self._fetcher = self._fetcher, None
        if f is not None:
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
        self.db.close()

    # -- callers and policy ----------------------------------------------------------------------------
    @staticmethod
    def _caller_name(who: str, admin: bool, body: dict[str, Any]) -> str:
        if admin and isinstance(body.get("caller"), str) and body["caller"].strip():
            return _slug(body["caller"])
        return _slug(who)

    def _profile(self, admin: bool, requested: Any) -> str:
        if requested in (None, "", safety.PUBLIC):
            return self.app_profile
        if requested == safety.OPERATOR_LOCAL:
            if not admin:
                raise _Bad("the operator_local profile is only for the hub's page and the hub's agent", 403)
            return safety.OPERATOR_LOCAL
        raise _Bad("profile: public or operator_local")

    def _robots(self, admin: bool, body: dict[str, Any]) -> bool:
        asked = body.get("respect_robots")
        if asked is True:
            return True
        if not self.settings()["respect_robots"]:
            return False
        return not (admin and asked is False)

    def _ttl(self, body: dict[str, Any]) -> Optional[float]:
        if body.get("fresh") is True:
            return 0.0
        return _num(body, "cache_ttl_s", 0, 30 * 86400, None)

    # -- one fetch, through the shared fetcher ---------------------------------------------------------
    def _enter(self, host: str) -> bool:
        with self._queued_lock:
            if self._queued.get(host, 0) >= MAX_QUEUED_PER_HOST:
                return False
            self._queued[host] = self._queued.get(host, 0) + 1
            return True

    def _leave(self, host: str) -> None:
        with self._queued_lock:
            n = self._queued.get(host, 1) - 1
            if n <= 0:
                self._queued.pop(host, None)
            else:
                self._queued[host] = n

    def _tier_problem(self, tier: str) -> str:
        if tier in ("browser", "window"):
            ok, why = self.browser_state()
            if not ok:
                return why
        return ""

    def _refused(self, url: str, tier: str, message: str, kind: str) -> FetchResult:
        fr = FetchResult(url=url, tier=tier if tier in ("browser", "window") else "http", fetched_at=time.time())
        fr.error, fr.error_kind = message, kind
        return fr

    def _run(self, caller: str, kind: str, url: str, *, tier: str = "auto", detail: str = "", **kw: Any) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        started = time.monotonic()
        problem = self._tier_problem(tier)
        if kw.get("profile") != self.app_profile:
            kw["cache_ttl_s"] = 0          # what the operator reached on this machine is never cached for the apps
        fr: FetchResult
        if problem:
            fr = self._refused(url, tier, problem, "unavailable")
        elif not self._enter(host):
            fr = self._refused(url, tier, f"too many requests are already waiting for {host}; try again later", "busy")
        else:
            try:
                fr = self.fetcher().get(url, tier=tier, **kw)
            finally:
                self._leave(host)
        ms = int((time.monotonic() - started) * 1000)
        self._after(caller, kind, host, fr, ms, detail)
        return fr

    def _after(self, caller: str, kind: str, host: str, fr: FetchResult, ms: int, detail: str = "") -> None:
        try:
            if host and not fr.from_cache and not (not fr.status and fr.error_kind in _NO_REQUEST):
                self.db.hosts.update(host, last_status=int(fr.status or 0), last_caller=caller, last_error=(fr.error or "")[:200])
            self.db.audit(caller, kind, host, status=fr.status, ok=fr.ok, tier=fr.tier, ms=ms, from_cache=fr.from_cache,
                          error_kind=fr.error_kind, detail=detail)
            self.hub.emit("web.fetch", {"host": host, "status": int(fr.status or 0), "ms": ms, "caller": caller, "ok": fr.ok,
                                        "tier": fr.tier, "from_cache": fr.from_cache, "kind": kind,
                                        **({"error_kind": fr.error_kind} if fr.error_kind else {})})
        except Exception:  # noqa: BLE001 - bookkeeping never fails a fetch
            logger.debug("web audit failed", exc_info=True)

    def _blocked_until(self, url: str) -> float:
        host = (urlsplit(url).hostname or "").lower()
        until = float(self.db.hosts.get(host).get("blocked_until_ts") or 0)
        return until if until > time.time() else 0.0

    # -- answers -----------------------------------------------------------------------------------------
    def _payload(self, fr: FetchResult, *, extract: Optional[str] = None, accept: str = "html") -> dict[str, Any]:
        d = fr.to_dict(with_text=False)
        d["headers"] = {k: v for k, v in (d.get("headers") or {}).items() if k != "set-cookie"}
        d["text"], d["text_truncated"] = _cap_text(fr.text or "")
        if accept == "any" and fr.body is not None:
            if len(fr.body) <= MAX_B64_BYTES:
                d["body_b64"] = base64.b64encode(fr.body).decode("ascii")
            else:
                d["body_omitted"] = True
            d["body_size"] = len(fr.body)
        if fr.blocked or fr.error_kind == "blocked":
            until = self._blocked_until(fr.url)
            if until:
                d["blocked_until_ts"] = until
        if extract and fr.ok and not fr.not_modified and fr.text:
            d["extract"] = self._extract(extract, fr)
        return d

    @staticmethod
    def _extract(kind: str, fr: FetchResult) -> dict[str, Any]:
        return extract_payload(kind, fr.text, fr.final_url or fr.url, cap=MAX_TEXT_BYTES)

    # -- fetch -------------------------------------------------------------------------------------------
    def fetch(self, who: str, admin: bool, body: dict[str, Any]) -> Any:
        url = str(body.get("url") or "").strip()
        if not url:
            raise _Bad("url is required")
        if len(url) > safety.MAX_URL_LEN:
            raise _Bad("url is too long")
        accept = body.get("accept") or "html"
        if accept not in ACCEPTS:
            raise _Bad("accept: html, json or any")
        tier = body.get("tier") or "auto"
        if tier not in TIERS:
            raise _Bad("tier: " + ", ".join(TIERS))
        if tier == "window" and not admin:
            raise _Bad("the window tier opens a visible browser: only the hub's page and agent may use it", 403)
        extract = body.get("extract") or None
        if extract is not None and extract not in EXTRACTS:
            raise _Bad("extract: " + ", ".join(EXTRACTS))
        if extract and accept != "html":
            raise _Bad("extract needs accept=html")
        profile = self._profile(admin, body.get("profile"))
        cap = int(_num(body, "max_bytes", 1024, MAX_FETCH_BYTES, None) or self.settings()["max_bytes"])
        timeout = _num(body, "timeout", 1, 120, None)
        caller = self._caller_name(who, admin, body)
        fr = self._run(caller, "fetch", url, tier=tier, accept=accept, etag=str(body.get("etag") or ""),
                       last_modified=str(body.get("last_modified") or ""), respect_robots=self._robots(admin, body),
                       max_bytes=min(cap, MAX_FETCH_BYTES), timeout=timeout, cache_ttl_s=self._ttl(body), profile=profile)
        return Reply(self._payload(fr, extract=extract, accept=accept))

    # -- fetch_file ----------------------------------------------------------------------------------------
    def fetch_file(self, who: str, admin: bool, body: dict[str, Any]) -> Any:
        url = str(body.get("url") or "").strip()
        if not url:
            raise _Bad("url is required")
        cap = int(_num(body, "max_bytes", 1, MAX_FILE_BYTES, DEFAULT_FILE_BYTES) or DEFAULT_FILE_BYTES)
        profile = self._profile(admin, body.get("profile"))
        caller = self._caller_name(who, admin, body)
        dest = body.get("dest_dir")
        if dest not in (None, ""):
            if not isinstance(dest, str) or not os.path.isabs(dest):
                raise _Bad("dest_dir must be an absolute folder path")
            why = _paths.unsafe_output_dir(dest, data_dir=self.hub.config.data_dir, allow_data_subdir=self.files_dir, lang="en")
            if why:
                raise _Bad(f"dest_dir refused: {why}")
            folder = dest
        else:
            folder = os.path.join(self.files_dir, caller)
        timeout = _num(body, "timeout", 1, 3600, 120)
        fr = self._run(caller, "file", url, tier="http", accept="any", respect_robots=False, max_bytes=cap, timeout=timeout,
                       cache_ttl_s=0, profile=profile)
        base = {"url": url, "final_url": fr.final_url, "status": fr.status, "content_type": fr.content_type}
        if not fr.ok or fr.body is None:
            return Reply({**base, "ok": False, "error": fr.error or "nothing was downloaded", "error_kind": fr.error_kind or "network",
                          "blocked": fr.blocked, "block_reason": fr.block_reason})
        if fr.truncated:
            return Reply({**base, "ok": False, "error": f"the file is larger than max_bytes ({cap}); nothing was saved",
                          "error_kind": "content"})
        sha = hashlib.sha256(fr.body).hexdigest()
        try:
            path = _write_unique(folder, _file_name(fr), fr.body, sha)
        except OSError as exc:
            return Reply({**base, "ok": False, "error": f"could not save the file: {exc}", "error_kind": "content"})
        return Reply({**base, "ok": True, "path": path, "sha256": sha, "size": len(fr.body), "filename": os.path.basename(path)})

    # -- search ----------------------------------------------------------------------------------------------
    def search(self, who: str, admin: bool, body: dict[str, Any]) -> Any:
        query = re.sub(r"\s+", " ", str(body.get("query") or "")).strip()
        if not query:
            raise _Bad("query is required")
        limit = int(_num(body, "limit", 1, 50, 10) or 10)
        days = _num(body, "freshness_days", 1, 3650, None)
        engines = body.get("engines")
        if engines is not None:
            if isinstance(engines, str):
                engines = [e for e in re.split(r"[,\s]+", engines) if e]
            if not isinstance(engines, list) or not all(isinstance(e, str) for e in engines):
                raise _Bad("engines: a list of engine names")
            unknown = [e for e in engines if e not in DEFAULT_ENGINES]
            if unknown:
                raise _Bad(f"unknown engine(s): {', '.join(unknown)} (use {', '.join(DEFAULT_ENGINES)})")
        news = bool(body.get("news"))
        caller = self._caller_name(who, admin, body)
        ws = self.web_search()
        started = time.monotonic()
        try:
            hits, errors = ws.search(query, limit, freshness_days=int(days) if days else None, engines=engines or None, news=news)
        except Exception as exc:  # noqa: BLE001
            hits, errors = [], {"search": f"{type(exc).__name__}: {exc}"[:200]}
        ms = int((time.monotonic() - started) * 1000)
        used = engines or ws.available_engines(news)
        try:
            self.db.audit(caller, "search", "", ok=bool(hits), ms=ms, detail=query[:120],
                          error_kind="" if hits or not errors else "network")
            self.hub.emit("web.search", {"caller": caller, "hits": len(hits), "ms": ms, "engines": list(used), "news": news})
        except Exception:  # noqa: BLE001
            logger.debug("web search audit failed", exc_info=True)
        out: dict[str, Any] = {"ok": bool(hits) or not errors, "query": query, "hits": hits, "errors": errors,
                               "engines": list(used), "ms": ms}
        if not out["ok"]:
            out["error"] = "; ".join(f"{k}: {v}" for k, v in errors.items())[:300]
        return Reply(out)

    # -- preview ---------------------------------------------------------------------------------------------
    def preview(self, who: str, admin: bool, body: dict[str, Any]) -> Any:
        url = str(body.get("url") or "").strip()
        if not url:
            raise _Bad("url is required")
        key_url = normalize_url(url)
        if not key_url:
            raise _Bad("url: an http(s) address")
        profile = self._profile(admin, body.get("profile"))
        caller = self._caller_name(who, admin, body)
        key = hashlib.sha256(key_url.encode("utf-8")).hexdigest()
        shared = profile == self.app_profile                  # a preview made with the operator's wider profile is not kept
        cached = self.db.preview_get(key) if shared else None
        now = time.time()
        if cached and now - cached[0] <= PREVIEW_TTL_S and not body.get("fresh"):
            return Reply({**cached[1], "from_cache": True, "cached_at": cached[0]})
        fr = self._run(caller, "preview", url, tier="auto", accept="html", respect_robots=self._robots(admin, body),
                       cache_ttl_s=0.0 if body.get("fresh") else self._ttl(body), profile=profile)
        if fr.ok and fr.text:
            base = fr.final_url or url
            m = _meta.page_meta(fr.text, base)
            payload = {"ok": True, "url": url, "final_url": base, "status": fr.status, "tier": fr.tier,
                       "title": m["title"], "description": m["description"], "image": m["image"], "site_name": m["site_name"],
                       "favicon": m["favicon"], "favicons": _meta.favicon_candidates(fr.text, base), "canonical": m["canonical"],
                       "lang": m["lang"], "author": m["author"], "published": m["published"], "from_cache": False}
            if shared:
                try:
                    self.db.preview_put(key, payload)
                except sqlite3.Error:
                    pass
            return Reply(payload)
        if cached:
            return Reply({**cached[1], "from_cache": True, "stale": True, "cached_at": cached[0],
                          "note": f"served from the preview cache after a failure: {fr.error}"})
        return Reply({"ok": False, "url": url, "status": fr.status, "error": fr.error or "no usable page",
                      "error_kind": fr.error_kind, "blocked": fr.blocked, "block_reason": fr.block_reason})

    # -- hosts -----------------------------------------------------------------------------------------------
    def hosts(self) -> dict[str, Any]:
        f = self.fetcher()
        now = time.time()
        rows = []
        for host, row in self.db.hosts.items():
            until = float(row.get("blocked_until_ts") or 0)
            interval = row.get("min_interval_s")
            rows.append({**row, "host": host, "blocked_now": until > now, "blocked_until_ts": until if until > now else 0.0,
                         "effective_min_interval_s": float(interval) if interval is not None else f.min_interval_s})
        rows.sort(key=lambda r: (not r["blocked_now"], -float(r.get("last_fetch_ts") or 0), r["host"]))
        return {"ok": True, "hosts": rows, "blocked": sum(1 for r in rows if r["blocked_now"])}

    def clear_host(self, host: str, reset: bool = False) -> dict[str, Any]:
        host = re.sub(r"^[a-z]+://", "", str(host or "").strip().lower()).split("/")[0].split(":")[0]
        if not host:
            return {"ok": False, "status": 400, "error": "host is required"}
        if not any(h == host for h, _ in self.db.hosts.items()):
            return {"ok": False, "status": 404, "error": f"no state for {host}"}
        f = self.fetcher()
        f.reset_host(host) if reset else f.clear_block(host)
        return {"ok": True, "host": host, "reset": bool(reset)}

    # -- open for a human ----------------------------------------------------------------------------------
    def open_for_human(self, who: str, admin: bool, body: dict[str, Any]) -> dict[str, Any]:
        url = str(body.get("url") or "").strip()
        if not url:
            raise _Bad("url is required")
        problem = safety.check_url(url, self._profile(admin, body.get("profile")), self.resolver)
        if problem:
            raise _Bad(problem)
        host = (urlsplit(url).hostname or "").lower()
        caller = self._caller_name(who, admin, body)
        ok, _why = self.browser_state()
        if not ok:
            try:
                opened = self.open_default(url)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "status": 500, "error": f"could not open the default browser: {type(exc).__name__}"}
            if opened is False:
                return {"ok": False, "status": 500, "error": "no default browser could be opened"}
            self.hub.emit("web.open", {"host": host, "caller": caller, "via": "default_browser"})
            return {"ok": True, "started": True, "via": "default_browser",
                    "note": "opened in the default browser (the family browser profile needs playwright in the hub's Python)"}
        if self._human.is_set():
            return {"ok": False, "status": 409, "error": "a browser window is already open; close it first"}
        self._human.set()
        fetcher = self.fetcher()

        def run() -> None:
            closed = None
            try:
                res = fetcher.open_for_human(url)
                closed = res.get("closed_by_user") if isinstance(res, dict) else None
            except Exception as exc:  # noqa: BLE001
                logger.info("open_for_human failed: %s", type(exc).__name__)
            finally:
                self._human.clear()
                try:
                    self.hub.emit("web.open.done", {"host": host, "caller": caller, "closed_by_user": closed})
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=run, name="hoard-hub-web-human", daemon=True).start()
        self.hub.emit("web.open", {"host": host, "caller": caller, "via": "family_profile"})
        return {"ok": True, "started": True, "via": "family_profile",
                "note": "a window with the family browser profile is open; solve the challenge and close it"}

    # -- status ----------------------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        cfg = self.settings()
        out: dict[str, Any] = {"ok": True, "enabled": cfg["enabled"], "respect_robots": cfg["respect_robots"],
                               "default_min_interval_s": cfg["default_min_interval_s"], "cache_ttl_s": cfg["cache_ttl_s"],
                               "max_bytes": cfg["max_bytes"], "user_agent": cfg["user_agent"] or DEFAULT_USER_AGENT,
                               "searxng": bool(cfg["searxng_url"]), "brave": bool(cfg["brave_api_key"])}
        if not cfg["enabled"]:
            return out
        f = self.fetcher()
        ok, why = self.browser_state()
        hosts = self.hosts()
        out.update({"browser": {"mode": self._browser_mode, "available": ok, "reason": why, "profile_dir": self.profile_dir,
                                "human_window_open": self._human.is_set()},
                    "cache": f.cache_stats(), "engines": self.web_search().available_engines(),
                    "news_engines": self.web_search().available_engines(news=True), "hosts": len(hosts["hosts"]),
                    "blocked_now": hosts["blocked"], "recent": self.db.recent(15)})
        return out

    # -- HTTP --------------------------------------------------------------------------------------------------
    def _disabled(self) -> dict[str, Any]:
        return {"ok": False, "status": 503, "error": "the web service is turned off (hub.json: web.enabled = false)"}

    def _guard(self, req: Request) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        who = req.caller()
        if who is None:
            return None, {"ok": False, "status": 401, "error": "a family bearer token is required: the hub's data/mcp-token or any app's own"}
        return who, None

    def get(self, req: Request) -> Optional[Any]:
        if not req.path.startswith("/api/web/"):
            return None
        who, err = self._guard(req)
        if err:
            return err
        assert who is not None
        admin = who in ("hub", "ui")
        try:
            if req.path == "/api/web/status":
                return self.status()
            if not self.settings()["enabled"]:
                return self._disabled()
            if req.path == "/api/web/hosts":
                return self.hosts()
            if req.path == "/api/web/preview":
                return self.preview(who, admin, {"url": req.q("url") or "", "fresh": req.q_bool("fresh")})
        except _Bad as exc:
            return {"ok": False, "status": exc.status, "error": str(exc)}
        return None

    def post(self, req: Request) -> Optional[Any]:
        if not req.path.startswith("/api/web/"):
            return None
        who, err = self._guard(req)
        if err:
            return err
        assert who is not None
        admin = who in ("hub", "ui")
        body = req.body or {}
        if not self.settings()["enabled"]:
            return self._disabled()
        try:
            if req.path == "/api/web/fetch":
                return self.fetch(who, admin, body)
            if req.path == "/api/web/fetch_file":
                return self.fetch_file(who, admin, body)
            if req.path == "/api/web/search":
                return self.search(who, admin, body)
            if req.path == "/api/web/preview":
                return self.preview(who, admin, body)
            if req.path == "/api/web/open":
                return self.open_for_human(who, admin, body)
            if req.path == "/api/web/hosts/clear":
                if not admin:
                    return {"ok": False, "status": 403, "error": "only the hub's page or the hub token may clear a block"}
                return self.clear_host(str(body.get("host") or ""), bool(body.get("reset")))
        except _Bad as exc:
            return {"ok": False, "status": exc.status, "error": str(exc)}
        return None

    # -- tools -------------------------------------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": "hub_web_fetch",
                "description": "Fetch a web page politely (robots, throttle, cache). "
                               "Keywords: descargar página, leer web, scrape\n"
                               "One shared fetcher for every app: per-host spacing, block cooldowns, a disk cache and an optional "
                               "real browser. Returns the readable text (or markdown, meta, jsonld, feed) and the HTTP status.",
                "inputSchema": {"type": "object", "properties": {
                    "url": {"type": "string", "description": "http(s) address."},
                    "extract": {"type": "string", "enum": [*EXTRACTS, "raw"], "default": "readable",
                                "description": "What to return: readable text, markdown, page meta, JSON-LD, a feed, or the raw text."},
                    "tier": {"type": "string", "enum": ["auto", "http", "browser"], "default": "auto",
                             "description": "browser needs playwright in the hub's Python."},
                    "max_chars": {"type": "integer", "default": 20000, "description": "Cut the returned text here."},
                    "fresh": {"type": "boolean", "default": False, "description": "Skip the cache."},
                    "profile": {"type": "string", "enum": ["public", "operator_local"], "default": "public",
                                "description": "operator_local also reaches this machine and the local network."}},
                    "required": ["url"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "openWorldHint": True},
            },
            {
                "name": "hub_web_search",
                "description": "Search the web (SearXNG, DDG, Bing, Brave, news). "
                               "Keywords: buscar en internet, buscar en la web, noticias\n"
                               "Merges the engines with rank fusion; one engine failing never hides the others. "
                               "Returns hits {title, url, snippet, engine, rank, published} and per-engine errors.",
                "inputSchema": {"type": "object", "properties": {
                    "query": {"type": "string"}, "limit": {"type": "integer", "default": 10},
                    "freshness_days": {"type": "integer", "description": "Only results from the last N days."},
                    "engines": {"type": "array", "items": {"type": "string", "enum": list(DEFAULT_ENGINES)}},
                    "news": {"type": "boolean", "default": False, "description": "Search the news engines."}},
                    "required": ["query"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "openWorldHint": True},
            },
            {
                "name": "hub_web_preview",
                "description": "Link preview: title, description, image, favicon. "
                               "Keywords: vista previa, enlace, metadatos, favicon\n"
                               "Cached for 7 days. Use it to label a link without reading the whole page.",
                "inputSchema": {"type": "object", "properties": {
                    "url": {"type": "string"}, "fresh": {"type": "boolean", "default": False}},
                    "required": ["url"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "openWorldHint": True},
            },
            {
                "name": "hub_web_hosts",
                "description": "Per-host web state: blocks, intervals. "
                               "Keywords: hosts bloqueados, desbloquear, límite de peticiones\n"
                               "Lists every host the family has fetched. Pass clear_block to lift a host's cooldown.",
                "inputSchema": {"type": "object", "properties": {
                    "clear_block": {"type": "string", "description": "A host whose block cooldown to lift."}},
                    "additionalProperties": False},
            },
        ]

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        def guarded(fn: Callable[[], Any]) -> dict[str, Any]:
            if not self.settings()["enabled"]:
                return {"ok": False, "error": "the web service is turned off (hub.json: web.enabled = false)"}
            try:
                res = fn()
            except _Bad as exc:
                return {"ok": False, "error": str(exc)}
            return res.payload if isinstance(res, Reply) else res

        def web_fetch(a: dict[str, Any]) -> dict[str, Any]:
            kind = str(a.get("extract") or "readable")
            body = {"url": a.get("url"), "tier": a.get("tier") or "auto", "fresh": bool(a.get("fresh")),
                    "profile": a.get("profile") or "public", "caller": "agent"}
            if kind != "raw":
                body["extract"] = kind
            res = guarded(lambda: self.fetch("hub", True, body))
            limit = max(500, min(int(a.get("max_chars") or 20000), 200_000))
            out = {k: res.get(k) for k in ("ok", "url", "final_url", "status", "tier", "from_cache", "stale", "blocked", "block_reason",
                                           "error", "error_kind", "note", "etag", "last_modified", "blocked_until_ts") if k in res}
            ex = res.get("extract")
            if isinstance(ex, dict):
                if kind in ("readable", "markdown"):
                    text = ex.get("text") if kind == "readable" else ex.get("markdown")
                    out["title"] = ex.get("title", "")
                    out["content"] = (text or "")[:limit]
                    out["content_truncated"] = len(text or "") > limit or bool(ex.get("text_truncated"))
                    if kind == "markdown":
                        out["links"] = (ex.get("links") or [])[:50]
                else:
                    out["data"] = {k: v for k, v in ex.items() if k != "kind"}
            elif res.get("ok"):
                text = res.get("text") or ""
                out["content"] = text[:limit]
                out["content_truncated"] = len(text) > limit or bool(res.get("text_truncated"))
            return out

        def web_search(a: dict[str, Any]) -> dict[str, Any]:
            body = {k: a[k] for k in ("query", "limit", "freshness_days", "engines", "news") if a.get(k) not in (None, "")}
            body["caller"] = "agent"
            return guarded(lambda: self.search("hub", True, body))

        def web_preview(a: dict[str, Any]) -> dict[str, Any]:
            return guarded(lambda: self.preview("hub", True, {"url": a.get("url"), "fresh": bool(a.get("fresh")), "caller": "agent"}))

        def web_hosts(a: dict[str, Any]) -> dict[str, Any]:
            if not self.settings()["enabled"]:
                return {"ok": False, "error": "the web service is turned off (hub.json: web.enabled = false)"}
            if a.get("clear_block"):
                res = self.clear_host(str(a["clear_block"]))
                res.pop("status", None)
                return res
            return self.hosts()

        return {"hub_web_fetch": web_fetch, "hub_web_search": web_search, "hub_web_preview": web_preview, "hub_web_hosts": web_hosts}


__all__ = ["WebFacet", "WebDb", "HostState", "RobotsStore", "web_settings", "DEFAULT_SETTINGS"]
