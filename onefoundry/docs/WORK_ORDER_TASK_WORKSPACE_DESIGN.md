# Work Orders and Task Workspace redesign

Execution model superseded by `WORK_ORDER_SESSIONS_V2.md` on 2026-10-04. This
file remains a record of the earlier UI design and installed behavior.

Design reference, 2026-09-29. The first source implementation is recorded in `onefoundry/PROGRESS.md` under “Work Orders and Task Workspace redesign.” The port 8765 app now exposes named chats and the four Task Workspace tabs at migration 0041; the live audit is recorded in `PROGRESS.md`. Coding-provider progress messages and provider-internal tool calls remain outside the current capture boundary, so acceptance item 3 is partial.

## Goal

Let the operator answer four questions immediately: What is the assignment? What is happening? What has actually been produced? What should I do next? Show agent-authored progress and a plain-English account of recorded actions. Keep IDs, hashes, event payloads, provider metadata, and diagnostic codes out of the ordinary path.

“Agent thinking” means deliberate, user-facing working updates: plan, action, finding, and next step. The interface must not promise or display a model's private reasoning. An agent report and a confirmed system receipt remain visibly different sources.

## Page 1: Work Orders

This page is the project queue. Each row shows title, assigned agent, state, last confirmed activity, and next action. Filter chips: **All**, **Needs me**, **In progress**, **Ready**, **Completed**. Search title and instructions. “Needs me” is derived only from a saved approval, block, review request, or unknown outcome; elapsed time alone is not an approval request.

```text
Work Orders                                      [+ New work order]
All 12   Needs me 2   In progress 3   Ready 4   Completed 3
[Search work orders...........................................]

Prepare district research       Researcher   Waiting for your review
Last confirmed: Research draft saved, 10:42 AM
Next: Review the draft and decide whether to hand off to GTM       [Open task]

Fix repository import          Engineer      In progress
Last confirmed: Coding attempt started, 10:39 AM
Next: Waiting for the runner's saved result                      [Open task]
```

Opening a row goes to its Task Workspace. The create flow stays short: title, desired outcome/instructions, agent, optional files, **Create draft**. Starting a text run, coding run, or manual record is an explicit choice inside the task, with availability and the effect of each choice explained before launch. A compact preview may show the brief and latest result, but it must not become another full task page.

Status language: **Draft**, **Ready to start**, **Working**, **Waiting for you**, **Completed**, **Failed**, **Outcome uncertain**. “Completed” requires a saved outcome, not merely a successful dispatch. For an uncertain external action, show **Outcome uncertain** and a reconciliation action; never imply an automatic retry.

## Page 2: Task Workspace

One work order has one stable workspace, reachable from Work Orders, Activity, Approvals, and notifications. Its header shows title, assigned agent, state, and a short brief. A pinned **Now / Next / Waiting on** strip uses the latest saved evidence; a missing progress update says so plainly. No invented percentage-complete meter.

Primary tabs: **Conversation**, **Activity**, **Results**, **Details**.

```text
← Work Orders / Fix repository import                  Working · Engineer
Now: Coding attempt is running
Next: Read the saved result and QA review when available
Waiting on: Runner                                      [Refresh]

Conversation   Activity   Results   Details

Main chat   Engineer: import fix   QA review   [+ New chat]
──────────────────────────────────────────────────────────────
You · 10:31       Please check the repository import failure.
Engineer · 10:32  Plan: Trace the import path and reproduce the error.
                  ▸ Actions in this turn · 3 observed
Engineer · 10:40  Found: The source validation rejects this path.
                  Next: Make the focused change and run the relevant tests.
                  ▸ Actions in this turn · 2 observed
System · 10:41    Test command finished with 12 passing tests.
                  ▸ View command, output, and saved test receipt
──────────────────────────────────────────────────────────────
[Attach] [Message Engineer...................................] [Send]
```

**Conversation** has a second row of named chat tabs scoped to this work order, plus **New chat**. A new chat starts a separate conversation with the current assigned agent by default, inherits the work-order brief and approved inputs, and does not silently inherit another chat's transcript. The operator can optionally attach an earlier result or summary. Chat messages are agent/model conversations; they do not by themselves start a coding runner, approve an action, or change the work-order owner. To involve another agent, use an explicit **Consult agent** or **Hand off** action, recorded with its authority and outcome. Historical agent chats remain readable after handoff.

**Each agent turn or work loop has a visible narrative and an expandable action trace.** A loop begins with the agent's stated plan, continues through what it tried and found, and ends with its next step or final answer. Longer runs can have several loops under one operator request; handoffs start a clearly attributed loop for the next agent. These are concise, deliberate progress messages, not hidden model reasoning. Updates appear in the conversation as they are saved, including while a supported run is active.

The feed has three clearly labeled item types: **You** (operator message), **Agent update** (actual saved agent-authored text), and **System activity** (plain-English projection of a confirmed event or receipt). Never fabricate an agent quote from a status event. Supported runs request updates at start, after meaningful findings, before a consequential next step, when blocked, and at completion. If the provider supplies no intermediate messages, show the observed actions and “No agent progress update received yet.” The current text-turn API returns a saved reply after an explicit send; it is not a continuous autonomous conversation.

Every loop has an **Actions in this turn** disclosure directly under its update. Its ordered list includes every captured tool invocation and harness command: tool name; safe arguments or exact redacted shell command; project-relative working directory; start/end time; outcome or exit code; bounded stdout/stderr; files read or changed; patch or script snapshot when saved; and linked test/runner receipts. Failed, denied, timed-out, and proposed-but-not-run actions are included and labeled as such. Clicking one action opens its detail without replacing the conversation. The disclosure defaults closed so the ordinary chat remains readable. A task-wide **Activity** tab gives the same evidence across threads and runs.

```text
⌄ Actions in this turn · 3 observed · Runner observed
  1  Read  src/import/source.py                         Completed
  2  Shell python -m pytest tests/test_import.py       Failed · exit 1
     ⌄ Output: 1 failed, 11 passed  [Show captured output]
  3  Edit  src/import/source.py                         Saved
     ⌄ View patch / saved script version
```

When a provider gives no reliable loop boundary, group actions under the one observed agent turn or run attempt; do not invent intermediate turns merely to make the timeline look busy.

The trace header states its coverage: **Runner observed**, **Provider reported**, **Partial**, or **Unavailable**. Every harness-owned command must be accounted for; failure to save one marks coverage **Partial**. “All actions” means all actions visible at the integration boundary and actually captured; the interface must not claim to know unobserved provider internals. If output is redacted or truncated, the action says so and gives the captured byte/count limit. Secrets and protected data are removed before persistence, not merely hidden in the browser. Technical identifiers remain in a deeper details disclosure.

**Activity** is a work-order-wide chronological feed across all chats, runs, handoffs, approvals, and results. Group noisy low-level events into meaningful steps: “Researcher started,” “Draft saved,” “QA review requested,” “Approval pending,” “Action confirmed,” or “Outcome uncertain.” Every item has a source label, time, agent, run or thread link, and a concise consequence. A technical details disclosure may show raw event payloads for diagnosis, with secrets redacted. A provider report is labeled **Agent reported** until independently confirmed by the owning result or external readback.

**Results** shows the latest deliverable first, followed by prior versions or attempts: readable text, files, code changes, test outcome, QA verdict, and requested approvals. Each result says whether it is a confirmed artifact, an agent claim, a synthetic fixture, or a manual record. A failed or unknown run does not erase earlier results. File names are visible; hashes and storage paths live in technical details.

**Details** holds the full instructions, inputs, assigned profile/history, start options, edit/block/reassign controls, model/usage information, and a collapsed **Technical details** section with IDs, SHA-256 values, exact attempts, event JSON, and receipts. Destructive or external actions retain their existing approval and readback gates. Diagnostics remain accessible without dominating the task.

## Data and behavior required to make the design true

- The current `task_conversations` constraint is unique on `(project_id, work_order_id, profile_id)` and `open()` reuses that one row. Multiple chats require an additive migration, explicit thread title/participant/state, thread-scoped send identities, and a selected-thread API. Preserve existing conversations as the first thread of their work order.
- Build a work-order activity read model from canonical append-only events, runs, results, approvals, and conversation turns. Keep the original evidence; map recognized event types to bounded plain-English messages. Unknown event types get a safe generic label, not dumped JSON or an invented success claim.
- Save agent-authored progress as bounded, redacted, attributed updates only when a supported provider or runner actually emits them. Preserve ordering, deduplicate on reconnect, and indicate whether a message was agent-reported or owner-confirmed. The runner and Tool Gateway remain the event writers.
- Add a bounded action record keyed to project, work order, run/attempt or chat turn, agent, and sequence; thread and loop links are present when known. Capture the runner's own harness commands and normalized provider tool events as they happen, before the raw provider stream is discarded. Record invocation, completion/failure, redaction/truncation, and provenance separately; a missing completion stays unknown. Link saved scripts, patches, test results, and outputs through artifacts. The current coding CLI path retains a receipt and short sanitized log, so existing runs cannot be retroactively shown as complete action traces.
- The page subscribes to persisted changes or polls by cursor. Reconnect reloads saved state. A spinner or typing indicator must never imply work is still running after an unknown or disconnected attempt. Show the last confirmed time and a manual refresh/reconcile action.
- Chat context, work-order ownership, run attempts, approvals, and results have separate identities. An operator message must not silently trigger a new run or transfer agent authority. Unknown sends and launches retain their original submission identities; no automatic resend.
- On mobile, the queue becomes full-width rows; the Task Workspace keeps a sticky title/status and horizontally scrollable tab rows. Conversation, progress, and composer stay readable at 390 px; technical detail is collapsed.

## Acceptance for the design's first implementation slice

1. An operator opens a work order and can identify its owner, latest confirmed action, current blocker, and next action without reading an ID or JSON.
2. The operator creates two chats under one work order; each retains its own messages after refresh and one chat's history is not silently sent in the other.
3. One supported multi-loop run shows the agent's plan, finding, and next step per loop. Expanding each loop shows its ordered observed tool calls and harness commands, including a failed action, safe command/output details, and linked receipts. Another run with only final provider output shows **Partial** or **Unavailable** coverage instead of invented progress.
4. A real saved agent update, a system event, and a confirmed result render as different sources. A failed or unknown attempt stays visibly failed/unknown; retry requires explicit operator action and preserves the first attempt.
5. Desktop and 390 px browser checks cover the queue, task tabs, chat switching, expanded action trace, an approval wait, and technical details. API/PostgreSQL tests cover thread identity, same-project scope, handoff, duplicate sends, ordering, redaction, truncation, uncertain actions, and event projection.

## Proposed sequence

1. Approve the information layout and wording using this design.
2. Build the work-order summary projection and plain-English Activity/Results views from existing saved evidence.
3. Add multiple work-order chats with migration and thread-scoped APIs; move Task Chat into the Task Workspace.
4. Capture supported provider progress and complete observed action traces, then add live refresh and browser acceptance. Do not call the Conversation redesign complete with receipt-only coding runs. Keep source-only, local browser, live provider, and deployment evidence separate.

Open product choice: should **New chat** offer another agent as a consult immediately, or start with the assigned agent and offer **Consult / Hand off** as separate actions? The second option is recommended because it keeps work ownership explicit.
