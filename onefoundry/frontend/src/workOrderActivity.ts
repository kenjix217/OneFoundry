import type { Run, RunEvent, WorkOrder } from './workApi';

const WORK_STATUS: Record<WorkOrder['status'], string> = {
  draft: 'Draft', ready: 'Ready to start', running: 'Working', blocked: 'Waiting for you',
  needs_review: 'Needs review', done: 'Completed', failed: 'Failed', unknown: 'Outcome uncertain',
};

export function workOrderStatusLabel(status: WorkOrder['status']): string {
  return WORK_STATUS[status];
}

export type ActivityItem = { id: string; at: string; title: string; detail: string; source: 'System' | 'Agent reported';
  runId: string | null; event: RunEvent };

const LABELS: Record<string, string> = {
  work_order_created: 'Work order created',
  work_order_ready: 'Work order marked ready',
  work_order_assigned: 'Assigned agent changed',
  work_order_blocked: 'Work order blocked',
  work_order_unblocked: 'Work order resumed',
  work_order_completion_evaluated: 'Deliverable check recorded',
  work_order_accepted: 'Work order accepted',
  work_order_returned: 'Work order returned',
  work_order_reopened: 'Work order reopened',
  run_started: 'Run started',
  run_handed_off: 'Work handed to another agent',
  run_finished: 'Run outcome recorded',
  run_result_recorded: 'Result saved',
  approval_requested: 'Approval requested',
  approval_decided: 'Approval decision saved',
  live_text_started: 'Text model call started',
  live_text_finished: 'Text model call ended',
  external_code_queued: 'Coding attempt queued',
  external_code_prelaunch: 'Coding launch recorded',
  external_code_session: 'Coding agent session observed',
  external_code_receipt: 'Runner result saved',
  external_code_unknown: 'Coding outcome uncertain',
  task_conversation_opened: 'Chat opened',
  task_conversation_turn_started: 'Message sent to agent',
  task_conversation_turn_finished: 'Agent reply recorded',
  task_conversation_unknown_acknowledged: 'Uncertain reply acknowledged',
  synthetic_text_handed_off: 'Fixture work handed to the next agent',
  qa_review_requested: 'QA review requested',
  role_tool_started: 'Agent tool action started',
  role_tool_finished: 'Agent tool action recorded',
  role_tool_denied: 'Agent tool action denied',
};

const STATUS: Record<string, string> = {
  done: 'A saved outcome is available.',
  failed: 'The attempt failed. Review the recorded reason before retrying.',
  unknown: 'The outcome is uncertain. Check the owner record before retrying.',
  running: 'The attempt was recorded as running.',
  queued: 'The runner has not returned a confirmed result.',
};

export function describeActivity(event: RunEvent): ActivityItem {
  const payload = event.payload;
  const tool = typeof payload.tool_id === 'string' && /^[a-z0-9_.-]{1,80}$/.test(payload.tool_id)
    ? payload.tool_id.replaceAll(/[_.-]/g, ' ') : null;
  const title = tool && event.event_type.startsWith('role_tool_')
    ? `${tool} · ${event.event_type === 'role_tool_denied' ? 'denied' :
      event.event_type === 'role_tool_started' ? 'started' : 'finished'}`
    : LABELS[event.event_type] ?? 'Additional activity recorded';
  const status = typeof payload.status === 'string' ? STATUS[payload.status] : undefined;
  const detail = event.event_type === 'work_order_created' ? 'The assignment was saved. Its agent can be reviewed in Assignment.'
    : event.event_type === 'work_order_ready' ? 'The assignment is ready for an explicit start.'
    : event.event_type === 'work_order_assigned' ? 'The assigned agent changed. Review the current owner in Assignment.'
    : event.event_type === 'work_order_blocked' ?
      `Work was paused${typeof payload.reason === 'string' ? `: ${payload.reason}` : '.'}`
    : event.event_type === 'work_order_unblocked' ? 'The operator resumed this assignment.'
    : ['work_order_accepted', 'work_order_returned', 'work_order_reopened'].includes(event.event_type)
      ? `The operator recorded a decision${typeof payload.reason === 'string' ? `: ${payload.reason}` : '.'}`
    : event.event_type === 'work_order_completion_evaluated'
      ? payload.status === 'needs_review' ? 'The run finished; the deliverable still needs operator review.'
        : 'The saved deliverable evidence was evaluated.'
    : event.event_type === 'approval_requested' ? 'An operator decision is required before this action proceeds.'
    : event.event_type === 'approval_decided' ? 'The operator decision was recorded.'
    : event.event_type === 'task_conversation_opened' ? 'A chat was saved under this work order.'
    : event.event_type === 'task_conversation_turn_started' ? 'A message was sent. The reply is not confirmed yet.'
    : event.event_type === 'task_conversation_unknown_acknowledged' ?
      'The uncertain reply was acknowledged without resending it.'
    : event.event_type === 'run_started' ? 'A run was started. Open the run for its saved state.'
    : event.event_type === 'run_handed_off' ? 'The work was handed to another agent.'
    : event.event_type === 'external_code_queued' ? 'A coding attempt was queued. Launch is not yet confirmed.'
    : event.event_type === 'external_code_prelaunch' ? 'The coding launch request was recorded.'
    : event.event_type === 'external_code_receipt' ? 'The runner saved its result. Open Results to inspect it.'
    : event.event_type === 'run_result_recorded' ? 'A result was saved. Open Results to read it.'
    : event.event_type === 'live_text_finished' && payload.status === 'done'
    ? 'The provider returned text and OneFoundry saved it. Review the output before treating the task as complete.'
    : event.event_type === 'task_conversation_turn_finished' && payload.status === 'done'
      ? 'The agent reply was saved in its chat.'
      : status ?? (event.event_type === 'external_code_session'
        ? 'A provider session was observed. Completion still needs a saved runner result.'
        : event.event_type === 'role_tool_started'
          ? 'The tool was invoked; completion has not been confirmed.'
          : event.event_type === 'role_tool_denied'
            ? 'The requested tool action was not admitted. No tool result was produced.'
            : event.event_type === 'role_tool_finished'
              ? 'A tool result was recorded. Exact arguments were not retained in this event.'
          : 'This step is recorded. The full record is retained in the activity log.');
  return { id: event.id, at: event.created_at, title, detail, source: 'System', runId: event.run_id, event };
}

export function workOrderNow(order: WorkOrder, runs: Run[]): { now: string; next: string } {
  const latest = runs.find((run) => run.work_order_id === order.id);
  if (order.status === 'blocked') return { now: order.block_reason ?? 'Work is paused.',
    next: 'Review the blocker and resume the work order when ready.' };
  if (order.status === 'unknown' || latest?.status === 'unknown') return {
    now: 'The latest outcome is uncertain.', next: 'Inspect the saved attempt before starting another one.' };
  if (latest?.status === 'running') return { now: 'A run is recorded as running.',
    next: 'Wait for a saved result or check the runner receipt.' };
  if (order.status === 'draft') return { now: 'The assignment is a draft.',
    next: 'Review it and mark it ready.' };
  if (order.status === 'ready') return { now: 'Ready to start.',
    next: 'Choose the text, coding, or manual start path.' };
  if (order.status === 'failed') return { now: 'The last attempt failed.',
    next: 'Review its result and decide whether to make a new attempt.' };
  if (order.status === 'needs_review') return { now: 'The run finished; completion needs your review.',
    next: 'Inspect the deliverable and either Accept or Return the work order.' };
  if (order.status === 'done') return { now: 'The deliverable was accepted or verified.',
    next: 'Review the saved result and evidence in Activity.' };
  return { now: 'Work is in progress.', next: 'Review the latest saved activity.' };
}
