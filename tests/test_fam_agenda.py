"""The app-side agenda helper: item normalisation, the FastAPI route (token, dates, providers that raise) and
its Node twin (installAgenda in js/hoard-link.js)."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoard_link import fam_agenda, family

ROOT = Path(__file__).resolve().parents[1]


# ---- normalisation -------------------------------------------------------------------

def test_parse_when_forms():
    assert fam_agenda.parse_when("2026-10-05") == (date(2026, 10, 5), None)
    day, moment = fam_agenda.parse_when("2026-10-05T09:30:00+02:00")
    assert day == date(2026, 10, 5) and moment.utcoffset() == timedelta(hours=2)
    assert fam_agenda.parse_when("2026-10-05T07:30:00Z")[1].utcoffset() == timedelta(0)
    assert fam_agenda.parse_when("2026-10-05 09:30")[1].tzinfo is None
    assert fam_agenda.parse_when(date(2026, 1, 2)) == (date(2026, 1, 2), None)
    for bad in ("", "tomorrow", "2026-13-01", "2026-02-30", None, 12, "2026-10-05T25:00:00"):
        assert fam_agenda.parse_when(bad) is None


def test_normalize_item_contract_shape():
    item = fam_agenda.normalize_item({"id": "kafka:deadline:41", "title": "  Renew   the lease ", "start": "2026-10-05",
                                      "kind": "Deadline", "priority": "HIGH", "url": "http://127.0.0.1:5200/#/d/41",
                                      "detail": "x" * 400, "sphere": "Personal"}, app="kafka")
    assert item == {"id": "kafka:deadline:41", "title": "Renew the lease", "start": "2026-10-05", "all_day": True,
                    "kind": "deadline", "priority": "high", "url": "http://127.0.0.1:5200/#/d/41",
                    "detail": "x" * 300, "sphere": "personal"}


def test_normalize_item_defaults_and_coercions():
    # no id → stable generated id; unknown kind/priority fall back; a javascript: url is dropped
    a = fam_agenda.normalize_item({"title": "Pay", "start": "2026-10-05", "kind": "weird", "priority": "meh",
                                   "url": "javascript:alert(1)"}, app="ledger")
    b = fam_agenda.normalize_item({"title": "Pay", "start": "2026-10-05"}, app="ledger")
    assert a["id"] == b["id"] and a["id"].startswith("ledger:other:")
    assert (a["kind"], a["priority"], a["url"]) == ("other", "normal", "")
    # bare id gets the app prefix; spaces in ids are replaced
    assert fam_agenda.normalize_item({"id": "7", "title": "t", "start": "2026-10-05"}, app="kafka")["id"] == "kafka:7"
    assert fam_agenda.normalize_item({"id": "a b:1", "title": "t", "start": "2026-10-05"})["id"] == "a_b:1"
    # date/datetime objects are accepted; a timed item keeps its offset
    tz = timezone(timedelta(hours=2))
    t = fam_agenda.normalize_item({"title": "Call", "start": datetime(2026, 10, 5, 9, 30, tzinfo=tz),
                                   "end": datetime(2026, 10, 5, 10, 0, tzinfo=tz)})
    assert (t["start"], t["end"], t["all_day"]) == ("2026-10-05T09:30:00+02:00", "2026-10-05T10:00:00+02:00", False)
    # all_day wins over a time; a date-only start is always all-day
    assert fam_agenda.normalize_item({"title": "x", "start": "2026-10-05T09:30:00", "all_day": True})["start"] == "2026-10-05"
    assert fam_agenda.normalize_item({"title": "x", "start": "2026-10-05", "all_day": False})["all_day"] is True
    # an end before the start is dropped; an all-day end keeps the day
    assert "end" not in fam_agenda.normalize_item({"title": "x", "start": "2026-10-05", "end": "2026-10-04"})
    assert fam_agenda.normalize_item({"title": "x", "start": "2026-10-05", "end": "2026-10-07"})["end"] == "2026-10-07"


def test_normalize_item_drops_the_unshowable():
    for bad in ({"start": "2026-10-05"}, {"title": "x"}, {"title": "x", "start": "soon"}, "text", None, 3,
                {"title": "   ", "start": "2026-10-05"}):
        assert fam_agenda.normalize_item(bad) is None


def test_normalize_items_dedupes_caps_and_unwraps():
    raw = [{"id": "a:1", "title": "one", "start": "2026-10-05"}, {"id": "a:1", "title": "dup", "start": "2026-10-06"},
           {"title": "no start"}, "junk"]
    assert [i["title"] for i in fam_agenda.normalize_items(raw)] == ["one"]
    assert fam_agenda.normalize_items({"items": raw})[0]["id"] == "a:1"
    assert fam_agenda.normalize_items("nope") == [] and fam_agenda.normalize_items(None) == []
    many = [{"id": f"a:{i}", "title": "t", "start": "2026-10-05"} for i in range(fam_agenda.MAX_ITEMS + 50)]
    assert len(fam_agenda.normalize_items(many)) == fam_agenda.MAX_ITEMS


def test_parse_range_defaults_swaps_and_caps():
    today = date(2026, 10, 5)
    assert fam_agenda.parse_range(None, None, today=today) == (date(2026, 9, 28), date(2026, 12, 4))
    assert fam_agenda.parse_range("2026-10-10", "2026-10-01", today=today) == (date(2026, 10, 1), date(2026, 10, 10))
    assert fam_agenda.parse_range("garbage", "2026-10-20", today=today) == (date(2026, 9, 28), date(2026, 10, 20))
    start, end = fam_agenda.parse_range("2026-01-01", "2030-01-01", today=today)
    assert (end - start).days == fam_agenda.MAX_SPAN_DAYS


def test_answer_never_raises():
    seen = {}

    def good(d0, d1, sphere):
        seen.update(d0=d0, d1=d1, sphere=sphere)
        return [{"id": "x:1", "title": "ok", "start": "2026-10-05"}, {"title": "dropped"}]

    body = fam_agenda.answer(good, "2026-10-01", "2026-10-31", "Work", today=date(2026, 10, 5))
    assert body["ok"] is True and [i["id"] for i in body["items"]] == ["x:1"]
    assert seen == {"d0": date(2026, 10, 1), "d1": date(2026, 10, 31), "sphere": "work"}
    assert (body["from"], body["to"], body["sphere"]) == ("2026-10-01", "2026-10-31", "work")

    def boom(*a):
        raise RuntimeError("db locked")

    bad = fam_agenda.answer(boom)
    assert bad["ok"] is False and "db locked" in bad["error"] and bad["items"] == []
    assert fam_agenda.answer(lambda *a: 12)["items"] == []          # a provider returning junk is an empty agenda


# ---- FastAPI ---------------------------------------------------------------------------

@pytest.fixture
def fastapi_app(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    data = tmp_path / "data"
    data.mkdir()
    token_file = data / "mcp-token"
    token_file.write_text("secret-token\n", encoding="utf-8")
    family.configure("kafka", str(data), enabled=False)
    app = FastAPI()
    return app, TestClient, token_file


def test_install_fastapi_route(fastapi_app):
    app, TestClient, token_file = fastapi_app
    calls = []

    def provider(d0, d1, sphere):
        calls.append((d0, d1, sphere))
        return [{"id": "7", "title": "Renew", "start": date(2026, 10, 5), "kind": "deadline"},
                {"title": "bad"}, {"id": "k:2", "title": "Call", "start": "2026-10-06T09:30:00+02:00"}]

    # a SPA catch-all registered BEFORE the helper: the agenda route must still win
    @app.get("/{full_path:path}")
    def spa(full_path: str):
        return {"index": True}

    assert fam_agenda.install_fastapi(app, provider) == {"installed": True, "path": "/api/family/agenda"}
    c = TestClient(app)
    assert c.get("/api/family/agenda").status_code == 401
    assert c.get("/api/family/agenda", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/family/agenda", headers={"Authorization": "secret-token"}).status_code == 401   # not a bearer header
    h = {"Authorization": "Bearer secret-token"}
    r = c.get("/api/family/agenda?from=2026-10-01&to=2026-10-31&sphere=Work", headers=h)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert [i["id"] for i in body["items"]] == ["kafka:7", "k:2"]
    assert body["items"][0]["start"] == "2026-10-05" and body["items"][1]["all_day"] is False
    assert calls[-1] == (date(2026, 10, 1), date(2026, 10, 31), "work")
    # defaults when from/to are missing or garbage: today-7 … today+60
    r = c.get("/api/family/agenda?from=banana", headers=h)
    d0, d1, sphere = calls[-1]
    assert r.status_code == 200 and sphere == "" and (d0, d1) == (date.today() - timedelta(days=7), date.today() + timedelta(days=60))
    # the token file is read at request time: a rotated token works, the old one does not
    token_file.write_text("rotated", encoding="utf-8")
    assert c.get("/api/family/agenda", headers=h).status_code == 401
    assert c.get("/api/family/agenda", headers={"Authorization": "Bearer rotated"}).status_code == 200


def test_install_fastapi_provider_errors_never_500(fastapi_app):
    app, TestClient, token_file = fastapi_app

    def boom(*a):
        raise ValueError("no such table")

    fam_agenda.install_fastapi(app, boom)
    r = TestClient(app).get("/api/family/agenda", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and "no such table" in r.json()["error"] and r.json()["items"] == []


def test_install_fastapi_async_provider_and_custom_path(fastapi_app):
    app, TestClient, token_file = fastapi_app

    async def provider(d0, d1, sphere):
        return {"items": [{"id": "a:1", "title": "async", "start": "2026-10-05"}]}

    fam_agenda.install_fastapi(app, provider, path="/api/x/agenda", token_file=str(token_file))
    r = TestClient(app).get("/api/x/agenda", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200 and r.json()["items"][0]["title"] == "async"


def test_install_fastapi_refuses_without_a_token_configured(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    family.configure("kafka", str(tmp_path / "empty"), enabled=False)       # token file does not exist
    app = FastAPI()
    fam_agenda.install_fastapi(app, lambda *a: [])
    c = TestClient(app)
    assert c.get("/api/family/agenda", headers={"Authorization": "Bearer "}).status_code == 401
    assert c.get("/api/family/agenda", headers={"Authorization": "Bearer anything"}).status_code == 401


# ---- the Node twin ---------------------------------------------------------------------

NODE_SCRIPT = r"""
const m = await import(process.argv[2]);
m.configure({ app: "kafka", dataDir: process.argv[3] });
const out = {};
out.norm = m.agendaNormalizeItem({ id: "7", title: " Renew  the lease ", start: "2026-10-05", kind: "Deadline", priority: "HIGH",
  url: "http://x/y", detail: "d", sphere: "Work" }, { app: "kafka" });
out.timed = m.agendaNormalizeItem({ title: "Call", start: "2026-10-05T09:30:00Z", end: "2026-10-05T10:00:00Z" });
out.zone = m.agendaNormalizeItem({ title: "Call", start: "2026-10-05 09:30:15+0200" }).start;
out.date_obj = m.agendaNormalizeItem({ title: "D", start: new Date("2026-10-05T07:30:00Z") }).start;
out.bad = [m.agendaNormalizeItem({ start: "2026-10-05" }), m.agendaNormalizeItem({ title: "x", start: "soon" }), m.agendaNormalizeItem("x"),
  m.agendaNormalizeItem({ title: "x", start: "2026-02-30" })];
out.js_url = m.agendaNormalizeItem({ title: "x", start: "2026-10-05", url: "javascript:alert(1)" }).url;
out.noid = m.agendaNormalizeItem({ title: "Pay", start: "2026-10-05" }, { app: "ledger" }).id.startsWith("ledger:other:");
out.range = [m.agendaRange("2026-10-10", "2026-10-01"), m.agendaRange("x", "2026-10-20", new Date(2026, 9, 5)),
  m.agendaRange("2026-01-01", "2030-01-01")];
out.items = m.agendaNormalizeItems([{ id: "a:1", title: "one", start: "2026-10-05" }, { id: "a:1", title: "dup", start: "2026-10-06" }, { title: "x" }]).map((i) => i.title);
out.boom = await m.agendaAnswer(() => { throw new Error("db locked"); }, "2026-10-01", "2026-10-31", "Work");
out.ok = await m.agendaAnswer(async (f, t, s) => [{ id: "x:1", title: `${f}|${t}|${s}`, start: "2026-10-05" }], "2026-10-01", "2026-10-31", "Work");

const routes = {};
const fake = { get(path, handler) { routes[path] = handler; } };
out.installed = m.installAgenda(fake, async () => [{ id: "n:1", title: "n", start: "2026-10-05" }]);
async function call(headers, query) {
  const res = { code: 200, body: null, status(c) { this.code = c; return this; }, json(b) { this.body = b; return this; } };
  await routes["/api/family/agenda"]({ headers, query }, res);
  return [res.code, res.body.ok, (res.body.items || []).length];
}
out.http = [await call({}, {}), await call({ authorization: "Bearer nope" }, {}), await call({ authorization: "Bearer js-secret" }, { from: "2026-10-01", to: "2026-10-31" })];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_install_agenda(tmp_path):
    base = (ROOT / "js" / "hoard-link.js").read_text(encoding="utf-8")
    merged = base
    (tmp_path / "hoard-link.mjs").write_text(merged, encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "mcp-token").write_text("js-secret\n", encoding="utf-8")
    script = tmp_path / "t.mjs"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    run = subprocess.run(["node", str(script), (tmp_path / "hoard-link.mjs").as_uri(), str(data)], capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["norm"] == {"id": "kafka:7", "title": "Renew the lease", "start": "2026-10-05", "all_day": True, "kind": "deadline",
                           "priority": "high", "url": "http://x/y", "detail": "d", "sphere": "work"}
    assert (out["timed"]["start"], out["timed"]["end"], out["timed"]["all_day"]) == (
        "2026-10-05T09:30:00+00:00", "2026-10-05T10:00:00+00:00", False)
    assert out["zone"] == "2026-10-05T09:30:15+02:00"
    assert out["date_obj"] == "2026-10-05T07:30:00+00:00"
    assert out["bad"] == [None, None, None, None] and out["js_url"] == "" and out["noid"] is True
    assert out["range"][0] == ["2026-10-01", "2026-10-10"] and out["range"][1] == ["2026-09-28", "2026-10-20"]
    assert out["range"][2][0] == "2026-01-01" and out["range"][2][1] == "2027-02-05"
    assert out["items"] == ["one"]
    assert out["boom"]["ok"] is False and "db locked" in out["boom"]["error"] and out["boom"]["items"] == []
    assert out["ok"]["items"][0]["title"] == "2026-10-01|2026-10-31|work"
    assert out["installed"] == {"installed": True, "path": "/api/family/agenda"}
    assert out["http"] == [[401, False, 0], [401, False, 0], [200, True, 1]]
