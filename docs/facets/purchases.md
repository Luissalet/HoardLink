# Purchases (`purchases`)

The life of one purchase across apps, built from the events they emit. Module `hoard_link/hub/purchases.py`, UI tab
"Compras" / "Purchases" (`ui/purchases.js`), data `<data>/purchases.db`.

```
paid -> shipped -> delivered -> filed -> stored -> closed
```

## Events (ids in `data`)

| event | effect |
|---|---|
| `ledger.mail.recorded {tx_id, merchant, amount, currency, date, order_ref?, message_id?, items?}` | creates/updates, stage `paid`, ref `ledger` = `hoard://ledger/tx/<tx_id>` |
| `phileas.shipment.new {shipment_id, merchant?, order_ref?, message_id?, items?, carrier, tracking_number}` | `shipped`, ref `phileas` = `hoard://phileas/shipment/<id>` |
| `phileas.shipment.delivered {shipment_id, ..., delivered_at}`, `phileas.update` with `status: delivered` | `delivered` |
| `kafka.document.archived {doc_id, kind, merchant?, amount?, date?, order_ref?, message_id?}` | `filed`, ref `invoice`/`receipt`/`warranty`/`document` = `hoard://kafka/document/<id>`; only purchase-like kinds (invoice, receipt, ticket, warranty, order, delivery note...) |
| `kafka.warranty.created {doc_id, shipment_id?, merchant?, until}` | ref `warranty`, `meta.warranty_until` |
| `homehoard.item.created {item_id, source_ref?}` | `stored` — needs `source_ref = hoard://hub/purchase/<id>` |
| `tantalus.watcher.bought {watcher_id, purchase_ref}` | ref `tantalus` — needs `purchase_ref = hoard://hub/purchase/<id>` |

Stages only move forward; `closed` is set by the person (`POST /api/purchases/<id>/close`) and a closed purchase takes
no more events. Each stage change emits `purchases.stage {id, stage, previous, merchant, title}`.

## Matching

In this order: the purchase the event names (`source_ref`/`purchase_ref`) -> a record of the event already in a
purchase's refs -> same normalised `order_ref` (case, punctuation and a leading `#` ignored, at least 4 characters) ->
same `message_id` -> fuzzy: similar merchant (folded, tokens, generic words such as `shop`, `s.a.` dropped), amount within
1 % when both are known, dates within 5 days (the event time stands in for a missing date; an item-less, amount-less
parcel matches on merchant and dates). A second record of the same kind (another payment, another parcel) never joins a
purchase that has one: it is another purchase. Events that can create a purchase: ledger, Phileas, Kafka documents and
warranties; HomeHoard and Tantalus events only attach.

Every new record of a purchase is linked in the `refs` facet (rel `purchase`) to the purchase's first record.

## Actions, once per purchase (recorded in `notified_json`)

* First time a payment (ledger) joins a purchase: `tantalus.watchers_match_purchase {title, merchant, url?}`; every match
  with `score >= 0.8` gets `tantalus.watcher_mark_bought {watcher_id, purchase_ref: "hoard://hub/purchase/<id>"}` and a
  normal notification "Dejo de vigilar X: ya lo has comprado". A missing app or tool is recorded and ignored.
* Delivery (and not already stored): notification "Ha llegado X. ¿Dónde lo guardas?" whose URL is HomeHoard's quick-add,
  `<homehoard url>/#/add?name=<item>&source_ref=hoard://hub/purchase/<id>&price=<amount>&merchant=<merchant>&date=<date>`
  (default `http://127.0.0.1:5196`).

Texts follow the hub language (`es`, `en`, or `auto`: Spanish unless the machine says English).

## Worker

Events are handled on the facet's own thread; `emit` never waits. The id of the last handled event is kept; after a
restart the events missed since then are replayed, and a first start does not replay history.

## HTTP and tools

`GET /api/purchases?stage=&q=&limit=` -> `{purchases, total, counts}`; `GET /api/purchases/<id>`;
`POST /api/purchases/<id>/close` and `/reopen` (a family token or the page). Tools `hub_purchases {stage, q, limit}` and
`hub_purchase {id}` (read-only).
