"""References between apps: the refs facet (graph in refs.db, HTTP with ownership rules, tools)."""

from __future__ import annotations

import pytest

from hoard_link.hub import tools
from hoard_link.hub.refs import RefsFacet, normalize_uri

from ._hub_fakes import FakeApp, http, make_hub, serve

LEDGER = "hoard://ledger/tx/12"
KAFKA = "hoard://kafka/document/7"
PHILEAS = "hoard://phileas/shipment/s1"
HOME = "hoard://homehoard/item/3"


@pytest.fixture
def hub(tmp_path):
    h = make_hub(tmp_path, extra_apps=["ledger", "kafka", "phileas"])
    yield h
    h.close()


def test_uri_validation_and_normalisation():
    assert normalize_uri("hoard://ledger/tx/12") == LEDGER
    assert normalize_uri("hoard://ledger/tx/a b".replace(" ", "%20")) == "hoard://ledger/tx/a%20b"
    for bad in ("", "ledger/tx/12", "http://ledger/tx/12", "hoard://ledger/tx", "hoard://ledger/tx/1?x=1", "hoard://led ger/tx/1"):
        with pytest.raises(ValueError):
            normalize_uri(bad)


def test_link_is_idempotent_keeps_labels_and_emits_once(hub):
    refs = hub.facet("refs")
    assert refs is not None
    res = refs.link(LEDGER, KAFKA, "purchase", from_label="Amazon 23.90 EUR", to_label="", by="ledger")
    assert res["ok"] and res["created"] and res["edge"]["rel"] == "purchase" and res["edge"]["by"] == "ledger"
    again = refs.link(LEDGER, KAFKA, "purchase", to_label="Invoice 114")
    assert again["ok"] and not again["created"]
    assert again["edge"]["from_label"] == "Amazon 23.90 EUR" and again["edge"]["to_label"] == "Invoice 114"
    # a second call with empty labels never blanks them
    assert refs.link(LEDGER, KAFKA, "purchase")["edge"]["to_label"] == "Invoice 114"
    assert refs.count() == 1
    evs = hub.events.query(type="refs.linked")
    assert len(evs) == 1 and evs[0]["data"] == {"from": LEDGER, "to": KAFKA, "rel": "purchase"}
    # another rel is another edge
    assert refs.link(LEDGER, KAFKA, "related")["created"] and refs.count() == 2
    # errors
    assert refs.link(LEDGER, LEDGER)["ok"] is False
    assert refs.link("nope", KAFKA)["status"] == 400
    assert refs.link(LEDGER, "hoard://kafka")["ok"] is False


def test_around_follows_links_both_ways_with_depth(hub):
    refs = hub.facet("refs")
    refs.link(LEDGER, PHILEAS, "purchase", from_label="Amazon 23.90 EUR", to_label="Parcel UPS")
    refs.link(KAFKA, LEDGER, "purchase", from_label="Invoice 114")
    refs.link(PHILEAS, HOME, "purchase", to_label="Kettle")
    one = refs.around(LEDGER)
    assert one["ok"] and one["depth"] == 1
    uris = {n["uri"]: n for n in one["nodes"]}
    assert set(uris) == {LEDGER, PHILEAS, KAFKA}
    assert uris[LEDGER]["depth"] == 0 and uris[PHILEAS]["depth"] == 1
    assert uris[PHILEAS]["label"] == "Parcel UPS" and uris[KAFKA]["label"] == "Invoice 114"
    assert uris[LEDGER]["label"] == "Amazon 23.90 EUR"
    assert (uris[LEDGER]["app"], uris[LEDGER]["kind"], uris[LEDGER]["id"]) == ("ledger", "tx", "12")
    assert uris[LEDGER]["app_url"].startswith("http://127.0.0.1:")      # the installed app's base url
    assert len(one["edges"]) == 2
    two = refs.around(LEDGER, depth=2)
    assert {n["uri"] for n in two["nodes"]} == {LEDGER, PHILEAS, KAFKA, HOME}
    assert next(n for n in two["nodes"] if n["uri"] == HOME)["depth"] == 2 and len(two["edges"]) == 3
    assert next(n for n in two["nodes"] if n["uri"] == HOME)["app_url"] == ""         # not installed here
    # a record with no links is still a node
    lone = refs.around("hoard://ledger/tx/99")
    assert [n["uri"] for n in lone["nodes"]] == ["hoard://ledger/tx/99"] and lone["edges"] == []
    assert refs.around("bad")["ok"] is False


def test_unlink_recent_and_label_search(hub):
    refs = hub.facet("refs")
    refs.link(LEDGER, KAFKA, "purchase", from_label="Café Müller 12.50", to_label="Factura")
    refs.link(LEDGER, PHILEAS, "purchase")
    recent = refs.recent(10)
    assert [e["to"] for e in recent] == [PHILEAS, KAFKA]
    hits = refs.search_labels("cafe muller")                      # accents and case folded
    assert [h["uri"] for h in hits] == [LEDGER] and hits[0]["title"] == "Café Müller 12.50" and hits[0]["app"] == "ledger"
    assert refs.search_labels("zzz") == [] and refs.search_labels("") == []
    assert {h["uri"] for h in refs.search_labels("hoard://kafka")} == {KAFKA}       # a uri fragment finds it too
    assert refs.unlink(LEDGER, KAFKA, "purchase")["removed"] == 1
    assert refs.unlink(LEDGER, KAFKA)["status"] == 404
    assert refs.unlink(edge_id=recent[0]["id"])["ok"] and refs.count() == 0


def test_http_ownership_rules_and_reads(tmp_path):
    ledger = FakeApp("ledger")
    h = make_hub(tmp_path, [ledger], extra_apps=["kafka", "people"])
    server = serve(h)
    try:
        url = h.config.url
        app_tok = {"Authorization": "Bearer " + ledger.token}
        hub_tok = {"Authorization": "Bearer " + h.token}
        # no token: refused
        status, body = http(url + "/api/refs", {"from": LEDGER, "to": KAFKA})
        assert status == 401
        # an app may link when one end is its own
        status, body = http(url + "/api/refs", {"from": LEDGER, "to": KAFKA, "rel": "purchase", "from_label": "Pay"}, app_tok)
        assert status == 200 and body["created"] and body["edge"]["by"] == "ledger"
        status, body = http(url + "/api/refs", {"from": KAFKA, "to": "hoard://people/contact/1"}, app_tok)
        assert status == 403 and "own app" in body["error"]
        status, body = http(url + "/api/refs", {"from": "bad", "to": KAFKA}, app_tok)
        assert status == 400
        # the hub's token links anything and may name who did it
        status, body = http(url + "/api/refs", {"from": KAFKA, "to": "hoard://people/contact/1", "by": "assistant"}, hub_tok)
        assert status == 200 and body["edge"]["by"] == "assistant"
        # reads
        status, body = http(url + "/api/refs?uri=" + "hoard%3A%2F%2Fledger%2Ftx%2F12&depth=2")
        assert status == 200 and {n["uri"] for n in body["nodes"]} == {LEDGER, KAFKA, "hoard://people/contact/1"}
        status, body = http(url + "/api/refs?q=pay")
        assert body["matches"][0]["uri"] == LEDGER
        status, body = http(url + "/api/refs?q=" + "hoard%3A%2F%2Fkafka%2Fdocument%2F7")
        assert body["ok"] and body["uri"] == KAFKA
        status, body = http(url + "/api/refs/recent?limit=5")
        assert status == 200 and len(body["edges"]) == 2
        # removing: an app only its own links
        edge_id = body["edges"][0]["id"]       # the people link (kafka -> people): ledger has no part in it
        status, body = http(url + "/api/refs/remove", {"id": edge_id}, app_tok)
        assert status == 403
        status, body = http(url + "/api/refs/remove", {"from": LEDGER, "to": KAFKA, "rel": "purchase"}, app_tok)
        assert status == 200 and body["removed"] == 1
        status, body = http(url + "/api/refs/remove", {"id": edge_id}, hub_tok)
        assert status == 200
        status, body = http(url + "/api/refs/remove", {"id": 999}, hub_tok)
        assert status == 404
    finally:
        server.shutdown()
        h.close()
        ledger.stop()


def test_tools(hub):
    names = {t["name"]: t for t in RefsFacet.tools()}
    assert set(names) == {"hub_refs", "hub_ref_link"}
    assert names["hub_refs"]["annotations"]["readOnlyHint"] is True
    for t in names.values():
        assert len(t["description"].splitlines()[0]) <= 110
    res = tools.call(hub, "hub_ref_link", {"from": LEDGER, "to": KAFKA, "rel": "purchase", "from_label": "Pay"})
    assert res["ok"] and res["created"]
    assert tools.call(hub, "hub_ref_link", {"from": "x", "to": KAFKA})["ok"] is False
    res = tools.call(hub, "hub_refs", {"uri": LEDGER, "depth": 2})
    assert res["ok"] and len(res["nodes"]) == 2
    assert tools.call(hub, "hub_refs", {"q": "pay"})["matches"][0]["uri"] == LEDGER
    assert tools.call(hub, "hub_refs", {})["edges"]
