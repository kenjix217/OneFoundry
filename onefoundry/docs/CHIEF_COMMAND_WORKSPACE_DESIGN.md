# Chief command workspace

Approved design, implemented and installed locally as 0.11.0 on 2026-10-07.
The initial sample remains at `output/chief-command-redesign-20261007/index.html`.
Implementation and qualification receipts are in `output/chief-command-implementation-20261007/`.
Installed update, preservation, rollback and browser receipts are in
`output/chief-command-install-20261007/`; see `PROGRESS.md` for the exact source commit and checks.

## Product contract

The operator talks to Chief. Chief owns planning, delegation, specialist recovery,
QA corrections, integration and the result. The operator manages company direction,
not an internal queue of agent assignments. A conversation remains usable when work stops.

Chief finishes verified internal work under the approved autonomy policy. The operator
can inspect or request changes at any time. Routine use of configured models/tools within
approved budgets does not require another permission request for each step. Outbound
communications, publishing, merging, deploying, new spending commitments and broader access
retain their exact approval contracts. Those requests appear
inside the conversation with the proposed action and one useful decision.

Chief must not ask the operator to select specialists, mark delegated drafts ready,
read every agent result, restart each failed child or approve routine QA corrections.
Those are Chief's responsibilities. A real missing account, tool or execution capability
requires one precise setup request after Chief has completed the work already possible.

## Observed 0.10.9 problem

Read-only installed evidence: the DistrictAI project has 33 work orders: 8 top-level
jobs and 25 delegated steps. All 6 failed orders are delegated steps. Of 12 orders
awaiting review, only 2 are top-level jobs. The current Home/Work views promote all
of these into the operator's queue and repeat the same entries across panels.

The owning contract mismatch is established with greater than 95% confidence:

- `WorkOrdersView.tsx` flattens every order and classifies all failed, blocked,
  unknown and needs-review states as "Needs me". It also labels drafts "Ready".
- `HomeView.tsx` repeats that same classification in both attention and recent work.
- A QA `needs_rework` verdict in `task_session_results.py` marks the specialist
  orders failed even though it is a quality decision with saved feedback.
- `task_session_store.py` refuses messages on failed/review/blocked/unknown orders.
  `WorkOrderReviewActions.tsx` renders nothing for failed orders; the Gateway only
  reopens done orders. Consequently "Manage" offers no failed-order recovery.
- Generic "Working"/"review its result" copy does not establish that any worker
  is executing or that the operator has an actionable decision.

This is a hierarchy, authority and recovery defect, beyond visual density. Real
provider/capability problems may also stop work; moving them to a drawer does not fix them.

## One workspace

- Open Home directly into Chief. A new job starts from the first message; no role,
  draft, mark-ready or execution-mode form appears in the ordinary flow.
- The left list contains operator conversations for top-level jobs. Each has one
  readable title and at most one useful state. Show archived conversations on request.
- The centre is the Chief conversation with one anchored composer. Progress updates,
  deliverables and genuine operator questions appear here once.
- A failed attempt does not disable the composer. "Continue from where we left off"
  is an ordinary message in the same conversation, with its previous work available.
- Click an agent's name to open a side panel showing assignment, supplied context,
  public conversation and files. On phones this becomes a detail page with a clear Back.
  There is no default specialist composer or specialist-management burden.
- Put earlier attempts and technical receipts behind Details. Never show raw IDs,
  entire assignment instructions or private model reasoning in the normal task list.
- Replace Manage with real optional actions: rename, pause safely, archive/restore
  the conversation, and view history. Archive preserves all work and does not cancel it.
- Remove duplicate Home panels, status-count filter bars, Now/Next boilerplate,
  separate ordinary Work/Task/Activity navigation, and default execution diagnostics.
  Configuration and diagnostics remain reachable through Settings/Details.

## Chief's responsibility

| Situation | Chief's work | Operator experience |
| --- | --- | --- |
| New request | Save one root objective; plan and delegate through the existing Gateway/SDK | Send a message |
| Specialist transient failure | Retry safely within configured bounds; retain receipts | A brief update when useful |
| QA requests changes | Read exact feedback; reuse confirmed findings; revise and request fresh QA | Chief is handling it |
| A task stopped | Inspect saved work and uncertainty; queue a safe new Chief turn and coordinate the remaining work | Message Chief in the same conversation |
| Tool/account/source access unavailable | Establish the precise missing capability; request the smallest necessary setup action | One clear explanation and an actionable request |
| External effect may already have happened | Read/reconcile exact receipts before considering a new attempt | Chief checks first; a specific confirmation only if the ambiguity cannot be resolved |
| Local deliverable verified | Assemble the result and complete under approved Chief autonomy | A result in the conversation; request changes normally |
| Publication or other outbound action | Prepare the exact reviewable action; wait for its existing approval | Approve or decline inline |
| Work no longer wanted | Pause future execution safely; optionally archive its conversation | One optional action; history remains |

Retries cannot create endless draft branches or silently duplicate unknown effects.
Use the existing configured limits and the provider/execution owners. Chief must
report an unavailable capability plainly; the UI must not substitute optimistic prose.

## Required implementation owners

1. **Root conversation projection:** a project-scoped owner returns each root once,
   derived active/waiting/recovery/approval/completion state and the actual next action.
   Bind orphan/legacy work explicitly. Do not silently hide unlinked work or remove history.
   Chief's records include every nested branch; they are not counted as operator jobs.
2. **Chief message/recovery owner:** extend the existing Gateway/session boundary with
   one idempotent message operation for start, ordinary continuation and safe recovery.
   Validate canonical Chief/root identity, expected versions, project scope, worker/model
   readiness and all active/unknown effects. Reuse start, message, reconciliation and
   assignment owners rather than building another agent loop or guessing from text keywords.
   A typed disposition says what was saved, queued, awaiting setup or unresolved.
3. **Recovery lifecycle:** known failed internal work can receive a new attempt in the
   same root/session. Preserve accepted inputs, historical attempts and QA decisions.
   Unknown writes, pending approvals and provider effects require exact reconciliation.
   Do not reset old evidence to success, cancel active effects blindly or restart all branches.
4. **Chief local completion:** define an explicit internal-completion policy through the
   existing profile/autonomy owner. New local jobs should not demand manual review of every
   specialist. Existing `operator_review` and outbound-action contracts are not silently
   reclassified. Completion requires verified deliverables/checks and required independent QA.
5. **Archive:** add project/operator-scoped archive metadata through the existing order owner
   if no suitable contract exists. It is reversible visibility, distinct from execution state.
   Do not implement persisted archive by changing historical failure/outcome records.
6. **Frontend:** make the root Chief conversation the default Home/Work surface, route delegated
   links to its agent drawer, and keep browser Back/deep links compatible. A delivery click opens
   its verified file preview. The composer remains a place to give direction and discuss a blocker.

The already prepared 0.10.10 Chief inspection tools support saved-record visibility.
They do not implement these recovery/completion contracts. Source-access activation
and the supported DistrictAI coding target remain separate live-capability requirements.

## Preview verification

The standalone sample preview was checked in a real browser at 1280×900 and 390×844:
ordinary message continuation, first-message creation, search, rename, pause,
archive/restore with preserved history, earlier attempts, agent context/public messages,
document previews and inline keep-as-draft. The mobile panel supports keyboard tab cycling,
focus containment and Escape/return to Chief. Both widths have one composer, one agent
link row, no page-width overflow and no browser console errors.

The sample download button was clicked, but browser automation timed out while waiting
for its download event; downloaded bytes are unverified. The preview uses in-memory sample
records: refresh resets it. It cannot demonstrate live dispatch, persisted recovery,
Chief-owned completion, archive persistence or an actual provider/coding connection.
The implementation subsequently passed full regression, real Gateway/SDK/PostgreSQL
checks with scripted models, and exact packaged update/checkpoint-restore qualification.
The installed 0.11.0 app passed preservation, health and desktop/mobile browser checks.
Live/paid provider calls and external coding launches remain unexercised; the redesigned
outbound approval buttons have component/owner coverage but were not clicked in the browser.
The original design-only slice did not change the product release version.

## Acceptance

- A new first message produces one visible root and a real queued Chief attempt, with
  exact idempotent readback. Send/reconnect/double-click cannot create duplicate work.
- Start/continue/recovery work from the same composer for draft, paused, failed and review
  cases. Known failure and unresolved external effects have distinct safe dispositions.
- QA rejection leads to Chief-owned revision/fresh review without operator child management.
- Existing 33 records appear as 8 top-level conversations and linked nested history;
  every old message, result, approval and receipt is still reachable.
- Only genuine operator decisions appear as attention requests. Working requires real
  active execution; an idle running database row alone is insufficient.
- Verified local completion follows its approved autonomy contract; outbound and explicitly
  requested operator-review jobs still require their existing owner decisions.
- Archive/restore preserve history and leave execution authority/effects unchanged.
- Test project isolation, input validation, cancellation/unknown effects, concurrency,
  duplicate submission and stale versions against PostgreSQL and real Gateway/SDK workers.
- Verify readable Chief chat, agent context/conversation/files, navigation and recovery at
  desktop/mobile widths. Run full regression and exact installed acceptance before live claims.
