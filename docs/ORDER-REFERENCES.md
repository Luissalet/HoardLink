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
on the detail in `hub_agora_piece_check`. A kept visual reference required by the order can be compared even when
the station did not use it as an input, including a reference from a cited order. The check records its author,
observation and evidence on the existing piece. This mark in `keep` compares a clip's preview 1.

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
that cannot be seen in the pair. At least one applicable detail must pass for each source. These records document what
the inspecting worker saw. A different piece revision needs its own observations.

Use `state: "reference_defect"` when an observed difference comes from a specific open defect recorded on the
reference with `hub_agora_piece_defect`. Include its `reference_defect_id` in each affected comparison.

```json
{
  "id": "k1",
  "state": "reference_defect",
  "comparisons": [
    {"reference_piece": 31, "reference_preview": 4, "reference_defect_id": 42,
     "observed": "The reference has a gap in the cape edge at the neck. The result has a continuous edge."}
  ]
}
```

The pair passes for that detail while the linked defect is open and the reference revision is current. Make the
difference visible in the result's title with the registered defect description or `reference defect #42` in English
or `defecto de referencia #42` in Spanish. For an existing piece, pass the optional `title` in the same check to
update the title and record the mark together. Both are covered by the check's undo token. The comparison still uses
the order's existing references, including the identity reference. Closing the defect with `hub_agora_defect_close`
or retaining a replacement revision invalidates the mark and the gate asks for a fresh comparison. If the gate
was already open, keeping the result also requires a current comparison.

En castellano, usa `state: "reference_defect"` cuando la diferencia observada provenga de un defecto abierto en la
pieza de referencia. Enlaza su `reference_defect_id` en cada par afectado. Decláralo en el título de la pieza
cotejada con la descripción registrada o con `defecto de referencia #42`. Puedes actualizar `title` en esa misma
comprobación. Si se cierra el defecto o se conserva una revisión que sustituye a la referencia, vuelve a cotejar.

`hub_agora_gate_open` returns HTTP 409 with `kept_reference_comparison` when a new visual piece with order details
lacks a passing comparison with a required visual reference. The error identifies the piece and reference, reports
`comparison_pending` or `comparison_failed`, and directs the worker to `hub_agora_piece_check`.
Retained draft guides remain inputs for pose and composition. Visual pieces kept from a production gate serve as
comparison sources. The keep or cancel decision stays on the person's page.
