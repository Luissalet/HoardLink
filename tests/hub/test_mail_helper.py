"""The stdlib mail helper (hub/mail_helper.py): pure functions, then the real file run with ``sys.executable`` against a fake
Faustus root whose ``mcp_servers/email_server.py`` hands out an in-memory IMAP connection."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from email.message import EmailMessage
from pathlib import Path

import pytest

from hoard_link.hub import mail_helper as mh

HELPER = Path(mh.__file__)


def _mail(subject="Hola", frm="Ana <ana@example.com>", to="me@example.com", cc="", body="Texto de prueba", html=None, mid="<a1@x>",
          in_reply_to="", refs="", pdf=None, date="Mon, 28 Sep 2026 10:00:00 +0200") -> EmailMessage:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"], m["Message-ID"] = subject, frm, to, date, mid
    if cc:
        m["Cc"] = cc
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if refs:
        m["References"] = refs
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if pdf:
        m.add_attachment(pdf, maintype="application", subtype="pdf", filename="factura.pdf")
    return m


# ---- pure functions ---------------------------------------------------------------------------

def test_plan_uids_incremental_takes_the_oldest_and_ignores_the_watermark_quirk():
    # IMAP "UID 10:*" always returns the last message, even below the watermark
    assert mh.plan_uids([b"7"], last_uid=10, limit=50) == ([], 0)
    assert mh.plan_uids([b"7", b"11", b"12", b"15"], last_uid=10, limit=2) == ([11, 12], 1)
    assert mh.plan_uids([b"12", b"11"], last_uid=10, limit=10) == ([11, 12], 0)


def test_plan_uids_first_read_takes_the_newest():
    assert mh.plan_uids([b"1", b"2", b"3", b"4"], last_uid=0, limit=2) == ([3, 4], 2)
    assert mh.plan_uids([], last_uid=0, limit=5) == ([], 0)
    assert mh.plan_uids([b"x", 3, "4"], last_uid=0, limit=0) == ([], 2)


def test_message_to_record_headers_text_links_and_attachment(tmp_path):
    msg = _mail(subject="Factura de octubre", cc="Bob <bob@example.com>, ana@example.com", html='<p>Hola <a href="https://pay.example.com/x">pagar</a></p>',
                in_reply_to="<root@x>", refs="<root@x> <mid@x>", pdf=b"%PDF-1.4 fake pdf bytes")
    rec = mh.message_to_record(msg, None, str(tmp_path / "att"))
    assert rec["subject"] == "Factura de octubre" and rec["from_address"] == "ana@example.com" and rec["from_name"] == "Ana"
    assert rec["to"] == ["me@example.com"] and rec["cc"] == ["bob@example.com", "ana@example.com"]
    assert rec["in_reply_to"] == "<root@x>" and rec["references"] == ["<root@x>", "<mid@x>"]
    assert rec["date_ts"] == rec["ts"] and rec["date_ts"] > 1.78e9
    assert "Hola" in rec["text"] and {"url": "https://pay.example.com/x", "label": "pagar"} in rec["links"]
    [att] = rec["attachments"]
    assert att["name"] == "factura.pdf" and att["size"] == len(b"%PDF-1.4 fake pdf bytes") and Path(att["path"]).read_bytes().startswith(b"%PDF")
    assert Path(att["path"]).name == att["sha"] + ".pdf"


def test_message_to_record_survives_garbage():
    rec = mh.message_to_record(_mail(date="not a date"), None, "")
    assert rec["date_ts"] is None and rec["attachments"] == []


def test_build_message_headers_cannot_be_injected():
    msg = mh.build_message({"subject": "Hola\r\nBcc: evil@x.com", "text": "t", "html": "<b>t</b>", "from_name": "Hub\nX"}, "me@example.com", ["a@b.co"])
    assert "\n" not in msg["Subject"] and msg["Bcc"] is None and msg["To"] == "a@b.co"
    assert msg.is_multipart()                                    # text + html alternative
    assert mh._addresses("a@b.co, not-an-address,x@y.org") == ["a@b.co", "x@y.org"]


def test_account_key_prefers_the_name():
    assert mh.account_key({"account_name": "Work", "imap_user": "w@corp.com", "id": 3}) == "Work"
    assert mh.account_key({"imap_user": "w@corp.com", "id": 3}) == "w@corp.com"
    assert mh.account_address({"imap_user": "login", "from_address": "me@x.com"}) == "me@x.com"


# ---- the real file against a fake Faustus --------------------------------------------------------

FAKE_SERVER = textwrap.dedent('''
    from email.message import EmailMessage

    def _raw(uid, subject, mid, day):
        m = EmailMessage()
        m["Subject"], m["From"], m["To"], m["Message-ID"] = subject, "Ana <ana@example.com>", "me@example.com", mid
        m["Date"] = f"{day} Sep 2026 10:00:00 +0000"
        m.set_content("cuerpo " + subject)
        return m.as_bytes()

    MESSAGES = {1: _raw(1, "uno", "<m1@x>", 25), 2: _raw(2, "dos", "<m2@x>", 26), 3: _raw(3, "tres", "<m3@x>", 27)}
    ACCOUNTS = [{"id": 1, "account_name": "Personal", "imap_user": "me@example.com", "from_address": "me@example.com",
                 "smtp_user": "me@example.com", "imap_host": "imap.example.com", "imap_port": 993, "enabled": 1, "owner": "luis"}]

    def _read_accounts_from_db(): return list(ACCOUNTS)
    def _filter_accounts_for_owner(rows): return rows
    def _decode_header(v): return str(v)
    def _q(f): return f
    def _resolve_send_config(account): raise RuntimeError("smtp not configured")

    class _Conn:
        def select(self, folder, readonly=True): return ("OK", [b"3"]) if folder == "INBOX" else ("NO", [])
        def response(self, name): return (name, [b"42"])
        def logout(self): pass
        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                crit = list(args[1:])
                if crit[0] == "UID":
                    lo = int(crit[1].split(":")[0])
                    hits = [u for u in MESSAGES if u >= lo] or [max(MESSAGES)]   # the IMAP quirk
                elif crit[0] == "HEADER":
                    mid = crit[2].strip('"')
                    hits = [u for u, raw in MESSAGES.items() if mid.encode() in raw]
                else:
                    hits = list(MESSAGES)
                return "OK", [" ".join(str(h) for h in hits).encode()]
            if cmd == "FETCH":
                raw = MESSAGES.get(int(args[0]))
                return ("OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]) if raw else ("NO", [])
            return "NO", []

    def _imap_connect(selector): return _Conn()
''')


@pytest.fixture
def fake_faustus(tmp_path):
    root = tmp_path / "faustus"
    (root / "mcp_servers").mkdir(parents=True)
    (root / "mcp_servers" / "email_server.py").write_text(FAKE_SERVER, encoding="utf-8")
    return root


def run_helper(root: Path, request: dict) -> dict:
    done = subprocess.run([sys.executable, str(HELPER), str(root)], input=json.dumps(request), capture_output=True, text=True,
                          encoding="utf-8", timeout=60, cwd=str(root))
    lines = [ln for ln in done.stdout.splitlines() if ln.strip().startswith("{")]
    assert lines, done.stderr
    return json.loads(lines[-1])


def test_real_helper_status(fake_faustus):
    ans = run_helper(fake_faustus, {"action": "status"})
    assert ans["ok"] and ans["accounts"][0]["account"] == "Personal" and ans["accounts"][0]["address"] == "me@example.com"
    assert "me@example.com" not in json.dumps(ans["accounts"][0]["user"])             # the masked form for display


def test_real_helper_first_fetch_then_incremental(fake_faustus, tmp_path):
    first = run_helper(fake_faustus, {"action": "fetch", "since_days": 14, "max": 300, "attachments_dir": str(tmp_path / "att")})
    assert first["ok"] and [m["subject"] for m in first["messages"]] == ["uno", "dos", "tres"]        # oldest first, newest last
    m = first["messages"][-1]
    assert (m["account"], m["folder"], m["uid"], m["to"], m["from_self"]) == ("Personal", "INBOX", 3, ["me@example.com"], False)
    folder = first["accounts"][0]["folders"]["INBOX"]
    assert folder["last_uid"] == 3 and folder["validity"] == 42 and folder["new"] == 3

    after_two = run_helper(fake_faustus, {"action": "fetch", "since": {"Personal": {"INBOX": 2}}, "validity": {"Personal": {"INBOX": 42}}})
    assert [m["uid"] for m in after_two["messages"]] == [3]

    nothing = run_helper(fake_faustus, {"action": "fetch", "since": {"Personal": {"INBOX": 3}}})
    assert nothing["ok"] and nothing["messages"] == [] and nothing["accounts"][0]["folders"]["INBOX"]["last_uid"] == 3

    renumbered = run_helper(fake_faustus, {"action": "fetch", "since": {"Personal": {"INBOX": 3}}, "validity": {"Personal": {"INBOX": 7}}})
    assert len(renumbered["messages"]) == 3                                  # UIDVALIDITY changed: start over

    capped = run_helper(fake_faustus, {"action": "fetch", "since": {"Personal": {"INBOX": 1}}, "max": 1})
    assert [m["uid"] for m in capped["messages"]] == [2] and capped["accounts"][0]["folders"]["INBOX"]["remaining"] == 1
    assert capped["accounts"][0]["folders"]["INBOX"]["last_uid"] == 2        # the watermark stops where the read stopped


def test_real_helper_read_and_send_errors(fake_faustus):
    by_id = run_helper(fake_faustus, {"action": "read", "message_id": "<m2@x>"})
    assert by_id["ok"] and by_id["message"]["subject"] == "dos" and by_id["message"]["uid"] == 2
    direct = run_helper(fake_faustus, {"action": "read", "account": "Personal", "folder": "INBOX", "uid": 1})
    assert direct["ok"] and direct["message"]["subject"] == "uno"
    assert not run_helper(fake_faustus, {"action": "read", "message_id": "<nope@x>"})["ok"]
    assert not run_helper(fake_faustus, {"action": "read"})["ok"]
    sent = run_helper(fake_faustus, {"action": "send", "subject": "hi", "text": "t"})
    assert not sent["ok"] and "smtp not configured" in sent["error"]


def test_real_helper_without_accounts_or_module(tmp_path, fake_faustus):
    (fake_faustus / "mcp_servers" / "email_server.py").write_text(FAKE_SERVER + "\nACCOUNTS.clear()\n", encoding="utf-8")
    assert "no enabled mail account" in run_helper(fake_faustus, {"action": "status"})["error"]
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert "not loadable" in run_helper(empty, {"action": "status"})["error"]


def test_the_helper_does_not_let_the_hubs_own_modules_shadow_faustus(tmp_path):
    """mail_helper.py sits next to the hub's mcp.py: Faustus's ``import mcp`` must find Faustus's package."""
    import json as _json
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path
    root = tmp_path / "faustus"
    (root / "mcp_servers").mkdir(parents=True)
    (root / "mcp_servers" / "__init__.py").write_text("", encoding="utf-8")
    (root / "mcp").mkdir()
    (root / "mcp" / "__init__.py").write_text("FROM_FAUSTUS = True\n", encoding="utf-8")
    (root / "mcp_servers" / "email_server.py").write_text(
        "import mcp\nassert mcp.FROM_FAUSTUS\n"
        "def _read_accounts_from_db():\n    return []\n", encoding="utf-8")
    helper = _Path(__file__).resolve().parents[2] / "hoard_link" / "hub" / "mail_helper.py"
    done = subprocess.run([_sys.executable, str(helper), str(root)], input=_json.dumps({"action": "status"}),
                          capture_output=True, text=True, cwd=str(root), timeout=60)
    answer = _json.loads(done.stdout.strip().splitlines()[-1])
    assert "not loadable" not in str(answer.get("error")), answer
