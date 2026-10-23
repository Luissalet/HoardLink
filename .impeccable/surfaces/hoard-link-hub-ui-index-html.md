---
version: 1
slug: "hoard-link-hub-ui-index-html"
primary_target: "hoard_link/hub/ui/index.html"
related_targets: ["hoard_link/hub/ui/navigation.js","hoard_link/hub/ui/navigation.css"]
---

## Direction contract
THESIS: A project library with explicit destinations; shared infrastructure has its own pages.
OWN-WORLD: Graphite, sage selection, warm white text, familiar sans, thin dividers and generous list rows. Native product controls, authored outline icons.
STORY: Pick a project, enter its Agora, inspect its tasks. Apps and shared tools remain directly discoverable in six navigation groups.
FIRST VIEWPORT: 224px navigation rail, compact environment header, 32px page title, broad project list, narrower running-app list. Each project has its own Open Agora action.
FORM: Project library, fourth grounded direction (other candidates: operations console, file browser, inbox, launchpad, catalog, activity ledger), seed 3964c2d5. Density, star maps and horizontal catalogs declined for navigation complexity; retain their information hierarchy and consistent state language.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Implemented scope and documentation

The Hub shell uses the actual final `navigation.css` graphite/sage overrides and shared native sans stack. Desktop has the 224px rail and a broad project list beside a narrower running-app area; at 1180px the latter stacks, and at 760px navigation expands in normal document flow. Routes retain full document scrolling and project list/detail alternate. Existing inherited facet fields may still use warm `--hoard-*` theme values directly. Navigation uses authored SVG outlines; this change introduces no shipping raster imagery.

The directory lists persistent `/api/agora/spaces`, not every Faustus project. Native workspace links derive from the coordination API's `workspace_url` and preserve `space_id`. All original facets remain reachable through the six groups and contextual navigation, with unmatched facets under System. Loading, empty, failed-load/retry and no-match states are explicit.

`DESIGN.md` records the Hub system in `hub-` tokens and scoped subsections while preserving the separate `apps/agora` design. `PRODUCT.md` adds only a bounded Hub navigation section. The generated `.impeccable/design.json` companion preserves the existing Ágora metadata/narrative/samples and adds scoped Hub metadata and six actual component patterns.