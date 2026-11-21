# Kept references in production

Workers start image, video and volume jobs with `hub_agora_job` and, for a later pass, `hub_agora_draft_anchors`.
Both return `required_references` for the current order and orders cited in its `refs`. Each entry identifies the
kept piece revision, its decision and note, a retained file and previews when available, and their Hub file URLs.
Use `refs: [{"order_id": 12}]` to cite an order in the same space. Existing order references can name an order
as `orden #12` or `order #12` in the reference note, point to its Hub order API URL, or point to a registered file
or preview. References to orders in other spaces do not expand.

Use the retained inputs directly. For Prospero, import with `studio_import` and the `piece_id`, adding `preview`
when the relevant view is a preview. Use the imported asset in the image or video reference input. A volume job can
use the retained image path. Draft composition, depth, normals and blocking still come in their existing guides.
`draft_anchors.derives_from` includes the kept references and retains the draft guides' roles. Register the references
the station actually used. `piece_add` warns about omitted lineage, including on a retry. A usable chain through an
intermediate piece counts. The Hub does not attach evidence of work the station has not reported.

## Record a visual comparison

For each order detail to preserve, open the retained visual reference and the result. For a clip or model,
register relevant stills or turntable previews with the piece and select the matching view. Record the observation
on the detail in `hub_agora_piece_check`. For example, a check on a clip with the result in preview 1 can include
this mark in `keep`:

```json
{
  "id": "k1",
  "state": "ok",
  "comparisons": [
    {"reference_piece": 31, "piece_preview": 1, "observed": "Both views show the braid over the short grey cape."}
  ]
}
```

Add `reference_preview` to select a retained preview of the reference. Without a preview index the comparison uses
the retained image file. The successful write returns an undo token and a check with both view URLs, both revision
numbers and their hashes. Use `state: "missing"` for a mismatch. `not_visible` records an observation about a detail
that cannot be seen in the pair. At least one applicable detail must match each source. These records document what
the inspecting worker saw. A different piece revision needs its own observations.

`hub_agora_gate_open` returns HTTP 409 with `kept_reference_comparison` if a new visual piece with order details has
a missing lineage link, a missing observation, or a failed comparison against a required visual reference. The error
identifies the affected piece and source. Retained draft guides remain inputs for pose and composition. Visual pieces
kept from a production gate serve as comparison sources. The gate opens only after the relevant checks pass. The
keep or cancel decision stays on the person's page.
