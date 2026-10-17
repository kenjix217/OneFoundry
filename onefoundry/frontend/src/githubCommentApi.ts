import { ApiError, request } from './api';

export interface GitHubCommentStatus {
  approval_id: string;
  status: 'not_attempted' | 'done' | 'failed' | 'unknown';
  retry_allowed: false;
  attempt_id?: string;
  reason_code?: string;
  comment_id?: number | null;
}

function path(projectId: string, approvalId?: string): string {
  const base = `/api/projects/${encodeURIComponent(projectId)}/github-pr-comments`;
  return approvalId ? `${base}/${encodeURIComponent(approvalId)}` : base;
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('Invalid GitHub PR comment response.', null);
  return value as Record<string, unknown>;
}

function parseStatus(value: unknown, approvalId: string): GitHubCommentStatus {
  const item = record(value);
  if (item.approval_id !== approvalId || !['not_attempted', 'done', 'failed', 'unknown'].includes(String(item.status)) ||
      item.retry_allowed !== false || (item.reason_code !== undefined && typeof item.reason_code !== 'string') ||
      (item.attempt_id !== undefined && typeof item.attempt_id !== 'string') ||
      (item.comment_id !== undefined && item.comment_id !== null && !Number.isSafeInteger(item.comment_id))) {
    throw new ApiError('Invalid GitHub PR comment status.', null);
  }
  return item as unknown as GitHubCommentStatus;
}

export async function proposeGitHubComment(projectId: string, fields: {
  run_id: string; source_id: string; number: number; comment: string; idempotency_key: string;
}): Promise<{ approval_id: string; status: 'pending' | 'approved' | 'rejected' }> {
  const item = record(await request(path(projectId), { method: 'POST', body: JSON.stringify(fields) }));
  if (typeof item.approval_id !== 'string' || !['pending', 'approved', 'rejected'].includes(String(item.status))) {
    throw new ApiError('Invalid GitHub PR comment proposal response.', null);
  }
  return { approval_id: item.approval_id, status: item.status as 'pending' | 'approved' | 'rejected' };
}

export async function getGitHubCommentStatus(projectId: string, approvalId: string): Promise<GitHubCommentStatus> {
  return parseStatus(await request(path(projectId, approvalId)), approvalId);
}

export async function dispatchGitHubComment(projectId: string, approvalId: string): Promise<GitHubCommentStatus> {
  return parseStatus(await request(`${path(projectId, approvalId)}/dispatch`, { method: 'POST' }), approvalId);
}

export async function reconcileGitHubComment(projectId: string, approvalId: string): Promise<GitHubCommentStatus> {
  return parseStatus(await request(`${path(projectId, approvalId)}/reconcile`, { method: 'POST' }), approvalId);
}
