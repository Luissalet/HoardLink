# Eyes: looking at any piece

The **eyes** facet (`hoard_link/hub/eyes.py`) gives every agent one way to see what a station produced: it turns
a piece into PNG views that can be opened, one contact sheet with all of them, and, on request, a description and
a check of «details to keep» by the shared vision model. Each look is kept with the exact camera, pose, seconds,
pages, widths or inputs it used, so the same look can be taken again on the next revision and a defect can go back
to the previous station with the same view.

## Tools

| Tool | Writes | What it does |
|---|---|---|
| `hub_eyes_look` | yes | Look at a piece (`path`, `url` or `app`); returns views, sheet, facts, `repeat` and `undo`. |
| `hub_eyes_get` | no | One look by id, with its progress while it is still running. |
| `hub_eyes_list` | no | Looks of a piece by `path`, `sha256`, `label` or `kind`, newest first. |
| `hub_eyes_forget` | yes | Moves a look aside; `restore: true` brings it back. Each answer carries the opposite call as `undo`. |

`hub_eyes_look` answers within `wait_s` (75 s by default, below the MCP bridge's 90 s). Longer work keeps running:
the answer has `status: "running"` and a `poll` call to `hub_eyes_get`.

## Pieces

The kind is detected from the file and can be forced with `kind`.

| Kind | Piece | Views | Options |
|---|---|---|---|
| `image` | PNG, JPEG, WebP… | the image (longest side `size`, 1600) | `crops` |
| `video` | MP4, MOV, WebM… | frames at `seconds`, or `frames` spread from start to end (8) | `seconds`, `frames`, `size`, `crops` |
| `mesh` | GLB, glTF, FBX, OBJ, STL, PLY, USD, DAE, 3MF, `.blend` | anatomical views in neutral clay and/or own colours | `views`, `material`, `front_axis`, `turn`, `up`, `pose`, `camera`, `engine`, `size` |
| `web` | URL, HTML file, site folder or ZIP | 390, 1024 and 1440 px, plus `-after` shots when `interaction` is given | `widths`, `interaction`, `full_page` |
| `document` | PDF, PPTX, DOCX, ODP, XLSX… | rendered pages with their text | `pages`, `size` |
| `game` | Godot project folder | `start`, the steps marked `shot`, and `end` | `inputs`, `resolution`, `scene` |
| `app` | `{app, tool, arguments}` | every image the app's own view tool returns | `crops` |

**Zones.** `crops: [{name, box: [x, y, w, h], zoom, views}]` cuts the same zone out of every view (or the named
ones) and enlarges it; the box is in fractions of the view (0–1) or in pixels. Zones are views too (`<view>--<name>`).

**Models.** Views are named after the subject's own side: `front`, `back`, `right` (its right profile), `left`,
`front_right`, `front_left`, `back_right`, `back_left`, `top`, `bottom`. The default front is Blender's `-y`
(glTF `+Z`). Declare a different front with `front_axis: "-x"`, `"+x"`, `"-y"` or `"+y"` in Blender's
world coordinates after standing the model up. The latest successful declaration is saved against the piece's
SHA-256 and reused for those bytes. The look identifies the declaration, and forgetting or restoring it undoes
or restores reuse. A different revision can be declared separately. Use `turn` by itself for a one-off rotation
(degrees, counterclockwise seen from above). `up: "y"` stands up a model that arrives lying down.
`material` is `neutral` (clay, to read shape), `color` (its textures or vertex colours) or
`both`; a model without colours of its own says so in `warnings`. `pose` is `{action, frame}` or
`{bones: {name: [x, y, z]}}` in degrees; `facts` lists bones and actions. The camera is orthographic and the same for
every view; `resolved.camera` holds it in absolute numbers. Blender renders in the background with Workbench by
default. Colour views of GLB and glTF models that combine vertex colours and textures use Eevee to show both in
the same image. Neutral views keep the requested engine. If a mixed model is rendered in Workbench, `warnings`
reports that the colour view omits vertex colours. `resolved.engine` records the requested engine and
`resolved.material_engines` records which engine rendered each material mode.

**3MF.** The Hub uses trimesh and its 3MF dependencies to read the build, then renders its geometry in Blender.
The preview follows the build transforms and converts the declared unit to metres. Print materials and colour
assignments are unavailable in this preview. For a topology or print-volume assessment use `print-mesh`.
Declare the model's front with the same `front_axis` option in the 3MF build's Z-up coordinates. A 3MF producer
can also set `<metadata name="hoard:FrontAxis">+y</metadata>` before the resources in `3D/3dmodel.model`,
with `xmlns:hoard="urn:hoard:eyes"` on the model. An explicit or saved look declaration takes precedence.

**Web.** Pages from disk are served on a temporary local port so relative links and fetches work. `interaction`
steps are `{click}`, `{fill: [selector, text]}`, `{press}`, `{hover}`, `{scroll}`, `{select: [selector, value]}` and
`{wait: ms}`; console errors land in `facts.console_errors`. Headless Chromium, or an installed Brave, Chrome or Edge.

**Documents.** Office files are converted to PDF by LibreOffice in a temporary folder with its own profile; PDF pages
are drawn with PyMuPDF.

**Games.** The project is copied to a temporary folder (Godot writes its cache into the project) and run with a
driver script that presses each `{key}` or `{action}`, waits `wait` ms and takes a screenshot when `shot` is true.
Godot needs a window to draw: it opens off screen for the few seconds of the sequence.

**Apps.** `app: {app: "vulcan", tool: "model_render_photos", arguments: {id: 12}}` calls that tool through the hub and
keeps the images its answer names (files, local URLs or inline images). A native PNG in an `image_base64` field
also becomes a view, for example `data_chart` with `action: "get"` and `image: true`. Use it for objects that live in an app's
library; files on disk are looked at directly, without importing them anywhere.

## Repeating a look and comparing revisions

`same_as: <look id>` takes the previous look's piece, kind and exact parameters — camera, turn, pose, seconds,
pages, widths, interaction, inputs and zones — and lets the call override any of them (usually `path`, the new
revision). The answer adds `compare`: one image with the previous views on the left and the new ones on the right,
row by row, and whether the piece bytes are the same (`same_piece`). A defect is returned to the previous station as
the look id plus the view name: «e20261119-221528-99bab0, `left-color`, zone `tubo`».

## Vision

`describe: true` or a `keep` list asks the hub's `vision` capability (the Sparks) about the views: one call per
view, at most `vision_views` (6) spread over the look, or exactly the views in `ask_views`. Each answer has a short
description and, for each detail to keep, whether the view shows it (`present`, `absent` or `unclear`) with a note.

Left and right are not left to the model. A detail such as «tubo de planos a la izquierda del personaje» is split
into the thing to look for («tubo de planos») and the side it must be on; the model is asked once per distinct thing
whether it sees it and where it is in the image as the viewer sees it (a box in 0-1000 coordinates, the main
figure's box and a word). The look then works out the side:

- the box is measured against the centre of the main figure (the image's centre for a side «del espectador»); a
  box and a word that contradict each other leave the place unknown;
- the view's orientation (`facing` on each view) turns that place into the subject's side: `front` (and the
  three-quarter fronts) mirror it, `back` keeps it, `profile` cannot tell. Model views know their orientation from
  the camera (after `turn`); for pictures, videos, pages and app images the caller declares it with `facing`
  (`front`, `back`, `profile`), otherwise it is `unknown`. `same_as` repeats it.

Sides in a detail are the subject's own unless it says the viewer's («del espectador», «de la imagen»,
«viewer's»). Per view, a thing that is seen but whose side does not match, or cannot be told (no orientation, a
profile, a place in the middle or unknown), is `doubtful` with a `reason`; `absent` only means the thing itself is
not seen. Each view row keeps the model's answer (`seen`), `where`, `facing`, the derived `side` and `side_ok`.

The look merges the views per detail: `present`, `absent` (no view sees it), `mixed` (seen in some views and missing
in others: lost in some frames or hidden from some angles), `doubtful` (seen, but its side is wrong or could not be
told; with the first `reason`) or `unclear`. A front view that confirms the side is not overruled by a profile that
cannot tell it.

`vision_timeout_s` (90) bounds the whole question. What arrived by then is returned with `status: "partial"`, the
views still waiting in `pending` and failures in `failed`; the views themselves are never held back by the model.
Open the views named in a `doubtful` or `absent` verdict before sending a piece back.

## Storage and programs

Looks live in `<data>/eyes/<look id>/` (`look.json`, the PNGs, `sheet.png`, `compare.png`); forgotten ones in
`<data>/eyes/.forgotten/`. Files are served at `GET /api/eyes/files/<look id>/<file>`; `GET /api/eyes/looks`,
`GET /api/eyes/looks/<id>`, `POST /api/eyes/look` and `POST /api/eyes/forget` mirror the tools. All need the hub
token or the hub page.

Blender, ffmpeg/ffprobe, LibreOffice (`soffice`) and Godot are found through `HOARD_HUB_BLENDER`, `HOARD_HUB_FFMPEG`,
`HOARD_HUB_FFPROBE`, `HOARD_HUB_SOFFICE` and `HOARD_HUB_GODOT`, then the PATH and the usual Program Files folders.
At most two looks render at once.
