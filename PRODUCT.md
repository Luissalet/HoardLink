# Product
<!-- impeccable:product-schema 1 -->

## Scope
Ágora is the shared workspace of the existing Hoard Hub. This product context covers the new complete desktop conversation surface in apps/agora, while retaining the Hub coordination service and all other HoardLink functions.

## Platform
Web interface in its own Windows desktop window, with a full web view for browser access. Tauri/Rust desktop shell and React/TypeScript frontend are the delegated architecture proposal; existing Python Hub remains the authoritative service and data store.

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

