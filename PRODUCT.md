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

This section applies to `hoard_link/hub/ui/index.html` and its assets. The separate `apps/agora` console requirements above remain unchanged. The Hub opens applications, enters persistent project spaces and manages shared tools through Home, Projects, Applications, Inbox, Automation and System. Home pairs an application launch area with a project stack. Application cards show one Open/Start and open action and an explicit Manage disclosure; startup groups and catalog controls remain available. Inbox, Automation and System open Notifications, Work and Resources directly, with a labelled native selector for related tools. Every facet remains accessible, including unmatched facets under System.

The project directory reads `/api/agora/spaces`, not the full Faustus project catalog. Selection carries `space_id` into tasks/discussions and the native Agora link, derived from `workspace_url` from `/api/coordination/config`. No URL is fabricated when configuration is absent. Project board and conversations use document scrolling and reflow on mobile. Rules and scheduled jobs use native visual forms with a separate advanced JSON mode; advanced fields round-trip, and invalid JSON remains visible for correction. Preserve existing APIs, identity, forms, state and data.

The visual world is a navy 216px rail, cool light canvas, white work surfaces and cobalt actions. Mobile Menu expands in document flow. Loading, empty, no-match and error/retry states remain explicit. Search includes local application/tool destinations alongside existing content search. The redesign adds no backend capability and starts no app or model merely by navigation.
