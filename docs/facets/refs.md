# References between apps (`refs`)

Apps already put `hoard://<app>/<kind>/<id>` in their records (`hoard_link.artifacts`). Each app only knows its own
side of a link; the hub keeps the graph so either end can find the other ("what is connected to this payment?").

Module `hoard_link/hub/refs.py`, UI tab "Enlaces" / "Links" (`ui/refs.js`), data `<data>/refs.db`.

## Store

`edges(id, from_uri, to_uri, rel, from_label, to_label, note, by_app, ts, UNIQUE(from_uri, to_uri, rel))`.
A link is stored directed (`from` -> `to`, named by `rel`: `related`, `purchase`, `source`...) and read both ways.
URIs are validated and normalised with `artifacts.parse_ref`; a record cannot link to itself.

## Python API (other facets call it)

| call | answer |
|---|---|
| `link(from_uri, to_uri, rel="related", *, from_label="", to_label="", note="", by="hub")` | `{ok, created, edge}`; idempotent: an existing edge only gains labels it lacked (a label is never blanked) and emits nothing |
| `around(uri, depth=1)` | `{ok, uri, depth, nodes: [{uri, app, kind, id, label, app_url, depth}], edges: [{id, from, to, rel, from_label, to_label, note, by, ts}]}`; depth 1..3 |
| `unlink(from_uri, to_uri, rel=None, *, edge_id=None)` | `{ok, removed}` |
| `recent(limit=30)` | newest edges |
| `search_labels(q, limit=10)` | records whose label or URI holds every word of `q` (accents and case folded): `{title, snippet, id, url, uri, app, kind, score, links}` — used by the global search |

`app_url` is the base URL of the installed app (empty when it is not installed here); nodes never carry a deep link,
because no URL scheme is assumed beyond the app's own address.

Each new edge emits `refs.linked {from, to, rel}` (source `hub`).

## HTTP

* `POST /api/refs` `{from, to, rel?, from_label?, to_label?, note?, by?}` — a family token: the calling app must own
  `from` or `to` (403 otherwise, 401 without a token); the hub's token and the page may link anything (and name `by`).
* `GET /api/refs?uri=<hoard://...>&depth=1|2` — the neighbourhood. `?q=<fragment>` — records by label (a `q` that
  starts with `hoard://` is treated as `uri`). No parameters — the latest links.
* `GET /api/refs/recent?limit=`.
* `POST /api/refs/remove` `{id}` or `{from, to, rel?}` — same ownership rule.

## Tools

`hub_refs {uri, q, depth, limit}` (read-only) and `hub_ref_link {from, to, rel, from_label, to_label, note}`.

## From an app

```python
from .hoard_link import fam_refs
fam_refs.link("hoard://ledger/tx/12", "hoard://kafka/document/7", "purchase", from_label="Amazon 23.90 EUR")
fam_refs.around("hoard://ledger/tx/12")      # {"ok": True, "nodes": [...], "edges": [...]}
```

`hoard_link/fam_refs.py` (Python, standard library) and `js/parts/refs.js` (`refsLink`, `refsAround`, `refsUnlink`,
merged into `js/hoard-link.js`). Both are blocking/awaitable, 5 s, and never raise: with the hub away they answer
`{"ok": false, "error": "hub unreachable"}`. They need `family.configure(...)` so the hub knows who is speaking.

## UI

Search box (a URI or a label fragment, 1 or 2 hops), the neighbourhood grouped by app with the direction and `rel` of
each link, every end clickable, and the recent links with a remove button.
