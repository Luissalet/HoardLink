# Shared Agora coordination

Luis authorised planning, debate and complete implementation on 13 October 2026. Agora is the common coordination product with multiple persistent spaces. The interface offers Conversation, Deliberation and Tasks and decisions. Council and Group remain internal compatible contracts, with all their capabilities and the development team's history preserved.

## Product and ownership

A space groups conversations, participants, tasks and decisions for a project or purpose. The current development Agora becomes **Faustus y Hoards**, retaining the `ecosystem` space and its existing common project link without changing its task, thread or message identifiers. A space contains sessions; a Council session is not itself a second space. Renaming a space changes its title, never its identity or history.

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

Other references checked on 13 October: [AutoGen teams](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html) and [state](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/state.html) document serial teams, selection and saved team state. [CrewAI hierarchical process](https://docs.crewai.com/v1.15.27/en/learn/hierarchical-process) documents manager-led delegation and task review. Their useful patterns complement native Faustus policies and Dispatch; they do not establish a comparative quality ranking.

| Behaviour | Reference pattern | Native implementation and evidence |
| --- | --- | --- |
| Durable room and member identities | Hermes persistent room/member sessions | Existing Group443 plus shared repositories496; membership edits retain the parent and unchanged sessions. Browser/API/MCP receipt and independent R2 tests. |
| FIFO and reconnect | Hermes server driver and room lease | Group driver reused; Council now has canonical ownership and FIFO. Independent retry/completed/interrupted and two-turn reproductions pass. |
| Membership during work | Hermes edits without replacing room history | Atomic current-step acquisition prevents removed queued members running; already started members finish once. Independent R2 reproductions pass. |
| Participant context | Hermes unseen room history | Native participant cursor supplies new peer messages and preserves parallel-round visibility. Native context tests cover the boundary. |
| Turn choice and saved state | AutoGen teams/state | Existing Council policies and Group serial/parallel modes remain; shared canonical state survives observers and preserves explicit budgets. |
| Delegation and review | CrewAI manager-led work | Existing Council roles, objections, synthesis and native Dispatch remain; Agora retains exact submission revisions and Git review. |
| Product integration | Project-specific comparison criterion | One Agora navigation, shared room catalogue, separate drafts and remembered room per mode/space; private browser tests and visual F1–F3 verdict. |

Limits of this comparison: native room functions are verified against deterministic model fixtures. No cross-project quality benchmark, multi-gateway parity, universal ranking or production activation is inferred from those tests. Remote-provider runners retain their original integrations; no reference project's runtime is added.

## Delivery evidence

Backend496 R2 is independently approved on Hub `649d257e` and Faustus `320c945e`: 176 tests pass, one retired boundary assertion is deselected. The two R1 failures and their original logs are retained. Interface497 R3 is independently approved on Hub548a4ddc and native77e9a5ff with457PASS+6JS. Visual F1–F3 and the final mobile selector target FINAL-F1 are scored resolved in separate limited verdicts; touch-only fe12f44f is composed as fc23b884. Parent494 stays open until composition, activation and preservation checks finish.

The isolated native composition starts at current shared `f7d974b6` and retains peer response-quality and Sparks-continuity source unchanged. Its broad test run records 762 passing, one skipped and two failed name assertions for intentional shared-module aliases. The corrected identity/fresh-import criterion passes all 16 tests in that file; the earlier failures are not overwritten or relabelled. The later broad native composition passes765 tests; the final coordination area passes216. Hub's full suite records4334PASS/86SKIP/2FAIL: one unchanged fake-DOM baseline failure and a2s quota wait that passes alone. Those failures remain in their original logs; the full suite is not labelled green. Private browser/API/MCP and offline desktop-relay evidence live in `D:/LocalAI/research-inbox/council-agora-20261013`.

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

## Production activation — 14 October 2026

Hubad94cd6 and nativef493eb2d are integrated and active. The original ecosystem is titled Faustus y Hoards and linked to existing project100d012d1ff3/workspaceD:/LocalAI. Native coordination binding reports bound=true/pending=false/authority=hub. Original Council and Group databases were empty at activation and remain unchanged; populated histories and migration are separately verified in private fixtures.

The actual Main browser shows all three modes at1440/390 with the selected title,44px mobile selector, loaded Hub iframe, no page errors or horizontal overflow. Main stdio MCP reads Hub173/Group9/Council12 tools with six read-only calls. Production page CSP initially blocked the embedded Hub; reviewed520R3 c20b5878+83cc4eac now allows the configured Hub origin on /agora, preserving all other pages. Independent review28PASS and real Main iframe pass. The initial blocked capture/log remain historical failures.

Consistent backups precede activation. All6330 original Agora messages retain IDs and field values;552 original threads and519 tasks retain IDs, with only subsequent coordination/doc activity changed. Native412 sessions and35 MCP configurations remain exact; original9 Council tables,3 Group tables and6 native coordination-cache tables are unchanged. Hub configuration/token and project/session/connector/key/controller files retain hashes; settings differ only in three existing runtime metadata fields, with Full/global Sparks selection and execution preferences preserved. All25 child Python processes of the previous Hub survive. The owned watcher pause is removed, its scheduled watcher restarted on reviewed all-space source and original SID preserved. No production QA room/model generation, model stop/load or push.

Evidence: research-inbox/council-agora-20261013/main-browser494.json, main-mcp494.json, history494-proof.json, activation-preservation494.json, activation-delta494.json and RESULT-494.md. Private model fixtures establish integration behaviour; global-model quality and universal comparative superiority remain unmeasured. Manual CAS/readback preserves161 paragraphs/158 other paragraphs and4 tables; the project graph adds only the owned feature node without embeddings. Final subsequent commits containing documentation only do not require another runtime reload.
