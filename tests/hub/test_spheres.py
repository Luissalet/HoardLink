"""The spheres facet: storage and validation, routing of accounts / chats / apps, classification,
quiet hours, the HTTP routes (through the real server) and the agent tools."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from hoard_link.hub import facets as facets_mod
from hoard_link.hub import tools
from hoard_link.hub.server import make_server
from hoard_link.hub.spheres import (NOTIFY_CHANNELS, SpheresFacet, blank_sphere, default_spheres, normalize_sphere,
                                    parse_days, sender_matches)

from .test_hub_and_server import _http

UI = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def sp(hub) -> SpheresFacet:
    f = hub.facet("spheres")
    assert isinstance(f, SpheresFacet)
    return f


@pytest.fixture
def served(hub, family):
    # an app with its own token (so "any app may switch the sphere, only the hub may edit" can be tested)
    app = hub.get("fake")
    Path(app.token_file).parent.mkdir(parents=True, exist_ok=True)
    Path(app.token_file).write_text("fake-app-token", encoding="utf-8")
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, hub.config.url, {"Authorization": "Bearer " + hub.token}, {"Authorization": "Bearer fake-app-token"}
    server.shutdown()


# ---- defaults, storage ----------------------------------------------------------------------------------

def test_defaults_are_personal_and_work(sp, hub):
    assert sp.active() == "personal"
    assert [s["id"] for s in sp.list()] == ["personal", "work"]
    personal = sp.get("personal")
    assert personal["mail_accounts"] == ["*"] and personal["apps"] == ["*"]
    assert personal["name"] == {"es": "Personal", "en": "Personal"}
    assert sp.get("work")["quiet_hours"] == {"start": "19:00", "end": "08:30", "days": "daily"}
    assert sp.get("nope") is None
    assert not (Path(hub.config.data_dir) / "spheres.json").exists()      # nothing written until someone changes it


def test_notify_channels_match_the_notify_facet():
    from hoard_link.hub.notify import CHANNELS
    assert set(NOTIFY_CHANNELS) == set(CHANNELS) | {"digest"}


def test_get_returns_copies(sp):
    s = sp.get("personal")
    s["vip"].append("x@y.z")
    s["name"]["es"] = "Otro"
    assert sp.get("personal")["vip"] == [] and sp.get("personal")["name"]["es"] == "Personal"
    sp.list()[0]["id"] = "zzz"
    assert sp.list()[0]["id"] == "personal"


def test_set_active_persists_and_emits(sp, hub):
    res = sp.set_active("work")
    assert res == {"ok": True, "active": "work", "from": "personal", "changed": True}
    assert sp.active() == "work"
    ev = hub.events.query(type="hub.sphere.changed")
    assert len(ev) == 1 and ev[0]["data"] == {"from": "personal", "to": "work"}
    # same value: nothing emitted
    assert sp.set_active("work")["changed"] is False
    assert len(hub.events.query(type="hub.sphere.changed")) == 1
    # persisted: a new facet over the same data dir sees it
    assert SpheresFacet(hub).active() == "work"
    bad = sp.set_active("nope")
    assert bad["ok"] is False and bad["status"] == 404 and "work" in bad["spheres"]


def test_file_edit_is_picked_up_and_corrupt_file_falls_back(sp, hub):
    path = Path(hub.config.data_dir) / "spheres.json"
    path.write_text(json.dumps({"active": "work", "spheres": [{"id": "work", "name": "Curro", "vip": "boss@corp.com, @corp.com"}]}),
                    encoding="utf-8")
    assert sp.active() == "work"
    assert sp.get("work")["name"] == {"es": "Curro", "en": "Curro"}
    assert sp.get("work")["vip"] == ["boss@corp.com", "@corp.com"]
    assert sp.get("personal") is not None            # personal can never be missing
    path.write_text("{ not json", encoding="utf-8")
    assert sp.active() == "personal" and [s["id"] for s in sp.list()] == ["personal", "work"]
    path.write_text(json.dumps({"active": "ghost", "spheres": [{"id": "BAD ID"}, {"id": "home"}]}), encoding="utf-8")
    assert sp.active() == "personal"
    assert [s["id"] for s in sp.list()] == ["personal", "home"]


# ---- validation ------------------------------------------------------------------------------------------

def test_normalize_sphere_accepts_strings_and_fills_defaults():
    s, errs = normalize_sphere({"id": "Family", "name": "Familia", "vip": "a@b.c; @d.e", "color": "#ABC"})
    assert not errs
    assert s["id"] == "family" and s["name"] == {"es": "Familia", "en": "Familia"} and s["color"] == "#abc"
    assert s["vip"] == ["a@b.c", "@d.e"] and s["apps"] == [] and s["notify"]["low"] == ["digest"]
    assert list(s) == ["id", "name", "color", "mail_accounts", "chat_sources", "vip", "keywords", "mute",
                       "quiet_hours", "notify", "digest", "apps"]


@pytest.mark.parametrize("raw,fragment", [
    ({"id": "bad id!"}, "id:"),
    ({"id": "x", "color": "red"}, "color"),
    ({"id": "x", "quiet_hours": {"start": "25:00"}}, "quiet_hours.start"),
    ({"id": "x", "quiet_hours": {"days": "someday"}}, "quiet_hours.days"),
    ({"id": "x", "notify": {"urgent": ["pigeon"]}}, "unknown channel"),
    ({"id": "x", "notify": {"critical": ["windows"]}}, "unknown priority"),
    ({"id": "x", "digest": {"at": "8am"}}, "digest.at"),
    ({"id": "x", "digest": {"channels": ["digest"]}}, "digest.channels"),
    ({"id": "x", "vip": 5}, "vip"),
    ({"id": "x", "name": {}}, "name"),
])
def test_normalize_sphere_rejects(raw, fragment):
    _, errs = normalize_sphere(raw)
    assert any(fragment in e for e in errs), errs


def test_parse_days():
    assert parse_days("daily") == set(range(7))
    assert parse_days("weekdays") == {0, 1, 2, 3, 4}
    assert parse_days("weekends") == {5, 6}
    assert parse_days(["mon", "Miércoles", "sun"]) == {0, 2, 6}
    assert parse_days("lun, vie") == {0, 4}


def test_upsert_merges_into_existing_and_adds_new(sp):
    res = sp.upsert({"id": "work", "vip": ["boss@corp.com"]})
    assert res["ok"] and res["sphere"]["vip"] == ["boss@corp.com"]
    assert sp.get("work")["keywords"][0] == "urgente"          # untouched keys survive a partial update
    res = sp.upsert({"id": "family", "name": {"es": "Familia", "en": "Family"}, "color": "#10aa55", "apps": ["homehoard"]})
    assert res["ok"] and [s["id"] for s in sp.list()] == ["personal", "work", "family"]
    assert sp.get("family")["apps"] == ["homehoard"]
    bad = sp.upsert({"id": "family", "color": "nope"})
    assert bad["ok"] is False and bad["status"] == 400 and sp.get("family")["color"] == "#10aa55"
    assert sp.upsert("nope")["status"] == 400


def test_save_all_replaces_and_keeps_personal(sp, hub):
    sp.set_active("work")
    work = sp.get("work")
    res = sp.save_all([work, {"id": "side", "name": "Side"}])
    assert res["ok"] and [s["id"] for s in res["spheres"]] == ["personal", "work", "side"]
    # dropping the active sphere falls back to personal and says so
    res = sp.save_all([sp.get("personal"), sp.get("side")])
    assert res["ok"] and res["active"] == "personal"
    assert [e["data"] for e in hub.events.query(type="hub.sphere.changed")][0] == {"from": "work", "to": "personal"}
    assert sp.save_all([])["status"] == 400
    dup = sp.save_all([sp.get("personal"), sp.get("personal")])
    assert dup["ok"] is False and "duplicated" in dup["error"]


def test_remove(sp):
    sp.upsert({"id": "side"})
    sp.set_active("side")
    assert sp.remove("personal")["status"] == 400
    assert sp.remove("ghost")["status"] == 404
    res = sp.remove("side")
    assert res["ok"] and res["active"] == "personal" and sp.ids() == ["personal", "work"]


# ---- routing of accounts, chats, apps --------------------------------------------------------------------------

def test_sphere_of_account(sp):
    # nothing claimed: the sphere with "*" (personal)
    assert sp.sphere_of_account("gmail-main", "me@gmail.com") == "personal"
    sp.upsert({"id": "work", "mail_accounts": ["corp", "ME@Corp.com"]})
    assert sp.sphere_of_account("corp") == "work"                       # by selector
    assert sp.sphere_of_account("whatever", "me@corp.com") == "work"    # by address, case-insensitive
    assert sp.sphere_of_account("gmail-main", "me@gmail.com") == "personal"
    assert sp.sphere_of_account("") == "personal"
    # without any "*" the default is still personal
    sp.upsert({"id": "personal", "mail_accounts": []})
    assert sp.sphere_of_account("unknown") == "personal"
    # "*" on another sphere takes over the unclaimed accounts
    sp.upsert({"id": "work", "mail_accounts": ["corp", "*"]})
    assert sp.sphere_of_account("unknown") == "work"


def test_sphere_of_chat_source(sp):
    assert sp.sphere_of_chat_source("slack-acme") == "personal"
    sp.upsert({"id": "work", "chat_sources": ["Slack-Acme"]})
    assert sp.sphere_of_chat_source("slack-acme") == "work"
    assert sp.sphere_of_chat_source("other") == "personal"


def test_app_allowed_and_sphere_of_app(sp):
    assert sp.app_allowed("personal", "ledger") is True       # "*"
    assert sp.app_allowed("work", "ledger") is False          # work mail never reaches Ledger
    assert sp.app_allowed("ghost", "ledger") is False
    sp.upsert({"id": "work", "apps": ["Cicero", "kafka"]})
    assert sp.app_allowed("work", "cicero") is True and sp.app_allowed("work", "kafka") is True
    assert sp.sphere_of_app("ledger") == "personal"
    sp.upsert({"id": "personal", "apps": ["ledger"]})
    sp.set_active("work")
    assert sp.sphere_of_app("kafka") == "work"
    assert sp.sphere_of_app("unlisted") == "work"             # no sphere allows it: the active one


# ---- classification ---------------------------------------------------------------------------------------------

def _work(sp, **kw):
    sp.upsert({"id": "work", **kw})


def test_classify_vip(sp):
    _work(sp, vip=["boss@corp.com", "@bigclient.io", "mamá"])
    c = sp.classify("work", sender="Jefe <BOSS@corp.com>", subject="hola")
    assert c == {"priority": "attention", "reasons": ["vip:boss@corp.com"]}
    assert sp.classify("work", sender="ceo@bigclient.io")["priority"] == "attention"
    assert sp.classify("work", sender="x@mail.bigclient.io")["priority"] == "attention"     # subdomain of the VIP domain
    assert sp.classify("work", sender="x@notbigclient.io")["priority"] == "normal"
    assert sp.classify("work", sender="Mamá <a@b.c>")["priority"] == "attention"             # a word of the display name
    assert sp.classify("work", sender="boss@corp.com.evil.net")["priority"] == "normal"


def test_classify_keywords_fold_accents_and_need_whole_words(sp):
    _work(sp, keywords=["urgente", "reunión", "hoy"])
    assert sp.classify("work", sender="a@b.c", subject="URGENTE: servidor")["reasons"] == ["keyword:urgente"]
    assert sp.classify("work", sender="a@b.c", text="Tenemos reunion mañana")["priority"] == "attention"
    assert sp.classify("work", sender="a@b.c", text="el hoyo del jardin")["priority"] == "normal"
    assert sp.classify("work", sender="a@b.c", subject="Informe", text="nada")["priority"] == "normal"


def test_classify_mention_direct_and_mute(sp):
    _work(sp, mute=["newsletter@spam.com", "@promo.net", "oferta"], vip=["boss@corp.com"])
    assert sp.classify("work", sender="a@b.c", mentions_me=True) == {"priority": "attention", "reasons": ["mention"]}
    assert sp.classify("work", sender="a@b.c", direct=True) == {"priority": "attention", "reasons": ["direct"]}
    assert sp.classify("work", sender="Newsletter@spam.com")["priority"] == "low"
    assert sp.classify("work", sender="x@promo.net")["reasons"] == ["mute:@promo.net"]
    assert sp.classify("work", sender="a@b.c", subject="Gran OFERTA")["priority"] == "low"
    # a muted sender stays low even when the text has a keyword ("urgent offer"), a VIP beats the mute
    assert sp.classify("work", sender="newsletter@spam.com", subject="urgent")["priority"] == "low"
    _work(sp, mute=["boss@corp.com"])
    assert sp.classify("work", sender="boss@corp.com")["priority"] == "attention"
    # a keyword beats a muted word
    _work(sp, mute=["oferta"], keywords=["urgent"])
    assert sp.classify("work", sender="a@b.c", subject="urgent oferta")["priority"] == "attention"


def test_classify_unknown_sphere_still_honours_direct(sp):
    assert sp.classify("ghost", sender="a@b.c")["priority"] == "normal"
    assert sp.classify("ghost", sender="a@b.c", direct=True)["priority"] == "attention"


def test_sender_matches():
    assert sender_matches("a@b.c", "Ana <A@B.C>")
    assert sender_matches("@b.c", "x@b.c") and sender_matches("@b.c", "x@sub.b.c") and not sender_matches("@b.c", "x@ab.c")
    assert not sender_matches("", "x@b.c")


# ---- quiet hours ----------------------------------------------------------------------------------------------------

def test_quiet_hours_cross_midnight(sp):
    # personal: 22:30 -> 08:00 daily; 2026-10-05 is a Monday
    q = lambda h, m, day=5: sp.in_quiet_hours("personal", datetime(2026, 10, day, h, m))  # noqa: E731
    assert q(23, 0) and q(2, 0) and q(7, 59) and q(22, 30)
    assert not q(8, 0) and not q(12, 0) and not q(22, 29)


def test_quiet_hours_same_day_window_and_days(sp):
    sp.upsert({"id": "work", "quiet_hours": {"start": "12:00", "end": "14:00", "days": "weekdays"}})
    assert sp.in_quiet_hours("work", datetime(2026, 10, 5, 12, 0))            # Monday
    assert not sp.in_quiet_hours("work", datetime(2026, 10, 5, 14, 0))
    assert not sp.in_quiet_hours("work", datetime(2026, 10, 10, 13, 0))       # Saturday
    # a night window belongs to the day it starts on: Friday 23:00 -> Saturday 02:00 counts, Saturday 23:00 does not
    sp.upsert({"id": "work", "quiet_hours": {"start": "22:00", "end": "06:00", "days": "weekdays"}})
    assert sp.in_quiet_hours("work", datetime(2026, 10, 9, 23, 0))            # Friday night
    assert sp.in_quiet_hours("work", datetime(2026, 10, 10, 2, 0))            # still Friday's window on Saturday 02:00
    assert not sp.in_quiet_hours("work", datetime(2026, 10, 10, 23, 0))       # Saturday night
    assert not sp.in_quiet_hours("work", datetime(2026, 10, 11, 2, 0))        # Sunday 02:00 is Saturday's window


def test_quiet_hours_disabled_and_unknown(sp):
    sp.upsert({"id": "work", "quiet_hours": {"start": "08:00", "end": "08:00", "days": "daily"}})
    assert not sp.in_quiet_hours("work", datetime(2026, 10, 5, 8, 0))
    sp.upsert({"id": "work", "quiet_hours": {"start": "", "end": "", "days": "daily"}})
    assert not sp.in_quiet_hours("work", datetime(2026, 10, 5, 3, 0))
    assert sp.in_quiet_hours("ghost") is False
    assert isinstance(sp.in_quiet_hours("personal"), bool)                      # default now


# ---- HTTP --------------------------------------------------------------------------------------------------------------

def test_http_get_and_facet_listing(served):
    hub, url, hubh, apph = served
    status, body = _http(url + "/api/spheres")
    assert status == 200 and body["ok"] and body["active"] == "personal"
    assert [s["id"] for s in body["spheres"]] == ["personal", "work"]
    status, body = _http(url + "/api/facets")
    mine = next(f for f in body["facets"] if f["id"] == "spheres")
    assert mine["ui_scripts"] == ["spheres.js"]
    # the script is served and is valid-looking JS
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    js = opener.open(url + "/ui/spheres.js", timeout=10).read().decode("utf-8")
    assert 'H.register("spheres"' in js and 'placement: "header"' in js


def test_http_edit_needs_hub_or_ui(served):
    hub, url, hubh, apph = served
    sphere = {"id": "family", "name": "Familia", "color": "#119955"}
    status, body = _http(url + "/api/spheres", {"sphere": sphere})
    assert status == 401 and "token" in body["error"]
    status, body = _http(url + "/api/spheres", {"sphere": sphere}, headers=apph)
    assert status == 403
    status, body = _http(url + "/api/spheres", {"sphere": sphere}, headers=hubh)
    assert status == 200 and body["ok"] and [s["id"] for s in body["spheres"]][-1] == "family"
    # the page itself (same-origin, no token) may edit too
    status, body = _http(url + "/api/spheres", {"sphere": {"id": "family", "vip": ["a@b.c"]}}, headers=UI)
    assert status == 200 and next(s for s in body["spheres"] if s["id"] == "family")["vip"] == ["a@b.c"]
    status, body = _http(url + "/api/spheres", {"sphere": {"id": "family", "color": "bad"}}, headers=hubh)
    assert status == 400 and "color" in body["error"]
    status, body = _http(url + "/api/spheres", {"spheres": [{"id": "work"}, {"id": "x2"}]}, headers=hubh)
    assert status == 200 and [s["id"] for s in body["spheres"]] == ["personal", "work", "x2"]
    status, body = _http(url + "/api/spheres", {"oops": 1}, headers=hubh)
    assert status == 400


def test_http_switch_by_any_family_token(served):
    hub, url, hubh, apph = served
    status, body = _http(url + "/api/spheres/active", {"id": "work"})
    assert status == 401
    status, body = _http(url + "/api/spheres/active", {"id": "work"}, headers=apph)       # Faustus / an app switches it
    assert status == 200 and body["active"] == "work"
    assert _http(url + "/api/spheres")[1]["active"] == "work"
    status, body = _http(url + "/api/spheres/active", {"id": "ghost"}, headers=apph)
    assert status == 404
    ev = hub.events.query(type="hub.sphere.changed")
    assert ev[0]["data"] == {"from": "personal", "to": "work"}


def test_http_remove(served):
    hub, url, hubh, apph = served
    _http(url + "/api/spheres", {"sphere": {"id": "family"}}, headers=hubh)
    assert _http(url + "/api/spheres/remove", {"id": "family"}, headers=apph)[0] == 403
    status, body = _http(url + "/api/spheres/remove", {"id": "personal"}, headers=hubh)
    assert status == 400
    status, body = _http(url + "/api/spheres/remove", {"id": "family"}, headers=hubh)
    assert status == 200 and body["removed"] == "family"
    assert _http(url + "/api/spheres/remove", {"id": "family"}, headers=hubh)[0] == 404


def test_the_server_never_returns_other_routes_as_ours(served):
    hub, url, hubh, apph = served
    assert _http(url + "/api/spheres/unknown", {}, headers=hubh)[0] == 404


# ---- tools ---------------------------------------------------------------------------------------------------------------

def test_tools_are_catalogued_and_described_properly():
    cat = {t["name"]: t for t in SpheresFacet.tools()}
    assert set(cat) == {"hub_spheres", "hub_sphere_set"}
    for t in cat.values():
        assert len(t["description"].split("\n")[0]) <= 110
    assert cat["hub_spheres"]["annotations"] == {"readOnlyHint": True}
    assert "annotations" not in cat["hub_sphere_set"]
    assert {t["name"] for t in tools.all_tools()} >= {"hub_spheres", "hub_sphere_set"}


def test_tools_run_through_the_hub(hub):
    res = tools.call(hub, "hub_spheres", {})
    assert res["ok"] and res["active"] == "personal" and len(res["spheres"]) == 2
    res = tools.call(hub, "hub_sphere_set", {"id": "work"})
    assert res["ok"] and res["active"] == "work"
    res = tools.call(hub, "hub_sphere_set", {"id": "nope"})
    assert res["ok"] is False


def test_tools_over_agent_call(served):
    hub, url, hubh, apph = served
    status, body = _http(url + "/api/agent/call", {"name": "hub_sphere_set", "arguments": {"id": "work"}}, headers=hubh)
    assert status == 200 and body["result"]["active"] == "work"
    status, body = _http(url + "/api/agent/call", {"name": "hub_sphere_set", "arguments": {"id": "nope"}}, headers=hubh)
    assert status == 400


def test_defaults_are_independent_copies():
    a, b = default_spheres(), default_spheres()
    a[0]["vip"].append("x")
    assert b[0]["vip"] == [] and blank_sphere("z")["vip"] == []
    assert facets_mod.FACET_MODULES.index("notify") < facets_mod.FACET_MODULES.index("spheres")
