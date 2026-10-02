"""hoard_link.notify_channels: the one implementation of the toast, ntfy, Telegram, SMTP senders and the quiet-hours check.
Network senders run against local fakes (an injected transport, or a real http.server on loopback); nothing leaves the machine."""

from __future__ import annotations

import json
import os
import smtplib
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard_link import notify_channels as nc
from tests.hub.conftest import free_port


# ---- pure helpers ------------------------------------------------------------------------------------------------

def test_xml_escape_and_urls():
    assert nc.xml_escape("Tom & <Jerry> \"x\" 'y'") == "Tom &amp; &lt;Jerry&gt; &quot;x&quot; &#x27;y&#x27;"
    assert nc.xml_escape("a\x00b\x1fc\x0bd") == "abcd" and nc.xml_escape(None) == ""
    assert nc.http_url(" http://a.test/x ") == "http://a.test/x" and nc.http_url("HTTPS://a.test") == "HTTPS://a.test"
    assert nc.http_url("hoard://a/b/1") == "" and nc.http_url("file:///c:/x") == "" and nc.http_url("javascript:alert(1)") == "" and nc.http_url(None) == ""
    assert nc.clean_url("hoard://ledger/tx/1") == "hoard://ledger/tx/1" and nc.clean_url("ftp://x") == ""
    assert len(nc.clean_url("http://a.test/" + "x" * 900)) == 500


def test_scrub_hides_secrets_of_four_characters_or_more():
    assert nc.scrub("bad token ABCD-1234 for chat 42", ["ABCD-1234", "42", None, ""]) == "bad token *** for chat 42"
    assert nc.scrub(None, ["secret"]) == "" and nc.scrub("xyz", ["xyz"]) == "xyz"          # three characters are not hidden


# ---- the toast ---------------------------------------------------------------------------------------------------

def test_build_toast_ps1_is_exact():
    ps = nc.build_toast_ps1("Tom & <Jerry>", 'say "hi"', "http://127.0.0.1:5200/#/x?a=1&b=2")
    expected_xml = ('<toast activationType="protocol" launch="http://127.0.0.1:5200/#/x?a=1&amp;b=2"><visual><binding template="ToastGeneric">'
                    '<text>Tom &amp; &lt;Jerry&gt;</text><text>say &quot;hi&quot;</text></binding></visual></toast>')
    assert ps == (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        f"$xml = @'\n{expected_xml}\n'@\n"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$doc.LoadXml($xml)\n"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        r"'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'" ").Show($toast)\n")


def test_toast_link_title_body_limits_and_attribution():
    for url in ("file:///c:/x", "hoard://a/b/1", "", None):
        assert "launch=" not in nc.build_toast_ps1("t", "b", url)
    ps = nc.build_toast_ps1("T" * 200, "B" * 500, None, app_name="Kafka's Hoard", app_id="My.App")
    assert "T" * 120 in ps and "T" * 121 not in ps and "B" * 300 in ps and "B" * 301 not in ps
    assert '<text placement="attribution">Kafka&#x27;s Hoard</text>' in ps and "CreateToastNotifier('My.App')" in ps
    assert "attribution" not in nc.build_toast_ps1("t", "b")


class Done:
    def __init__(self, code):
        self.returncode = code


def test_send_toast_runs_powershell_without_a_window_and_cleans_up():
    ran = []

    def runner(cmd, **kw):
        ran.append((cmd, Path(cmd[-1]).read_bytes(), kw))
        return Done(0)

    res = nc.send_toast("Hola <b>", "mundo", "http://127.0.0.1:1/z", platform="win32", runner=runner)
    assert res == {"ok": True}
    cmd, raw, kw = ran[0]
    assert cmd[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"] and kw["timeout"] == 20 and kw["capture_output"] is True
    assert raw.startswith(b"\xef\xbb\xbf") and "Hola &lt;b&gt;".encode() in raw                  # the BOM Windows PowerShell 5.1 needs
    assert not os.path.exists(cmd[-1])
    assert nc.send_toast("x", platform="win32", runner=lambda cmd, **kw: Done(3)) == {"ok": False, "error": "powershell exit 3"}

    def boom(cmd, **kw):
        raise OSError("no powershell")
    assert nc.send_toast("x", platform="win32", runner=boom) == {"ok": False, "error": "OSError"}
    assert nc.send_toast("x", platform="linux", runner=runner) == {"ok": False, "unsupported": True, "error": "unsupported"}
    assert len(ran) == 1


# ---- a loopback web server for the HTTP senders -------------------------------------------------------------------

class Web:
    def __init__(self):
        self.requests: list[dict] = []
        self.answer = (200, {"ok": True})
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append({"method": method, "path": self.path, "headers": dict(self.headers),
                                       "json": json.loads(raw) if raw else None})
                status, payload = outer.answer
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._any("GET")

            def do_POST(self):  # noqa: N802
                self._any("POST")

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def web():
    w = Web()
    yield w
    w.close()


def test_http_json_post_get_errors_and_unreachable(web):
    status, data = nc.http_json(web.url + "/x", {"a": 1}, {"X-T": "1"})
    assert (status, data) == (200, {"ok": True})
    assert web.requests[0]["method"] == "POST" and web.requests[0]["json"] == {"a": 1} and web.requests[0]["headers"]["X-T"] == "1"
    assert nc.http_json(web.url + "/y")[0] == 200 and web.requests[1]["method"] == "GET"
    web.answer = (503, {"error": "down"})
    assert nc.http_json(web.url + "/z", {}) == (503, {"error": "down"})
    web.answer = (200, b"<html>not json</html>")
    assert nc.http_json(web.url + "/z", {}) == (200, None)
    status, name = nc.http_json(f"http://127.0.0.1:{free_port()}/", {}, timeout=1)
    assert status is None and isinstance(name, str) and name


# ---- ntfy ----------------------------------------------------------------------------------------------------------

def test_ntfy_posts_json_with_priority_click_token_tags_and_attachment(web):
    res = nc.send_ntfy(web.url + "/", "hoard_test", "Disk full", "C: is at 99%", priority="urgent", url="http://127.0.0.1:5200/x",
                       token="tk_secret_1234", tags=["warning"], attach="https://img.test/a.png")
    assert res == {"ok": True}
    req = web.requests[0]
    assert req["path"] == "/" and req["headers"]["Authorization"] == "Bearer tk_secret_1234"
    assert req["json"] == {"topic": "hoard_test", "title": "Disk full", "message": "C: is at 99%", "priority": 5, "tags": ["warning"],
                           "click": "http://127.0.0.1:5200/x", "attach": "https://img.test/a.png"}
    nc.send_ntfy(web.url, "t", "Only a title", priority="low", url="file:///x", attach="ftp://x")
    assert web.requests[1]["json"] == {"topic": "t", "title": "Only a title", "message": "Only a title", "priority": 2, "tags": ["bell"]}
    assert "Authorization" not in web.requests[1]["headers"]
    assert [nc.ntfy_priority(p) for p in ("urgent", "high", "normal", "low", "weird", 9, 0, 4)] == [5, 4, 3, 2, 3, 5, 1, 4]


def test_ntfy_errors(web):
    assert nc.send_ntfy(web.url, "", "x") == {"ok": False, "error": "not configured: missing topic"}
    assert nc.send_ntfy("ftp://x", "t", "x") == {"ok": False, "error": "invalid ntfy server"}
    web.answer = (503, {"error": "down"})
    assert nc.send_ntfy(web.url, "t", "x") == {"ok": False, "error": "http 503"}
    down = nc.send_ntfy("http://127.0.0.1:1", "t", "x", token="tk_secret_1234", timeout=1)
    assert down["ok"] is False and down["error"] and "tk_secret_1234" not in down["error"]
    leaky = nc.send_ntfy(web.url, "t", "x", token="tk_secret_1234", http=lambda url, payload, headers=None, timeout=1: (None, "Boom tk_secret_1234"))
    assert leaky == {"ok": False, "error": "Boom ***"}


# ---- Telegram ------------------------------------------------------------------------------------------------------

def test_telegram_text_escapes_and_only_links_http():
    assert nc.telegram_text("Tom & Jerry <3", "line", "http://x.test/a?b=1&c=2") == (
        '<b>Tom &amp; Jerry &lt;3</b>\nline\n<a href="http://x.test/a?b=1&amp;c=2">http://x.test/a?b=1&amp;c=2</a>')
    assert nc.telegram_text("T", "", "http://x.test", "Open") == '<b>T</b>\n<a href="http://x.test">Open</a>'
    assert nc.telegram_text("T", "b", "hoard://a/b") == "<b>T</b>\nb"


def test_telegram_send_and_errors(web):
    token = "123456:SECRET-token-ABCDE"
    res = nc.send_telegram(token, 98765, "<b>hi</b>", api_base=web.url)
    assert res == {"ok": True}
    req = web.requests[0]
    assert req["path"] == f"/bot{token}/sendMessage" and req["json"] == {"chat_id": "98765", "text": "<b>hi</b>", "parse_mode": "HTML"}
    nc.send_telegram(token, "1", "x" * 5000, api_base=web.url, parse_mode=None)
    assert len(web.requests[1]["json"]["text"]) == 4000 and "parse_mode" not in web.requests[1]["json"]
    web.answer = (400, {"ok": False, "description": f"Bad Request: chat 98765 not found for {token}"})
    err = nc.send_telegram(token, "98765", "x", api_base=web.url)["error"]
    assert token not in err and "98765" not in err and "***" in err
    web.answer = (500, b"")
    assert nc.send_telegram(token, "98765", "x", api_base=web.url) == {"ok": False, "error": "http 500"}
    assert nc.send_telegram("", "1", "x") == {"ok": False, "error": "not configured: missing bot token or chat id"}
    assert nc.send_telegram(token, "", "x")["ok"] is False
    leaky = nc.send_telegram(token, "98765", "x", http=lambda url, payload, headers=None, timeout=1: (None, f"Err {token} {98765}"))
    assert token not in leaky["error"] and "98765" not in leaky["error"]


def test_telegram_discovers_the_chat_id(web):
    web.answer = (200, {"ok": True, "result": []})
    res = nc.telegram_discover_chat_id("tok-123456", api_base=web.url)
    assert res["ok"] is False and "write to the bot" in res["error"] and res["chat_id"] == "" and res["name"] == ""
    web.answer = (200, {"ok": True, "result": [{"message": {"chat": {"id": 1, "first_name": "Old"}}},
                                               {"message": {"chat": {"id": 555, "first_name": "Luis", "last_name": "S"}}}]})
    assert nc.telegram_discover_chat_id("tok-123456", api_base=web.url) == {"ok": True, "chat_id": "555", "name": "Luis S", "error": ""}
    assert web.requests[-1]["path"].startswith("/bottok-123456/getUpdates")
    web.answer = (200, {"ok": True, "result": [{"my_chat_member": {"chat": {"id": -7, "title": "Family"}}}]})
    assert nc.telegram_discover_chat_id("tok-123456", api_base=web.url)["name"] == "Family"
    web.answer = (401, {"ok": False, "description": "Unauthorized tok-123456"})
    bad = nc.telegram_discover_chat_id("tok-123456", api_base=web.url)
    assert bad["ok"] is False and "tok-123456" not in bad["error"]
    assert nc.telegram_discover_chat_id("")["error"] == "save the bot token first"


# ---- mail ----------------------------------------------------------------------------------------------------------

class FakeSMTP:
    def __init__(self, fail=None):
        self.sent, self.login_args, self.quit_called, self.fail = [], None, False, fail

    def login(self, user, password):
        if self.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.login_args = (user, password)

    def send_message(self, msg):
        if self.fail == "send":
            raise smtplib.SMTPException("nope")
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True


def test_email_parts():
    subject, text, html = nc.email_parts("Hola\r\nBcc: x", "line 1\n\nline 2", "http://a.test/x?a=1&b=2")
    assert "\n" not in subject and text == "line 1\n\nline 2\n\nhttp://a.test/x?a=1&b=2"
    assert "<h3>Hola" in html and "<p>line 1</p><p>line 2</p>" in html and 'href="http://a.test/x?a=1&amp;b=2"' in html
    assert nc.email_parts("Only", "", None)[1] == "Only"


def test_send_smtp():
    fake = FakeSMTP()
    cfg = {"host": "smtp.test", "port": 465, "user": "me@x.com", "password": "pw-secret-1", "from": "Hoard <me@x.com>"}
    res = nc.send_smtp(cfg, ["a@b.co", "", "c@d.co"], "Subj\nect", "body", html="<b>b</b>", smtp_factory=lambda c: fake)
    assert res == {"ok": True} and fake.login_args == ("me@x.com", "pw-secret-1") and fake.quit_called
    msg = fake.sent[0]
    assert msg["To"] == "a@b.co, c@d.co" and msg["From"] == "Hoard <me@x.com>" and "\n" not in msg["Subject"] and msg.is_multipart()
    fake2 = FakeSMTP()
    nc.send_smtp({"host": "h", "port": 25, "tls": False}, "a@b.co, c@d.co", "s", "b", smtp_factory=lambda c: fake2)     # a string is a comma list
    assert fake2.sent[0]["To"] == "a@b.co, c@d.co"
    fake2.sent.clear()
    nc.send_smtp({"host": "h"}, ["a@b.co"], "s", "b", smtp_factory=lambda c: fake2, default_from="hub@localhost")
    assert fake2.sent[0]["From"] == "hub@localhost" and fake2.login_args is None
    assert nc.send_smtp({"host": ""}, ["a@b.co"], "s")["error"].startswith("not configured")
    assert nc.send_smtp({"host": "h"}, [], "s")["error"].startswith("not configured")
    assert nc.send_smtp(cfg, ["a@b.co"], "s", smtp_factory=lambda c: FakeSMTP("auth")) == {"ok": False, "error": "authentication failed"}
    assert nc.send_smtp(cfg, ["a@b.co"], "s", smtp_factory=lambda c: FakeSMTP("send")) == {"ok": False, "error": "SMTPException"}

    def refuse(c):
        raise OSError("connection refused pw-secret-1")
    assert nc.send_smtp(cfg, ["a@b.co"], "s", smtp_factory=refuse) == {"ok": False, "error": "OSError"}


def test_default_smtp_picks_ssl_starttls_or_plain(monkeypatch):
    made = []

    class S:
        def __init__(self, kind):
            made.append(kind)

        def starttls(self, context=None):
            made.append("starttls")

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda host, port, timeout, context: S(f"ssl:{port}"))
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout: S(f"plain:{port}"))
    nc.default_smtp({"host": "h", "port": 465, "tls": True})
    nc.default_smtp({"host": "h", "port": 587})
    nc.default_smtp({"host": "h", "port": 25, "tls": False})
    assert made == ["ssl:465", "plain:587", "starttls", "plain:25"]


def test_send_via_helper():
    calls = []

    def run(action, payload, timeout):
        calls.append((action, payload, timeout))
        return {"ok": True}

    assert nc.send_via_helper(run, "Subject", "Body", html="<b>B</b>", to=["a@b.co"]) == {"ok": True}
    assert calls[0] == ("send", {"subject": "Subject", "text": "Body", "html": "<b>B</b>", "to": ["a@b.co"]}, 60.0)
    assert nc.send_via_helper(lambda a, p, t: {"ok": False, "error": "smtp not configured"}, "s") == {"ok": False, "error": "smtp not configured"}
    assert nc.send_via_helper(lambda a, p, t: "weird", "s")["ok"] is False

    def boom(a, p, t):
        raise RuntimeError("x")
    assert nc.send_via_helper(boom, "s") == {"ok": False, "error": "RuntimeError"}


# ---- quiet hours -----------------------------------------------------------------------------------------------------

def at(h, m=0, day=5):                          # 2026-10-05 is a Monday
    return datetime(2026, 10, day, h, m)


@pytest.mark.parametrize("now,start,end,prio,allow_high,expected", [
    (at(23, 30), "23:00", "08:00", "normal", True, True),         # late evening, window crosses midnight
    (at(3), "23:00", "08:00", "normal", True, True),              # small hours
    (at(8), "23:00", "08:00", "normal", True, False),             # end is exclusive
    (at(12), "23:00", "08:00", "normal", True, False),
    (at(23, 0), "23:00", "08:00", "low", True, True),             # start is inclusive
    (at(14), "13:00", "15:00", "normal", True, True),             # a same-day window
    (at(15), "13:00", "15:00", "normal", True, False),
    (at(3), "23:00", "08:00", "urgent", True, False),             # urgent always gets through
    (at(3), "23:00", "08:00", "urgent", False, False),
    (at(3), "23:00", "08:00", "high", True, False),               # high gets through unless allow_high=False
    (at(3), "23:00", "08:00", "high", False, True),
    (at(3), "08:00", "08:00", "normal", True, False),             # start == end: off
    (at(3), "", "08:00", "normal", True, False),                  # unreadable: off
    (at(3), "25:00", "08:00", "normal", True, False),
    (at(3), 23 * 60, 8 * 60, "normal", True, True),               # minutes since midnight work too
])
def test_in_quiet_hours(now, start, end, prio, allow_high, expected):
    assert nc.in_quiet_hours(now, start, end, priority=prio, allow_high=allow_high) is expected


def test_quiet_hours_days_and_midnight_ownership():
    weekdays = {0, 1, 2, 3, 4}
    # Friday night 23:30 is inside Friday's window; Saturday 03:00 belongs to the window that started on Friday
    assert nc.in_quiet_hours(at(23, 30, day=9), "23:00", "08:00", days=weekdays) is True           # Friday
    assert nc.in_quiet_hours(at(3, 0, day=10), "23:00", "08:00", days=weekdays) is True            # Saturday 03:00 <- Friday's window
    assert nc.in_quiet_hours(at(23, 30, day=10), "23:00", "08:00", days=weekdays) is False         # Saturday night: no window
    assert nc.in_quiet_hours(at(3, 0, day=12), "23:00", "08:00", days=weekdays) is False           # Monday 03:00 <- Sunday's window: none
    assert nc.in_quiet_hours(at(14, day=10), "13:00", "15:00", days=weekdays) is False
    assert nc.in_quiet_hours(None, "00:00", "23:59") in (True, False)                              # now defaults to the clock
