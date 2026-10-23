---
version: 1
slug: "hoard-link-hub-ui-index-html"
primary_target: "hoard_link/hub/ui/index.html"
related_targets: ["hoard_link/hub/ui/navigation.js","hoard_link/hub/ui/navigation.css","hoard_link/hub/ui/interiors.css","hoard_link/hub/ui/mail.js","hoard_link/hub/ui/chats.js","hoard_link/hub/ui/refs.js","hoard_link/hub/ui/family.js"]
---

## Direction contract
THESIS: A personal workbench that makes opening an app, entering a project and managing shared work distinct tasks.
OWN-WORLD: Deep navy navigation, cool white work surfaces, cobalt actions, dark ink, large application identities, quiet status labels and 12px corners. Restrained colour for daytime desktop work; 15px controls and 16px reading.
STORY: Resume an app from Home; enter project-specific Agora from Projects; open Inbox, Automation or System directly at its working view and switch tools locally.
FIRST VIEWPORT: Stable 216px rail, global search, 36px page title. Home pairs a broad app launch area with a project stack; context pages have one selected tool and a visible labelled selector. App cards show one opening action and an explicit management disclosure.
FORM: Workbench, grounded candidate3 of7 (control room, launch library, workbench, catalog, inbox, file browser, service map); seed2dc9dabe. Signature interaction: context changes replace the working surface, retaining its form state; no navigation strip scrolling.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Implemented scope and documentation

The final Hub workbench uses navy navigation, cool white surfaces and cobalt actions. The observed CSS has a 216px rail,1576px maximum content width,36px desktop title and 12px principal containers. Body type is 15px; global buttons and rail links 14px; later interior control overrides 15px. Mail reading remains 16px/1.8. The chosen direction contract above is retained exactly; these measurements describe the actual cascade.

Home pairs app launch rows with up to three project cards. Launch descriptions are clamped to three lines; their full text stays in the DOM and title attribute, with full application JSON available through Applications → Manage → Details. The six primary destinations are Inicio, Proyectos, Aplicaciones, Bandeja, Automatización and Sistema. Bandeja opens Avisos, Automatización opens Trabajos and Sistema opens Recursos; labelled native selectors switch tools. Applications has one opening action plus Manage, catalog management and native Startup groups disclosures. Resources groups connections/engines, GPU leases and browser lanes. Global Search combines local app/tool destinations with existing content search.

Project spaces come from /api/agora/spaces; native Agora URLs come from coordination workspace_url and preserve space_id. All original tools, native APIs/MCP, data and scoped forms remain. No new backend or data capability is claimed. The separate apps/agora visual system is unchanged.

Home stacks at 1180px; the rail becomes a normal-flow menu at 900px; project cards, resources and automation fields become one column at 600px. Full document scrolling remains the reading model. Embedded board columns/cards constrain long content; dialogs own scrolling. Rules/jobs have native visual forms and exclusive advanced full JSON. Invalid JSON stays open with its text/error; unknown fields round-trip when their document/action kind is unchanged.

DESIGN.md and its sidecar record the actual scoped Hub system while preserving separate Agora tokens, metadata, narrative and component samples. Authored navigation SVG and real app icons remain; no generated raster ships. The old docs/hub.png is not current proof.

## Bounded verification

Task 1269 records 128 area pytest cases and three Node client suites passing,15 browser/space cases passing, and native MCP checks of five mail tools and four scoped boards. These are area checks, not the entire repository suite. Desktop 1440/mobile 390 captures cover 21 routes; Home also has a 1280 capture. Final fix 1 project and rules/jobs visual/advanced captures verify the two material finish findings:complete board content within the page and exclusive automation-editing representations.

Evidence is external in C:/Users/luism/Documents/Codex/2026-10-23/es/outputs/hub-whole1269 (r2-*.png, fix1-*.png, metrics and area-final-tests.txt). Source freeze is e3032f7e32a50b465e333cb0e4882e6a2ac03c90. The verdict pass scores both listed material fixes resolved and returns ship; it does not certify every real app, state, provider or viewport.
