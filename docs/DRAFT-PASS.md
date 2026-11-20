# Draft pass (pasada 0) and each station's draft mode

Before anything expensive runs, an order can go through its whole chain in draft, in two to four structural variants,
and the person chooses the structure at an early gate. The expensive pass then starts from the kept variant and is
anchored to it. Nothing discarded is deleted.

Code: `hoard_link/hub/agora_drafts.py` (store and operations, folded into the Ágora next to the orders),
`hoard_link/hub/station_drafts.py` with `station_drafts.json` (draft modes), and the page script
`hoard_link/hub/ui/drafts.js` with `drafts.css`, which adds the early gate to the Orders page. Tables in the Ágora
database: `agora_draft_passes`, `agora_draft_variants`, `agora_draft_pieces`.

## Draft mode of a station

Each production handoff of a Hoard (`x-family.production.handoffs[]` in its `faustus-plugin.json`) may say how the
station makes a cheap version of what it delivers:

```json
"draft": {"how": {"es": "…", "en": "…"}, "seconds": 70, "gpu": true,
          "settings": {"template": "qwen21_edit", "steps": 8, "width": 512, "height": 768},
          "tool": "studio_generate_image", "produces": "draft-image", "anchors": ["composition"],
          "measured": "where the time comes from"}
```

`how` and `seconds` are required; `anchors` says what the draft leaves for the expensive pass. Until a Hoard
declares its own, the Hub carries a starting declaration in `station_drafts.json` (shown as `declared_in: "hub"`); the
manifest's block always wins (`"manifest"`). Starting declarations: Writer (outline of the brief), Prospero (few-step
low-resolution image over the blocking; front and back only for the sheet; animatic of key frames without
in-betweens), Vulcan (primitive blocking already split in parts and rigged, with flat colour, depth and normals from the
shot's camera), Phidias (closing of the posed blocking without repair), Cicero (slide outline in text) and Vitruvius
(page outline in text).

`hub_station_drafts` lists every handoff with its draft mode and, with `from_type` and `to_type`, each chain with
`draft_seconds`, `draft_gpu_seconds` and `without_draft` (the steps that have no draft mode yet).

## Draft pass

| Operation | Who | Does |
|---|---|---|
| `draft_pass_open` | an agent | Opens a pass on an order: `variants` [{`key`, `title`, `idea`, `jobs`}], `baseline` (time and GPU of a full pass, to compare), `note`. A variant's `jobs` are plan jobs (as in `plan_set`): they become Ágora tasks of the order on branch `pasada0-<key>`, titled «[Pasada 0 · key] …», with `after` naming jobs of the same variant. |
| `draft_variant_set` | an agent | Attaches pieces of the order to a variant with their `anchor`, notes `measures` (`seconds`, `gpu_seconds`, `gpu`, `engine`, `steps`, `resolution`, `device`, `note`) and sets `status` `drafting` or `ready` (ready needs at least one piece). `remove` takes pieces out. |
| `draft_gate_open` | an agent | Shows the pass to the person once every variant is ready. The order waits. |
| `draft_decide` | only the person's pages | `variant_id`, `decision` `keep` or `cancel`, `note` and `signature` (the variant's signature as the page showed it). Keeping one discards the others still undecided, with a note that says which one was kept. |
| `draft_passes` | read | The passes of an order, or (no `order_id`) those waiting for the person (`all_spaces`). Each variant with its pieces by anchor, measures and decision note; totals against the baseline. |
| `draft_anchors` | read | For `order_id` or the `task_id` of one of its jobs: the kept variant, its pieces by anchor, the guides and the `derives_from` to pass to `piece_add`. |

Anchors are short slugs. Those the guides know: `depth`, `normals`, `composition`, `blocking_color` (image),
`keyframe_start`, `keyframe_end`, `keyframe`, `animatic` (video), `blocking`, `parts` (splitting the model) and
`outline` (text). Any other slug is kept under `other`.

What a decision does:

* **Keep**: the variant becomes `kept`, its pieces become kept pieces of the order, so `job` hands them to every later
  job like anything else the person kept, and `draft_anchors` returns them to the expensive pass.
* **Cancel** (discard): the variant becomes `discarded` with the person's note; its pieces are marked cancelled
  («variante descartada en la pasada 0») and whatever was made from them becomes stale. Nothing is deleted and nothing
  is sent back to a station: a discarded draft is not a defect.
* **A piece shared by several variants** (the same blocking of the staircase in B and C, say) follows the variant that
  keeps it: discarding C leaves it alone while B is kept or still undecided, whatever the order of the decisions, and
  only C's own pieces are cancelled. It is cancelled when the last variant using it is discarded. The decision lists
  those pieces under `shared`, and the page marks them «also in B».
* **What the person saw is what is decided.** Each variant carries a `signature` of what the person judges: its title
  and idea and, for each piece, its id, anchor, revision and bytes (not measures, notes or checks). An agent may still
  change a variant while the gate is open (`draft_variant_set` then answers `shown_again: true`), but a decision sent
  with the signature of what the page showed before is refused with 409 `variant_changed`; the page then shows the
  variant as it is now, marked «it changed while you were looking at it», and the person decides again. A piece whose
  file changed after it was registered, with no kept copy, is refused with `piece_changed`, as at any gate.
* Each decision is stored per piece in the order's decisions with its situation (the pass, the variant and the
  alternatives that were on the table). When every variant is decided the pass is `decided` and the order goes back to
  production.

The expensive pass reads `draft_anchors` and registers its pieces with the `derives_from` it returns: the blocking and
its parts as `material`, the rest as `reference`. Those pieces then carry the id of the person's decision.

With an edit engine that takes several reference images (Prospero's `qwen21_edit`), the character's canonical image
goes first and the kept variant's key frame, depth and normals follow as guides, each named in the prompt: given the
blocking as the first image, the engine keeps the blocking's mannequin instead of the character. A video shot between
two key frames is Prospero's `wan22_flf2v` (`reference_asset_id` the first frame, `end_asset_id` the last).

Every agent write returns `undo` for `hub_agora_order_undo`; undoing a pass opened with jobs also drops the tasks it
created.

## The early gate on the Orders page

While a pass waits, the Orders page shows it at the top, before the other gates: one card per variant, side by side,
with its idea, time and GPU, and its pieces end to end (blocking, depth, normals, key frames, draft image…), each one
opening at full size. The person writes an optional note and presses **Keep this one** or **Discard**. Texts in Spanish
and English.

## Tools

| Tool | Does |
|---|---|
| `hub_station_drafts` | Each station's draft mode and the draft time of a chain |
| `hub_agora_draft_pass_open` | Open a draft pass with its variants (and their jobs) |
| `hub_agora_draft_variant_set` | Pieces by anchor, measures and readiness of a variant |
| `hub_agora_draft_gate_open` | Show the pass to the person |
| `hub_agora_draft_passes` | Read the passes of an order or those waiting |
| `hub_agora_draft_anchors` | What the expensive pass starts from |

HTTP: `GET /api/agora/draft_passes`, `GET /api/agora/draft_anchors`, `POST /api/agora/<operation>` with a bearer token
(or from the page; `draft_decide` only from the person's pages).
