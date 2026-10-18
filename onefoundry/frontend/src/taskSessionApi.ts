import { ApiError, request } from './api';

export type TeamMessage = { id: string; session_id: string; client_message_id: string | null;
  source_session_id?: string | null; source_turn_id?: string | null;
  sequence: number; actor_id: string; kind: 'operator' | 'agent' | 'system' | 'legacy_operator' | 'legacy_agent';
  body: string; created_at: string };
export type TeamAttempt = { purpose?: "work" | "discussion"; id: string; session_id: string; status: 'queued' | 'running' | 'done' | 'failed' | 'unknown';
  unknown_acknowledged?: boolean; terminal_result_reconciled?: boolean; source_message_id?: string | null;
  started_at?: string | null; not_before?: string | null;
  client_attempt_id: string | null;
  reason: string | null; input_tokens: number | null; output_tokens: number | null; cost_usd: string | null;
  provider?: string | null; model_id?: string | null; retry_of_attempt_id?: string | null;
  created_at: string; finished_at: string | null };
export type TeamSession = { waiting_reason?: 'local_recording' | 'recording_ready' | 'recording_unconfirmed' | null; id: string; profile_id: string; status: 'open' | 'working' | 'waiting_for_agent' | 'waiting_for_operator' | 'closed';
  execution_mode: 'legacy' | 'team'; work_order_status?: string | null };
export type TeamChild = { work_order_id?: string; id: string; profile_id: string; parent_session_id: string | null; status: TeamSession['status']; work_order_status?: string | null; execution_mode?: 'legacy' | 'team' };
export type TeamAssignment = { id: string; child_session_id: string; objective: string; expected_result: string;
  acceptance_check: string; acceptance_kind?: 'text_report' | 'artifact' | 'operator_review' | 'qa_review'; status: 'pending' | 'ready' | 'active' | 'satisfied' | 'blocked' | 'canceled';
  result_message_id: string | null };
export type TeamDecisionCause = { id: string; main_session_id: string; plan_revision: number;
  kind: 'blocker' | 'synthesis'; detail: string; dispatch_attempt_id: string | null;
  resolved_at: string | null; handled_at: string | null; created_at: string };
export type TeamToolCall = { id: string; session_id: string; attempt_id: string;
  tool_name: string;
  status: 'started' | 'done' | 'failed'; result: Record<string, unknown> | null;
  reason: string | null; created_at: string; finished_at: string | null };
export type TeamSessionView = { session: TeamSession; messages: TeamMessage[]; attempts: TeamAttempt[];
  children: TeamChild[]; assignments: TeamAssignment[]; decision_causes: TeamDecisionCause[];
  tool_calls: TeamToolCall[];
  history_omitted?: { messages: number; attempts: number; decision_causes: number; tool_calls: number } };
export type TeamEnqueue = { session_id: string; message_id: string | null; attempt_id: string | null;
  sequence: number; state: 'queued' | 'coalesced' | 'waiting_for_input' };

function view(value: unknown): TeamSessionView {
  if (!value || typeof value !== 'object' || !('session' in value) || !('messages' in value) ||
      !('children' in value) || !('attempts' in value) || !('assignments' in value) ||
      !('decision_causes' in value) || !('tool_calls' in value)) {
    throw new ApiError('The team session response was incomplete.', null);
  }
  const item = value as TeamSessionView;
  if (!item.session || !['legacy', 'team'].includes(item.session.execution_mode) ||
      !Array.isArray(item.messages) || !Array.isArray(item.attempts) ||
      !Array.isArray(item.children) || !Array.isArray(item.assignments) ||
      !Array.isArray(item.decision_causes) || !Array.isArray(item.tool_calls)) {
    throw new ApiError('The team session response was invalid.', null);
  }
  return item;
}

function base(projectId: string, orderId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(orderId)}/team-session`;
}

export async function getTeamSession(projectId: string, orderId: string, sessionId?: string, signal?: AbortSignal): Promise<TeamSessionView> {
  const path = sessionId ? `${base(projectId, orderId)}/sessions/${encodeURIComponent(sessionId)}` : base(projectId, orderId);
  return view(await request(path, { signal }));
}

export async function sendTeamMessage(projectId: string, orderId: string, sessionId: string,
  clientMessageId: string, sequence: number, body: string): Promise<TeamEnqueue> {
  return await request(`${base(projectId, orderId)}/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST', body: JSON.stringify({ client_message_id: clientMessageId, expected_sequence: sequence, body }),
  }) as TeamEnqueue;
}

export async function continueTeamSession(projectId: string, orderId: string, sessionId: string,
  clientAttemptId: string): Promise<TeamEnqueue> {
  return await request(`${base(projectId, orderId)}/sessions/${encodeURIComponent(sessionId)}/continue`, {
    method: 'POST', body: JSON.stringify({ client_attempt_id: clientAttemptId }),
  }) as TeamEnqueue;
}

export async function reconcileTeamActions(projectId: string, orderId: string, sessionId: string): Promise<number> {
  const value = await request(`${base(projectId, orderId)}/sessions/${encodeURIComponent(sessionId)}/reconcile`, { method: 'POST' });
  if (!value || typeof value !== 'object' || !('reconciled_actions' in value) ||
      typeof value.reconciled_actions !== 'number' || !Number.isSafeInteger(value.reconciled_actions) || value.reconciled_actions < 0) {
    throw new ApiError('Saved-action reconciliation could not be confirmed.', null);
  }
  return value.reconciled_actions;
}

export async function acknowledgeUnknown(projectId: string, orderId: string, sessionId: string,
  attemptId: string, clientActionId: string, reason: string): Promise<void> {
  await request(`${base(projectId, orderId)}/sessions/${encodeURIComponent(sessionId)}/attempts/${encodeURIComponent(attemptId)}/acknowledge`, {
    method: 'POST', body: JSON.stringify({ client_action_id: clientActionId, decision: 'abandon_unverified', reason }),
  });
}

export type TeamChildReturn = { assignment_id: string; session_id: string; message_id: string;
  attempt_id: string; sequence: number; prior_result_message_id: string };

export async function returnTeamChild(projectId: string, orderId: string, sessionId: string,
  clientActionId: string, sequence: number, resultMessageId: string, body: string): Promise<TeamChildReturn> {
  const value = await request(`${base(projectId, orderId)}/sessions/${encodeURIComponent(sessionId)}/return`, {
    method: 'POST', body: JSON.stringify({ client_action_id: clientActionId, expected_sequence: sequence,
      expected_result_message_id: resultMessageId, body }),
  });
  if (!value || typeof value !== 'object' || !('assignment_id' in value) || typeof value.assignment_id !== 'string' ||
      !('session_id' in value) || value.session_id !== sessionId || !('message_id' in value) || typeof value.message_id !== 'string' ||
      !('attempt_id' in value) || typeof value.attempt_id !== 'string' || !('sequence' in value) || !Number.isSafeInteger(value.sequence) ||
      !('prior_result_message_id' in value) || value.prior_result_message_id !== resultMessageId) {
    throw new ApiError('The revision could not be confirmed. Check saved state before retrying.', null);
  }
  return value as TeamChildReturn;
}
