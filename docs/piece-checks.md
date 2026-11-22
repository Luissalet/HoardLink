# Production piece checks

`hub_piece_validate` takes a local `path` or an Orders `piece_id` with its
`space_id`. When given an ID it uses the retained file for that revision,
or the original after Orders has verified its fingerprint. The station
catalogue lists the profiles in `handoffs[].piece_checks` and marks planned
outputs with `piece_check`. An explicit `profile` can select the intended use
of a file, such as printing a GLB. For unregistered files the Hub can also
infer a profile from a known piece kind or the file extension.

Dedicated workers use declared local paths or retained revisions granted by
an assignment. Validation by ID uses the declared retained copy. Declare any
additional reference files before passing their absolute paths. For web checks,
pass a declared ZIP. For spreadsheet checks, pass explicit sheet requirements.
The Hub does not reach the network for a dedicated worker. Web citations in a
document, deck or page stay `unmeasured`, a web page is opened with every request
outside its own local server aborted, and a deck that links external resources is
not rendered (its rendered-text check stays `unmeasured`).

Los trabajadores dedicados usan rutas locales declaradas o revisiones
conservadas que autoriza el encargo. La validación por identificador usa la
copia conservada y declarada. Declara los ficheros de referencia adicionales
antes de pasar sus rutas absolutas. Para comprobar una web, usa un ZIP declarado.
Para una hoja de cálculo, indica los requisitos de la hoja.
El Hub no sale a la red por un trabajador dedicado. Las citas web de un
documento, una presentación o una página quedan sin medir, una página web se abre
con toda petición ajena a su propio servidor local abortada, y una presentación
que enlaza recursos externos no se renderiza (su comprobación de texto
renderizado queda sin medir).

An executed request returns `ok: true` and a separate `verdict` of `pass`,
`fail` or `unmeasured`. A measured defect takes precedence over a pending
measurement. Each check includes a reason in Spanish and English, and
applicable measurements, expected values and locations. Invalid or
unreadable inputs are reported separately from a measured pass. The same
tools are available by MCP and `POST /api/agent/tools/<tool>`.

| Profile | Requirements | Measurements |
| --- | --- | --- |
| `print-mesh` | `build_volume_mm: [x,y,z]`, `min_wall_mm` | Boundary and non-manifold edges, face orientation, dimension fit and the smallest sampled wall. The fit uses the orientation in the file. Vulcan's wall sample is an estimate and needs a closed mesh and a ray engine. STL and other unitless formats use millimetres and report the assumption. |
| `game-mesh` | `max_triangles`, `bones: [name]`, `pivots: [nodeName]`, optional `skinned: true` | GLB/glTF triangle count, joint names, named node pivots, missing or invalid vertex influences on skinned meshes, and dimensions of each texture. Rigid nodes outside a skin have no vertex weights; a file with no skin passes the skin checks as not applicable unless `bones` or `skinned` asks for one. |
| `video` | `size_px`, `duration_s`, optional `duration_tolerance_s`, `interval_s`, `min_similarity`, references and `identity` | Clip format, visual continuity and subject feature matches, with scores and exact sample times grouped by shot. |
| `image` | `size_px`, optional `alpha`, `neutral_background`, `neutral_ratio`, `views`, `view_boxes` | Pixel size, alpha-channel presence, ratio of neutral or transparent border pixels, and counts of separate files or declared non-blank regions. |
| `web` | `viewports: [[w,h],...]`, optionally `resolve_remote` | Visible names, image alternatives, language and title, overlapping text, clipped text and horizontal overflow (with selectors and pixels beyond the viewport), broken links and images, and browser errors. Default sizes are 1440 × 900 and 390 × 844. |
| `slides` | Optional `slide_count`, `size_emu: [w,h]`, `resolve_remote` | PPTX title placeholders, image alternatives, overlapping boxes of the same kind, boxes outside the slide, image and link relationships, and editable words missing from the render. |
| `spreadsheet` | Optional `dataset: {name, version}`, `sheet`, `sha256` or explicit `columns` and `row_count` | XLSX header order, row count, observed cell types and the export fingerprint. The source dataset version is read through Nightingale when specified. [XLSX requirements](piece-checks-xlsx.md). |
| `document` | Optional `citations: {id: target}` and `resolve_remote` | Local paths and HTTP targets, numbered Markdown references such as `[B1, B2]`, DOCX hyperlinks and PDF URI annotations. An HTTP denial or timeout is unresolved. Source reachability gives no verdict on the claim it supports. |
| `narrative` | Optional `min_words`, `max_words`, `page_count` and `continuity_declarations: {defect_id: note}` | PDF word and page counts, pages with no extractable text, embedded font programs, ordered text comparison and continuity defects. |

For a narrative order, register the PDF with kind `pdf` and link a single
`reviewed-manuscript` as material. Link the current `manuscript` revision as
material of the review, with the original editorial dossier as context.
The technical check reads the retained DOCX or UTF-8 text of the review.
It keeps punctuation and order while normalising Unicode and layout whitespace.
Use the same text for title and body in both files. Extra text in running
headers, footers or page numbers appears as a difference.

Return continuity defects with `hub_agora_piece_defect` and close them with
`hub_agora_defect_close` against the corrected revision. The PDF check reads
the review and its material writing history, including superseded revisions.
A closure counts when its corrected revision is in the current material
lineage. To show an outstanding defect with the export, supply its native
defect ID and a note in `continuity_declarations`. The check reports that note
without closing the defect. For a local file, supply `source_path` for the
text comparison. Continuity remains pending until the review is linked in Orders.

Declare references as `[{"path": "...", "region": [left, top, right, bottom]}]`.
Regions use fractions of the frame or image. Set `region` on the clip or on
each shot to exclude irrelevant background. For a cut, supply `shots` with
`id`, `start_s`, `end_s`, `references` and optional `region` for each
continuous span. Each shot starts a new sampling interval and includes its
last decodable frame. The continuity score compares colour and gradient
histograms in the regions.

Set `identity: {min_similarity, region, references}` on the clip or a shot to
compare the subject with a crop from the sheet or a reference frame. Use
`time_s` on a reference to name a frame from a video directly. Shot settings
override clip settings. Without an identity region or reference list the
check uses the shot's continuity inputs. Supply a tight subject crop and
references that cover the visible angles. DINOv2 compares subject features on the
CPU and returns the best reference and its cosine similarity (0–1). The
threshold applies to each sample. A flat crop or a missing model leaves the
measurement pending. Calibrate the threshold for the piece and review the
sampled frames when angle, framing or lighting change.

**Identidad en vídeo y desbordamiento web.** La opción `identity` coteja los
rasgos de cada muestra con un recorte de la hoja de personaje o de un
fotograma de referencia. Devuelve la referencia más parecida, la puntuación
y los segundos que no alcanzan el umbral. La comprobación de desbordamiento
incluye el selector y los píxeles que sobresalen cuando identifica una caja
o un fragmento de texto.

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
FFprobe, Pillow and NumPy. For identity, set `HOARD_HUB_IDENTITY_MODEL` to a
local DINOv2 directory containing `config.json` and `model.safetensors`.
Set `HOARD_HUB_IDENTITY_PYTHON` if Torch, Transformers, NumPy and Pillow are
installed in a separate Python environment. It uses the installed
checkpoint and records its SHA-256. Images use Pillow. PPTX uses python-pptx and
headless LibreOffice with a temporary profile, and PDF text and URI extraction
use PyMuPDF. Missing engines leave a measurement pending. These checks do
not start a Hoard service or call the shared inference service.
