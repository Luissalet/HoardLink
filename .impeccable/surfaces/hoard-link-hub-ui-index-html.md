---
version: 1
slug: "hoard-link-hub-ui-index-html"
primary_target: "hoard_link/hub/ui/index.html"
related_targets: ["hoard_link/hub/ui/navigation.js","hoard_link/hub/ui/navigation.css","hoard_link/hub/ui/interiors.css","hoard_link/hub/ui/mail.js","hoard_link/hub/ui/chats.js","hoard_link/hub/ui/refs.js","hoard_link/hub/ui/family.js"]
---

## Direction contract
THESIS: A project library with explicit destinations; shared infrastructure has its own pages.
OWN-WORLD: Graphite, sage selection, warm white text, familiar sans, thin dividers and generous list rows. Native product controls, authored outline icons.
STORY: Pick a project, enter its Agora, inspect its tasks. Apps and shared tools remain directly discoverable in six navigation groups.
FIRST VIEWPORT: 224px navigation rail, compact environment header, 32px page title, broad project list, narrower running-app list. Each project has its own Open Agora action.
FORM: Project library, fourth grounded direction (other candidates: operations console, file browser, inbox, launchpad, catalog, activity ledger), seed 3964c2d5. Density, star maps and horizontal catalogs declined for navigation complexity; retain their information hierarchy and consistent state language.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Implemented scope and documentation

The Hub shell uses the actual final `navigation.css` graphite/sage overrides and shared native sans stack. Desktop has the 224px rail and a broad project list beside a narrower running-app area; at 1180px the latter stacks, and at 760px navigation expands in normal document flow. Routes retain full document scrolling and project list/detail alternate. The later `interiors.css` override gives main-content/dialog fields graphite Hub surfaces; remaining facet-specific `--hoard-*` exceptions stay local. Navigation uses authored SVG outlines; this change introduces no shipping raster imagery.

The directory lists persistent `/api/agora/spaces`, not every Faustus project. Native workspace links derive from the coordination API's `workspace_url` and preserve `space_id`. All original facets remain reachable through the six groups and contextual navigation, with unmatched facets under System. Loading, empty, failed-load/retry and no-match states are explicit.

`DESIGN.md` records the Hub system in `hub-` tokens and scoped subsections while preserving the separate `apps/agora` design. `PRODUCT.md` adds only a bounded Hub navigation section. The generated `.impeccable/design.json` companion preserves the existing Ágora metadata/narrative/samples and adds scoped Hub metadata and six actual component patterns.
## Shared-tool interiors

The existing graphite/sage world and the separate `apps/agora` system remain unchanged. Interior controls use 14px type and 38px minimum height (44px for content buttons/selectors at 760px); row titles use 15px and mail body text 16px/1.8 with a 72ch reading measure. Main content now caps at 1640px and heading spacing is 28px. Low-height desktop viewports return the rail to normal document flow.

Mail is one selected Attention/Unclaimed/Recent queue, not three simultaneous columns. It pages 20 loaded messages; requests are capped at 40 attention, 100 unclaimed (seven days) and 40 recent. Search explicitly filters only the loaded subject/sender/excerpt fields. The full reader replaces the list; Back retains queue/category/search/page within its sphere. Message HTML is not interpreted: text DOM supplies paragraphs, simple headings, HTTP/HTTPS links and available attachment labels/sizes. Settings drafts survive polling but are hidden and absent from pending forms while reading. Sphere changes reset scope, and superseded list/detail responses cannot replace newer scope.

Work, notifications, chats, references and agent-session records reflow at narrow widths. Chat connections live in the Sources and connections disclosure. References use native buttons and wrapping URI text. Repo/audit tables become labeled records at 1400px and reduce from three columns to two at 760px. Documentation of these implemented patterns does not claim that empty-page captures validate populated layouts or actions; execution results and the independent finish verdict belong to their actual QA records.

Second-round populated QA uses isolated native fixtures (mail, chat/notification/reference records, jobs, purchases, a long-path/branch Git repository, audit and an agent session with writes/details). It includes disabled Chat source controls and open notification settings. Captures at 390/1280/1920px and round2-metrics.json record these bounded views without horizontal overflow or nested vertical reading panes; this is not coverage of every real app or state. Disabled source rows keep full opacity and explicit state, session summaries/reasons use 14px, and the mobile repository header stacks 44px actions. The independent final disposition is recorded separately.
