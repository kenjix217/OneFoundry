import { ApiError, request } from './api';

export interface BrowserFixtureInput {
  idempotency_key: string;
  approved_url: string;
  html: string;
  css: string;
  viewport_width: number;
  viewport_height: number;
  max_page_bytes: number;
  timeout_seconds: number;
  lifetime_seconds: number;
}

export interface BrowserFixture {
  id: string;
  project_id: string;
  run_id: string;
  approval_id: string;
  actor_id: string;
  approved_url: string;
  source_sha256: string;
  proposal_digest: string;
  viewport_width: number;
  viewport_height: number;
  max_page_bytes: number;
  timeout_seconds: number;
  lifetime_seconds: number;
  status: string;
  created_at: string;
  decided_at: string | null;
}

export interface BrowserJob {
  id: string;
  project_id: string;
  run_id: string;
  fixture_id: string;
  actor_id: string;
  status: string;
  reason_code: string | null;
  request_sha256: string;
  binding_digest: string;
  source_sha256: string;
  image_id: string;
  receipt_sha256: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface BrowserEvidence {
  job_id: string;
  project_id: string;
  run_id: string;
  status: 'done';
  title: string;
  text: string;
  blocked_request_count: number;
  receipt_sha256: string;
  finished_at: string;
}

function invalid(field: string): never { throw new ApiError(`The server returned invalid ${field}.`, null); }
function object(value: unknown, field: string): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : invalid(field);
}
function string(value: unknown, field: string): string { return typeof value === 'string' ? value : invalid(field); }
function nullableString(value: unknown, field: string): string | null { return value === null ? null : string(value, field); }
function integer(value: unknown, field: string): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : invalid(field);
}
function scopedProjectRun(value: { project_id: string; run_id: string }, projectId: string, runId: string): void {
  if (value.project_id !== projectId || value.run_id !== runId) invalid('browser record scope');
}
function basePath(projectId: string, runId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`;
}
function fixture(value: unknown, projectId: string, runId: string): BrowserFixture {
  const row = object(value, 'browser fixture');
  const item: BrowserFixture = {
    id: string(row.id, 'fixture ID'), project_id: string(row.project_id, 'project ID'), run_id: string(row.run_id, 'run ID'),
    approval_id: string(row.approval_id, 'approval ID'), actor_id: string(row.actor_id, 'actor ID'),
    approved_url: string(row.approved_url, 'approved URL'), source_sha256: string(row.source_sha256, 'source hash'),
    proposal_digest: string(row.proposal_digest, 'proposal digest'),
    viewport_width: integer(row.viewport_width, 'viewport width'), viewport_height: integer(row.viewport_height, 'viewport height'),
    max_page_bytes: integer(row.max_page_bytes, 'maximum page bytes'), timeout_seconds: integer(row.timeout_seconds, 'timeout'),
    lifetime_seconds: integer(row.lifetime_seconds, 'lifetime'), status: string(row.status, 'fixture status'),
    created_at: string(row.created_at, 'created time'), decided_at: nullableString(row.decided_at, 'decision time'),
  };
  scopedProjectRun(item, projectId, runId);
  return item;
}
function job(value: unknown, projectId: string, runId: string): BrowserJob {
  const row = object(value, 'browser job');
  const item: BrowserJob = {
    id: string(row.id, 'job ID'), project_id: string(row.project_id, 'project ID'), run_id: string(row.run_id, 'run ID'),
    fixture_id: string(row.fixture_id, 'fixture ID'), actor_id: string(row.actor_id, 'actor ID'),
    status: string(row.status, 'job status'), reason_code: nullableString(row.reason_code, 'job reason'),
    request_sha256: string(row.request_sha256, 'request hash'), binding_digest: string(row.binding_digest, 'binding digest'),
    source_sha256: string(row.source_sha256, 'source hash'), image_id: string(row.image_id, 'image ID'),
    receipt_sha256: nullableString(row.receipt_sha256, 'receipt hash'),
    created_at: string(row.created_at, 'created time'), finished_at: nullableString(row.finished_at, 'finished time'),
  };
  scopedProjectRun(item, projectId, runId);
  return item;
}

export async function proposeBrowserFixture(projectId: string, runId: string, fields: BrowserFixtureInput): Promise<BrowserFixture> {
  return fixture(await request(`${basePath(projectId, runId)}/browser-static-fixtures`, {
    method: 'POST', body: JSON.stringify(fields),
  }), projectId, runId);
}
export async function getBrowserFixture(projectId: string, runId: string, fixtureId: string, signal?: AbortSignal): Promise<BrowserFixture> {
  const saved = fixture(await request(`${basePath(projectId, runId)}/browser-static-fixtures/${encodeURIComponent(fixtureId)}`, { signal }), projectId, runId);
  if (saved.id !== fixtureId) invalid('fixture identity');
  return saved;
}
export async function listBrowserJobs(projectId: string, runId: string, signal?: AbortSignal): Promise<BrowserJob[]> {
  const value = await request(`${basePath(projectId, runId)}/browser-static-jobs`, { signal });
  if (!Array.isArray(value)) invalid('browser job list');
  return value.map((item) => job(item, projectId, runId));
}
export async function dispatchBrowserJob(projectId: string, runId: string, fixtureId: string, idempotencyKey: string): Promise<BrowserJob> {
  const saved = job(await request(`${basePath(projectId, runId)}/browser-static-jobs`, {
    method: 'POST', body: JSON.stringify({ fixture_id: fixtureId, idempotency_key: idempotencyKey }),
  }), projectId, runId);
  if (saved.fixture_id !== fixtureId) invalid('dispatched fixture identity');
  return saved;
}
export async function getBrowserJobByKey(projectId: string, runId: string, idempotencyKey: string): Promise<BrowserJob | null> {
  try {
    return job(await request(`${basePath(projectId, runId)}/browser-static-jobs/by-key/${encodeURIComponent(idempotencyKey)}`), projectId, runId);
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404) return null;
    throw cause;
  }
}
export async function getBrowserJob(projectId: string, runId: string, jobId: string): Promise<BrowserJob> {
  const saved = job(await request(`${basePath(projectId, runId)}/browser-static-jobs/${encodeURIComponent(jobId)}`), projectId, runId);
  if (saved.id !== jobId) invalid('job identity');
  return saved;
}
export async function reconcileBrowserJob(projectId: string, runId: string, jobId: string): Promise<BrowserJob> {
  const saved = job(await request(`${basePath(projectId, runId)}/browser-static-jobs/${encodeURIComponent(jobId)}/reconcile`, {
    method: 'POST', body: '{}',
  }), projectId, runId);
  if (saved.id !== jobId) invalid('job identity');
  return saved;
}
export async function getBrowserEvidence(projectId: string, runId: string, jobId: string): Promise<BrowserEvidence> {
  const row = object(await request(`${basePath(projectId, runId)}/browser-static-jobs/${encodeURIComponent(jobId)}/evidence`), 'browser evidence');
  const item: BrowserEvidence = {
    job_id: string(row.job_id, 'job ID'), project_id: string(row.project_id, 'project ID'), run_id: string(row.run_id, 'run ID'),
    status: row.status === 'done' ? 'done' : invalid('evidence status'), title: string(row.title, 'evidence title'),
    text: string(row.text, 'evidence text'), blocked_request_count: integer(row.blocked_request_count, 'blocked request count'),
    receipt_sha256: string(row.receipt_sha256, 'receipt hash'), finished_at: string(row.finished_at, 'finished time'),
  };
  scopedProjectRun(item, projectId, runId);
  if (item.job_id !== jobId || item.title.length > 128 || item.text.length > 4096) invalid('browser evidence identity or bounds');
  return item;
}
