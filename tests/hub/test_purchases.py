"""Purchases: one row per purchase built from events of Ledger, Phileas, Kafka, HomeHoard and Tantalus — matching rules,
stage track, refs links, the two actions (stop watching, "where do you keep it?"), HTTP, tools and restart behaviour."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hoard_link.hub import tools
from hoard_link.hub.events import EventLog
from hoard_link.hub.purchases import PurchasesFacet, merchant_similar, order_key, quick_add_url

from ._hub_fakes import FakeApp, RecordingNotify, http, make_hub, serve, wait_for

_n = [0]


def ev(type_, data, source="ledger", ts=None):
    _n[0] += 1
    return {"id": _n[0], "ts": ts or time.time(), "type": type_, "source": source, "data": data}


def ledger_ev(tx=12, **kw):
    data = {"tx_id": tx, "merchant": "Amazon", "amount": 23.90, "currency": "EUR", "date": "2026-10-01",
            "order_ref": "#123-4567890", "items": ["Kettle"], **kw}
    return ev("ledger.mail.recorded", data)


@pytest.fixture
def world(tmp_path):
    tantalus = FakeApp("tantalus", handlers={
        "watchers_match_purchase": lambda a: {"matches": [{"watcher_id": 5, "score": 0.93, "name": "Kettle watcher"},
                                                           {"watcher_id": 6, "score": 0.5}]},
        "watcher_mark_bought": lambda a: {"ok": True}})
    homehoard = FakeApp("homehoard")
    hub = make_hub(tmp_path, [tantalus, homehoard], extra_apps=["ledger", "phileas", "kafka"])
    hub.config.language = "es"
    notify = RecordingNotify()
    hub._facets_by_id["notify"] = notify
    yield SimpleNamespace(hub=hub, p=hub.facet("purchases"), notify=notify, tantalus=tantalus, homehoard=homehoard)
    hub.close()
    tantalus.stop()
    homehoard.stop()


# ---- helpers ---------------------------------------------------------------------------------------------

def test_normalisers():
    assert order_key("#123-4567890") == order_key("1234567890") == "1234567890"
    assert order_key("12") == "" and order_key(None) == ""
    assert merchant_similar("Amazon", "AMAZON EU S.a.r.l.") and merchant_similar("Café Müller", "cafe muller shop")
    assert merchant_similar("Mercadona", "MERCADONA S.A.") and not merchant_similar("Amazon", "Zalando") and not merchant_similar("", "x")
    purchase = {"id": 4, "items": ["Kettle 1.7 L"], "merchant": "Amazon", "amount": 23.9, "date": "2026-10-01"}
    assert quick_add_url("http://127.0.0.1:5196/", purchase) == (
        "http://127.0.0.1:5196/#/add?name=Kettle%201.7%20L&source_ref=hoard://hub/purchase/4&price=23.9&merchant=Amazon&date=2026-10-01")
    assert quick_add_url("http://h", {"id": 1, "items": [], "merchant": "", "amount": None, "date": ""}) == "http://h/#/add?source_ref=hoard://hub/purchase/1"


# ---- the whole life of a purchase ----------------------------------------------------------------------------

def test_a_purchase_from_payment_to_shelf(world):
    hub, p, notify = world.hub, world.p, world.notify
    e = hub.events.emit
    e("ledger.mail.recorded", {"tx_id": 12, "merchant": "Amazon", "amount": 23.9, "currency": "EUR", "date": "2026-10-01",
                               "order_ref": "#123-4567890", "items": ["Kettle"], "message_id": "<m1@amazon>"}, source="ledger")
    assert p.wait_idle()
    [one] = p.list()["purchases"]
    assert one["stage"] == "paid" and one["merchant"] == "Amazon" and one["amount"] == 23.9 and one["items"] == ["Kettle"]
    assert one["refs"]["ledger"]["uri"] == "hoard://ledger/tx/12" and one["refs"]["ledger"]["app_url"] == hub.get("ledger").url
    # a new payment: Tantalus is asked, the watcher above 0.8 is marked bought, the person is told
    calls = {c["name"]: c["arguments"] for c in world.tantalus.calls}
    assert calls["watchers_match_purchase"] == {"title": "Kettle", "merchant": "Amazon"}
    assert calls["watcher_mark_bought"] == {"watcher_id": 5, "purchase_ref": "hoard://hub/purchase/1"}
    assert len([c for c in world.tantalus.calls if c["name"] == "watcher_mark_bought"]) == 1
    assert [n["title"] for n in notify.sent] == ["Dejo de vigilar Kettle watcher: ya lo has comprado"]
    assert notify.sent[0]["priority"] == "normal" and notify.sent[0]["group"] == "purchase"

    # the parcel: same order (normalised) and similar merchant -> the same purchase
    e("phileas.shipment.new", {"shipment_id": "s1", "merchant": "AMAZON EU S.a.r.l.", "order_ref": "1234567890", "carrier": "UPS",
                               "tracking_number": "1Z999"}, source="phileas")
    assert p.wait_idle()
    [one] = p.list()["purchases"]
    assert one["stage"] == "shipped" and one["refs"]["phileas"]["uri"] == "hoard://phileas/shipment/s1" and one["meta"]["carrier"] == "UPS"
    assert one["merchant"] == "Amazon"                                                   # the first name stays

    # arrival: the person is asked where it goes, once
    e("phileas.shipment.delivered", {"shipment_id": "s1", "delivered_at": "2026-10-04T10:00:00"}, source="phileas")
    assert p.wait_idle()
    assert p.get_purchase(1)["stage"] == "delivered"
    arrived = notify.sent[-1]
    home = hub.get("homehoard").url
    assert arrived["title"] == "Ha llegado Kettle. ¿Dónde lo guardas?"
    assert arrived["url"] == f"{home}/#/add?name=Kettle&source_ref=hoard://hub/purchase/1&price=23.9&merchant=Amazon&date=2026-10-01"
    n_before = len(notify.sent)
    e("phileas.update", {"shipment_id": "s1", "status": "delivered", "title": "Delivered"}, source="phileas")    # the same news again
    e("phileas.shipment.delivered", {"shipment_id": "s1"}, source="phileas")
    assert p.wait_idle()
    assert len(notify.sent) == n_before and len(p.list()["purchases"]) == 1

    # the invoice is archived (no order ref: merchant + amount + date), a warranty arrives for the parcel
    e("kafka.document.archived", {"doc_id": 7, "kind": "invoice", "merchant": "amazon", "amount": "23,90", "date": "2026-10-02"}, source="kafka")
    assert p.wait_idle()
    assert p.get_purchase(1)["stage"] == "filed" and p.get_purchase(1)["refs"]["invoice"]["uri"] == "hoard://kafka/document/7"
    e("kafka.warranty.created", {"doc_id": 8, "shipment_id": "s1", "until": "2028-10-04"}, source="kafka")
    assert p.wait_idle()
    got = p.get_purchase(1)
    assert got["refs"]["warranty"]["uri"] == "hoard://kafka/document/8" and got["meta"]["warranty_until"] == "2028-10-04" and got["stage"] == "filed"

    # on the shelf, and the watcher that waited for it
    e("homehoard.item.created", {"item_id": 3, "source_ref": "hoard://hub/purchase/1"}, source="homehoard")
    e("tantalus.watcher.bought", {"watcher_id": 5, "purchase_ref": "hoard://hub/purchase/1"}, source="tantalus")
    assert p.wait_idle()
    got = p.get_purchase(1)
    assert got["stage"] == "stored" and set(got["refs"]) == {"ledger", "phileas", "invoice", "warranty", "homehoard", "tantalus"}
    assert len(p.list()["purchases"]) == 1
    stages = [x["data"]["stage"] for x in reversed(hub.events.query(type="purchases.stage"))]
    assert stages == ["paid", "shipped", "delivered", "filed", "stored"]

    # every app record is linked to the others in the refs graph (rel purchase)
    graph = hub.facet("refs").around("hoard://ledger/tx/12", depth=2)
    assert {n["uri"] for n in graph["nodes"]} == {"hoard://ledger/tx/12", "hoard://phileas/shipment/s1", "hoard://kafka/document/7",
                                                    "hoard://kafka/document/8", "hoard://homehoard/item/3", "hoard://tantalus/watcher/5"}
    assert {e_["rel"] for e_ in graph["edges"]} == {"purchase"}
    direct = hub.facet("refs").around("hoard://ledger/tx/12", depth=1)
    assert {"hoard://phileas/shipment/s1", "hoard://kafka/document/7"} <= {n["uri"] for n in direct["nodes"]}
    assert any(n["label"] == "Amazon 23.9 EUR" for n in direct["nodes"])
    assert len(hub.events.query(type="refs.linked")) == 5

    # close it, reopen it: the stage the records say
    assert p.close_purchase(1)["purchase"]["stage"] == "closed"
    assert p.reopen_purchase(1)["purchase"]["stage"] == "stored"
    assert p.close_purchase(99)["status"] == 404


# ---- matching -------------------------------------------------------------------------------------------------------

def run(p, *events):
    for e in events:
        p.ingest(e)
    return p.list()["purchases"]


@pytest.fixture
def bare(tmp_path):
    """A purchases facet over a stub hub: nothing else on the family (no refs, no notify, no apps)."""
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path), language="es"), events=EventLog(None),
                          facet=lambda _id: None, get=lambda _id: None)
    p = PurchasesFacet(hub)
    yield p
    p.close()


def test_matching_by_order_ref_message_id_and_fuzzy(bare):
    p = bare
    # same order reference in other clothes
    rows = run(p, ledger_ev(), ev("phileas.shipment.new", {"shipment_id": "a", "order_ref": "123-4567890", "merchant": "whatever"}))
    assert len(rows) == 1 and rows[0]["stage"] == "shipped"
    # the same mail
    rows = run(p, ledger_ev(13, merchant="Zalando", order_ref="", message_id="<z1>", amount=50.0, items=["Shoes"]),
               ev("kafka.document.archived", {"doc_id": 1, "kind": "receipt", "message_id": "<z1>"}))
    assert len(rows) == 2 and {r["stage"] for r in rows} == {"shipped", "filed"}
    assert rows[0]["refs"].get("receipt") or rows[1]["refs"].get("receipt")


@pytest.mark.parametrize("second, same", [
    ({"merchant": "AMAZON EU", "amount": 23.9, "date": "2026-10-03"}, True),            # similar merchant, same amount, 2 days
    ({"merchant": "Amazon", "amount": 24.1, "date": "2026-10-01"}, True),               # +0.8 %
    ({"merchant": "Amazon", "amount": 25.2, "date": "2026-10-01"}, False),              # +5 %
    ({"merchant": "Amazon", "amount": 23.9, "date": "2026-10-08"}, False),              # 7 days later
    ({"merchant": "Zalando", "amount": 23.9, "date": "2026-10-01"}, False),             # other merchant
    ({"merchant": "Amazon", "date": "2026-10-02"}, True),                               # no amount on one side: merchant + dates
])
def test_fuzzy_matching(bare, second, same):
    p = bare
    run(p, ledger_ev(order_ref=""))
    rows = run(p, ev("kafka.document.archived", {"doc_id": 9, "kind": "invoice", **second}))
    assert len(rows) == (1 if same else 2)


def test_two_records_of_the_same_kind_are_two_purchases(bare):
    p = bare
    rows = run(p, ledger_ev(1, order_ref=""), ledger_ev(2, order_ref=""))
    assert len(rows) == 2                                                                  # another payment is another purchase
    rows = run(p, ev("phileas.shipment.new", {"shipment_id": "a", "merchant": "Amazon"}),
               ev("phileas.shipment.new", {"shipment_id": "b", "merchant": "Amazon"}))
    assert len(rows) == 2                                                                  # one parcel joins each payment: never two on one
    assert {r["refs"]["phileas"]["uri"] for r in rows} == {"hoard://phileas/shipment/a", "hoard://phileas/shipment/b"}
    # the same event twice is the same purchase
    rows2 = run(p, ledger_ev(1, order_ref=""))
    assert len(rows2) == len(rows)


def test_what_creates_a_purchase_and_what_only_attaches(bare):
    p = bare
    assert run(p, ev("homehoard.item.created", {"item_id": 1, "source_ref": "hoard://hub/purchase/77"})) == []
    assert run(p, ev("homehoard.item.created", {"item_id": 1})) == []
    assert run(p, ev("tantalus.watcher.bought", {"watcher_id": 1, "purchase_ref": "hoard://hub/purchase/77"})) == []
    assert run(p, ev("kafka.document.archived", {"doc_id": 3, "kind": "manual", "merchant": "Bosch"})) == []
    assert run(p, ev("phileas.update", {"shipment_id": "x", "status": "in_transit"})) == []
    assert run(p, ev("ledger.mail.recorded", {"merchant": "No tx id"})) == []
    assert run(p, ev("something.else", {})) == []
    rows = run(p, ev("phileas.shipment.new", {"shipment_id": "s", "merchant": "Etsy", "items": ["Mug"], "carrier": "Correos"}))
    assert rows[0]["stage"] == "shipped" and rows[0]["amount"] is None and rows[0]["title"] == "Mug"
    # a payment that arrives later joins the parcel's purchase by merchant and date
    rows = run(p, ledger_ev(5, merchant="ETSY", order_ref="", items=["Mug"], date=time.strftime("%Y-%m-%d")))
    assert len(rows) == 1 and rows[0]["stage"] == "shipped" and rows[0]["amount"] == 23.9
    assert set(rows[0]["refs"]) == {"phileas", "ledger"}
    # a closed purchase takes no more events: the same merchant opens a new one
    p.close_purchase(rows[0]["id"])
    rows = run(p, ledger_ev(6, merchant="ETSY", order_ref="", items=["Mug"], date=time.strftime("%Y-%m-%d")))
    assert len(rows) == 2


def test_delivered_update_event_and_no_notice_when_it_is_already_stored(bare):
    p = bare
    rows = run(p, ledger_ev(), ev("homehoard.item.created", {"item_id": 1, "source_ref": "hoard://hub/purchase/1"}))
    assert rows[0]["stage"] == "stored"
    rows = run(p, ev("phileas.update", {"shipment_id": "s", "status": "delivered", "order_ref": "1234567890"}, source="phileas"))
    assert rows[0]["stage"] == "stored" and "delivered" not in rows[0]["notified"]


# ---- actions with things missing ----------------------------------------------------------------------------------------

def test_no_tantalus_no_notify_is_not_an_error(tmp_path):
    hub = make_hub(tmp_path, extra_apps=["ledger", "phileas"])          # no tantalus, no homehoard installed
    hub._facets_by_id.pop("notify", None)
    try:
        p = hub.facet("purchases")
        p.ingest(ledger_ev())
        p.ingest(ev("phileas.shipment.delivered", {"shipment_id": "s1", "order_ref": "1234567890"}, source="phileas"))
        one = p.list()["purchases"][0]
        assert one["stage"] == "delivered"
        assert one["notified"]["tantalus"]["error"] and one["notified"]["delivered"]["sent"] is False
        assert one["notified"]["delivered"]["url"].startswith("http://127.0.0.1:5196/#/add?")        # HomeHoard's default address
    finally:
        hub.close()


def test_tantalus_below_the_threshold_marks_nothing(world):
    world.tantalus.handlers["watchers_match_purchase"] = lambda a: {"matches": [{"watcher_id": 6, "score": 0.79}]}
    world.p.ingest(ledger_ev())
    assert [c["name"] for c in world.tantalus.calls] == ["watchers_match_purchase"] and world.notify.sent == []
    # asked once per purchase, even when a second payment record joins it
    world.p.ingest(ev("kafka.document.archived", {"doc_id": 1, "kind": "invoice", "order_ref": "1234567890"}))
    assert len(world.tantalus.calls) == 1


# ---- worker, restart ---------------------------------------------------------------------------------------------------------

def test_events_are_followed_on_the_worker_and_missed_ones_after_a_restart(tmp_path):
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path), language="es"), events=EventLog(None),
                          facet=lambda _id: None, get=lambda _id: None)
    hub.events.emit("ledger.mail.recorded", {"tx_id": 1, "merchant": "Old shop", "amount": 5}, source="ledger")     # before the first start
    p = PurchasesFacet(hub)
    p.start()
    try:
        hub.events.emit("ledger.mail.recorded", {"tx_id": 2, "merchant": "Live shop", "amount": 7, "date": "2026-10-01"}, source="ledger")
        assert wait_for(lambda: len(p.list()["purchases"]) == 1)
        assert p.list()["purchases"][0]["merchant"] == "Live shop"            # a first run does not replay history
    finally:
        p.close()
    hub.events.emit("ledger.mail.recorded", {"tx_id": 3, "merchant": "While away", "amount": 9, "date": "2026-10-01"}, source="ledger")
    p2 = PurchasesFacet(hub)
    p2.start()
    try:
        assert wait_for(lambda: len(p2.list()["purchases"]) == 2)
        assert {r["merchant"] for r in p2.list()["purchases"]} == {"Live shop", "While away"}
        t0 = time.monotonic()
        for i in range(50):
            hub.events.emit("agent.call", {"i": i}, source="ledger")
        assert time.monotonic() - t0 < 3                                       # emit never waits for us
    finally:
        p2.close()


# ---- HTTP and tools ----------------------------------------------------------------------------------------------------------------

def test_http_and_tools(world):
    hub, p = world.hub, world.p
    p.ingest(ledger_ev())
    p.ingest(ledger_ev(20, merchant="Zalando", amount=59.0, order_ref="ZAL-5555", items=["Shoes"]))
    p.ingest(ev("phileas.shipment.new", {"shipment_id": "z", "order_ref": "ZAL-5555"}, source="phileas"))
    server = serve(hub)
    tok = {"Authorization": "Bearer " + hub.token}
    try:
        url = hub.config.url
        status, body = http(url + "/api/purchases")
        assert status == 200 and body["total"] == 2 and body["counts"]["paid"] == 1 and body["counts"]["shipped"] == 1
        status, body = http(url + "/api/purchases?stage=shipped")
        assert [x["merchant"] for x in body["purchases"]] == ["Zalando"]
        status, body = http(url + "/api/purchases?q=kettle")
        assert [x["merchant"] for x in body["purchases"]] == ["Amazon"]
        status, body = http(url + "/api/purchases?q=zzz")
        assert body["purchases"] == []
        status, body = http(url + "/api/purchases/1")
        assert status == 200 and body["purchase"]["uri"] == "hoard://hub/purchase/1" and body["purchase"]["stage_index"] == 1
        status, body = http(url + "/api/purchases/99")
        assert status == 404
        status, body = http(url + "/api/purchases/1/close", {})
        assert status == 401
        status, body = http(url + "/api/purchases/1/close", {}, tok)
        assert status == 200 and body["purchase"]["stage"] == "closed"
        status, body = http(url + "/api/purchases/1/reopen", {}, tok)
        assert body["purchase"]["stage"] == "paid"
        status, body = http(url + "/api/purchases/99/close", {}, tok)
        assert status == 404
    finally:
        server.shutdown()
    res = tools.call(hub, "hub_purchases", {"stage": "paid"})
    assert res["ok"] and [x["id"] for x in res["purchases"]] == [1]
    res = tools.call(hub, "hub_purchase", {"id": 2})
    assert res["purchase"]["merchant"] == "Zalando" and "phileas" in res["purchase"]["refs"]
    assert tools.call(hub, "hub_purchase", {"id": 99})["ok"] is False and tools.call(hub, "hub_purchase", {"id": "x"})["ok"] is False
    for t in PurchasesFacet.tools():
        assert t["annotations"]["readOnlyHint"] is True and len(t["description"].splitlines()[0]) <= 110


def test_enrich_finds_the_shipment_and_the_payment_that_went_by_before(tmp_path):
    phileas = FakeApp("phileas", handlers={"shipments_list": lambda a: {"shipments": [
        {"id": "s_9", "order_ref": "3481958", "merchant": "PC Maker", "carrier": "ups", "tracking_number": "1Z", "status": "in_transit"},
        {"id": "s_8", "order_ref": "999999", "merchant": "Other"}]}})
    ledger = FakeApp("ledger", handlers={"tx_find": lambda a: {"matches": [{"tx_id": 77, "score": 0.95, "date": "2026-10-01", "amount": 1719.85}]}})
    hub = make_hub(tmp_path, [phileas, ledger], extra_apps=["kafka"])
    try:
        p = hub.facet("purchases")
        p.background_enrich = False
        hub._facets_by_id["notify"] = RecordingNotify()
        got = p.ingest(ev("kafka.document.archived", {"doc_id": "d_1", "kind": "invoice", "merchant": "PC Maker", "amount": 1719.85,
                                                      "currency": "EUR", "date": "2026-10-01", "order_ref": "3481958"}, source="kafka"))
        res = p.enrich(got["id"], force=True)
        assert sorted(res["found"]) == ["hoard://ledger/tx/77", "hoard://phileas/shipment/s_9"]
        v = p.view(p._fetch(got["id"]))
        assert set(v["refs"]) >= {"invoice", "phileas", "ledger"}
        assert v["milestones"] == {"paid": True, "shipped": True, "delivered": False, "filed": True, "stored": False}
        assert p.enrich(got["id"])["skipped"] == "recently"          # not asked again within ten minutes
    finally:
        hub.close()
        phileas.stop()
        ledger.stop()
