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
| `mesh` | GLB, glTF, FBX, OBJ, STL, PLY, USD, DAE, `.blend` | anatomical views in neutral clay and/or own colours | `views`, `material`, `turn`, `up`, `pose`, `camera`, `engine`, `size` |
| `web` | URL, HTML file, site folder or ZIP | 390, 1024 and 1440 px, plus `-after` shots when `interaction` is given | `widths`, `interaction`, `full_page` |
| `document` | PDF, PPTX, DOCX, ODP, XLSX… | rendered pages with their text | `pages`, `size` |
| `game` | Godot project folder | `start`, the steps marked `shot`, and `end` | `inputs`, `resolution`, `scene` |
| `app` | `{app, tool, arguments}` | every image the app's own view tool returns | `crops` |

**Zones.** `crops: [{name, box: [x, y, w, h], zoom, views}]` cuts the same zone out of every view (or the named
ones) and enlarges it; the box is in fractions of the view (0–1) or in pixels. Zones are views too (`<view>--<name>`).

**Models.** Views are named after the subject's own side: `front`, `back`, `right` (its right profile), `left`,
`front_right`, `front_left`, `back_right`, `back_left`, `top`, `bottom`. The front is the glTF forward axis; when a
model faces elsewhere, `turn` rotates it (degrees, counterclockwise seen from above) and `up: "y"` stands up a model
that arrives lying down. `material` is `neutral` (clay, to read shape), `color` (its textures or vertex colours) or
`both`; a model without colours of its own says so in `warnings`. `pose` is `{action, frame}` or
`{bones: {name: [x, y, z]}}` in degrees; `facts` lists bones and actions. The camera is orthographic and the same for
every view; `resolved.camera` holds it in absolute numbers. Rendered by Blender in the background (`workbench`, or
`eevee`).

**Web.** Pages from disk are served on a temporary local port so relative links and fetches work. `interaction`
steps are `{click}`, `{fill: [selector, text]}`, `{press}`, `{hover}`, `{scroll}`, `{select: [selector, value]}` and
`{wait: ms}`; console errors land in `facts.console_errors`. Headless Chromium, or an installed Brave, Chrome or Edge.

**Documents.** Office files are converted to PDF by LibreOffice in a temporary folder with its own profile; PDF pages
are drawn with PyMuPDF.

**Games.** The project is copied to a temporary folder (Godot writes its cache into the project) and run with a
driver script that presses each `{key}` or `{action}`, waits `wait` ms and takes a screenshot when `shot` is true.
Godot needs a window to draw: it opens off screen for the few seconds of the sequence.

**Apps.** `app: {app: "vulcan", tool: "model_render_photos", arguments: {id: 12}}` calls that tool through the hub and
keeps the images its answer names (files, local URLs or inline images). Use it for objects that live in an app's
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
description and, for each detail to keep, `present`, `absent` or `unclear` with a note. Left and right in a detail
mean the subject's own side unless it says «del espectador». The look merges them per detail: `present`, `absent`,
`mixed` (views disagree: lost in some frames or angles) or `unclear`.

`vision_timeout_s` (90) bounds the whole question. What arrived by then is returned with `status: "partial"`, the
views still waiting in `pending` and failures in `failed`; the views themselves are never held back by the model.
The model's reading of left and right in renders is unreliable: open the views named in the notes before acting on
an `absent`.

## Storage and programs

Looks live in `<data>/eyes/<look id>/` (`look.json`, the PNGs, `sheet.png`, `compare.png`); forgotten ones in
`<data>/eyes/.forgotten/`. Files are served at `GET /api/eyes/files/<look id>/<file>`; `GET /api/eyes/looks`,
`GET /api/eyes/looks/<id>`, `POST /api/eyes/look` and `POST /api/eyes/forget` mirror the tools. All need the hub
token or the hub page.

Blender, ffmpeg/ffprobe, LibreOffice (`soffice`) and Godot are found through `HOARD_HUB_BLENDER`, `HOARD_HUB_FFMPEG`,
`HOARD_HUB_FFPROBE`, `HOARD_HUB_SOFFICE` and `HOARD_HUB_GODOT`, then the PATH and the usual Program Files folders.
At most two looks render at once.
