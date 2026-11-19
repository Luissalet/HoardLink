# Production piece checks

`hub_piece_validate` takes a local `path` or an Orders `piece_id` with its
`space_id`. When given an ID it uses the retained file for that revision,
or the original after Orders has verified its fingerprint. The station
catalogue lists the profiles in `handoffs[].piece_checks` and marks planned
outputs with `piece_check`. An explicit `profile` can select the intended use
of a file, such as printing a GLB. For unregistered files the Hub can also
infer a profile from a known piece kind or the file extension.

An executed request returns `ok: true` and a separate `verdict` of `pass`,
`fail` or `unmeasured`. A measured defect takes precedence over a pending
measurement. Each check includes a reason in Spanish and English, and
applicable measurements, expected values and locations. Invalid or
unreadable inputs are reported separately from a measured pass. The same
tools are available by MCP and `POST /api/agent/tools/<tool>`.

| Profile | Requirements | Measurements |
| --- | --- | --- |
| `print-mesh` | `build_volume_mm: [x,y,z]`, `min_wall_mm` | Boundary and non-manifold edges, face orientation, dimension fit and the smallest sampled wall. The fit uses the orientation in the file. Vulcan's wall sample is an estimate and needs a closed mesh and a ray engine. STL and other unitless formats use millimetres and report the assumption. |
| `game-mesh` | `max_triangles`, `bones: [name]`, `pivots: [nodeName]` | GLB/glTF triangle count, joint names, named node pivots, missing or invalid vertex influences on skinned meshes, and dimensions of each texture. Rigid nodes outside a skin have no vertex weights. |
| `video` | `size_px`, `duration_s`, optional `duration_tolerance_s`, `interval_s`, `min_similarity` and references | Clip format and visual continuity, with scores and exact sample times grouped by shot. |
| `image` | `size_px`, optional `alpha`, `neutral_background`, `neutral_ratio`, `views`, `view_boxes` | Pixel size, alpha-channel presence, ratio of neutral or transparent border pixels, and counts of separate files or declared non-blank regions. |
| `web` | `viewports: [[w,h],...]`, optionally `resolve_remote` | Visible names, image alternatives, language and title, overlapping text, clipped text, horizontal overflow, broken links and images, and browser errors. Default sizes are 1440 × 900 and 390 × 844. |
| `slides` | Optional `slide_count`, `size_emu: [w,h]`, `resolve_remote` | PPTX title placeholders, image alternatives, overlapping boxes of the same kind, boxes outside the slide, image and link relationships, and editable words missing from the render. |
| `document` | Optional `citations: {id: target}` and `resolve_remote` | Local paths and HTTP targets, numbered Markdown references such as `[B1, B2]`, DOCX hyperlinks and PDF URI annotations. An HTTP denial or timeout is unresolved. Source reachability gives no verdict on the claim it supports. |

Declare references as `[{"path": "...", "region": [left, top, right, bottom]}]`.
Regions use fractions of the frame or image. Set `region` on the clip or on
each shot to exclude irrelevant background. For a cut, supply `shots` with
`id`, `start_s`, `end_s`, `references` and optional `region` for each
continuous span. Each shot starts a new sampling interval and includes its
last decodable frame. The score compares colour and gradient histograms in
the regions. A change in lighting, pose, background or framing can change
the score. Identity and continuity details still need visual review.

A one-file character sheet needs `view_boxes` and an expected `views` count.
Use the same coordinate format as for video regions. The inspector counts
regions with luminance variation. A directory of individual view files can
be checked directly with `profile: "image"`. A neutral border is a measurable
proxy for a neutral background.

Use `hub_piece_validate_record` to save a fresh verdict against the order
revision, with the native Orders undo token. Use `hub_piece_look_checked`
before a visual review. It passes the checked file to Eyes with the technical
result in the look label and returns the full result alongside the look. Pass
Eyes settings in `visual`. Use `hub_piece_gate_checked` with `order_id` and
`pieces: [{piece_id, requirements}]` to open the native gate. Each verdict,
including pending measurements and defects, appears in the gate note. The
response includes the complete reports and the native gate undo token.
`language` chooses the note language, Spanish by default. The keep or cancel
decision stays in Orders. A gate opened through the ordinary Orders workflow
shows the latest recorded result or a pending check for each supported piece
revision that has no report in the note.

Mesh inspection uses the detected Vulcan environment. Web checks use the
Vitruvius browser engine headlessly on a loopback server. Video needs FFmpeg,
FFprobe, Pillow and NumPy. Images use Pillow. PPTX uses python-pptx and
headless LibreOffice with a temporary profile, and PDF text and URI extraction
use PyMuPDF. Missing engines leave a measurement pending. These checks do
not start a Hoard service or request inference.
