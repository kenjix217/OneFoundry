import { ApiError, request } from './api';

export interface SalesDraft {
  id: string;
  project_id: string;
  source_run_id: string;
  run_id: string;
  approval_id: string;
  actor_id: string;
  recipient: 'sales-preview@onefoundry.invalid';
  subject: string;
  body: string;
  body_sha256: string;
  delivery_status: 'pending' | 'rejected' | 'unknown' | 'delivered';
  sent_at: string | null;
  created_at: string;
}

export interface SalesEligibility {
  eligible: boolean;
  reason: string | null;
  sales_profile_id: string | null;
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ApiError('The server returned an invalid Sales record.', null);
  }
  return value as Record<string, unknown>;
}

function draft(value: unknown, projectId: string, runId: string): SalesDraft {
  const row = record(value);
  if (row.project_id !== projectId || row.run_id !== runId || row.source_run_id !== runId ||
    row.recipient !== 'sales-preview@onefoundry.invalid' ||
    !['pending', 'rejected', 'unknown', 'delivered'].includes(String(row.delivery_status))) {
    throw new ApiError('The Sales draft did not match this local run.', null);
  }
  for (const field of ['id', 'approval_id', 'actor_id', 'subject', 'body', 'body_sha256', 'created_at']) {
    if (typeof row[field] !== 'string') throw new ApiError('The Sales draft is incomplete.', null);
  }
  if (row.sent_at !== null && typeof row.sent_at !== 'string') throw new ApiError('Invalid Sales delivery time.', null);
  return row as unknown as SalesDraft;
}

function path(projectId: string, runId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`;
}

export async function salesEligibility(projectId: string, runId: string, signal?: AbortSignal): Promise<SalesEligibility> {
  const row = record(await request(`${path(projectId, runId)}/sales-eligibility`, { signal }));
  if (typeof row.eligible !== 'boolean' || (row.reason !== null && typeof row.reason !== 'string') ||
    (row.sales_profile_id !== null && typeof row.sales_profile_id !== 'string')) {
    throw new ApiError('Invalid Sales eligibility response.', null);
  }
  return row as unknown as SalesEligibility;
}

export async function getSalesDraft(projectId: string, runId: string, signal?: AbortSignal): Promise<SalesDraft> {
  return draft(await request(`${path(projectId, runId)}/sales-draft`, { signal }), projectId, runId);
}

export async function createSalesDraft(projectId: string, runId: string): Promise<SalesDraft> {
  return draft(await request(`${path(projectId, runId)}/sales-draft`, { method: 'POST' }), projectId, runId);
}
