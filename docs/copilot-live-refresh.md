# Live copilot updates

The shared Python launcher forwards each `faustus:event` of type `tool_output`
as `hoard:copilot-change`, without ending or remounting the chat. It calls
`window.hoardCopilot.onChange(detail)` when supplied, otherwise the existing
`onTurnEnd(detail)` refresh callback for compatibility. Actual turn completion
continues to emit `hoard:copilot-turn-end` and invoke `onTurnEnd`.

Hosts reload saved data in place, coalesce notifications and preserve unfinished
edits. The tool receipt means the tool has returned; a failed or read-only tool
can also trigger a harmless reload. Deltas and tool starts do not refresh views.
No extra model calls, full-page reloads or automatic tool replays are involved.

Silver listens through its existing refresh registry (250 ms debounce). Trainer
fields and dialogue accept saved changes to untouched fields while keeping local
drafts. A pristine team builder follows the saved team, including its initially
empty state; a locally edited team remains intact. Switching trainers mounts a
separate editor. API/MCP mutations and their persistence are unchanged.

Verification uses the real native Silver handlers and panel with a controlled
SSE fixture: saved name/team changes and empty-builder content appear while the
turn remains open; a local note survives a concurrent saved change. This tests
transport and rendering, not model quality. Native MCP separately reads results.
The generated Node launcher and other host implementations require their normal
vendor synchronization before these callbacks become available there.
