# Production orders, pieces and the person's gate

An **order** is what the person asks for, in their words. Whoever leads splits it into **jobs** (ordinary Agora tasks),
the stations deliver **pieces**, check them and send defects back to the job that caused them, and the person decides at
**gates** whether to keep or cancel what is shown. The same machinery serves any branch: a scene that becomes concept
art, video, a model, a render, a playable scene and a printable figure; a report and its slides; a web; a feature of the
ecosystem itself.

Code: `hoard_link/hub/agora_orders.py` (store and operations, folded into the Agora like `agora_threads.py`), the page
`hoard_link/hub/ui/orders.js` and `orders.css`. Tables live in the Agora database (`agora_orders`, `agora_order_jobs`,
`agora_pieces`, `agora_piece_checks`, `agora_piece_defects`, `agora_gates`, `agora_gate_pieces`,
`agora_order_decisions`, `agora_order_journal`).

## Order

`title`, `words` (the request as the person wrote it), `refs` (where it comes from: `{app, object, place, path, url,
note}`, for example a chapter of a Writer project or a file), `deliverables`, `template` (the branch it follows, such as
`produccion-visual`, `presentacion`, `software-web`), `keep` (details no piece may lose, such as «the tube on her left»),
`folder` (where its files live; piece paths may be relative to it) and `deadline`.

States: `open` → `producing` (it has a plan) → `waiting` (a gate is open) → back to `producing` when every gate is
decided → `delivered`, or `cancelled` at any point. Opening and deciding gates moves the state; `order_update` sets it by
hand.

An order is written from the Hub's page (Orders → New order), by a tool from a Faustus chat or an agent, or by any Hoard
over `POST /api/agora/order_create` with its bearer token and `refs` pointing at the selection it was made on.

The [production dossier](order-dossier.md) adds the audience, constraints and material fingerprints to the request,
deliverables and details to keep. It can carry a source station's compiled context with its provenance.
The [source picker and job ownership](order-intake.md) guide covers choosing a native dossier in the form, recording
repositories, and opening the page against an isolated test catalogue.

## Work admission and budgets

See [Studio work admission](STUDIO-ADMISSION.md) for machine turns, retry and GPU budgets, capability routing, and
first-pass and station measures. Orders shows a budget hold alongside the gate and includes its reason in the worker's job.
Construction tasks can declare a station with `context.station` in Agora. Station measures also use a task's
repository or, for closed historical tasks, the registered repositories identified by their submitted commits.
A task spanning several repositories is counted once against the combined set of stations and repositories.
The configured ComfyUI checkout participates when it is a Git repository. Tasks without enough evidence stay
unattributed.

## Plan

`plan_set` adds, changes or removes jobs. Each new job is an Agora task of the order's space, labelled `order-<id>`, with
its body naming the station and the pieces; claims, locks, review, checkpoints and dependencies are the Agora's own.
Beside the task the order keeps:

| Field | Meaning |
|---|---|
| `station`, `skill` | The Hoard that does the job and the skill it is done with |
| `repo` | Repository recorded on the Agora task, defaulting to the discovered station's repository |
| `inputs` | Pieces it starts from |
| `produces` | Kinds of piece it must deliver |
| `check` | How its output is checked (also the task's acceptance) |
| `assignee` | `{agent, model, machine}` it is meant for (the agent is mentioned in the task) |
| `after` | Jobs it waits for: keys of the same call or task ids (Agora dependencies) |
| `branch` | A name for a parallel branch |

Parallel branches and sibling derivatives are jobs that wait on the same job. `task_id` changes a job; `remove` takes jobs
out of the plan (an open task is dropped, a started one keeps its state).

Keeping a piece can satisfy the same order's `after` dependency on the job that produced it while that job's
review continues. When a job has explicit inputs from that predecessor, the handoff via a decision waits for
those inputs to be kept. Other outstanding dependencies still apply. A keep also lets an order's next pass
start at an unstarted root with no explicit predecessor or output when the order returns to production.
The Hub opens a blocked job for its owner to continue or for an agent to claim, and posts the kept pieces
and the person's note in the job thread. It mentions the task owner, planned assignee or order producer,
in that order. The decision response includes the IDs in `ready_jobs`.

On the Orders page, **Undo last decision** sends the decision's `undo` token to `order_undo` and restores its
pieces, gate, task states and generated messages. The person's page is required for that token. If a task,
piece or another affected row has moved on, the request returns a conflict and leaves it intact.

Each decision also posts a notice in the order's Agora thread. The order exposes its `thread_id` once
that thread exists. The space's direction, foreman and gate opener receive the outcome and note through
their inbox and heartbeat. [See how the notice is delivered and acknowledged](AGORA.md#production-orders-pieces-and-the-persons-gate).

`job` (task id) is what a worker reads to start: the order's dossier; the station and
check; the input pieces; the pieces the person kept in this order with their notes (they fix the look); the defects sent
back to this job; and an earlier kept piece of each kind it delivers, from another order.

## Piece

| Field | Meaning |
|---|---|
| `kind` | `brief`, `text`, `image`, `character_sheet`, `video`, `audio`, `mesh`, `rigged_model`, `set`, `render`, `playable_scene`, `figure`, `document`, `slides`, `web`, `code_branch`, `feature`, `report`, `data`, or any short slug |
| `path`, `url`, `app` + `object` + `place` + `source_rev` | Where it is: a file, an address, or an object of an app at an exact place and revision |
| `previews` | Images that show it (turntable frames of a mesh, screenshots of a web, slides) |
| `task_id`, `station`, `by`, `model` | Who made it |
| `derives_from` | `[{piece, role, note}]`: `material` (made from it) or `reference` (only looked at it) |
| `keep` | Details to keep: a union of the order's, every detail of the pieces it derives from, every detail of the revision it replaces and the ones given here (given ones add, never replace; one already inherited keeps its origin); each starts unchecked |
| `revision`, `sha256`, `supersedes` | Its revision, content print and the piece it replaces |

States: `provisional` → `checked` (a passing check left no detail missing or unchecked) → `kept` by the person, or
`cancelled` by the person, or `stale` (made from a cancelled or stale piece).

* **Same output, same piece.** The logical output is the job, the kind and the place (file, address or app object). A
  retry with the same content (the file's print, or the object's `source_rev`) and the same inputs returns the piece it
  already is (`existing`); other content is its next revision, superseding the previous one. The same bytes from another
  job or of another kind are a piece of their own, with their own lineage.
* **Each revision keeps its bytes.** A registered file (and each preview) is copied by content into the Agora's folder
  (`order_pieces/` beside the database; a local web page with the files of its folder, printed as a whole: changing only a style or an image makes another revision), and that copy is what the page
  shows and the person decides on, even after the station overwrites the file. A file too big to copy (over 2 GB, or a
  site over 64 MB) is checked against its print instead: if it changed, it is not shown and the gate refuses the decision
  (`piece_changed`) until the station registers it again. `bytes` says `kept` or `unverified`.
* **Order thumbnails.** The order list uses the first registered preview as the thumbnail, including for documents
  such as editable slide decks and PDFs. An image with no registered preview uses the image file itself.
* **Material and reference.** Cancelling a piece makes stale every piece made from it, directly or not, and nothing is
  deleted. A piece that only used it as a reference stays valid and carries a warning that its reference was
  cancelled. A piece with no inputs and no job reads as «provenance not given».
* **Takes and frames.** Register a render or a pose with its articulated GLB as material. When a take uses that
  render or pose, the Hub carries the GLB revision onto the take and onto a frame extracted from it. Keep the
  render and take links as well. The figure request uses the pinned material GLB when the lineage identifies one.
  A take or frame without a usable articulated origin offers **Request articulated model** on its piece card.
  Agents can queue the same modelling job with `hub_agora_draft_model_request`; the order's usual undo applies.
* **Decisions travel.** A piece made from or looking at a kept piece carries the id of the person's decision that kept
  it.
* **Checks** (`piece_check`): `note`, `ok`, and per detail (`id` or text) a `state` — `ok`, `missing` or `not_visible`
  (the detail cannot be seen in this piece, which is not a failure) — with `how` and `evidence`. Inheriting a detail never
  inherits its approval: every piece is checked on its own. When an earlier check or the piece note declares work
  pending, record what you verified and how in a passing check. Send `resolves_pending` with the pending check IDs,
  `"piece_note"`, or both, as identified in the gate refusal. The original notes remain in the history.
* **Details travel with the relay.** A piece receives the details of every piece it derives from (material or
  reference), so a detail first set at one station («square buckle» on the sheet, not in the order) reaches the next
  station to be checked there. Each detail says where it was set: `source` `order`, or `piece` with `from` (the piece
  that set it) and `via` (the parent it came through). A new revision of the same piece (`supersedes`, or other
  content under the same name) keeps every detail of the revision it replaces, with its origin and unchecked again;
  neither that revision's checks nor the person's decision on it carry over.
* **Defects** (`piece_defect`): `note`, `expected`, `observed`, `evidence`, and where it shows: `view`, `pose`, `at`
  (second), `step` (slide, block, size, steps). It goes to the job that made the piece (or `task_id`): its task hears it
  in its thread, mentioning the owner. `defect_close` closes it with the piece (`fixed_in`, usually the next revision)
  where the same check passed again. The defect still applies to the original when the fix is on a different piece.

## Gate

`gate_open` shows pieces of an order to the person for one decision. Before opening, it verifies each selected revision
has a passing latest check, its required details are marked, and its recorded checks have no unresolved pending work.
A refusal gives the reason in Spanish and English and leaves the order and its pending decisions as they were.

Record known defects on the affected piece with `piece_defect` and route required fixes back to the originating job.
Defects are required by default, including older records. For a defect outside the brief, send `required: false` and
use the same short, specific defect description in `piece_defect.note`, the piece title and the first line of its note.
The title puts the limitation where the person can see it when deciding. The gate note can repeat a disclosed defect.
The Hub recognises common Spanish and English phrases for pending work and defects in free-form notes. Record defects
explicitly so the rule can evaluate them independently of wording.

On the Orders page each piece has the preview its
kind needs — image, turntable frames (a mesh with previews), video, audio, a web (its screenshots first, and «See it
working» loads it in a sandboxed frame without the page's origin, so it cannot act as the person; a page with its own address is framed from there; its scripts are written in as
`data:` addresses, so `defer`, `async` and modules keep their timing), PDF (a browser without a PDF viewer, such as a
headless Chromium build, gets a note and a link to open the file instead of a blank frame), text — and its details to
keep with their state and where each was set.
Registered views are available even when there is only one mesh view or the original is an editable document.
The pending decision cards and the order gate also link to the original file. Downloads use the registered filename.
Select a numbered view when a
piece has several. In the pending list, select the image to enlarge it. The desktop console saves downloadable
pieces in Downloads and gives each copy a distinct name.
The person writes an optional note and presses **Keep** or **Cancel**; **Decide separately** splits a piece into a gate of
its own (`gate_split`).

* **Keep** fixes the piece as the reference for what comes next: `job` hands it to every job of the order.
* **Cancel** sends it back to its station: the note becomes a defect of the job that made it (marked as the person's,
  not a technical failure), the task moves to `changes` if it had been delivered, and what was made from it becomes stale.
* Pieces not yet decided keep moving as provisional.
* Only the person's own pages decide (`POST /api/agora/gate_decide` from the Hub page or Faustus Studio speaking for the
  person). There is no tool for it, and the operation refuses any agent.
* Every decision is stored with its situation: the order and its state, the deadline, the gate and the pieces that were
  on the table (`order_decisions`).

Before the expensive work, an order can go through a draft pass: its whole chain in draft, in variants, shown to the
person at an early gate on the same page. An agent can correct the pass note with
`hub_agora_draft_pass_note_set` (`agent`, `pass_id`, `note`), including while the gate is shown. Pass an empty
string to clear the note. The decision and variant signatures stay the same, and the result supplies an undo token
for `hub_agora_order_undo`. See `docs/DRAFT-PASS.md`.

## Undo

Every write an agent makes (`order_create`, `order_update`, `plan_set`, `piece_add`, `piece_check`, `piece_defect`,
`defect_close`, `gate_open`, `gate_split`) returns `undo`. `order_undo` with that token puts back the rows the call
changed; tasks it created are dropped and task states it moved are restored. Messages it posted stay. A token works once.

## HTTP and tools

Reads (`GET`, `space_id` for the space, `all_spaces=1` on `orders`): `/api/agora/orders`, `/api/agora/order?order_id=`,
`/api/agora/job?task_id=`, `/api/agora/order_decisions[?order_id=]`, and `/api/agora/piece_file/<piece>` (its file),
`?preview=<n>` (one preview), `?inline=1` (a local web page as one document, its own styles, scripts and images written in, for the sandboxed frame) or `/<relative path>` (a file beside it). Files are served only
when a piece names them or they sit in the piece's folder, and only to the family (the page or a bearer token).

Writes (`POST /api/agora/<operation>` with a bearer token, or from the page): the operations above.

| Tool | Does |
|---|---|
| `hub_agora_orders` | Orders of a space (or all), with counts, thumbnails and the gates waiting for the person |
| `hub_agora_pending_decisions` | Pending gates and early passes, with registered previews and original filenames per artifact and a count per space |
| `hub_agora_order` | One order: words, refs, plan with task states, pieces with checks and defects, gates, decisions |
| `hub_agora_order_create` / `hub_agora_order_update` | Write an order / change it |
| `hub_agora_plan_set` | Compose or recompose the plan |
| `hub_agora_job` | Everything a worker needs to start a job |
| `hub_agora_piece_add` | Register a piece |
| `hub_agora_piece_check` | Note a check, detail by detail |
| `hub_agora_piece_defect` / `hub_agora_defect_close` | Send a defect back / close it on the revision that fixed it |
| `hub_agora_gate_open` / `hub_agora_gate_split` | Show pieces to the person / decide one separately |
| `hub_agora_order_decisions` | The person's decisions with their situation |
| `hub_agora_order_undo` | Undo one write |

## The page

Navigation → **Orders** (its own place, before Projects). On top, what waits for the person's decision, one card per gate;
then the box to write an order (words, title, project, details that must not be lost, deliverables, folder); then the
orders, each opening into its words and references, the plan (job, station, delivers, waits for, for whom, state, and
«sent back» when a defect is open), the pieces with their state, provenance and defects, and the person's decisions. The
tab's badge counts the open gates. Texts in Spanish and English.

**Your pending decisions** in the Hub sidebar and on Home covers all spaces. The space sidebar keeps the entry visible next to Home and Inbox, shows a zero when the space has no pending decisions, and opens its list in the centre. The servers rail marks spaces with pending decisions. All views read `GET /api/agora/pending_decisions` for the same rows and per-space counts.

The Hub, Agora console and Studio use the same cards. Inspect the pieces and enlarge their images, enter an optional note, and choose **Keep** or **Cancel**. An early gate counts once and offers **Keep this one** and **Discard** on each undecided variant. Keeping a variant closes the early gate. Discarding one leaves the others available. Each choice is written to the space's production decisions and the queue refreshes. **Open order**, when available from the current browser, focuses the gate in Orders, where the full order, provenance and checks remain available.

The cards submit `POST /api/agora/production_decide`, which uses the same transactions as Orders. The Hub desktop window opens the write session on load. **Restore session** renews it in place and keeps the current note, so the person can try the action again. To enable writes in a regular browser profile, open the Hub from its launcher. Studio forwards choices from the signed-in person's API with its dedicated relay credential. The placement link provisions that credential on the remote host. Agent tools can read open gates and their results.

In the space's decision log and the Ágora console, production decisions show who decided, the date, order, outcome and note. `order_decisions` returns `decided_by` with the member's ID and current display name. The history shows **Your decision** to the member who decided. Receipts from before attribution use the installation's person, since only that person's pages could write them.
