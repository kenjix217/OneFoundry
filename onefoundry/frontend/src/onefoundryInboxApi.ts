import { ApiError, request } from './api';

export interface InboxItem {
  subject_ref: { kind: 'organization'; id: string };
  subject_name: string;
  action: { action_key: string; label: string; reason: string; workflow_revision: number; source_revision: number | null };
  reason: string; required_actor: string | null; due_at: string | null; blocked_reason: string | null;
  evidence_state: string; work_order_id: string | null; work_order_status: string | null; approval_pending: boolean;
}
export interface Inbox { actionable: InboxItem[]; waiting: InboxItem[]; blocked: InboxItem[]; unknown: InboxItem[] }
export interface Outcomes {
  reviewed_research: number; qualified_districts: number; saved_drafts: number; districts_with_drafts: number;
  sent: number; sent_basis: string; observed_tokens: number | null; observed_cost_usd: string | null;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/onefoundry`;
function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ApiError('Invalid inbox readback.', null);
  return value as Record<string, unknown>;
}
export async function getInbox(projectId: string): Promise<Inbox> {
  const row = object(await request(`${base(projectId)}/inbox`));
  if (!['actionable', 'waiting', 'blocked', 'unknown'].every((key) => Array.isArray(row[key]))) {
    throw new ApiError('Invalid inbox readback.', null);
  }
  return row as unknown as Inbox;
}
export async function getOutcomes(projectId: string): Promise<Outcomes> {
  const row = object(await request(`${base(projectId)}/outcomes`));
  if (!['reviewed_research', 'qualified_districts', 'saved_drafts', 'districts_with_drafts', 'sent'].every(
    (key) => Number.isInteger(row[key]) && Number(row[key]) >= 0)) throw new ApiError('Invalid outcome readback.', null);
  if (row.observed_tokens !== null && !Number.isInteger(row.observed_tokens)) throw new ApiError('Invalid usage readback.', null);
  if (row.observed_cost_usd !== null && typeof row.observed_cost_usd !== 'string') throw new ApiError('Invalid cost readback.', null);
  return row as unknown as Outcomes;
}
