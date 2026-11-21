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
in-betweens), Vulcan (`blocking_build` for a rigged primitive take with an editable scene, animated GLB and colour,
depth and normals from the same camera at each key), Phidias (closing of the posed blocking without repair),
Cicero (slide outline in text) and Vitruvius (page outline in text).

`hub_station_drafts` lists every handoff with its draft mode and, with `from_type` and `to_type`, each chain with
`draft_seconds`, `draft_gpu_seconds` and `without_draft` (the steps that have no draft mode yet).

## Draft pass

For a visual pass, call `hub_agora_draft_pass_open` with `template: "visual"` and each variant's `key`, `title`
and `idea`. The Hub creates a separate plan for each variant in this order:

1. **Writer** outlines the gesture, camera and details to keep.
2. **Vulcan** builds the rigged blocking from that outline. Keep the GLB and the colour, depth and normals for
   each key pose from the same camera. `blocking_build` with `gesture: "pass"` moves the map from the character's
   left hand to the right by default. Set `pass_direction: "right_to_left"` for the opposite gesture.
3. **Prospero** makes draft image keys guided by the corresponding blocking images and depth maps. Check the
   contact with the map, the silhouette and the props at each key, including the two-handed pose of a pass.
4. **Prospero** assembles the keys into a draft shot. In the editing module, import the stills with `media_import`,
   place them in order with `timeline_edit` and export with `render_start`. **Phidias** can work in parallel
   from the posed blocking. On the Orders page, pause the aligned take and select **Make a figure from this frame**
   (**Hacer figura de este fotograma**). The action queues a production job with the pinned take, blocking, frame at
   24 fps and 216 mm size. It leaves the draft gate open. An agent can queue the same work with
   `hub_agora_draft_figure_request` and undo it with `hub_agora_order_undo`. Follow the queued job:
   `blocking_freeze` exports the evaluated GLB and grip receipt. Register the static GLB as a derived piece and
   pass its ID, hash and size to `print_import`. Run `sculpt_start`, then `close_start`, and check the exported
   3MF for contacts, dimensions and topology before slicing.

Attach the pieces to the corresponding variant after each job. The next job can locate them with
`hub_agora_draft_passes` by order and variant. A variant can override the template with its own `jobs`, including
an empty list when it only needs to register existing pieces. Omitting `template` keeps the optional custom plan.

| Operation | Who | Does |
|---|---|---|
| `draft_pass_open` | an agent | Opens a pass on an order: `variants` [{`key`, `title`, `idea`, `jobs`}], optional `template: "visual"`, `baseline` (time and GPU of a full pass, to compare), `note`. A variant's `jobs` are plan jobs (as in `plan_set`): they become Ágora tasks of the order on branch `pasada0-<key>`, titled «[Pasada 0 · key] …», with `after` naming jobs of the same variant. |
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

For draft keys with Prospero's `qwen21_edit`, try the matching blocking colour first, the canonical character image
second and the matching depth third. Name the references in the prompt. Image references can carry mannequin parts
or duplicate props, so inspect the generated key before using it. Use the accepted draft keys to make the animatic.
For a generated shot between two key frames, Prospero's `wan22_flf2v` uses `reference_asset_id` for the first frame
and `end_asset_id` for the last.

Every agent write returns `undo` for `hub_agora_order_undo`; undoing a pass opened with jobs also drops the tasks it
created.

## The early gate on the Orders page

While a pass waits, the Orders page shows it at the top, before the other gates. Each variant leads with its first
and last key frames and a playable short take when available. Other key frames and draft images follow. Still images
open at full size. **See how it was made** expands the production files, including blocking, depth and normals,
and shows the variant's time and GPU. The person writes an optional note and presses **Keep this one** or **Discard**.
The interface is available in Spanish and English.

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


### Figure from a registered frame

Use `hub_agora_draft_figure_request` with the order ID, the registered image ID
in `frame_piece`, and seconds into the aligned take. Pass `model_piece` when an
order has multiple articulated GLBs. The result pins both revisions and gives
`freeze_arguments` for baking the articulated GLB at 24 fps with print size and a 2 mm wall for the blocking's paper. Record the model as
material and the image as a reference using the returned `derives_from`. If
there is no articulated model, the tool asks for one before creating any job.
The figure job checks the closed mesh, transferred colours and sliced layers.
