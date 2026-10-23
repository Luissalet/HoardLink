---
version: 1
slug: "hoard-link-hub-ui-index-html"
primary_target: "hoard_link/hub/ui/index.html"
related_targets: ["hoard_link/hub/ui/navigation.js","hoard_link/hub/ui/discovery.js","hoard_link/hub/ui/navigation.css","hoard_link/hub/ui/interiors.css","hoard_link/hub/ui/workspace.css","hoard_link/hub/ui/automation_form.js","hoard_link/hub/ui/notify.js","hoard_link/hub/ui/search.js","hoard_link/hub/ui/mail.js","hoard_link/hub/ui/chats.js","hoard_link/hub/ui/refs.js","hoard_link/hub/ui/family.js"]
---

## Direction contract
MODE: Operate. Success is finding work and completing an action without knowing internal component names.
THESIS: Attention and continuity first; enter one project and its Agora, discover tools by purpose, and automate common actions through their intent.
OWN-WORLD: White navigation, a green-neutral canvas, dark green reading ink, restrained green actions, divided lists and a readable document. Actual application identities and authored SVG remain. No new product raster or approved visual comp is claimed: this is a code-led replacement of the previous world.
STORY: See pending work on Home, continue a visited project, open its pending task or conversation, discover an application by purpose, and choose a routine by the action wanted.
FIRST VIEWPORT: A 224px white rail, global search and 34px heading. Home pairs attention with project continuity; application launch entries follow. Project overview precedes its Agora; contextual links stay visible and wrap. The narrow layout uses a document-flow Menu and a 30px heading.
FORM: Seven grounded directions considered: control room, launch library, workbench, catalog, inbox, file browser and service map. Seed 9087cc82 assigned direction 4, catalog. The implemented catalog of work is translated into attention/project/purpose by the fixed user brief; this contract does not add another approval gate.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Implemented scope

The final Hub authority is workspace.css, loaded after navigation.css and interiors.css. It defines white/green-neutral surfaces,224px rail, 1480px content cap, 16px/1.55 body, 34px desktop heading and 21px sections. At600px headings become 30px and sections 20px. Home stacks at 1150px; Menu replaces the rail at 900px and application/notice layouts become single-column at 600px. Existing interior record and control breakpoints remain. Divided rows and native disclosures reduce the amount of long text before an action.

Home combines /api/today with unread notices, shows at most five attention entries, up to three project spaces ordered by browser-local visits, and up to four application launch entries. It does not infer server activity or recent files from those visits. A project opens an overview of pending tasks and conversations from its selected board, with direct entry into its own Agora and native full conversation. The directory is /api/agora/spaces; it is not the complete native Faustus catalog. Native links use coordination workspace_url and preserve space_id.

Applications uses purpose categories, known-entry Spanish/English descriptions and Spanish synonyms; unknown manifests retain their real fallback. Common automation actions use labelled backup/application/group controls and five known events; custom tools/events remain technical advanced paths. Full JSON is an exclusive alternate editor. The landing offers scheduling, event response, run history and purchases. System puts connections first and explains stopped/unconfirmed states. Help/settings points to existing options. Notice bodies remain intact behind an excerpt disclosure; content search retains recoverable failure and optional source details.

The backend, APIs, MCP, identities, configuration, drafts and data are retained. Separate apps/agora context, tokens, component samples and copy remain scoped and unchanged. Existing application icons and navigation SVG ship; no generated raster was introduced. docs/hub.png remains historical and is not current visual proof.

## Verification and review status

Source documented initially: dca9cbe1d832432a9679d49e356b5a2ab7ccedc5 (task 1301). Independent source/finish review and its fixes are pending reconciliation; the historical critique 1296 remains preserved and is not marked resolved here.

Author/root evidence: broad run214 PASS/4 FAIL in 633.49 s; the four legacy browser-flow failures were followed by a focused 4 PASS in 33.54 s. New workflow/label cases:4 PASS in 39.86 s. Four Node suites passed. Native MCP stdio performed eight reads across three isolated project scopes. These are distinct runs, not a green full-repository suite. No real app/model was started and no real automation was saved for these checks.

The single detector run over markup/JS/CSS was DEGRADED regex, with 43 advisory findings (18 colors/25 sizes) against the preceding DESIGN contract. This documentation records the intentional final palette and type scales; it does not rerun the detector or certify contrast. Fourteen desktop/mobile captures are in C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-user-workspace1301. Captures include desktop 1270x893 or 1280x900 and mobile 380x822; DOM overflow checks separately use 390px. Do not label every image 390x844. One mobile application-intent capture is invalidated by viewport change and awaits replacement. The logs legacy-flows-fixed.txt and workflow-labels-final.txt retain the focused results.

## Historical evidence

Task 1269 documented the previous navy/cobalt workbench: 128 area pytest cases, three Node suites, 15 browser/space cases and native MCP checks of five mail tools/four boards. Its 21-route desktop/mobile captures and ship verdict resolved that delivery's board and exclusive-editor findings. Those results do not validate the replacement1301 world. Its evidence remains external at C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-whole1269, source e3032f7e32a50b465e333cb0e4882e6a2ac03c90.
