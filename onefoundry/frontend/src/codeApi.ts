import { ApiError, request } from './api';
import { parseApproval, parseRun, type Approval, type Run } from './workApi';

export type CodeStage = 'engineer' | 'qa' | 'merge';
export type CodeJobStatus = 'queued' | 'done' | 'failed' | 'unknown';

export interface CodeEligibility {
  eligible: boolean;
  reason: string | null;
  fixture_id: 'engineer_qa_v1';
  engineer_profile_id: string | null;
  qa_profile_id: string | null;
}

export interface CodeTest {
  name: string;
  status: 'passed' | 'failed';
  exit_code: number;
}

export interface CodeArtifact {
  id: string;
  kind: string;
  path: string;
  sha256: string;
  byte_size: number;
}

export interface CodeJob {
  id: string;
  project_id: string;
  run_id: string;
  source_job_id: string | null;
  approval_id: string | null;
  stage: CodeStage;
  status: CodeJobStatus;
  reason_code: string | null;
  branch: string | null;
  commit: string | null;
  baseline_commit: string | null;
  merged_commit: string | null;
  observed_head_commit: string | null;
  diff_sha256: string | null;
  tests: CodeTest[];
  artifacts: CodeArtifact[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface CodeDetail {
  run: Run;
  jobs: CodeJob[];
  synthetic_merge_approval_id: string | null;
}

export interface ArtifactPreview {
  kind: string;
  sha256: string;
  byte_size: number;
  text: string;
}

function invalid(field: string): never {
  throw new ApiError(`The server returned an invalid ${field}.`, null);
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid(field);
  return value as Record<string, unknown>;
}

function string(value: unknown, field: string): string {
  return typeof value === 'string' ? value : invalid(field);
}

function nullableString(value: unknown, field: string): string | null {
  return value === null ? null : string(value, field);
}

function enumValue<const T extends readonly string[]>(value: unknown, values: T, field: string): T[number] {
  return typeof value === 'string' && values.includes(value) ? value as T[number] : invalid(field);
}

function array(value: unknown, field: string): unknown[] {
  return Array.isArray(value) ? value : invalid(field);
}

function nonnegativeInteger(value: unknown, field: string): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : invalid(field);
}

function integer(value: unknown, field: string): number {
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : invalid(field);
}

function codeEligibility(value: unknown): CodeEligibility {
  const row = record(value, 'synthetic code eligibility');
  if (typeof row.eligible !== 'boolean') invalid('synthetic code eligibility');
  return {
    eligible: row.eligible, reason: nullableString(row.reason, 'eligibility reason'),
    fixture_id: enumValue(row.fixture_id, ['engineer_qa_v1'], 'fixture ID'),
    engineer_profile_id: nullableString(row.engineer_profile_id, 'Engineer profile ID'),
    qa_profile_id: nullableString(row.qa_profile_id, 'QA profile ID'),
  };
}

function codeTest(value: unknown): CodeTest {
  const row = record(value, 'test result');
  return {
    name: string(row.name, 'test name'),
    status: enumValue(row.status, ['passed', 'failed'], 'test status'),
    exit_code: integer(row.exit_code, 'test exit code'),
  };
}

function artifact(value: unknown): CodeArtifact {
  const row = record(value, 'artifact');
  return {
    id: string(row.id, 'artifact ID'), kind: string(row.kind, 'artifact kind'),
    path: string(row.path, 'artifact path'), sha256: string(row.sha256, 'artifact SHA-256'),
    byte_size: nonnegativeInteger(row.byte_size, 'artifact size'),
  };
}

function codeJob(value: unknown): CodeJob {
  const row = record(value, 'runner job');
  return {
    id: string(row.id, 'job ID'), project_id: string(row.project_id, 'project ID'),
    run_id: string(row.run_id, 'run ID'),
    source_job_id: nullableString(row.source_job_id, 'source job ID'),
    approval_id: nullableString(row.approval_id, 'job approval ID'),
    stage: enumValue(row.stage, ['engineer', 'qa', 'merge'], 'job stage'),
    status: enumValue(row.status, ['queued', 'done', 'failed', 'unknown'], 'job status'),
    reason_code: nullableString(row.reason_code, 'job reason code'),
    branch: nullableString(row.branch, 'branch'), commit: nullableString(row.commit, 'commit'),
    baseline_commit: nullableString(row.baseline_commit, 'baseline commit'),
    merged_commit: nullableString(row.merged_commit, 'merged commit'),
    observed_head_commit: nullableString(row.observed_head_commit, 'observed target HEAD'),
    diff_sha256: nullableString(row.diff_sha256, 'diff SHA-256'),
    tests: array(row.tests, 'test results').map(codeTest),
    artifacts: array(row.artifacts, 'artifacts').map(artifact),
    created_at: string(row.created_at, 'job creation time'),
    started_at: nullableString(row.started_at, 'job start time'),
    finished_at: nullableString(row.finished_at, 'job finish time'),
  };
}

function codeDetail(value: unknown, projectId: string, runId?: string): CodeDetail {
  const row = record(value, 'synthetic code detail');
  const savedRun = parseRun(row.run);
  if (savedRun.project_id !== projectId || savedRun.mode !== 'synthetic_code' ||
    (runId && savedRun.id !== runId)) invalid('synthetic code scope');
  const jobs = array(row.jobs, 'runner jobs').map(codeJob);
  if (jobs.some((job) => job.project_id !== projectId || job.run_id !== savedRun.id)) invalid('runner job scope');
  return {
    run: savedRun, jobs,
    synthetic_merge_approval_id: nullableString(row.synthetic_merge_approval_id, 'merge approval ID'),
  };
}

function projectPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}`;
}

function runPath(projectId: string, runId: string): string {
  return `${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/synthetic-code`;
}

export async function getCodeEligibility(projectId: string, workId: string, signal?: AbortSignal): Promise<CodeEligibility> {
  return codeEligibility(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/synthetic-code/eligibility`, { signal }));
}

export async function launchSyntheticCode(projectId: string, workId: string): Promise<CodeDetail> {
  const value = await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/synthetic-code/runs`, {
    method: 'POST', body: JSON.stringify({ fixture_id: 'engineer_qa_v1' }),
  });
  const detail = codeDetail(value, projectId);
  if (detail.run.work_order_id !== workId) invalid('work order scope');
  return detail;
}

export async function getSyntheticCode(projectId: string, runId: string, signal?: AbortSignal): Promise<CodeDetail> {
  return codeDetail(await request(runPath(projectId, runId), { signal }), projectId, runId);
}

export async function reconcileSyntheticCode(projectId: string, runId: string): Promise<CodeDetail> {
  return codeDetail(await request(`${runPath(projectId, runId)}/reconcile`, {
    method: 'POST', body: '{}',
  }), projectId, runId);
}

export async function createMergeProposal(projectId: string, runId: string): Promise<Approval> {
  const approval = parseApproval(await request(`${runPath(projectId, runId)}/merge-approval`, {
    method: 'POST', body: '{}',
  }));
  if (approval.project_id !== projectId || approval.run_id !== runId ||
    approval.action_type !== 'merge' || !approval.synthetic_merge) invalid('merge approval scope');
  return approval;
}

export async function getArtifactPreview(projectId: string, runId: string, saved: CodeArtifact, signal?: AbortSignal): Promise<ArtifactPreview> {
  const row = record(await request(`${runPath(projectId, runId)}/artifacts/${encodeURIComponent(saved.id)}`, { signal }), 'artifact preview');
  const preview = {
    kind: string(row.kind, 'artifact kind'), sha256: string(row.sha256, 'artifact SHA-256'),
    byte_size: nonnegativeInteger(row.byte_size, 'artifact size'), text: string(row.text, 'artifact text'),
  };
  if (preview.kind !== saved.kind || preview.sha256 !== saved.sha256 || preview.byte_size !== saved.byte_size) {
    invalid('artifact preview identity');
  }
  return preview;
}
