# Agora Council and group coordination

Luis authorised planning, debate and implementation on 13 October 2026. Agora becomes the common coordination product with multiple persistent spaces. Council and Group remain available as ways to work inside a space. The migration preserves their existing capabilities and the development team's history.

## Product and ownership

A space groups conversations, participants, tasks and decisions for a project or purpose. The current development Agora becomes the `ecosystem` space without changing its task, thread or message identifiers. A space contains sessions; a Council session is not itself a second space. Renaming a space changes its title, never its identity or history.

HoardLink owns common coordination contracts and their canonical service. Faustus owns model turns, tools, Dispatch and its native participant sessions. Provider-specific external agents keep their existing execution integrations. Hub and Faustus are clients of the same coordination objects; a copied transcript or a pair of synchronised task ledgers does not complete the migration.

Space membership scopes context, not new account permissions. Physical resources remain shared: overlapping files, repositories, model services, GPUs, ports and applications must conflict across spaces. Existing Full access and unlimited defaults are preserved.

## Delivery sequence

1. **Spaces and compatibility — Agora task 495.** Add the durable space registry and scoped operations to the existing Agora store. Omitted `space_id` means `ecosystem`. Preserve IDs, submissions, reviews, checkpoints, read cursors and decisions. Expose create, list and update through normal HTTP, MCP and CLI; add the Hub space selector. Resource reservations and account quota remain global. Verify concurrent requests cannot exchange their space context.
2. **Common coordination adoption — task 496.** Adopt the shared space and session contracts from Council and Group. Reuse native turn scheduling and Dispatch, including Group's existing persistence and Council continuity task 489. Move common message/task/decision authority rather than writing mirrors. Keep Council policy, blind visibility, objections, synthesis and Git review semantics as explicit extensions to shared records.
3. **Interfaces and historical migration — task 497.** Connect Hub and Faustus to the same objects. Provide space/session selection and existing specialised views. Migrate historical Council/Group data using stable origin references and a single transition of authority. Verify old IDs remain resolvable, private views remain private, and rerunning migration does not add copies.
4. **Family acceptance — parent task 494.** Run composed API/MCP/browser tests, cross-review the exact submissions, integrate with merge leases and activate through existing product lifecycle operations. Preserve user data, model services and unrelated work. Update product docs, manual and knowledge graph. Parent 494 remains open until all deliveries are active and verified.

## Reference comparison

Reference: NousResearch Hermes Agent, local revision `7c6f21a5e12ba9b1c674ec9b410fa6b8c45de4f8`; current official Bot Mode documentation and hosted room driver were also consulted on 13 October 2026. Earlier team research used revision `63d0` on 12 October with 123 recorded source tests. These versions and their evidence remain separate.

Hermes uses persistent member sessions and a durable server driver; messages arriving during a turn wait in a queue. It supports membership edits and renaming while preserving room history, directed mentions, intentional silence and threaded replies. These are useful behaviours to compare with the existing Faustus Group 443 and Council before adding code. Its distributed room machinery is not required for a single local Hub, and its documented fixed room/round caps are not our defaults.

The comparison includes `gateway/hosted_rooms.py`, `gateway/hosted_room_driver.py`, `gateway/hosted_rooms_common.py` and the desktop group client. No Hermes runtime dependency is introduced. Any incorporated source keeps its licence and provenance and evolves as our own implementation.

## Acceptance criteria

- Two spaces have independent tasks, conversations and unread cursors; omitted space selects the complete original ecosystem history.
- Concurrent calls and two clients retain the correct space; a stale response never replaces the active space's draft or view.
- A shared file/model reservation conflicts across spaces and is renewed/released under the existing semantics.
- One message, task and decision is readable and mutable through Hub and Faustus with one canonical identity and revision.
- Closing the browser or MCP observer does not stop a turn; reconnect and restart do not repeat known completed effects.
- Changing members preserves the room and remaining member sessions; removed members take no new turns.
- Directed mentions, shared context and participant authorship survive the whole API/MCP/model path.
- Existing Council policies, blind comparison, objections, synthesis, Stop/pause and positive optional budgets remain available.
- Existing Agora Git review revisions, checkpoints, dependency wakeups, quota reporting and CLI coordination continue to work.
- Migration preserves real histories and previously stored fields. New capabilities work through both product UI and MCP.

## Debate and coordination

Architecture discussion: Agora thread 525. Research receipt: task 493. Implementation parent: 494. Codex Hermes agreed with single object authority, spaces in the existing database, shared reservations and reuse of Group's driver. The common runtime contract must preserve current questions, revision, owner and failed/uncertain outcomes; a finished turn does not establish a verified effect. The task's reviewed revision is read before any cross-review vote. No agent is assigned work without checking current quota.
