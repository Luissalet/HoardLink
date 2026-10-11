"""Browser lanes (Agora #265): the arbiter (serialising, expiry, holders, profiles), the HTTP endpoints and hub tools, the
``lane()`` client (including the hub being down) and the Hub UI panel."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request

import pytest

from hoard_link import LaneBusy, LaneError, lane, lane_status, registrable_domain
from hoard_link import browser_lane as bl
from hoard_link.hub import tools
from hoard_link.hub.browser_lanes import TICKET_GRACE_S, LaneArbiter
from hoard_link.hub.browser_lanes import LaneError as ArbiterError
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server

from .conftest import free_port
from .test_hub_and_server import _http
from ._hub_fakes import wait_for


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path=None, clock=None, alive=None):
    return LaneArbiter(str(tmp_path / "browser-lanes.json") if tmp_path else None, now=clock or Clock(),
                       alive=alive or (lambda pid, created: True))


# ---- the lane key ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("shop.example.co.uk", "example.co.uk"),
    ("https://www.Tienda.com:8443/p/1?x=1", "tienda.com"),
    ("a.b.c.example.com", "example.com"),
    ("shop.example.com.es", "example.com.es"),
    ("example.com", "example.com"),
    ("tienda.es", "tienda.es"),
    ("//deep.name.shop.com.br/x", "shop.com.br"),
    ("acme.myshopify.com", "acme.myshopify.com"),
    ("www.acme.myshopify.com", "acme.myshopify.com"),
    ("http://127.0.0.1:5000/x", "127.0.0.1:5000"),          # a dev server per port is a site of its own
    ("http://127.0.0.1:5001/x", "127.0.0.1:5001"),
    ("http://127.0.0.1/x", "127.0.0.1"),
    ("localhost:8080", "localhost:8080"),
    ("localhost:9090", "localhost:9090"),
    ("localhost", "localhost"),
    ("http://localhost:80/", "localhost"),                   # the scheme's default port is no port
    ("https://localhost:443/", "localhost"),
    ("[::1]:80", "[::1]:80"),
    ("::1", "::1"),                                          # a bare IPv6 literal
    ("[::1]", "::1"),
    ("http://[::1]:9000/x", "[::1]:9000"),
    ("b\u00fccher.de", "xn--bcher-kva.de"),                  # an international name and its punycode form: one lane
    ("shop.b\u00fccher.de", "xn--bcher-kva.de"),
    ("https://www.xn--bcher-kva.de/x", "xn--bcher-kva.de"),
    ("B\u00dcCHER.de", "xn--bcher-kva.de"),
    ("tienda.uk.com", "tienda.uk.com"),                      # rented suffixes keep the tenant
    ("www.tienda.uk.com", "tienda.uk.com"),
    ("a.b.proyecto.eu.org", "proyecto.eu.org"),
    ("", ""),
])
def test_registrable_domain(value, expected):
    assert registrable_domain(value) == expected


def test_a_lane_needs_a_domain_or_a_profile():
    arb = make()
    with pytest.raises(ArbiterError):
        arb.acquire(domain="", holder="x")
    with pytest.raises(LaneError):
        lane("", holder="x")


# ---- the arbiter ----------------------------------------------------------------------------------------

def test_busy_answer_says_who_holds_it_and_since_when():
    clock = Clock()
    arb = make(clock=clock)
    a = arb.acquire("https://shop.example.co.uk/x", holder="tantalus", purpose="reading prices", ttl_s=60)
    assert a["granted"] and a["domain"] == "example.co.uk" and a["lease_id"]
    clock.t += 7
    b = arb.acquire("other.example.co.uk", holder="faustus", wait_s=0)
    assert b["granted"] is False and b["busy"] is True
    assert b["held_by"] == "tantalus" and b["since"] == 1000.0 and b["purpose"] == "reading prices"
    assert b["expires_in_s"] == 53 and b["domain"] == "example.co.uk"


def test_same_domain_serialises_and_different_domains_do_not():
    arb = make()
    a = arb.acquire("a.example.com", holder="one", ttl_s=60)
    other = arb.acquire("another-shop.org", holder="two", ttl_s=60)          # a different site: at once
    assert a["granted"] and other["granted"]
    got: dict = {}

    def second():
        t0 = time.monotonic()
        got["res"] = arb.acquire("www.example.com", holder="three", wait_s=10, ttl_s=60)
        got["waited"] = time.monotonic() - t0

    th = threading.Thread(target=second)
    th.start()
    assert wait_for(lambda: len(arb.status()["waiting"]) == 1)
    assert arb.status()["waiting"][0]["holder"] == "three"
    time.sleep(0.3)
    assert "res" not in got                                                  # still waiting on "one"
    assert arb.release(a["lease_id"], holder="one")["released"] is True
    th.join(5)
    assert got["res"]["granted"] is True and got["waited"] >= 0.25
    assert [l["holder"] for l in arb.status()["lanes"]] == ["two", "three"]


def test_waiters_are_served_in_arrival_order():
    arb = make()
    a = arb.acquire("shop.com", holder="first", ttl_s=60)
    order: list[str] = []

    def waiter(name):
        res = arb.acquire("shop.com", holder=name, wait_s=10, ttl_s=60)
        assert res["granted"]
        order.append(name)
        time.sleep(0.05)
        arb.release(res["lease_id"], holder=name)

    threads = []
    for name in ("second", "third", "fourth"):
        th = threading.Thread(target=waiter, args=(name,))
        th.start()
        threads.append(th)
        assert wait_for(lambda n=len(threads): len(arb.status()["waiting"]) == n)
    arb.release(a["lease_id"], holder="first")
    for th in threads:
        th.join(5)
    assert order == ["second", "third", "fourth"]


def test_expiry_frees_the_lane():
    clock = Clock()
    arb = make(clock=clock)
    events: list = []
    arb.on_event = lambda kind, lane_: events.append((kind, lane_["holder"], lane_.get("reason")))
    a = arb.acquire("shop.com", holder="crashed", ttl_s=30)
    assert arb.acquire("shop.com", holder="next", wait_s=0)["busy"] is True
    clock.t += 31                                                             # the holder never renewed
    b = arb.acquire("shop.com", holder="next", wait_s=0, ttl_s=30)
    assert b["granted"] is True
    assert ("expired", "crashed", "ttl") in events and ("acquired", "next", None) in events
    assert arb.renew(a["lease_id"], holder="crashed")["status"] == 404         # the old lease is gone
    assert arb.status()["reaped"][-1]["holder"] == "crashed"


def test_a_waiter_gets_the_lane_when_the_holder_expires_in_real_time():
    arb = LaneArbiter(None)
    arb.acquire("shop.com", holder="crashed", ttl_s=1)
    t0 = time.monotonic()
    b = arb.acquire("shop.com", holder="next", wait_s=10, ttl_s=30)
    assert b["granted"] is True and 0.8 <= time.monotonic() - t0 < 5


def test_renew_keeps_a_lane_and_only_the_holder_can():
    clock = Clock()
    arb = make(clock=clock)
    a = arb.acquire("shop.com", holder="tantalus", ttl_s=30)
    clock.t += 20
    r = arb.renew(a["lease_id"], holder="tantalus")
    assert r["ok"] and r["expires_at"] == 1050.0
    clock.t += 20                                                             # 40 s after the grant, but renewed at 20
    assert arb.acquire("shop.com", holder="other", wait_s=0)["busy"] is True
    refused = arb.renew(a["lease_id"], holder="other")
    assert refused["ok"] is False and refused["status"] == 403 and refused["held_by"] == "tantalus"
    assert arb.renew("nope", holder="x")["status"] == 404


def test_release_by_the_wrong_holder_is_refused():
    arb = make()
    a = arb.acquire("shop.com", holder="tantalus", ttl_s=60)
    bad = arb.release(a["lease_id"], holder="faustus")
    assert bad["ok"] is False and bad["status"] == 403 and "tantalus" in bad["error"]
    assert arb.release(a["lease_id"])["status"] == 403                          # no holder named: refused too
    assert len(arb.status()["lanes"]) == 1
    assert arb.acquire("shop.com", holder="faustus", wait_s=0)["busy"] is True
    forced = arb.release(a["lease_id"], force=True)                              # the hub UI's override
    assert forced["released"] is True
    assert arb.acquire("shop.com", holder="faustus", wait_s=0)["granted"] is True
    ok = make()
    b = ok.acquire("x.com", holder="me")
    assert ok.release(b["lease_id"], holder="me")["released"] is True
    assert ok.release(b["lease_id"], holder="me") == {"ok": True, "released": False, "lease_id": b["lease_id"],
                                                      "detail": "already gone"}


def test_a_browser_profile_cannot_be_shared_by_two_holders(tmp_path):
    arb = make()
    prof = str(tmp_path / "edge-profile")
    a = arb.acquire("shop-one.com", holder="tantalus", profile=prof, ttl_s=60)
    assert a["granted"] and a["profile"]
    same_profile = arb.acquire("shop-two.com", holder="faustus", profile=prof + "/", wait_s=0)
    assert same_profile["busy"] is True and same_profile["held_by"] == "tantalus"
    other_profile = arb.acquire("shop-two.com", holder="faustus", profile=str(tmp_path / "second"), wait_s=0, ttl_s=60)
    assert other_profile["granted"] is True
    # a request for both keys takes both or neither
    both = arb.acquire("shop-one.com", holder="argus", profile=str(tmp_path / "third"), wait_s=0)
    assert both["busy"] is True and not any("third" in l["profile"] for l in arb.status()["lanes"])
    profile_only = arb.acquire("", holder="phileas", profile=prof, wait_s=0)
    assert profile_only["busy"] is True
    assert arb.release(a["lease_id"], holder="tantalus")["released"]
    assert arb.acquire("", holder="phileas", profile=prof, wait_s=0, ttl_s=60)["granted"] is True


def test_a_holder_whose_process_is_gone_loses_the_lane_at_once():
    dead: set = set()
    arb = make(alive=lambda pid, created: pid not in dead)
    a = arb.acquire("shop.com", holder="app", pid=4242, ttl_s=3600)
    assert a["granted"] and len(arb.status()["lanes"]) == 1
    dead.add(4242)
    assert arb.status()["lanes"] == [] and arb.status()["reaped"][-1]["reason"] == "owner process gone"
    assert arb.acquire("shop.com", holder="x", pid=4242)["status"] == 404         # a dead pid cannot even ask


def test_lanes_survive_a_hub_restart(tmp_path):
    clock = Clock()
    first = make(tmp_path, clock)
    a = first.acquire("shop.com", holder="tantalus", purpose="reads", ttl_s=60)
    second = make(tmp_path, clock)
    row = second.status()["lanes"][0]
    assert row["holder"] == "tantalus" and row["lease_id"] == a["lease_id"] and row["domain"] == "shop.com"
    assert second.acquire("shop.com", holder="other", wait_s=0)["busy"] is True
    clock.t += 61
    assert make(tmp_path, clock).status()["lanes"] == []                          # and it still expires


def test_a_waiter_that_gives_up_leaves_the_queue():
    arb = make()
    arb.acquire("shop.com", holder="a", ttl_s=60)
    res = arb.acquire("shop.com", holder="b", wait_s=0.3)
    assert res["busy"] and res["waited_s"] >= 0.25
    assert arb.status()["waiting"] == []


# ---- over HTTP, the agent tools and the UI -------------------------------------------------------------

@pytest.fixture
def live_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", str(tmp_path / "data" / "mcp-token"))
    monkeypatch.delenv("HOARD_TOKEN_FILE", raising=False)
    monkeypatch.setattr("hoard_link.browser_lane._family_token_file", lambda: "")             # not whatever descriptor another test left
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "data"), roots=[str(tmp_path / "none")], icon_dirs=[],
                    faustus_urls=["http://127.0.0.1:1"])
    hub = Hub(cfg)
    server = make_server(hub, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, cfg.url
    server.shutdown()


def _auth(hub):
    return {"Authorization": "Bearer " + hub.token}


def test_http_endpoints(live_hub):
    hub, url = live_hub
    h = _auth(hub)
    status, a = _http(url + "/api/lane/acquire", {"domain": "https://www.shop.example.com/x", "holder": "tantalus",
                                                  "purpose": "prices", "ttl_s": 60}, headers=h)
    assert status == 200 and a["granted"] and a["domain"] == "example.com"
    status, b = _http(url + "/api/lane/acquire", {"domain": "example.com", "holder": "faustus", "wait_s": 0.2}, headers=h)
    assert status == 200 and b["granted"] is False and b["busy"] and b["held_by"] == "tantalus"
    assert "blocked_by_lease" not in b and a["lease_id"] not in json.dumps(b)           # a busy answer leaks no lease id
    status, st = _http(url + "/api/lane", headers=h)
    assert status == 200 and st["lanes"][0]["holder"] == "tantalus" and st["lanes"][0]["domain"] == "example.com"
    status, r = _http(url + "/api/lane/renew", {"lease_id": a["lease_id"], "holder": "tantalus", "ttl_s": 120}, headers=h)
    assert status == 200 and r["lane"]["ttl_s"] == 120
    status, bad = _http(url + "/api/lane/release", {"lease_id": a["lease_id"], "holder": "faustus"}, headers=h)
    assert status == 403 and "tantalus" in bad["error"]
    assert _http(url + "/api/lane/renew", {"lease_id": "nope"}, headers=h)[0] == 404
    status, err = _http(url + "/api/lane/acquire", {"holder": "x"}, headers=h)
    assert status == 400 and "domain" in err["error"]
    status, _ = _http(url + "/api/lane/acquire", {"domain": "x.com"}, headers={**h, "Sec-Fetch-Site": "cross-site",
                                                                              "Sec-Fetch-Mode": "cors"})
    assert status == 403                                                         # a web page cannot take a lane
    threading.Timer(0.3, lambda: _http(url + "/api/lane/release", {"lease_id": a["lease_id"], "holder": "tantalus"},
                                       headers=h)).start()
    status, c = _http(url + "/api/lane/acquire", {"domain": "example.com", "holder": "faustus", "wait_s": 5}, headers=h)
    assert status == 200 and c["granted"] is True
    import os
    assert os.path.isfile(hub.config.lanes_file)


def test_every_lane_write_needs_a_family_token(live_hub):
    """P2 of the review: without a token nobody can take, renew, release or force-release a lane."""
    hub, url = live_hub
    _, a = _http(url + "/api/lane/acquire", {"domain": "shop.com", "holder": "tantalus", "ttl_s": 60}, headers=_auth(hub))
    assert a["granted"]
    for path, body in (("/api/lane/acquire", {"domain": "other.com", "holder": "x"}),
                       ("/api/lane/renew", {"lease_id": a["lease_id"], "holder": "tantalus"}),
                       ("/api/lane/release", {"lease_id": a["lease_id"], "holder": "whatever", "force": True}),
                       ("/api/lane/release", {"lease_id": a["lease_id"], "holder": "tantalus"})):
        status, reply = _http(url + path, body)
        assert status == 401, (path, status, reply)
        status, _ = _http(url + path, body, headers={"Authorization": "Bearer not-a-token"})
        assert status == 401
    assert hub.lanes.status()["lanes"][0]["holder"] == "tantalus"                  # still held
    status, anon = _http(url + "/api/lane")                                          # reading is open, but gives no lease ids
    assert status == 200 and anon["lanes"][0]["holder"] == "tantalus"
    assert "lease_id" not in anon["lanes"][0] and "id" not in anon["lanes"][0] and a["lease_id"] not in json.dumps(anon)
    _, authed = _http(url + "/api/lane", headers=_auth(hub))
    assert authed["lanes"][0]["lease_id"] == a["lease_id"]


def test_a_lane_is_bound_to_the_token_that_took_it(live_hub, monkeypatch):
    hub, url = live_hub
    owners = {"tok-tantalus": "tantalus", "tok-argus": "argus", hub.token: "hub"}
    monkeypatch.setattr(hub, "token_owner", lambda t: owners.get(t))
    tan, argus, hubtok = ({"Authorization": "Bearer " + t} for t in ("tok-tantalus", "tok-argus", hub.token))

    # an app speaks only for itself: the holder it claims is replaced by its id
    _, a = _http(url + "/api/lane/acquire", {"domain": "shop.com", "holder": "faustus", "ttl_s": 60}, headers=tan)
    assert a["granted"] and hub.lanes.status()["lanes"][0]["holder"] == "tantalus"
    # another app cannot renew or release it, even claiming the holder name "tantalus"
    for path in ("/api/lane/renew", "/api/lane/release"):
        status, reply = _http(url + path, {"lease_id": a["lease_id"], "holder": "tantalus"}, headers=argus)
        assert status == 403, (path, reply)
    # the hub's own token (Faustus, agents) is another owner too
    assert _http(url + "/api/lane/release", {"lease_id": a["lease_id"], "holder": "tantalus"}, headers=hubtok)[0] == 403
    # nobody forces from the API
    status, reply = _http(url + "/api/lane/release", {"lease_id": a["lease_id"], "holder": "tantalus", "force": True},
                          headers=tan)
    assert status == 403 and "hub page" in reply["error"]
    assert _http(url + "/api/lane/release", {"lease_id": a["lease_id"], "force": True}, headers=hubtok)[0] == 403
    assert hub.lanes.status()["lanes"][0]["holder"] == "tantalus"
    # the owner can
    assert _http(url + "/api/lane/renew", {"lease_id": a["lease_id"], "holder": "anything"}, headers=tan)[0] == 200
    assert _http(url + "/api/lane/release", {"lease_id": a["lease_id"]}, headers=tan)[1]["released"] is True

    # hub-token callers name their own holder
    _, b = _http(url + "/api/lane/acquire", {"domain": "shop.com", "holder": "faustus", "ttl_s": 60}, headers=hubtok)
    assert b["granted"] and hub.lanes.status()["lanes"][0]["holder"] == "faustus"

    # the Hub page (same-origin, no token) may force a lane away, and only release
    page = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Origin": url}
    status, reply = _http(url + "/api/lane/acquire", {"domain": "other.com", "holder": "ui"}, headers=page)
    assert status == 403
    assert _http(url + "/api/lane/renew", {"lease_id": b["lease_id"]}, headers=page)[0] == 403
    status, reply = _http(url + "/api/lane/release", {"lease_id": b["lease_id"], "holder": "nobody", "force": True}, headers=page)
    assert status == 200 and reply["released"] is True and hub.lanes.status()["lanes"] == []


def test_events_on_the_bus(live_hub):
    hub, url = live_hub
    h = _auth(hub)
    _, a = _http(url + "/api/lane/acquire", {"domain": "shop.com", "holder": "tantalus", "purpose": "reads", "ttl_s": 60}, headers=h)
    _http(url + "/api/lane/release", {"lease_id": a["lease_id"], "holder": "tantalus"}, headers=h)
    _http(url + "/api/lane/acquire", {"domain": "gone.com", "holder": "app", "ttl_s": 1}, headers=h)
    time.sleep(1.2)
    hub.lanes.status()                                                            # reaping happens on the next call
    types = [(e["type"], e["data"].get("holder")) for e in hub.events.query(limit=50, newest_first=False)
             if e["type"].startswith("hub.lane.")]
    assert ("hub.lane.acquired", "tantalus") in types and ("hub.lane.released", "tantalus") in types
    assert ("hub.lane.expired", "app") in types


def test_lane_tools(live_hub):
    hub, url = live_hub
    names = {t["name"] for t in tools.catalogue()}
    assert {"hub_lane_status", "hub_lane_acquire", "hub_lane_renew", "hub_lane_release"} <= names
    ro = {t["name"]: t.get("annotations", {}).get("readOnlyHint") for t in tools.catalogue()}
    assert ro["hub_lane_status"] is True and not ro["hub_lane_acquire"] and not ro["hub_lane_release"]
    r = tools.call(hub, "hub_lane_acquire", {"domain": "https://shop.example.com", "purpose": "agent read"})
    assert r["granted"] is True and "status" not in r
    busy = tools.call(hub, "hub_lane_acquire", {"domain": "example.com", "holder": "other"})
    assert busy["granted"] is False and busy["held_by"] == "agent"
    st = tools.call(hub, "hub_lane_status", {})
    assert st["lanes"][0]["holder"] == "agent"
    assert tools.call(hub, "hub_lane_release", {"lease_id": r["lease_id"], "holder": "other"})["ok"] is False
    assert tools.call(hub, "hub_lane_release", {"lease_id": r["lease_id"]})["released"] is True
    assert tools.call(hub, "hub_lane_acquire", {})["ok"] is False
    status, body = _http(url + "/api/agent/call", {"tool": "hub_lane_status", "arguments": {}},
                         headers={"Authorization": "Bearer " + hub.token})
    assert status == 200 and body["result"]["lanes"] == []


def test_lane_panel_is_in_the_ui(live_hub):
    _, url = live_hub
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    html = opener.open(url + "/", timeout=5).read().decode()
    js = opener.open(url + "/ui/app.js", timeout=5).read().decode()
    assert 'id="lane-panel"' in html and 'id="lane-rows"' in html
    assert "/api/lane" in js and "Carriles de navegador" in js and "Browser lanes" in js


# ---- the client -----------------------------------------------------------------------------------------

def test_client_takes_renews_and_releases(live_hub):
    hub, url = live_hub
    with lane("https://shop.example.com/p", holder="tantalus", ttl_s=3, wait_s=5, purpose="prices", hub_url=url) as l:
        assert l.via == "hub" and l.acquired and l.domain == "example.com" and l.lease_id
        first = hub.lanes.status()["lanes"][0]["expires_at"]
        assert wait_for(lambda: hub.lanes.status()["lanes"][0]["expires_at"] > first, timeout=5)   # the heartbeat
        assert lane_status(url)["lanes"][0]["holder"] == "tantalus"
    assert l.state == "released" and hub.lanes.status()["lanes"] == []


def test_two_concurrent_readers_of_one_domain_run_one_after_the_other(live_hub):
    hub, url = live_hub
    spans: dict[str, tuple[float, float]] = {}

    def reader(name, host):
        with lane(host, holder=name, ttl_s=30, wait_s=10, hub_url=url):
            start = time.monotonic()
            time.sleep(0.5)                                                       # "reading the shop"
            spans[name] = (start, time.monotonic())

    threads = [threading.Thread(target=reader, args=("a", "shop.example.com")),
               threading.Thread(target=reader, args=("b", "www.example.com")),
               threading.Thread(target=reader, args=("c", "elsewhere.org"))]
    for th in threads:
        th.start()
        time.sleep(0.05)
    for th in threads:
        th.join(15)
    (a0, a1), (b0, b1) = spans["a"], spans["b"]
    assert a1 <= b0 + 0.02 or b1 <= a0 + 0.02                                     # never overlapping
    c0, c1 = spans["c"]
    assert c0 < min(a1, b1)                                                        # another site was not held up


def elsewhere(fn):
    """Run ``fn`` in another thread (an empty context: it is not 'nested' in the caller's lanes) and hand back its result."""
    box: dict = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=run)
    th.start()
    th.join(20)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def test_client_busy_raises_and_says_who(live_hub):
    hub, url = live_hub

    def competitor():
        t0 = time.monotonic()
        with pytest.raises(LaneBusy) as exc:
            with lane("shop.example.com", holder="faustus", wait_s=0.6, hub_url=url):
                pytest.fail("must not enter")
        assert time.monotonic() - t0 >= 0.5
        busy = exc.value
        assert busy.held_by == "tantalus" and busy.since and busy.domain == "example.com" and "tantalus" in str(busy)
        # the same, without an exception
        with lane("example.com", holder="argus", wait_s=0, raise_on_busy=False, hub_url=url) as l:
            assert l.acquired is False and l.held_by == "tantalus" and l.state == "busy"

    with lane("example.com", holder="tantalus", ttl_s=30, hub_url=url):
        elsewhere(competitor)
        assert hub.lanes.status()["waiting"] == []
    with lane("example.com", holder="faustus", wait_s=0, hub_url=url) as l:       # free again after the first one left
        assert l.acquired


def test_client_waits_longer_than_one_poll(live_hub, monkeypatch):
    hub, url = live_hub
    monkeypatch.setattr(bl, "POLL_WAIT_S", 0.3)                                    # several polling rounds
    first = hub.lanes.acquire("example.com", holder="tantalus", ttl_s=30)
    threading.Timer(1.0, lambda: hub.lanes.release(first["lease_id"], holder="tantalus")).start()
    with lane("example.com", holder="faustus", wait_s=10, hub_url=url) as l:
        assert l.acquired and l.waited_s >= 0.9


def test_hub_down_proceeds_without_a_lane(caplog):
    dead = f"http://127.0.0.1:{free_port()}"
    with caplog.at_level("WARNING", logger="hoard_link.browser_lane"):
        with lane("example.com", holder="tantalus", wait_s=5, hub_url=dead) as l:
            assert l.acquired and l.via == "local" and "not reachable" in l.warning
        with lane("example.com", holder="tantalus", hub_url=dead) as l2:
            assert l2.via == "local"
    assert sum("not reachable" in r.getMessage() for r in caplog.records) == 1          # logged once, not every time
    assert lane_status(dead) is None


def test_a_hub_without_lanes_proceeds(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Old(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with lane("example.com", holder="x", hub_url=f"http://127.0.0.1:{srv.server_port}") as l:
            assert l.via == "local" and l.acquired
    finally:
        srv.shutdown()


def test_nesting_in_one_thread_counts_instead_of_deadlocking(live_hub):
    hub, url = live_hub
    with lane("example.com", holder="tantalus", wait_s=0.5, hub_url=url) as outer:
        with lane("shop.example.com", holder="tantalus", wait_s=0.5, hub_url=url) as inner:
            assert inner.via == "nested" and inner.acquired and inner.lease_id == outer.lease_id
        assert len(hub.lanes.status()["lanes"]) == 1                                # the inner exit kept the lane
    assert hub.lanes.status()["lanes"] == []
    # another thread is not "nested": it waits
    with lane("example.com", holder="tantalus", wait_s=0.5, hub_url=url):
        def other():
            try:
                with lane("example.com", holder="tantalus", wait_s=0.4, hub_url=url):
                    return "got it"
            except LaneBusy as exc:
                return "busy:" + exc.held_by

        assert elsewhere(other) == "busy:tantalus"


def test_async_client(live_hub):
    hub, url = live_hub

    async def go():
        async with lane("example.com", holder="faustus", hub_url=url, wait_s=2) as l:
            assert l.via == "hub"

            def competitor():
                with pytest.raises(LaneBusy):
                    with lane("example.com", holder="argus", hub_url=url, wait_s=0.2):
                        pass

            elsewhere(competitor)
            async with lane("shop.example.com", holder="faustus", hub_url=url, wait_s=0.2) as inner:   # same task: nested
                assert inner.via == "nested"
        return hub.lanes.status()["lanes"]

    assert asyncio.run(go()) == []


def test_a_lost_lane_is_noticed_by_the_heartbeat(live_hub):
    hub, url = live_hub
    with lane("example.com", holder="tantalus", ttl_s=3, hub_url=url) as l:
        hub.lanes.release(l.lease_id, force=True)                                   # someone force-released it
        assert wait_for(lambda: l.lost, timeout=6)


# ---- the review's follow-ups: queue tickets, owners, client token / retries -------------------------------------------------

def test_a_waiter_keeps_its_place_between_polls_with_a_server_issued_ticket():
    clock = Clock()
    arb = make(clock=clock)
    first = arb.acquire("shop.com", holder="a", ttl_s=60, owner="o-a")
    b1 = arb.acquire("shop.com", holder="b", wait_s=0, owner="o-b", final=False)          # told busy, will come back
    assert b1["busy"] and b1["ticket"] and "queue_seq" in b1
    c = arb.acquire("shop.com", holder="c", wait_s=0, owner="o-c")                          # arrives later
    assert c["busy"] and c["held_by"] == "a" and "ticket" not in c
    arb.release(first["lease_id"], holder="a", owner="o-a")
    late = arb.acquire("shop.com", holder="c", wait_s=0, owner="o-c")                       # c cannot overtake the parked b
    assert late["busy"] and late["held_by"] == "b"
    got = arb.acquire("shop.com", holder="b", wait_s=0, owner="o-b", ticket=b1["ticket"])
    assert got["granted"] is True                                                             # b gets it, in its place


def test_a_ticket_cannot_be_forged_or_borrowed():
    clock = Clock()
    arb = make(clock=clock)
    arb.acquire("shop.com", holder="a", ttl_s=60, owner="o-a")
    b = arb.acquire("shop.com", holder="b", wait_s=0, owner="o-b", final=False)
    arb.acquire("shop.com", holder="x", wait_s=0, owner="o-x", queue_seq=1)                  # a client-chosen position is ignored
    # another owner / holder / lane presenting b's ticket gets a place of its own, at the back
    for kw in ({"holder": "b", "owner": "o-evil"}, {"holder": "evil", "owner": "o-b"}):
        res = arb.acquire("shop.com", wait_s=0, ticket=b["ticket"], **kw)
        assert res["busy"]
    other_lane = arb.acquire("other.com", holder="b", wait_s=0, owner="o-b", ticket=b["ticket"])
    assert other_lane["granted"] is True                                                      # not queued behind anything there
    assert arb.acquire("shop.com", holder="b", wait_s=0, owner="o-b", ticket="invented")["busy"] is True
    assert [w["holder"] for w in arb.status()["waiting"]][:1] == ["b"]                       # b is still first in line


def test_a_parked_place_is_lost_after_the_grace_period_and_a_final_poll_leaves_at_once():
    clock = Clock()
    arb = make(clock=clock)
    first = arb.acquire("shop.com", holder="a", ttl_s=600, owner="o-a")
    b = arb.acquire("shop.com", holder="b", wait_s=0, owner="o-b", final=False)
    assert [w["holder"] for w in arb.status()["waiting"]] == ["b"]
    clock.t += TICKET_GRACE_S + 1                                                             # b never came back
    assert arb.status()["waiting"] == []
    arb.release(first["lease_id"], holder="a", owner="o-a")
    assert arb.acquire("shop.com", holder="c", wait_s=0, owner="o-c")["granted"] is True      # b no longer blocks c
    arb.acquire("other.com", holder="a", ttl_s=60)
    arb.acquire("other.com", holder="d", wait_s=0, final=True)
    assert arb.status()["waiting"] == []                                                      # a final poll does not park
    del b


def test_owner_binding_in_the_arbiter():
    arb = make()
    a = arb.acquire("shop.com", holder="tantalus", ttl_s=60, owner="tantalus")
    assert arb.renew(a["lease_id"], "tantalus", owner="argus")["status"] == 403
    assert arb.release(a["lease_id"], "tantalus", owner="argus")["status"] == 403
    assert arb.status()["lanes"][0]["owner"] == "tantalus"
    assert arb.renew(a["lease_id"], "tantalus", owner="tantalus")["ok"] is True
    assert arb.release(a["lease_id"], force=True)["released"] is True
    legacy = arb.acquire("old.com", holder="x", ttl_s=60)                                     # no owner: not bound
    assert arb.release(legacy["lease_id"], "x", owner="anyone")["released"] is True


def test_lane_tools_cap_the_ttl_and_cannot_force(live_hub):
    hub, url = live_hub
    r = tools.call(hub, "hub_lane_acquire", {"domain": "shop.example.com", "ttl_s": 7200})
    assert r["granted"] and r["lane"]["ttl_s"] == 600 and r["lane"]["owner"] == "mcp"
    renewed = tools.call(hub, "hub_lane_renew", {"lease_id": r["lease_id"], "ttl_s": 7200})
    assert renewed["ok"] and renewed["lane"]["ttl_s"] == 600
    assert tools.call(hub, "hub_lane_renew", {"lease_id": r["lease_id"], "holder": "other"})["ok"] is False
    schema = next(t for t in tools.catalogue() if t["name"] == "hub_lane_release")["inputSchema"]
    assert "force" not in schema["properties"]
    assert tools.call(hub, "hub_lane_release", {"lease_id": r["lease_id"], "force": True})["released"] is True   # holder default: own lane
    assert "lease_id" not in json.dumps(tools.call(hub, "hub_lane_status", {}))
    # a lane an app took over HTTP is not the tools' to release or force
    _, b = _http(url + "/api/lane/acquire", {"domain": "x.com", "holder": "agent", "ttl_s": 60}, headers=_auth(hub))
    res = tools.call(hub, "hub_lane_release", {"lease_id": b["lease_id"], "holder": "agent", "force": True})
    assert res["ok"] is False and hub.lanes.status()["lanes"][0]["holder"] == "agent"


def test_client_sends_its_token_and_gives_up_cleanly_when_refused(live_hub, tmp_path, monkeypatch):
    hub, url = live_hub
    seen = []
    real_post = bl._hubclient.fetch

    def spy(u, body=None, **kw):
        seen.append((u.rsplit("/", 1)[-1], (kw.get("headers") or {}).get("Authorization")))
        return real_post(u, body, **kw)

    monkeypatch.setattr(bl._hubclient, "fetch", spy)
    with lane("shop.example.com", holder="faustus", hub_url=url, wait_s=1) as l:
        assert l.via == "hub"
    assert seen and all(auth == "Bearer " + hub.token for _, auth in seen)
    # a token the hub does not know -> no lane, proceed, say why
    bad = tmp_path / "bad-token"
    bad.write_text("nope")
    monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", str(bad))
    with lane("shop.example.com", holder="faustus", hub_url=url, wait_s=1) as l:
        assert l.via == "local" and "refused" in l.warning and hub.lanes.status()["lanes"] == []
    # an explicit token file wins
    good = tmp_path / "good-token"
    good.write_text(hub.token)
    with lane("shop.example.com", holder="faustus", hub_url=url, wait_s=1, token_file=str(good)) as l:
        assert l.via == "hub"


def test_client_retries_a_failed_release(live_hub, monkeypatch):
    hub, url = live_hub
    real = bl._hubclient.fetch
    calls = {"release": 0}

    def flaky(u, body=None, **kw):
        if u.endswith("/api/lane/release"):
            calls["release"] += 1
            if calls["release"] < 3:
                return None, None                                                            # the hub did not answer
        return real(u, body, **kw)

    monkeypatch.setattr(bl._hubclient, "fetch", flaky)
    monkeypatch.setattr(bl.time, "sleep", lambda s: None)
    with lane("shop.example.com", holder="faustus", hub_url=url, wait_s=1):
        pass
    assert calls["release"] == 3 and hub.lanes.status()["lanes"] == []


def test_a_polling_client_is_not_overtaken(live_hub, monkeypatch):
    hub, url = live_hub
    monkeypatch.setattr(bl, "POLL_WAIT_S", 0.3)
    first = hub.lanes.acquire("example.com", holder="tantalus", ttl_s=30, owner="hub")
    order = []

    def waiter(name):
        with lane("example.com", holder=name, wait_s=20, hub_url=url):
            order.append(name)
            time.sleep(0.2)

    t1 = threading.Thread(target=waiter, args=("second",))
    t1.start()
    time.sleep(0.8)                                                                           # "second" has polled a few times
    t2 = threading.Thread(target=waiter, args=("third",))
    t2.start()
    time.sleep(0.5)
    hub.lanes.release(first["lease_id"], force=True)
    t1.join(15)
    t2.join(15)
    assert order == ["second", "third"]
