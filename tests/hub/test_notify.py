"""The notifications facet ("Hermes"): routing by sphere and priority, quiet hours, duplicates, the rate limit,
every channel (toast, ntfy, Telegram, mail through the gateway or SMTP) against local fakes, secrets masking,
history, HTTP routes through the real server and the agent tools. Nothing here touches the network."""

from __future__ import annotations

import json
import os
import smtplib
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hoard_link.hub import tools
from hoard_link.hub.notify import (CHANNELS, MASK, NotifyFacet, build_toast_ps1, clean_url, is_masked, mask,
                                   xml_escape)
from hoard_link.hub.server import make_server

from .conftest import free_port
from .test_hub_and_server import _http

UI = {"Sec-Fetch-Site": "same-origin"}
NOON = datetime(2026, 10, 5, 12, 0)      # a Monday, outside every default quiet window
NIGHT = datetime(2026, 10, 5, 23, 30)


class Rec:
    """A channel sender that records what it was asked and answers what it is told."""

    def __init__(self, result=None):
        self.calls: list[tuple[dict, dict]] = []
        self.result = {"ok": True} if result is None else result

    def __call__(self, note, cfg):
        self.calls.append((dict(note), dict(cfg)))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    @property
    def titles(self):
        return [n["title"] for n, _ in self.calls]


@pytest.fixture
def fx(hub):
    """The notify facet with every sender faked and every channel enabled; quiet hours off (the clock never matters)."""
    f = hub.facet("notify")
    assert isinstance(f, NotifyFacet)
    f.senders = {c: Rec() for c in CHANNELS}
    f.update_config({"channels": {c: {"enabled": True} for c in CHANNELS}})
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "", "end": "", "days": "daily"}})
    return f


def events(hub, type_):
    return list(reversed(hub.events.query(type=type_)))


# ---- routing -----------------------------------------------------------------------------------------------

def test_normal_goes_through_the_spheres_route(fx, hub):
    res = fx.send("Backup done", "12 files", app="ledger", url="http://127.0.0.1:1/x", group="job", tags=["a", "b"], now=NOON)
    assert res["ok"] and res["held"] is None and res["sphere"] == "personal" and res["priority"] == "normal"
    assert [c["channel"] for c in res["channels"]] == ["windows"] and res["delivered"] == ["windows"]
    note, cfg = fx.senders["windows"].calls[0]
    assert note["title"] == "Backup done" and note["body"] == "12 files" and note["app"] == "ledger" and note["id"] == res["id"]
    assert note["url"] == "http://127.0.0.1:1/x" and note["tags"] == ["a", "b"] and cfg["enabled"] is True
    for c in ("ntfy", "telegram", "email"):
        assert fx.senders[c].calls == []
    row = fx.get_notification(res["id"])
    assert row["title"] == "Backup done" and row["group"] == "job" and row["held"] is None and row["seen"] is False
    assert row["channels"][0]["channel"] == "windows" and row["channels"][0]["ok"] is True
    ev = events(hub, "notify.sent")[-1]["data"]
    assert ev["id"] == res["id"] and ev["app"] == "ledger" and ev["sphere"] == "personal" and ev["channels"] == ["windows"]
    assert ev["title"] == "Backup done" and ev["priority"] == "normal"


def test_urgent_uses_every_channel_of_the_route(fx):
    res = fx.send("Disk full", priority="urgent", now=NOON)
    assert [c["channel"] for c in res["channels"]] == ["windows", "telegram"]
    assert fx.senders["telegram"].titles == ["Disk full"] and fx.senders["windows"].titles == ["Disk full"]
    res = fx.send("Careful", priority="high", now=NOON)
    assert [c["channel"] for c in res["channels"]] == ["windows"]
    assert fx.send("x", priority="bogus", now=NOON)["priority"] == "normal"


def test_a_disabled_channel_is_skipped_not_failed(fx):
    fx.update_config({"channels": {"telegram": {"enabled": False}}})
    res = fx.send("Disk full", priority="urgent", now=NOON)
    tg = next(c for c in res["channels"] if c["channel"] == "telegram")
    assert tg["ok"] is False and tg["skipped"] is True and tg["error"] == "disabled"
    assert res["delivered"] == ["windows"] and fx.senders["telegram"].calls == []


def test_low_is_kept_for_the_digest(fx, hub):
    res = fx.send("Price dropped", app="tantalus", priority="low", url="http://x.test/p", group="price", now=NOON)
    assert res["held"] == "digest" and res["channels"] == [] and res["delivered"] == []
    assert all(r.calls == [] for r in fx.senders.values())
    item = events(hub, "digest.item")[-1]["data"]
    assert item == {"title": "Price dropped", "url": "http://x.test/p", "watch": "tantalus", "kind": "price", "sphere": "personal"}
    held = events(hub, "notify.held")[-1]["data"]
    assert held["reason"] == "digest" and held["id"] == res["id"] and held["app"] == "tantalus"
    assert fx.send("Another", priority="low", now=NOON)["held"] == "digest"
    assert events(hub, "digest.item")[-1]["data"]["kind"] == "notify"           # no group: "notify"


def test_a_route_with_digest_and_a_channel_pushes_and_keeps(fx, hub):
    hub.facet("spheres").upsert({"id": "personal", "notify": {"normal": ["windows", "digest"]}})
    res = fx.send("Both", now=NOON)
    assert res["held"] is None and res["delivered"] == ["windows"]
    assert events(hub, "digest.item")[-1]["data"]["title"] == "Both"


def test_quiet_hours_hold_everything_but_urgent(fx, hub):
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "22:30", "end": "08:00", "days": "daily"}})
    res = fx.send("Late news", app="links", priority="high", now=NIGHT)
    assert res["held"] == "quiet" and fx.senders["windows"].calls == []
    assert events(hub, "notify.held")[-1]["data"]["reason"] == "quiet"
    assert events(hub, "digest.item")[-1]["data"]["title"] == "Late news"        # it shows up in the digest instead
    res = fx.send("Fire", priority="urgent", now=NIGHT)
    assert res["held"] is None and res["delivered"] == ["windows", "telegram"]
    assert fx.send("Morning", now=NOON)["held"] is None


def test_the_sphere_decides_the_route(fx, hub):
    res = fx.send("Standup moved", sphere="work", now=NOON)                      # work: normal -> digest
    assert res["sphere"] == "work" and res["held"] == "digest"
    res = fx.send("Build broke", sphere="work", priority="high", now=NOON)
    assert res["held"] is None and res["delivered"] == ["windows"]
    # a sphere nobody knows still gets the default routes
    res = fx.send("Odd", sphere="ghost", now=NOON)
    assert res["sphere"] == "ghost" and res["delivered"] == ["windows"]


def test_the_apps_sphere_is_the_default(fx, hub):
    sp = hub.facet("spheres")
    sp.upsert({"id": "personal", "apps": ["ledger"]})
    sp.upsert({"id": "work", "apps": ["kafka"]})
    assert fx.send("Invoice", app="kafka", priority="high", now=NOON)["sphere"] == "work"
    assert fx.send("Paid", app="ledger", now=NOON)["sphere"] == "personal"
    assert fx.send("Who", app="unlisted", now=NOON)["sphere"] == sp.active()


def test_no_spheres_facet_still_works(fx, hub):
    hub._facets_by_id.pop("spheres")
    res = fx.send("Alone", app="x", now=NIGHT)                                    # no quiet hours without spheres
    assert res["held"] is None and res["sphere"] == "personal" and res["delivered"] == ["windows"]
    assert fx.routing()["personal"]["notify"]["urgent"] == ["windows", "telegram"]


def test_everything_off(fx):
    fx.update_config({"enabled": False})
    res = fx.send("Nope", now=NOON)
    assert res["ok"] and res["held"] == "disabled" and fx.senders["windows"].calls == []


def test_title_is_required_and_text_is_clipped(fx):
    res = fx.send("   ")
    assert res["ok"] is False and res["status"] == 400
    res = fx.send("t" * 500, "b" * 5000, url="javascript:alert(1)", now=NOON)
    row = fx.get_notification(res["id"])
    assert len(row["title"]) == 200 and len(row["body"]) == 2000 and row["url"] == ""
    assert clean_url("hoard://ledger/tx/1") == "hoard://ledger/tx/1" and clean_url("file:///etc/passwd") == ""


# ---- dedupe, rate ----------------------------------------------------------------------------------------------

def test_duplicates_inside_the_window_are_held(fx, hub):
    first = fx.send("Netflix charged", app="ledger", now=NOON)
    again = fx.send("Netflix charged", app="ledger", now=NOON + timedelta(minutes=5))
    assert first["held"] is None and again["held"] == "duplicate" and again["duplicate_of"] == first["id"]
    assert len(fx.senders["windows"].calls) == 1
    assert events(hub, "notify.held")[-1]["data"]["reason"] == "duplicate"
    # another app, or another title, is not a duplicate
    assert fx.send("Netflix charged", app="kafka", now=NOON)["held"] is None
    assert fx.send("Netflix charged again", app="ledger", now=NOON)["held"] is None
    # past the window (6 h) it goes out again
    assert fx.send("Netflix charged", app="ledger", now=NOON + timedelta(hours=6, minutes=1))["held"] is None


def test_dedupe_key_beats_the_title(fx):
    a = fx.send("Payment 1", app="ledger", dedupe_key="pay:42", now=NOON)
    b = fx.send("Payment 1 (retry)", app="ledger", dedupe_key="pay:42", now=NOON)
    c = fx.send("Payment 1", app="ledger", dedupe_key="pay:43", now=NOON)
    d = fx.send("Payment 1", app="ledger", now=NOON)          # no key: compared by title with keyless rows only
    assert a["held"] is None and b["held"] == "duplicate" and c["held"] is None and d["held"] is None


def test_dedupe_window_is_configurable(fx):
    fx.update_config({"dedupe_window_s": 0})
    assert fx.send("Same", now=NOON)["held"] is None and fx.send("Same", now=NOON)["held"] is None
    fx.update_config({"dedupe_window_s": 60})
    assert fx.send("Other", now=NOON)["held"] is None
    assert fx.send("Other", now=NOON + timedelta(seconds=30))["held"] == "duplicate"
    assert fx.send("Other", now=NOON + timedelta(seconds=61))["held"] is None


def test_held_duplicates_do_not_extend_the_window(fx):
    fx.send("Tick", now=NOON)
    for m in (1, 2, 3):
        assert fx.send("Tick", now=NOON + timedelta(hours=2 * m - 1, minutes=50))["held"] in ("duplicate", None)
    # the original at NOON anchors the window: at +6h01 it is free again no matter how many duplicates were held
    assert fx.send("Tick", now=NOON + timedelta(hours=6, minutes=1))["held"] is None


def test_rate_limit_holds_the_rest_and_sends_one_summary(fx, hub):
    fx.update_config({"rate_per_app": 3})
    t0 = NOON.timestamp()
    results = [fx.send(f"Job {i}", app="noisy", now=t0 + i) for i in range(6)]
    assert [r["held"] for r in results] == [None, None, None, "rate", "rate", "rate"]
    titles = fx.senders["windows"].titles
    assert titles[:3] == ["Job 0", "Job 1", "Job 2"] and len(titles) == 4            # + exactly one summary push
    assert "noisy" in titles[3]
    assert [e["data"]["reason"] for e in events(hub, "notify.held")] == ["rate", "rate", "rate"]
    summaries = fx.history(50, app="noisy", group="rate-summary")
    assert len(summaries) == 1 and summaries[0]["held"] is None
    # another app is not affected
    assert fx.send("Fine", app="quiet", now=t0 + 10)["held"] is None
    # an hour later the app may talk again
    assert fx.send("Job 7", app="noisy", now=t0 + 3700)["held"] is None


def test_channels_override_skips_routing_quiet_hours_and_rate(fx, hub):
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "22:30", "end": "08:00", "days": "daily"}})
    fx.update_config({"rate_per_app": 1})
    res = fx.send("Resumen — Personal", "body", app="hub", group="digest", channels_override=["windows", "ntfy", "pigeon", "digest"], now=NIGHT)
    assert res["held"] is None and [c["channel"] for c in res["channels"]] == ["windows", "ntfy"]
    assert fx.senders["ntfy"].titles == ["Resumen — Personal"] and fx.senders["telegram"].calls == []
    for i in range(3):
        assert fx.send(f"Digest {i}", app="hub", channels_override=["windows"], now=NIGHT)["held"] is None
    # same title again: no dedupe key, so it is sent; with a key it is deduplicated
    assert fx.send("Digest 0", app="hub", channels_override=["windows"], now=NIGHT)["held"] is None
    assert fx.send("K", app="hub", dedupe_key="k", channels_override=["windows"], now=NIGHT)["held"] is None
    assert fx.send("K2", app="hub", dedupe_key="k", channels_override=["windows"], now=NIGHT)["held"] == "duplicate"
    res = fx.send("Empty override", channels_override=[], now=NIGHT)
    assert res["held"] is None and res["channels"] == []


def test_a_failing_sender_never_breaks_send(fx):
    fx.senders["windows"].result = RuntimeError("boom")
    fx.senders["telegram"].result = {"ok": False, "error": "chat not found"}
    res = fx.send("Fire", priority="urgent", now=NOON)
    by = {c["channel"]: c for c in res["channels"]}
    assert res["ok"] and res["delivered"] == []
    assert by["windows"]["ok"] is False and by["windows"]["error"] == "RuntimeError"
    assert by["telegram"]["error"] == "chat not found"
    assert fx.get_notification(res["id"])["channels"][1]["error"] == "chat not found"


@pytest.mark.parametrize("value,ok,error", [(True, True, None), (None, True, None), (False, False, "failed"), ("", True, None),
                                            ("nope", False, "nope"), ({"ok": True, "extra": 1}, True, None), ({"error": "x"}, False, "x")])
def test_sender_results_are_normalised(fx, value, ok, error):
    fx.senders["windows"].result = value
    res = fx.send(f"N {value!r}", now=NOON)
    c = res["channels"][0]
    assert c["channel"] == "windows" and c["ok"] is ok and c.get("error") == error


def test_concurrent_identical_sends_deliver_once(fx):
    out = []
    ts = [threading.Thread(target=lambda: out.append(fx.send("Same thing", app="ledger", now=NOON))) for _ in range(12)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert sorted(r["held"] or "sent" for r in out).count("sent") == 1
    assert sum(1 for r in out if r["held"] == "duplicate") == 11 and len(fx.senders["windows"].calls) == 1


# ---- the Windows toast ----------------------------------------------------------------------------------------------

def test_toast_xml_is_escaped_and_the_link_restricted():
    ps = build_toast_ps1("Tom & <Jerry>", 'say "hi"', "http://127.0.0.1:5200/#/x?a=1&b=2")
    assert "Tom &amp; &lt;Jerry&gt;" in ps and "&quot;hi&quot;" in ps
    assert 'launch="http://127.0.0.1:5200/#/x?a=1&amp;b=2"' in ps and "activationType=\"protocol\"" in ps
    assert "launch=" not in build_toast_ps1("t", "b", "file:///c:/x") and "launch=" not in build_toast_ps1("t", "b", "hoard://a/b/1")
    assert xml_escape("a\x00b\x1fc") == "abc"
    assert "CreateToastNotifier" in ps


def test_windows_channel_is_unsupported_off_windows(hub):
    f = hub.facet("notify")
    f.platform = "linux"
    f.update_config({"channels": {"windows": {"enabled": True}}})
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "", "end": "", "days": "daily"}})
    res = f.send("Hola", now=NOON)
    c = res["channels"][0]
    assert c["channel"] == "windows" and c["ok"] is False and c["unsupported"] is True and c["error"] == "unsupported"
    assert res["ok"] and res["delivered"] == []


class _Done:
    def __init__(self, code):
        self.returncode = code


def test_windows_toast_runs_powershell_synchronously_when_asked(hub):
    f = hub.facet("notify")
    f.platform = "win32"
    f.toast_async = False
    ran = []

    def runner(cmd, **kw):
        ran.append((cmd, Path(cmd[-1]).read_text(encoding="utf-8-sig"), kw))
        return _Done(0)

    f.powershell_runner = runner
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "", "end": "", "days": "daily"}})
    res = f.send("Hola <b>", "mundo", url="http://127.0.0.1:1/z", now=NOON)
    assert res["delivered"] == ["windows"]
    cmd, script, kw = ran[0]
    assert cmd[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"] and kw["timeout"] == 20
    assert "Hola &lt;b&gt;" in script and "mundo" in script
    assert not os.path.exists(cmd[-1])                                         # the temp script is removed
    f.powershell_runner = lambda cmd, **kw: _Done(1)
    res = f.send("Hola 2", now=NOON)
    assert res["channels"][0]["ok"] is False and res["channels"][0]["error"] == "powershell exit 1"

    def boom(cmd, **kw):
        raise OSError("no powershell")
    f.powershell_runner = boom
    assert f.send("Hola 3", now=NOON)["channels"][0]["error"] == "OSError"


def test_windows_toast_in_background_records_a_late_failure(hub):
    f = hub.facet("notify")
    f.platform = "win32"
    f.toast_async = True
    gate = threading.Event()

    def runner(cmd, **kw):
        gate.wait(5)
        return _Done(7)

    f.powershell_runner = runner
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "", "end": "", "days": "daily"}})
    t0 = time.monotonic()
    res = f.send("Slow toast", now=NOON)
    assert time.monotonic() - t0 < 2 and res["channels"][0] == {**res["channels"][0], "ok": True, "async": True}
    gate.set()
    for _ in range(100):
        row = f.get_notification(res["id"])
        if row["channels"] and row["channels"][0].get("ok") is False:
            break
        time.sleep(0.05)
    assert row["channels"][0]["ok"] is False and row["channels"][0]["error"] == "powershell exit 7"


# ---- ntfy, Telegram: real HTTP against local fakes --------------------------------------------------------------------

class FakeWeb:
    def __init__(self):
        self.requests: list[dict] = []
        self.responses: dict[str, tuple[int, dict]] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                       "json": json.loads(raw) if raw else None})
                status, payload = {**{"": (200, {"ok": True})}, **outer.responses}.get(
                    next((p for p in outer.responses if self.path.startswith(p)), ""), (200, {"ok": True}))
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _handle

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def web():
    w = FakeWeb()
    yield w
    w.close()


@pytest.fixture
def real(hub):
    """The facet with its own (real) senders; only the clock and quiet hours are neutralised."""
    f = hub.facet("notify")
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "", "end": "", "days": "daily"},
                                 "notify": {"urgent": ["ntfy", "telegram"], "high": ["ntfy"], "normal": ["ntfy", "telegram"], "low": ["digest"]}})
    return f


def test_ntfy_posts_json_with_priority_click_and_token(real, web):
    real.update_config({"channels": {"ntfy": {"enabled": True, "server": web.url, "topic": "hoard_test", "token": "tk_secret_1234"}}})
    res = real.send("Disk full", "C: is at 99%", priority="urgent", url="http://127.0.0.1:5200/x", tags=["warning"], now=NOON)
    assert res["delivered"] == ["ntfy"]
    req = web.requests[0]
    assert req["method"] == "POST" and req["path"] == "/"
    assert req["json"] == {"topic": "hoard_test", "title": "Disk full", "message": "C: is at 99%", "priority": 5,
                           "tags": ["warning"], "click": "http://127.0.0.1:5200/x"}
    assert req["headers"]["Authorization"] == "Bearer tk_secret_1234"
    real.send("Plain", priority="high", now=NOON)
    assert web.requests[1]["json"]["priority"] == 4 and web.requests[1]["json"]["tags"] == ["bell"] and "click" not in web.requests[1]["json"]


def test_ntfy_errors(real, web):
    real.update_config({"channels": {"ntfy": {"enabled": True, "server": web.url, "topic": ""}}})
    res = real.send("No topic", priority="high", now=NOON)
    assert res["channels"][0]["ok"] is False and "topic" in res["channels"][0]["error"]
    real.update_config({"channels": {"ntfy": {"topic": "t1"}}})
    web.responses[""] = (503, {"error": "down"})
    web.responses["/"] = (503, {"error": "down"})
    res = real.send("Server down", priority="high", now=NOON)
    assert res["channels"][0]["error"] == "http 503"
    real.update_config({"channels": {"ntfy": {"server": "http://127.0.0.1:1"}}})
    res = real.send("Nobody home", priority="high", now=NOON)
    assert res["channels"][0]["ok"] is False and res["channels"][0]["error"]


def test_telegram_sends_html_and_scrubs_the_token(real, web):
    token = "123456:SECRET-token-ABCDE"
    real.update_config({"channels": {"telegram": {"enabled": True, "bot_token": token, "chat_id": "98765", "api_base": web.url}}})
    res = real.send("Tom & Jerry <3", "line", url="http://x.test/a?b=1&c=2", priority="urgent", now=NOON)
    assert res["delivered"] == ["telegram"]
    req = web.requests[0]
    assert req["path"] == f"/bot{token}/sendMessage"
    assert req["json"]["chat_id"] == "98765" and req["json"]["parse_mode"] == "HTML"
    assert "<b>Tom &amp; Jerry &lt;3</b>" in req["json"]["text"] and "line" in req["json"]["text"] and "http://x.test/a?b=1&amp;c=2" in req["json"]["text"]
    web.responses["/bot"] = (400, {"ok": False, "description": f"Bad Request: chat 98765 not found for {token}"})
    res = real.send("Fail", priority="urgent", now=NOON)
    err = next(c for c in res["channels"] if c["channel"] == "telegram")["error"]
    assert token not in err and "98765" not in err and "***" in err
    assert token not in json.dumps(fx_rows(real))


def fx_rows(f):
    return f.history(100)


def test_telegram_not_configured(real):
    real.update_config({"channels": {"telegram": {"enabled": True}}})
    res = real.send("X", priority="urgent", now=NOON)
    tg = next(c for c in res["channels"] if c["channel"] == "telegram")
    assert tg["ok"] is False and "not configured" in tg["error"]


def test_telegram_discovers_the_chat_id(real, web):
    real.update_config({"channels": {"telegram": {"bot_token": "tok-123456", "api_base": web.url}}})
    web.responses["/bot"] = (200, {"ok": True, "result": []})
    assert "write to the bot" in real.telegram_discover_chat_id()["error"]
    web.responses["/bot"] = (200, {"ok": True, "result": [{"message": {"chat": {"id": 1, "first_name": "Old"}}},
                                                          {"message": {"chat": {"id": 555, "first_name": "Luis", "last_name": "S"}}}]})
    res = real.telegram_discover_chat_id()
    assert res == {"ok": True, "chat_id": "555", "name": "Luis S"}
    assert web.requests[-1]["path"].startswith("/bottok-123456/getUpdates")
    real.update_config({"channels": {"telegram": {"bot_token": None}}})
    assert real.telegram_discover_chat_id()["status"] == 400


# ---- e-mail -----------------------------------------------------------------------------------------------------------

class FakeMailgate:
    def __init__(self, result=None):
        self.sent = []
        self.result = {"ok": True} if result is None else result

    def send_mail(self, subject, text, to=None, html=""):
        self.sent.append({"subject": subject, "text": text, "to": to, "html": html})
        return self.result


def _email_fx(hub, **cfg):
    f = hub.facet("notify")
    hub.facet("spheres").upsert({"id": "personal", "quiet_hours": {"start": "", "end": "", "days": "daily"},
                                 "notify": {"urgent": ["email"], "high": ["email"], "normal": ["email"], "low": ["digest"]}})
    f.update_config({"channels": {"email": {"enabled": True, **cfg}}})
    return f


def test_email_goes_through_the_mail_gateway(hub):
    f = _email_fx(hub, via="faustus", to="me@example.com, you@example.com")
    gate = FakeMailgate()
    hub._facets_by_id["mailgate"] = gate
    res = f.send("Invoice due", "Pay by Friday\nthanks", url="http://x.test/i", now=NOON)
    assert res["delivered"] == ["email"]
    sent = gate.sent[0]
    assert sent["subject"] == "Invoice due" and sent["to"] == ["me@example.com", "you@example.com"]
    assert "Pay by Friday" in sent["text"] and "http://x.test/i" in sent["text"] and "<h3>Invoice due</h3>" in sent["html"]
    f.update_config({"channels": {"email": {"to": []}}})
    f.send("No recipient configured", now=NOON)
    assert gate.sent[1]["to"] is None                                         # the gateway uses its own default
    gate.result = {"ok": False, "error": "no account"}
    res = f.send("Fail", now=NOON)
    assert res["channels"][0] == {**res["channels"][0], "ok": False, "error": "no account"}


def test_email_without_a_gateway(hub):
    f = _email_fx(hub, via="faustus")
    hub._facets_by_id.pop("mailgate", None)                                    # whatever the real facet is, here there is none
    assert hub.facet("mailgate") is None
    res = f.send("Hello", now=NOON)
    assert res["channels"][0]["ok"] is False and res["channels"][0]["error"] == "mail gateway not available"
    hub._facets_by_id["mailgate"] = object()                                   # present but without send_mail
    assert f.send("Hello 2", now=NOON)["channels"][0]["error"] == "mail gateway not available"
    st = f.channel_status()["email"]
    assert st["configured"] is False and st["detail"] == "mail gateway not available"
    hub._facets_by_id["mailgate"] = FakeMailgate()
    assert f.channel_status()["email"]["configured"] is True


class FakeSMTP:
    instances: list = []

    def __init__(self, fail=None):
        self.fail, self.logged, self.messages, self.quit_called = fail, None, [], False
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        if self.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials hunter2")
        self.logged = (user, password)

    def send_message(self, msg):
        if self.fail == "send":
            raise smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"no hunter2")})
        self.messages.append(msg)

    def quit(self):
        self.quit_called = True


def test_email_through_smtp(hub):
    f = _email_fx(hub, via="smtp", host="smtp.example.com", port=587, user="bot@example.com", password="hunter2",
                  to="me@example.com", **{"from": "Hoard <bot@example.com>"})
    FakeSMTP.instances = []
    seen_cfg = {}

    def factory(cfg):
        seen_cfg.update(cfg)
        return FakeSMTP()

    f.smtp_factory = factory
    res = f.send("Invoice due", "Pay it", url="http://x.test/i", now=NOON)
    assert res["delivered"] == ["email"]
    smtp = FakeSMTP.instances[0]
    assert smtp.logged == ("bot@example.com", "hunter2") and smtp.quit_called
    msg = smtp.messages[0]
    assert msg["Subject"] == "Invoice due" and msg["To"] == "me@example.com" and msg["From"] == "Hoard <bot@example.com>"
    assert "Pay it" in msg.get_body(("plain",)).get_content() and "<h3>" in msg.get_body(("html",)).get_content()
    assert seen_cfg["host"] == "smtp.example.com" and seen_cfg["port"] == 587 and seen_cfg["tls"] is True


def test_smtp_failures_never_leak_the_password(hub):
    f = _email_fx(hub, via="smtp", host="h", user="u", password="hunter2", to="me@example.com")
    f.smtp_factory = lambda cfg: FakeSMTP(fail="auth")
    res = f.send("A", now=NOON)
    assert res["channels"][0]["error"] == "authentication failed"
    f.smtp_factory = lambda cfg: FakeSMTP(fail="send")
    res = f.send("B", now=NOON)
    assert res["channels"][0]["ok"] is False and "hunter2" not in json.dumps(res)
    f.smtp_factory = lambda cfg: (_ for _ in ()).throw(OSError("connection refused hunter2"))
    res = f.send("C", now=NOON)
    assert res["channels"][0]["error"] == "OSError"
    f.update_config({"channels": {"email": {"host": ""}}})
    assert "not configured" in f.send("D", now=NOON)["channels"][0]["error"]


def test_default_smtp_factory_picks_ssl_starttls_or_plain(monkeypatch):
    made = []

    class S:
        def __init__(self, kind): made.append(kind); self.started = False
        def starttls(self, context=None): made.append("starttls")

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda host, port, timeout, context: S(f"ssl:{port}"))
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout: S(f"plain:{port}"))
    NotifyFacet._default_smtp({"host": "h", "port": 465, "tls": True})
    NotifyFacet._default_smtp({"host": "h", "port": 587, "tls": True})
    NotifyFacet._default_smtp({"host": "h", "port": 25, "tls": False})
    assert made == ["ssl:465", "plain:587", "starttls", "plain:25"]


# ---- config and secrets --------------------------------------------------------------------------------------------------

def test_mask_and_is_masked():
    assert mask("") == "" and mask("abc") == MASK and mask("1234567890") == MASK + "7890"
    assert is_masked(MASK + "7890") and not is_masked("plain") and not is_masked(None)


def test_config_view_masks_secrets_and_shows_status(hub):
    f = hub.facet("notify")
    f.update_config({"channels": {"ntfy": {"token": "ntfy-secret-token"}, "telegram": {"bot_token": "1234:telegram-secret", "chat_id": "42"},
                                  "email": {"password": "smtp-secret-pass", "via": "smtp", "host": "h", "to": ["a@b.c"]}}})
    view = f.config_view()
    text = json.dumps(view)
    for secret in ("ntfy-secret-token", "telegram-secret", "smtp-secret-pass"):
        assert secret not in text
    assert view["channels"]["ntfy"]["token"] == MASK + "oken" and view["channels"]["telegram"]["bot_token"].endswith("cret")
    assert view["channels"]["telegram"]["chat_id"] == "42"
    assert view["status"]["telegram"]["configured"] is True and view["status"]["email"]["detail"].startswith("SMTP h:465")
    assert set(view["routing"]) == {"personal", "work"} and view["routing"]["work"]["notify"]["normal"] == ["digest"]
    assert view["priorities"] == ["low", "normal", "high", "urgent"]
    # the secrets are on disk, in the file only the hub reads
    on_disk = json.loads((Path(hub.config.data_dir) / "notify.json").read_text(encoding="utf-8"))
    assert on_disk["channels"]["ntfy"]["token"] == "ntfy-secret-token"


def test_a_masked_or_empty_secret_keeps_the_stored_one_and_null_clears_it(hub):
    f = hub.facet("notify")
    f.update_config({"channels": {"ntfy": {"token": "ntfy-secret-token"}}})
    view = f.update_config({"channels": {"ntfy": {"token": MASK + "oken", "topic": "abc"}}})
    assert f.config()["channels"]["ntfy"]["token"] == "ntfy-secret-token" and view["channels"]["ntfy"]["topic"] == "abc"
    f.update_config({"channels": {"ntfy": {"token": ""}}})
    assert f.config()["channels"]["ntfy"]["token"] == "ntfy-secret-token"
    f.update_config({"channels": {"ntfy": {"token": "new-token-1"}}})
    assert f.config()["channels"]["ntfy"]["token"] == "new-token-1"
    view = f.update_config({"channels": {"ntfy": {"token": None}}})
    assert f.config()["channels"]["ntfy"]["token"] == "" and view["channels"]["ntfy"]["token"] == ""


def test_config_validation(hub):
    f = hub.facet("notify")
    before = f.config()
    cases = [({"enabled": "yes"}, "enabled"), ({"rate_per_app": 0}, "rate_per_app"), ({"dedupe_window_s": -1}, "dedupe_window_s"),
             ({"channels": {"pigeon": {}}}, "unknown channel"), ({"channels": {"ntfy": {"server": "ftp://x"}}}, "server"),
             ({"channels": {"ntfy": {"topic": "bad topic!"}}}, "topic"), ({"channels": {"email": {"port": 99999}}}, "port"),
             ({"channels": {"email": {"via": "carrier"}}}, "via"), ({"channels": {"email": {"tls": "yes"}}}, "tls"),
             ({"channels": {"windows": {"enabled": "maybe"}}}, "enabled"), ({"channels": []}, "channels")]
    for patch, fragment in cases:
        res = f.update_config(patch)
        assert res["ok"] is False and res["status"] == 400 and fragment in res["error"], (patch, res)
    assert f.update_config("x")["status"] == 400
    assert f.config() == before                                           # an invalid patch changes nothing


def test_config_accepts_nested_smtp_and_string_values(hub):
    f = hub.facet("notify")
    f.update_config({"channels": {"email": {"via": "smtp", "to": "a@b.c; d@e.f", "smtp": {"host": "h.example", "port": "587", "user": "u", "tls": False}}}})
    e = f.config()["channels"]["email"]
    assert e["host"] == "h.example" and e["port"] == 587 and e["to"] == ["a@b.c", "d@e.f"] and e["tls"] is False and e["via"] == "smtp"
    f.update_config({"dedupe_window_s": 3600.0, "rate_per_app": 5, "enabled": False})
    c = f.config()
    assert c["dedupe_window_s"] == 3600 and c["rate_per_app"] == 5 and c["enabled"] is False


def test_config_survives_restart_and_bad_files(hub):
    f = hub.facet("notify")
    f.update_config({"rate_per_app": 7, "channels": {"ntfy": {"topic": "mine"}}})
    g = NotifyFacet(hub)
    assert g.config()["rate_per_app"] == 7 and g.config()["channels"]["ntfy"]["topic"] == "mine"
    g.close()
    path = Path(hub.config.data_dir) / "notify.json"
    path.write_text("{ nope", encoding="utf-8")
    assert f.config()["rate_per_app"] == 20 and f.config()["enabled"] is True
    path.write_text(json.dumps({"rate_per_app": "many", "channels": {"ntfy": {"topic": 5, "bogus": 1}, "alien": {}}}), encoding="utf-8")
    assert f.config()["rate_per_app"] == 20 and f.config()["channels"]["ntfy"]["topic"] == ""


# ---- history ------------------------------------------------------------------------------------------------------------------

def test_history_filters(fx, hub):
    t0 = NOON.timestamp()
    a = fx.send("A one", app="ledger", now=t0)
    b = fx.send("B two", app="kafka", sphere="work", now=t0 + 10)
    c = fx.send("C three", app="ledger", priority="low", now=t0 + 20)
    d = fx.send("A one", app="ledger", now=t0 + 30)                                         # duplicate
    assert [r["id"] for r in fx.history()] == [d["id"], c["id"], b["id"], a["id"]]
    assert [r["id"] for r in fx.history(app="kafka")] == [b["id"]]
    assert [r["id"] for r in fx.history(sphere="work")] == [b["id"]]
    assert {r["id"] for r in fx.history(held="any")} == {b["id"], c["id"], d["id"]}
    assert [r["id"] for r in fx.history(held="none")] == [a["id"]]
    assert [r["id"] for r in fx.history(held="duplicate")] == [d["id"]]
    assert [r["id"] for r in fx.history(since=t0 + 15)] == [d["id"], c["id"]]
    assert [r["id"] for r in fx.history(since=datetime.fromtimestamp(t0 + 15).isoformat())] == [d["id"], c["id"]]
    assert [r["id"] for r in fx.history(since_id=b["id"])] == [d["id"], c["id"]]
    assert [r["id"] for r in fx.history(q="three")] == [c["id"]]
    assert len(fx.history(limit=2)) == 2
    with pytest.raises(ValueError):
        fx.history(since="yesterday-ish")
    row = fx.history(app="kafka")[0]
    assert set(row) >= {"id", "ts", "app", "sphere", "priority", "title", "body", "url", "group", "dedupe_key", "tags", "icon_app", "channels", "held", "seen"}
    assert row["icon_app"] == "kafka"


def test_unseen_counting_and_mark_read(fx):
    a = fx.send("One", now=NOON)
    b = fx.send("Two", app="x", now=NOON)
    dup = fx.send("One", now=NOON)                                                         # held duplicate: not counted
    assert dup["held"] == "duplicate" and fx.unseen_count() == 2
    assert [r["id"] for r in fx.history(unseen=True)] == [dup["id"], b["id"], a["id"]]
    assert fx.mark_read([a["id"], "junk", 999]) == 1 and fx.unseen_count() == 1
    assert fx.mark_read([]) == 0
    assert fx.mark_read(None) >= 1 and fx.unseen_count() == 0 and fx.history(unseen=True) == []


def test_history_survives_restart_and_dedupe_with_it(fx, hub):
    fx.send("Persist me", app="ledger", now=NOON)
    g = NotifyFacet(hub)
    g.senders = {c: Rec() for c in CHANNELS}
    assert g.history()[0]["title"] == "Persist me"
    assert g.send("Persist me", app="ledger", now=NOON + timedelta(minutes=1))["held"] == "duplicate"
    g.close()


def test_test_channel(fx, hub):
    res = fx.test_channel("telegram")
    assert res["ok"] and res["delivered"] is True and res["result"]["channel"] == "telegram"
    assert fx.senders["telegram"].calls[0][0]["group"] == "test"
    fx.update_config({"channels": {"ntfy": {"enabled": False}}})
    assert fx.test_channel("ntfy")["delivered"] is True                                     # ignores the enabled flag
    assert fx.test_channel("pigeon")["status"] == 400
    # a test never counts against an app's rate nor anchors a duplicate
    assert fx._pushed_last_hour("hub", time.time()) == 0
    fx.senders["windows"].result = {"ok": False, "error": "no toast"}
    assert fx.test_channel("windows")["delivered"] is False


# ---- HTTP, through the real server --------------------------------------------------------------------------------------------

@pytest.fixture
def served(hub, fx):
    app = hub.get("fake")
    Path(app.token_file).parent.mkdir(parents=True, exist_ok=True)
    Path(app.token_file).write_text("fake-app-token", encoding="utf-8")
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, hub.config.url, {"Authorization": "Bearer " + hub.token}, {"Authorization": "Bearer fake-app-token"}
    server.shutdown()


def test_http_post_uses_the_callers_identity(served, fx):
    hub, url, hubh, apph = served
    assert _http(url + "/api/notify", {"title": "x"})[0] == 401
    status, body = _http(url + "/api/notify", {"title": "Hi from fake", "body": "b", "priority": "high", "group": "g"}, headers=apph)
    assert status == 200 and body["ok"] and body["app"] == "fake" and body["delivered"] == ["windows"]
    assert fx.get_notification(body["id"])["app"] == "fake" and fx.get_notification(body["id"])["group"] == "g"
    # an app may not speak for another; naming itself is fine
    status, body = _http(url + "/api/notify", {"title": "Spoof", "app": "ledger"}, headers=apph)
    assert status == 403 and "on behalf" in body["error"]
    assert _http(url + "/api/notify", {"title": "Self", "app": "fake"}, headers=apph)[0] == 200
    # hub / ui choose the app (default hub)
    assert _http(url + "/api/notify", {"title": "From hub"}, headers=hubh)[1]["app"] == "hub"
    assert _http(url + "/api/notify", {"title": "As ledger", "app": "ledger"}, headers=hubh)[1]["app"] == "ledger"
    assert _http(url + "/api/notify", {"title": "From ui", "app": "kafka"}, headers=UI)[1]["app"] == "kafka"
    status, body = _http(url + "/api/notify", {"body": "no title"}, headers=hubh)
    assert status == 400 and "title" in body["error"]


def test_http_channels_override_is_for_hub_and_ui_only(served, fx):
    hub, url, hubh, apph = served
    status, body = _http(url + "/api/notify", {"title": "Forced", "channels": ["ntfy"]}, headers=hubh)
    assert body["delivered"] == ["ntfy"]
    status, body = _http(url + "/api/notify", {"title": "Forced 2", "channels": ["ntfy"]}, headers=apph)
    assert body["delivered"] == ["windows"]                       # an app's list is ignored: the sphere's route applies


def test_http_get_history_and_filters(served, fx):
    hub, url, hubh, apph = served
    _http(url + "/api/notify", {"title": "One", "app": "ledger"}, headers=hubh)
    _http(url + "/api/notify", {"title": "Two", "app": "kafka", "priority": "low"}, headers=hubh)
    status, body = _http(url + "/api/notify")
    assert status == 200 and body["ok"] and [n["title"] for n in body["notifications"]] == ["Two", "One"] and body["unseen"] == 2
    assert [n["title"] for n in _http(url + "/api/notify?app=ledger")[1]["notifications"]] == ["One"]
    assert [n["title"] for n in _http(url + "/api/notify?held=any")[1]["notifications"]] == ["Two"]
    assert [n["title"] for n in _http(url + "/api/notify?held=none&limit=5")[1]["notifications"]] == ["One"]
    assert [n["title"] for n in _http(url + "/api/notify?sphere=personal&q=Tw")[1]["notifications"]] == ["Two"]
    assert [n["title"] for n in _http(url + "/api/notify?since=0")[1]["notifications"]] == ["Two", "One"]
    assert _http(url + "/api/notify?since=not-a-time")[0] == 400
    assert len(_http(url + "/api/notify?unseen=1")[1]["notifications"]) == 2


def test_http_read(served, fx):
    hub, url, hubh, apph = served
    ids = [_http(url + "/api/notify", {"title": f"N{i}", "app": "a"}, headers=hubh)[1]["id"] for i in range(3)]
    assert _http(url + "/api/notify/read", {"ids": ids[:1]})[0] == 401
    assert _http(url + "/api/notify/read", {"ids": ids[:1]}, headers=apph)[0] == 403
    status, body = _http(url + "/api/notify/read", {"ids": ids[:1]}, headers=UI)
    assert status == 200 and body == {"ok": True, "marked": 1, "unseen": 2}
    status, body = _http(url + "/api/notify/read", {"all": True}, headers=hubh)
    assert body["marked"] == 2 and body["unseen"] == 0
    assert _http(url + "/api/notify/read", {}, headers=hubh)[0] == 400


def test_http_config_never_returns_secrets(served, fx):
    hub, url, hubh, apph = served
    patch = {"channels": {"telegram": {"bot_token": "9999:very-secret-bot", "chat_id": "7"}, "ntfy": {"token": "ntfy-very-secret"},
                          "email": {"password": "mail-very-secret"}}}
    assert _http(url + "/api/notify/config", patch)[0] == 401
    assert _http(url + "/api/notify/config", patch, headers=apph)[0] == 403
    status, body = _http(url + "/api/notify/config", patch, headers=hubh)
    assert status == 200 and body["ok"]
    for text in (json.dumps(body), json.dumps(_http(url + "/api/notify/config")[1]), json.dumps(_http(url + "/api/notify")[1])):
        for secret in ("very-secret", "9999:"):
            assert secret not in text
    got = _http(url + "/api/notify/config")[1]
    assert got["channels"]["telegram"]["bot_token"] == MASK + "bot" if len("9999:very-secret-bot") < 8 else got["channels"]["telegram"]["bot_token"].startswith(MASK)
    # saving the masked view back (what the page does) keeps the secrets
    status, body = _http(url + "/api/notify/config", {"channels": got["channels"]}, headers=UI)
    assert status == 200 and fx.config()["channels"]["telegram"]["bot_token"] == "9999:very-secret-bot"
    status, body = _http(url + "/api/notify/config", {"rate_per_app": "lots"}, headers=hubh)
    assert status == 400 and "rate_per_app" in body["error"]


def test_http_test_and_discover(served, fx):
    hub, url, hubh, apph = served
    assert _http(url + "/api/notify/test", {"channel": "windows"}, headers=apph)[0] == 403
    status, body = _http(url + "/api/notify/test", {"channel": "windows"}, headers=hubh)
    assert status == 200 and body["delivered"] is True and body["result"]["channel"] == "windows"
    assert _http(url + "/api/notify/test", {"channel": "pigeon"}, headers=hubh)[0] == 400
    status, body = _http(url + "/api/notify/telegram/discover", {}, headers=hubh)
    assert status == 400 and "token" in body["error"]
    assert _http(url + "/api/notify/nothing", {}, headers=hubh)[0] == 404


def test_the_ui_script_is_served_and_listed(served):
    hub, url, hubh, apph = served
    body = _http(url + "/api/facets")[1]
    assert next(f for f in body["facets"] if f["id"] == "notify")["ui_scripts"] == ["notify.js"]
    import urllib.request
    js = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + "/ui/notify.js", timeout=10).read().decode("utf-8")
    assert 'H.register("notify"' in js and 'placement: "tab"' in js


# ---- tools -------------------------------------------------------------------------------------------------------------------------

def test_tool_descriptions():
    cat = {t["name"]: t for t in NotifyFacet.tools()}
    assert set(cat) == {"hub_notify", "hub_notify_history"}
    for t in cat.values():
        assert len(t["description"].split("\n")[0]) <= 110, t["name"]
    assert "avísame" in cat["hub_notify"]["description"] and "annotations" not in cat["hub_notify"]
    assert cat["hub_notify_history"]["annotations"] == {"readOnlyHint": True}
    assert cat["hub_notify"]["inputSchema"]["required"] == ["title"]
    assert {t["name"] for t in tools.all_tools()} >= {"hub_notify", "hub_notify_history"}


def test_tools_send_and_read_history(fx, hub):
    res = tools.call(hub, "hub_notify", {"title": "Remember the milk", "body": "2 L", "priority": "high", "group": "reminder"})
    assert res["ok"] and res["app"] == "hub" and res["delivered"] == ["windows"]
    res = tools.call(hub, "hub_notify", {"title": "By ntfy", "channels": ["ntfy"]})
    assert res["delivered"] == ["ntfy"]
    assert tools.call(hub, "hub_notify", {"body": "x"})["ok"] is False
    hist = tools.call(hub, "hub_notify_history", {"limit": 5})
    assert hist["ok"] and [n["title"] for n in hist["notifications"]][:2] == ["By ntfy", "Remember the milk"] and hist["unseen"] >= 2
    assert [n["title"] for n in tools.call(hub, "hub_notify_history", {"q": "milk"})["notifications"]] == ["Remember the milk"]
    assert tools.call(hub, "hub_notify_history", {"held": "none", "app": "hub"})["ok"]


def test_tools_over_agent_call(served, fx):
    hub, url, hubh, apph = served
    status, body = _http(url + "/api/agent/call", {"name": "hub_notify", "arguments": {"title": "Via MCP", "priority": "urgent"}}, headers=hubh)
    assert status == 200 and body["result"]["delivered"] == ["windows", "telegram"]
    status, body = _http(url + "/api/agent/call", {"name": "hub_notify_history", "arguments": {}}, headers=hubh)
    assert body["result"]["notifications"][0]["title"] == "Via MCP"
