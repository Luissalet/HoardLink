"""Today, the agenda fan-out, the digest, its jobs and the family calendar (facet ``today``).

Deterministic: the clock is injected, the apps are local http.server threads answering
``/api/family/agenda`` with a token, the other facets (spheres, notify, mail, chats, worktrack) are fakes,
and the language model is an injected callable (or a fake ``Link``)."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from hoard_link.hub import facets as hub_facets, tools as hub_tools
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server
from hoard_link.hub import today as today_mod
from hoard_link.hub.today import TodayFacet, build_ics, ics_escape, ics_fold

from .conftest import free_port, write_manifest

TZ = timezone(timedelta(hours=2))
NOW_DT = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)          # a Monday
NOW = NOW_DT.timestamp()
TOKEN = "kafka-secret"


# ---- fakes -----------------------------------------------------------------------------

class AgendaApp:
    """A family app: /api/health plus /api/family/agenda behind its bearer token."""

    def __init__(self, app_id: str, items=None, mode: str = "ok", token: str = TOKEN):
        self.app_id, self.items, self.mode, self.token = app_id, items or [], mode, token
        self.calls: list[dict] = []
        self.port = free_port()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D102
                pass

            def _send(self, status, payload, ctype="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                u = urlsplit(self.path)
                if u.path == "/api/health":
                    return self._send(200, {"ok": True, "service": f"{outer.app_id}-hoard"})
                if u.path != "/api/family/agenda":
                    return self._send(404, {"error": "not found"})
                outer.calls.append({"query": {k: v[0] for k, v in parse_qs(u.query).items()},
                                    "auth": self.headers.get("Authorization", "")})
                if outer.mode == "404":
                    return self._send(404, {"error": "not found"})
                if outer.mode == "html":
                    return self._send(200, b"<html>spa</html>", "text/html")
                if self.headers.get("Authorization", "") != "Bearer " + outer.token:
                    return self._send(401, {"ok": False, "error": "token"})
                if outer.mode == "error":
                    return self._send(200, {"ok": False, "error": "db locked", "items": []})
                return self._send(200, {"ok": True, "items": outer.items})

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeSpheres:
    def __init__(self):
        self.active_id = "personal"
        self._list = [
            {"id": "personal", "name": {"es": "Personal", "en": "Personal"}, "apps": ["*"],
             "digest": {"enabled": True, "at": "08:30", "days": "daily", "summarize": True, "channels": ["windows"]},
             "notify": {"normal": ["windows"]}},
            {"id": "work", "name": {"es": "Trabajo", "en": "Work"}, "apps": ["kafka"],
             "digest": {"enabled": True, "at": "08:45", "days": "weekdays", "summarize": False, "channels": ["telegram"]},
             "notify": {"normal": ["digest"]}},
        ]

    def active(self):
        return self.active_id

    def list(self):
        return json.loads(json.dumps(self._list))

    def get(self, sid):
        return next((json.loads(json.dumps(s)) for s in self._list if s["id"] == sid), None)

    def app_allowed(self, sphere, app):
        s = self.get(sphere)
        return bool(s) and ("*" in s["apps"] or app in s["apps"])


class FakeNotify:
    def __init__(self):
        self.calls: list[dict] = []

    def send(self, title, body="", *, app="hub", sphere=None, priority="normal", url="", group="", dedupe_key="",
             channels_override=None):
        self.calls.append({"title": title, "body": body, "app": app, "sphere": sphere, "priority": priority, "url": url,
                           "group": group, "dedupe_key": dedupe_key, "channels_override": channels_override})
        return {"ok": True, "id": len(self.calls), "held": None}


class OldNotify:
    """A notify facet without channel overrides."""

    def __init__(self):
        self.calls: list[dict] = []

    def send(self, title, body="", *, app="hub", sphere=None, priority="normal", url="", group="", dedupe_key=""):
        self.calls.append({"priority": priority, "group": group, "sphere": sphere})
        return {"ok": True, "held": None}


class FakeMail:
    def __init__(self):
        self.calls = []

    def attention(self, sphere=None, days=7, limit=50, kind="mail"):
        self.calls.append((sphere, days, limit, kind))
        if kind == "chat":
            return [{"id": 9, "kind": "chat", "source": "slack", "from": "bob", "text": "ping " * 100, "priority": "attention"}]
        return [{"id": 1, "kind": "mail", "from_name": "Ana", "from_addr": "ana@x.org", "subject": "Contract to sign",
                 "snippet": "please", "text": "SECRET BODY", "links": ["http://x"], "priority": "attention"}]


class FakeChats:
    def attention(self, sphere=None, days=7, limit=50):
        return [{"id": 20, "kind": "chat", "channel": "#ops", "from": "carol", "text": "deploy is red"}]


class FakeWork:
    def active(self):
        return [{"job_id": "j1", "app": "kafka", "title": "Indexing", "progress": 0.4, "kind": "index"},
                {"job_id": "j2", "app": "ledger", "title": "Import", "progress": 0.9, "kind": "import"}]


def deadline(i, title, start, **kw):
    return {"id": f"kafka:deadline:{i}", "title": title, "start": start, "kind": "deadline", "url": f"http://127.0.0.1:5200/#/{i}", **kw}


KAFKA_ITEMS = [
    deadline(1, "Late tax form", "2026-10-01", priority="high"),                       # overdue
    {"id": "kafka:birthday:2", "title": "Mum's birthday", "start": "2026-10-02", "kind": "birthday"},   # past, not overdue
    deadline(3, "Pay rent", "2026-10-05"),                                              # today, all day
    deadline(4, "Call the plumber", "2026-10-05T09:30:00+02:00", priority="urgent"),    # today, timed (already passed)
    deadline(5, "Dentist", "2026-10-06T12:00:00+02:00"),                                # tomorrow
    deadline(6, "Passport renewal", "2026-10-09T18:00:00+02:00"),                       # this week
    deadline(7, "Far away", "2026-11-20"),                                              # outside the week
    {"title": "no start"},                                                              # dropped by the normaliser
    deadline(8, "Work review", "2026-10-07", sphere="work"),
]
LEDGER_ITEMS = [{"id": "ledger:renewal:3", "title": "Netflix renewal", "start": "2026-10-06", "kind": "renewal", "priority": "low"}]


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "apps"
    apps = {"kafka": AgendaApp("kafka", KAFKA_ITEMS), "ledger": AgendaApp("ledger", LEDGER_ITEMS),
            "plain": AgendaApp("plain", mode="404")}
    for app_id, srv in apps.items():
        folder = write_manifest(root / f"{app_id}-folder", app_id, srv.port, extra={"x-family": {"agenda": True}} if app_id != "plain" else None)
        (folder / "data").mkdir()
        (folder / "data" / "mcp-token").write_text(TOKEN, encoding="utf-8")
    # an app that is not running (its port is closed)
    dead_port = free_port()
    folder = write_manifest(root / "dead-folder", "dead", dead_port, extra={"x-family": {"agenda": True}})
    (folder / "data").mkdir()
    (folder / "data" / "mcp-token").write_text(TOKEN, encoding="utf-8")
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(root)], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False, language="es")
    hub = Hub(cfg)
    hub_facets.close_all(hub.facets)         # the facets the hub loaded by itself: tests use fakes
    hub.facets, hub._facets_by_id = [], {}
    hub.jobs.jobs.clear()                    # ... and whatever their start() left behind (digest jobs, today.json)
    hub.jobs._save()
    (Path(cfg.data_dir) / "today.json").unlink(missing_ok=True)
    clock = {"now": NOW}
    spheres, notify = FakeSpheres(), FakeNotify()
    hub._facets_by_id = {"spheres": spheres, "notify": notify, "mailgate": FakeMail(), "worktrack": FakeWork()}
    facet = TodayFacet(hub, clock=lambda: clock["now"], tz=TZ, background=False)
    hub.facets = [facet]
    hub._facets_by_id["today"] = facet
    ns = type("Env", (), {})()
    ns.hub, ns.facet, ns.apps, ns.clock, ns.spheres, ns.notify, ns.tmp = hub, facet, apps, clock, spheres, notify, tmp_path
    yield ns
    facet.close()
    hub.close()
    for srv in apps.values():
        srv.close()


def ids(items):
    return [i["id"] for i in items]


# ---- ICS ---------------------------------------------------------------------------------

def unfold(text: str) -> list[str]:
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    lines = text[:-2].split("\r\n")
    out: list[str] = []
    for line in lines:
        assert len(line.encode("utf-8")) <= 75, line
        if line.startswith(" "):
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def test_ics_escape_and_fold():
    assert ics_escape("a,b;c\\d\nline2\r\nline3") == "a\\,b\\;c\\\\d\\nline2\\nline3"
    assert ics_escape("tab\x07bell") == "tabbell"
    long = "Ñandú — " + "é" * 120 + ", fin; ok"
    folded = ics_fold("SUMMARY:" + ics_escape(long))
    parts = folded.split("\r\n")
    assert len(parts) > 2 and all(len(p.encode("utf-8")) <= 75 for p in parts) and all(p.startswith(" ") for p in parts[1:])
    assert "".join([parts[0]] + [p[1:] for p in parts[1:]]) == "SUMMARY:" + ics_escape(long)   # no multibyte char was cut
    assert ics_fold("short") == "short"
    assert ics_fold("x" * 75) == "x" * 75 and ics_fold("x" * 76) == "x" * 75 + "\r\n x"


def test_build_ics_is_valid_rfc5545():
    items = [
        {"id": "kafka:deadline:3", "title": "Pay rent, again; now", "start": "2026-10-05", "all_day": True, "kind": "deadline",
         "priority": "high", "url": "http://127.0.0.1:5200/#/3", "detail": "line one\nline two, with; punctuation\\",
         "app_name": "Kafka's Hoard"},
        {"id": "ledger:tx 4", "title": "Call", "start": "2026-10-05T09:30:00+02:00", "end": "2026-10-05T10:15:00+02:00",
         "all_day": False, "kind": "other", "priority": "normal", "app_name": "Ledger"},
        {"id": "x:3", "title": "Floating " + "long " * 30, "start": "2026-10-06T08:00:00", "all_day": False, "kind": "exam",
         "priority": "low", "app_name": ""},
        {"id": "x:4", "title": "Trip", "start": "2026-10-08", "end": "2026-10-10", "all_day": True, "kind": "other",
         "priority": "urgent", "app_name": "Kafka's Hoard"},
        {"id": "bad", "title": "no date", "start": "soon"},
    ]
    text = build_ics(items, name="Hoard — Personal, all", now=NOW_DT)
    lines = unfold(text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in lines and any(l.startswith("PRODID:") for l in lines)
    assert "X-WR-CALNAME:Hoard — Personal\\, all" in lines
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 4            # the unusable item is skipped
    ev1 = lines[lines.index("BEGIN:VEVENT"):lines.index("END:VEVENT")]
    assert "UID:kafka:deadline:3@hoard" in ev1
    assert "DTSTAMP:20261005T080000Z" in ev1                                       # 10:00 +02:00
    assert "DTSTART;VALUE=DATE:20261005" in ev1 and "DTEND;VALUE=DATE:20261006" in ev1 and "TRANSP:TRANSPARENT" in ev1
    assert "SUMMARY:Pay rent\\, again\\; now (Kafka's Hoard)" in ev1
    assert "DESCRIPTION:line one\\nline two\\, with\\; punctuation\\\\\\nKafka's Hoard\\ndeadline" in ev1
    assert "URL:http://127.0.0.1:5200/#/3" in ev1 and "CATEGORIES:DEADLINE" in ev1 and "PRIORITY:3" in ev1
    assert "UID:ledger:tx_4@hoard" in lines                                         # whitespace never reaches a UID
    assert "DTSTART:20261005T073000Z" in lines and "DTEND:20261005T081500Z" in lines   # aware → UTC
    assert "DTSTART:20261006T080000" in lines                                       # naive → floating, no Z
    assert "DTSTART;VALUE=DATE:20261008" in lines and "DTEND;VALUE=DATE:20261011" in lines   # all-day end is exclusive
    assert any(l.startswith("SUMMARY:Floating long long") and l.endswith("long") for l in lines)   # folded line, unfolded back
    assert "PRIORITY:1" in lines and "PRIORITY:9" in lines


# ---- language ------------------------------------------------------------------------------

def test_language_resolution(env, monkeypatch):
    import locale
    f = env.facet
    assert f.lang() == "es"                                           # config says es
    env.hub.config.language = "en"
    assert f.lang() == "en"
    env.hub.config.language = "auto"
    for k in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(locale, "getlocale", lambda *a: (None, None))
    assert f.lang() == "es"                                           # on doubt: Spanish
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert f.lang() == "en"
    monkeypatch.setenv("LANG", "es_ES.UTF-8")
    assert f.lang() == "es"
    monkeypatch.setenv("LANG", "C.UTF-8")
    assert f.lang() == "es"


# ---- agenda ----------------------------------------------------------------------------------

def test_agenda_fans_out_with_each_apps_token(env):
    res = env.facet.agenda("2026-10-01", "2026-10-31", None)
    assert res["errors"] == []
    assert ids(res["items"]) == ["kafka:deadline:1", "kafka:birthday:2", "kafka:deadline:3", "kafka:deadline:4",
                                 "ledger:renewal:3", "kafka:deadline:5", "kafka:deadline:8", "kafka:deadline:6"]
    first = res["items"][0]
    assert first["app"] == "kafka" and first["app_name"] == "Kafka's Hoard" and first["sphere"] == "personal"
    # sorted by day, all-day before timed within a day, then priority
    assert [i["title"] for i in res["items"] if i["start"].startswith("2026-10-05")] == ["Pay rent", "Call the plumber"]
    kafka, ledger = env.apps["kafka"], env.apps["ledger"]
    assert len(kafka.calls) == 1 and kafka.calls[0]["auth"] == "Bearer " + TOKEN
    assert kafka.calls[0]["query"] == {"from": "2026-10-01", "to": "2026-10-31"}      # no sphere param when asking for everything
    assert len(ledger.calls) == 1
    by_app = {a["app"]: a for a in res["apps"]}
    assert by_app["kafka"]["items"] == 8 and by_app["dead"]["skipped"] == "not running"
    assert "error" not in by_app["dead"]                                              # a stopped app is not an error
    assert by_app["plain"]["skipped"] == "no agenda route"


def test_agenda_sphere_filters_apps_and_items(env):
    work = env.facet.agenda("2026-10-01", "2026-10-31", "work")
    # work only allows kafka: ledger is not even asked; items carrying another sphere are dropped, unlabelled ones inherit it
    assert env.apps["ledger"].calls == []
    assert env.apps["kafka"].calls[-1]["query"]["sphere"] == "work"
    assert {i["sphere"] for i in work["items"]} == {"work"}
    assert "kafka:deadline:8" in ids(work["items"])
    personal = env.facet.agenda("2026-10-01", "2026-10-31", "personal")
    assert "kafka:deadline:8" not in ids(personal["items"])                           # explicitly a work item
    assert "ledger:renewal:3" in ids(personal["items"])


def test_agenda_window_filter_and_multi_day_overlap(env):
    env.apps["kafka"].items = [deadline(1, "Trip", "2026-09-28", end="2026-10-02"), deadline(2, "Inside", "2026-10-05")]
    res = env.facet.agenda("2026-10-01", "2026-10-31", None, refresh=True)
    assert "kafka:deadline:1" in ids(res["items"])                                     # starts before the window, ends inside
    res = env.facet.agenda("2026-10-03", "2026-10-31", None, refresh=True)
    assert "kafka:deadline:1" not in ids(res["items"])


def test_agenda_cache_60s_and_refresh(env):
    f, kafka = env.facet, env.apps["kafka"]
    f.agenda("2026-10-01", "2026-10-31", None)
    f.agenda("2026-10-01", "2026-10-31", None)
    assert len(kafka.calls) == 1                                                       # cached
    f.agenda("2026-10-01", "2026-10-30", None)
    assert len(kafka.calls) == 2                                                       # another window: another key
    env.clock["now"] += 59
    f.agenda("2026-10-01", "2026-10-31", None)
    assert len(kafka.calls) == 2
    env.clock["now"] += 2
    f.agenda("2026-10-01", "2026-10-31", None)
    assert len(kafka.calls) == 3                                                       # 61 s later: asked again
    f.agenda("2026-10-01", "2026-10-31", None, refresh=True)
    assert len(kafka.calls) == 4


def test_agenda_remembers_unsupported_apps_for_an_hour(env):
    plain = env.apps["plain"]
    env.facet.agenda("2026-10-01", "2026-10-31", None)
    assert len(plain.calls) == 1                                                       # an app without the flag is probed once
    env.facet.agenda("2026-10-02", "2026-10-31", None)
    assert len(plain.calls) == 1                                                       # the 404 is remembered
    env.clock["now"] += 3599
    env.facet.agenda("2026-10-03", "2026-10-31", None)
    assert len(plain.calls) == 1
    env.clock["now"] += 2
    env.facet.agenda("2026-10-04", "2026-10-31", None)
    assert len(plain.calls) == 2                                                       # asked again after the hour


def test_agenda_spa_catch_all_counts_as_unsupported(env):
    env.apps["plain"].mode = "html"                                                    # 200 with an HTML page
    env.facet.agenda("2026-10-01", "2026-10-31", None)
    env.facet.agenda("2026-10-02", "2026-10-31", None)
    assert len(env.apps["plain"].calls) == 1


def test_agenda_reports_real_failures_but_keeps_the_rest(env):
    env.apps["ledger"].mode = "error"
    env.apps["kafka"].token = "something-else"                                         # the hub's token no longer matches
    res = env.facet.agenda("2026-10-01", "2026-10-31", None)
    errs = {e["app"]: e["error"] for e in res["errors"]}
    assert "db locked" in errs["ledger"] and "refused the token" in errs["kafka"]
    assert res["items"] == []
    env.apps["kafka"].token = TOKEN
    res = env.facet.agenda("2026-10-01", "2026-10-31", None, refresh=True)
    assert "kafka:deadline:3" in ids(res["items"]) and [e["app"] for e in res["errors"]] == ["ledger"]


def test_agenda_manifest_can_opt_out(env):
    app = env.hub.get("ledger")
    app.family["agenda"] = False
    env.facet.agenda("2026-10-01", "2026-10-31", None)
    assert env.apps["ledger"].calls == []


def test_parse_day_words():
    today = datetime(2026, 10, 5).date()
    p = today_mod._parse_day
    assert p("today", today) == today and p("hoy", today) == today
    assert p("tomorrow", today).isoformat() == "2026-10-06" and p("mañana", today).isoformat() == "2026-10-06"
    assert p("+7d", today).isoformat() == "2026-10-12" and p("-3", today).isoformat() == "2026-10-02"
    assert p("2026-12-25", today).isoformat() == "2026-12-25"
    assert p("banana", today, today) == today and p("", today) is None


# ---- today -----------------------------------------------------------------------------------

def emit(env, type_, data, ts=None, source="hub"):
    return env.hub.events.emit(type_, data, source=source, ts=ts or NOW - 600)


def test_today_buckets(env):
    t = env.facet.today("personal")
    assert t["ok"] and t["date"] == "2026-10-05" and t["sphere"] == "personal"
    ag = t["agenda"]
    assert [i["title"] for i in ag["overdue"]] == ["Late tax form"]                    # the birthday is history, not overdue
    assert [i["title"] for i in ag["today"]] == ["Pay rent", "Call the plumber"]       # a timed item earlier today stays in today
    assert [i["title"] for i in ag["tomorrow"]] == ["Netflix renewal", "Dentist"]      # all-day first
    assert [i["title"] for i in ag["week"]] == ["Passport renewal"]                   # Far away (Nov) is out
    json.dumps(t)                                                                       # the payload is plain JSON


def test_today_defaults_to_the_active_sphere_and_all(env):
    assert env.facet.today()["sphere"] == "personal"
    env.spheres.active_id = "work"
    t = env.facet.today()
    assert t["sphere"] == "work" and [i["title"] for i in t["agenda"]["week"]] == ["Work review", "Passport renewal"]
    assert env.facet.today("all")["sphere"] == "all"
    assert "Work review" in [i["title"] for i in env.facet.today("all")["agenda"]["week"]]


def test_today_attention_from_mailgate_and_chats(env):
    t = env.facet.today("personal")
    mail, chats = t["attention"]["mail"], t["attention"]["chats"]
    assert [m["subject"] for m in mail] == ["Contract to sign"] and "links" not in mail[0] and len(mail[0]["text"]) <= 300
    assert len(chats) == 1 and chats[0]["kind"] == "chat" and len(chats[0]["text"]) <= 300   # chat rows come from the gateway
    fm = env.hub._facets_by_id["mailgate"]
    assert fm.calls == [("personal", 7, 50, "mail"), ("personal", 7, 50, "chat")]
    # a chats facet that offers attention() answers for chats; the gateway is then asked for mail only
    env.hub._facets_by_id["chats"] = FakeChats()
    fm.calls.clear()
    t = env.facet.today("personal")
    assert [c["channel"] for c in t["attention"]["chats"]] == ["#ops"] and fm.calls == [("personal", 7, 50, "mail")]
    # nothing available → empty, no error
    env.hub._facets_by_id.pop("mailgate")
    env.hub._facets_by_id.pop("chats")
    t = env.facet.today("personal")
    assert t["attention"] == {"mail": [], "chats": []} and "errors" not in t


def test_today_attention_failure_is_reported_not_raised(env):
    class Broken:
        def attention(self, *a, **k):
            raise RuntimeError("mail.db locked")

    env.hub._facets_by_id["mailgate"] = Broken()
    t = env.facet.today("personal")
    assert t["attention"] == {"mail": [], "chats": []} and "mail.db locked" in t["errors"]["attention"]
    assert t["agenda"]["today"]                                                         # the rest still works


def test_today_news_since_last_digest_and_sphere_rules(env):
    emit(env, "digest.item", {"title": "Restock: GPU", "url": "http://x/1", "watch": "tantalus", "kind": "restock"}, ts=NOW - 3600)
    emit(env, "digest.item", {"title": "Work news", "sphere": "work"}, ts=NOW - 3500)
    emit(env, "digest.item", {"title": "Restock: GPU", "url": "http://x/1", "watch": "tantalus"}, ts=NOW - 3400)   # duplicate
    emit(env, "digest.item", {"title": "Too old"}, ts=NOW - 30 * 3600)
    personal = env.facet.today("personal")["news"]
    assert [n["title"] for n in personal] == ["Restock: GPU"]                           # no sphere → personal; dupes folded; >24 h out
    assert [n["title"] for n in env.facet.today("work")["news"]] == ["Work news"]
    assert {n["title"] for n in env.facet.today("all")["news"]} == {"Restock: GPU", "Work news"}
    # after a digest was sent for personal, only newer items count
    env.facet._cfg["last_digest"]["personal"] = {"ts": NOW - 3300, "date": "2026-10-05"}
    emit(env, "digest.item", {"title": "Fresh"}, ts=NOW - 100)
    assert [n["title"] for n in env.facet.today("personal")["news"]] == ["Fresh"]
    assert [n["title"] for n in env.facet.today("work")["news"]] == ["Work news"]      # work has had no digest: still the last 24 h


def test_today_system_incidents_and_jobs(env):
    emit(env, "cassandra.incident.opened", {"incident_id": "i1", "app": "kafka", "service_kind": "app", "to_state": "down",
                                              "probable_cause": "port closed"}, ts=NOW - 7200, source="cassandra")
    emit(env, "cassandra.incident.opened", {"incident_id": "i2", "app": "ledger", "to_state": "down"}, ts=NOW - 7000, source="cassandra")
    emit(env, "cassandra.incident.closed", {"incident_id": "i2", "app": "ledger"}, ts=NOW - 6000, source="cassandra")
    emit(env, "cassandra.incident.opened", {"incident_id": "i3", "app": "ledger", "to_state": "degraded"}, ts=NOW - 5000, source="cassandra")
    emit(env, "cassandra.incident.opened", {"incident_id": "old", "app": "kafka"}, ts=NOW - 9 * 86400, source="cassandra")   # out of the window
    for i in range(30):                                                                  # noise must not push the incidents out
        emit(env, "agent.call", {"tool": "x"}, ts=NOW - 4000 + i)
    sysd = env.facet.today("personal")["system"]
    assert [i["incident_id"] for i in sysd["incidents"]] == ["i3", "i1"]                # newest first; i2 was closed
    assert sysd["incidents"][1]["probable_cause"] == "port closed"
    assert [j["job_id"] for j in sysd["jobs"]] == ["j1", "j2"]
    work = env.facet.today("work")["system"]
    assert [i["incident_id"] for i in work["incidents"]] == ["i1"]                      # ledger is not part of work
    assert [j["job_id"] for j in work["jobs"]] == ["j1"]
    # a later incident re-opened with the same id after a close counts again
    emit(env, "cassandra.incident.opened", {"incident_id": "i2", "app": "ledger"}, ts=NOW - 10, source="cassandra")
    assert "i2" in [i["incident_id"] for i in env.facet.today("personal")["system"]["incidents"]]


def test_today_prefers_cassandras_open_incidents(env, monkeypatch):
    emit(env, "cassandra.incident.opened", {"incident_id": "stale", "app": "kafka"}, ts=NOW - 3600, source="cassandra")
    calls = []

    def fake_call(app_id, tool, args, caller="hub", timeout=0):
        calls.append((app_id, tool, args))
        return {"ok": True, "result": {"ok": True, "result": {"incidents": [
            {"id": 7, "service": "ledger", "kind": "app", "opened": "2026-10-05T08:00:00+02:00", "change": "up → down",
             "probable_cause": "process gone"},
            {"id": 3, "service": "babel", "kind": "app", "opened": "2026-10-01T08:00:00+02:00", "change": "up → down"}]}}}

    monkeypatch.setattr(env.facet, "_running", lambda app_id: True)
    monkeypatch.setattr(env.hub, "get", lambda app_id: object())
    monkeypatch.setattr(env.hub, "call_app", fake_call)
    inc = env.facet.today("personal")["system"]["incidents"]
    # the bus's stale one is ignored, and so is an app down for days (a state, not news)
    assert [(i["incident_id"], i["app"], i["to_state"]) for i in inc] == [("7", "ledger", "down")]
    assert calls == [("cassandra", "svc_incidents", {"open_only": True, "limit": 50})]
    env.facet.today("personal")
    assert len(calls) == 1                                                                         # cached for a minute


def test_today_notifications_today(env):
    emit(env, "notify.sent", {"id": 1, "app": "ledger", "sphere": "personal", "priority": "high", "title": "Payment failed",
                              "channels": ["windows"]}, ts=NOW - 600)
    emit(env, "notify.held", {"id": 2, "app": "links", "sphere": "personal", "reason": "quiet", "title": "New paper"}, ts=NOW - 300)
    emit(env, "notify.sent", {"id": 3, "app": "kafka", "sphere": "work", "title": "Work thing"}, ts=NOW - 200)
    emit(env, "notify.sent", {"id": 4, "app": "ledger", "sphere": "personal", "title": "Yesterday"}, ts=NOW - 86400)
    emit(env, "notify.test", {"id": 5, "sphere": "personal", "title": "ignored type"}, ts=NOW - 100)
    n = env.facet.today("personal")["notifications"]
    assert [(x["kind"], x["title"]) for x in n] == [("held", "New paper"), ("sent", "Payment failed")]     # newest first
    assert {x["reason"] for x in n} == {"", "quiet"}
    assert [x["title"] for x in env.facet.today("work")["notifications"]] == ["Work thing"]


def test_today_works_without_any_other_facet(env):
    env.hub._facets_by_id = {"today": env.facet}
    t = env.facet.today()
    assert t["sphere"] == "all" and t["attention"] == {"mail": [], "chats": []} and t["system"] == {"incidents": [], "jobs": []}
    assert [i["id"] for i in t["agenda"]["today"]] == ["kafka:deadline:3", "kafka:deadline:4"]
    assert env.facet.ics("all").startswith("BEGIN:VCALENDAR")


# ---- digest -----------------------------------------------------------------------------------

def test_compose_digest_spanish_sections(env):
    emit(env, "digest.item", {"title": "Restock: GPU", "watch": "tantalus"}, ts=NOW - 3600)
    emit(env, "cassandra.incident.opened", {"incident_id": "i1", "app": "kafka", "to_state": "down", "probable_cause": "port closed"},
         ts=NOW - 7200, source="cassandra")
    emit(env, "notify.held", {"id": 2, "app": "links", "sphere": "personal", "reason": "quiet", "title": "x"}, ts=NOW - 300)
    d = env.facet.compose_digest("personal", summarize=False)
    md = d["markdown"]
    assert d["title"] == "Resumen — Personal — lunes, 5 de octubre de 2026" and d["lang"] == "es"
    assert [l for l in md.splitlines() if l.startswith("## ")] == ["## Hoy", "## Atención", "## Novedades", "## Sistema"]
    assert "**Vencido**" in md and "- jue 1 · Late tax form · Kafka's Hoard [alta]" in md
    assert "- 09:30 · Call the plumber · Kafka's Hoard [urgente]" in md and "- Pay rent · Kafka's Hoard" in md
    assert "**Mañana**" in md and "**Próximos días**" in md
    assert "- Correo · Ana — Contract to sign" in md and "- Chat · bob" in md
    assert "- Restock: GPU · tantalus" in md
    assert "- Incidencia · kafka (down) — port closed" in md and "- En marcha · kafka: Indexing (40 %)" in md
    assert "- 1 avisos retenidos hoy" in md
    assert "Lo que necesita tu atención" not in md and d["summary"] == []
    assert d["items"] >= 8 and "payload" in d


def test_compose_digest_english_and_empty_sections(env):
    env.hub.config.language = "en"
    env.hub._facets_by_id.pop("mailgate")
    env.hub._facets_by_id.pop("worktrack")
    d = env.facet.compose_digest("work", summarize=False)
    md = d["markdown"]
    assert d["title"] == "Digest — Work — Monday, October 5, 2026"
    assert [l for l in md.splitlines() if l.startswith("## ")] == ["## Today", "## Attention", "## News", "## System"]
    assert "## Attention\nNothing pending." in md and "## News\nNothing pending." in md and "## System\nNothing pending." in md
    assert "- Wed 7 · Work review · Kafka's Hoard" in md


def test_digest_summary_written_by_the_model_only_from_the_items(env):
    prompts = []

    def llm(system, user):
        prompts.append((system, user))
        return "Sure!\n- Pay rent today\n* Late tax form is overdue\n\n- a\n- b\n- c\n- d (sixth line is cut)"

    f = env.facet
    f._llm = llm
    d = f.compose_digest("personal")                                                   # personal.digest.summarize is true
    assert len(prompts) == 1
    assert "SOLO" in prompts[0][0] and "no inventes" in prompts[0][0] and "Pay rent" in prompts[0][1]
    assert d["summary"] == ["- Sure!", "- Pay rent today", "- Late tax form is overdue", "- a", "- b"]   # at most five
    assert d["markdown"].startswith("## Lo que necesita tu atención\n- Sure!") and "\n\n## Hoy" in d["markdown"]
    assert d["template"].startswith("## Hoy")                                          # the template alone stays available
    # work does not summarize: the model is not even asked; an explicit flag overrides the sphere
    prompts.clear()
    assert f.compose_digest("work")["summary"] == [] and prompts == []
    assert f.compose_digest("work", summarize=True)["summary"] and len(prompts) == 1
    assert f.compose_digest("personal", summarize=False)["summary"] == [] and len(prompts) == 1


def test_digest_summary_failures_fall_back_to_the_template(env):
    f = env.facet
    for answer in (None, "", "NADA", "nada.", "  NADA \n", 42):
        f._llm = lambda s, u, a=answer: a
        d = f.compose_digest("personal")
        assert d["summary"] == [] and d["markdown"].startswith("## Hoy"), answer

    def boom(s, u):
        raise TimeoutError("model is loading")

    f._llm = boom
    assert f.compose_digest("personal")["summary"] == []
    env.hub.config.language = "en"
    f._llm = lambda s, u: "NONE"
    assert f.compose_digest("personal")["summary"] == []


def test_default_llm_never_loads_a_model(env, monkeypatch):
    import hoard_link.link as link_mod
    from hoard_link.types import Resolution
    calls = {"chat": 0, "cfg": []}

    def fake_link(resolution):
        class FakeLink:
            def __init__(self, cfg, client=None):
                calls["cfg"].append(cfg)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def resolve(self, cap):
                return resolution

            async def chat(self, messages, **kw):
                calls["chat"] += 1
                calls["kw"] = kw
                calls["messages"] = messages
                return type("R", (), {"text": "- hello"})()
        return FakeLink

    def res(state, resident):
        return Resolution(capability="llm", provider="ollama", url="http://127.0.0.1:11434", model="qwen", api="ollama",
                          state=state, reason="x", details={"resident": resident})

    monkeypatch.setattr(link_mod, "Link", fake_link(res("unavailable", None)))
    assert env.facet._default_llm("sys", "user") is None and calls["chat"] == 0
    monkeypatch.setattr(link_mod, "Link", fake_link(res("resolved", False)))            # it WOULD load: refused
    assert env.facet._default_llm("sys", "user") is None and calls["chat"] == 0
    monkeypatch.setattr(link_mod, "Link", fake_link(res("resolved", True)))
    assert env.facet._default_llm("sys", "user") == "- hello" and calls["chat"] == 1
    assert calls["cfg"][-1].only_resident is True and calls["cfg"][-1].gpu_lease is False
    assert calls["messages"][0]["role"] == "system" and calls["messages"][1]["content"] == "user"

    class Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("no httpx")

    monkeypatch.setattr(link_mod, "Link", Exploding)
    assert env.facet._default_llm("sys", "user") is None                              # any error → no summary


def test_send_digest_stores_notifies_and_emits(env):
    f = env.facet
    f._llm = lambda s, u: "- Pay rent today"
    res = f.send_digest("personal")
    assert res["ok"] and res["sphere"] == "personal" and res["summary"] == ["- Pay rent today"]
    call = env.notify.calls[0]
    assert (call["app"], call["sphere"], call["priority"], call["group"]) == ("hub", "personal", "normal", "digest")
    assert call["channels_override"] == ["windows"] and call["title"] == res["title"] and call["body"] == res["markdown"]
    assert call["url"] == env.hub.config.url and call["dedupe_key"].startswith("digest:personal:")
    saved = Path(env.hub.config.data_dir) / "digests" / "2026-10-05-personal.json"
    assert res["file"] == str(saved) and saved.is_file()
    record = json.loads(saved.read_text(encoding="utf-8"))
    assert record["markdown"] == res["markdown"] and record["sent"]["ok"] is True and record["summary"] == ["- Pay rent today"]
    evs = env.hub.events.query(type="hub.today.digest", since_ts=0)
    assert len(evs) == 1 and evs[0]["data"]["sphere"] == "personal" and evs[0]["data"]["items"] == res["items"]
    last = env.facet.today("personal")["last_digest"]
    assert last["date"] == "2026-10-05" and last["sent"] is True and last["summary"] is True and last["ts"] == NOW
    # persisted across a restart of the facet
    again = TodayFacet(env.hub, clock=lambda: NOW, tz=TZ, background=False)
    assert again._last_digest("personal")["ts"] == NOW
    # work uses its own channels
    env.facet.send_digest("work")
    assert env.notify.calls[1]["channels_override"] == ["telegram"] and env.notify.calls[1]["sphere"] == "work"


def test_send_digest_without_override_support_raises_priority_for_digest_only_routing(env):
    old = OldNotify()
    env.hub._facets_by_id["notify"] = old
    env.facet.send_digest("personal", summarize=False)
    env.facet.send_digest("work", summarize=False)                                      # work routes "normal" to the digest only
    assert [(c["sphere"], c["priority"], c["group"]) for c in old.calls] == [("personal", "normal", "digest"), ("work", "high", "digest")]


def test_send_digest_with_no_notify_still_stores(env):
    env.hub._facets_by_id.pop("notify")
    res = env.facet.send_digest("personal", summarize=False)
    assert res["ok"] and res["sent"]["ok"] is False and Path(res["file"]).is_file()
    assert env.facet.today("personal")["last_digest"]["sent"] is False


def test_digest_wanted_event_sends_each_sphere_once_a_day(env):
    env.facet.start()                                                                    # subscribes to the event log
    try:
        env.hub.events.emit("digest.wanted", {"date": "2026-10-05"}, source="hub")
        deadline_ = time.time() + 10
        while time.time() < deadline_ and len(env.notify.calls) < 2:
            time.sleep(0.05)
        assert sorted(c["sphere"] for c in env.notify.calls) == ["personal", "work"]
        time.sleep(0.2)
        env.hub.events.emit("digest.wanted", {}, source="hub")                          # already digested today
        time.sleep(0.5)
        assert len(env.notify.calls) == 2
        env.hub.events.emit("hub.job.ran", {"job": "x"}, source="hub")                  # other events are ignored
        time.sleep(0.2)
        assert len(env.notify.calls) == 2
    finally:
        env.facet.close()


def test_digest_wanted_for_one_sphere_and_disabled_digest(env):
    env.facet._digest_wanted({"data": {"sphere": "work"}})
    assert [c["sphere"] for c in env.notify.calls] == ["work"]
    env.spheres._list[0]["digest"]["enabled"] = False
    env.notify.calls.clear()
    env.facet._digest_wanted({"data": {}})
    assert [c["sphere"] for c in env.notify.calls] == []                               # work already had it today, personal is off


# ---- the digest jobs ----------------------------------------------------------------------------

def jobs_by_name(hub):
    return {j["name"]: j for j in hub.jobs.list()}


def test_ensure_jobs_creates_one_per_sphere(env):
    env.facet._ensure_jobs()
    jobs = jobs_by_name(env.hub)
    assert set(jobs) == {"Resumen — Personal", "Resumen — Trabajo"}
    p, w = jobs["Resumen — Personal"], jobs["Resumen — Trabajo"]
    assert (p["at"], p["days"]) == ("08:30", None) and (w["at"], w["days"]) == ("08:45", ["weekdays"])
    assert p["then"] == [{"kind": "hub", "tool": "hub_today_digest", "args": {"sphere": "personal"}}]
    assert w["then"][0]["args"] == {"sphere": "work"} and p["enabled"] and w["enabled"]
    stored = json.loads((Path(env.hub.config.data_dir) / "today.json").read_text(encoding="utf-8"))
    assert stored["jobs"]["personal"]["id"] == p["id"] and stored["jobs"]["work"]["id"] == w["id"]
    env.facet._ensure_jobs()
    env.facet._ensure_jobs()
    assert len(env.hub.jobs.list()) == 2                                               # idempotent


def test_ensure_jobs_never_recreates_a_deleted_job(env):
    env.facet._ensure_jobs()
    pid = jobs_by_name(env.hub)["Resumen — Personal"]["id"]
    assert env.hub.jobs.remove(pid)["ok"]
    env.facet._ensure_jobs()
    assert set(jobs_by_name(env.hub)) == {"Resumen — Trabajo"}
    # not even after a restart of the facet (today.json remembers)
    TodayFacet(env.hub, clock=lambda: NOW, tz=TZ, background=False)._ensure_jobs()
    assert set(jobs_by_name(env.hub)) == {"Resumen — Trabajo"}


def test_ensure_jobs_follows_the_sphere_but_not_manual_edits(env):
    env.facet._ensure_jobs()
    pid = jobs_by_name(env.hub)["Resumen — Personal"]["id"]
    # the person moves the job by hand: untouched while the sphere is unchanged
    env.hub.jobs.update(pid, {"at": "07:00"})
    env.facet._ensure_jobs()
    assert env.hub.jobs.get(pid)["at"] == "07:00"
    # the sphere's time changes: the job follows
    env.spheres._list[0]["digest"]["at"] = "09:15"
    env.spheres._list[0]["digest"]["days"] = ["mon", "wed"]
    env.facet._ensure_jobs()
    j = env.hub.jobs.get(pid)
    assert (j["at"], j["days"]) == ("09:15", ["mon", "wed"])
    # a renamed sphere renames the job
    env.spheres._list[0]["name"]["es"] = "Mi vida"
    env.facet._ensure_jobs()
    assert env.hub.jobs.get(pid)["name"] == "Resumen — Mi vida"
    # a digest turned off disables ITS job; turned on again re-enables it
    env.spheres._list[0]["digest"]["enabled"] = False
    env.facet._ensure_jobs()
    assert env.hub.jobs.get(pid)["enabled"] is False
    env.spheres._list[0]["digest"]["enabled"] = True
    env.facet._ensure_jobs()
    assert env.hub.jobs.get(pid)["enabled"] is True
    # a job the person disabled themselves stays disabled
    env.hub.jobs.update(pid, {"enabled": False})
    env.facet._ensure_jobs()
    assert env.hub.jobs.get(pid)["enabled"] is False


def test_ensure_jobs_adopts_an_existing_job_and_skips_disabled_spheres(env):
    env.hub.jobs.add({"name": "Resumen — Personal", "at": "06:00", "then": [{"kind": "event", "type": "x"}]})
    env.spheres._list[1]["digest"]["enabled"] = False
    env.facet._ensure_jobs()
    jobs = jobs_by_name(env.hub)
    assert set(jobs) == {"Resumen — Personal"} and jobs["Resumen — Personal"]["at"] == "06:00"   # adopted, not duplicated
    env.spheres._list[1]["digest"]["enabled"] = True
    env.spheres._list[1]["digest"]["at"] = "25:99"                                       # an invalid time creates nothing
    env.facet._ensure_jobs()
    assert set(jobs_by_name(env.hub)) == {"Resumen — Personal"}


def test_ensure_jobs_without_spheres_does_nothing(env):
    env.hub._facets_by_id.pop("spheres")
    env.facet._ensure_jobs()
    assert env.hub.jobs.list() == []


def test_start_only_makes_jobs_when_the_scheduler_runs(env):
    env.hub.config.jobs_enabled = False
    env.facet.start()
    env.facet.close()
    assert env.hub.jobs.list() == []
    env.hub.config.jobs_enabled = True
    env.facet.start()
    env.facet.close()
    assert set(jobs_by_name(env.hub)) == {"Resumen — Personal", "Resumen — Trabajo"}


def test_digest_job_runs_through_the_hub_tool(env):
    env.facet._ensure_jobs()
    pid = jobs_by_name(env.hub)["Resumen — Personal"]["id"]
    env.facet._llm = lambda s, u: None
    res = env.hub.jobs.run_now(pid)
    assert res["ok"] is True and env.notify.calls[0]["sphere"] == "personal" and env.notify.calls[0]["group"] == "digest"


# ---- the calendar -------------------------------------------------------------------------------

def test_calendar_reply_checks_the_token_and_spheres(env):
    f = env.facet
    token = f.ics_token()
    assert len(token) >= 24
    bad = f.calendar_reply("nope", "all")
    assert bad.status == 403 and bad.body is None
    assert f.calendar_reply("", "all").status == 403
    ok = f.calendar_reply(token, "all")
    assert ok.status == 200 and ok.content_type.startswith("text/calendar") and ok.headers["Content-Disposition"].endswith('hoard-all.ics"')
    lines = unfold(ok.body.decode("utf-8"))
    summaries = [l for l in lines if l.startswith("SUMMARY:")]
    assert "SUMMARY:Pay rent (Kafka's Hoard)" in summaries and "SUMMARY:Netflix renewal (Ledger's Hoard)" in summaries
    assert "SUMMARY:Far away (Kafka's Hoard)" in summaries                              # window is -14 … +120 days
    assert "DTSTART:20261005T073000Z" in lines                                          # the 09:30 +02:00 call
    work = unfold(f.calendar_reply(token, "work").body.decode("utf-8"))
    assert "SUMMARY:Work review (Kafka's Hoard)" in work and "SUMMARY:Netflix renewal (Ledger's Hoard)" not in work
    assert "X-WR-CALNAME:Hoard — Trabajo" in work


def test_ics_window_is_14_days_back_120_ahead(env):
    env.apps["ledger"].items = []
    env.apps["kafka"].items = [deadline(1, "Too early", "2026-09-20"), deadline(2, "Edge back", "2026-09-21"),
                               deadline(3, "Edge ahead", "2027-02-02"), deadline(4, "Too late", "2027-02-04")]
    titles = [l[8:] for l in unfold(env.facet.ics("all")) if l.startswith("SUMMARY:")]
    assert titles == ["Edge back (Kafka's Hoard)", "Edge ahead (Kafka's Hoard)"]
    # the apps were asked for exactly that window
    q = env.apps["kafka"].calls[-1]["query"]
    assert (q["from"], q["to"]) == ("2026-09-21", "2027-02-02")


def test_rotate_token_invalidates_the_old_link(env):
    f = env.facet
    old = f.ics_token()
    new = f.rotate_ics_token()
    assert new != old and f.calendar_reply(old, "all").status == 403 and f.calendar_reply(new, "all").status == 200
    assert TodayFacet(env.hub, clock=lambda: NOW, tz=TZ, background=False).ics_token() == new       # persisted


def test_ics_info_and_config(env):
    f = env.facet
    f._lan_ip = lambda: "192.168.1.20"
    info = f.ics_info()
    assert info["ok"] and info["token_set"] and info["lan_url"] is None and info["export_path"] == ""
    assert info["url"] == f"{env.hub.config.url}/calendar.ics?token={f.ics_token()}&sphere=all"
    assert set(info["urls"]) == {"all", "personal", "work"} and info["urls"]["work"].endswith("sphere=work")
    assert f.set_ics_config(lan_port="abc")["ok"] is False and f.set_ics_config(lan_port=70000)["ok"] is False
    out = env.tmp / "cloud"
    assert f.set_ics_config(export_path=str(out))["export_path"] == str(out)
    assert f.set_ics_config(export_path="")["export_path"] == ""


def test_export_folder_writes_one_file_per_sphere(env):
    f = env.facet
    assert f.export_ics()["ok"] is False                                                # no folder set
    out = env.tmp / "cloud" / "sub"
    f.set_ics_config(export_path=str(out))
    res = f.export_ics()
    assert res["ok"] and sorted(Path(p).name for p in res["files"]) == ["hoard-all.ics", "hoard-personal.ics", "hoard-work.ics"]
    work = unfold((out / "hoard-work.ics").read_bytes().decode("utf-8"))
    assert "SUMMARY:Work review (Kafka's Hoard)" in work and not list(out.glob("*.tmp"))
    assert "token" not in (out / "hoard-all.ics").read_text(encoding="utf-8").lower()   # the file carries no token
    # the hourly housekeeping re-exports when due
    (out / "hoard-all.ics").unlink()
    f._next_export = NOW - 1
    f._cfg["ics"]["export_path"] = str(out)
    assert f.clock() >= f._next_export
    f.export_ics()
    assert (out / "hoard-all.ics").is_file() and f._next_export == NOW + 3600


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def test_lan_listener_serves_only_the_calendar(env):
    f = env.facet
    port = free_port()
    f._lan_ip = lambda: "192.168.1.20"
    info = f.set_ics_config(lan_port=port)
    try:
        token = f.ics_token()
        assert info["lan_port"] == port and info["lan_url"] == f"http://192.168.1.20:{port}/calendar.ics?token={token}&sphere=all"
        base = f"http://127.0.0.1:{port}"
        st, body, hdr = _get(f"{base}/calendar.ics?token={token}&sphere=work")
        assert st == 200 and hdr["Content-Type"].startswith("text/calendar") and body.startswith(b"BEGIN:VCALENDAR\r\n")
        assert b"Work review" in body
        assert _get(f"{base}/calendar.ics?token=wrong")[0] == 403
        assert _get(f"{base}/calendar.ics")[0] == 403
        for path in ("/", "/api/today", "/api/health", "/calendar.ics.bak", "/api/today/ics"):
            assert _get(base + path + ("" if "?" in path else f"?token={token}"))[0] == 404, path
        st, _, _ = _get(f"{base}/calendar.ics?token={token}")
        assert st == 200
        # rotating the token closes the old link on the listener as well
        f.rotate_ics_token()
        assert _get(f"{base}/calendar.ics?token={token}")[0] == 403
    finally:
        f.set_ics_config(lan_port=0)
    time.sleep(0.2)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    assert f.ics_info()["lan_url"] is None


def test_lan_listener_port_in_use_is_reported_not_raised(env):
    f = env.facet
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        info = f.set_ics_config(lan_port=busy)
    assert info["ok"] and "cannot listen" in info["lan_error"] and info["lan_url"] is None


# ---- HTTP + tools -----------------------------------------------------------------------------------

@pytest.fixture
def served(env):
    server = make_server(env.hub, port=env.hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield env, env.hub.config.url, {"Authorization": "Bearer " + env.hub.token}
    server.shutdown()


def _json(url, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_http_routes_and_permissions(served):
    env, url, auth = served
    assert _json(url + "/api/today")[0] == 401                                            # mail subjects are not for anonymous callers
    assert _json(url + "/api/today/ics")[0] == 401
    st, body = _json(url + "/api/today?sphere=personal", headers=auth)
    assert st == 200 and body["ok"] and body["agenda"]["today"] and body["sphere"] == "personal"
    st, body = _json(url + "/api/today/agenda?from=2026-10-01&to=2026-10-07&sphere=all", headers=auth)
    assert st == 200 and body["count"] == len(body["items"]) and body["from"] == "2026-10-01" and body["sphere"] == "all"
    assert {i["app"] for i in body["items"]} == {"kafka", "ledger"}
    st, body = _json(url + "/api/today/agenda?from=today&to=%2B3d", headers=auth)
    assert (body["from"], body["to"]) == ("2026-10-05", "2026-10-08")
    st, body = _json(url + "/api/today/digest?sphere=work", headers=auth)
    assert st == 200 and body["markdown"].startswith("## Hoy") and "payload" not in body and env.notify.calls == []   # preview: nothing sent
    # writing needs the hub token or the page
    assert _json(url + "/api/today/digest", {"sphere": "personal"})[0] == 403 and env.notify.calls == []
    st, body = _json(url + "/api/today/digest", {"sphere": "work", "send": False}, headers=auth)
    assert st == 200 and body["sphere"] == "work" and env.notify.calls == []
    env.facet._llm = lambda s, u: None
    st, body = _json(url + "/api/today/digest", {"sphere": "personal"}, headers=auth)
    assert st == 200 and body["ok"] and body["sent"]["ok"] and len(env.notify.calls) == 1
    # an app token may read but not write
    kafka_auth = {"Authorization": "Bearer " + TOKEN}
    assert _json(url + "/api/today", headers=kafka_auth)[0] == 200
    st, body = _json(url + "/api/today/ics/rotate", {}, headers=kafka_auth)
    assert st == 403 and env.facet.ics_token()
    st, body = _json(url + "/api/today/digest", {"sphere": "personal"}, headers=kafka_auth)
    assert st == 403 and len(env.notify.calls) == 1


def test_http_calendar_route_and_rotation(served):
    env, url, auth = served
    st, info = _json(url + "/api/today/ics", headers=auth)
    assert st == 200 and info["token_set"] and info["url"].startswith(url + "/calendar.ics?token=")
    token = env.facet.ics_token()
    st, body, hdr = _get(f"{url}/calendar.ics?token={token}&sphere=personal")           # no bearer: the token is the key
    assert st == 200 and hdr["Content-Type"].startswith("text/calendar") and body.startswith(b"BEGIN:VCALENDAR\r\n")
    assert b"Pay rent" in body and b"Work review" not in body
    assert _get(f"{url}/calendar.ics?token=wrong")[0] == 403
    assert _get(f"{url}/calendar.ics")[0] == 403
    st, rotated = _json(url + "/api/today/ics/rotate", {}, headers=auth)
    assert st == 200 and rotated["url"] != info["url"]
    assert _get(f"{url}/calendar.ics?token={token}")[0] == 403
    assert _get(rotated["url"])[0] == 200
    out = env.tmp / "exp"
    st, cfg = _json(url + "/api/today/ics/config", {"export_path": str(out)}, headers=auth)
    assert st == 200 and cfg["export_path"] == str(out)
    st, res = _json(url + "/api/today/ics/export", {}, headers=auth)
    assert st == 200 and res["ok"] and (out / "hoard-personal.ics").is_file()
    st, bad = _json(url + "/api/today/ics/config", {"lan_port": "x"}, headers=auth)
    assert st == 400 and "lan_port" in bad["error"]


def test_tools_catalogue_and_handlers(served):
    env, url, auth = served
    cat = {t["name"]: t for t in TodayFacet.tools()}
    assert set(cat) == {"hub_today", "hub_agenda", "hub_today_digest", "hub_ics_link"}
    for t in cat.values():
        first = t["description"].split("\n")[0]
        assert len(first) <= 110, first
        assert t["inputSchema"]["type"] == "object"
    assert cat["hub_today"]["annotations"]["readOnlyHint"] and cat["hub_agenda"]["annotations"]["readOnlyHint"]
    assert cat["hub_ics_link"]["annotations"]["readOnlyHint"] and "annotations" not in cat["hub_today_digest"]
    blob = " ".join(t["description"].lower() for t in cat.values())
    for word in ("agenda", "hoy", "resumen", "calendario", "today", "digest", "deadlines"):
        assert word in blob
    names = {t["name"] for t in hub_tools.all_tools()}
    assert set(cat) <= names                                                              # reachable through the MCP catalogue
    assert {t["name"] for t in hub_facets.catalogue()} >= set(cat)
    r = hub_tools.call(env.hub, "hub_today", {"sphere": "personal"})
    assert r["ok"] and r["agenda"]["overdue"][0]["title"] == "Late tax form"
    r = hub_tools.call(env.hub, "hub_agenda", {"from": "2026-10-05", "to": "+1d", "sphere": "personal"})
    assert r["ok"] and [i["title"] for i in r["items"]] == ["Pay rent", "Call the plumber", "Netflix renewal", "Dentist"]
    r = hub_tools.call(env.hub, "hub_agenda", {})
    assert (r["from"], r["to"]) == ("2026-10-05", "2026-10-19")                           # default: today … +14 days
    env.facet._llm = lambda s, u: "- one thing"
    r = hub_tools.call(env.hub, "hub_today_digest", {"sphere": "personal"})              # what the job runs: sends
    assert r["ok"] and r["summary"] == ["- one thing"] and len(env.notify.calls) == 1
    r = hub_tools.call(env.hub, "hub_today_digest", {"sphere": "personal", "send": False})
    assert r["ok"] and "payload" not in r and len(env.notify.calls) == 1 and r["summary"] == []
    r = hub_tools.call(env.hub, "hub_ics_link", {})
    assert r["ok"] and "/calendar.ics?token=" in r["url"]
    # and over the agent route with the hub token
    st, body = _json(url + "/api/agent/call", {"tool": "hub_today", "arguments": {}}, headers=auth)
    assert st == 200 and body["result"]["ok"]


def test_facet_loads_in_a_real_hub(tmp_path):
    """The hub loads the facet by itself (FACET_MODULES order), starts and stops it."""
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(tmp_path / "none")], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False)
    hub = Hub(cfg)
    try:
        facet = hub.facet("today")
        assert isinstance(facet, TodayFacet) and facet.info()["ui_scripts"] == ["today.js"]
        assert (tmp_path / "data" / "today.json").is_file()
        t = facet.today()
        assert t["ok"] and t["agenda"] == {"overdue": [], "today": [], "tomorrow": [], "week": []}
        assert (Path(today_mod.__file__).parent / "ui" / "today.js").is_file()
    finally:
        hub.close()


def test_send_digest_reports_delivery_honestly(env):
    class Mute:
        def send(self, title, body="", **kw):
            return {"ok": True, "held": None, "delivered": [], "channels": [{"channel": "windows", "unsupported": True}]}

    env.hub._facets_by_id["notify"] = Mute()
    env.facet.send_digest("personal", summarize=False)
    assert env.facet.today("personal")["last_digest"]["sent"] is False               # pushed nowhere: not "sent"
    env.hub._facets_by_id["notify"] = env.notify
    env.facet.send_digest("personal", summarize=False)
    assert env.facet.today("personal")["last_digest"]["sent"] is True


def test_real_spheres_and_notify_facets_work_together(tmp_path):
    """No fakes for the siblings: the hub's own spheres + notify + mailgate facets, as loaded at start."""
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(tmp_path / "none")], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"], jobs_enabled=False, language="es")
    hub = Hub(cfg)
    try:
        if hub.facet("spheres") is None or hub.facet("notify") is None:
            pytest.skip("spheres/notify facets are not shipped in this build")
        assert hub.jobs.list() == []                                                    # jobs_enabled is False: no digest jobs
        hub.facet("today")._ensure_jobs()
        names = {j["name"]: j for j in hub.jobs.list()}
        assert {"Resumen — Personal", "Resumen — Trabajo"} <= set(names)
        assert names["Resumen — Trabajo"]["days"] == ["weekdays"]
        facet = hub.facet("today")
        facet._llm = lambda s, u: None
        res = facet.send_digest("personal")
        assert res["ok"] and Path(res["file"]).is_file() and isinstance(res["sent"], dict)
        t = facet.today("personal")
        assert t["last_digest"]["date"] == res["date"]
        assert hub.events.query(type="hub.today.digest", since_ts=0)
    finally:
        hub.close()


# ---- the page script (a minimal DOM stub, run with node when present) ---------------------------------

UI_SCRIPT = r"""
import fs from "node:fs";
class El { constructor(tag){this.tag=tag;this.children=[];this.className="";this._text="";this.dataset={};this.style={};this.attrs={};this.hidden=false;}
  appendChild(c){this.children.push(c);return c;} set textContent(v){this._text=String(v);this.children=[];} get textContent(){return this._text+this.children.map(c=>c.textContent).join("");}
  setAttribute(k,v){this.attrs[k]=v;} querySelector(sel){ const cls=sel.startsWith(".")?sel.slice(1):null; const walk=(n)=>{for(const c of n.children){ if(cls && (c.className||"").split(" ").includes(cls)) return c; const r=walk(c); if(r) return r;} return null;}; return walk(this);} remove(){} addEventListener(){} select(){} }
const head = new El("head");
globalThis.document = { createElement:(t)=>new El(t), head, body:new El("body"), documentElement:{dataset:{sphere:"work"}}, hidden:false, addEventListener(){}, querySelector:(s)=>null, execCommand(){return true}, readyState:"complete" };
globalThis.location = {origin:"http://x"};
globalThis.localStorage = { _:{}, getItem(k){return this._[k]??null}, setItem(k,v){this._[k]=v} };
globalThis.MutationObserver = class { observe(){} };
Object.defineProperty(globalThis,"navigator",{value:{ clipboard: { writeText: async()=>{} } },configurable:true});
globalThis.window = globalThis; globalThis.confirm = ()=>true;
let reg=null; const bus=new EventTarget(); let calls=[];
const api = async (path, body)=>{ calls.push([path,body]); if(path.startsWith("/api/today/ics")) return {ok:true,url:"/calendar.ics?token=t&sphere=all",urls:{all:"/calendar.ics?token=t&sphere=all",work:"/calendar.ics?token=t&sphere=work"},lan_url:"http://1.2.3.4:5/calendar.ics?token=t",lan_urls:{},export_path:"",lan_port:5,lan_error:""}; if(path.startsWith("/api/today/digest")) return {ok:true,sent:{ok:true}}; return {ok:true,date:"2026-10-05",sphere:"work",last_digest:{ts:1790000000},
  agenda:{overdue:[{title:"Late",start:"2026-10-01",all_day:true,priority:"high",app_name:"Kafka",url:"http://a/b"}],today:[{title:"Call",start:"2026-10-05T09:30:00+02:00",all_day:false,priority:"urgent",app_name:"Kafka",detail:"d"}],tomorrow:[],week:[{title:"Trip",start:"2026-10-09",all_day:true}]},
  attention:{mail:[{subject:"S",from_name:"Ana"}],chats:[{text:"hey",from:"bob"}]}, news:[{title:"N",url:"javascript:x",watch:"w"}], system:{incidents:[{app:"kafka",to_state:"down",probable_cause:"c"}],jobs:[{title:"J",progress:0.5,app:"k"}]}, notifications:[{kind:"sent"},{kind:"held"}], errors:{agenda:[{app:"x"}]}}; };
globalThis.HubFacets = { ctx:{ $:(s)=>null, el:(tag,cls,text)=>{const e=new El(tag); if(cls) e.className=cls; if(text!==undefined&&text!==null) e.textContent=text; return e;}, api, toast:(m)=>calls.push(["toast",m]), L:(o)=>o.es, lang:()=>"es", fmtWhen:(v)=>"when", bus }, register(id,spec){reg=spec;} };
await import(process.argv[2]);
const box=new El("section"); reg.mount(box, HubFacets.ctx); await reg.load(HubFacets.ctx);
const txt=box.textContent; 
for (const w of ["Hoy","Agenda","Atención","Novedades","Sistema","Vencido","Late","Call","Trip","Ana — S","bob","N","Incidencias abiertas","Trabajos en marcha","50 %","1 enviados, 1 retenidos","Algunas apps no respondieron: x","Enviar resumen ahora","Calendario","urgente","alta"]) if(!txt.includes(w)) throw new Error("missing "+w+" in "+txt);
// javascript: urls never become links
const find=(n,f)=>{ if(f(n)) return n; for(const c of n.children){const r=find(c,f); if(r) return r;} return null;};
if (find(box,(n)=>n.tag==="a"&&String(n.href).startsWith("javascript"))) throw new Error("js link");
if(!find(box,(n)=>n.tag==="a"&&n.href==="http://a/b")) throw new Error("link missing");
if(calls[0][0]!=="/api/today?sphere=work") throw new Error("sphere not used "+calls[0][0]);
bus.dispatchEvent(new CustomEvent("sphere",{detail:"personal"})); await new Promise(r=>setTimeout(r,10));
if(!calls.some(c=>c[0]==="/api/today?sphere=personal")) throw new Error("did not follow sphere");
// buttons
const head2 = box.querySelector(".today-head"); const buttons = head2.children.filter(c=>c.tag==="button");
await buttons.find(b=>b.textContent.includes("Enviar")).onclick(); 
if(!calls.some(c=>c[0]==="/api/today/digest"&&c[1].send===true)) throw new Error("digest not posted");
await buttons.find(b=>b.textContent==="Calendario").onclick(); await new Promise(r=>setTimeout(r,10));
const t2=box.textContent; if(!t2.includes("Copiar enlace")||!t2.includes("Cambiar el token")) throw new Error("ics panel "+t2);
console.log("ui ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_today_ui_script_renders_and_follows_the_sphere(tmp_path):
    script = tmp_path / "ui.mjs"
    script.write_text(UI_SCRIPT, encoding="utf-8")
    ui = Path(today_mod.__file__).parent / "ui" / "today.js"
    check = subprocess.run(["node", "--check", str(ui)], capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr
    run = subprocess.run(["node", str(script), ui.as_uri()], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr + run.stdout
    assert run.stdout.strip() == "ui ok"


def test_agenda_forgets_an_unsupported_app_when_it_restarts_or_on_refresh(env):
    env.facet._unsupported["kafka"] = NOW
    assert "kafka" not in {a["app"] for a in env.facet.agenda("2026-10-01", "2026-10-31")["apps"]}
    env.facet._on_event({"type": "hub.app.started", "data": {"app": "kafka"}})
    assert "kafka" in {a["app"] for a in env.facet.agenda("2026-10-01", "2026-10-31")["apps"]}
    env.facet._unsupported["ledger"] = NOW
    assert "ledger" in {a["app"] for a in env.facet.agenda("2026-10-01", "2026-10-31", refresh=True)["apps"]}
