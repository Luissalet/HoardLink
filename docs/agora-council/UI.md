# Hub Ágora: space controls

## Overview

Surface: the existing Ágora tab in Hoard Hub (`hoard_link/hub/ui/agora.js`). Mode: **Operate**. This is the preserving UI extension in delivery 495, reviewed at submission revision 2, source `2cad9641f32723429a2ccef71e4973fc566ee5b0` over `45197009bc384c643ae51bc2aad065410b195587`.

The task is to select a persistent coordination space, create one or rename its title while retaining the existing agents, tasks, conversations, reviews, decisions, reservations and full Ágora link. The default is `ecosystem`; existing record identities and history remain canonical. See [SPACES.md](SPACES.md) for the service contract and [PLAN.md](PLAN.md) for the remaining migration.

This document records only the Hub extension. Root `PRODUCT.md` and `DESIGN.md` primarily describe the separate `apps/agora` conversation surface; its three-column composition is not the authority for this tab. Source integration does not establish production runtime activation. Full Council/Group adoption and history migration remain outside this delivery.

## Colors

Use the incumbent Hub palette from `hoard_link/ui/hoard-theme.css`, selected by `html[data-hoard-app="hub"]`, through the mappings in `hoard_link/hub/ui/style.css`. The space controls introduce no new palette or raster assets.

The warm charcoal canvas, sunken fields, ivory text, muted labels and warm borders remain unchanged. Amber identifies the existing primary save action. The selector and toolbar actions inherit these same primitives; retain their source tokens rather than copying the separate app's surface values.

## Typography

Keep the shared Source Sans 3 stack for operational text and the existing Hub base (14px). Small toolbar buttons use the existing 12px rule. The selector inherits the surrounding font; its label uses the incumbent hint treatment. Cinzel remains the Hub brand face and is not added to the space controls.

## Layout

The space row sits below the existing new-task/new-conversation toolbar and above the existing search/type filters and board. Its order is the labelled selector, **Nuevo espacio**, then **Renombrar espacio** (with the corresponding English labels).

The row is flexible and wraps, with 6px between controls and 8px between label and selector. The selector has a maximum width of `min(360px, 65vw)` and can shrink. At narrow widths, actions wrap into the next line; the captured 390px surface shows the rename action on that line. Keep these controls in normal flow above the board.

The existing board and detail layout remains authoritative: desktop detail splits beside the board from 1200px; the board becomes one column at 640px and below. Creating or renaming opens the existing inline form container between the space row and filters.

## Elevation & Depth

The added toolbar remains flat. Shared borders and sunken field backgrounds distinguish controls; the inline form uses the existing elevated surface. No new shadow or ornamental treatment is introduced.

## Shapes

The selector uses the existing 8px field corners, warm border and 6px by 8px padding. Toolbar buttons inherit the Hub's small-button treatment (4px by 9px padding). The inline form retains its 10px corners and 8px inset.

## Components

- **Space selector:** a native `select` inside a visible label, with an explicit localized `aria-label`. Options display the space title and retain the persistent ID as their value. The title is presentation; renaming preserves identity and history.
- **Create/rename form:** a labelled title input, primary save action and ghost cancel action. Rename pre-fills the observed title and submits its observed revision. Empty titles produce the existing error toast; pending save disables that button; failed responses leave the form available. The input receives focus when the form opens. A stale revision follows the service's conflict response rather than silently replacing a newer title.
- **Space views:** switching retains each space's current task/conversation detail and inline form nodes during the current page session, including their drafts. This is session preservation, not durable storage of unsent text. Asynchronous calls capture their space and selection epoch; an older response cannot repaint the newly selected space. Task block comments and review calls retain their space context.
- **Return links:** non-default spaces use `?space_id=<id>`; a selected conversation also uses `#agora-<thread-id>`. The default space keeps the existing URL form. Escalation links include the non-default space context so the destination opens the correct conversation.

## Do's and Don'ts

- **Do** extend the incumbent Hub controls and keep every existing workflow discoverable.
- **Do** preserve persistent IDs, selected context and independent drafts when changing space.
- **Do** leave quota pools and physical resource reservations shared across spaces, as [SPACES.md](SPACES.md) specifies.
- **Don't** turn this narrow toolbar extension into a replacement of the Hub layout or the separate app's design system.
- **Don't** describe unsent drafts as persisted across page reloads, or this delivery as the completed Council/Ágora unification.

Evidence: the private fixture captures `research-inbox/council-agora-20261013/desktop.png` (1440×900) and `mobile.png` (390×844) show the incumbent toolbar and wrapping. `REVIEWER-495-R2.txt` records **ship**, approval on revision 2, 44 passing area tests and independent R1-red/R2-green probes for block context and escalation links. The reviewer reused those captures because R2 made no visual change; it did not claim fresh keyboard, focus, screen-reader or long-title QA. The documenter inspected the supplied captures and exact source without another browser run.
