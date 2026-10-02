"""Chat sources (facet ``chats``): Slack, Microsoft Teams / Outlook (Graph) and JSON-lines files, in the same place as the mail.

Work may run on Slack or on Microsoft 365; the hub reads both with the standard library only and stores what it reads in the
SAME ``mail.db`` ``messages`` table the mail gateway uses (``kind = 'chat'``; Outlook inbox messages are stored as
``kind = 'mail'`` through the gateway's own ``ingest``, so they get a priority, interests and ``mail.received`` like IMAP mail).

``<data>/chats.json`` is a list of sources ``{id, kind, name, sphere, enabled, interval_min, ...}``:

* ``slack``  ``{token, channels, include_dms, me, api_base, backfill_days}``: ``conversations.list`` / ``conversations.history``
  (with an ``oldest`` watermark per channel), ``users.info`` (cached), ``auth.test``. ``channels`` empty = every conversation
  the token is a member of (plus DMs and group DMs when ``include_dms``).
* ``graph``  ``{access_token | tenant + client_id (device-code login), teams_chats, outlook, api_base, login_base, scope}``.
* ``jsonl``  ``{path}``: one JSON message per line ``{ts, channel, from, text, direct, mentions_me}``, read incrementally.

Secrets (``token``, ``access_token``, ``refresh_token``, ``client_secret``) are never returned unmasked (``••••`` + last 4).
Per-source state (watermarks, last fetch, login) lives in ``<data>/chats_state.json``.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .facets import Facet, Request
from .mailgate import MailStore, allowed_spheres, fold

logger = logging.getLogger("hoard_hub.chats")

KINDS = ("slack", "graph", "jsonl")
MASK = "••••"
SECRET_KEYS = {"slack": ("token",), "graph": ("access_token", "refresh_token", "client_secret"), "jsonl": ()}
SLACK_SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name", "channel_archive",
                       "channel_unarchive", "group_join", "group_leave", "group_topic", "group_purpose", "group_name", "pinned_item",
                       "unpinned_item", "bot_add", "bot_remove", "message_deleted"}
GRAPH_SCOPE = "offline_access Chat.Read Mail.Read User.Read"
_ID_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class ChatError(Exception):
    pass


class RateLimited(ChatError):
    def __init__(self, retry_after: float = 60.0):
        super().__init__(f"rate limited, retry in {int(retry_after)} s")
        self.retry_after = retry_after


def mask(value: Any) -> str:
    s = str(value or "")
    if not s:
        return ""
    return MASK + (s[-4:] if len(s) > 8 else "")


def _is_masked(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MASK)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")[:40]
    return s or "source"


def parse_ts(value: Any) -> float:
    """Epoch seconds from a number, a numeric string (Slack ``1700000000.0002``) or an ISO 8601 string (Graph, with ``Z``)."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00").replace("z", "+00:00"))
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slack_plain(text: str, users: dict[str, str]) -> str:
    """Slack mrkdwn → plain text: ``<@U1>`` → ``@name``, ``<#C1|general>`` → ``#general``, ``<http://x|label>`` → ``label (http://x)``."""
    t = str(text or "")
    t = re.sub(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>", lambda m: "@" + (m.group(2) or users.get(m.group(1), m.group(1))), t)
    t = re.sub(r"<#([CGD][A-Z0-9]+)(?:\|([^>]*))?>", lambda m: "#" + (m.group(2) or m.group(1)), t)
    t = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", t)
    t = re.sub(r"<(https?://[^>|]+)\|([^>]+)>", r"\2 (\1)", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    t = re.sub(r"<mailto:([^>|]+)(?:\|[^>]*)?>", r"\1", t)
    return _html.unescape(t).strip()


def _bad(status: int, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": error, **extra}


def _loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def http_request(url: str, *, method: str = "GET", headers: Optional[dict[str, str]] = None, form: Optional[dict[str, Any]] = None,
                 timeout: float = 25.0) -> tuple[int, Any, dict[str, str]]:
    """``(status, json, response headers)``; network errors raise :class:`ChatError`. Loopback never goes through a proxy."""
    data = urllib.parse.urlencode({k: v for k, v in (form or {}).items() if v is not None}).encode("utf-8") if form is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": "hoard-hub-chats", **(headers or {})}
    if data is not None:
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if _loopback(url) else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status, rh = resp.read(), resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status, rh = exc.code, dict(exc.headers.items()) if exc.headers else {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ChatError(f"{type(exc).__name__}: {str(getattr(exc, 'reason', exc))[:120]}") from exc
    try:
        body = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        body = {}
    return status, body, {k.lower(): v for k, v in rh.items()}


def _retry_after(headers: dict[str, str], default: float = 60.0) -> float:
    try:
        return max(1.0, min(3600.0, float(headers.get("retry-after", default))))
    except (TypeError, ValueError):
        return default


class ChatSources(Facet):
    id = "chats"
    ui_scripts = ("chats.js",)

    def __init__(self, hub: Any, *, clock: Callable[[], float] = time.time):
        super().__init__(hub)
        self.clock = clock
        self.data_dir = hub.config.data_dir
        self.path = os.path.join(self.data_dir, "chats.json")
        self.state_path = os.path.join(self.data_dir, "chats_state.json")
        self.background = True            # tests switch the fetch thread off
        self.tick_s = 30.0
        self.first_delay_s = 15.0
        self.login_poll_s: Optional[float] = None        # tests shorten the device-code polling
        self._own_store: Optional[MailStore] = None
        self._lock = threading.RLock()
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._login_threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ files
    def _read_sources(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("sources") or []
        return [s for s in raw if isinstance(s, dict) and s.get("id") and s.get("kind") in KINDS] if isinstance(raw, list) else []

    def _write_json(self, path: str, value: Any) -> None:
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def _write_sources(self, sources: list[dict[str, Any]]) -> None:
        with self._lock:
            self._write_json(self.path, sources)

    def _state(self) -> dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._write_json(self.state_path, state)

    def _update_state(self, source_id: str, **kw: Any) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            st = state.setdefault(source_id, {})
            st.update(kw)
            self._save_state(state)
            return st

    # ------------------------------------------------------------------ store / spheres
    @property
    def store(self) -> MailStore:
        gate = self.hub.facet("mailgate") if hasattr(self.hub, "facet") else None
        if gate is not None and hasattr(gate, "store"):
            return gate.store
        if self._own_store is None:
            self._own_store = MailStore(os.path.join(self.data_dir, "mail.db"), clock=self.clock)
        return self._own_store

    def _spheres(self) -> Any:
        return self.hub.facet("spheres") if hasattr(self.hub, "facet") else None

    def sphere_of(self, src: dict[str, Any]) -> str:
        explicit = str(src.get("sphere") or "").strip()
        if explicit:
            return explicit
        fn = getattr(self._spheres(), "sphere_of_chat_source", None)
        if fn is not None:
            try:
                return str(fn(src["id"]) or "personal")
            except Exception:  # noqa: BLE001
                logger.exception("sphere_of_chat_source failed")
        return "personal"

    def _classify(self, sphere: str, **kw: Any) -> dict[str, Any]:
        fn = getattr(self._spheres(), "classify", None)
        if fn is None:
            return {"priority": "normal", "reasons": []}
        try:
            res = fn(sphere, **kw) or {}
            pr = str(res.get("priority") or "normal")
            return {"priority": pr if pr in ("attention", "normal", "low") else "normal", "reasons": [str(r) for r in res.get("reasons") or []][:8]}
        except Exception:  # noqa: BLE001
            logger.exception("classify failed")
            return {"priority": "normal", "reasons": []}

    # ------------------------------------------------------------------ sources: validation and views
    def _normalize(self, raw: dict[str, Any], old: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        base = dict(old or {})
        kind = str(raw.get("kind") or base.get("kind") or "").lower()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if old and old.get("kind") != kind:
            raise ValueError("a source's kind cannot change")
        name = str(raw.get("name") if raw.get("name") is not None else base.get("name") or "").strip()[:80]
        sid = str(raw.get("id") or base.get("id") or slug(name or kind)).strip().lower()
        if not _ID_OK.match(sid):
            raise ValueError("id must be lower-case letters, digits, - or _ (max 40)")
        out: dict[str, Any] = {"id": sid, "kind": kind, "name": name or sid,
                               "sphere": str(raw.get("sphere") if "sphere" in raw else base.get("sphere", "")).strip()[:60],
                               "enabled": bool(raw.get("enabled", base.get("enabled", True)))}
        try:
            out["interval_min"] = max(1, min(1440, int(raw.get("interval_min", base.get("interval_min", 5)))))
        except (TypeError, ValueError):
            raise ValueError("interval_min must be a number") from None

        def pick(key: str, default: Any = "") -> Any:
            return raw[key] if key in raw else base.get(key, default)

        def secret(key: str) -> str:
            v = raw.get(key) if key in raw else None
            if v is None or _is_masked(v):
                return str(base.get(key) or "")
            return str(v).strip()

        def url(key: str, default: str) -> str:
            v = str(pick(key, "") or "").strip().rstrip("/")
            if not v:
                return default
            if not v.lower().startswith(("http://", "https://")):
                raise ValueError(f"{key} must be an http(s) URL")
            return v

        def clean_days(key: str, default: int) -> int:
            try:
                return max(0, min(60, int(pick(key, default))))
            except (TypeError, ValueError):
                return default

        if kind == "slack":
            chans = pick("channels", [])
            if isinstance(chans, str):
                chans = re.split(r"[,\n;]", chans)
            out.update({"token": secret("token"), "channels": [str(c).strip() for c in chans if str(c).strip()][:200],
                        "include_dms": bool(pick("include_dms", False)), "me": str(pick("me", "") or "").strip()[:40],
                        "api_base": url("api_base", "https://slack.com/api"), "backfill_days": clean_days("backfill_days", 2)})
        elif kind == "graph":
            out.update({"access_token": secret("access_token"), "refresh_token": secret("refresh_token"), "client_secret": secret("client_secret"),
                        "client_id": str(pick("client_id", "") or "").strip()[:80], "tenant": str(pick("tenant", "") or "").strip()[:80],
                        "scope": str(pick("scope", "") or "").strip()[:300], "teams_chats": bool(pick("teams_chats", True)),
                        "outlook": bool(pick("outlook", False)), "api_base": url("api_base", "https://graph.microsoft.com/v1.0"),
                        "login_base": url("login_base", "https://login.microsoftonline.com"), "backfill_days": clean_days("backfill_days", 2),
                        "expires_at": float(pick("expires_at", 0) or 0)})
            if "access_token" in raw and raw["access_token"] and not _is_masked(raw["access_token"]) and "expires_at" not in raw:
                out["expires_at"] = 0.0                     # a pasted token: its lifetime is unknown, never pre-emptively refreshed
        else:
            path = str(pick("path", "") or "").strip()
            if not path:
                raise ValueError("path is required")
            out["path"] = path
        return out

    def public(self, src: dict[str, Any], state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        out = dict(src)
        for key in SECRET_KEYS.get(src.get("kind", ""), ()):
            if key in out:
                out[key] = mask(out[key])
        st = (state if state is not None else self._state()).get(src["id"], {})
        now = self.clock()
        last = float(st.get("last_fetch_ts") or 0)
        out["state"] = {"last_fetch_ts": st.get("last_fetch_ts"), "last_ok": st.get("last_ok"), "last_error": st.get("last_error", ""),
                        "last_new": st.get("last_new", 0), "login": st.get("login"), "me": st.get("me_name") or src.get("me") or "",
                        "retry_after_ts": st.get("retry_after_ts"),
                        "due_in_s": max(0, int(last + src.get("interval_min", 5) * 60 - now)) if last and src.get("enabled") else (0 if src.get("enabled") else None)}
        return out

    def sources(self) -> list[dict[str, Any]]:
        state = self._state()
        return [self.public(s, state) for s in self._read_sources()]

    def get_source(self, source_id: str) -> Optional[dict[str, Any]]:
        for s in self._read_sources():
            if s["id"] == source_id:
                return s
        return None

    def save_source(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            sources = self._read_sources()
            sid = str(raw.get("id") or "").strip().lower()
            old = next((s for s in sources if s["id"] == sid), None) if sid else None
            src = self._normalize(raw, old)
            if old is None and any(s["id"] == src["id"] for s in sources):
                # a name-derived id that collides: number it
                base, n = src["id"], 2
                while any(s["id"] == f"{base}-{n}" for s in sources):
                    n += 1
                src["id"] = f"{base}-{n}"
            sources = [src if s["id"] == src["id"] else s for s in sources] if old else sources + [src]
            self._write_sources(sources)
        self._ensure_thread()
        return self.public(src)

    def remove_source(self, source_id: str) -> bool:
        with self._lock:
            sources = self._read_sources()
            kept = [s for s in sources if s["id"] != source_id]
            if len(kept) == len(sources):
                return False
            self._write_sources(kept)
            state = self._state()
            if state.pop(source_id, None) is not None:
                self._save_state(state)
        return True

    # ------------------------------------------------------------------ ingest
    def _emit_chat(self, mail_id: int, sphere: str, source: str, channel: str, sender: str, text: str, priority: str) -> None:
        try:
            self.hub.events.emit("chat.received", {"mail_id": mail_id, "sphere": sphere, "source": source, "channel": channel[:80],
                                                   "from": sender[:80], "text": re.sub(r"\s+", " ", text)[:120], "priority": priority}, source="hub")
        except Exception:  # noqa: BLE001
            logger.exception("chat.received emit failed")

    def _add_chat(self, src: dict[str, Any], *, channel_id: str, channel_label: str, sender_id: str, sender_name: str, text: str,
                  ts: float, message_id: str, thread: str = "", mentions_me: bool = False, direct: bool = False) -> bool:
        sphere = self.sphere_of(src)
        cls = self._classify(sphere, sender=sender_name or sender_id, subject=channel_label, text=text, mentions_me=mentions_me, direct=direct)
        res = self.store.insert_message(kind="chat", source=src["id"], sphere=sphere, folder=channel_id, message_id=message_id, thread=thread,
                                        date_ts=ts, from_addr=sender_id, from_name=sender_name, to=[], subject=channel_label, text=text,
                                        priority=cls["priority"], reasons=cls["reasons"])
        if res["created"]:
            self._emit_chat(res["id"], sphere, src["id"], channel_label, sender_name or sender_id, text, cls["priority"])
        return bool(res["created"])

    # ------------------------------------------------------------------ Slack
    def _slack_call(self, src: dict[str, Any], method: str, **params: Any) -> dict[str, Any]:
        token = src.get("token") or ""
        if not token:
            raise ChatError("no Slack token")
        status, data, headers = http_request(f"{src.get('api_base') or 'https://slack.com/api'}/{method}", method="POST",
                                             headers={"Authorization": f"Bearer {token}"}, form=params)
        if status == 429 or (isinstance(data, dict) and data.get("error") == "ratelimited"):
            raise RateLimited(_retry_after(headers))
        if not isinstance(data, dict) or not data.get("ok"):
            raise ChatError(str((data or {}).get("error") or f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}")
        return data

    def _slack_user(self, src: dict[str, Any], st: dict[str, Any], uid: str, budget: list[int]) -> str:
        users = st.setdefault("users", {})
        if uid in users:
            return users[uid]
        if budget[0] <= 0:
            return uid
        budget[0] -= 1
        try:
            u = self._slack_call(src, "users.info", user=uid).get("user") or {}
            prof = u.get("profile") or {}
            name = prof.get("display_name") or u.get("real_name") or prof.get("real_name") or u.get("name") or uid
        except RateLimited:
            raise
        except ChatError:
            name = uid
        users[uid] = str(name)
        return users[uid]

    def _slack_conversations(self, src: dict[str, Any]) -> list[dict[str, Any]]:
        types = "public_channel,private_channel" + (",im,mpim" if src.get("include_dms") else "")
        found: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(10):
            data = self._slack_call(src, "conversations.list", types=types, exclude_archived="true", limit=200, cursor=cursor or None)
            found += data.get("channels") or []
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
        wanted = [str(c).strip().lstrip("#") for c in src.get("channels") or [] if str(c).strip()]
        out = []
        for ch in found:
            is_dm = bool(ch.get("is_im") or ch.get("is_mpim"))
            if wanted:
                if ch.get("id") in wanted or fold(ch.get("name") or "") in [fold(w) for w in wanted]:
                    out.append(ch)
            elif is_dm or ch.get("is_member"):
                out.append(ch)
        # configured ids that conversations.list did not show (a DM id, a channel the token cannot list)
        have = {c.get("id") for c in out}
        for w in wanted:
            if re.fullmatch(r"[CGD][A-Z0-9]{6,}", w) and w not in have:
                out.append({"id": w, "name": w})
        return out

    def _fetch_slack(self, src: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        if not src.get("me"):
            auth = self._slack_call(src, "auth.test")
            src["me"] = str(auth.get("user_id") or "")
            st["me_name"] = str(auth.get("user") or "")
            with self._lock:
                sources = [dict(s, me=src["me"]) if s["id"] == src["id"] else s for s in self._read_sources()]
                self._write_sources(sources)
        me = src.get("me") or ""
        marks: dict[str, str] = st.setdefault("marks", {})
        users = st.setdefault("users", {})
        if me and st.get("me_name"):
            users.setdefault(me, st["me_name"])
        budget = [60]
        new = seen = 0
        errors: list[str] = []
        convs = self._slack_conversations(src)
        for ch in convs:
            cid = str(ch.get("id") or "")
            is_im, is_mpim = bool(ch.get("is_im")), bool(ch.get("is_mpim"))
            oldest = marks.get(cid) or f"{now - int(src.get('backfill_days', 2)) * 86400:.6f}"
            collected: list[dict[str, Any]] = []
            cursor = ""
            try:
                for _ in range(5):
                    data = self._slack_call(src, "conversations.history", channel=cid, oldest=oldest, limit=200, cursor=cursor or None)
                    collected += data.get("messages") or []
                    cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
                    if not (data.get("has_more") and cursor):
                        break
                if is_im:
                    label = "DM · " + self._slack_user(src, st, str(ch.get("user") or ""), budget) if ch.get("user") else "DM"
                elif is_mpim:
                    label = str(ch.get("name") or "group DM")
                else:
                    label = "#" + str(ch.get("name") or cid)
                top = float(oldest)
                for m in sorted(collected, key=lambda x: float(x.get("ts") or 0)):
                    ts = float(m.get("ts") or 0)
                    top = max(top, ts)
                    if m.get("subtype") in SLACK_SKIP_SUBTYPES or m.get("hidden"):
                        continue
                    uid = str(m.get("user") or m.get("bot_id") or "")
                    if uid and uid == me:
                        continue
                    seen += 1
                    sender = self._slack_user(src, st, uid, budget) if m.get("user") else str(m.get("username") or uid or "bot")
                    raw_text = str(m.get("text") or "")
                    for mention in set(re.findall(r"<@([UW][A-Z0-9]+)", raw_text)):
                        self._slack_user(src, st, mention, budget)
                    text = slack_plain(raw_text, users) or ("[file]" if m.get("files") else "")
                    if not text:
                        continue
                    if self._add_chat(src, channel_id=cid, channel_label=label, sender_id=uid, sender_name=sender, text=text, ts=ts,
                                      message_id=f"slack:{src['id']}:{cid}:{m.get('ts')}", thread=str(m.get("thread_ts") or ""),
                                      mentions_me=bool(me and f"<@{me}" in raw_text), direct=is_im or is_mpim):
                        new += 1
                marks[cid] = f"{top:.6f}"
            except RateLimited:
                raise
            except ChatError as exc:
                if str(exc) in ("not_in_channel", "channel_not_found", "missing_scope", "is_archived"):
                    errors.append(f"{cid}: {exc}")
                    continue
                raise
        return {"new": new, "seen": seen, "channels": len(convs), "errors": errors}

    # ------------------------------------------------------------------ Microsoft Graph
    def _graph_token_refresh(self, src: dict[str, Any]) -> bool:
        if not (src.get("refresh_token") and src.get("client_id")):
            return False
        tenant = src.get("tenant") or "common"
        status, data, _ = http_request(f"{src.get('login_base') or 'https://login.microsoftonline.com'}/{tenant}/oauth2/v2.0/token", method="POST",
                                       form={"client_id": src["client_id"], "grant_type": "refresh_token", "refresh_token": src["refresh_token"],
                                             "scope": src.get("scope") or GRAPH_SCOPE, "client_secret": src.get("client_secret") or None})
        if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            return False
        self._store_tokens(src["id"], data)
        fresh = self.get_source(src["id"]) or {}
        src["access_token"], src["refresh_token"], src["expires_at"] = fresh.get("access_token"), fresh.get("refresh_token"), fresh.get("expires_at")
        return True

    def _store_tokens(self, source_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            sources = self._read_sources()
            for s in sources:
                if s["id"] == source_id:
                    s["access_token"] = str(data.get("access_token") or "")
                    if data.get("refresh_token"):
                        s["refresh_token"] = str(data["refresh_token"])
                    s["expires_at"] = self.clock() + float(data.get("expires_in") or 3600)
            self._write_sources(sources)

    def _graph_get(self, src: dict[str, Any], st: dict[str, Any], path_or_url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if src.get("expires_at") and src["expires_at"] - self.clock() < 120:
            self._graph_token_refresh(src)
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = src.get("api_base", "https://graph.microsoft.com/v1.0").rstrip("/") + path_or_url
            if params:
                url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="$,/:")
        for attempt in (1, 2):
            if not src.get("access_token"):
                raise ChatError("login needed (no access token)")
            status, data, headers = http_request(url, headers={"Authorization": f"Bearer {src['access_token']}"})
            if status == 401 and attempt == 1 and self._graph_token_refresh(src):
                continue
            if status == 401:
                st["needs_login"] = True
                raise ChatError("login needed (token rejected)")
            if status == 429:
                raise RateLimited(_retry_after(headers))
            if status >= 400:
                err = (data.get("error") or {}) if isinstance(data, dict) else {}
                raise ChatError(str(err.get("code") or err.get("message") or f"HTTP {status}")[:160] if isinstance(err, dict) else f"HTTP {status}")
            return data if isinstance(data, dict) else {}
        raise ChatError("login needed")

    def _graph_pages(self, src: dict[str, Any], st: dict[str, Any], path: str, params: dict[str, Any], pages: int = 3) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        data = self._graph_get(src, st, path, params)
        for _ in range(pages):
            out += data.get("value") or []
            nxt = data.get("@odata.nextLink")
            if not nxt:
                break
            data = self._graph_get(src, st, nxt)
        return out

    def _graph_me(self, src: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        me = self._graph_get(src, st, "/me")
        st["me_id"] = str(me.get("id") or "")
        st["me_name"] = str(me.get("displayName") or me.get("userPrincipalName") or "")
        st["me_mail"] = str(me.get("mail") or me.get("userPrincipalName") or "")
        return me

    @staticmethod
    def _graph_text(body: dict[str, Any], *, chat: bool = False) -> tuple[str, list[dict[str, str]]]:
        """Plain text and links of a Graph message body. Chat messages drop inline tags without a space (so ``<b>hoy</b>?`` stays ``hoy?``)."""
        from . import mail_helper

        content = str((body or {}).get("content") or "")
        if str((body or {}).get("contentType") or "").lower() != "html":
            return mail_helper._Text.tidy(content), []
        if not chat:
            return mail_helper._Text.from_html(content)
        t = mail_helper._Text
        raw = re.sub(r"<!--.*?-->", " ", t.DROP.sub(" ", content), flags=re.S)
        return t.tidy(_html.unescape(t.TAG.sub("", t.BLOCK.sub("\n", raw)))), []

    def _fetch_graph(self, src: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        marks: dict[str, float] = st.setdefault("marks", {})
        if not st.get("me_id"):
            self._graph_me(src, st)
        me_id = st.get("me_id") or ""
        default_mark = now - int(src.get("backfill_days", 2)) * 86400
        new = seen = 0
        errors: list[str] = []
        if src.get("teams_chats", True):
            chats = self._graph_pages(src, st, "/me/chats", {"$top": 50}, pages=2)
            for chat in chats:
                cid = str(chat.get("id") or "")
                mark = float(marks.get(cid) or default_mark)
                preview = parse_ts(((chat.get("lastMessagePreview") or {}).get("createdDateTime")))
                if preview and preview <= mark:
                    continue
                kind = str(chat.get("chatType") or "")
                msgs = self._graph_pages(src, st, f"/chats/{urllib.parse.quote(cid, safe='')}/messages", {"$top": 50, "$orderby": "createdDateTime desc"}, pages=2)
                top = mark
                topic = str(chat.get("topic") or "")
                fresh = []
                for m in msgs:
                    created = parse_ts(m.get("createdDateTime"))
                    if created <= mark:
                        continue
                    fresh.append((created, m))
                label = topic
                for created, m in sorted(fresh, key=lambda x: x[0]):
                    top = max(top, created)
                    sender = ((m.get("from") or {}).get("user") or {})
                    sid, sname = str(sender.get("id") or ""), str(sender.get("displayName") or "")
                    if sid and sid == me_id:
                        continue
                    if m.get("messageType") not in (None, "message") or m.get("deletedDateTime"):
                        continue
                    if not label and kind == "oneOnOne" and sname:
                        label = sname
                    text, _ = self._graph_text(m.get("body") or {}, chat=True)
                    if not text:
                        continue
                    seen += 1
                    mentions = any((((x.get("mentioned") or {}).get("user") or {}).get("id") == me_id) for x in (m.get("mentions") or []))
                    if self._add_chat(src, channel_id=cid, channel_label=label or "Teams chat", sender_id=sid, sender_name=sname or sid, text=text,
                                      ts=created, message_id=f"graph:{src['id']}:{cid}:{m.get('id')}", thread=str(m.get("replyToId") or ""),
                                      mentions_me=mentions, direct=kind == "oneOnOne"):
                        new += 1
                marks[cid] = top
        if src.get("outlook"):
            n, s = self._fetch_outlook(src, st, marks, default_mark)
            new, seen = new + n, seen + s
        return {"new": new, "seen": seen, "errors": errors}

    def _fetch_outlook(self, src: dict[str, Any], st: dict[str, Any], marks: dict[str, Any], default_mark: float) -> tuple[int, int]:
        mark = float(marks.get("outlook") or default_mark)
        select = "id,internetMessageId,subject,from,toRecipients,ccRecipients,receivedDateTime,body,bodyPreview,conversationId,hasAttachments"
        rows = self._graph_pages(src, st, "/me/mailFolders/inbox/messages",
                                 {"$top": 50, "$orderby": "receivedDateTime desc", "$filter": f"receivedDateTime gt {iso_utc(mark)}", "$select": select}, pages=4)
        gate = self.hub.facet("mailgate") if hasattr(self.hub, "facet") else None
        sphere = self.sphere_of(src)
        new = seen = 0
        top = mark
        me_mail = (st.get("me_mail") or "").lower()
        for m in sorted(rows, key=lambda r: parse_ts(r.get("receivedDateTime"))):
            when = parse_ts(m.get("receivedDateTime"))
            top = max(top, when)
            frm = ((m.get("from") or {}).get("emailAddress") or {})
            address, name = str(frm.get("address") or "").lower(), str(frm.get("name") or "")
            text, links = self._graph_text(m.get("body") or {})
            attachments: list[dict[str, Any]] = []
            if m.get("hasAttachments"):
                try:
                    for a in self._graph_pages(src, st, f"/me/messages/{urllib.parse.quote(str(m.get('id')), safe='')}/attachments",
                                               {"$select": "name,contentType,size"}, pages=1)[:10]:
                        attachments.append({"name": str(a.get("name") or ""), "mime": str(a.get("contentType") or ""), "size": int(a.get("size") or 0),
                                            "sha": "", "path": ""})
                except ChatError:
                    attachments.append({"name": "(attachment)", "mime": "", "size": 0, "sha": "", "path": ""})
            rec = {"message_id": str(m.get("internetMessageId") or f"graph:{src['id']}:{m.get('id')}"), "subject": str(m.get("subject") or ""),
                   "from_name": name, "from_address": address, "date_ts": when, "ts": when,
                   "to": [str((r.get("emailAddress") or {}).get("address") or "").lower() for r in m.get("toRecipients") or []],
                   "cc": [str((r.get("emailAddress") or {}).get("address") or "").lower() for r in m.get("ccRecipients") or []],
                   "in_reply_to": "", "references": [str(m.get("conversationId") or "")] if m.get("conversationId") else [],
                   "text": text, "links": links, "attachments": attachments, "account": src["id"], "account_address": me_mail,
                   "folder": "outlook:inbox", "uid": 0, "from_self": bool(me_mail and address == me_mail)}
            seen += 1
            if gate is not None and hasattr(gate, "ingest"):
                res = gate.ingest(rec, sphere=sphere, source=src["id"])
                new += 1 if res["created"] else 0
            else:
                r = self.store.insert_message(kind="mail", source=src["id"], sphere=sphere, folder=rec["folder"], message_id=rec["message_id"],
                                              thread=(rec["references"] or [""])[0], date_ts=when, from_addr=address, from_name=name,
                                              to={"to": rec["to"], "cc": rec["cc"]}, subject=rec["subject"], text=text, links=links,
                                              attachments=attachments)
                new += 1 if r["created"] else 0
        marks["outlook"] = top
        return new, seen

    # ---- device-code login
    def start_login(self, source_id: str) -> dict[str, Any]:
        src = self.get_source(source_id)
        if src is None:
            return _bad(404, "unknown source")
        if src["kind"] != "graph":
            return _bad(400, "device-code login is for Microsoft 365 (graph) sources")
        if not src.get("client_id"):
            return _bad(400, "client_id is required (register an app in Entra ID: public client, device-code flow)")
        tenant = src.get("tenant") or "common"
        login_base = src.get("login_base") or "https://login.microsoftonline.com"
        try:
            status, data, _ = http_request(f"{login_base}/{tenant}/oauth2/v2.0/devicecode", method="POST",
                                           form={"client_id": src["client_id"], "scope": src.get("scope") or GRAPH_SCOPE})
        except ChatError as exc:
            return _bad(502, str(exc))
        if status != 200 or not isinstance(data, dict) or not data.get("device_code"):
            err = str((data or {}).get("error_description") or (data or {}).get("error") or f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}"
            return _bad(502, err[:200])
        expires = self.clock() + float(data.get("expires_in") or 900)
        login = {"state": "pending", "user_code": data.get("user_code"), "verification_uri": data.get("verification_uri") or data.get("verification_url"),
                 "expires_ts": expires, "message": data.get("message") or ""}
        self._update_state(source_id, login=login)
        t = threading.Thread(target=self._poll_login, args=(source_id, dict(src), str(data["device_code"]), float(data.get("interval") or 5), expires),
                             name=f"hoard-hub-login-{source_id}", daemon=True)
        self._login_threads[source_id] = t
        t.start()
        return {"ok": True, "source": source_id, "user_code": login["user_code"], "verification_uri": login["verification_uri"],
                "expires_in": int(expires - self.clock()), "interval": data.get("interval")}

    def _poll_login(self, source_id: str, src: dict[str, Any], device_code: str, interval: float, expires: float) -> None:
        tenant = src.get("tenant") or "common"
        url = f"{src.get('login_base') or 'https://login.microsoftonline.com'}/{tenant}/oauth2/v2.0/token"
        wait = self.login_poll_s if self.login_poll_s is not None else interval

        def finish(state: str, message: str = "") -> None:
            st = self._state().get(source_id, {}).get("login") or {}
            st = {**st, "state": state, "message": message}
            self._update_state(source_id, login=st, **({"needs_login": False} if state == "ok" else {}))

        while self.clock() < expires and not self._stop.is_set():
            if self._stop.wait(wait):
                return
            try:
                status, data, _ = http_request(url, method="POST", form={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                                                           "client_id": src["client_id"], "device_code": device_code})
            except ChatError as exc:
                finish("error", str(exc))
                return
            if status == 200 and isinstance(data, dict) and data.get("access_token"):
                self._store_tokens(source_id, data)
                finish("ok", "signed in")
                return
            err = str((data or {}).get("error") or "") if isinstance(data, dict) else ""
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                wait += 5 if self.login_poll_s is None else self.login_poll_s
                continue
            finish({"authorization_declined": "declined", "expired_token": "expired"}.get(err, "error"), err or f"HTTP {status}")
            return
        finish("expired", "the code expired")

    # ------------------------------------------------------------------ JSON lines
    def _fetch_jsonl(self, src: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        path = src["path"]
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ChatError(f"file not readable: {type(exc).__name__}") from exc
        offset = int(st.get("offset") or 0)
        if size < offset:
            offset = 0                                     # the file was rewritten
        new = seen = 0
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return {"new": 0, "seen": 0}                   # no complete line yet
        for raw in chunk[:end + 1].split(b"\n"):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            seen += 1
            ts = parse_ts(rec.get("ts")) or self.clock()
            channel = str(rec.get("channel") or "general")
            sender = str(rec.get("from") or "")
            text = str(rec.get("text") or "")
            if not text:
                continue
            digest = hashlib.sha1(line.encode("utf-8")).hexdigest()[:20]
            if self._add_chat(src, channel_id=channel, channel_label=channel, sender_id=sender, sender_name=sender, text=text, ts=ts,
                              message_id=f"jsonl:{src['id']}:{digest}", mentions_me=bool(rec.get("mentions_me")), direct=bool(rec.get("direct"))):
                new += 1
        st["offset"] = offset + end + 1
        return {"new": new, "seen": seen}

    # ------------------------------------------------------------------ fetching
    def fetch_source(self, source_id: str, *, force: bool = False) -> dict[str, Any]:
        src = self.get_source(source_id)
        if src is None:
            return _bad(404, "unknown source")
        if not src.get("enabled") and not force:
            return {"ok": True, "source": source_id, "skipped": "disabled", "new": 0}
        lock = self._fetch_locks.setdefault(source_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"ok": False, "source": source_id, "error": "already fetching"}
        now = self.clock()
        try:
            st = dict(self._state().get(source_id, {}))
            if not force and float(st.get("retry_after_ts") or 0) > now:
                return {"ok": True, "source": source_id, "skipped": "rate-limited", "new": 0}
            try:
                fn = {"slack": self._fetch_slack, "graph": self._fetch_graph, "jsonl": self._fetch_jsonl}[src["kind"]]
                res = fn(src, st)
                st.update(last_ok=True, last_error="", last_new=res.get("new", 0), retry_after_ts=0)
                out = {"ok": True, "source": source_id, **res}
            except RateLimited as exc:
                st.update(last_ok=False, last_error=str(exc), retry_after_ts=now + exc.retry_after)
                out = {"ok": False, "source": source_id, "error": str(exc), "new": 0}
            except ChatError as exc:
                st.update(last_ok=False, last_error=str(exc)[:200])
                out = {"ok": False, "source": source_id, "error": str(exc)[:200], "new": 0}
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat source %s failed", source_id)
                st.update(last_ok=False, last_error=f"{type(exc).__name__}: {str(exc)[:160]}")
                out = {"ok": False, "source": source_id, "error": st["last_error"], "new": 0}
            st["last_fetch_ts"] = now
            with self._lock:
                state = self._state()
                prev = state.get(source_id, {})
                keep = {k: prev[k] for k in ("login",) if k in prev}
                state[source_id] = {**st, **keep}
                self._save_state(state)
            return out
        finally:
            lock.release()

    def fetch_all(self, *, force: bool = False, only_due: bool = False) -> dict[str, Any]:
        results = []
        state = self._state()
        for src in self._read_sources():
            if not src.get("enabled"):
                continue
            if only_due and float(state.get(src["id"], {}).get("last_fetch_ts") or 0) + src.get("interval_min", 5) * 60 > self.clock():
                continue
            results.append(self.fetch_source(src["id"], force=force))
        return {"ok": all(r.get("ok") for r in results), "results": results, "new": sum(int(r.get("new") or 0) for r in results)}

    def test_source(self, source_id: str) -> dict[str, Any]:
        src = self.get_source(source_id)
        if src is None:
            return _bad(404, "unknown source")
        st = dict(self._state().get(source_id, {}))
        try:
            if src["kind"] == "slack":
                auth = self._slack_call(src, "auth.test")
                if not src.get("me"):
                    self.save_source({"id": src["id"], "me": auth.get("user_id")})
                detail = f"{auth.get('user') or ''} @ {auth.get('team') or ''}".strip(" @")
            elif src["kind"] == "graph":
                me = self._graph_me(src, st)
                self._update_state(source_id, me_id=st["me_id"], me_name=st["me_name"], me_mail=st["me_mail"])
                detail = str(me.get("displayName") or me.get("userPrincipalName") or "")
            else:
                size = os.path.getsize(src["path"])
                detail = f"{size} bytes"
        except RateLimited as exc:
            return _bad(429, str(exc))
        except (ChatError, OSError) as exc:
            return _bad(502, str(exc)[:200], source=source_id)
        return {"ok": True, "source": source_id, "detail": detail}

    # ------------------------------------------------------------------ thread
    def _want_thread(self) -> bool:
        return bool(self.background and any(s.get("enabled") for s in self._read_sources()))

    def _ensure_thread(self) -> None:
        alive = self._thread is not None and self._thread.is_alive()
        if self._want_thread() and not alive:
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, name="hoard-hub-chats", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        stop = self._stop
        wait = self.first_delay_s
        while not stop.wait(wait):
            wait = self.tick_s
            if not any(s.get("enabled") for s in self._read_sources()):
                return
            try:
                self.fetch_all(only_due=True)
            except Exception:  # noqa: BLE001
                logger.exception("chat fetch failed")

    def start(self) -> None:
        self._ensure_thread()

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        if self._own_store is not None:
            self._own_store.close()
            self._own_store = None

    # ------------------------------------------------------------------ python API
    @staticmethod
    def _chat_view(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "kind": row["kind"], "source": row["source"], "sphere": row["sphere"], "channel": row["folder"],
                "channel_label": row["subject"], "from_id": row["from_addr"], "from_name": row["from_name"], "text": row["text"],
                "snippet": row["snippet"], "date_ts": row["date_ts"], "thread": row["thread"], "priority": row["priority"],
                "reasons": row["reasons"], "dismissed": row["dismissed"], "claims": row["claims"]}

    def recent(self, sphere: Optional[str] = None, source: Optional[str] = None, limit: int = 50, days: Optional[float] = None) -> list[dict[str, Any]]:
        rows = self.store.query(kind="chat", spheres=[sphere] if sphere else None, source=source, limit=limit, days=days)
        return [self._chat_view(r) for r in rows]

    def attention(self, sphere: Optional[str] = None, days: float = 7, limit: int = 50) -> list[dict[str, Any]]:
        """Chat messages that need you (priority attention, not dismissed)."""
        rows = self.store.query(kind="chat", spheres=[sphere] if sphere else None, days=days, attention=True, limit=limit)
        return [self._chat_view(r) for r in rows]

    def search(self, q: str, sphere: Optional[str] = None, days: float = 30, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.store.query(kind="chat", spheres=[sphere] if sphere else None, q=q, days=days, limit=limit)
        return [self._chat_view(r) for r in rows]

    # ------------------------------------------------------------------ HTTP
    def get(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/chats"):
            return None
        who = req.caller()
        if who is None:
            return _bad(401, "a family bearer token (or the hub page) is required")
        if p == "/api/chats/sources":
            return {"ok": True, "sources": self.sources()} if who in ("ui", "hub") else _bad(403, "ui or hub only")
        if p == "/api/chats/messages":
            allowed = allowed_spheres(self.hub, self.store, who)
            sphere = (req.q("sphere") or "").strip()
            if sphere in ("all", "*"):
                sphere = ""
            if sphere and allowed is not None and sphere not in allowed:
                return _bad(403, f"app '{who}' is not allowed in sphere '{sphere}'")
            spheres = [sphere] if sphere else (sorted(allowed) if allowed is not None else None)
            has_since = req.q("since_id") is not None
            days = req.q("days")
            rows = self.store.query(kind="chat", spheres=spheres, source=req.q("source") or None, folder=req.q("channel") or None,
                                    since_id=req.q_int("since_id") if has_since else None, q=req.q("q") or "",
                                    days=float(days) if days else None, limit=req.q_int("limit", 50),
                                    order="asc" if (req.q("order") == "asc" or (has_since and req.q("order") != "desc")) else "desc")
            views = [self._chat_view(r) for r in rows]
            return {"ok": True, "count": len(views), "last_id": max([v["id"] for v in views], default=req.q_int("since_id")), "messages": views}
        return None

    def post(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/chats"):
            return None
        who = req.caller()
        if who is None:
            return _bad(401, "a family bearer token (or the hub page) is required")
        if who not in ("ui", "hub"):
            return _bad(403, "ui or hub only")
        body = req.body if isinstance(req.body, dict) else {}
        if p == "/api/chats/sources":
            try:
                src = self.save_source(body.get("source") if isinstance(body.get("source"), dict) else body)
            except ValueError as exc:
                return _bad(400, str(exc))
            return {"ok": True, "source": src}
        if p == "/api/chats/sources/remove":
            return {"ok": True, "removed": True} if self.remove_source(str(body.get("id") or "")) else _bad(404, "unknown source")
        if p == "/api/chats/fetch":
            if body.get("id"):
                res = self.fetch_source(str(body["id"]), force=bool(body.get("force")))
                return res if res.get("ok") else {**res, "status": res.get("status", 502)}
            return self.fetch_all(force=bool(body.get("force")))
        m = re.fullmatch(r"/api/chats/sources/([^/]+)/(test|login)", p)
        if m:
            sid, action = urllib.parse.unquote(m.group(1)), m.group(2)
            return self.test_source(sid) if action == "test" else self.start_login(sid)
        return None

    # ------------------------------------------------------------------ tools
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {"name": "hub_chat_recent",
             "description": "Recent chat messages (Slack, Teams) / mensajes recientes del chat. Keywords: slack, teams, canal.",
             "inputSchema": {"type": "object", "properties": {
                 "sphere": {"type": "string", "description": "Sphere id; empty = all."}, "source": {"type": "string", "description": "Chat source id."},
                 "limit": {"type": "integer", "default": 20}}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_chat_search",
             "description": "Search stored chat messages / buscar en los chats (Slack, Teams). Keywords: mensaje, conversación.",
             "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}, "sphere": {"type": "string"},
                                                              "days": {"type": "number", "default": 30}, "limit": {"type": "integer", "default": 20}},
                             "required": ["q"], "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
        ]

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        def sph(a: dict[str, Any]) -> Optional[str]:
            s = str(a.get("sphere") or "").strip()
            return None if s in ("", "all", "*") else s

        def recent(a: dict[str, Any]) -> Any:
            rows = self.recent(sph(a), str(a.get("source") or "") or None, int(a.get("limit") or 20))
            return {"ok": True, "count": len(rows), "messages": rows}

        def search(a: dict[str, Any]) -> Any:
            rows = self.search(str(a.get("q") or ""), sph(a), float(a.get("days") or 30), int(a.get("limit") or 20))
            return {"ok": True, "count": len(rows), "messages": rows}

        return {"hub_chat_recent": recent, "hub_chat_search": search}
