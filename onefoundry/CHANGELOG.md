# OneFoundry changelog

## 0.21.2

- Record a known rejection when a Chief action becomes unavailable during its turn, so stale retry, cancellation and completion calls do not create an uncertain outcome before dispatch.
- Allow an explicit operator acknowledgement to preserve unstarted queued work. Running work, unresolved tool receipts and pending approvals continue to hold recovery.
- Open blocked-agent reviews on the saved feedback, with the latest requested changes first and assignment context available in its own tab.

## 0.21.1

- Separate Current work actions with wrapping, touch-sized buttons so Resume work and review controls stay readable on mobile. Preserve the same callbacks and disabled states.

## 0.21.0

- Prefer publisher-provided Markdown at the same searched URL and read meaningful HTML content before navigation. Keep search JSON, source admission, full-body secret checks and existing byte/time bounds.
- Add scoped, hashed pages of attached text so team agents can inspect complete files after prompt excerpts run out of space. Preserve existing project and work-order links and reject altered or sensitive content. Migration `0094_input_file_pages` admits the new native read receipt without widening SQL grants.
- Keep existing database privileges visible throughout runtime-role reprovisioning and roll back failed grant changes atomically.
- Distinguish active tool work from unresolved stopped actions in Current work, accept nullable progress targets, and show compact recent saved-file cards with exact file selection.
- Show current delegated work with clear assignment labels and keep past agents available in a collapsed history.
- Make doctor honor the existing validated health deadline. Preserve safe preflight stage and error categories without saving exception text.

## 0.20.3

- Stop a team turn after its saved terminal report or completion, and reconcile exact verified saved terminal outcomes without replaying uncertain attempts or rewriting history.
- Add Current work counts, saved deliverables and visible conversation resume controls.
- Preserve specific web failure categories and fit extracted text within the existing serialized result bound; allow explicitly configured larger bounded source responses.

## 0.20.2

- Settle known memory-preparation dependency failures before a model turn starts, preserving safe failure reasons instead of leaving work running until interruption recovery.
- Return specific repository read failures for protected content, unsupported files, size limits and changed source revisions. Preserve exact-source reads, hashes and credential screening.
- Manage an already configured private search service with installed stack startup, shutdown and readiness so app-network recreation does not strand research.
- Allow the operator CLI to configure the existing unfinished-assignment capacity, retaining its validated 1–50 range and explicit restart requirement.

## 0.20.1

- Fix Codex AI connection status to show expired logins and reconnect requirements, with same-account reconnect, login completion, and status refresh controls. Preserve agent bindings and keep saved models unavailable until the connection is renewed and models are refreshed.

## 0.20.0

- Expand the existing Tool Gateway with a project extension catalog, shared API/MCP/webhook connections, exact-call approvals and an Integration Hub / Tool Foundry interface.
- Add the Python SDK, inert package validation/import/export, reviewed skill drafts and immutable versions, and a separate isolated host worker for Python and pinned stdio MCP.
- Add signed, deduplicated inbound webhooks to the existing automation pipeline. Keep credentials outside packages and preserve role grants, run snapshots and uncertain-effect receipts.
- Keep extension tool grants bound to the full package/tool identity, reserve reviewed identities after revocation, and serialize package activation and related approval decisions with consistent project lock ordering.
- Patch the transitive source-map-js build dependency and guard its indexed-map input bound.
- Source implementation and disposable acceptance are separate from installation and live provider activation.
- Document the extension workflow, Python SDK quick start, gateway architecture, contributor setup, and current verification limits in the public README and operator guide.
- Explain cloud API and local Ollama role models, external Codex/Claude coding workers, scoped assignments, and explicit Codex-to-company-memory retention with current qualification limits.

## 0.19.1

- Complete Grok account device sign-in, automatic resume, same-account reconnection, and protected token renewal/recovery. Keep API-key connections separate.
- Correct Grok catalog and streaming proxy authentication, and enable configured Grok accounts in the existing SDK team/tool runtime with bounded output and operator approval gates.
- Preserve stopped-installation data, private configuration and profiles; add no database migration. Live account acceptance remains separate from package installation.

## 0.19.0

- Add copy and composer-edit controls to operator and agent messages, collapse detailed agent activity by default, and follow new content until the reader scrolls up.
- Add per-conversation Archive with existing Restore and recoverable Trash controls.
- Preserve long pasted messages up to the existing one-million-character API bound and keep the complete initial objective in later native team context. Configured finite context budgets and provider limits still apply.
- Add repository safeguards for credentials, personal runtime settings, database exports, local project records and repository clones. Keep source configuration examples, migrations and synthetic tests versioned.

## 0.18.3

- Allow verified replacement extensions to complete after canceled prelaunch work or a completed candidate whose installation proposals were rejected. Preserve the original failed/rejected history.
- Keep completion blocked for active or unknown coding, unapproved activation, queued installation, and mismatched project/task/capability checkpoints.

## 0.18.2

- Index approved and restored facts atomically with publication in configured hybrid memory, so new knowledge is immediately searchable without a manual indexing command.
- Preserve pending review, prior facts and withdrawal state on local model failure; retain lexical behavior, explicit model-change backfills and project/approval boundaries.

## 0.18.1

- Preserve exact whitespace in repository citations while retaining content and byte limits, so reviewed code excerpts can enter company knowledge.
- Read approved activation records with a bounded streaming parser; a growing approval history no longer blocks an otherwise valid installation.

## 0.18.0

- Add bounded exact-commit tree pages and character-offset file ranges for saved local and GitHub sources, retaining project, profile, source, protected-path, and secret checks.
- Allow granted team repository and verified artifact reads under the existing repository gate, and static browser fixtures under the existing browser gate. Generic live-text reads and GitHub PR tools remain excluded.
- Normalize exported source directory permissions for non-root images without changing private worker storage. Retain immutable verified package records when preparing a corrected package for the same candidate.

## 0.17.5

- Capture complete source-safety evidence using the approved per-file context bound. Small valid changes to large README and lockfiles no longer fail against the unrelated 64 KB command-output default. Preserve exact leading and trailing whitespace.
- Retain default command, patch, scope, secret, and total-observation bounds; no new permission or automatic retry is introduced.

## 0.17.4

- Return known prelaunch coding rejections to Chief as blocked assignments, with one durable failure cause and no automatic redispatch. Existing or uncertain coding attempts remain protected.
- Preserve exact manifest-size errors so the next approved task can be made smaller. This recovery patch does not complete the pending native readers or repository-to-memory task.

## 0.17.3

- Retain exact scoped failed patches and sanitized behavior-check evidence so repairs can use actual assertions. Failed evidence cannot authorize QA acceptance or installation.
- Parse complete provider SSE framing and preserve safe transport failure categories. Retry only an explicit undelivered overload rejection, within configured request and retry bounds.
- Preserve all existing application features and the pending native reader acceptance gates.

## 0.17.2

- Keep completed coding-session evidence when scoped candidate validation rejects output, with bounded provider diagnostics and validated session metadata.
- Carry verified repository identities into task continuation and resolve approved maintenance sources beyond the displayed repository page. Show separate coding activity in conversation status.
- Allow bounded implementation turns of up to 80 provider requests by default, preserving explicit configured limits and existing wall, output, and isolation boundaries.
- Recognize explicit streamed provider-overload rejections. Retain the existing single delayed retry and prohibit automatic replay after tool actions.
- This recovery release does not mark the pending native reader build or original repository-to-memory task complete.

## 0.17.1

- Preserve coding terminal evidence when commands produce large output, and retain typed failure reasons without recording private command contents.
- Continue the original task once after a maintenance-scope decision. Let the operator explicitly discard a stopped, unverified disposable build while preserving its unknown receipt; a verified replacement can then satisfy the original capability checkpoint.
- Explain the same-run search prerequisite when a public-page fetch is denied.
- Dispatch each native role tool at its registered version, return known read failures for recovery, and repair the persistent video worker’s default queue poll.
- Preserve the Zoho 0.17.0 sign-in release and earlier source-staging/worker fixes. Full self-extension acceptance remains separately tracked.

## 0.17.0

- Add a visible Connect to Zoho button using the operator's existing Zoho account, discovered public OAuth registration, PKCE and a local callback. Normal setup no longer asks the operator to obtain an access token.
- Store sign-in and refresh credentials in the protected local vault; reject expired, replayed or changed sign-ins. Migration 0084 separates credential renewal from mailbox approval identity. Automatic renewal leaves exact mail approval requirements in place.
- Preserve manual credential setup under Advanced connection settings. A successful sign-in checks account metadata and keeps mailbox access off until the operator enables it.


## 0.16.0

- Let Chief prepare task-linked tool grants and named read-only repository mounts. Approved setup is applied by its existing owner, verified by a real committed-file read, and followed by one task continuation.
- Read registered GitHub files through the existing account and HTTPS owners at their saved commits. Capability inspection checks the intended specialist and points to related registered tools before commissioning duplicate functionality.
- Support an existing Codex subscription in the separate isolated worker without changing local Codex authentication. Freeze the Engineer's reasoning setting with the approved coding target.
- Add separate release preparation with candidate/QA identity checks, fresh worker readiness, interrupted-build receipts, and installation checkpoints covering private configuration and host worker evidence.
- Forward the video renderer's typed settings into installed Compose and initialize its private asset volume, so configured editing is no longer silently disabled in the app container.
- Preserve the Zoho Mail baseline and existing approval boundaries. Installation and the live self-extension acceptance exercise are recorded separately in `PROGRESS.md`.
- Approved activation advances the explicitly configured clean maintenance clone with the release; failed readback restores its previous revision.

## 0.15.0

- Add a project-scoped Zoho-hosted Mail connection with protected local credentials, bounded reads/search and native draft/send/reply proposals through the Tool Gateway. Save setup without credentials; access starts off and agent profiles require explicit tool grants.
- Require exact approval for every mailbox write, preserve native draft mode and record one-attempt receipts without replaying ambiguous outcomes. Migration 0082 adds connection/action rows with immutable approved proposal content.
- Add the Zoho setup screen and explicit draft/save versus send approval controls. OAuth token entry and renewal are manual in this release; installation creates no mailbox authorization and sends no test email.

## 0.14.0

- Add linked, task-bound self-extension through the separate isolated Codex runner, exact independent QA, concrete activation approvals, maintenance drain, rollback/readback and one checkpoint resumption. Keep source merging, package preparation and installed activation under existing operator authority.
- Add source-cited company knowledge proposals through existing capture review and approved-fact publication, plus readable improvement progress in conversation Details.
- Add scoped capability preflight to Chief project inspection and task Execution status details. Retain every observed blocker, check committed source identity through the existing reader, and separate setup/access failures from missing implementation.
- Enable the core team worker, Researcher public-web policy and operator-selected local recording by default. Preserve explicit false overrides, tool grants, source scopes, required search/renderer connections and outbound approvals.
- Default native team repository reading and external coding policy to enabled; retain explicit disable, source/profile scopes, runner readiness and approval checks. Use the existing dedicated container source directory without adding host mounts.
- Add a task-driven self-extension design covering capability diagnosis, Engineer/Codex work, independent QA, approved activation and restart-safe continuation.

## 0.13.0

- Open directly into a focused conversation composer with Chief selected. Replace the ambiguous Talk to Chief heading with an agent dropdown, including custom project profiles.
- Start direct agent conversations from the first message using the existing SDK worker, selected profile model/tools/memory, exact replay and saved history. Switching agents opens a new conversation and preserves an unsent message. Chief remains the default coordinator; direct chats retain operator review and outbound/unknown-outcome holds.

## 0.12.0

- Keep the conversation sidebar visible throughout Settings. Return to the selected conversation with one button, preserving an unsent message.
- Show archived conversations as saved history. Delete an archived conversation into recoverable Trash; restoring it keeps its work paused and returns it to Archived. Migration 0080 retains work orders, files, messages and events.

## 0.11.1

- Use one global semantic palette across Chief, Settings, forms, agent panels, dialogs and historical activity. Remove page-specific colors, stale color fallbacks and competing palette definitions.
- Replace the crowded Settings tab strip with a labeled page selector that fits desktop and mobile widths while preserving all available destinations and deep links.
- Add regression checks for palette ownership, complete theme roles and Settings navigation.

## 0.11.0

- Open directly into one Chief conversation per operator objective. Keep delegated agents, public context and files in a side panel; move configuration and historical records behind Settings.
- Start from the first message and continue stopped jobs through the same chat, with atomic persistence, exact request replay and preserved results. Unknown outcomes and pending approvals allow inspection-only discussion without replaying effects.
- Give Chief bounded recovery of failed unaccepted assignments. Complete new local jobs only after accepted work and independent QA; preserve existing operator review and outbound approval contracts.
- Add reversible rename, future-turn pause, archive and restore controls in schema 0079. Archive preserves execution and history; pause lets the current bounded turn finish.
- Keep legacy text startup reconciliation from settling team-owned runs. Save source receipts when attached text files enter a team turn.
- Preserve message identity across reconnects and reloads. Keep stopped, unverified and unavailable-worker states readable after reopening a conversation.
- Classify rejected SDK model actions as known failures when no uncertain tool effect exists, so ordinary chat continuation remains available.

## 0.10.10

- Give the canonical Chief read-only project, work-order tree and saved agent-session inspection, including nested handoffs, failures and closed branches. Keep reads available when work-order status changes while preserving action gates and project isolation.
- Add exact paged inspection receipts with a fixed record cutoff and change detection, and include nested team progress in project-inbox and work-order Chief context. Schema 0078 expands only admitted receipt names; checkpoint restore preserves rollback history.
- Add an explicit default-off native team repository-read gate; approved Engineer/QA reads use the existing exact committed-source owner while legacy text restrictions stay intact.

- Report pending, blocked and canceled assignment reads with saved state and next steps instead of a missing-target failure. Return exact paged QA rework feedback without treating rejection as an accepted result.
- Explain sequential team turns and declared input scope so the Chief yields to specialists and provides every required finding to a review.
- Reject dependencies on blocked or canceled assignments with a saved reason and actionable correction, preventing rework that would wait forever on rejected QA.
- Retain explicitly approved read-only repository mounts across installed lifecycle commands, updates and rollback, with strict private configuration validation.

## 0.10.9

- Add an explicit Start work action that admits a Chief draft, preserves its chat, saves the current message and queues the existing team workflow in one transaction, with exact-request recovery and replay.
- Make ordinary Task Send discussion-only, while retaining the older action API for compatibility.
- Show rejected agent actions as rejected, retain safe field diagnostics and report registered repository metadata and configured execution limits without granting tools.

## 0.10.8

- Remove the default SDK model-call cutoff for native tasks; retain explicitly configured finite limits, timeouts, cancellation, usage policy and approvals.
- Report large saved artifacts with exact owner-read content, without applying the text-message limit to the generated artifact envelope; clarify concise summary guidance.
- Show durable native tool activity and verified saved-video previews with an expanded full-frame player.
- Return completed child work for a new idempotent revision while retaining earlier messages, results and artifacts.
- Add bounded local video editing recipes and immutable render receipts through the Tool Gateway and separate isolated renderer; activation remains off by default.
- Package the reviewed, separately launched read-only mail MCP adapter and its strict scoped contracts; the update does not activate a mail connection.

## 0.10.7

- Remove the default native prompt-size cap; save unlimited policies as null and show the effective limit. Preserve provider, spending, cancellation and turn/time limits.
- Keep large confirmed team results intact and page declared dependency content within existing context bounds.
- Page project agent metadata so the Chief can find profiles beyond the initial roster window.
- Report required-context budget failures before a provider call with an actionable saved reason.

## 0.10.6

- Discover the current Codex account catalog with client compatibility version 0.160.0; retain configured overrides and provider-reported model availability.
- Show redacted model-discovery errors and reload unavailable saved models after failed discovery.

## 0.10.5

- Refresh Task and Manage work-order status/version when saved execution readiness changes, so completion and cancellation expose the current state and review controls automatically.

## 0.10.4

- Continue default Task chats beyond the history window; page older saved turns and retain exact submission recovery.
- Let completed team work release assignment capacity and new confirmed evidence reset the Chief no-progress window.
- Wake the Chief for successive batches within the same plan while retaining per-attempt, depth, approval and no-progress safeguards.

## 0.10.3 — continuing long team sessions

- Keep the latest saved messages, attempts, causes and actions in the bounded Task read window so long goals retain their current sequence and execution state.
- Keep unresolved outcomes and action receipts visible outside that window; explain when earlier history is omitted from the display.
- Verify a deterministic goal across 109 SDK attempts with restart, artifact handoff, exact-byte QA and operator acceptance. Per-attempt timeouts, loop bounds, automatic-work limits and approvals remain enforced.

## 0.10.2 — Chief wakeup after canceled rework

- Wake the Chief after every remaining assignment has confirmed evidence, including when a canceled branch shares the same plan revision.
- Record the ready cause when canceling the last unfinished branch; empty or wholly canceled plans still require operator input.
- Preserve blocked/unfinished evidence gates, independent QA, approval boundaries and existing team limits.

## 0.10.1 — scoped team result contracts

- Advertise result fields for the assignment's evidence kind: text, saved artifact or independent QA. Preserve exact artifact, dependency and role checks.
- Return a safe correction step for incompatible result fields, including saved failed-call replay, so the bounded SDK can correct a rejected input within its existing turn.
- Preserve interrupted unknown history; continuation still requires reconciliation and an explicit acknowledgement before a separate attempt.

## 0.10.0 — team workflow and optional local video

- Connect explicit SDK dispatch, owned assignments/results, dependencies, independent QA and operator acceptance. Preserve legacy/unknown history and use canonical Chief identity for historical custom profiles.
- Add opt-in operator-selected window/tab recording, microphone narration, one-clip trim/caption and exact video playback receipts. Autonomous recording and AI voice generation are not implemented.
- Follow selected-capture migration 0073 with team workflow migration 0074; preserve existing memory identities and capture grants. Worker and media remain off by default.

## 0.9.0 — selected company memory capture

- Add separately scoped API-key capture with an exact selected-content preview, bounded expiry, visible-source retention notice and pending local operator review. Existing reader keys remain read-only.
- Reuse the authoritative PostgreSQL source/candidate/fact lifecycle through a private draft-only persistence identity. Reject publication, review, raw transcript reads and irreversible deletion from the plugin.
- Preserve the English Nomic hybrid retrieval configuration and current fact provenance. Capture access never permits ambient or whole-chat import.

## 0.8.0 — local semantic company memory

- Add opt-in hybrid exact-word and vector search using a checksum-pinned local Nomic English v1.5 model through DistrictAI's native embedding runtime protocol. Reuse the existing PostgreSQL store; vectors are derived from approved current facts, never transcript archives.
- Preserve project/company grants and fact provenance. Withdrawn and superseded facts stay out of retrieval even when their old vectors remain. Indexing rechecks facts after inference, and missing indexes, changed models or unavailable inference fail explicitly.
- Add a bounded operator indexing command and semantic context for internal agents. Keep the original lexical role tool contract and add `approved_memory_v2` for configured semantic reads. Defaults remain lexical until a separately approved rollout enables local embeddings.

## 0.7.0 — shared company memory

- Add explicitly authorized conversation sources and operator-reviewed correction proposals. Superseded and withdrawn facts retain provenance and stay out of current retrieval. Withdrawal is reversible.
- Give internal team turns bounded approved company facts with saved attempt context. Add a private scoped MCP companion and Codex plugin for search/read over the same Postgres store. Local Codex uses a private API key and stdio bridge; separately configured cloud clients can use OAuth.
- Apply the normal schema upgrade from 0059 through 0071, including team sessions, coordination rule limits, connector sign-out and document actions. Existing unknown runs are retained without replay.

## 0.6.4 — task chat agent updates and provider deadline

- Show saved, provider-authored public progress summaries during Codex account Task chat turns and keep them visible with the final reply. Raw reasoning and incomplete fragments remain private.
- Poll exact saved turn state while Send is in progress, and explain an unknown provider outcome in plain language without suggesting a replay.
- Give Task chat its own configured 120-second provider deadline, separate from the 30-second live-text default. The deadline is an upper bound, not a send delay.

## 0.6.3 — saved team work and task work logs

- Add a Team tab to each task showing linked agent assignments, their saved chats, and recorded runs, with an explicit empty state when the Chief only proposed a handoff.
- Add an expandable work log to each task-chat reply showing saved inputs and project actions, with clear limits when no intermediate agent update was recorded.
- Document a bounded, opt-in contract for future direct agent messages. This release does not enable agent-to-agent messaging or change the database schema.

## 0.6.2 — clearer task chat send and assignment feedback

- Show an outgoing task message immediately while the agent replies; offer an exact retry only when a submission remains unconfirmed after the request stops.
- Give the Chief the exact team-assignment fields and show failed proposals as readable replies with a clear notice that no assignment was saved. Historical malformed proposals keep their raw record under Reply details.

## 0.6.1 — visible Discord server setup

- Show the Discord server configuration fields by default when a project has no saved route, while retaining personal DM mode for existing DM bindings.
- Add setup guidance for the application, bot token, Discord IDs, server invite, and local confirmation steps.

## 0.6.0 — Discord server channel

- Remove the duplicate Chief of Staff web composer from Settings → Messaging; web chat remains in Work → Task workspace.
- Add one mention-gated Discord server text channel per project, bound to an application, server, channel, and operator user ID. Preserve personal DM mode and explicit local identity confirmation.
- Add migration `0059_discord_guild_channel` without enabling or changing existing channel bindings.

## 0.5.0 — project Chief of Staff inbox

- Let the operator message the project Chief of Staff without selecting a work order or configuring a remote channel.
- Route verified personal Discord and WeChat DMs into the same project conversation. The Chief can discuss an existing order, create a new or linked follow-up order, and create an Automation rule under existing project policy.
- Add migration `0058_project_chief_inbox` while preserving channel identities and historical message receipts. Slack and Telegram are not connected by this release.

## 0.3.0 — connected operator workspace

- Group the application into Home, Work, Inbox, Team, Knowledge, and Settings with connected task navigation and responsive layouts.
- Add local layout customization, independently scrolling panels, readable capability selection, and a focused work-creation dialog with visible actions.
- Remove internal identifier/hash metadata from presentation and retire the superseded Tools page and obsolete styles.
- Automatically send manually saved results for memory review while keeping publication approval explicit.
- Preserve existing data, credentials, approvals, and configuration. The database remains at `0052_outbound_smtp`; unrelated agent-automation development is excluded.

## 0.2.1 — approval-bound outgoing email

- Add project-scoped SMTP account setup with verified TLS, write-only credentials, and an explicit on/off control separate from incoming IMAP.
- Let operators and agents propose exact plain-text emails. Each proposed message waits for an individual operator decision; approval makes one SMTP submission attempt, and ambiguous outcomes are never retried automatically.
- Record the exact recipient, sender, subject, body digest, approval, run, attempt, and SMTP acceptance state. SMTP acceptance does not confirm recipient delivery.
- Add additive migration `0052_outbound_smtp`. No SMTP account is configured or enabled by an update. Batch approvals remain future work.

## 0.2.0 — project automation and mail connections

- Add per-project Automation with internal signals, scheduled rules, bounded triage, and approval-bound agent proposals.
- Add read-only IMAP mailbox setup and Gmail/Microsoft mailbox connections. Mail, connected accounts, and watched folders have project-scoped controls and protected credentials.
- Add watched document imports and exact approval-bound new-file writes. Coordination defaults to `off`; no mailbox or cloud account is connected by upgrading.
- Migrate the installed schema from `0042_work_order_acceptance` through `0051_document_writes` while preserving existing project data.

## 0.1.4 — licensed, source-aligned localhost release

- Include the MIT license in the checksum-verified offline release bundle.
- Install the committed OneFoundry revision that contains the current product README and architecture documentation. Application behavior is unchanged from 0.1.3.

## 0.1.3 — align CLI configuration with runtime

- Reject runner timeouts above the app's 60-second settings limit before changing installed configuration.

## 0.1.2 — installed operator CLI

- Add an executable `onefoundry` command with version, health, doctor, logs, backup, restart, and safe non-secret configuration commands.
- Keep prior release bundles readable by the new updater; retain the local Infra status feed across updates.
- Avoid an extra QA draft readback while the run list is still loading.

## 0.1.1 — committed localhost application update

- Include the committed Task and Activity workspace UI and work-order acceptance flow.
- Package the current application and schema as a prebuilt release for the localhost stack.

## 0.1.0 — initial versioned release procedure

- Establish an independent OneFoundry product version shared by the Python and frontend packages.
- Add a prebuilt Docker image bundle with an external SHA-256 checksum and exact source commit.
- Add local and Linux server install, update, status, and data-backed rollback commands.

This entry documents the release tooling. Version 0.1.0 has not been published or deployed by this change.
