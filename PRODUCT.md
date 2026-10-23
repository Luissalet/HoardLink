# Product
<!-- impeccable:product-schema 1 -->

## Scope
Ágora is the shared workspace of the existing Hoard Hub. This product context covers the new complete desktop conversation surface in apps/agora, while retaining the Hub coordination service and all other HoardLink functions.

## Platform
web

The React/TypeScript interface has a Rust/Tauri Windows shell and a full browser view. The existing Python Hub remains the authoritative service and data store; the agents agreed this architecture in the Agora decision thread.

## Stack
Delegated by Luis: agents must debate, decide and implement while he sleeps, without requesting another confirmation. Frontend choices are ours. Source documentation and available tools confirm Rust/cargo and an existing local Hub at127.0.0.1:8810.

## Users and work
Explicit primary user: Luis, supervising Claude, Codex, Cursor and Faustus as a team.
He needs to give one natural request to everyone, see their attributed responses and progress, choose each leader's actual available model and reasoning level, and see remaining usage continuously.
Agents coordinate tasks, path locks, discussions, independent revision-aware reviews, checkpoints, integrations and decisions through the same Ágora.

## Required workflows
- Four provider leaders with working model/effort choices from actual catalogues.
- Always-visible remaining quota, reset and freshness; correctly shared account pools.
- Central group conversation with Todos/per-leader destinations, durable delivery and honest queued/running/error states.
- Economical worker creation, child identity and results below its leader; principal settings/history unchanged by helper creation.
- Tasks/reviews/activity accessible alongside conversation; preserve existing capabilities and data.
- Full separate window/view, not a narrow nested Hub tab.
- Use the delivered surface for current Faustus/Hoards work once verified.

## Constraints and truth
No push. Isolated development and normal Ágora locks/review/integration protocol.
No invented models, reasoning options, prices, quotas, replies, running workers or completion.
Unknown quota is unknown, not0; several agents can share one account quota. Local GLM does not have a subscription percentage unless a real source supplies one.
Existing CLI/MCP/API/tables/history remain canonical. Only human UI writes asLuis; agents retain their own identities.
No new arbitrary autonomy barriers or approval gates. Real credential/data/irreversible and financial protections retain their role.
Model QA uses already-resident GLM on the three Sparks, no PCGPU models. Watch/Book excluded and SABER parked.
Principals remain intact; explicit model changes from the leader's own selector apply to that leader, cheap helper selection affects the child.

## Voice and accessibility
Castilian Spanish, plain operational text without slogans. Keyboard operation, clear focus, readable contrast and status beyond colour. Models and reasoning appear only as meaningful user choices.

## Open decisions
Architecture discussion is thread222. Details of provider runner discovery and session mapping are being verified by the second Claude agent/task208.
Inferred visual assumption: a calm dark workspace fits the existing Hub family; this is a design choice delegated by the user, not a claimed user preference.
No assumption of instant replies when a provider runner is unavailable.


## Hub workbench (separate scope)

This section applies to `hoard_link/hub/ui/index.html` and its assets. The separate `apps/agora` console requirements above remain unchanged. The Hub helps Luis see what needs attention, continue a project, discover an application by purpose and automate common work. Its six primary destinations are Home, Projects, Applications, Inbox, Automation and System, with visible Search and Help/settings access. Related tools use visible contextual links that wrap rather than a selector. New ungrouped tab facets fall back to System; this does not turn header controls such as spheres into tab routes.

Home combines pending items from `/api/today` with unread notifications, up to three project spaces ordered by locally remembered visits and up to four application launch entries. Recent projects are browser-local history, not server activity or a list of recent files. Project entry opens an overview of pending tasks and conversations from that project's board, then its own Agora with direct task/thread opening. Selection preserves `space_id`; native full-conversation links use coordination `workspace_url`. The directory still reads `/api/agora/spaces`, not the complete native Faustus project catalog. It neither invents missing spaces nor moves existing work.

Applications combines purpose categories, concise Spanish/English descriptions and Spanish search synonyms for known entries. Unknown manifests retain their purpose/name/capabilities fallback under Other tools. This is local catalog discovery, not semantic search or a capability audit. One opening action, Manage, catalog controls and startup groups remain available. Startup configuration problems have a plain explanation; the raw launch reason remains in Manage → Details. The known descriptions follow the actual products: Dorian personal context/reflections with sources, Pygmalion datasets/model development and evaluation, Atlas shared project files/folders, Vitruvius software/design work, and Galton model evaluation through the Pygmalion studio. Navigation and discovery start no app or model.

Automation starts with Schedule an action, When something happens, Review runs and Purchases/orders. Five known event types have human labels. Backups and application/group start/stop/restart choices use normal controls without JSON or internal IDs. Custom events, application/Hub tools and event parameters remain technical advanced paths. Within an open action editor, changing type and returning restores that type’s application/tool/group/event/parameter draft. For app-start/stop events, Affected application is a condition on the Hub event: choosing it writes source hub and where["data.app"], preserving other advanced filters. Rule/job forms preserve unknown fields while their kind is unchanged; full JSON is an exclusive alternate editing mode and invalid text remains available for correction. Existing APIs, CLI/MCP, forms, identities, drafts, configuration and data remain canonical.

Notifications offer a plain excerpt of at most 180 characters and a disclosure of the complete original body. Search keeps local app/project/tool destinations alongside content results, a retry path and technical source details behind a disclosure. System puts connections before GPU resources and explains that a stopped app can be available on demand and an unconfirmed service is not necessarily failed. Help links to existing settings; it does not create new account or model settings.

The Hub defaults to the official Hoard family dark theme: warm charcoal, gold actions, ivory text and the family serif brand stack. This is Luis's explicit correction in task 1363 after rejecting the light identity of 1301. `html[data-hoard-app="hub"]` selects the existing `hoard_link/ui/hoard-theme.css` tokens; `workspace.css` retains the task-first layout without replacing that palette. The original repository `app-icon.png` is packaged byte-for-byte as `hoard_link/hub/ui/hub-icon.png` for the header and favicon (SHA256 7d39b4930c47485506ab6e6bb2889b581224f79c811ec27c068e7b1f1802ee9d). No raster was generated or edited. The separate `apps/agora` console remains outside this correction.

In the project's Agora, Te esperan / Waiting for you displays each complete escalated question and Responder aquí / Reply here beside it. The inline composer uses the native post path for Enviar; a reply reopens the escalation without completing its task. Responder y cerrar uses the native resolve path and is offered only for non-task threads. The full-thread entry remains available. The send captures its original project/thread, disables duplicate submissions, retains text on error and hides rather than discards it on Cancel. Drafts remain scoped per project/thread during refresh, language changes and project switching; this is in-memory continuity, not persistence after a browser reload. Late inbox reads cannot undo a refreshed/sent state. Selecting another project from Agora stays in Agora.

Waiting items, conversations and board load in parallel. The screen inbox requests one ordinary message while preserving complete escalations, and simultaneous inbox requests are shared. Board SQL selects non-final tasks before hydrating, including open tasks, with fifteen final tasks and full-space counters retained. The HTTP/CLI/MCP contracts, authentication and Luis-only UI identity rules remain canonical; no new MCP tools or authentication bypass were added.

Current source: 260805ebbbd2c89b3f7b4d4ba3eaa77fce0fe437 plus 0f52044d04f515a40958a4c99cf795334a0a6f49, approved independently at submission_revision 2 with a bounded SHIP verdict. The 1301 work/project/purpose/automation workflows above remain in force; its former light-world guidance is superseded. This records the reviewed correction, not a new usability score or a blanket closure of critique 1296. No claim covers every live app, custom automation or the complete Faustus project inventory. Evidence and honest historical QA are recorded in the surface brief and docs/hub-navigation.md.
