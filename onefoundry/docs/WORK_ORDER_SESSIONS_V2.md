# Work orders as continuing team sessions

Status: design decision, 2026-10-04. This supersedes the chat and delegation
model in `WORK_ORDER_TASK_WORKSPACE_DESIGN.md`,
`TEAM_WORK_VISIBILITY_DESIGN.md`, and
`AGENT_AUTOMATION_RULE_AUTHORING_PLAN.md` for work-order execution. The existing
app remains on the older path until the acceptance gates below pass.

The executable wake, context, recovery, and spending decisions are specified
in [WORK_ORDER_COORDINATION_EXECUTION.md](WORK_ORDER_COORDINATION_EXECUTION.md).

## Why this change is needed

The DistrictAI demo-video work order is `ready` with no child work orders. Its
Chief of Staff proposed four assignments, but the reply did not satisfy the
exact JSON action contract (`invalid_decision_format`); none was saved. A later
text-model turn ended with an unknown provider outcome. The current **Continue
conversation** control acknowledges that uncertain turn, but does not resume
execution or create assignments. Task chat makes one text-model call per Send,
records `tools_active: false`, and interprets a single JSON object after the
reply. A model answer can therefore sound like a plan while no agent or tool
actually runs. This is a workflow failure, not a reason to ask the operator to
repeat the same request.

## Operator contract

One work order is one durable, continuing team session. The operator can talk
to the owning agent, inspect what the team has done, resolve a failure, and
continue in the same work order. A failed or uncertain attempt does not close
the conversation or discard its confirmed work.

1. Creating or opening a work order shows its main conversation and current
   objective. **Start work** starts an attempt for the owning agent. Sending a
   message while the session is idle also starts an attempt. The API saves the
   message and returns immediately; the worker reports progress and results
   separately. A page reload does not change execution.
2. The owning agent uses admitted Tool Gateway actions to inspect records,
   delegate, consult another agent, request approval, and record a result. An
   action is shown as done only after a saved gateway or runner receipt. Prose
   such as “I assigned QA” never substitutes for an assignment receipt.
3. A delegation opens a child agent session under the same work order. The
   parent and child each keep their own conversation, actions, attempts, and
   evidence. A separate child work order is created only when the agent is
   accountable for a separate deliverable or acceptance check. A consultation
   needs no extra work order.
4. Child agents report results and blockers to the Chief by default. The
   operator can read every saved agent session, but this visibility does not
   add sibling transcripts to prompts or wake sibling models. The Chief owns
   coordination and may arrange a specific peer consultation when a real
   dependency requires it. Messages cannot grant tools or approve outbound
   effects.
5. A failed tool or provider attempt appears in the session with its safe
   diagnostic and last confirmed action. The agent may continue with another
   action or ask for help. The operator can send a new message or choose
   **Continue work**; both create a new attempt in the same session using
   confirmed history. Neither silently replays the failed attempt.
6. The work order completes only when its deliverable and acceptance checks
   have confirmed evidence or the operator explicitly accepts it. A final
   model reply alone is a conversation turn, not completion.

The ordinary view shows objective, current agent, public plan/update, observed
actions, result, blocker, and next step. Private model reasoning is never
requested or displayed. Technical IDs, hashes, prompt/usage metadata, and raw
receipts remain expandable.

## Ownership and persistent records

| Owner | Durable record | Meaning |
| --- | --- | --- |
| Work-order store | Existing work order and acceptance evidence | Objective, owner, deliverable, and final outcome. Existing IDs remain stable. |
| Session store | `task_sessions` | One main session plus linked child sessions; project, work order, parent, agent profile, status, and creation source. |
| Session store | `task_messages` | Ordered operator, agent, and system messages with sender, recipient, public text, source, and idempotency identity. Append only. |
| Session worker | `task_attempts` | One bounded provider or runner continuation; input message, profile/config snapshot, provider binding, state, start/end, failure, usage. An attempt can fail while its session stays open. |
| Tool Gateway | Existing action, event, approval, and artifact receipts plus a session/action link | Exact effects, authority, and readback. Gateway and runner remain the only event writers. |
| Execution Gateway | External coding attempt and provider session receipt | Engineer code work in the isolated runner, with provider resume only where verified. |

Every new row has project ID and operator/agent actor ID. Foreign keys bind a
child session, sender, recipient, attempt, and action to the same project and
work order. Messages and completed attempts are immutable. An idempotency key
prevents duplicate messages or launches after reconnect. The session worker
claims pending attempts transactionally and has one active attempt per
session; independent child sessions may run concurrently within configured
bounds.

The session state is `open`, `working`, `waiting_for_agent`,
`waiting_for_operator`, or `closed`. Attempt outcome is separately `queued`,
`running`, `done`, `failed`, or `unknown`. Work-order completion state stays
separate. An uncertain attempt never changes the whole work order to `unknown`
merely because one model request timed out. An uncertain external action still
requires its own readback or operator decision before that action is repeated.

## Execution contract

The browser sends `POST /work-orders/{id}/session/messages` with a client
message ID, expected session sequence, text, and attachment IDs. The server
atomically appends the message and queues one attempt, then returns `202` with
the saved IDs and current state. `GET /work-orders/{id}/session` returns the
main session, child-session index, and a paginated timeline. A cursor endpoint
returns new messages, public progress, actions, approvals, and outcomes for
live refresh. `POST /sessions/{session_id}/continue` queues a new attempt from
the last confirmed state, with an optional operator message. It is idempotent
and refuses to repeat an unresolved side-effecting action. Existing chat APIs
stay readable during migration.

The worker gives the agent a bounded context assembled from the objective,
the current session's confirmed messages, child results and messages addressed
to it, current work-order evidence, and approved memory. Omitted history stays
in the database and is visible to the operator; context truncation is recorded
and must not be mistaken for forgetting the work order. The agent receives
only currently admitted tools. Tool calls pass through the Tool Gateway with
run/session binding, current permissions, idempotency, bounds, and receipts.
Each agent runs on its own profile's model account, so agents can use
different providers and switch between turns. The installed OpenAI Agents SDK
owns the model/tool loop for every text provider (Responses for OpenAI and the
Codex account; Chat Completions for Anthropic, Gemini, Grok, Ollama, and
cataloged compatible providers). OneFoundry owns the work order, messages,
permission decisions, receipts, and recovery, and adds no loop of its own. The
isolated Codex app-server route remains for coding work and is not required
for text agents. (Revised 2026-10-04; this replaces a Codex-only route.)

`delegate_agent` opens a child session with agent, objective, expected result,
and optional separate deliverable. A child result is saved as evidence for the
Chief and any declared dependent task. The worker may save an attributed
question or blocker, but saving it alone does not start another agent. The
Chief decides when a new model turn is needed; the scheduler can release a
predeclared dependent task from a confirmed result without calling the Chief
model merely to forward data. Model turns still have configured time, cost,
depth, and concurrency bounds as safety rails.

### Agent communication and token use

The default is coordinator-led work: the Chief gives each child a bounded
assignment, the child works in its own context, and it returns a concise
result, artifact reference, or blocker. The operator sees the complete saved
sessions. Other agents see only their own messages and the specific result or
artifact they need. Reading or displaying another agent's log never triggers
inference. The Chief is woken at meaningful decision points, such as a blocker
that needs a new plan, a failed acceptance check, or final synthesis. Ordinary
status events and completed independent steps do not wake it.

When one child's output is a known input to another, the Chief records that
dependency in the assignment. The scheduler passes the confirmed output to
the dependent child's next natural turn. This transfer needs no model call or
chat exchange. If the dependency was unforeseen, the child first checks saved
evidence; otherwise it leaves a specific question for the Chief. The Chief
may route a targeted peer consultation when that is cheaper or clearer than
answering itself. A peer message is passive until that decision; it cannot
create a ping-pong loop. An explicitly approved consultation has one question
and one answer by default, with the answer delivered only to its requester.

The spending objective is completion cost, not merely staying under a ceiling.
Before starting another agent or consultation, compare its expected benefit
with the estimated effort to finish the remaining deliverable and QA. For an
illustrative $50 target and $100 ceiling, spending $50 on coordination before
production is finished is a planning failure even though the ceiling remains.
Keep a reserve for integration, revision, and QA; show target versus forecast
at Chief checkpoints only when a trustworthy cost source exists. The current
Codex account path records tokens but no verified billed USD cost; its dollar
total and dollar forecast must remain unknown. Show observed turns/tokens and
an explicitly labeled effort forecast for that path. If projected work exceeds
the target, the Chief narrows or sequences the plan before opening more agent
work. The ceiling is only a final safety stop. Record usage per child and
consultation so the operator can identify waste, but never use a token cap as
the primary remedy.

This choice follows the [OpenAI manager pattern](https://developers.openai.com/api/docs/guides/agents/orchestration),
which keeps one agent responsible for synthesis and uses specialists for
bounded tasks. [OpenAI's multi-agent guidance](https://developers.openai.com/api/docs/guides/responses-multi-agent)
warns that added subagents can raise token use on dependent work. Anthropic's
[research-system report](https://www.anthropic.com/engineering/multi-agent-research-system)
describes a lead agent collecting focused child results and reports high token
consumption in its own multi-agent research workload; its figures are not a
cost forecast for OneFoundry.

For the demo-video task, the Chief can assign Researcher a product review and
make GTM's script depend on its confirmed result. When Researcher finishes,
the scheduler supplies that result to GTM and starts the waiting script task;
neither a sibling chat nor a new Chief turn is needed. QA starts after a draft
exists. The Chief returns when QA finds an issue requiring a decision or the
deliverable is ready for final review. If Researcher is blocked by a question
only GTM can answer, it saves the question for the Chief to route at the next
decision point rather than waking GTM immediately.

The current subscription-Codex Task chat adapter supplies text and public
summaries but no Tool Gateway calls. It cannot be advertised as a working
autonomous Chief session. The local runner already implements `thread/start`,
`thread/resume`, and `turn/start` for Codex app-server in read-only mode, and
has an offline resume test. It does **not** yet expose OneFoundry Gateway
tools or prove a live resumed Chief turn. Before enabling the new path, prove
that a restricted, isolated Codex process can resume its saved thread and
invoke only an admitted Gateway tool over a scoped bridge. A Codex child agent
must map to a recorded OneFoundry child session before its work is shown as a
team assignment. The existing coding provider remains in the separate
isolated runner. Capability is checked at Start and shown in plain language.
Do not change the operator's account or local Codex authentication.

Official OpenAI documentation describes saved app-server thread IDs,
`thread/resume` after process restart, and local tool/agent support:
<https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server>,
<https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations>.

## Recovery semantics

| Event | Saved state | What Continue does |
| --- | --- | --- |
| Provider call rejected before action | Failed attempt and diagnostic | Starts a fresh attempt in the same session using confirmed history. |
| Tool failed with confirmed no effect | Failed action receipt | Agent can choose another step or retry that tool under its ordinary idempotency policy. |
| Provider timed out with no observed side effect | Unknown attempt; session open | Starts a new attempt with the uncertainty in context; it does not submit the same provider request identity again. |
| External action outcome unknown | Unknown action receipt | Requires owner readback or operator resolution before that action is repeated; unrelated work can continue. |
| Process or browser stopped | Claimed attempt reconciled from durable receipt | Reconnect reads state; unconfirmed attempt becomes unknown and is not silently relaunched. |
| Agent needs operator input or approval | Waiting state and precise question/request | Operator reply or approval wakes that same session. |

An old work order and its saved chats remain readable. Migration creates one
main session for each work order, imports historical chat turns as attributed
legacy messages, and links existing child work orders as child sessions without
replaying them. The rejected assignment proposal is shown as rejected history,
not imported as four real tasks. The video work order can then continue in
place. New session records become canonical only after dual-read comparison
and an explicit cutover; legacy tables are retired after the compatibility and
rollback gates pass.

## Implementation order and release gates

These are independently testable commits, but the new workflow is only called
ready after all five acceptance gates pass.

1. **Session ledger and read API.** Add project-scoped session, message, and
   attempt tables, append-only guards, migration/backfill, read model, and
   compatibility tests. Existing work orders and old chats retain IDs/history.
2. **Immediate Send and recovery.** Move execution out of the HTTP request,
   claim attempts in a worker, stream saved updates, and make Continue create a
   real new attempt in the same session. Test reload, duplicate send, failure,
   timeout, interrupted worker, and long conversations.
3. **Codex-backed Chief.** Extend the isolated Codex app-server route with a
   scoped Gateway bridge for delegation, consultation, and result reporting.
   Prove real `thread/resume` and one admitted tool call through the existing
   account, including restart and denial cases. Retire post-hoc JSON action
   interpretation after the new path passes parity. The text-only adapter
   fails clearly if selected for a team session.
4. **Agent child sessions and messages.** Save exact sender/recipient messages,
   child and parent results, declared dependencies, and bounded continuation.
   Exercise Engineer to QA and Researcher to GTM handoffs, an unexpected
   blocker routed through the Chief, and an approved peer consultation.
   Verify that viewing a sibling log or saving a peer message causes zero
   model calls, a declared dependency releases only its intended child,
   unrelated transcripts stay out of model context, and target-versus-forecast
   completion cost is visible. Preserve separate Execution Gateway isolation
   for coding.
5. **Unified Task UI and installation.** Main chat with child agent sessions,
   live work log, Continue, evidence, and approvals. Browser acceptance at
   desktop and 390 px; full repository regression, PostgreSQL isolation,
   migration/rollback, interrupted-worker, and real-service integration checks.
   Install only after an exact runtime backup and read back image, schema, API,
   browser bundle, and the existing video work order.

Acceptance scenarios: the Chief delegates a demo-video research task, the
Researcher reports a blocker to the Chief, the operator resolves it in the
same work order, the Researcher continues, GTM and QA exchange saved messages,
the Chief sees child results, and completion waits for a playable artifact and
review evidence. A provider timeout between any of these steps leaves the
same session usable without duplicate side effects. No step claims work that
has only been proposed in text.
