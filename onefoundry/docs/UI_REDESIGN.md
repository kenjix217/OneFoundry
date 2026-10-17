# Operator workspace redesign

Implemented in the React application and packaged in OneFoundry 0.3.0. Installations use the existing release updater and rollback checkpoints.

## Navigation and ownership

| Area | Operator workflow | Presentation owner |
| --- | --- | --- |
| Home | Attention, recent work, team, direct creation and setup links | `HomeView.tsx` |
| Work | Queue → task → conversation / progress / results / assignment; automation rules, events and limits | `WorkOrdersView.tsx`, `TaskChatView.tsx`, `AutomationView.tsx` |
| Inbox | Review requested actions; follow-ups when enabled | `ApprovalInbox.tsx`, `OneFoundryOperatorInboxView.tsx` |
| Team | Agents, reporting structure, readable capability choices | `TeamView.tsx`, `ProfileEditor.tsx`, `ProfileCapabilities.tsx` |
| Knowledge | Browse/review memory, add/import results, reusable procedures | `MemoryView.tsx`, `SkillLibrary.tsx` |
| Settings | Project setup, AI accounts, mail/files, repositories, coding agents, messaging, health | Existing domain views; `ConnectionsView.tsx` groups mail/files |

`workspaceNavigation.ts` owns grouping and labels. `workNavigation.ts` preserves existing work/task/activity links and browser history. The retired Tools route redirects to Repositories. Unavailable project links return to Home with an explanation. Optional business pages keep their existing feature gates.

Chrome stays fixed. Home, task conversation/context, activity list/detail, setup navigation/editor, model connection/catalog, memory browse/review and approval list/form own their scroll regions. Mobile navigation collapses behind a menu; long tabs scroll horizontally. Compact/focus views retain task actions.

Customize controls themes, spacing, icon/label navigation, list widths and Home panel order/visibility. Preferences are browser-local, validated, recover from malformed storage and can be reset. No new server configuration is needed.

## Simpler workflows and setup

- Home opens the new-work dialog directly and sends an incomplete AI setup directly to AI Connections. Work creation has a dedicated dialog with an anchored action row, independently scrolling fields and optional acceptance checks. Opening it preserves the queue height and filters; Cancel/Escape restore the queue. The previous inline form and its competing height rules were removed.
- Capabilities come from the existing registered-tool owner. Operators select readable choices; saved permission values and enforcement are unchanged. Failed discovery preserves saved choices and offers retry.
- Saving a manual result also creates its pending memory review through existing APIs. Publication still requires an explicit review. If review fails, the confirmed result remains available; retry sends that result without creating another one.
- Existing starter profiles, repository discovery, relevant-procedure matching and sign-in completion remain with their current owners. Navigation and rendering do not launch providers, coding workers or outbound actions.
- Installation owns persistent storage, database/role provisioning, optional local-model service, repository mounts, worker availability and channel/automation enablement. Keep those in the existing installer/configuration system. The UI reads availability rather than asking for internal paths or flags in everyday work.
- Next installer work: offer optional repository-folder and worker choices once during installation and pass them to existing configuration owners. This UI change does **not** alter installer behavior or automatically enable network features. Provider consent, account secrets and external messaging identity confirmation remain explicit operator choices.

UI metadata uses names, dates, readable outcomes and availability. Internal IDs, SHA values, proposal/binding digests and raw event JSON remain in API/storage/log owners. Exact user content, outbound comment/email text, code patches and saved logs are not rewritten; external account identifiers needed for identity confirmation remain visible in setup.

## Retirement register

| Removed presentation | Replacement / retained behavior | Status |
| --- | --- | --- |
| `ToolsPluginsView.tsx` and its page test | Repository check moved to `GitHubSourceCheck.tsx` in Repositories; replacement tests cover explicit invocation, owner mismatch, stale replies and failures | Deleted |
| Inline work creation and competing `.work-create` styles | `WorkOrderCreateDialog.tsx` owns creation and attachment recovery; `workOrderCreate.css` bounds the dialog and anchors its actions | Deleted from queue |
| App's flat sidebar and duplicated inline team cards | Six-area navigation and `TeamView.tsx` | Deleted |
| Mail/account/folder sections inside Automation | Settings → Accounts & files; existing APIs and component owners retained | Removed from former owner |
| MCP/plugin “planned” cards and obsolete team hero decorations | No nonfunctional destination advertised | Deleted |
| Raw technical event disclosures and identifier metadata | Plain-language activity; original records remain available to backend/log owners | Deleted from presentation |
| Old nav, hero, Tools cards, unused task picker/setup styles and event-JSON styles | Active workspace/component styles | 131 obsolete selectors removed, plus obsolete period decoration |

`uiRetirement.test.ts` checks that all application TS/TSX modules are reachable from `main.tsx`, the retired page is absent, and hashes/internal identifiers are not directly rendered as interface metadata. Active shared domain components were retained; no compatibility API or archived Go code was deleted.

## Verification

See the dated entry in `PROGRESS.md` for final counts and limitations. Acceptance uses a disposable PostgreSQL database, current FastAPI source and the production frontend bundle, with synthetic local records and outbound workers disabled. It is not installed-runtime or live-provider acceptance.
