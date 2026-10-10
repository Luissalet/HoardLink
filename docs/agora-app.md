# Ágora desktop console

Ágora has a complete workspace in its own Windows window, and a full browser view at `/agora/` on the local Hub. The existing Hub tab, task histories, revisions, locks, decisions, CLI and MCP remain available. It uses the same Python Hub service; the desktop window is Rust/Tauri with a React/TypeScript interface. There is no data migration into a second coordination system.

The left rail contains Claude, Codex, Cursor and Faustus. Each provider exposes a discovered model catalog and its supported reasoning settings, shared quota observations and child-worker states. Unavailable catalogs and quota readings remain unknown. Quotas belong to account pools, not individual chats; adding a helper does not add capacity. Expired readings retain their provenance and show unknown percentages.

The central conversation uses the canonical team thread. Messages select all four leaders or one recipient. Delivery receipts distinguish queued attention from an actual running agent. A mention alone does not wake an idle desktop chat. Results retain their actual author and source. Model preferences apply to the selected console leader's next execution, without rewriting unrelated existing chats.

The helper control creates a separate session with the selected model and reasoning. It preserves the principal's configuration, history and identity. External workers can register their actual lifecycle through the console API/MCP. A declared model observation is identified as declared; it is not promoted to a measured provider reading. Provider authentication failures are visible and are not represented as working sessions.

The right pane shows pending tasks and recent completed work, with the actual global completion total. Task details open their ordinary history and support replies. Full operations, including task creation, cross review, decisions and locks, remain accessible in the Hub. On narrow browser screens the leader rail becomes a compact strip and the tasks become a drawer. Draft messages survive page reloads on the same client.

## Run and build

From a source checkout, install dependencies with `npm ci` in `apps/agora`, then run `npm run desktop:build`. Start the native window with `scripts/start-agora.ps1`; `-Build` builds first and `-Web` opens the full browser view. The Hub must already be running. The shell only relays `/api/agora/` requests to a loopback Hub; it exposes no arbitrary shell, URL or credential-file access to frontend code.

For development, `npm run dev` in `apps/agora` starts the local UI on port62840 and proxies the Hub API. `AGORA_HUB_URL` can select a different loopback QA Hub. Tests use a separate Hub data directory and synthetic messages, preferences, workers and quotas. The native application contains self-hosted fonts and bundled frontend assets.

Build output and installed local binaries are generated files. Source distributions require the build step for `/agora/`; a missing build produces an explicit error rather than an empty console.

## Validation boundaries

Compilation establishes that the native shell builds; browser checks establish the shared frontend's actual interactions. A scripted provider in isolated QA only checks lifecycle behavior. Real provider participation requires a separate receipt from that provider and must not be inferred from a mock, a queued mention, a heartbeat or a success-shaped response. The task submissions and QA receipts record which providers and interactions were exercised.
