import { ApiError, request } from './api';

type ProgressCounts = { active_attempts: number; queued_attempts: number; unfinished_assignments: number;
  blocked_assignments: number; ready_artifacts: number };
type ProgressBlocker = { kind: string; detail: string; session_id?: string; attempt_id?: string };
type ProgressAction = { kind: 'open_agent' | 'review_approval' | 'review_deliverables' | 'review_outcome' | 'wait' | 'message_chief' | 'none';
  label: string; session_id?: string };
export type ChiefProgress = { counts: ProgressCounts; blocker: ProgressBlocker | null;
  next_action: ProgressAction | null; paused: boolean };

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('Current work could not be read.', null);
  return value as Record<string, unknown>;
}

export async function getChiefProgress(projectId: string, orderId: string, signal?: AbortSignal): Promise<ChiefProgress> {
  const row = record(await request(`/api/projects/${encodeURIComponent(projectId)}/chief-workspace/${encodeURIComponent(orderId)}/progress`, { signal }));
  const counts = record(row.counts);
  for (const key of ['active_attempts', 'queued_attempts', 'unfinished_assignments', 'blocked_assignments', 'ready_artifacts']) {
    if (!Number.isSafeInteger(counts[key]) || (counts[key] as number) < 0) throw new ApiError('Current work counts are incomplete.', null);
  }
  const blocker = row.blocker === null ? null : record(row.blocker);
  if (blocker && (typeof blocker.kind !== 'string' || typeof blocker.detail !== 'string' ||
      blocker.session_id !== undefined && typeof blocker.session_id !== 'string' ||
      blocker.attempt_id !== undefined && typeof blocker.attempt_id !== 'string'))
    throw new ApiError('Current work blocker is incomplete.', null);
  const action = row.next_action === null ? null : record(row.next_action);
  if (action && (!['open_agent', 'review_approval', 'review_deliverables', 'review_outcome', 'wait', 'message_chief', 'none'].includes(String(action.kind)) ||
      typeof action.label !== 'string' || action.session_id !== undefined && typeof action.session_id !== 'string' ||
      action.kind === 'open_agent' && typeof action.session_id !== 'string'))
    throw new ApiError('Current work action is incomplete.', null);
  if (typeof row.paused !== 'boolean') throw new ApiError('Current work pause state is incomplete.', null);
  return row as ChiefProgress;
}
