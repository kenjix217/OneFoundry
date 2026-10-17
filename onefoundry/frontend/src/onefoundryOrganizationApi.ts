import { ApiError, request } from './api';

export type BusinessMode = 'off' | 'records' | 'manual' | 'event' | 'inbox';
export interface Organization {
  id: string; project_id: string; display_name: string; canonical_domain: string | null;
  identity_reviewed: boolean; qualification: 'unreviewed' | 'qualified' | 'declined'; revision: number;
}
export interface Source {
  id: string; source_kind: 'public_excerpt' | 'input_file'; official_url: string | null;
  excerpt: string | null; observed_at: string; revision: number; superseded_by: string | null;
}
export interface Claim {
  id: string; source_id: string; claim_text: string; locator: string; review_state: 'candidate' | 'approved' | 'rejected';
  verification_state: 'not_run' | 'verified' | 'failed' | 'unknown'; revision: number; fresh: boolean;
}
export interface Detail {
  organization: Organization; sources: Source[]; claims: Claim[]; workflow_revision: number; blocked_reason?: string | null;
  next_action: { action_key: string; label: string; reason: string; workflow_revision: number; source_revision: number | null };
  linked_order?: { work_order_id: string; step_key: 'research' | 'draft'; status: string; run_id: string | null; run_status: string | null } | null;
  context_packet?: { id: string; claim_ids: string[]; statement_versions: { id: string; version: number }[]; excluded: { id: string; reason: string }[]; sha256: string } | null;
  budget?: { linked_orders_limit: number; model_attempts_limit: number; public_web_calls_limit: number;
    effective_context_bytes_limit?: number | null;
    reservations: { id: string; kind: string; state: string; observed_tokens: number | null; observed_cost_usd: number | null }[] } | null;
  intent?: { id: string; state: 'pending' | 'leased' | 'done' | 'blocked' | 'unknown'; attempts: number;
    created_at: string; lease_until: string | null; error_code: string | null; work_order_id: string | null } | null;
  drafts?: Draft[];
}
export interface CompanyStatement {
  id: string; statement_key: string; version: number; body: string; state: 'draft' | 'approved' | 'retired';
  approved_by: string | null; retired_by: string | null; is_current: boolean;
}
export interface DraftSegment {
  kind: 'plain' | 'district_fact' | 'company_fact'; text: string; claim_id?: string;
  statement_id?: string; statement_version?: number;
}
export interface Draft {
  id: string; context_packet_id: string; revision: number; segments: DraftSegment[];
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ApiError('Invalid organization readback.', null);
  return value as Record<string, unknown>;
}
function organization(value: unknown): Organization {
  const row = object(value);
  if (typeof row.id !== 'string' || typeof row.project_id !== 'string' || typeof row.display_name !== 'string' ||
      !Number.isInteger(row.revision) || !['unreviewed', 'qualified', 'declined'].includes(String(row.qualification))) {
    throw new ApiError('Invalid organization readback.', null);
  }
  return row as unknown as Organization;
}
function detail(value: unknown): Detail {
  const row = object(value);
  if (!Array.isArray(row.sources) || !Array.isArray(row.claims) || !Number.isInteger(row.workflow_revision) || !row.next_action) {
    throw new ApiError('Invalid organization detail.', null);
  }
  return { organization: organization(row.organization), sources: row.sources as Source[], claims: row.claims as Claim[],
    workflow_revision: row.workflow_revision as number, next_action: object(row.next_action) as unknown as Detail['next_action'],
    linked_order: row.linked_order as Detail['linked_order'], context_packet: row.context_packet as Detail['context_packet'],
    budget: row.budget as Detail['budget'], drafts: row.drafts as Draft[] | undefined,
    intent: row.intent as Detail['intent'],
    blocked_reason: row.blocked_reason as Detail['blocked_reason'] };
}
const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/organizations`;
const json = (body: unknown) => ({ method: 'POST', body: JSON.stringify(body) });

export async function getBusinessMode(): Promise<BusinessMode> {
  const value = object(await request('/api/onefoundry/business-mode'));
  if (value.mode !== 'off' && value.mode !== 'records' && value.mode !== 'manual' && value.mode !== 'event' && value.mode !== 'inbox') {
    throw new ApiError('Unknown business mode.', null);
  }
  return value.mode;
}
export async function listOrganizations(projectId: string): Promise<Organization[]> {
  const value = await request(base(projectId));
  if (!Array.isArray(value)) throw new ApiError('Invalid organizations list.', null);
  return value.map(organization);
}
export async function getOrganization(projectId: string, id: string): Promise<Detail> {
  return detail(await request(`${base(projectId)}/${encodeURIComponent(id)}`));
}
export async function createOrganization(projectId: string, name: string): Promise<Organization> {
  return organization(await request(base(projectId), json({ display_name: name })));
}
export async function addSource(projectId: string, id: string, data: {
  official_url: string; excerpt: string; observed_at: string; expected_workflow_revision: number;
}): Promise<void> {
  await request(`${base(projectId)}/${encodeURIComponent(id)}/sources`, json({ ...data, source_kind: 'public_excerpt' }));
}
export async function addClaim(projectId: string, id: string, data: {
  source_id: string; claim_text: string; locator: string; expected_source_revision: number;
}): Promise<void> {
  await request(`${base(projectId)}/${encodeURIComponent(id)}/claims`, json(data));
}
export async function reviewClaim(projectId: string, claimId: string, decision: 'approved' | 'rejected', expectedRevision: number): Promise<void> {
  await request(`/api/projects/${encodeURIComponent(projectId)}/claims/${encodeURIComponent(claimId)}/review`,
    json({ decision, expected_revision: expectedRevision }));
}
export async function qualifyOrganization(projectId: string, id: string, qualification: 'qualified' | 'declined', expectedRevision: number): Promise<void> {
  await request(`${base(projectId)}/${encodeURIComponent(id)}/qualification`,
    json({ qualification, expected_revision: expectedRevision }));
}

export async function listCompanyStatements(projectId: string): Promise<CompanyStatement[]> {
  const value = await request(`/api/projects/${encodeURIComponent(projectId)}/company-statements`);
  if (!Array.isArray(value)) throw new ApiError('Invalid company facts readback.', null);
  return value as CompanyStatement[];
}
export async function createCompanyStatement(projectId: string, body: string,
  statementKey?: string, expectedVersion?: number): Promise<CompanyStatement> {
  return object(await request(`/api/projects/${encodeURIComponent(projectId)}/company-statements`,
    json({ body, ...(statementKey ? { statement_key: statementKey, expected_version: expectedVersion } : {}) }))) as unknown as CompanyStatement;
}
export async function decideCompanyStatement(projectId: string, statementId: string,
  decision: 'approve' | 'retire', expectedVersion: number): Promise<CompanyStatement> {
  return object(await request(`/api/projects/${encodeURIComponent(projectId)}/company-statements/${encodeURIComponent(statementId)}/${decision}`,
    json({ expected_version: expectedVersion }))) as unknown as CompanyStatement;
}
export async function requestLinkedOrder(projectId: string, orgId: string, step: 'research' | 'draft',
  profileId: string, revision: number, idempotencyKey: string): Promise<{ work_order_id: string; context_packet_id: string }> {
  const value = object(await request(`${base(projectId)}/${encodeURIComponent(orgId)}/${step}-orders`,
    json({ profile_id: profileId, expected_revision: revision, idempotency_key: idempotencyKey })));
  if (typeof value.work_order_id !== 'string' || typeof value.context_packet_id !== 'string') {
    throw new ApiError('Linked order was not confirmed.', null);
  }
  return value as { work_order_id: string; context_packet_id: string };
}
export async function importResearchCandidates(projectId: string, orgId: string, runId: string): Promise<void> {
  await request(`${base(projectId)}/${encodeURIComponent(orgId)}/research-candidates/from-run/${encodeURIComponent(runId)}`,
    { method: 'POST' });
}
export async function saveOneFoundryDraft(projectId: string, orgId: string, packetId: string,
  segments: DraftSegment[], expectedRevision: number): Promise<Draft> {
  return object(await request(`${base(projectId)}/${encodeURIComponent(orgId)}/drafts`,
    json({ context_packet_id: packetId, segments, expected_revision: expectedRevision }))) as unknown as Draft;
}
