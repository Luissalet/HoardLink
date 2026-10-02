"""The app side of the hub's mail gateway: ask the hub for the mail, instead of running your own IMAP helper.

The hub reads the inbox once for the whole family (``/api/mail/*``, facet ``mailgate``). An app registers what it is
interested in, then asks for the messages that match, and says which ones it took (``claim``) so they stop showing up in
the person's "sin dueño" tray. Keep your own mail helper as the fallback for when the hub, or its gateway, is not there::

    from hoard_link import family, fam_mail

    family.configure("ledger", DATA_DIR)                     # the app's token is how the hub knows who asks
    if fam_mail.available():
        fam_mail.register_interest({"subject_terms": ["factura", "recibo"], "from_domains": ["amazon.es"], "has_attachment": True})
        page = fam_mail.messages(since_id=last_seen)         # {ok, messages, last_id}; resume from last_id
        for m in page["messages"]:
            ...                                              # m["subject"], m["text"], m["attachments"][i]["path"] …
            fam_mail.claim([m["id"]], "payment", "hoard://ledger/tx/12")
        last_seen = page["last_id"]
    else:
        ...                                                  # the app's own helper

Standard library only (runs inside apps that vendor ``hoard_link/``). Nothing here raises: when the hub cannot be reached the
answer is ``{"ok": False, "error": "hub unreachable"}``. The hub only returns mail of the spheres the app is allowed in.
"""

from __future__ import annotations

import email.utils
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from . import family as _f
from ._hubclient import fetch

CACHE_S = 30.0
_cache: dict[str, Any] = {"at": 0.0, "key": "", "ok": False}
_lock = threading.Lock()


def _get(path: str, timeout: float) -> tuple[Optional[int], Any]:
    return fetch(_f._hub() + path, method="GET", timeout=timeout, headers=_f._headers())


def _post(path: str, body: dict[str, Any], timeout: float) -> tuple[Optional[int], Any]:
    return _f._post(path, body, timeout)


def _answer(status: Optional[int], body: Any) -> dict[str, Any]:
    if status is None:
        return {"ok": False, "error": "hub unreachable"}
    if not isinstance(body, dict):
        return {"ok": False, "status": status, "error": f"HTTP {status}"}
    body.setdefault("ok", 200 <= status < 300)
    if status == 401:
        body["error"] = f"the hub refused this app's token ({_f.status().get('token_file') or 'no token file'})"
    return body


def available(timeout: float = 1.0) -> bool:
    """True when the hub is up AND its mail gateway is on and has read the inbox at least once (and recently).
    Cached for 30 seconds; when it is False, use the app's own mail helper."""
    key = _f._hub() + "|" + _f._token()
    with _lock:
        if _cache["key"] == key and time.time() - _cache["at"] < CACHE_S:
            return bool(_cache["ok"])
    status, body = _get("/api/mail/status", timeout)
    ok = False
    if status == 200 and isinstance(body, dict) and body.get("ready"):
        fresh, interval = body.get("fresh_s"), int(body.get("interval_min") or 0)
        ok = fresh is None or interval == 0 or float(fresh) <= interval * 180 + 900        # a stalled hub pass is "not available"
    with _lock:
        _cache.update(at=time.time(), key=key, ok=ok)
    return ok


def forget_availability() -> None:
    """Drop the 30-second cache (after changing the hub, or in tests)."""
    with _lock:
        _cache.update(at=0.0, key="", ok=False)


def register_interest(spec: dict[str, Any], sphere: Optional[str] = None, timeout: float = 10.0) -> dict[str, Any]:
    """Tell the hub which mail this app wants. ``spec``: ``subject_terms, from_domains, from_addresses, text_terms, regex,
    has_attachment`` — a message matches when ANY non-empty criterion matches (case-insensitive, accents folded).
    ``sphere`` limits it to one sphere. Registering again replaces the previous interest."""
    body: dict[str, Any] = {"spec": spec or {}}
    if sphere:
        body["sphere"] = sphere
    return _answer(*_post("/api/mail/interests", body, timeout))


def _kafka_shape(m: dict[str, Any]) -> dict[str, Any]:
    """The keys the Kafka-style mail helper's record has, next to the gateway's own."""
    name, addr = str(m.get("from_name") or ""), str(m.get("from_addr") or "")
    ts = m.get("date_ts") or None
    m["from"] = f"{name} <{addr}>" if name and addr else (addr or name)
    m["from_address"] = addr
    m["ts"] = ts
    m["date"] = email.utils.formatdate(ts) if ts else ""
    m["account"] = m.get("source") or ""
    m["from_self"] = "own mail" in (m.get("reasons") or [])
    m.setdefault("text", "")
    m.setdefault("links", [])
    m.setdefault("attachments", [])
    return m


def messages(since_id: int = 0, limit: int = 100, full: bool = True, interest: bool = True, timeout: float = 20.0) -> dict[str, Any]:
    """The messages the hub has stored with ``id`` > ``since_id`` (oldest first), for this app's spheres; with
    ``interest=True`` only those that match the interest it registered. ``{ok, messages, last_id}``: pass ``last_id`` as the
    next ``since_id``. Every message carries the gateway's keys (``id, source, sphere, from_addr, from_name, to, subject,
    snippet, priority, text, links, attachments[{name, mime, size, sha, path, url}]``) AND the Kafka helper's
    (``message_id, subject, from, from_address, date, ts, text, links, attachments`` with the local ``path``)."""
    q = f"since_id={int(since_id)}&limit={int(limit)}&kind=mail&interest={1 if interest else 0}" + ("&full=1" if full else "")
    res = _answer(*_get("/api/mail/messages?" + q, timeout))
    if res.get("ok"):
        res["messages"] = [_kafka_shape(m) for m in res.get("messages") or [] if isinstance(m, dict)]
        res.setdefault("last_id", int(since_id))
    else:
        res.setdefault("messages", [])
        res.setdefault("last_id", int(since_id))
    return res


def claim(ids: list[int], kind: str, ref: str, timeout: float = 10.0) -> dict[str, Any]:
    """Record "this mail is mine" (``kind`` e.g. ``payment`` / ``document`` / ``shipment``; ``ref`` the ``hoard://`` uri of what the
    app made from it). A claimed message leaves the "needs you" and "sin dueño" lists."""
    return _answer(*_post("/api/mail/claim", {"ids": [int(i) for i in ids], "kind": kind, "ref": ref}, timeout))


def copy_attachment(att: dict[str, Any], dest_dir: str, timeout: float = 30.0) -> str:
    """Copy one attachment (an element of a message's ``attachments``) into ``dest_dir`` and return the new path ('' when it cannot
    be had). Uses the local file when the hub's ``path`` is readable from here, else downloads it through the hub."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return ""
    src = str(att.get("path") or "")
    name = str(att.get("sha") or "") or os.path.splitext(os.path.basename(src))[0]
    ext = os.path.splitext(src)[1] or os.path.splitext(str(att.get("name") or ""))[1] or ".bin"
    dest = os.path.join(dest_dir, (name or "attachment") + ext.lower())
    if os.path.isfile(dest):
        return dest
    try:
        if src and os.path.isfile(src):
            shutil.copyfile(src, dest)
            return dest
        url = str(att.get("url") or "")
        if not url:
            return ""
        req = urllib.request.Request(_f._hub() + url if url.startswith("/") else url, headers=_f._headers())
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as resp, open(dest + ".part", "wb") as out:
            shutil.copyfileobj(resp, out)
        os.replace(dest + ".part", dest)
        return dest
    except (OSError, urllib.error.URLError, ValueError):
        try:
            os.remove(dest + ".part")
        except OSError:
            pass
        return ""
