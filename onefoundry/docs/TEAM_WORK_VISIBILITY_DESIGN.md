# Team work visibility

The proposed message and execution model below is superseded by
`WORK_ORDER_SESSIONS_V2.md` on 2026-10-04. It remains a historical description
of the installed read-only Team view.

Status: saved task and chat viewer implemented in the frontend; direct agent messages and intermediate text-model progress are design only. This document changes the earlier assumption in `EVENT_DRIVEN_COORDINATION_DESIGN.md` that agents never talk to one another. That rule remains the active runtime behavior until the bounded message contract below is implemented and tested.

## What exists

The Tool Gateway saves work-order assignments, lineage, handoffs, runs, outputs, and append-only events. Task conversations save operator/project messages and final agent replies per work order. Coding runs can save provider-reported progress and runner-observed actions. The Chief's follow-up is a project update based on child state, not a message from the child agent. The current text-chat adapter makes one model call and saves a final reply; it does not capture intermediate progress. Conversation capture is off by default for run traces. None of these records is a direct agent-to-agent conversation.

The Task **Team** tab reads existing work-order lineage, run summaries, and saved chats. It makes no agent calls or mutations. The Task **Work log** disclosure shows only saved turn facts. A chat reply can contain the agent's own plan, finding, and next step, but the UI does not create quotes or private reasoning that were never recorded. Empty and failed reads stay distinct.

## Direct message contract for a later slice

One bounded message is a deliberate handoff or consultation attached to a work order. It is not an unrestricted agent chat loop. Direct agent messaging is a project opt-in and defaults off, preserving the current conversation-capture boundary. The sender must be an active agent run or the operator. The recipient must be an active profile in the same project. Sending alone does not start a run, approve an outbound action, transfer work ownership, or mark a work order done. The recipient sees the message as labelled task data at its next authorized run. To request new work, the sender uses the existing assignment/handoff path; a message cannot grant tools or autonomy.

Owner: a focused `agent_message_store` behind the Tool Gateway, with a project-scoped read API. The Gateway validates the saved and current profile permissions, sender run, recipient, work-order lineage, content limit, and an idempotency key before writing. The runner may submit its own observed progress through its existing receipt path; the browser cannot impersonate an agent sender. Only the Gateway/runner may append events. A database trigger blocks message update/delete, matching the event rule. Secrets and protected data are masked before persistence.

Suggested record: `id`, `project_id`, `work_order_id`, `source_run_id`, `from_actor_id`, `from_profile_id`, `to_profile_id`, `kind` (`handoff_note` or `consultation`), bounded `body`, `idempotency_key`, `created_at`, and `delivery_state` (`saved`, `included_in_run`, `unavailable`). Each record links to an append-only event and, where applicable, the created child work order or recipient run. Message identity is unique per sender run and idempotency key. Do not store chain of thought, provider prompts, credentials, or raw tool streams in this table.

The read API returns a timeline for one work-order family with cursor pagination and typed entries: assignment, agent message, task chat turn, run progress, observed action, result, and approval. Every entry carries source and certainty (`agent reported`, `gateway saved`, `runner observed`, or `outcome unknown`). Reads enforce exact project and work-order scope. They may show the operator each agent's existing task chat without making that chat part of another agent's prompt by default.

Delivery uses a bounded, redacted message excerpt added to the recipient's next work-order dossier. The recipient receives no automatic response trigger from the message itself. If a message arrives after the recipient run has started, it waits for the next authorized run. The sender gets a saved receipt; a failed or unknown write remains explicit and is reconciled by idempotency key, never blindly repeated. If a recipient is removed or unavailable, the record stays visible and delivery is marked unavailable. A direct message must not create an unbounded back-and-forth or bypass coordination budgets, depth limits, and approval gates.

## Visible working updates

For text chat, the model can provide a short user-facing plan, finding, blocker, and next step. A future adapter should accept a bounded structured `progress_narration` field or supported provider progress event, validate and redact it server-side, and save it with turn ID, sequence, timestamp, agent profile, and `agent reported` source. The same provider call should produce the final answer; do not make extra polling model calls just to invent progress. A provider with only a final response shows that response and “No intermediate update recorded.” Coding progress remains labelled provider reported or runner observed according to its source. Private model reasoning is never requested, stored, or displayed.

## Acceptance

1. A Chief creates two child work orders. The Team view shows both saved assignments, their owners, statuses, task chats, runs, and evidence, including when one child has no chat or run.
2. A bounded handoff note is saved once under duplicate delivery, appears with sender and recipient, and is included in the recipient's next authorized dossier. No run starts from the note alone.
3. Cross-project access, inactive sender, invalid recipient, oversize content, revoked permission, and forged agent identity fail closed. Unknown persistence is read back before retry.
4. A text turn with an actual progress narration displays it as agent reported. A final-only turn displays no invented intermediate steps. A failed/unknown turn never claims completion.
5. Desktop and 390 px browser checks cover team navigation, child chat readback, a failed read, progress disclosures, and the absence of a false agent-to-agent transcript. PostgreSQL tests cover scope, immutability, ordering, idempotency, redaction, and dossier inclusion.
