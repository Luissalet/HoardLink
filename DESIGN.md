---
name: HoardLink Hub and Ágora
description: Scoped design systems for the Hub navigation shell and the separate Ágora team console.
colors:
  hub-sage: "#c4d9b5"
  hub-sage-hover: "#d7e7cd"
  hub-canvas: "#191b1b"
  hub-surface: "#202323"
  hub-raised: "#272b2a"
  hub-text: "#f0f1ed"
  hub-muted: "#b3bdb8"
  hub-divider: "#3b4240"
  hub-ink: "#192319"
  hub-selected: "#354332"
  hub-selected-text: "#e1efda"
  hub-nav-hover: "#2d3430"
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
    fontSize: "32px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  hub-title:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "17px"
    fontWeight: 600
  hub-body:
    fontFamily: "'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', 'Liberation Sans', Arial, sans-serif"
    fontSize: "14px"
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
  hub-link: "6px"
  hub-nav: "7px"
  hub-card: "10px"
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
    backgroundColor: "{colors.hub-sage}"
    textColor: "{colors.hub-ink}"
    rounded: "{rounded.hub-link}"
    padding: "10px 15px"
  hub-nav-link:
    textColor: "{colors.hub-muted}"
    rounded: "{rounded.hub-nav}"
    padding: "12px"
  hub-nav-link-selected:
    backgroundColor: "{colors.hub-selected}"
    textColor: "{colors.hub-selected-text}"
    rounded: "{rounded.hub-nav}"
    padding: "12px"
  hub-project-row:
    textColor: "{colors.hub-text}"
    padding: "23px 0"
  hub-app-card:
    backgroundColor: "{colors.hub-surface}"
    textColor: "{colors.hub-text}"
    rounded: "{rounded.hub-card}"
    padding: "22px"
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

**Creative North Star: “The Project Library”**

The Hub is a graphite directory of project spaces and shared tools. Sage marks selection and the explicit action to enter a project's native workspace. Familiar shared sans typography, outline icons and generous divided rows make destinations readable without turning the directory into a dashboard of metrics.

**Key Characteristics:**
- Graphite canvas, sage selection and warm white text.
- Six navigation groups with contextual subnavigation.
- Broad project rows, restrained running-app summaries and full document scrolling.
- Code-led interface with authored SVG outline icons; no new shipping raster imagery.

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

The `hub-` palette is defined by the final overrides in `navigation.css`, loaded after `style.css` and the shared Hoard theme. Sage is the primary accent; graphite canvas/surface/raised values separate the frame, and warm white/muted text separates content from metadata. Selected navigation uses `hub-selected` and `hub-selected-text`; ordinary hover uses `hub-nav-hover`. Fine rules use `hub-divider`.

The later `interiors.css` rules bring fields in main content and dialogs onto the Hub graphite surface, text and divider tokens. Other preserved facet-specific controls can still consume `--hoard-*` values directly; keep those exceptions local. The existing Hub SVG brand mark retains its original gold identity.

**The Hub Scope Rule.** Use the `hub-` palette for the navigation shell; keep legacy facet-specific and separate Ágora tokens scoped to their own controls and surface.

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

Hub headings, navigation, body and controls inherit the shared sans stack in `hoard_link/ui/hoard-theme.css`; no font download or display face is introduced. Source Sans 3 is first in the stack when available, followed by the system fallbacks recorded in frontmatter. The page heading uses `hub-heading` and steps down to 27px at the mobile breakpoint. Section/project titles use `hub-title`; page descriptions use the 14px body with a 1.65 line-height and 70ch maximum. Rail links are 14px, contextual links 13px, and project metadata 12px with a 1.5 line-height. The Hub brand label is sans (18px, weight 650; 17px mobile). Shared-tool controls use 14px type, row subjects/titles 15px and long-form mail reading 16px at a 1.8 line-height with a 72ch maximum measure. Supporting metadata remains 12–13px; the Mail reader title scales from 24px to 32px.

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

Desktop uses a 224px rail and flexible workspace. The rail is sticky at the document top with a minimum viewport height, except on desktop viewports at most 850px high where it returns to document flow; the compact environment header remains in document flow (76px minimum). Content is centered up to 1640px with 32px top, 42px side and 60px bottom padding. The page heading separates title/description from wrapping actions, followed by a 28px gap.

Home places the project list beside a 250px running-app area with a 44px gap and a thin divider. It shows at most four title-sorted persistent spaces and a View all link; the Projects route contains the full filterable list. Below 1180px, the running-app area moves beneath the project list and row actions can wrap. Shared tool rows follow, with the existing Today facet below them.

At 760px and below, the rail becomes a normal-flow top bar. Menu expands the navigation in the document, pushing content down; it is not an overlay or drawer. The header becomes 60px minimum with wrapping utilities. Main padding becomes 26px 20px 40px; row actions indent under the project name; primary links and the menu retain a 42px minimum height while content buttons/selectors use a 44px minimum. Navigation links have a 44px minimum height. The page heading wraps with a 28px following gap.

Pages use full document scrolling. The Hub's embedded project board/list and selected task or conversation detail alternate within the same route instead of creating nested reading panes. Scoped overrides remove legacy max heights and inner scrolling from the embedded conversation, detail and other reading regions. The task board is two columns on desktop and one on mobile. Events wrap their data and become stacked records on mobile. Shared-tool buttons/selectors are at least 38px high on desktop. Work, notification, chat, reference and agent-session rows reflow their content and actions at the 760px breakpoint rather than preserving desktop columns. Repo/audit tables become labeled records at 1400px (three fields per row, two at 760px), using labels from their actual table headers. The repository detail dialog owns its vertical scrolling; its body text wraps and its mobile header stacks title/actions with 44px controls.

### Separate Ágora console (apps/agora)

The desktop composition is a 64px top bar over three columns: 266px leader roster, flexible conversation with a 360px minimum, and 320px operations pane. At widths above 1500px the side columns grow to 284px and 356px; below 1080px they contract to 236px and 274px. The conversation remains the flexible center, with message text capped at 75ch.

Below 900px, the operations pane becomes a right-side drawer up to 320px wide. At 640px and below, the top bar is 56px, the roster becomes a horizontal leader strip with 192px cards, and the conversation fills the remaining vertical space. The composer stays at the foot of the conversation. The spacing rhythm is compact and built from repeated 4, 8, 12, 16, 20, 24 and 32px steps; use exact values only where the implementation establishes them.

## Elevation & Depth

### Hub shell

The directory is flat: tonal surfaces, fine borders and row dividers carry hierarchy. The application catalog explicitly removes card and icon shadows. Existing resource status dots, dialogs and facet-specific state treatments retain their local behavior; they are not a new shell elevation scale. The rail and main content have no decorative shadow.

### Separate Ágora console (apps/agora)

The system is mostly flat. Tonal changes between canvas, roster and raised controls establish the primary hierarchy; 1px warm dividers organise panels and rows. A soft leftward shadow separates the operations drawer on small screens, and the modal backdrop darkens the underlying workspace. No shadow vocabulary is used for ordinary cards or buttons.

## Shapes

### Hub shell

Rows use open surfaces and bottom dividers. Primary links have 6px corners; rail links 7px; contextual links 5px; application cards 10px. Shared buttons retain the theme's 8px corners; the later content/dialog field overrides use 6px corners and 9px by 11px padding. Authored navigation icons are inline SVG outlines, normally 20px with 1.6px rounded strokes; project folder icons are 26px desktop and 22px wide mobile. The existing Hub identity remains an SVG asset.

### Separate Ágora console (apps/agora)

Controls and containers use gently rounded corners: 6px for buttons, selectors and small fields; 8px for identity marks; 10px for the composer and dialogs. Fine borders define inputs and the composer, while list rows rely on separators rather than individual outlined cards. The roster leader marks are compact rounded squares; participant marks in the conversation are circular.

## Components

### Hub shell: navigation and controls

- **Navigation:** Home, Projects, Applications, Communication, Automation and System form the six groups. The active group exposes contextual subnavigation. A separate Hub search link follows a divider. Selection is expressed with `aria-current`, sage tonal fill and text; hover uses a quieter graphite-green fill. Existing facets absent from explicit groups remain reachable under System.
- **Primary workspace link:** Sage fill with dark ink, 6px corners, 10px by 15px padding, 13px semibold text and a 16px arrow. Hover uses the lighter sage token. It opens the coordination-configured native workspace in a new tab with the selected `space_id`; if the URL is unavailable the link is omitted.
- **Project row:** Open divided row with folder icon, name, workspace description, Open Agora link and Tasks and discussions button. The project name and task button enter the embedded scoped board; opening the native workspace is a separate explicit action.
- **Tool row:** Descriptive title, optional supporting line and trailing arrow. Rows are divided, with a quiet filled hover state; group pages and Home shortcuts share the pattern.
- **Search field:** Graphite `hub-surface` fill, shell text/divider, 6px corners, 9px by 11px padding and sage focus border in main content. Project search retains its 440px maximum width; Mail search uses up to 380px and becomes full-width on mobile. Scope the later interior overrides to main content and dialogs.
- **Mailbox:** One active Attention, Unclaimed or Recent queue with counts and a sage selected underline. Divided full-width message buttons separate sender, subject/excerpt and date on desktop; at 1400px the summary spans below sender/date. Filters and local pagination remain above/below the list. Search applies to loaded subject/sender/excerpt fields; page size is 20, over up to 40 attention, 100 unclaimed (both seven days) and 40 recent messages, rather than an archive-wide search.
- **Mail reader:** Selecting a row replaces queues/filters/list with a full document reader. Back preserves queue, category, query and page within the sphere, returning focus to a row. The title, sender, date/sphere, claims/reasons, body and related links/attachments form a reading hierarchy. Paragraphs and simple headings are built as text DOM; only resolved HTTP/HTTPS links become active, with no message HTML execution. Settings/status are hidden while reading; their unsaved draft is retained but excluded from the copilot's pending forms until visible again. A sphere change resets list/filter/reader state; superseded list/detail responses are discarded. Loading and error/retry states remain explicit without substituting successful or populated content.
- **Chat sources:** Connection administration sits in a native Sources and connections disclosure; the history remains the primary reading region. Chat author/date, message and source controls wrap on mobile. Disabled connection rows retain full opacity and an explicit written state. Reference result and relation endpoints use native buttons, preserving visible keyboard access. Agent-session summaries and reasons use 14px text.
- **Application card:** Shell surface, 1px divider, 10px corners and 22px padding; no card shadow. Application controls, startup groups and status logic stay available on the Applications page.
- **States and access:** Loading and failure are written out, failures expose Retry, and empty project/search results use text. `:focus-visible` is a 2px sage outline offset 4px, subject to more specific inherited field rules. The skip link becomes visible on focus; route transitions focus main content and return to the document top. Reduced-motion preference disables transitions, animation and smooth scrolling.

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

- **Do** keep project spaces separate from shared resources and tools, with the six destinations and all original facets reachable.
- **Do** preserve full document scrolling, contextual selection, readable wrapping and keyboard access at desktop and mobile sizes.
- **Do** treat the project directory as persistent Ágora spaces, not a complete registry of every Faustus project.
- **Do** derive the native workspace URL from coordination configuration and preserve the selected `space_id`.
- **Don't** infer running apps, activity or successful native-workspace availability from placeholders.
- **Don't** add raster decoration or import the separate Ágora console's amber display typography into the shell.

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

