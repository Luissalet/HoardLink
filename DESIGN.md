---
name: HoardLink Hub and Ágora
description: Scoped design systems for the Hub navigation shell and the separate Ágora team console.
colors:
  hub-cobalt: "#315ed6"
  hub-cobalt-hover: "#244ab4"
  hub-canvas: "#f2f5fa"
  hub-surface: "#ffffff"
  hub-raised: "#f5f7fb"
  hub-text: "#172842"
  hub-muted: "#586a83"
  hub-divider: "#dce3ee"
  hub-ink: "#ffffff"
  hub-selected: "#315ed6"
  hub-selected-text: "#ffffff"
  hub-nav-hover: "#203552"
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
  hub-heading:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "36px"
    fontWeight: 650
    lineHeight: 1.18
    letterSpacing: "-0.035em"
  hub-title:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "19px"
    fontWeight: 650
  hub-body:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "15px"
    fontWeight: 400
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
  hub-nav: "9px"
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
    backgroundColor: "{colors.hub-cobalt}"
    textColor: "{colors.hub-ink}"
    rounded: "{rounded.hub-link}"
    padding: "11px 14px"
  hub-nav-link:
    textColor: "#b9cbe3"
    rounded: "{rounded.hub-nav}"
    padding: "13px 14px"
  hub-nav-link-selected:
    backgroundColor: "{colors.hub-selected}"
    textColor: "{colors.hub-selected-text}"
    rounded: "{rounded.hub-nav}"
    padding: "13px 14px"
  hub-project-card:
    textColor: "{colors.hub-text}"
    padding: "28px"
  hub-app-card:
    backgroundColor: "{colors.hub-surface}"
    textColor: "{colors.hub-text}"
    rounded: "{rounded.hub-card}"
    padding: "26px"
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

This file describes two separate implemented surfaces. The **Hub shell** is `hoard_link/hub/ui/index.html`, with `navigation.js`, `navigation.css` and the shared-tool overrides in `interiors.css`; its tokens use the `hub-` prefix. The **Ágora console** is the separate `apps/agora` surface; its existing unprefixed tokens and guidance below remain valid only there. Do not apply the Ágora amber/Cinzel shell to Hub navigation or treat the Hub rail as a replacement for Ágora's leader roster.

### Hub shell

**Creative North Star: “The Personal Workbench”**

The Hub separates opening an application, entering a project and managing shared work. A deep navy rail sits beside cool white working surfaces; cobalt marks selection and primary action. Existing application identities, dark ink and quiet status labels keep the local environment readable.

**Key Characteristics:**
- Navy navigation, cool white surfaces and cobalt actions.
- Six primary destinations with a labelled local tool selector.
- Home pairs application launch rows with a project stack.
- Authored navigation SVG and existing app icons; no new shipping raster imagery.

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

The final navigation.css overrides define the Hub light palette and remap the existing shared-theme aliases. Cobalt marks actions and selected navigation; the navy rail uses pale blue text, white selected text and a quieter navy hover fill. Cool canvas, white surfaces, dark ink, muted text and fine dividers organise the workspace. The existing gold SVG Hub identity and actual application icons remain.

The later interiors.css rules apply these surfaces to retained facets and dialogs. Green, amber and red accompany written states.

**The Hub Scope Rule.** Use the Hub workbench palette in hoard_link/hub/ui; keep the separate apps/agora amber console and its tokens scoped to that application.

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

The Hub retains the shared Source Sans 3/system sans stack and adds no font download. Body type is 15px. The page heading is 36px/1.18 at weight 650, falling to 32px at 900px and 30px at 600px. Section headings use 22px, project/catalog names 19px, Home launch names 17px and Home project names 16px. Supporting text is generally 13–14px, metadata 11–13px.

Global buttons and rail links use 14px. The later interior override sets main-content controls and dialog buttons/inputs/selectors to 15px; individual components retain their local label sizes. Form labels use 14px and JSON fields 13px. Mail reading remains 16px/1.8 with a 72ch measure. The Hub label is 19px at weight 650.

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

Desktop uses a 216px navy rail and flexible workspace. The white header remains in document flow with an 82px minimum. Content caps at 1576px with 38px top/side and 56px bottom padding; headings have a 28px following gap. The rail is sticky except where the later interior low-height override returns it to flow at heights of 850px or less above the menu layout.

Home pairs application launches with a project stack in a 1.6:1 grid, gap 32px. It shows up to five running apps or the first five catalog entries, and up to four other apps when running entries exist. The companion stack has up to three title-sorted project spaces. Projects has a two-column card directory and a 520px maximum filter. Home stacks at 1180px.

At 900px, Menu expands a two-column navigation in normal document flow; header utilities wrap and content padding becomes 30px 24px 44px. At 600px, content padding is 28px 18px 40px; project cards, resources and automation fields become single-column. Content controls have a 38px minimum height, rising to 44px at 760px. The menu is 44px.

Pages use full document scrolling. Embedded boards and task/conversation details alternate; board columns/cards constrain wrapping and become one column at 600px. Mail uses a 170px queue rail beside filters/list, stacked above the list at 760px. Other retained records reflow at 760px; repo/audit tables become labelled records at 1400px, with three columns reducing to two at 760px. Dialogs own their scrolling; the automation editor is at most 860px/94vw wide and 90dvh high with a sticky header.

### Separate Ágora console (apps/agora)

The desktop composition is a 64px top bar over three columns: 266px leader roster, flexible conversation with a 360px minimum, and 320px operations pane. At widths above 1500px the side columns grow to 284px and 356px; below 1080px they contract to 236px and 274px. The conversation remains the flexible center, with message text capped at 75ch.

Below 900px, the operations pane becomes a right-side drawer up to 320px wide. At 640px and below, the top bar is 56px, the roster becomes a horizontal leader strip with 192px cards, and the conversation fills the remaining vertical space. The composer stays at the foot of the conversation. The spacing rhythm is compact and built from repeated 4, 8, 12, 16, 20, 24 and 32px steps; use exact values only where the implementation establishes them.

## Elevation & Depth

### Hub shell

White working surfaces use pale borders on a cool canvas. Application cards and icons have no decorative shadow; retained record lists use dividers. The rail has no shadow. Dialogs use a soft navy shadow (0 24px 70px #14243c33) and translucent backdrop (#14243c80); these do not define general card elevation.

### Separate Ágora console (apps/agora)

The system is mostly flat. Tonal changes between canvas, roster and raised controls establish the primary hierarchy; 1px warm dividers organise panels and rows. A soft leftward shadow separates the operations drawer on small screens, and the modal backdrop darkens the underlying workspace. No shadow vocabulary is used for ordinary cards or buttons.

## Shapes

### Hub shell

Application/project cards, launch rows and resource surfaces use 12px corners; primary links use 8px and rail links 9px. Shared fields use 6px, automation fields 8px, grouped form panels 10px and dialogs 14px. Authored navigation SVG is normally 20px with 1.7px rounded strokes. Project folders sit on pale cobalt tiles. Catalog icons are 56px and Home launch identities 52px (44px at 600px); compact app docks use 36px.

### Separate Ágora console (apps/agora)

Controls and containers use gently rounded corners: 6px for buttons, selectors and small fields; 8px for identity marks; 10px for the composer and dialogs. Fine borders define inputs and the composer, while list rows rely on separators rather than individual outlined cards. The roster leader marks are compact rounded squares; participant marks in the conversation are circular.

## Components

### Hub shell: navigation and controls

- **Navigation:** Inicio, Proyectos, Aplicaciones, Bandeja, Automatización and Sistema are the six primary destinations, with a separate Search entry. Bandeja opens Notifications, Automation opens Work and System opens Resources. A labelled native selector switches tools locally; unmatched retained facets remain under System. Selection uses aria-current, white text and cobalt fill on navy.
- **Applications:** Home launch rows use real app state/icons and the existing opening path. Home descriptions show an excerpt of at most three lines; the complete description remains in the text DOM and title attribute, and Applications → Manage → Details exposes the full application JSON. Catalog cards show one opening action and a native Manage disclosure retaining start/stop/restart, browser, windows, logs and other controls. Catalog management and Startup groups are native disclosures.
- **Project card:** White surface, pale border,12px corners and 28px padding (20px in the Home stack). The name/Tasks and discussions enter the embedded scoped board; Open Agora is a separate coordination-configured link preserving space_id. No URL is fabricated. Space management remains discoverable.
- **Primary link:** Cobalt with white text,8px corners,11px by 14px padding and 13px semibold type. Home project links use a quieter pale-cobalt variant.
- **Resources and Search:** Resources groups connections/engines, GPU/leases and browser lanes. Header Search/Ctrl+K opens local application/tool destinations alongside existing federated content search. Project and mail-list filters keep their own scope.
- **Automation forms:** Rules/jobs use labelled native fields for name, enabled state, event/cooldown or local time/interval, and repeatable actions. Parameter JSON remains where the existing model needs it. Advanced configuration exclusively replaces visual fields with full JSON. Switching back parses/rebuilds the form; invalid JSON stays open with text/error intact. Unknown fields survive unchanged document/action kinds; changing an action kind replaces that action structure. Existing APIs and copilot pending forms remain authoritative.
- **Mail and retained interiors:** One Attention/Unclaimed/Recent queue,20-message client pages and local loaded-result search remain. The full reader replaces queues/list; Back preserves queue/category/query/page within the sphere. Text DOM builds readable paragraphs/headings/HTTP links without interpreting message HTML. Hidden drafts survive polling and stay excluded from pending forms; sphere changes invalidate old responses. Chat sources remain a native disclosure; reference actions are buttons. Work, notifications and sessions use divided responsive records; repo/audit records retain actual header labels.
- **States and access:** Loading, empty, no-match and failure are written out, with Retry for project-load failure. The shell focus ring is 3px cobalt with 3px offset, subject to component-specific field rules. Skip-to-content and main focus remain available; navigation returns to document top. Reduced motion removes transitions, animation and smooth scrolling.

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

- **Do** keep opening apps, entering projects and managing shared tools distinct, with every original tool reachable.
- **Do** preserve full document scrolling, labelled local tool selection, wrapping and keyboard access.
- **Do** treat projects as persistent Ágora spaces and preserve coordination workspace_url and space_id.
- **Do** preserve unknown fields and invalid JSON while editing automation.
- **Don't** infer running apps or successful workspace availability from placeholders.
- **Don't** add raster decoration or import the separate Ágora amber display typography.
- **Don't** describe the navigation redesign as new backend, data or MCP capabilities.

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

