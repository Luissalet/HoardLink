"""Mail commons: ``fam_mail.faustus_dir / faustus_python / FaustusHelper / MailRouter``, the superset helper
(``hoard_link/mail_helper.py``) against a fake Faustus with an IMAP simulator, and the Node twins in js/hoard-link.js
(``faustusDir``, ``spawnHelperRunner``, ``famMailRouter``, ``mailMessages`` fields).

The hub is a fake ``http.server`` that speaks the gateway's HTTP surface; the router scenarios run once in Python and once in
Node against it and must give the same answers and make the same requests."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from hoard_link import fam_mail, family
from tests.commons.test_notify_router import run_link_js
from tests.hub.conftest import free_port

ROOT = Path(__file__).resolve().parents[2]
HELPER_FILE = ROOT / "hoard_link" / "mail_helper.py"
T0 = 1_800_000_000.0
DAY = 86400.0


# ---------------------------------------------------------------------------------------------------------------------
# A fake hub: the gateway's HTTP surface
# ---------------------------------------------------------------------------------------------------------------------

def hub_rows() -> list[dict[str, Any]]:
    """Eight stored messages, oldest first; ``days`` is how old they are at T0."""
    spec = [
        (1, "Factura vieja", "billing@tienda.es", 40, []),
        (2, "Factura de octubre", "billing@tienda.es", 20, []),
        (3, "Hola", "pepe@example.com", 10, []),
        (4, "Recibo luz", "avisos@mail.energia.es", 5, []),
        (5, "Nota propia", "me@example.com", 4, ["own mail"]),
        (6, "Tu reserva", "noreply@vueling.com", 3, []),
        (7, "Factura marzo", "billing@tienda.es", 2, []),
        (8, "Promo", "news@shop.com", 1, []),
    ]
    rows = []
    for i, subject, addr, days, reasons in spec:
        rows.append({"id": i, "source": "Personal", "sphere": "personal", "from_addr": addr, "from_name": "", "to": ["me@example.com"],
                     "subject": subject, "snippet": subject, "priority": "normal", "reasons": reasons, "text": "cuerpo " + subject.lower(),
                     "links": [], "attachments": [], "date_ts": T0 - days * DAY, "message_id": f"<h{i}@x>"})
    return rows


HUB_EXTRAS = {6: {"html": '<script type="application/ld+json">{"@context":"http://schema.org"}</script>',
                  "images": [{"alt": "Logo", "src": "https://cdn.x/logo.png"}],
                  "headers": {"list_unsubscribe": "<mailto:u@x>", "one_click": True, "gmail_category": "updates", "message_id": "<h6@x>"}}}


class FakeMailHub:
    """``GET /api/mail/status``, ``GET /api/mail/messages``, ``POST /api/mail/interests``, ``POST /api/mail/claim``. ``log`` holds every
    request as ``[method, path, body]``; ``cfg`` can be changed while it runs."""

    def __init__(self, rows: list[dict] | None = None, extras: dict | None = None, **cfg: Any):
        self.rows = rows if rows is not None else hub_rows()
        self.extras = extras if extras is not None else HUB_EXTRAS
        self.cfg = {"ready": True, "fail_messages": False, "fail_page": 0, "faustus_dir": "", "fresh_s": None, "interval_min": 10}
        self.cfg.update(cfg)
        self.log: list[list[Any]] = []
        self.message_requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                outer.log.append(["GET", self.path, None])
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == "/api/mail/status":
                    c = outer.cfg
                    return self._send(200, {"ok": True, "ready": c["ready"], "interval_min": c["interval_min"], "fresh_s": c["fresh_s"],
                                            "faustus_dir": c["faustus_dir"]})
                if parts.path == "/api/mail/messages":
                    outer.message_requests += 1
                    if outer.cfg["fail_messages"] or outer.cfg["fail_page"] == outer.message_requests:
                        return self._send(503, {"ok": False, "error": "gateway busy"})
                    since, limit = int(q.get("since_id", 0)), int(q.get("limit", 100))
                    wanted = [f for f in (q.get("fields") or "").split(",") if f]
                    page = [dict(r) for r in sorted(outer.rows, key=lambda r: r["id"]) if r["id"] > since][:limit]
                    for r in page:
                        for name in wanted:
                            extra = outer.extras.get(r["id"], {})
                            if name in extra:
                                r[name] = extra[name]
                    return self._send(200, {"ok": True, "messages": page, "last_id": page[-1]["id"] if page else since})
                self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.log.append(["POST", self.path, body])
                if self.path == "/api/mail/interests":
                    return self._send(200, {"ok": True})
                if self.path == "/api/mail/claim":
                    return self._send(200, {"ok": True, "claimed": len(body.get("ids") or [])})
                self._send(404, {"ok": False, "error": "not found"})

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def calls(self) -> list[list[Any]]:
        """The log without the availability polls."""
        return [e for e in self.log if not e[1].startswith("/api/mail/status")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def isolated():
    saved = dict(family._state)
    fam_mail.forget_availability()
    fam_mail._hub_hint.update(at=0.0, key="", dir="")
    yield
    family._state.clear()
    family._state.update(saved)
    fam_mail.forget_availability()
    fam_mail._hub_hint.update(at=0.0, key="", dir="")


@pytest.fixture
def hubs(tmp_path, isolated):
    made: list[FakeMailHub] = []

    def make(**kw):
        hub = FakeMailHub(**kw)
        made.append(hub)
        family.configure("ledger", str(tmp_path / "appdata"), hub=hub.url)
        fam_mail.forget_availability()
        return hub

    yield make
    for hub in made:
        hub.close()


# ---------------------------------------------------------------------------------------------------------------------
# A fake Faustus: mcp_servers/email_server.py with an IMAP simulator
# ---------------------------------------------------------------------------------------------------------------------

FAKE_SERVER = textwrap.dedent('''
    import email.utils, re, time
    from datetime import datetime, timezone
    from email.message import EmailMessage

    NOW = time.time()
    HOST = "__HOST__"
    SENT = []

    LD = ('<html><body><img src="https://cdn.x/logo.png" alt="Vueling logo"><img src="https://t.x/p.gif" width="1" height="1">'
          '<script type="application/ld+json">{"@context":"http://schema.org","@type":"FlightReservation"}</script>'
          '<p>Vuelo BCN-MAD</p><a href="https://vueling.com/manage">Gestionar</a></body></html>')
    PLAIN = '<p>Hola <a href="https://pay.example/1">pagar</a></p><img src="https://cdn.x/h.png" alt="Cabecera tienda">'

    def _build(uid, subject, frm, days, text, html=None, extra=None):
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = subject, frm, "me@example.com"
        m["Message-ID"] = "<m%d@x>" % uid
        m["Date"] = email.utils.formatdate(NOW - days * 86400)
        for k, v in (extra or {}).items():
            m[k] = v
        m.set_content(text)
        if html:
            m.add_alternative(html, subtype="html")
        return m.as_bytes()

    MESSAGES = {
        1: _build(1, "Factura antigua", "Tienda <billing@tienda.es>", 60, "vieja"),
        2: _build(2, "Oferta -50%", "Shop <news@shop.com>", 10, "rebajas", None, {"List-Unsubscribe": "<https://shop.com/u>"}),
        3: _build(3, "Hola", "Pepe <pepe@example.com>", 5, "que tal"),
        4: _build(4, "Factura de octubre", "Tienda <billing@tienda.es>", 3, "importe 12,50", PLAIN),
        5: _build(5, "Tu reserva de vuelo", "Vueling <noreply@vueling.com>", 2, "vuelo BCN-MAD", LD,
                  {"List-Unsubscribe": "<mailto:u@vueling.com>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}),
        6: _build(6, "Nota propia", "Yo <me@example.com>", 1, "recordatorio"),
    }
    CATEGORY = {5: "promotions", 2: "promotions", 4: "updates"}
    ACCOUNTS = [{"id": 1, "account_name": "Personal", "imap_user": "me@example.com", "from_address": "me@example.com",
                 "smtp_user": "me@example.com", "imap_host": HOST, "imap_port": 993, "enabled": 1, "owner": "luis"}]

    def _read_accounts_from_db(): return list(ACCOUNTS)
    def _filter_accounts_for_owner(rows): return rows
    def _decode_header(v): return str(v)
    def _q(f): return f
    def _resolve_send_config(account): raise RuntimeError("smtp not configured")

    def _parsed(uid):
        return email.message_from_bytes(MESSAGES[uid])

    def _day(uid):
        return email.utils.parsedate_to_datetime(_parsed(uid)["Date"]).date()

    def _gm(raw, uids):
        days = re.search(r"newer_than:(\\d+)d", raw)
        out = set(uids)
        if days:
            limit = datetime.fromtimestamp(NOW - int(days.group(1)) * 86400, timezone.utc).date()
            out = {u for u in out if _day(u) >= limit}
        raw = re.sub(r"newer_than:\\d+d", "", raw)
        cat = re.search(r"category:(\\w+)", raw)
        if cat:
            return {u for u in out if CATEGORY.get(u) == cat.group(1)}
        subjects = [t.strip().strip('"').lower() for blob in re.findall(r"subject:\\(([^)]*)\\)", raw) for t in blob.split(" OR ")]
        senders = [t.strip().lower() for blob in re.findall(r"from:\\(([^)]*)\\)", raw) for t in blob.split(" OR ")]
        rest = re.sub(r"(subject|from):\\([^)]*\\)", " ", raw)
        words = [w.lower() for w in re.findall(r"[\\w.@-]+", rest) if w != "OR"]
        keep = set()
        for u in out:
            msg = _parsed(u)
            subj, frm = str(msg["Subject"]).lower(), str(msg["From"]).lower()
            body = MESSAGES[u].decode("utf-8", "replace").lower()
            if any(t in subj for t in subjects) or any(s in frm for s in senders) or any(w in subj or w in body for w in words):
                keep.add(u)
        return keep

    def _search(crit):
        res, i = set(MESSAGES), 0
        crit = [c.decode() if isinstance(c, bytes) else c for c in crit]
        while i < len(crit):
            t = crit[i]
            if t == "UID":
                lo = int(crit[i + 1].split(":")[0])
                res &= ({u for u in MESSAGES if u >= lo} or {max(MESSAGES)})      # the IMAP quirk
                i += 2
            elif t == "SINCE":
                since = datetime.strptime(crit[i + 1], "%d-%b-%Y").date()
                res = {u for u in res if _day(u) >= since}
                i += 2
            elif t in ("SUBJECT", "FROM", "TEXT"):
                needle = crit[i + 1].strip('"').lower()
                if t == "TEXT":
                    res = {u for u in res if needle in MESSAGES[u].decode("utf-8", "replace").lower()}
                else:
                    name = "Subject" if t == "SUBJECT" else "From"
                    res = {u for u in res if needle in str(_parsed(u)[name]).lower()}
                i += 2
            elif t == "HEADER":
                mid = crit[i + 2].strip('"')
                res = {u for u in res if mid.encode() in MESSAGES[u]}
                i += 3
            elif t == "X-GM-RAW":
                res = _gm(crit[i + 1].strip('"'), res)
                i += 2
            else:
                i += 1
        return sorted(res)

    class _Conn:
        def select(self, folder, readonly=True):
            ok = folder == "INBOX" or (folder == "[Gmail]/All Mail" and "gmail" in HOST)
            return ("OK", [b"6"]) if ok else ("NO", [])
        def list(self): return ("OK", [])
        def response(self, name): return (name, [b"42"])
        def logout(self): pass
        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return "OK", [" ".join(str(h) for h in _search(args[1:])).encode()]
            if cmd == "FETCH":
                spec = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
                what = args[1]
                if "HEADER.FIELDS" in what:
                    fields = re.search(r"HEADER\\.FIELDS \\(([^)]*)\\)", what).group(1).split()
                    out = []
                    for u in [int(x) for x in spec.split(",")]:
                        if u not in MESSAGES:
                            continue
                        msg = _parsed(u)
                        head = "".join("%s: %s\\r\\n" % (f, msg[f]) for f in fields if msg[f] is not None) + "\\r\\n"
                        out.append((("%d (UID %d BODY[HEADER.FIELDS (%s)] {%d}" % (u, u, " ".join(fields), len(head))).encode(), head.encode()))
                        out.append(b")")
                    return "OK", out
                raw = MESSAGES.get(int(spec))
                return ("OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]) if raw else ("NO", [])
            return "NO", []

    def _imap_connect(selector): return _Conn()
''')


def make_faustus(tmp_path: Path, host: str = "imap.example.com", name: str = "faustus") -> Path:
    root = tmp_path / name
    (root / "mcp_servers").mkdir(parents=True)
    (root / "mcp_servers" / "email_server.py").write_text(FAKE_SERVER.replace("__HOST__", host), encoding="utf-8")
    return root


@pytest.fixture
def faustus(tmp_path):
    return make_faustus(tmp_path)


@pytest.fixture
def gmail_faustus(tmp_path):
    return make_faustus(tmp_path, "imap.gmail.com", "faustus-gmail")


def helper_for(root: Path) -> fam_mail.FaustusHelper:
    return fam_mail.FaustusHelper(str(root), python=sys.executable, ask_hub=False)


def subjects(answer: dict) -> list[str]:
    return [m["subject"] for m in answer["messages"]]


# ---------------------------------------------------------------------------------------------------------------------
# faustus_dir / faustus_python
# ---------------------------------------------------------------------------------------------------------------------

def clean_env(monkeypatch, tmp_path):
    for key in (*fam_mail.FAUSTUS_ENV, *fam_mail.FAUSTUS_PYTHON_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(fam_mail, "_sibling_candidates", lambda: [])
    monkeypatch.setattr(fam_mail, "COMMON_FAUSTUS_PATHS", ())


def test_faustus_dir_first_hit_wins(tmp_path, monkeypatch, isolated):
    clean_env(monkeypatch, tmp_path)
    a, b, c = make_faustus(tmp_path, name="a"), make_faustus(tmp_path, name="b"), make_faustus(tmp_path, name="c")
    assert fam_mail.faustus_dir(None, ask_hub=False) is None
    monkeypatch.setenv("HOARD_HUB_FAUSTUS_DIR", str(c))
    assert fam_mail.faustus_dir(None, ask_hub=False) == c.resolve()
    monkeypatch.setenv("FAUSTUS_DIR", str(b))
    assert fam_mail.faustus_dir(None, ask_hub=False) == b.resolve()                 # the earlier variable wins
    assert fam_mail.faustus_dir(str(a), ask_hub=False) == a.resolve()               # the app's setting beats the environment
    assert fam_mail.faustus_dir(lambda: str(a), ask_hub=False) == a.resolve()       # a callable works too
    assert fam_mail.faustus_dir(str(tmp_path / "nowhere"), ask_hub=False) == b.resolve()   # a wrong setting falls through
    assert fam_mail.faustus_dir(lambda: 1 / 0, ask_hub=False) == b.resolve()        # and a failing one is ignored


def test_faustus_dir_needs_the_email_server_and_expands_home(tmp_path, monkeypatch, isolated):
    clean_env(monkeypatch, tmp_path)
    (tmp_path / "empty").mkdir()
    assert fam_mail.faustus_dir(str(tmp_path / "empty"), ask_hub=False) is None
    home_faustus = make_faustus(tmp_path / "home", name="faustus")
    monkeypatch.setattr(fam_mail, "COMMON_FAUSTUS_PATHS", ("~/faustus",))
    assert fam_mail.faustus_dir(None, ask_hub=False) == home_faustus.resolve()


def test_faustus_dir_siblings_extras_and_the_hubs_hint(tmp_path, monkeypatch, hubs):
    clean_env(monkeypatch, tmp_path)
    sibling, extra, hinted = make_faustus(tmp_path, name="sib"), make_faustus(tmp_path, name="ex"), make_faustus(tmp_path, name="hint")
    monkeypatch.setattr(fam_mail, "_sibling_candidates", lambda: [sibling])
    assert fam_mail.faustus_dir(None, ask_hub=False) == sibling.resolve()
    monkeypatch.setattr(fam_mail, "_sibling_candidates", lambda: [])
    assert fam_mail.faustus_dir(None, ask_hub=False, extra=[str(extra)]) == extra.resolve()
    hub = hubs(faustus_dir=str(hinted))
    assert fam_mail.faustus_dir(None, ask_hub=False) is None                         # the hub itself never asks the hub
    assert fam_mail.faustus_dir(None) == hinted
    assert fam_mail.faustus_dir(None) == hinted                                      # remembered: one request
    assert [e[1] for e in hub.log].count("/api/mail/status") == 1
    hub.cfg["faustus_dir"] = str(tmp_path / "gone")
    fam_mail._hub_hint.update(at=0.0)
    assert fam_mail.faustus_dir(None) is None                                        # a hint that is not Faustus is ignored


def test_faustus_python_prefers_the_venv(tmp_path, monkeypatch):
    clean_env(monkeypatch, tmp_path)
    root = make_faustus(tmp_path)
    assert fam_mail.faustus_python(root) is None
    own = tmp_path / "python-own"
    own.write_text("", encoding="utf-8")
    assert fam_mail.faustus_python(root, str(own)) == str(own)
    assert fam_mail.faustus_python(root, lambda: str(own)) == str(own)
    assert fam_mail.faustus_python(root, str(tmp_path / "missing")) is None
    monkeypatch.setenv("HOARD_FAUSTUS_PYTHON", str(own))
    assert fam_mail.faustus_python(root) == str(own)
    for rel in ("venv/bin/python", "venv/Scripts/python.exe"):                      # both layouts, Windows first
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    assert fam_mail.faustus_python(root, str(own)) == str(root / "venv/Scripts/python.exe")
    (root / "venv/Scripts/python.exe").unlink()
    assert fam_mail.faustus_python(root, str(own)) == str(root / "venv/bin/python")
    assert fam_mail.faustus_python(None) == str(own)


# ---------------------------------------------------------------------------------------------------------------------
# FaustusHelper
# ---------------------------------------------------------------------------------------------------------------------

class Recorder:
    """A ``subprocess.run`` stand-in that records how it was called."""

    def __init__(self, stdout='noise\n{"ok": true, "error": "", "messages": []}\n', raises=None, returncode=0):
        self.calls: list[tuple[list, dict]] = []
        self.stdout, self.raises, self.returncode = stdout, raises, returncode

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_helper_run_builds_the_command_and_keeps_secrets_out(tmp_path, monkeypatch, isolated):
    clean_env(monkeypatch, tmp_path)
    root = make_faustus(tmp_path)
    monkeypatch.setenv("KAFKA_SECRET", "s3cret")
    monkeypatch.setenv("KEEP_ME", "yes")
    rec = Recorder()
    helper = fam_mail.FaustusHelper(str(root), owner=lambda: "luis", python=sys.executable, runner=rec, env_drop_prefixes=("KAFKA_",), ask_hub=False)
    assert helper.available() and helper.faustus_dir() == root.resolve() and helper.python() == sys.executable
    out = helper.run("scan", {"subject_terms": ["x"]}, timeout=7)
    assert out == {"ok": True, "error": "", "messages": []}                          # the last JSON line, whatever came before
    (cmd, kw), = rec.calls
    assert cmd == [sys.executable, str(fam_mail.HELPER), str(root.resolve())]
    assert json.loads(kw["input"]) == {"subject_terms": ["x"], "action": "scan", "owner": "luis"}
    assert kw["timeout"] == 7 and kw["cwd"] == str(root.resolve()) and kw["encoding"] == "utf-8"
    assert "KAFKA_SECRET" not in kw["env"] and kw["env"]["KEEP_ME"] == "yes" and kw["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)         # no console window on Windows
    helper.run("status", {"owner": "ana"})
    assert json.loads(rec.calls[-1][1]["input"])["owner"] == "ana"                  # an explicit owner is not overridden


def test_helper_run_never_raises(tmp_path, monkeypatch, isolated):
    clean_env(monkeypatch, tmp_path)
    nothing = fam_mail.FaustusHelper(str(tmp_path / "nope"), ask_hub=False)
    assert not nothing.available() and "Faustus folder not found" in nothing.run("status")["error"]
    root = make_faustus(tmp_path)
    no_python = fam_mail.FaustusHelper(str(root), ask_hub=False)
    assert not no_python.available() and "no venv" in no_python.run("status")["error"]
    for raised, text in ((subprocess.TimeoutExpired("x", 1), "took too long"), (OSError("boom"), "OSError")):
        h = fam_mail.FaustusHelper(str(root), python=sys.executable, runner=Recorder(raises=raised), ask_hub=False)
        assert text in h.run("status")["error"]
    for stdout in ("", "not json\n", "{broken\n", '{"no_ok": 1}\n'):
        h = fam_mail.FaustusHelper(str(root), python=sys.executable, runner=Recorder(stdout=stdout, returncode=3), ask_hub=False)
        assert h.run("status") == {"ok": False, "error": "mail helper exit 3"}


def test_helper_status_is_remembered(tmp_path, monkeypatch, isolated):
    clean_env(monkeypatch, tmp_path)
    root = make_faustus(tmp_path)
    now = {"t": 1000.0}
    rec = Recorder(stdout='{"ok": true, "accounts": []}\n')
    h = fam_mail.FaustusHelper(str(root), python=sys.executable, runner=rec, ask_hub=False, clock=lambda: now["t"])
    first = h.status()
    assert first["ok"] and first["faustus_dir"] == str(root.resolve())
    h.status()
    assert len(rec.calls) == 1
    now["t"] += fam_mail.STATUS_TTL_S + 1
    h.status()
    assert len(rec.calls) == 2
    h.status(refresh=True)
    assert len(rec.calls) == 3
    rec.stdout = '{"ok": false, "error": "x"}\n'
    h.status(refresh=True)
    now["t"] += 31                                                                   # a failure is retried after thirty seconds
    h.status()
    assert len(rec.calls) == 5


# ---------------------------------------------------------------------------------------------------------------------
# The real helper file against the fake Faustus
# ---------------------------------------------------------------------------------------------------------------------

def test_helper_status_and_account_listing(faustus):
    out = helper_for(faustus).run("status")
    assert out["ok"] and out["helper"] == 2 and out["accounts"][0]["account"] == "Personal"
    assert "me@example.com" not in out["accounts"][0]["user"]


def test_scan_by_subject_terms_returns_records_with_images_and_headers(faustus, tmp_path):
    out = helper_for(faustus).run("scan", {"subject_terms": ["factura"], "since_days": 30, "attachments_dir": str(tmp_path / "att")})
    assert out["ok"] and subjects(out) == ["Factura de octubre"]                    # the 60-day-old one is outside the window
    rec = out["messages"][0]
    assert rec["images"] == [{"alt": "Cabecera tienda", "src": "https://cdn.x/h.png"}]
    assert "html" not in rec                                                         # no structured markup: the raw HTML is not shipped
    assert rec["headers"] == {"list_unsubscribe": "", "one_click": False, "gmail_category": "", "message_id": "<m4@x>"}
    assert rec["account"] == "Personal" and rec["folder"] == "INBOX" and rec["uid"] == 4 and rec["from_self"] is False
    assert {"url": "https://pay.example/1", "label": "pagar"} in rec["links"]
    wide = helper_for(faustus).run("scan", {"subject_terms": ["factura"], "since_days": 90})
    assert subjects(wide) == ["Factura de octubre", "Factura antigua"]               # newest first


def test_scan_by_sender_domain_carries_html_markup_and_unsubscribe_headers(faustus):
    out = helper_for(faustus).run("scan", {"sender_domains": ["vueling.com"], "since_days": 30})
    [rec] = out["messages"]
    assert rec["subject"] == "Tu reserva de vuelo"
    assert "ld+json" in rec["html"] and len(rec["html"]) <= 240_000                  # structured markup: the raw HTML is included
    assert rec["images"] == [{"alt": "Vueling logo", "src": "https://cdn.x/logo.png"}]   # the tracking pixel is dropped
    assert rec["headers"]["list_unsubscribe"] == "<mailto:u@vueling.com>" and rec["headers"]["one_click"] is True
    assert "Vuelo BCN-MAD" in rec["text"] and "ld+json" not in rec["text"]


def test_scan_html_modes_skip_own_skip_known_and_free_text(faustus):
    h = helper_for(faustus)
    assert "html" in h.run("scan", {"subject_terms": ["factura"], "html": "all"})["messages"][0]
    assert "html" not in h.run("scan", {"sender_domains": ["vueling.com"], "html": "none"})["messages"][0]
    assert subjects(h.run("scan", {"subject_terms": ["nota"], "skip_own": True})) == []
    assert subjects(h.run("scan", {"subject_terms": ["nota"]})) == ["Nota propia"]
    assert h.run("scan", {"subject_terms": ["nota"]})["messages"][0]["from_self"] is True
    both = h.run("scan", {"subject_terms": ["factura", "hola"], "since_days": 30})
    assert subjects(both) == ["Factura de octubre", "Hola"]
    known = h.run("scan", {"subject_terms": ["factura", "hola"], "since_days": 30, "skip": ["<m4@x>"]})
    assert subjects(known) == ["Hola"] and known["accounts"][0]["matches"] == 2 and known["accounts"][0]["new"] == 1
    assert subjects(h.run("scan", {"query": "rebajas", "since_days": 30})) == ["Oferta -50%"]
    assert h.run("scan", {"subject_terms": ["factura"], "max": 1, "since_days": 90})["messages"][0]["subject"] == "Factura de octubre"
    assert h.run("scan", {})["messages"] == []                                       # no criterion: nothing


def test_scan_gmail_queries_and_categories(gmail_faustus):
    h = helper_for(gmail_faustus)
    out = h.run("scan", {"subject_terms": ["reserva"], "sender_domains": ["shop.com"], "since_days": 30, "categories": True})
    assert out["accounts"][0]["folder"] == "[Gmail]/All Mail"
    by_subject = {m["subject"]: m for m in out["messages"]}
    assert set(by_subject) == {"Tu reserva de vuelo", "Oferta -50%"}
    assert by_subject["Tu reserva de vuelo"]["headers"]["gmail_category"] == "promotions"
    raw = h.run("scan", {"gmail_query": "rebajas", "since_days": 30})
    assert subjects(raw) == ["Oferta -50%"]
    assert raw["messages"][0]["headers"]["gmail_category"] == ""                     # categories are opt-in for a scan


def test_headers_action_is_one_small_record_per_message(faustus, gmail_faustus):
    out = helper_for(faustus).run("headers", {"since_days": 30})
    assert out["ok"] and len(out["messages"]) == 5                                   # the 60-day-old one is outside
    by_id = {m["message_id"]: m for m in out["messages"]}
    vueling = by_id["<m5@x>"]
    assert vueling["from_address"] == "noreply@vueling.com" and vueling["one_click"] is True and vueling["list_unsubscribe"] == "<mailto:u@vueling.com>"
    assert by_id["<m6@x>"]["from_self"] is True and by_id["<m3@x>"]["list_unsubscribe"] == ""
    assert all("subject" not in m and "text" not in m for m in out["messages"])
    assert out["accounts"][0]["in_window"] == 5
    gm = helper_for(gmail_faustus).run("headers", {"since_days": 30})
    cats = {m["message_id"]: m["category"] for m in gm["messages"]}
    assert cats["<m5@x>"] == "promotions" and cats["<m2@x>"] == "promotions" and cats["<m3@x>"] == "" and gm["accounts"][0]["gmail_categories"]


def test_fetch_carries_images_headers_and_gmail_categories(faustus, gmail_faustus):
    first = helper_for(faustus).run("fetch", {"since_days": 30, "max": 50})
    assert first["ok"] and [m["uid"] for m in first["messages"]] == [2, 3, 4, 5, 6]          # oldest first, newest last
    vueling = next(m for m in first["messages"] if m["uid"] == 5)
    assert "ld+json" in vueling["html"] and vueling["headers"]["one_click"] is True and vueling["headers"]["gmail_category"] == ""
    folder = first["accounts"][0]["folders"]["INBOX"]
    assert folder["new"] == 5 and folder["last_uid"] == 6
    after = helper_for(faustus).run("fetch", {"since": {"Personal": {"INBOX": 5}}})
    assert [m["uid"] for m in after["messages"]] == [6]                              # incremental: above the watermark
    gm = helper_for(gmail_faustus).run("fetch", {"since_days": 30})
    assert {m["uid"]: m["headers"]["gmail_category"] for m in gm["messages"]}[5] == "promotions"
    off = helper_for(gmail_faustus).run("fetch", {"since_days": 30, "categories": False})
    assert {m["headers"]["gmail_category"] for m in off["messages"]} == {""}


def test_read_one_returns_the_whole_message(faustus):
    h = helper_for(faustus)
    one = h.run("read", {"message_id": "<m5@x>", "html": "all"})
    assert one["ok"] and one["message"]["subject"] == "Tu reserva de vuelo" and one["message"]["images"][0]["alt"] == "Vueling logo"
    assert "html" in one["message"] and one["message"]["headers"]["one_click"] is True
    direct = h.run("read", {"account": "Personal", "folder": "INBOX", "uid": 4})
    assert direct["ok"] and direct["message"]["subject"] == "Factura de octubre" and "html" not in direct["message"]
    assert not h.run("read", {"message_id": "<nope@x>"})["ok"]
    assert "smtp not configured" in h.run("send", {"subject": "hi", "text": "t"})["error"]


# ---------------------------------------------------------------------------------------------------------------------
# MailRouter against the fake hub (Python)
# ---------------------------------------------------------------------------------------------------------------------

class FakeHelper:
    def __init__(self, answer=None, available=True, raises=None):
        self.answer = answer if answer is not None else {"ok": True, "accounts": [], "messages": []}
        self.up, self.raises = available, raises
        self.calls: list[tuple[str, dict]] = []

    def available(self):
        return self.up

    def run(self, action, payload=None, timeout=0):
        self.calls.append((action, dict(payload or {})))
        if self.raises:
            raise self.raises
        return self.answer


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def router_for(helper=None, **kw):
    mem = {"wm": 0}
    clock = kw.pop("clock", Clock())
    r = fam_mail.MailRouter(helper if helper is not None else FakeHelper(),
                            watermark_get=kw.pop("watermark_get", lambda: mem["wm"]), watermark_set=kw.pop("watermark_set", lambda v: mem.update(wm=v)),
                            clock=clock, **kw)
    r.mem, r.clock_obj = mem, clock
    return r


def test_router_pages_the_hub_and_moves_the_watermark_only_on_commit(hubs):
    hub = hubs()
    r = router_for(page=3, max_pages=2, claim_kind="document")
    a = r.scan_ex(since_days=60)
    assert [m["hub_id"] for m in a["messages"]] == [1, 2, 3, 4, 5, 6] and a["more"] is True and a["pending_since"] == 6
    assert r.last_source == "hub" and r.mem["wm"] == 0                               # nothing moved yet
    r.commit()
    assert r.mem["wm"] == 6 and r.watermark() == 6
    r.commit()                                                                       # a second commit is a no-op
    b = r.scan_ex(since_days=60)
    assert [m["hub_id"] for m in b["messages"]] == [7, 8] and b["more"] is False and b["pending_since"] == 8
    paths = [e[1] for e in hub.calls()]
    assert "since_id=0&limit=3&kind=mail&interest=1&full=1" in paths[0] and "since_id=3&" in paths[1] and "since_id=6&" in paths[2]
    r.commit()
    assert r.scan(since_days=60) == [] and r.watermark() == 8 and r.last_meta["pending_since"] is None


def test_router_a_scan_that_failed_never_moves_the_watermark(hubs):
    hub = hubs(fail_messages=True)
    helper = FakeHelper({"ok": False, "error": "no account"})
    r = router_for(helper)
    a = r.scan_ex(since_days=60)
    assert not a["ok"] and a["pending_since"] is None and r.last_source == "" and r.last_error == "no account"
    r.mem["wm"] = 5
    r.commit()
    assert r.mem["wm"] == 5
    assert hub.message_requests == 1 and len(helper.calls) == 1                      # auto: hub first, then the helper


def test_router_first_scan_applies_the_cutoff_then_trusts_the_watermark(hubs):
    hubs()
    r = router_for()
    first = r.scan_ex(since_days=30)
    assert [m["hub_id"] for m in first["messages"]] == [2, 3, 4, 5, 6, 7, 8] and first["pending_since"] == 8   # id 1 is 40 days old
    r.commit()
    assert r.scan(since_days=30) == []
    r.mem["wm"] = 0
    assert [m["hub_id"] for m in r.scan(since_days=4.5)] == [5, 6, 7, 8]


def test_router_limit_resumes_exactly_where_it_stopped(hubs):
    hubs()
    r = router_for()
    seen = []
    for _ in range(4):
        a = r.scan_ex(since_days=60, limit=3)
        seen.append([m["hub_id"] for m in a["messages"]])
        r.commit()
    assert seen == [[1, 2, 3], [4, 5, 6], [7, 8], []]
    assert r.scan_ex(limit=3)["more"] is False


def test_router_filters_run_on_this_side_of_the_hub(hubs):
    hubs()
    r = router_for()
    ids = lambda **c: [m["hub_id"] for m in r.scan(since_days=60, **c)]            # noqa: E731
    assert ids(query="factura") == [1, 2, 7]
    assert ids(query="FACTURA marzo") == [7]
    assert ids(skip=["<h2@x>", "<h3@x>"]) == [1, 4, 5, 6, 7, 8]
    assert ids(sender_domains=["tienda.es"]) == [1, 2, 7]
    assert ids(sender_domains=["@energia.es"]) == [4]                                # a subdomain counts; a leading @ is fine
    assert ids(skip_own=True) == [1, 2, 3, 4, 6, 7, 8]
    assert [m["hub_id"] for m in r.scan(since_days=60, limit=2, sender_domains=["tienda.es"])] == [1, 2]
    assert ids(order="desc")[:3] == [8, 7, 6]
    assert r.mem["wm"] == 0                                                          # no commit, no movement
    assert [m["hub_id"] for m in r.scan(since_days=3)] == [6, 7, 8]


def test_router_deep_rereads_everything_and_leaves_the_watermark(hubs):
    hub = hubs()
    r = router_for()
    r.mem["wm"] = 8
    assert r.scan(since_days=30) == []
    deep = r.scan_ex(since_days=30, deep=True)
    assert [m["hub_id"] for m in deep["messages"]] == [2, 3, 4, 5, 6, 7, 8] and deep["pending_since"] is None
    r.commit()
    assert r.mem["wm"] == 8
    assert "since_id=0&" in hub.calls()[-1][1]


def test_router_asks_for_fields_and_defaults_them(hubs):
    hub = hubs()
    r = router_for()
    rows = {m["hub_id"]: m for m in r.scan(since_days=60, fields="all")}
    assert any("fields=html,images,headers" in e[1] for e in hub.calls())
    assert rows[6]["html"].startswith("<script") and rows[6]["images"] == [{"alt": "Logo", "src": "https://cdn.x/logo.png"}]
    assert rows[6]["headers"]["one_click"] is True
    assert rows[2]["html"] == "" and rows[2]["images"] == [] and rows[2]["headers"] == {}   # asked for, absent: empty, never missing
    plain = {m["hub_id"]: m for m in router_for().scan(since_days=60)}
    assert "html" not in plain[6] and "images" not in plain[6]                        # the default answer is what it always was


def test_router_registers_the_interest_once_then_every_six_hours_and_when_it_changes(hubs):
    hub = hubs()
    clock = Clock()
    r = router_for(interest=lambda c: {"subject_terms": c.get("subject_terms", [])}, sphere="work", clock=clock)
    posts = lambda: [e for e in hub.calls() if e[0] == "POST"]                      # noqa: E731
    r.scan(subject_terms=["a"])
    r.scan(subject_terms=["a"])
    assert len(posts()) == 1 and posts()[0][2] == {"spec": {"subject_terms": ["a"]}, "sphere": "work"}
    r.scan(subject_terms=["b"])
    assert len(posts()) == 2 and posts()[1][2]["spec"] == {"subject_terms": ["b"]}
    clock.now += 21_601
    r.scan(subject_terms=["b"])
    assert len(posts()) == 3
    r.forget_interest()
    r.scan(subject_terms=["b"])
    assert len(posts()) == 4
    assert r.ensure_interest({"subject_terms": ["b"]}) == {"ok": True, "cached": True}
    assert r.ensure_interest({"subject_terms": ["b"]}, force=True) == {"ok": True}
    assert router_for(interest=None).ensure_interest()["skipped"] == "no interest"


def test_router_a_refused_interest_is_an_error_and_auto_falls_back(hubs):
    hubs()
    helper = FakeHelper({"ok": True, "messages": [{"message_id": "<f@x>", "subject": "from helper", "ts": T0}]})

    class Refuses:
        def available(self):
            return True

        def register_interest(self, spec, sphere=None):
            return {"ok": False, "error": "no gateway for you"}

    r = router_for(helper, interest={"subject_terms": ["x"]}, hub=Refuses())
    a = r.scan_ex()
    assert a["ok"] and a["source"] == "faustus" and a["fallback_from"] == "no gateway for you" and subjects(a) == ["from helper"]
    strict = router_for(helper, interest={"subject_terms": ["x"]}, hub=Refuses(), source_getter=lambda: "hub")
    b = strict.scan_ex()
    assert not b["ok"] and b["source"] == "hub" and "no gateway" in b["error"] and strict.last_error == b["error"]


def test_router_source_modes(hubs):
    hub = hubs()
    helper = FakeHelper({"ok": True, "accounts": [{"account": "Personal"}], "messages": [{"message_id": "<f@x>", "subject": "H", "ts": T0 - 5}]})
    mode = {"v": "auto"}
    r = router_for(helper, source_getter=lambda: mode["v"], interest={"subject_terms": ["x"]})
    assert r.mode() == "auto" and r.source_now() == "hub"
    hub.cfg["ready"] = False
    fam_mail.forget_availability()
    assert r.source_now() == "faustus"
    assert subjects(r.scan_ex(subject_terms=["x"], limit=5, deep=True, fields="all", order="asc", since_days=7)) == ["H"]
    (action, payload), = helper.calls
    assert action == "scan" and payload == {"subject_terms": ["x"], "max": 5, "since_days": 7}    # router-only keys do not reach the helper
    assert r.last_source == "faustus" and hub.calls() == []                          # the gateway was not read
    mode["v"] = "hub"
    assert r.source_now() == "hub" and r.mode() == "hub"
    for alias in ("own", "helper", "FAUSTUS"):
        mode["v"] = alias
        assert r.mode() == "faustus" and r.source_now() == "faustus"
    mode["v"] = "nonsense"
    assert r.mode() == "auto"
    assert router_for(source_getter=lambda: 1 / 0).mode() == "auto"
    hub.cfg["ready"] = True
    fam_mail.forget_availability()
    mode["v"] = "auto"
    assert r.status() == {"setting": "auto", "effective": "hub", "hub_available": True, "helper_available": True,
                          "interest_registered": False, "hub_since_id": 0, "last_source": "faustus", "last_error": ""}


def test_router_deep_can_go_to_the_helper(hubs):
    hubs()
    helper = FakeHelper({"ok": True, "messages": [{"message_id": "<f@x>", "subject": "deep", "ts": T0}]})
    r = router_for(helper, deep_uses_helper=True)
    assert r.source_now(deep=True) == "faustus" and r.source_now() == "hub"
    assert subjects(r.scan_ex(deep=True)) == ["deep"]
    helper.up = False
    assert r.source_now(deep=True) == "hub"


def test_router_helper_order_failures_and_missing_helper(hubs):
    hubs()
    rows = [{"message_id": "<a@x>", "subject": "old", "ts": 10}, {"message_id": "<b@x>", "subject": "new", "ts": 20}]
    helper = FakeHelper({"ok": True, "messages": rows + ["junk"]})
    r = router_for(helper, source_getter=lambda: "faustus")
    assert subjects(r.scan_ex(order="asc")) == ["old", "new"] and subjects(r.scan_ex(order="desc")) == ["new", "old"]
    helper.raises = RuntimeError("x")
    a = r.scan_ex()
    assert not a["ok"] and a["error"] == "mail helper: RuntimeError" and r.scan() == []
    none = fam_mail.MailRouter(None, source_getter=lambda: "faustus")
    assert none.scan_ex()["error"] == "no mail helper configured" and not none.helper_up()
    helper.raises, helper.answer = None, {"ok": False}
    assert r.scan_ex()["error"] == "the mail helper failed"


def test_router_the_watermark_lives_in_memory_without_callbacks(hubs):
    hubs()
    r = fam_mail.MailRouter(FakeHelper(), clock=Clock())
    assert r.watermark() == 0
    r.scan(since_days=60, limit=3)
    r.commit()
    assert r.watermark() == 3 and len(r.scan(since_days=60, limit=3)) == 3
    bad = fam_mail.MailRouter(FakeHelper(), watermark_get=lambda: "abc", clock=Clock())
    assert bad.watermark() == 0


def test_router_claims_what_came_from_the_hub_grouped_by_ref(hubs):
    hub = hubs()
    r = router_for(claim_kind="document")
    rows = r.scan(since_days=60, limit=3)
    assert r.claim(rows, ref="hoard://kafka/doc/1") == {"ok": True, "claimed": 3, "groups": 1}
    assert r.claim(rows[0], ref=lambda m: f"hoard://kafka/doc/{m['hub_id']}", kind="payment") == {"ok": True, "claimed": 1, "groups": 1}
    mixed = r.claim(rows + [{"subject": "from the helper"}, "junk"], ref=lambda m: "A" if m["hub_id"] < 3 else "B")
    assert mixed == {"ok": True, "claimed": 3, "groups": 2}
    assert r.claim([{"subject": "helper only"}]) == {"ok": True, "claimed": 0, "groups": 0}
    claims = [e[2] for e in hub.calls() if e[1] == "/api/mail/claim"]
    assert claims[0] == {"ids": [1, 2, 3], "kind": "document", "ref": "hoard://kafka/doc/1"}
    assert claims[1] == {"ids": [1], "kind": "payment", "ref": "hoard://kafka/doc/1"}
    assert claims[2:] == [{"ids": [1, 2], "kind": "document", "ref": "A"}, {"ids": [3], "kind": "document", "ref": "B"}]


def test_router_auto_claim_only_claims_hub_mail(hubs):
    hub = hubs()
    r = router_for(auto_claim=True, claim_kind="shipment")
    r.scan(since_days=60, limit=2)
    assert [e[2] for e in hub.calls() if e[1] == "/api/mail/claim"] == [{"ids": [1, 2], "kind": "shipment", "ref": ""}]
    helper = FakeHelper({"ok": True, "messages": [{"message_id": "<f@x>", "subject": "H"}]})
    h = router_for(helper, auto_claim=True, source_getter=lambda: "faustus")
    h.scan()
    assert len([e for e in hub.calls() if e[1] == "/api/mail/claim"]) == 1


def test_router_a_failing_page_after_data_returns_what_was_read(hubs):
    hubs(fail_page=2)
    r = router_for(page=3, max_pages=3)
    a = r.scan_ex(since_days=60)
    assert a["ok"] and [m["hub_id"] for m in a["messages"]] == [1, 2, 3] and a["more"] is True and a["pending_since"] == 3


def test_router_endpoint_down_in_hub_mode_is_an_error_not_a_fallback(hubs):
    hub = hubs(fail_messages=True)
    helper = FakeHelper()
    r = router_for(helper, source_getter=lambda: "hub")
    a = r.scan_ex(since_days=60)
    assert not a["ok"] and a["error"] == "gateway busy" and not helper.calls and r.last_source == ""
    assert hub.message_requests == 1


def test_messages_fields_python_and_the_unavailable_hub(hubs):
    hub = hubs()
    page = fam_mail.messages(since_id=5, limit=10, fields=["html", "images", "bogus"])
    assert page["ok"] and [m["id"] for m in page["messages"]] == [6, 7, 8] and page["last_id"] == 8
    assert "fields=html,images" in hub.log[-1][1] and "headers" not in hub.log[-1][1].split("fields=")[1]
    assert "headers" not in page["messages"][0] and page["messages"][1]["html"] == ""
    fam_mail.messages(fields="all")
    assert hub.log[-1][1].endswith("fields=html,images,headers")
    fam_mail.messages()
    assert "fields" not in hub.log[-1][1]
    hub.close()
    gone = fam_mail.messages(since_id=3, fields="all")
    assert gone == {"ok": False, "error": "hub unreachable", "messages": [], "last_id": 3}


# ---------------------------------------------------------------------------------------------------------------------
# The router end to end with the real helper (no hub)
# ---------------------------------------------------------------------------------------------------------------------

def test_router_with_the_real_helper_when_the_hub_is_not_there(faustus, tmp_path, isolated):
    family.configure("ledger", str(tmp_path / "appdata"), hub="http://127.0.0.1:1")
    fam_mail.forget_availability()
    r = fam_mail.MailRouter(helper_for(faustus), source_getter=lambda: "auto", interest={"subject_terms": ["factura"]})
    assert r.source_now() == "faustus"
    rows = r.scan(subject_terms=["factura", "reserva"], since_days=30, limit=10)
    assert [m["subject"] for m in rows] == ["Tu reserva de vuelo", "Factura de octubre"]
    assert r.last_source == "faustus" and r.last_error == "" and all("hub_id" not in m for m in rows)
    assert r.scan(subject_terms=["factura", "reserva"], since_days=30, limit=2, order="asc")[0]["subject"] == "Factura de octubre"
    r.commit()
    assert r.watermark() == 0 and r.claim(rows) == {"ok": True, "claimed": 0, "groups": 0}
    assert r.status()["helper_available"] is True and r.status()["effective"] == "faustus"
    bad = fam_mail.MailRouter(fam_mail.FaustusHelper(str(tmp_path / "none"), ask_hub=False), source_getter=lambda: "faustus")
    assert bad.scan(subject_terms=["x"]) == [] and "Faustus folder not found" in bad.last_error


# ---------------------------------------------------------------------------------------------------------------------
# Scenarios that run in Python and in Node and must agree
# ---------------------------------------------------------------------------------------------------------------------

HELPER_ROWS = {"ok": True, "accounts": [{"account": "Personal", "matches": 2}],
               "messages": [{"message_id": "<f1@x>", "subject": "Factura A", "ts": T0 - 100}, {"message_id": "<f2@x>", "subject": "Factura B", "ts": T0 - 200}]}

SCENARIOS: dict[str, dict[str, Any]] = {
    "first-scan-cutoff-then-quiet": {
        "steps": [{"op": "scan", "criteria": {"since_days": 30, "limit": 100}}, {"op": "commit"},
                  {"op": "scan", "criteria": {"since_days": 30}}, {"op": "commit"}, {"op": "status"}],
        "expect": [{"ok": True, "source": "hub", "ids": [2, 3, 4, 5, 6, 7, 8], "pending": 8, "more": False}, {"wm": 8},
                   {"ids": [], "pending": None}, {"wm": 8}, {"setting": "auto", "effective": "hub", "hub_available": True, "hub_since_id": 8}]},
    "paging": {
        "page": 3, "max_pages": 2,
        "steps": [{"op": "scan", "criteria": {"since_days": 60}}, {"op": "commit"}, {"op": "scan", "criteria": {"since_days": 60}}, {"op": "commit"}],
        "expect": [{"ids": [1, 2, 3, 4, 5, 6], "more": True, "pending": 6}, {"wm": 6}, {"ids": [7, 8], "more": False, "pending": 8}, {"wm": 8}]},
    "limit": {
        "steps": [{"op": "scan", "criteria": {"since_days": 60, "limit": 2}}, {"op": "commit"}, {"op": "scan", "criteria": {"since_days": 60, "limit": 2}},
                  {"op": "commit"}, {"op": "scan", "criteria": {"since_days": 60}}, {"op": "commit"}],
        "expect": [{"ids": [1, 2], "more": True, "pending": 2}, {"wm": 2}, {"ids": [3, 4], "more": True, "pending": 4}, {"wm": 4},
                   {"ids": [5, 6, 7, 8], "more": False, "pending": 8}, {"wm": 8}]},
    "filters": {
        "steps": [{"op": "scan", "criteria": {"since_days": 60, "query": "factura", "sender_domains": ["tienda.es"], "skip": ["<h2@x>"]}},
                  {"op": "scan", "criteria": {"since_days": 60, "sender_domains": ["@energia.es"]}},
                  {"op": "scan", "criteria": {"since_days": 60, "skip_own": True}},
                  {"op": "scan", "criteria": {"since_days": 60, "order": "desc", "limit": 3}},
                  {"op": "scan", "criteria": {"since_days": 60, "deep": True, "query": "reserva"}}],
        "expect": [{"ids": [1, 7]}, {"ids": [4]}, {"ids": [1, 2, 3, 4, 6, 7, 8]}, {"ids": [3, 2, 1]}, {"ids": [6], "pending": None}]},
    "fields": {
        "steps": [{"op": "scan", "criteria": {"since_days": 60, "fields": "all"}, "peek": [2, 6]},
                  {"op": "scan", "criteria": {"since_days": 60, "fields": ["images"]}, "peek": [2, 6]},
                  {"op": "scan", "criteria": {"since_days": 60}, "peek": [6]}],
        "expect": [{"peek": {"2": {"html": False, "images": [], "headers": {}}, "6": {"html": True, "images": [{"alt": "Logo", "src": "https://cdn.x/logo.png"}]}}},
                   {"peek": {"2": {"html": False, "images": []}}}, {"peek": {"6": {"html": False, "images": None, "headers": None}}}]},
    "interest": {
        "interest_fn": True, "sphere": "work",
        "steps": [{"op": "scan", "criteria": {"subject_terms": ["a"], "limit": 1}}, {"op": "scan", "criteria": {"subject_terms": ["a"], "limit": 1}},
                  {"op": "scan", "criteria": {"subject_terms": ["b"], "limit": 1}}, {"op": "tick", "seconds": 21601},
                  {"op": "scan", "criteria": {"subject_terms": ["b"], "limit": 1}}, {"op": "forget"}, {"op": "scan", "criteria": {"subject_terms": ["b"], "limit": 1}}],
        "expect": [{"ok": True}] * 5},
    "static-interest": {
        "interest": {"subject_terms": ["factura"], "has_attachment": True},
        "steps": [{"op": "scan", "criteria": {"limit": 1}}, {"op": "scan", "criteria": {"limit": 1}}],
        "expect": [{"ok": True}, {"ok": True}]},
    "auto-fallback-when-the-gateway-is-not-ready": {
        "hub": {"ready": False}, "helper": {"available": True, "answer": HELPER_ROWS},
        "steps": [{"op": "scan", "criteria": {"subject_terms": ["factura"], "limit": 5, "deep": True, "fields": "all", "since_days": 7, "gmail_query": "x"}},
                  {"op": "scan", "criteria": {"subject_terms": ["factura"], "order": "asc"}}, {"op": "commit"}, {"op": "status"}],
        "expect": [{"ok": True, "source": "faustus", "ids": [None, None], "last_source": "faustus", "pending": None},
                   {"subjects": ["Factura B", "Factura A"]}, {"wm": 0},
                   {"effective": "faustus", "hub_available": False, "helper_available": True}]},
    "camel-case-criteria": {
        "hub": {"ready": False}, "helper": {"available": True, "answer": HELPER_ROWS},
        "steps": [{"op": "scan", "criteria": {"since_days": 9, "sender_domains": ["a.es"], "skip_own": True, "subject_terms": ["x"]}}],
        "expect": [{"ok": True, "source": "faustus"}]},
    "hub-mode-never-falls-back": {
        "mode": "hub", "hub": {"fail_messages": True}, "helper": {"available": True, "answer": HELPER_ROWS},
        "steps": [{"op": "scan", "criteria": {"since_days": 60}}, {"op": "commit"}, {"op": "status"}],
        "expect": [{"ok": False, "source": "hub", "error": "gateway busy", "ids": [], "last_source": "", "last_error": "gateway busy"}, {"wm": 0},
                   {"setting": "hub", "effective": "hub"}]},
    "auto-falls-back-when-the-hub-fails": {
        "hub": {"fail_messages": True}, "helper": {"available": True, "answer": HELPER_ROWS},
        "steps": [{"op": "scan", "criteria": {"since_days": 60}}, {"op": "commit"}],
        "expect": [{"ok": True, "source": "faustus", "fb": "gateway busy", "last_source": "faustus"}, {"wm": 0}]},
    "faustus-mode-ignores-the-hub": {
        "mode": "own", "helper": {"available": True, "answer": HELPER_ROWS},
        "steps": [{"op": "scan", "criteria": {"since_days": 60}}, {"op": "mode", "value": "helper"}, {"op": "scan", "criteria": {}}, {"op": "status"}],
        "expect": [{"ok": True, "source": "faustus"}, {"ok": True}, {"setting": "faustus", "effective": "faustus", "hub_available": False}]},
    "helper-failure": {
        "mode": "faustus", "helper": {"available": True, "answer": {"ok": False, "error": "no account"}},
        "steps": [{"op": "scan", "criteria": {}}, {"op": "commit"}],
        "expect": [{"ok": False, "source": "faustus", "error": "no account", "last_error": "no account", "ids": []}, {"wm": 0}]},
    "no-helper-at-all": {
        "mode": "faustus", "helper": None,
        "steps": [{"op": "scan", "criteria": {}}, {"op": "status"}],
        "expect": [{"ok": False, "error": "no mail helper configured"}, {"helper_available": False}]},
    "claims": {
        "steps": [{"op": "scan", "criteria": {"since_days": 60, "limit": 4}}, {"op": "claim", "ref": "hoard://ledger/tx/1"},
                  {"op": "claim", "ref": "hoard://ledger/tx/2", "kind": "payment", "only": [2]}],
        "expect": [{"ids": [1, 2, 3, 4]}, {"claimed": 4, "groups": 1}, {"claimed": 1, "groups": 1}]},
    "auto-claim": {
        "auto_claim": True,
        "steps": [{"op": "scan", "criteria": {"since_days": 60, "limit": 2}}],
        "expect": [{"ids": [1, 2]}]},
    "page-fails-after-data": {
        "page": 3, "max_pages": 3, "hub": {"fail_page": 2},
        "steps": [{"op": "scan", "criteria": {"since_days": 60}}],
        "expect": [{"ok": True, "ids": [1, 2, 3], "more": True, "pending": 3}]},
    "deep-with-helper": {
        "deep_uses_helper": True, "helper": {"available": True, "answer": HELPER_ROWS},
        "steps": [{"op": "scan", "criteria": {"deep": True}}, {"op": "scan", "criteria": {"since_days": 60, "limit": 1}}],
        "expect": [{"source": "faustus"}, {"source": "hub", "ids": [1]}]},
}


def scenario_hub_cfg(scn: dict) -> dict:
    return dict(scn.get("hub") or {})


def interest_fn(criteria: dict) -> dict:
    return {"subject_terms": criteria.get("subject_terms", [])}


def run_scenario_python(url: str, scn: dict, data_dir: str) -> dict:
    family.configure("ledger", data_dir, hub=url)
    fam_mail.forget_availability()
    st = {"now": T0, "mode": scn.get("mode", "auto"), "wm": scn.get("wm0", 0)}
    helper_cfg = scn.get("helper", None)
    helper = None
    if "helper" in scn and helper_cfg is not None:
        helper = FakeHelper(helper_cfg["answer"], helper_cfg["available"])
    router = fam_mail.MailRouter(helper, source_getter=lambda: st["mode"], interest=interest_fn if scn.get("interest_fn") else scn.get("interest"),
                                 watermark_get=lambda: st["wm"], watermark_set=lambda v: st.update(wm=v), claim_kind="document",
                                 page=scn.get("page", 100), max_pages=scn.get("max_pages", 6), sphere=scn.get("sphere"), clock=lambda: st["now"],
                                 auto_claim=bool(scn.get("auto_claim")), deep_uses_helper=bool(scn.get("deep_uses_helper")))
    results, last_rows = [], []
    for step in scn["steps"]:
        op = step["op"]
        if op == "scan":
            a = router.scan_ex(**step["criteria"])
            last_rows = a["messages"]
            peek = {}
            for i in step.get("peek", []):
                m = next((x for x in last_rows if x.get("hub_id") == i), None)
                peek[str(i)] = None if m is None else {"html": bool(m.get("html")), "images": m.get("images"), "headers": m.get("headers")}
            results.append({"op": "scan", "ok": a["ok"], "source": a["source"], "ids": [m.get("hub_id") for m in a["messages"]], "subjects": [m["subject"] for m in a["messages"]],
                            "more": a.get("more"), "pending": a.get("pending_since"), "error": a.get("error"), "fb": a.get("fallback_from", ""),
                            "last_source": router.last_source, "last_error": router.last_error, "peek": peek})
        elif op == "commit":
            router.commit()
            results.append({"op": "commit", "wm": st["wm"]})
        elif op == "claim":
            rows = [m for m in last_rows if not step.get("only") or m.get("hub_id") in step["only"]]
            c = router.claim(rows, ref=step["ref"], kind=step.get("kind"))
            results.append({"op": "claim", "claimed": c["claimed"], "groups": c["groups"]})
        elif op == "status":
            s = router.status()
            results.append({"op": "status", **s})
        elif op == "tick":
            st["now"] += step["seconds"]
        elif op == "mode":
            st["mode"] = step["value"]
        elif op == "forget":
            router.forget_interest()
    return {"results": results, "helper_calls": [[a, p] for a, p in (helper.calls if helper else [])], "wm": st["wm"]}


JS_SCENARIO = """
const { url, dataDir, scn, t0 } = input;
fam.configure({ app: "ledger", dataDir, hub: url });
fam.mailForgetAvailability();
const st = { now: t0, mode: scn.mode ?? "auto", wm: scn.wm0 ?? 0 };
const hasHelper = "helper" in scn && scn.helper !== null;
const helperCalls = [];
const interestFn = (c) => ({ subject_terms: c.subject_terms ?? [] });
const router = fam.famMailRouter({
  runHelper: hasHelper ? async (a, p) => { helperCalls.push([a, p]); return scn.helper.answer; } : null,
  helperAvailable: async () => hasHelper && Boolean(scn.helper.available),
  sourceGetter: () => st.mode,
  interest: scn.interest_fn ? interestFn : (scn.interest ?? null),
  watermarkGet: () => st.wm, watermarkSet: (v) => { st.wm = v; },
  claimKind: "document", page: scn.page ?? 100, maxPages: scn.max_pages ?? 6, sphere: scn.sphere ?? null,
  clock: () => st.now, autoClaim: Boolean(scn.auto_claim), deepUsesHelper: Boolean(scn.deep_uses_helper),
});
const results = [];
let lastRows = [];
for (const step of scn.steps) {
  if (step.op === "scan") {
    const a = await router.scanEx(step.criteria);
    lastRows = a.messages;
    const peek = {};
    for (const i of step.peek ?? []) {
      const m = lastRows.find((x) => x.hub_id === i);
      peek[String(i)] = m === undefined ? null : { html: Boolean(m.html), images: m.images ?? null, headers: m.headers ?? null };
    }
    results.push({ op: "scan", ok: a.ok, source: a.source, ids: a.messages.map((m) => m.hub_id ?? null), subjects: a.messages.map((m) => m.subject),
      more: a.more ?? null, pending: a.pending_since ?? null, error: a.error ?? null, fb: a.fallback_from ?? "",
      last_source: router.lastSource, last_error: router.lastError, peek });
  } else if (step.op === "commit") {
    await router.commit();
    results.push({ op: "commit", wm: st.wm });
  } else if (step.op === "claim") {
    const rows = lastRows.filter((m) => !step.only || step.only.includes(m.hub_id));
    const c = await router.claim(rows, step.ref, step.kind ?? null);
    results.push({ op: "claim", claimed: c.claimed, groups: c.groups });
  } else if (step.op === "status") {
    results.push({ op: "status", ...(await router.status()) });
  } else if (step.op === "tick") { st.now += step.seconds; }
  else if (step.op === "mode") { st.mode = step.value; }
  else if (step.op === "forget") { router.forgetInterest(); }
}
return { results, helper_calls: helperCalls, wm: st.wm };
"""


def normalise(run: dict) -> dict:
    """Python gives ``None`` where a missing JS value is ``null`` too; make both sides JSON-comparable."""
    return json.loads(json.dumps(run, default=str))


def subset(expected: Any, got: Any, path: str = "") -> None:
    if isinstance(expected, dict):
        assert isinstance(got, dict), f"{path}: {got!r}"
        for k, v in expected.items():
            assert k in got, f"{path}.{k} missing in {got!r}"
            subset(v, got[k], f"{path}.{k}")
    else:
        assert expected == got, f"{path}: expected {expected!r}, got {got!r}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_python(name, hubs, tmp_path):
    scn = SCENARIOS[name]
    hub = hubs(**scenario_hub_cfg(scn))
    run = normalise(run_scenario_python(hub.url, scn, str(tmp_path / "appdata")))
    assert len(run["results"]) == len(scn["expect"]), run["results"]
    for i, (want, got) in enumerate(zip(scn["expect"], run["results"])):
        subset(want, got, f"{name}[{i}]")


def interest_posts(hub: FakeMailHub) -> list[dict]:
    return [e[2] for e in hub.calls() if e[1] == "/api/mail/interests"]


def test_scenario_python_requests_made_to_the_hub(hubs, tmp_path):
    scn = SCENARIOS["interest"]
    hub = hubs()
    run_scenario_python(hub.url, scn, str(tmp_path / "appdata"))
    assert [p["spec"]["subject_terms"] for p in interest_posts(hub)] == [["a"], ["b"], ["b"], ["b"]] and all(p["sphere"] == "work" for p in interest_posts(hub))
    hub2 = hubs()
    run_scenario_python(hub2.url, SCENARIOS["static-interest"], str(tmp_path / "appdata"))
    assert interest_posts(hub2) == [{"spec": {"subject_terms": ["factura"], "has_attachment": True}}]
    hub3 = hubs(ready=False)
    run = run_scenario_python(hub3.url, SCENARIOS["auto-fallback-when-the-gateway-is-not-ready"], str(tmp_path / "appdata"))
    assert hub3.calls() == []                                                        # the gateway is only asked about its status
    assert run["helper_calls"][0] == ["scan", {"subject_terms": ["factura"], "max": 5, "since_days": 7, "gmail_query": "x"}]
    assert run["helper_calls"][1] == ["scan", {"subject_terms": ["factura"], "max": 100, "since_days": 30}]
    hub4 = hubs()
    run_scenario_python(hub4.url, SCENARIOS["faustus-mode-ignores-the-hub"], str(tmp_path / "appdata"))
    assert hub4.log == []                                                            # not even a status poll


JS_SCENARIOS = sorted(SCENARIOS)


@pytest.mark.parametrize("name", JS_SCENARIOS)
def test_scenario_node_matches_python(name, hubs, tmp_path):
    scn = SCENARIOS[name]
    hub_py = hubs(**scenario_hub_cfg(scn))
    py = normalise(run_scenario_python(hub_py.url, scn, str(tmp_path / "appdata")))
    py_calls = hub_py.calls()
    hub_js = hubs(**scenario_hub_cfg(scn))
    js = run_link_js(JS_SCENARIO, {"url": hub_js.url, "dataDir": str(tmp_path / "appdata"), "scn": scn, "t0": T0})
    py_status = [r for r in py["results"] if r["op"] == "status"]
    js_status = [r for r in js["results"] if r["op"] == "status"]
    assert js["results"] == py["results"], name
    assert js["helper_calls"] == py["helper_calls"], name
    assert js["wm"] == py["wm"]
    assert hub_js.calls() == py_calls, name
    assert py_status == js_status


def test_node_camel_case_criteria_reach_the_helper_as_snake_case(hubs, tmp_path):
    hub = hubs(ready=False)
    scn = {"helper": {"available": True, "answer": HELPER_ROWS}, "steps": []}
    js = run_link_js("""
        const { url, dataDir, answer } = input;
        fam.configure({ app: "ledger", dataDir, hub: url });
        fam.mailForgetAvailability();
        const calls = [];
        const router = fam.famMailRouter({ runHelper: async (a, p) => { calls.push([a, p]); return answer; } });
        const rows = await router.scan({ sinceDays: 9, senderDomains: ["a.es"], skipOwn: true, subjectTerms: ["x"], gmailQuery: "q", limit: 3, deep: true });
        return { calls, n: rows.length, source: router.lastSource, mode: await router.mode() };
    """, {"url": hub.url, "dataDir": str(tmp_path / "d"), "answer": scn["helper"]["answer"]})
    assert js["calls"] == [["scan", {"since_days": 9, "sender_domains": ["a.es"], "skip_own": True, "subject_terms": ["x"], "gmail_query": "q", "max": 3}]]
    assert js["n"] == 2 and js["source"] == "faustus" and js["mode"] == "auto"


def test_node_router_never_rejects(hubs, tmp_path):
    hub = hubs()
    js = run_link_js("""
        const { url, dataDir } = input;
        fam.configure({ app: "ledger", dataDir, hub: url });
        fam.mailForgetAvailability();
        const bad = { mailAvailable: async () => { throw new Error("x"); }, mailRegisterInterest: async () => { throw new Error("x"); },
                      mailMessages: async () => { throw new Error("x"); }, mailClaim: async () => { throw new Error("x"); } };
        const r = fam.famMailRouter({ hub: bad, runHelper: async () => { throw new Error("helper"); }, interest: { subject_terms: ["x"] },
                                      sourceGetter: () => { throw new Error("mode"); } });
        const a = await r.scanEx({});
        const none = fam.famMailRouter({ sourceGetter: () => "faustus" });
        const b = await none.scanEx({});
        const claim = await r.claim([{ hub_id: 1 }], "r");
        return { a: [a.ok, a.source, a.error], b: [b.ok, b.error], claim, mode: await r.mode(), status: await none.status() };
    """, {"url": hub.url, "dataDir": str(tmp_path / "d")})
    assert js["a"] == [False, "faustus", "mail helper: Error"] and js["b"] == [False, "no mail helper configured"]
    assert js["claim"] == {"ok": True, "claimed": 0, "groups": 1} and js["mode"] == "auto" and js["status"]["helper_available"] is False


def test_node_mail_messages_fields_match_python(hubs, tmp_path):
    hub = hubs()
    py = fam_mail.messages(since_id=5, limit=10, fields=["html", "images"])
    js = run_link_js("""
        const { url, dataDir } = input;
        fam.configure({ app: "ledger", dataDir, hub: url });
        const a = await fam.mailMessages({ sinceId: 5, limit: 10, fields: ["html", "images", "bogus"] });
        const b = await fam.mailMessages({ fields: "all" });
        const c = await fam.mailMessages({});
        return { ids: a.messages.map((m) => m.id), last: a.last_id, htmls: a.messages.map((m) => m.html), images: a.messages.map((m) => m.images),
                 noHeaders: a.messages.every((m) => !("headers" in m)), b: b.messages.map((m) => [m.html === undefined, m.images === undefined, m.headers === undefined]).slice(0, 1),
                 plain: "html" in c.messages[0] };
    """, {"url": hub.url, "dataDir": str(tmp_path / "d")})
    assert js["ids"] == [m["id"] for m in py["messages"]] and js["last"] == py["last_id"] == 8
    assert js["htmls"] == [m["html"] for m in py["messages"]] and js["images"] == [m["images"] for m in py["messages"]] and js["noHeaders"]
    assert js["b"] == [[False, False, False]] and js["plain"] is False
    queries = [e[1].split("?", 1)[1] for e in hub.log if e[1].startswith("/api/mail/messages")]
    assert queries[1].endswith("&fields=html,images") and queries[2].endswith("&fields=html,images,headers") and "fields" not in queries[3]
    assert queries[0] == queries[1]                                                  # the same query string Python sent


# ---------------------------------------------------------------------------------------------------------------------
# Node: faustusDir and spawnHelperRunner
# ---------------------------------------------------------------------------------------------------------------------

def test_node_faustus_dir_matches_python(tmp_path, monkeypatch, isolated):
    clean_env(monkeypatch, tmp_path)
    a, b = make_faustus(tmp_path, name="a"), make_faustus(tmp_path, name="b")
    (tmp_path / "empty").mkdir()
    cases = [{"setting": str(a), "env": {}}, {"setting": None, "env": {"FAUSTUS_DIR": str(b)}},
             {"setting": str(a), "env": {"FAUSTUS_DIR": str(b)}}, {"setting": None, "env": {"HOARD_HUB_FAUSTUS_DIR": str(b)}},
             {"setting": str(tmp_path / "empty"), "env": {"HOARD_FAUSTUS_DIR": str(a)}},
             {"setting": None, "env": {"FAUSTUS_DIR": str(a), "HOARD_FAUSTUS_DIR": str(b)}}]
    js = run_link_js("""
        const out = input.cases.map((c) => fam.faustusDir(c.setting, { env: c.env }));
        out.push(fam.faustusDir(null, { env: {}, extra: [input.extra] }));
        return out;
    """, {"cases": cases, "extra": str(b)})
    py = []
    for c in cases:
        for key in fam_mail.FAUSTUS_ENV:
            monkeypatch.delenv(key, raising=False)
        for key, value in c["env"].items():
            monkeypatch.setenv(key, value)
        found = fam_mail.faustus_dir(c["setting"], ask_hub=False)
        py.append(str(found) if found else None)
    assert js[:6] == py == [str(a.resolve()), str(b.resolve()), str(a.resolve()), str(b.resolve()), str(a.resolve()), str(a.resolve())]
    assert js[6] == str(b.resolve())                                                   # `extra` is the last resort


def test_node_faustus_python_and_hub_hint(hubs, tmp_path):
    root = make_faustus(tmp_path)
    own = tmp_path / "python-own"
    own.write_text("", encoding="utf-8")
    hub = hubs(faustus_dir=str(root))
    js = run_link_js("""
        const { root, own, url, dataDir } = input;
        fam.configure({ app: "ledger", dataDir, hub: url });
        const first = fam.faustusPython(root, null, { env: {} });
        const viaSetting = fam.faustusPython(root, own, { env: {} });
        const viaEnv = fam.faustusPython(root, null, { env: { HOARD_FAUSTUS_PYTHON: own } });
        const fn = fam.faustusPython(root, () => own, { env: {} });
        const wrong = fam.faustusPython(root, "/nope/python", { env: {} });
        return { first, viaSetting, viaEnv, fn, wrong, hint: await fam.faustusHubHint(), candidates: fam.PYTHON_CANDIDATES };
    """, {"root": str(root), "own": str(own), "url": hub.url, "dataDir": str(tmp_path / "d")})
    assert js["first"] is None and js["wrong"] is None
    assert js["viaSetting"] == js["viaEnv"] == js["fn"] == str(own)
    assert js["hint"] == str(root)
    assert js["candidates"] == list(fam_mail.PYTHON_CANDIDATES)                      # the same Windows-first order on both sides


def spawn_scan_in_node(root: Path, criteria: dict, **opts: Any) -> dict:
    return run_link_js("""
        const { root, python, helper, criteria, opts } = input;
        const run = fam.spawnHelperRunner({ helperPath: helper, setting: root, python, owner: opts.owner ?? null,
                                            envDropPrefixes: opts.drop ?? [], env: { ...process.env, KAFKA_SECRET: "s3cret" } });
        return await run(opts.action ?? "scan", criteria, opts.timeout ?? 60000);
    """, {"root": str(root), "python": sys.executable, "helper": str(HELPER_FILE), "criteria": criteria, "opts": opts})


def test_node_spawn_runner_runs_the_real_helper_like_python_does(faustus):
    criteria = {"subject_terms": ["factura", "reserva"], "since_days": 30}
    py = helper_for(faustus).run("scan", criteria)
    js = spawn_scan_in_node(faustus, criteria)
    assert js["ok"] and subjects(js) == subjects(py) == ["Tu reserva de vuelo", "Factura de octubre"]
    for jm, pm in zip(js["messages"], py["messages"]):
        for key in ("uid", "images", "headers", "links", "text", "from_address", "message_id", "account"):
            assert jm[key] == pm[key], key
        assert ("html" in jm) == ("html" in pm)
    st = spawn_scan_in_node(faustus, {}, action="status")
    assert st["ok"] and st["accounts"][0]["account"] == "Personal"


def test_node_spawn_runner_failures_and_environment(tmp_path, faustus):
    none = run_link_js("""
        const run = fam.spawnHelperRunner({ helperPath: input.helper, setting: input.setting, env: {} });
        return await run("status");
    """, {"helper": str(HELPER_FILE), "setting": str(tmp_path / "nowhere")})
    assert none["ok"] is False and "Faustus folder not found" in none["error"]
    no_python = run_link_js("""
        const run = fam.spawnHelperRunner({ helperPath: input.helper, setting: input.setting, env: {} });
        return await run("status");
    """, {"helper": str(HELPER_FILE), "setting": str(faustus)})
    assert no_python == {"ok": False, "error": "Faustus has no venv with Python"}
    captured = run_link_js("""
        const { EventEmitter } = await import("node:events");
        const seen = {};
        const spawnFn = (cmd, args, opts) => {
          Object.assign(seen, { cmd, args, cwd: opts.cwd, hide: opts.windowsHide, secret: opts.env.KAFKA_SECRET ?? null, keep: opts.env.KEEP ?? null, enc: opts.env.PYTHONIOENCODING });
          const child = new EventEmitter();
          child.stdout = new EventEmitter();
          let sent = "";
          child.stdin = new EventEmitter();
          child.stdin.end = (data) => { sent = data; setImmediate(() => { child.stdout.emit("data", Buffer.from('log\\n{"ok": true, "echo": 1}\\n')); child.emit("close", 0); }); seen.sent = JSON.parse(data); };
          return child;
        };
        const run = fam.spawnHelperRunner({ helperPath: "/h/mail_helper.py", setting: input.setting, python: input.python, owner: () => "luis",
          envDropPrefixes: ["KAFKA_"], env: { KAFKA_SECRET: "x", KEEP: "yes" }, spawnFn });
        const answer = await run("scan", { subject_terms: ["a"] });
        return { answer, seen };
    """, {"setting": str(faustus), "python": sys.executable})
    seen = captured["seen"]
    assert captured["answer"] == {"ok": True, "echo": 1}
    assert seen["cmd"] == sys.executable and seen["args"] == ["/h/mail_helper.py", str(faustus.resolve())] and seen["cwd"] == str(faustus.resolve())
    assert seen["hide"] is True and seen["secret"] is None and seen["keep"] == "yes" and seen["enc"] == "utf-8"      # no console window; secrets stay out
    assert seen["sent"] == {"subject_terms": ["a"], "action": "scan", "owner": "luis"}
    for stdout, expect in (("", "mail helper exit 3"), ("{broken\\n", "mail helper exit 3"), ('{"no_ok": 1}\\n', "mail helper exit 3")):
        got = run_link_js("""
            const { EventEmitter } = await import("node:events");
            const spawnFn = () => {
              const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stdin = new EventEmitter();
              child.stdin.end = () => setImmediate(() => { child.stdout.emit("data", Buffer.from(input.stdout)); child.emit("close", 3); });
              return child;
            };
            return await fam.spawnHelperRunner({ helperPath: "/h", setting: input.setting, python: input.python, spawnFn })("status");
        """, {"setting": str(faustus), "python": sys.executable, "stdout": stdout.replace("\\n", "\n")})
        assert got == {"ok": False, "error": expect}
    timeout = run_link_js("""
        const { EventEmitter } = await import("node:events");
        let killed = false;
        const spawnFn = () => {
          const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stdin = new EventEmitter();
          child.stdin.end = () => {}; child.kill = () => { killed = true; };
          return child;
        };
        const run = fam.spawnHelperRunner({ helperPath: "/h", setting: input.setting, python: input.python, spawnFn });
        return { answer: await run("status", {}, 30), killed };
    """, {"setting": str(faustus), "python": sys.executable})
    assert timeout == {"answer": {"ok": False, "error": "the mail read took too long"}, "killed": True}


def test_node_router_end_to_end_with_the_real_helper(faustus, tmp_path):
    hub = FakeMailHub(ready=False)
    try:
        js = run_link_js("""
            const { url, dataDir, root, python, helper } = input;
            fam.configure({ app: "ledger", dataDir, hub: url });
            fam.mailForgetAvailability();
            const router = fam.famMailRouter({ runHelper: fam.spawnHelperRunner({ helperPath: helper, setting: root, python }),
              helperAvailable: async () => fam.faustusDir(root, { env: {} }) !== null && fam.faustusPython(root, python, { env: {} }) !== null });
            const rows = await router.scan({ subject_terms: ["factura", "reserva"], since_days: 30, limit: 10 });
            return { subjects: rows.map((m) => m.subject), source: router.lastSource, status: await router.status() };
        """, {"url": hub.url, "dataDir": str(tmp_path / "d"), "root": str(faustus), "python": sys.executable, "helper": str(HELPER_FILE)})
    finally:
        hub.close()
    assert js["subjects"] == ["Tu reserva de vuelo", "Factura de octubre"] and js["source"] == "faustus"
    assert js["status"]["effective"] == "faustus" and js["status"]["helper_available"] is True
