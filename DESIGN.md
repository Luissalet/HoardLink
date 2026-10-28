---
name: HoardLink Hub and Ágora
description: Scoped design systems for the Hub navigation shell and the separate Ágora team console.
colors:
  hub-accent: "#e2b24c"
  hub-accent-hover: "#e8c16f"
  hub-canvas: "#1c1a14"
  hub-sunken: "#161410"
  hub-surface: "#24221b"
  hub-raised: "#2f2c25"
  hub-elevated: "#38342b"
  hub-text: "#ece9e1"
  hub-muted: "#bebbb4"
  hub-divider: "#433e35"
  hub-border-hover: "#6a6458"
  hub-scrollbar: "#6a6458"
  hub-selection: "#e2b24c"
  hub-selection-text: "#1b1813"
  hub-focus: "#e2b24c"
  hub-nav-text: "#bebbb4"
  hub-nav-hover-text: "#e2b24c"
  hub-nav-hover: "#2f2c25"
  hub-selected: "rgba(226, 178, 76, 0.14)"
  hub-folder: "#bebbb4"
  hub-project-hover: "rgba(226, 178, 76, 0.14)"
  hub-profile: "#2f2c25"
  hub-project-agora: "rgba(226, 178, 76, 0.14)"
  hub-ink: "#1b1813"
  hub-success-bg: "#20372a"
  hub-success-line: "#528565"
  hub-info-line: "#607fa5"
  hub-warning-bg: "#3a301c"
  hub-warning-line: "#94773b"
  hub-danger-bg: "#3a2521"
  hub-danger-line: "#a16b60"
  hub-error: "#e07a6e"
  amber: "#e2b24c"
  canvas: "#1c1a14"
  surface: "#201e17"
  raised: "#24221b"
  text: "#ece9e1"
  muted: "#bebbb4"
  divider: "#433e35"
  positive: "#83b49a"
  warning: "#d3927b"
  claude-mark: "#dfa991"
  codex-mark: "#b5d9c3"
  cursor-mark: "#c6ccd2"
  faustus-mark: "#e4c889"
typography:
  hub-brand:
    fontFamily: "'Cinzel', 'Iowan Old Style', Charter, Georgia, 'Liberation Serif', 'Noto Serif', 'Times New Roman', serif"
    fontSize: "19px"
  hub-heading:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "34px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.03em"
  hub-section:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "21px"
    fontWeight: 650
  hub-body:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.55
  hub-label:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "12px"
    fontWeight: 400
  wordmark:
    fontFamily: "Cinzel, Georgia, serif"
    fontSize: "23px"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "0.01em"
  heading:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.15
  title:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.4
rounded:
  hub-link: "8px"
  hub-nav: "8px"
  hub-card: "12px"
  sm: "6px"
  md: "8px"
  lg: "10px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  2xl: "24px"
  3xl: "32px"
components:
  hub-primary-link:
    backgroundColor: "{colors.hub-accent}"
    textColor: "{colors.hub-ink}"
    rounded: "{rounded.hub-link}"
    padding: "10px 15px"
  hub-nav-link:
    textColor: "{colors.hub-nav-text}"
    rounded: "{rounded.hub-nav}"
    padding: "12px 14px"
  hub-nav-link-selected:
    backgroundColor: "{colors.hub-selected}"
    textColor: "{colors.hub-accent-hover}"
    rounded: "{rounded.hub-nav}"
    padding: "12px 14px"
  hub-project-row:
    textColor: "{colors.hub-text}"
    padding: "20px 0"
  hub-app-card:
    backgroundColor: "{colors.hub-surface}"
    textColor: "{colors.hub-text}"
    rounded: "{rounded.hub-card}"
    padding: "24px"
  button-primary:
    backgroundColor: "{colors.amber}"
    textColor: "{colors.canvas}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 13px"
  composer:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "12px 14px"
  model-selector:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "7px 9px"
  task-row:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    padding: "17px 20px"
---

# Design System: HoardLink Hub and Ágora

## Overview

This file describes two separate implemented surfaces. The **Hub shell** is `hoard_link/hub/ui/index.html`, with `navigation.js`, `discovery.js`, `navigation.css`, `interiors.css` and final `workspace.css` overrides; its tokens use the `hub-` prefix. The **Ágora console** is the separate `apps/agora` surface; its existing unprefixed tokens and guidance below remain valid only there. Both use family charcoal, gold and serif branding, but their layouts, token scopes and component contracts remain distinct. Do not treat the Hub rail as a replacement for Ágora's leader roster.

### Hub shell

**Creative North Star: “The Personal Workspace”**

The Hub begins with attention and continuity: pending work, a project to continue and tools described by purpose. The official Hoard dark theme supplies warm charcoal, ivory text and gold actions, current context and keyboard focus. Luis explicitly rejected the light 1301 identity; task 1363 restores the family identity while retaining those workflows. The serif brand and original Hub icon anchor the rail.

**Key Characteristics:**
- Official warm charcoal family surfaces, ivory text and gold actions.
- Six primary destinations, visible contextual links and a Help/settings route.
- Attention and project continuity before application launch entries.
- Original Hub icon reused byte-for-byte for header/favicon; existing app icons and navigation SVG remain.

### Separate Ágora console (apps/agora)

**Creative North Star: “The Team Room”**

Ágora extends the Hoard family's warm charcoal workspace into a full team console. A restrained amber wordmark anchors the frame, while quiet dividers make the three working areas legible: a permanent leader roster, a central group conversation and an operations pane. Source Sans 3 carries the information; Cinzel is reserved for the Ágora wordmark.

The surface favors named states, authorship and source details over decoration. The desktop keeps four leaders visible beside the conversation. At narrow widths, the leaders become a horizontal roster and operations open as a drawer; the conversation and composer remain the main work area. The visual system is a description of the shipped UI, not a claim that every provider workflow is complete.

**Key Characteristics:**
- Warm charcoal surfaces with a focused amber accent.
- Compact, divided work areas with a readable conversation measure.
- Status, authorship and quota provenance are written out alongside color.
- Provider state uses distinct restrained avatar colors; the wordmark remains the only display face.

## Colors

### Hub shell

`html[data-hoard-app="hub"]` selects the existing official tokens in `hoard_link/ui/hoard-theme.css`. `style.css` maps the Hub variables to them. `workspace.css`, loaded after `navigation.css` and `interiors.css`, owns final layout and component treatments without a light-palette remap. The shared theme itself was not edited.

Gold is the primary action, current context, focus and text-selection signal, with dark accent ink on filled buttons. Canvas, sunken, surface, raised and hover roles form the warm charcoal hierarchy; ivory/muted roles preserve reading hierarchy. Selected navigation and project entries use the family's translucent accent-soft. Green, amber, blue and clay/red state treatments accompany written states. Frontmatter records the reused family values and the scoped semantic state colors from workspace.css/style.css. Sidecar ramps are preview metadata, never contrast certification.

**The Family Source Rule.** Keep Hub colors bound to the official `data-hoard-app="hub"` family tokens. The shared palette does not merge Hub navigation with the separate apps/agora console.

### Separate Ágora console (apps/agora)

A warm near-black foundation holds soft ivory text; amber marks the primary action and focus, while muted neutrals carry secondary information.

### Primary
- **Hub Amber** ({colors.amber}): Wordmark, primary action, focus outline, selected tab and active quota emphasis.

### Secondary
- **Leader Marks** ({colors.claude-mark}, {colors.codex-mark}, {colors.cursor-mark}, {colors.faustus-mark}): Muted identity colors for the four leader avatars. Keep their use local to identity markers.

### Neutral
- **Warm Canvas** ({colors.canvas}): Main conversation and page background.
- **Roster Charcoal** ({colors.surface}): Header, roster and operations pane.
- **Raised Charcoal** ({colors.raised}): Selectors, composer and dialog surfaces.
- **Ivory Text** ({colors.text}): Primary text and controls.
- **Soft Ash** ({colors.muted}): Secondary labels and descriptive copy.
- **Divider Umber** ({colors.divider}): Fine separators and meter tracks.

### Status
- **Quiet Green** ({colors.positive}): Positive/available state dots.
- **Muted Clay** ({colors.warning}): Unavailable or failed state dots and related status treatment.

**The Amber Signal Rule.** Reserve amber for the brand mark, primary action, focus and selected navigation; do not turn every leader or status into an amber accent.

## Typography

### Hub shell

The Hub retains the shared Source Sans 3/system sans stack and adds no font download. The Hoard Hub brand uses the family Cinzel/serif stack at 19px. Font fallback remains supported; this does not certify availability of one concrete face on every machine. The final body is 16px/1.55. Page headings use the frontmatter Hub heading role, becoming 30px at 600px; section headings are 21px, or 20px on mobile. Project names and rule names are 17px; launch and attention titles are 16px. Descriptions generally use 14–15px, metadata 12–13px and form section headings 19px.

Rail links use 15px; primary and local navigation links use 14px. Inherited content controls remain 15px with a 38px minimum, becoming 44px at 760px. Mail reading remains 16px/1.8 with a 72ch measure. Notice detail uses 15px/1.75 and a 75ch measure; source/advanced text uses 13px. Local component sizes remain intentional, not an instruction to shrink the whole workspace.

### Separate Ágora console (apps/agora)

**Display Font:** Cinzel (with Georgia, serif fallback; wordmark only)
**Body Font:** Source Sans 3 (with sans-serif fallback)
**Label/Mono Font:** Source Sans 3; tabular numerals are used for quota and task identifiers.

**Character:** Cinzel gives the small brand mark a distinct formal note. Source Sans 3 stays clear across dense status rows, task details and longer messages.

### Hierarchy
- **Wordmark** (600, 23px, line-height 1): Ágora identity in the top bar.
- **Heading** (600, 24px, line-height 1.15): Main conversation heading; the mobile heading steps down to 20px.
- **Title** (600, 16px, line-height 1.4): Leader names and message emphasis.
- **Body** (400, 15px base; conversation messages 14px desktop and 13px mobile, line-height 1.6–1.65): Conversation text, with a measure capped at 75ch.
- **Label** (400–600, 10–13px, line-height 1.4): Quotas, timestamps, state descriptions, task metadata and controls.

**The One Display Face Rule.** Cinzel appears in the wordmark; use Source Sans 3 for headings, messages, labels and controls.

## Layout

### Hub shell

Desktop uses a 224px family-surface rail and a flexible workspace. The transparent header has an 84px minimum with 20px 40px padding. Content caps at 1480px with 18px 40px 64px padding; the page-heading following gap is 32px. Above 900px the final workspace override keeps the rail sticky, replacing the earlier interior low-height exception.

Dense layer (compact.css, 28-10-2026, loaded last): 200px rail, 52px sticky header holding the status bar, content up to 1720px with 18px 24px padding, 24px page titles, 32px controls. Home: three status tiles (Faustus, Hoards, GPU) in a 3-column grid (2 at 1300px, 1 at 900px), attention and projects 1:1 with a 16px gap (stacking at 1100px), then the launcher of every Hoard in auto-fill 190px tiles of 44px height. The application catalog uses auto-fill 280px compact tiles with an 8px gap; the status table scrolls horizontally inside its own border and drops PID/windows at 900px and uptime/RAM at 600px. The project directory is one list of divided rows. Project overview, automation landing and Help cap their reading surface at 950px.

At 900px the rail becomes a document-flow Menu with 18px 24px padding; the header is at least 64px, and content uses 20px 24px 48px. At 600px content becomes 20px 18px 40px; application launches/catalog, notices and inherited resource/form structures become single-column. Contextual links wrap in normal flow. Pages use document scrolling; boards and task/conversation details alternate, while editing dialogs own scrolling. The automation dialog caps at 780px with width calc(100% - 32px).

Inherited interior patterns remain: mail has a 170px queue rail, stacked at 760px; records reflow at 760px, repo/audit records at 1400px and board columns at 600px. These are retained component behaviors, not a second global layout authority.

### Separate Ágora console (apps/agora)

The desktop composition is a 64px top bar over three columns: 266px leader roster, flexible conversation with a 360px minimum, and 320px operations pane. At widths above 1500px the side columns grow to 284px and 356px; below 1080px they contract to 236px and 274px. The conversation remains the flexible center, with message text capped at 75ch.

Below 900px, the operations pane becomes a right-side drawer up to 320px wide. At 640px and below, the top bar is 56px, the roster becomes a horizontal leader strip with 192px cards, and the conversation fills the remaining vertical space. The composer stays at the foot of the conversation. The spacing rhythm is compact and built from repeated 4, 8, 12, 16, 20, 24 and 32px steps; use exact values only where the implementation establishes them.

## Elevation & Depth

### Hub shell

Family charcoal surfaces use warm borders on the deep canvas. Project rows, application launch rows, notifications and rules use dividers rather than separate elevated cards. Catalog/overview/attention containers retain bordered surfaces without decorative shadow. Dialogs use the family shadow (0 8px 28px rgba(0,0,0,0.38)) and a black 70% backdrop. Fields and semantic error/success/warning states follow the dark surface hierarchy.

### Separate Ágora console (apps/agora)

The system is mostly flat. Tonal changes between canvas, roster and raised controls establish the primary hierarchy; 1px warm dividers organise panels and rows. A soft leftward shadow separates the operations drawer on small screens, and the modal backdrop darkens the underlying workspace. No shadow vocabulary is used for ordinary cards or buttons.

## Shapes

### Hub shell

The main containers retain gently curved 12px corners. Rail, primary links, contextual project emphasis and shared controls use 8px where the final override defines them; inherited shared fields retain 6px and dialogs 14px. Project/notice/rule rows are square and divided. Authored navigation SVG retains rounded strokes. Project icons are quiet 24px folder marks; launch icons are 42px, reducing to 40px in the mobile row. Catalog application icons retain their existing 56px scale.

### Separate Ágora console (apps/agora)

Controls and containers use gently rounded corners: 6px for buttons, selectors and small fields; 8px for identity marks; 10px for the composer and dialogs. Fine borders define inputs and the composer, while list rows rely on separators rather than individual outlined cards. The roster leader marks are compact rounded squares; participant marks in the conversation are circular.

## Components

### Hub shell: navigation and controls

- **Navigation:** Inicio, Proyectos, Aplicaciones, Bandeja, Automatización and Sistema remain primary destinations, with Buscar and Ayuda y ajustes. Contextual links are visible, wrap and show the current page with a gold rule. Projects have Resumen and Ágora links. Inbox opens Avisos; System opens Estado y conexiones; Automation opens an intention landing.
- **Status bar:** Faustus pill (state dot, port, Open/Start/Stop), Hoards pill (running/total, warning count) and GPU pill (one 6px meter per GPU); the pills link to System. Drawn by status.js from the existing snapshot and lease polls.
- **Faustus states:** running (green), busy/not answering (amber, port held, Open and Stop only), starting/stopping (amber), off (red border, Start).
- **Project cards:** auto-fill 340px grid; icon, title, one-line description, count chips (waiting in amber first) and relative last activity; the card itself opens the project.
- **Launcher tile and app menu:** tile = main button (28px icon with a state dot on its corner, ellipsised name; click opens in a window) plus a tool strip: open-in-tab arrow (shown on hover/focus, always on touch), one power icon (filled play green on hover / filled square muted red) and ⋯. The menu is a fixed popover (290-360px, card-2, 10px radius, deep shadow) with an icon+name+mono state header, 32px items with an icon, label and gesture hint, separators between open / lifecycle / inspect groups; arrow keys move, Escape, outside click, scroll or navigation close it. Right click, middle click (tab) and the ContextMenu key work on tiles, Applications cards and table rows.
- **Status table:** state dot and label, icon, name, one-line description, port, PID, uptime, RAM, windows and row actions; state filter segment, search, Start all/Stop all. Rows are keyed and updated in place every poll.
- **Home:** status tiles first; attention uses a bordered family surface, project continuity divided rows and every Hoard a compact launcher tile (running first, unavailable dimmed). The first five attention entries are bounded summaries, not a promise of complete global prioritisation.
- **Quick reply:** Te esperan presents the complete question, Reply here, inline answer and full-thread entry. Send posts through the native API; Reply and close resolves only non-task threads. Task replies reopen their escalation without completing the task. Keep duplicate-send disabling, visible errors and retained drafts through refresh/language/project changes; Cancel preserves text. The textarea is at least 110px with 14px padding, 8px corners, sunken background and inherited text; question measure is 75ch at 15px/1.65.
- **Project:** the overview makes pending tasks and conversations from the selected board actionable. Full-conversation links use configured workspace_url and preserve space_id. Recent visit ordering is local to this browser.
- **Applications:** compact tiles (40px icon, ellipsised name and description, state pill, uptime/memory line, Open highlighted only when running) with Manage as a floating menu that closes on outside click or Escape; Tiles/Table toggle remembered per browser. Purpose categories and brief descriptions aid known catalog entries; unknown manifests keep their actual fallback. One opening action, Manage and startup controls remain. Unavailable startup gets a plain explanation with the original reason retained in Details.
- **Notifications and search:** notice summaries are plain excerpts with the full original body in a native disclosure. Content search retains retry and separates technical source details; local destinations use divided text rows.
- **Automation:** intention links precede normal forms. Five known events and common backup/application/group actions use human labels. Custom tools/events retain technical fields in advanced disclosures; full JSON remains a mutually exclusive mode. Within the open editor, each action type retains its own draft on type switching. Lifecycle events use Affected application as a Hub-event data.app filter. Preserve unknown fields when kind is unchanged and invalid JSON text on error.
- **States and accessibility:** loading, empty, no-match and recovery are written. The final visible focus outline uses hub-focus with a 3px offset; reduced-motion behavior is inherited. No detector result is a contrast certification.

### Separate Ágora console (apps/agora)

### Buttons
- **Shape:** Compact rounded corners (6px).
- **Primary:** Amber fill, dark text, 8px by 13px padding; used for the send action and modal confirmation.
- **Hover / Focus:** Hover lightens the amber fill. Keyboard focus uses a visible 2px amber outline with a 3px offset; disabled controls reduce opacity.
- **Ghost:** Transparent icon controls use muted text and a charcoal hover surface.

### Chips
- **Style:** Provider initials sit in small bordered avatar tiles with a muted identity tint; message participants use circular mini-avatars.
- **State:** Online, working and unavailable states also have text labels. Their dots use green or clay to reinforce the written state.

### Cards / Containers
- **Corner Style:** The main workspace is square-edged; the composer and dialogs use 10px corners.
- **Background:** Conversation uses the canvas; roster and operations use the surface; controls and dialogs use the raised surface.
- **Shadow Strategy:** Flat at rest; reserve a soft shadow for the mobile operations drawer.
- **Border:** 1px warm dividers separate columns, tabs, messages and task rows.
- **Internal Padding:** Leader rows use compact 10–20px insets; conversation messages use 17–22px vertical spacing; task rows use 17px by 20px.

### Inputs / Fields
- **Style:** Raised charcoal fill, warm border where visible, 6px corners on selectors and 10px on the outer composer. The message textarea blends into the composer.
- **Focus:** 2px amber `:focus-visible` outline, offset 3px; inside the composer the textarea outline moves inward.
- **Error / Disabled:** Disabled controls dim; errors use a clay-toned surface and readable text.

### Navigation
- The top bar pairs the Cinzel wordmark with workspace context, Hub connection and compact utility controls. The task pane uses text tabs with an amber bottom rule for the active tab.
- At mobile widths the workspace label and direct Hub link recede, while connection status and the roster remain available.

### Signature Component: Leader Roster
- Each leader block combines a name, text state, model selector and quota rows. Child helpers branch beneath the owning leader; their detail and result expand in place or through the mobile results affordance.
- Keep the four leaders discoverable. On narrow screens, horizontal scrolling is signposted and the quota label, value, freshness and reset details remain readable.

### Signature Component: Conversation
- Messages are divided by subtle rules, with avatar, author, timestamp and attribution kept together. Preserve the long-form reading measure and stable author order.
- The recipient selector and textarea form one bordered composer that remains available at the bottom of the conversation.

### Signature Component: Operations Pane
- Pending/completed tabs, search and task rows form a compact inspection column. At narrow widths the same work opens in a drawer; task details use a larger dialog with its thread and reply field.

## Do's and Don'ts

### Hub shell

- **Do** begin with pending work, a project to continue and tools described by purpose.
- **Do** preserve document scrolling, visible contextual links, wrapping and keyboard access.
- **Do** treat projects as persistent Ágora spaces and preserve coordination workspace_url and space_id.
- **Do** preserve unknown fields and invalid JSON while editing automation.
- **Don't** infer running apps, complete catalogs or successful work from placeholders.
- **Do** reuse the original Hub icon for header/favicon without changing its bytes; reserve the family serif stack for the brand.
- **Don't** restore the rejected light remap or replace the original identity asset.
- **Don't** use the separate Ágora console layout as the Hub navigation contract.
- **Don't** describe this correction as new API/MCP capabilities or migrated data.

### Separate Ágora console (apps/agora)

### Do:
- **Do** use the warm charcoal, ivory and amber palette for the app frame and primary actions.
- **Do** keep names and status words visible next to state color; unknown quota remains explicitly unknown rather than becoming a numeric zero.
- **Do** preserve author, leader/helper identity and quota freshness in dense or narrow layouts.
- **Do** keep the composer visually distinct and reachable while reading the conversation.
- **Do** use visible keyboard focus and honor reduced-motion preferences.

### Don't:
- **Don't** add marketing slogans, hero metrics or decorative gradients to this operational surface.
- **Don't** present mock activity or an unavailable provider as a live response.
- **Don't** rely on color alone for connectivity, availability, quota or task state.
- **Don't** spread the provider avatar tints across unrelated controls or panels.

