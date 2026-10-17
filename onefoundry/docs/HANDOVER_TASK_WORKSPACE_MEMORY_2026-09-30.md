# Task Workspace, skills, and memory handover

## Original assignment

Finish and independently verify multiple chats per work order and the Skill → result → DreamCycle candidate → approved memory path. Keep this work separate from the Codex CLI and Claude Code execution work in the current session. Preserve the shared dirty checkout and do not edit `/home/ken/districtAI`.

## Current live evidence (2026-09-30)

- The active app is `http://127.0.0.1:8765/`; `/health/ready` reports ready. Compare its image, migrations, OpenAPI, and browser behavior with the checkout before making a live claim.
- Use the existing **Workflow Audit 2026-09-30** project (`0d63e578-51f8-4dda-9db7-ca10f7b5dd32`). Leave the DistrictAI project untouched. The audit work order **Microcheck live checklist** is `353facc2-0066-426e-a4e4-b2d664337649`.
- The earlier audit found `POST .../conversations` unavailable in an older live image. This is no longer the current result: the active OpenAPI exposes `GET` and `POST`. **Audit follow-up chat** was created through the live API and now contains a confirmed Codex text turn. **Main chat** retains its separate earlier turn. Both persisted after page reload and switched correctly at desktop and 390 px. A disposable PostgreSQL test inspected the second provider prompt and found no first-chat transcript; the live provider prompt itself was not retained for inspection.
- **Tiny Change Review v1** is pinned to the custom Audit Writer. A real `gpt-6-sol` Codex text run saved the exact skill snapshot and cited the approved project memory item. This proves text inference and prompt input selection for that work order; it does not prove an Engineer coding CLI session or live tool invocation.
- The manual Engineer → QA handoff produced an operator-entered result. Its DreamCycle candidate was absent from memory while pending and appeared in project memory only after approval. Cross-project retrieval returned zero matches. The DreamCycle path here is deterministic and operator-reviewed, not an autonomous dream loop.
- The Task Workspace now shows Conversation, Activity, Results, and Details tabs. The saved text turn correctly labels its action disclosure **Unavailable**; no provider-internal tool trace was captured. Agent-authored plan/finding/next-step loops and complete coding action traces still need their own evidence and acceptance.

## Source owners and existing tests

- Chat API and persistence: `src/onefoundry/task_conversation_api.py`, `task_conversation_store.py`, `task_conversation_schemas.py`; UI: `frontend/src/TaskChatView.tsx`, `frontend/src/conversationApi.ts`.
- Skill selection and snapshots: `src/onefoundry/skill_api.py`, `tool_gateway.py`, `live_text_store.py`; memory review: `memory_api.py`, `memory_policy.py`, `tool_gateway.py`.
- Work order presentation and activity: `frontend/src/WorkOrdersView.tsx`, `WorkOrderEvidenceView.tsx`, `workOrderActivity.ts`, `workOrderTrace.ts`.
- Relevant tests: `tests/test_task_conversation_postgres.py`, `test_skill_library_postgres.py`, `test_memory_postgres.py`, `test_live_text_postgres.py`; `frontend/tests/taskChat.test.tsx`, `workOrderEvidence.test.tsx`, `workOrderActivity.test.ts`, `workOrderTrace.test.ts`.
- `docs/WORK_ORDER_TASK_WORKSPACE_DESIGN.md` holds the product design and now reflects the installed app's named-chat and tab behavior.

## Verification update (2026-09-30)

- The installed app is ready, its database is at `0041_external_action_trace`, and its chat API/store files match this checkout by SHA-256. Two saved chats each have one completed turn. Repeating the existing create and send identities returned 200; stale sequence and inactive-chat sends returned 409; a cross-project chat read returned 404 and cross-project memory search returned no match.
- Tiny Change Review v1 has the same SHA-256 in the saved skill version, live-text run snapshot, and both chat turns. The approved project memory item links to its source result and candidate. Disposable PostgreSQL tests verified pending and rejected candidates remain outside retrieval, project/company scope, handoff history, and unknown-send handling. No new candidate was created in the installed database.
- Desktop browser checks covered queue, blocked work order, chat switching, all four tabs, and collapsed technical details. At 390 px, the queue, chat switching, and all task tabs remained usable with document width 390 px. The two console errors were a missing favicon and an expected 404 for a QA chat that had not been opened; there was no page script error.
- Focused tests passed: 50 PostgreSQL tests and 20 frontend tests. The full `make check` gate was not rerun because this verification changed documentation only. Live coding-provider progress, complete ordered coding action receipts, and a browser example of an unknown chat outcome remain unaccepted; text replies correctly disclose action coverage as unavailable.

## Acceptance to complete

1. Two named chats under one work order each retain their own turns after refresh; the second chat's provider prompt excludes the first chat transcript. Check idempotent create/send, stale sequence, unknown outcome, handoff history, and project isolation against PostgreSQL and the live API.
2. Verify the pinned skill version and SHA in the saved run and turn, then verify pending and rejected DreamCycle candidates never enter retrieval. An approved item must retain its result and candidate lineage and obey project/company scope.
3. Browser-check the work order queue and Task Workspace at desktop and 390 px: chat switching, Activity/Results/Details, blocked/unknown states, and collapsed technical evidence. Keep ordinary content in plain English; IDs and event JSON belong in technical details.
4. Distinguish agent-authored progress from system activity and owner-confirmed results. Never show private reasoning as chain of thought or claim complete tool coverage when the provider only returned final text. For coding turns, require saved ordered action receipts before marking coverage complete.
5. Run focused tests and the repository-defined regression gates for any source change. Report active-container acceptance separately from checkout tests. Do not restart, migrate, commit, or deploy without the owner's explicit instruction.

## Suggested first steps

Read the current `AGENTS.md` and the design file. Inspect the shared dirty diff before editing. Reconfirm the active endpoint and route schema, then use the audit project for reversible checks. Reproduce a specific remaining defect or failed acceptance gate before changing source. Coordinate any overlapping edits to `TaskChatView.tsx` or `task_conversation_store.py` with the primary session.
