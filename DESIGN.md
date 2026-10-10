---
name: HoardLink Ágora
description: HoardLink Ágora team coordination console.
colors:
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

# Design System: HoardLink Ágora

## Overview

**Creative North Star: “The Team Room”**

Ágora extends the Hoard family's warm charcoal workspace into a full team console. A restrained amber wordmark anchors the frame, while quiet dividers make the three working areas legible: a permanent leader roster, a central group conversation and an operations pane. Source Sans 3 carries the information; Cinzel is reserved for the Ágora wordmark.

The surface favors named states, authorship and source details over decoration. The desktop keeps four leaders visible beside the conversation. At narrow widths, the leaders become a horizontal roster and operations open as a drawer; the conversation and composer remain the main work area. The visual system is a description of the shipped UI, not a claim that every provider workflow is complete.

**Key Characteristics:**
- Warm charcoal surfaces with a focused amber accent.
- Compact, divided work areas with a readable conversation measure.
- Status, authorship and quota provenance are written out alongside color.
- Provider state uses distinct restrained avatar colors; the wordmark remains the only display face.

## Colors

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

The desktop composition is a 64px top bar over three columns: 266px leader roster, flexible conversation with a 360px minimum, and 320px operations pane. At widths above 1500px the side columns grow to 284px and 356px; below 1080px they contract to 236px and 274px. The conversation remains the flexible center, with message text capped at 75ch.

Below 900px, the operations pane becomes a right-side drawer up to 320px wide. At 640px and below, the top bar is 56px, the roster becomes a horizontal leader strip with 192px cards, and the conversation fills the remaining vertical space. The composer stays at the foot of the conversation. The spacing rhythm is compact and built from repeated 4, 8, 12, 16, 20, 24 and 32px steps; use exact values only where the implementation establishes them.

## Elevation & Depth

The system is mostly flat. Tonal changes between canvas, roster and raised controls establish the primary hierarchy; 1px warm dividers organise panels and rows. A soft leftward shadow separates the operations drawer on small screens, and the modal backdrop darkens the underlying workspace. No shadow vocabulary is used for ordinary cards or buttons.

## Shapes

Controls and containers use gently rounded corners: 6px for buttons, selectors and small fields; 8px for identity marks; 10px for the composer and dialogs. Fine borders define inputs and the composer, while list rows rely on separators rather than individual outlined cards. The roster leader marks are compact rounded squares; participant marks in the conversation are circular.

## Components

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

