import { ApiError, request } from './api';

export type MemoryScope = 'project' | 'company';
export type CandidateStatus = 'pending' | 'approved' | 'rejected';

export interface SourceResult {
  id: string;
  project_id: string;
  actor_id: string;
  run_id: string | null;
  summary: string;
  scope: MemoryScope;
  source_kind: 'operator_entered' | 'synthetic_sales' | 'synthetic_qa' | 'conversation_capture';
  source_sales_draft_id?: string | null;
  source_runner_job_id?: string | null;
  source_sha256?: string | null;
  source_capture_id?: string | null;
  created_at: string;
}

export interface Candidate {
  id: string;
  project_id: string;
  actor_id: string;
  source_result_id: string;
  summary: string;
  scope: MemoryScope;
  status: CandidateStatus;
  created_at: string;
  reviewed_at: string | null;
  reviewed_by_actor_id: string | null;
  replaces_memory_id?: string | null;
  source_capture_id?: string | null;
}

export interface MemoryItem {
  id: string;
  project_id: string;
  actor_id: string;
  scope: MemoryScope;
  text: string;
  source_result_id: string;
  source_candidate_id: string;
  created_at: string;
  score: number;
  similarity?: number | null;
  state?: 'current' | 'withdrawn' | 'superseded';
  superseded_by?: string | null;
}

function invalid(field: string): never {
  throw new ApiError(`The server returned an invalid ${field}.`, null);
}

function object(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid(field);
  return value as Record<string, unknown>;
}

function string(value: unknown, field: string): string {
  return typeof value === 'string' ? value : invalid(field);
}

function nullableString(value: unknown, field: string): string | null {
  return value === null ? null : string(value, field);
}

function scope(value: unknown): MemoryScope {
  return value === 'project' || value === 'company' ? value : invalid('memory scope');
}

function status(value: unknown): CandidateStatus {
  return value === 'pending' || value === 'approved' || value === 'rejected'
    ? value : invalid('candidate status');
}

function array(value: unknown, field: string): unknown[] {
  return Array.isArray(value) ? value : invalid(field);
}

function result(value: unknown): SourceResult {
  const row = object(value, 'result');
  if (!['operator_entered', 'synthetic_sales', 'synthetic_qa', 'conversation_capture'].includes(String(row.source_kind))) invalid('result source kind');
  return {
    id: string(row.id, 'result ID'), project_id: string(row.project_id, 'project ID'),
    actor_id: string(row.actor_id, 'result actor'), run_id: nullableString(row.run_id, 'run ID'),
    summary: string(row.summary, 'result summary'), scope: scope(row.scope),
    source_kind: row.source_kind as SourceResult['source_kind'], created_at: string(row.created_at, 'result time'),
    source_sales_draft_id: row.source_sales_draft_id == null ? null : string(row.source_sales_draft_id, 'Sales source ID'),
    source_runner_job_id: row.source_runner_job_id == null ? null : string(row.source_runner_job_id, 'QA source ID'),
    source_capture_id: row.source_capture_id == null ? null : string(row.source_capture_id, 'capture source ID'),
    source_sha256: row.source_sha256 == null ? null : string(row.source_sha256, 'source digest'),
  };
}

function candidate(value: unknown): Candidate {
  const row = object(value, 'candidate');
  return {
    id: string(row.id, 'candidate ID'), project_id: string(row.project_id, 'project ID'),
    actor_id: string(row.actor_id, 'candidate actor'),
    source_result_id: string(row.source_result_id, 'source result ID'),
    summary: string(row.summary, 'candidate summary'), scope: scope(row.scope),
    status: status(row.status), created_at: string(row.created_at, 'candidate time'),
    replaces_memory_id: row.replaces_memory_id == null ? null : string(row.replaces_memory_id, 'replacement ID'),
    source_capture_id: row.source_capture_id == null ? null : string(row.source_capture_id, 'capture source ID'),
    reviewed_at: nullableString(row.reviewed_at, 'review time'),
    reviewed_by_actor_id: nullableString(row.reviewed_by_actor_id, 'review actor'),
  };
}

function memory(value: unknown): MemoryItem {
  const row = object(value, 'memory item');
  if (row.state != null && !['current', 'withdrawn', 'superseded'].includes(String(row.state))) invalid('memory state');
  if (typeof row.score !== 'number' || !Number.isFinite(row.score) || row.score < 0) invalid('memory score');
  if (row.similarity != null && (typeof row.similarity !== 'number' || !Number.isFinite(row.similarity) ||
      row.similarity < -1.000001 || row.similarity > 1.000001)) invalid('memory similarity');
  return {
    id: string(row.id, 'memory ID'), project_id: string(row.project_id, 'project ID'),
    actor_id: string(row.actor_id, 'memory actor'), scope: scope(row.scope),
    text: string(row.text, 'memory text'), state: row.state == null ? 'current' : row.state as MemoryItem['state'],
    superseded_by: row.superseded_by == null ? null : string(row.superseded_by, 'superseding memory ID'),
    source_result_id: string(row.source_result_id, 'source result ID'),
    source_candidate_id: string(row.source_candidate_id, 'source candidate ID'),
    created_at: string(row.created_at, 'memory time'), score: row.score,
    ...(row.similarity == null ? {} : { similarity: row.similarity as number }),
  };
}

function projectPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}`;
}

function inProject<T extends { project_id: string }>(item: T, projectId: string): T {
  if (item.project_id !== projectId) invalid('project scope');
  return item;
}

export async function listResults(projectId: string, signal?: AbortSignal): Promise<SourceResult[]> {
  return array(await request(`${projectPath(projectId)}/results`, { signal }), 'result list')
    .map((row) => inProject(result(row), projectId));
}

export async function createResult(projectId: string, runId: string, fields: { summary: string; scope: MemoryScope }): Promise<SourceResult> {
  return inProject(result(await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/results`, {
    method: 'POST', body: JSON.stringify(fields),
  })), projectId);
}

export async function listCandidates(projectId: string, signal?: AbortSignal): Promise<Candidate[]> {
  return array(await request(`${projectPath(projectId)}/candidates`, { signal }), 'candidate list')
    .map((row) => inProject(candidate(row), projectId));
}

export async function createCandidate(projectId: string, resultId: string): Promise<Candidate> {
  return inProject(candidate(await request(`${projectPath(projectId)}/results/${encodeURIComponent(resultId)}/candidates`, {
    method: 'POST', body: '{}',
  })), projectId);
}

export async function proposeSyntheticCandidate(
  projectId: string, runId: string, selectedScope: MemoryScope,
): Promise<{ result: SourceResult; candidate: Candidate }> {
  const raw = object(await request(
    `${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/synthetic-memory-candidate`,
    { method: 'POST', body: JSON.stringify({ scope: selectedScope }) },
  ), 'synthetic proposal');
  const source = inProject(result(raw.result), projectId);
  const proposal = inProject(candidate(raw.candidate), projectId);
  if (source.run_id !== runId || proposal.source_result_id !== source.id ||
      proposal.scope !== selectedScope || source.scope !== selectedScope ||
      !['synthetic_sales', 'synthetic_qa'].includes(source.source_kind)) invalid('synthetic proposal lineage');
  return { result: source, candidate: proposal };
}

export async function reviewCandidate(projectId: string, candidateId: string, decision: 'approved' | 'rejected'): Promise<Candidate> {
  return inProject(candidate(await request(`${projectPath(projectId)}/candidates/${encodeURIComponent(candidateId)}/review`, {
    method: 'POST', body: JSON.stringify({ decision }),
  })), projectId);
}

export async function listMemory(
  projectId: string,
  options: { query?: string; scope?: MemoryScope; signal?: AbortSignal } = {},
): Promise<MemoryItem[]> {
  const params = new URLSearchParams();
  if (options.query?.trim()) params.set('query', options.query.trim());
  if (options.scope) params.set('scope', options.scope);
  const search = params.size ? `?${params.toString()}` : '';
  return array(await request(`${projectPath(projectId)}/memory${search}`, { signal: options.signal }), 'memory list')
    .map((row) => {
      const item = memory(row);
      if (options.scope && item.scope !== options.scope) invalid('memory scope');
      if (item.scope === 'project') return inProject(item, projectId);
      return item;
    });
}


export interface CapturedMessage { speaker: 'user' | 'assistant'; text: string; sent_at: string }
export interface CaptureFields {
  client_capture_id: string; source_ref: string; authorized_company_content: true;
  messages: CapturedMessage[]; summary: string; scope: MemoryScope; replaces_memory_id?: string | null;
}
export interface CaptureSource {
  id: string; project_id: string; actor_id: string; source_ref: string; messages: CapturedMessage[];
  source_sha256: string; created_at: string;
}

export async function captureConversation(projectId: string, fields: CaptureFields): Promise<Candidate> {
  const raw = object(await request(`${projectPath(projectId)}/memory/captures`, {
    method: 'POST', body: JSON.stringify(fields),
  }), 'capture receipt');
  const item = inProject(candidate(raw.candidate), projectId);
  const source = inProject(result(raw.result), projectId);
  if (source.source_kind !== 'conversation_capture' || source.source_capture_id !== raw.source_id ||
      item.source_result_id !== source.id) invalid('capture lineage');
  return item;
}

export async function readCaptureSource(projectId: string, sourceId: string): Promise<CaptureSource> {
  const row = object(await request(`${projectPath(projectId)}/memory/captures/${encodeURIComponent(sourceId)}`), 'capture source');
  return inProject({ id: string(row.id, 'capture ID'), project_id: string(row.project_id, 'source project'),
    actor_id: string(row.actor_id, 'source actor'), source_ref: string(row.source_ref, 'source reference'),
    source_sha256: string(row.source_sha256, 'source digest'), created_at: string(row.created_at, 'capture time'),
    messages: array(row.messages, 'captured messages').map((value) => {
      const message = object(value, 'captured message');
      if (message.speaker !== 'user' && message.speaker !== 'assistant') invalid('visible speaker');
      return { speaker: message.speaker as CapturedMessage['speaker'], text: string(message.text, 'excerpt'), sent_at: string(message.sent_at, 'source time') };
    }),
  }, projectId);
}

export async function readMemory(projectId: string, memoryId: string): Promise<MemoryItem> {
  return memory(await request(`${projectPath(projectId)}/memory/${encodeURIComponent(memoryId)}?include_history=true`));
}

export async function listMemoryHistory(projectId: string, signal?: AbortSignal): Promise<MemoryItem[]> {
  return array(await request(`${projectPath(projectId)}/memory/history`, { signal }), 'memory history')
    .map((row) => inProject(memory(row), projectId));
}

export async function transitionMemory(projectId: string, memoryId: string, action: 'withdraw' | 'restore', reason: string): Promise<MemoryItem> {
  return inProject(memory(await request(`${projectPath(projectId)}/memory/${encodeURIComponent(memoryId)}/state`, {
    method: 'POST', body: JSON.stringify({ action, reason }),
  })), projectId);
}
