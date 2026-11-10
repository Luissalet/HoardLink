# The bus writes to the Brain (`brain`)

A rule can end in Faustus's Brain: when an event matches, the hub creates or updates an entity or a note there and
links it to others. Nobody copies anything by hand.

Module `hoard_link/hub/brainbus.py`, UI tab "Cerebro" / "Brain" (`ui/brain.js`), data `<data>/brain_bus.db` (queue,
history, what each key points to) and `<data>/brain_bus.json` (a fixed Faustus address, when one is set).

## The action

`brain` is one more action kind of a rule (`then`), next to `tool`, `hub`, `event`...:

```json
{"kind": "brain",
 "key": "phileas:shipment:${event.data.shipment_id}",
 "entity": {"name": "${event.data.person}", "type": "person", "aliases": [], "summary": ""},
 "note": {"folder": "Familia/Envíos", "title": "Envío ${event.data.merchant} ${event.data.order_ref|event.data.shipment_id}",
          "mode": "append", "date": "${event.data.due}", "tags": ["envío"],
          "text": ["Transportista: ${event.data.carrier}", "Seguimiento: ${event.data.tracking_number}"]},
 "links": [{"to": "${event.data.merchant}", "type": "organization", "rel": "related_to"},
           {"each": "${event.data.attendees}", "to": "${item.name|item}", "type": "person"}]}
```

* `entity` and `note` are both optional, one of them is required. Entity types are Faustus's: `person`, `project`,
  `organization`, `place`, `tool`, `concept`, `event`, `other`.
* `${a.b|c.d|'text'}` takes the first path with a value. A list is written joined by commas. A text line whose
  placeholders are all empty is left out, so one template serves events that carry different fields.
* `key` names the thing. The same key always lands on the same entity and the same note. An event that lacks a field
  the key is made of writes nothing. Without `key`, the entity name (or the note path) is the key.
* `note.mode`: `replace` keeps the latest text; `append` adds one dated line per event and never the same line twice.
* `links`: every target is an entity. With `entity`, each one is a relation from it (`rel`, `related_to` by default;
  Faustus's vocabulary: `works_at`, `part_of`, `knows`, `owns`, `uses`...). The note cites every entity and carries
  `[[Name]]` links to them. `each` repeats a link over a list of the event.

The note is `<folder>/<title>.md` in the vault. The hub owns only the block between `<!-- hoard-hub:begin -->` and
`<!-- hoard-hub:end -->`: what you write outside it stays.

## Once, not twice

* The same event again (same key, same content) queues nothing and writes nothing.
* A later event about the same key updates the same entity and the same note.
* Entities are found by name or alias in Faustus (`POST /api/brain/entities`), so a name already there is reused.

## Where Faustus is, and when it is not there

The target is the fixed address when one is set (the tab's "Cambiar destino", `POST /api/brain/target`,
`HOARD_HUB_BRAIN_URL`); otherwise the Spark the placement names, otherwise this PC (`faustus_urls[0]`).

Every write is a row on disk before anything is sent. Faustus not answering, a 5xx, or Faustus being moved between the
PC and a Spark leave the row `pending`; it is retried after 20 s, 1 min, 3 min, 10 min and then every 15 min, in
order, and nothing is dropped. A write Faustus refuses (4xx) is `failed` with its reason and can be retried from the
tab. A Faustus without `POST /api/brain/entities` still gets the notes (an entity on its own becomes a card note under
`Familia/<type>/`); the write is `partial` and the hub adds the entities by itself once the route answers.

States: `pending`, `done`, `partial`, `failed`. Each finished write emits `hub.brain.written {rule, key, status, note,
entity, event_id}`.

## Recommended rules

Installed with the other recommended rules (same on/off, same "removed stays removed"). `cooldown_s` is 0.

| Rule | Event | Writes |
| --- | --- | --- |
| `rule-brain-people-commitment` | `people.commitment.added`, `people.commitment.noted` | the person as an entity; the commitment as a line in "Compromisos con ‹person›" |
| `rule-brain-shipment` | `phileas.shipment.new`, `phileas.shipment.delivered` | a note per shipment, a line per news, linked to the shop |
| `rule-brain-kafka-deadline` | `kafka.deadline.created` | a note with its date |
| `rule-brain-kafka-document` | `kafka.document.archived` | a note with its date, linked to the issuer |
| `rule-brain-kafka-warranty` | `kafka.warranty.created` | a note with the end date, linked to the shop |
| `rule-brain-minutes` | `funes.minutes.ready` | a note of the meeting linked to each attendee as a person |
| `rule-brain-purchase` (off) | `purchases.stage` | a note per purchase with its stages, linked to the shop |
| `rule-brain-agora-delivery` (off) | `agora.task.done` | a note of the delivery linked to its space |

Any other event works the same way: add a rule with a `brain` action in the Rules tab or with `hub_rule_add`.

## HTTP

| Route | What |
| --- | --- |
| `GET /api/brain` (`?probe=1` asks Faustus) | `{target: {base, source: fixed\|placement\|pc, node?, moving, reachable?, entities_api?}, counts, waiting_s, last_error, rules: [{id, name, when, then, installed, enabled, sample, writes, pending, failed, last_ts}]}` |
| `GET /api/brain/history?rule=&status=&limit=` | `{writes: [{id, ts, rule, rule_name, event_id, event_type, key, plan, status, attempts, error, result, url, summary}], counts}`; `url` opens the note in Faustus |
| `POST /api/brain/test` `{rule_id \| rule, event: {type, data} \| event_id, write?, force?}` | what the rule would write: `{matches, writes: [{key, effect: create\|update\|unchanged, entity, links, note: {path, mode, date, text}}]}`; `write: true` writes it and waits (`written`) |
| `POST /api/brain/rules/<id>` `{enabled}` | turn a rule on or off; a recommended one that is missing is installed |
| `POST /api/brain/retry` `{id?}` | try again now (one write, or everything that waits or failed) |
| `POST /api/brain/target` `{url}` | fix the Faustus address; empty follows the placement |

The POST routes take a family token or the hub's own page.

## Agent tools

`hub_brain_status`, `hub_brain_rule_set`, `hub_brain_test`, `hub_brain_history`, `hub_brain_retry`,
`hub_brain_target`. Rules themselves are ordinary rules: `hub_rule_add`, `hub_rule_update`, `hub_rule_remove`.

## What it does not do

* It does not delete from the Brain: turning a rule off or removing it leaves what was written.
* It does not sign in: it reaches a Faustus that accepts the hub without a session (the family's local setup).
* An entity's vault page appears a little after the write (Faustus renders it in the background); until then the
  note's `[[Name]]` link shows as unresolved.
* A rule is a template, not a program: it cannot look anything up beyond what the event carries.
