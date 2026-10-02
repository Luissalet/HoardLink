"""Read and send mail through the account(s) configured in Faustus — one pass for the whole family.

The hub's mail gateway (``mailgate.py``) runs this file with Faustus's own Python, inside the Faustus folder::

    [<faustus>/venv/Scripts/python.exe | venv/bin/python, mail_helper.py, <faustus root>]   (JSON on stdin, one JSON line on stdout)

so the mail password never leaves Faustus: the hub only receives messages. The file imports nothing from
``hoard_link`` and uses the standard library only, because it runs under another interpreter. Adapted from
Kafka's Hoard helper (same author, MIT).

Requests (``action``):

    {"action": "status"}
        which accounts would be read and what sending would use; nothing is fetched
    {"action": "fetch", "since": {"<account>": {"INBOX": <last uid>}}, "validity": {"<account>": {"INBOX": <uidvalidity>}},
     "since_days": 14, "max": 300, "folders": ["INBOX"], "attachments_dir": "/abs/folder", "account": "optional"}
        incremental: only the UIDs above the watermark (the first time, the last ``since_days`` days); messages come
        oldest first (newest last). Every PDF / image attachment (<= 15 MB) is written to ``attachments_dir`` as
        ``<sha256>.<ext>``. Answer: ``{"ok", "error", "accounts": [{"account", "address", "folders": {"INBOX": {"last_uid",
        "validity", "new", "remaining"}}}], "messages": [...]}``
    {"action": "read", "message_id": "<...>", "account": "?", "folder": "?", "uid": 0}
        one message (found by Message-ID in any folder searched, or directly by account + folder + uid)
    {"action": "send", "subject": "...", "text": "...", "html": "...", "to": [...], "from_name": "..."}
        a notification mail through the account's SMTP; no ``to`` = the account's own address

``owner`` (optional) picks the Faustus user whose accounts are used; without it the only owner that has accounts is
used. Every account of that owner is read (``account`` narrows it to one).

Each fetched message is the record Kafka's helper returns (``message_id, subject, from_name, from_address, date, ts,
text, links, attachments``) plus ``account, account_id, account_address, folder, uid, to, cc, date_ts, in_reply_to,
references, from_self``.
"""

from __future__ import annotations

import contextlib
import email
import email.header
import email.utils
import hashlib
import html as _html
import io
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

HELPER_VERSION = 1
MAX_TEXT = 24_000
MAX_LINKS = 60
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_ATTACHMENTS = 10
ATTACHMENT_EXT = {"pdf", "png", "jpg", "jpeg", "webp", "heic", "tif", "tiff"}
SEND_TIMEOUT_S = 25


def _mask(address: str) -> str:
    address = str(address or "")
    if "@" not in address:
        return "***" if address else ""
    local, _, domain = address.partition("@")
    return (local[:3] + "***@" + domain) if local else "***@" + domain


# ------------------------------------------------------------------ Faustus plumbing
def _load_server(root: str):
    sys.path.insert(0, root)
    os.chdir(root)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from mcp_servers import email_server  # type: ignore
    return email_server


def _pick_owner(server, requested: str) -> str:
    if requested:
        return requested
    for key in ("ODYSSEUS_MCP_EMAIL_OWNER", "ODYSSEUS_EMAIL_OWNER"):
        if os.environ.get(key, "").strip():
            return os.environ[key].strip()
    rows = [r for r in server._read_accounts_from_db() if r.get("enabled", 1)]
    owners = sorted({str(r.get("owner") or "").strip() for r in rows} - {""})
    return owners[0] if len(owners) == 1 else ""


def _accounts(server, wanted):
    rows = [r for r in server._read_accounts_from_db() if r.get("enabled", 1)]
    try:
        rows = server._filter_accounts_for_owner(rows)
    except Exception:  # noqa: BLE001 — older Faustus builds
        pass
    if wanted:
        rows = [r for r in rows if wanted in (str(r.get("id")), str(r.get("account_name") or ""), str(r.get("imap_user") or ""))]
    return rows


def _selector(row: dict):
    for key in ("account_name", "imap_user", "id"):
        if row.get(key):
            return str(row[key])
    return None


def account_key(row: dict) -> str:
    """The name the hub keeps this account's watermark and sphere under: the account's name, else its login."""
    return str(row.get("account_name") or row.get("imap_user") or row.get("id") or "")


def account_address(row: dict) -> str:
    for key in ("from_address", "imap_user", "smtp_user"):
        if row.get(key) and "@" in str(row[key]):
            return str(row[key])
    return str(row.get("imap_user") or "")


# ------------------------------------------------------------------ message -> text
class _Text:
    BLOCK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d|/table|p|div|tr|li|h\d)\b[^>]*>", re.I)
    DROP = re.compile(r"<\s*(style|script|head|title)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
    HREF = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a\s*>""", re.I | re.S)
    TAG = re.compile(r"<[^>]+>")

    @classmethod
    def from_html(cls, raw: str):
        raw = cls.DROP.sub(" ", raw)
        raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
        links = []
        for href, label in cls.HREF.findall(raw):
            label = re.sub(r"\s+", " ", _html.unescape(cls.TAG.sub(" ", label))).strip()
            href = _html.unescape(href).strip()
            if href.lower().startswith(("http://", "https://")):
                links.append({"url": href[:1500], "label": label[:160]})
        text = cls.BLOCK.sub("\n", raw)
        text = _html.unescape(cls.TAG.sub(" ", text))
        return cls.tidy(text), links

    @staticmethod
    def tidy(text: str) -> str:
        text = text.replace("\r", "").replace(" ", " ").replace("​", "")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        out, blank = [], 0
        for line in lines:
            if not line:
                blank += 1
                if blank == 1 and out:
                    out.append("")
                continue
            blank = 0
            out.append(line)
        return "\n".join(out).strip()


def _decode(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    for charset in (part.get_content_charset(), "utf-8", "latin-1"):
        if not charset:
            continue
        try:
            return payload.decode(charset, errors="replace" if charset == "latin-1" else "strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _save_attachments(msg, directory: str, server=None) -> list:
    """Write every PDF / image attachment into ``directory`` as <sha256>.<ext> and describe it."""
    out: list = []
    if not directory:
        return out
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return out
    for part in msg.walk():
        if part.is_multipart() or len(out) >= MAX_ATTACHMENTS:
            continue
        ctype = part.get_content_type()
        raw_name = part.get_filename() or ""
        try:
            name = str(server._decode_header(raw_name)) if (server is not None and raw_name) \
                else str(email.header.make_header(email.header.decode_header(raw_name)))
        except Exception:  # noqa: BLE001
            name = raw_name
        name = os.path.basename(name.replace("\\", "/"))[:160]
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        if ctype == "application/pdf":
            ext = "pdf"
        elif ctype.startswith("image/"):
            if not name and "attachment" not in str(part.get("Content-Disposition", "")).lower():
                continue                                    # inline pictures without a name are logos
            ext = ext or ctype.split("/", 1)[1].replace("jpeg", "jpg")
        elif ctype in ("application/octet-stream", "application/force-download") and ext in ATTACHMENT_EXT:
            pass
        else:
            continue
        if ext not in ATTACHMENT_EXT:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        if not payload or len(payload) > MAX_ATTACHMENT_BYTES:
            continue
        sha = hashlib.sha256(payload).hexdigest()
        path = os.path.join(directory, f"{sha}.{ext}")
        try:
            if not os.path.exists(path):
                with open(path, "wb") as handle:
                    handle.write(payload)
        except OSError:
            continue
        out.append({"name": name or f"attachment.{ext}", "mime": ctype, "size": len(payload), "sha": sha, "path": path})
    return out


def message_to_record(msg, server=None, attachments_dir: str = "") -> dict:
    """Subject, sender, recipients, date, plain text (HTML converted), links and (when asked) the saved attachments of one message."""
    def header(name: str) -> str:
        value = msg.get(name, "") or ""
        if server is not None:
            try:
                return str(server._decode_header(value))
            except Exception:  # noqa: BLE001
                pass
        try:
            return str(email.header.make_header(email.header.decode_header(value)))
        except Exception:  # noqa: BLE001
            return str(value)

    plain, html_text, links = "", "", []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart() or "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not plain:
            plain = _Text.tidy(_decode(part))
        elif ctype == "text/html" and not html_text:
            html_text, links = _Text.from_html(_decode(part))
    text = html_text if len(html_text) > len(plain) * 0.6 or not plain else plain
    for url in re.findall(r"https?://[^\s<>\"')\]]+", plain):
        links.append({"url": url[:1500], "label": ""})
    seen, unique = set(), []
    for link in links:
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        unique.append(link)
    sender = header("From")
    name, address = email.utils.parseaddr(sender)
    date_raw = msg.get("Date", "")
    try:
        when = email.utils.parsedate_to_datetime(date_raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        ts = when.timestamp()
    except (TypeError, ValueError, IndexError):
        ts = None

    def addresses(name_: str) -> list:
        found = []
        for _, addr in email.utils.getaddresses([header(name_)]):
            addr = addr.strip().lower()
            if addr and "@" in addr and addr not in found:
                found.append(addr[:200])
        return found[:30]

    refs = re.findall(r"<[^<>\s]+>", msg.get("References", "") or "")
    return {"message_id": (msg.get("Message-ID", "") or "").strip()[:300], "subject": header("Subject")[:300],
            "from_name": name[:120], "from_address": address[:200], "date": date_raw[:80], "ts": ts, "date_ts": ts,
            "to": addresses("To"), "cc": addresses("Cc"), "in_reply_to": (msg.get("In-Reply-To", "") or "").strip()[:300],
            "references": [r[:300] for r in refs[:10]],
            "text": text[:MAX_TEXT], "links": unique[:MAX_LINKS], "attachments": _save_attachments(msg, attachments_dir, server)}


# ------------------------------------------------------------------ IMAP
def _uids(conn, criteria: list) -> list:
    try:
        status, data = conn.uid("SEARCH", None, *criteria)
    except Exception:  # noqa: BLE001
        return []
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _imap_since(days: int) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return when.strftime("%d-%b-%Y")


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def plan_uids(uids, last_uid: int, limit: int):
    """Which UIDs to fetch. ``uids`` is what the IMAP search returned (bytes / str / int, any order, maybe including
    ones at or below the watermark: ``UID N:*`` always returns the last message). Incremental (``last_uid`` > 0): the
    OLDEST ``limit`` ones above the watermark, so the watermark moves without leaving a gap and the next pass gets
    the rest. First read (``last_uid`` 0): the NEWEST ``limit``. Returns ``(selected ascending, remaining)``."""
    found = set()
    for u in uids:
        try:
            n = int(u)
        except (TypeError, ValueError):
            continue
        if n > int(last_uid or 0):
            found.add(n)
    ordered = sorted(found)
    limit = max(0, int(limit))
    chosen = ordered[:limit] if last_uid else ordered[-limit:] if limit else []
    return chosen, len(ordered) - len(chosen)


def _validity(conn):
    try:
        typ, data = conn.response("UIDVALIDITY")
        if data and data[0]:
            return int(data[0])
    except Exception:  # noqa: BLE001
        pass
    return None


def _select(conn, server, folder: str) -> bool:
    try:
        status, _ = conn.select(server._q(folder) if hasattr(server, "_q") else folder, readonly=True)
    except Exception:  # noqa: BLE001
        return False
    return status == "OK"


def _fetch_raw(conn, uid) -> bytes:
    status, data = conn.uid("FETCH", str(uid) if not isinstance(uid, (bytes, str)) else uid, "(BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return b""
    return data[0][1]


def _own_addresses(row: dict) -> set:
    return {str(row.get(k) or "").lower() for k in ("imap_user", "from_address", "smtp_user")} - {""}


def _stamp(record: dict, row: dict, folder: str, uid: int) -> dict:
    record["account"] = account_key(row)
    record["account_id"] = str(row.get("id") or "")
    record["account_address"] = account_address(row)
    record["folder"] = folder
    record["uid"] = int(uid)
    record["from_self"] = record.get("from_address", "").lower() in _own_addresses(row)
    return record


def fetch(server, request: dict) -> dict:
    since_days = max(1, min(int(request.get("since_days") or 14), 365))
    limit = max(1, min(int(request.get("max") or 300), 2000))
    since = request.get("since") if isinstance(request.get("since"), dict) else {}
    validity_req = request.get("validity") if isinstance(request.get("validity"), dict) else {}
    folders = [str(f) for f in (request.get("folders") or ["INBOX"]) if str(f).strip()][:6] or ["INBOX"]
    attachments_dir = str(request.get("attachments_dir") or "").strip()
    out, errors, infos = [], [], []
    for row in _accounts(server, request.get("account")):
        key = account_key(row)
        info = {"account": key, "address": account_address(row), "folders": {}}
        infos.append(info)
        conn = None
        try:
            conn = server._imap_connect(_selector(row))
            for folder in folders:
                if not _select(conn, server, folder):
                    continue
                validity = _validity(conn)
                last = int((since.get(key) or {}).get(folder) or 0)
                old_validity = (validity_req.get(key) or {}).get(folder)
                if last and old_validity and validity and int(old_validity) != int(validity):
                    last = 0                                    # the server renumbered the folder: start over
                found = _uids(conn, ["UID", f"{last + 1}:*"]) if last else _uids(conn, ["SINCE", _imap_since(since_days)])
                chosen, remaining = plan_uids(found, last, max(0, limit - len(out)))
                seen_max = max([n for n in (_to_int(u) for u in found) if n is not None] + [last])
                done_max = last
                for uid in chosen:
                    raw = _fetch_raw(conn, uid)
                    done_max = max(done_max, uid)
                    if not raw:
                        continue
                    out.append(_stamp(message_to_record(email.message_from_bytes(raw), server, attachments_dir), row, folder, uid))
                info["folders"][folder] = {"last_uid": seen_max if not remaining or not last else done_max, "validity": validity,
                                           "new": len(chosen), "remaining": remaining if last else 0,
                                           "skipped_old": 0 if last else remaining}
        except Exception as exc:  # noqa: BLE001 — one bad account must not hide the others
            errors.append(f"{key}: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.logout()
    out.sort(key=lambda r: (r.get("ts") or 0, r.get("uid") or 0))          # oldest first, newest last
    return {"ok": not errors or bool(out) or any(i["folders"] for i in infos), "error": "; ".join(errors), "accounts": infos,
            "messages": out}


def _folders_for_read(host: str) -> list:
    if "gmail" in host or "googlemail" in host:
        return ["INBOX", "[Gmail]/All Mail", "[Gmail]/Todos"]
    return ["INBOX"]


def read_one(server, request: dict) -> dict:
    mid = str(request.get("message_id") or "").strip()
    folder = str(request.get("folder") or "").strip()
    uid = request.get("uid")
    if not mid and not (folder and uid):
        return {"ok": False, "error": "message_id (or account + folder + uid) required"}
    for row in _accounts(server, request.get("account")):
        conn = None
        try:
            conn = server._imap_connect(_selector(row))
            if folder and uid:
                if not _select(conn, server, folder):
                    continue
                raw = _fetch_raw(conn, int(uid))
                if raw:
                    return {"ok": True, "error": "", "message": _stamp(message_to_record(email.message_from_bytes(raw), server), row, folder, int(uid))}
                continue
            for fld in _folders_for_read(str(row.get("imap_host") or "").lower()):
                if not _select(conn, server, fld):
                    continue
                uids = _uids(conn, ["HEADER", "Message-ID", f'"{mid}"'])
                if not uids:
                    continue
                raw = _fetch_raw(conn, uids[-1])
                if raw:
                    return {"ok": True, "error": "", "message": _stamp(message_to_record(email.message_from_bytes(raw), server), row, fld, int(uids[-1]))}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.logout()
    return {"ok": False, "error": "message not found"}


# ------------------------------------------------------------------ SMTP
def _connect(cfg: dict):
    host, port = cfg["smtp_host"], int(cfg.get("smtp_port") or 465)
    security = str(cfg.get("smtp_security") or "").strip().lower()
    # A stored mode that contradicts the well-known port (implicit TLS on 587, STARTTLS on 465) cannot work: trust the port.
    if port == 587 and security == "ssl":
        security = "starttls"
    elif port == 465 and security == "starttls":
        security = "ssl"
    if security not in ("ssl", "starttls", "none"):
        security = "starttls" if port == 587 else "ssl"
    context = ssl.create_default_context()
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT_S, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT_S)
        if security == "starttls":
            client.starttls(context=context)
    client.login(cfg["smtp_user"], cfg["smtp_password"])
    return client


def _addresses(value) -> list:
    if isinstance(value, str):
        value = value.split(",")
    out = []
    for item in value or []:
        item = re.sub(r"[\r\n]+", "", str(item)).strip()
        if item and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", item):
            out.append(item)
    return out[:10]


def send_info(server, request: dict) -> dict:
    _sel, cfg = server._resolve_send_config(request.get("account") or None)
    sender = str(cfg.get("from_address") or cfg.get("smtp_user") or "")
    to = _addresses(request.get("to")) or _addresses(sender)
    return {"cfg": cfg, "sender": sender, "to": to,
            "info": {"account": cfg.get("account_name") or "", "from": _mask(sender), "to": [_mask(a) for a in to],
                     "server": f"{cfg.get('smtp_host')}:{cfg.get('smtp_port')}"}}


def build_message(request: dict, sender: str, to: list) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = re.sub(r"[\r\n]+", " ", str(request.get("subject") or "Hoard Hub"))[:200]
    msg["From"] = formataddr((re.sub(r"[\r\n]+", " ", str(request.get("from_name") or "Hoard Hub"))[:80], sender))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.partition("@")[2] or None)
    msg.set_content(str(request.get("text") or request.get("subject") or ""))
    if request.get("html"):
        msg.add_alternative(str(request["html"]), subtype="html")
    return msg


def send(server, request: dict) -> dict:
    try:
        meta = send_info(server, request)
    except Exception as exc:  # noqa: BLE001 — Faustus's messages carry no secrets
        return {"ok": False, "error": str(exc)[:200] or type(exc).__name__}
    cfg, sender, to, info = meta["cfg"], meta["sender"], meta["to"], meta["info"]
    if not to:
        return {"ok": False, "error": "no recipient", **info}
    msg = build_message(request, sender, to)
    client = None
    try:
        client = _connect(cfg)
        client.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error": "authentication failed", **info}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error": type(exc).__name__, **info}
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.quit()
    return {"ok": True, "error": "", **info}


# ------------------------------------------------------------------ entry
def handle(request: dict, root: str) -> dict:
    try:
        server = _load_server(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Faustus mail module not loadable ({type(exc).__name__})"}
    owner = _pick_owner(server, str(request.get("owner") or "").strip())
    if owner:
        os.environ["ODYSSEUS_MCP_EMAIL_OWNER"] = owner
    try:
        rows = _accounts(server, request.get("account"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"accounts not readable ({type(exc).__name__})"}
    if not rows:
        return {"ok": False, "error": "Faustus has no enabled mail account" + (f" for {owner}" if owner else "")}
    info = {"helper": HELPER_VERSION,
            "accounts": [{"account": account_key(r), "id": str(r.get("id") or ""), "address": account_address(r),
                          "user": _mask(r.get("imap_user") or ""), "server": f"{r.get('imap_host')}:{r.get('imap_port')}"} for r in rows]}
    try:
        info.update(send_info(server, request)["info"])
    except Exception:  # noqa: BLE001 — reading may work even when sending is not configured
        pass
    action = request.get("action")
    if action == "send":
        return send(server, request)
    if action == "fetch":
        return fetch(server, request)
    if action == "read":
        return read_one(server, request)
    return {"ok": True, "error": "", **info}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        request = {}
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    real_stdout = sys.stdout
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        answer = handle(request if isinstance(request, dict) else {}, root)
    real_stdout.write(json.dumps(answer, ensure_ascii=False, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
