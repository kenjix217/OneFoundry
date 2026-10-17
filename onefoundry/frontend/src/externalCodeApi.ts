import { ApiError, request } from './api';
import { parseProviderProgress, type ProviderProgress } from './workOrderTrace';
import { parseRun, type Run } from './workApi';

export type CodingAdapterId = 'codex_cli' | 'claude_code';
export type AttemptAdapterId = CodingAdapterId | 'offline_probe';
export type ExternalCodeStatus = 'queued' | 'done' | 'failed' | 'unknown';

export interface CodingAdapterAvailability {
  adapter_id: CodingAdapterId;
  available: boolean;
  reason: string | null;
}

export interface ExternalCodeEligibility {
  eligible: boolean;
  reason: string | null;
  adapters: CodingAdapterAvailability[];
}
export interface OfflineSourceProbeEligibility { eligible: boolean; reason: string | null }

export interface ExternalCodeArtifact {
  id: string;
  kind: string;
  path: string;
  sha256: string;
  byte_size: number;
}

export interface ExternalCodeTest {
  name: string;
  status: 'passed' | 'failed';
  exit_code: number;
}

export interface ExternalCodeAttempt {
  id: string;
  project_id: string;
  run_id: string;
  adapter_id: AttemptAdapterId;
  fixture_id: 'engineer_qa_v1' | 'local_source_probe_v1' | 'github_snapshot_codex_fixture_v1' | 'repository_code_v1';
  target_id: 'engineer_qa_v1' | 'local_source_probe_v1' | 'github_snapshot_codex_fixture_v1' | 'repository_code_v1';
  source_id: string | null;
  source_root_id: string | null;
  source_relative_path: string | null;
  source_branch: string | null;
  source_revision: string | null;
  snapshot_attempt_id?: string | null;
  idempotency_key?: string | null;
  intent_sha256?: string | null;
  snapshot_source_id?: string | null;
  snapshot_account_id?: string | null;
  snapshot_owner?: string | null;
  snapshot_repo?: string | null;
  snapshot_branch?: string | null;
  snapshot_revision?: string | null;
  snapshot_tree?: string | null;
  snapshot_path?: string | null;
  snapshot_sha256?: string | null;
  snapshot_byte_size?: number | null;
  snapshot_request_sha256?: string | null;
  snapshot_receipt_sha256?: string | null;
  request_sha256: string;
  status: ExternalCodeStatus;
  reason_code: string | null;
  provider_session_id: string | null;
  baseline_commit: string | null;
  branch: string | null;
  commit: string | null;
  diff_sha256: string | null;
  tokens_used: number | null;
  cost_usd: number | null;
  receipt_sha256: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  tests: ExternalCodeTest[];
  artifacts: ExternalCodeArtifact[];
}

export interface ExternalCodeDetail {
  run: Run;
  attempts: ExternalCodeAttempt[];
}

export interface ExternalCodeArtifactPreview {
  kind: string;
  sha256: string;
  byte_size: number;
  text: string;
}

export interface ExternalCodeProgress {
  project_id: string; run_id: string; attempt_id: string; coverage: 'partial';
  latest_sequence: number; terminal: boolean;
  items: Array<ProviderProgress & { sequence: number }>;
}

function invalid(name: string): never { throw new ApiError(`The server returned an invalid ${name}.`, null); }
function record(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid(name);
  return value as Record<string, unknown>;
}
function array(value: unknown, name: string): unknown[] { return Array.isArray(value) ? value : invalid(name); }
function string(value: unknown, name: string): string { return typeof value === 'string' ? value : invalid(name); }
function nullableString(value: unknown, name: string): string | null {
  return value === null ? null : string(value, name);
}
function boolean(value: unknown, name: string): boolean { return typeof value === 'boolean' ? value : invalid(name); }
function nonnegativeInteger(value: unknown, name: string): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : invalid(name);
}
function nonnegativeNumber(value: unknown, name: string): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : invalid(name);
}
function nullableNumber(value: unknown, name: string): number | null {
  return value === null ? null : nonnegativeNumber(value, name);
}
function nullableDecimal(value: unknown, name: string): number | null {
  if (value === null) return null;
  if (typeof value === 'string' && /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?$/.test(value)) {
    return nonnegativeNumber(Number(value), name);
  }
  return nullableNumber(value, name);
}
function adapterId(value: unknown): CodingAdapterId {
  return value === 'codex_cli' || value === 'claude_code' ? value : invalid('coding adapter');
}
function attemptAdapterId(value: unknown): AttemptAdapterId {
  return value === 'offline_probe' ? value : adapterId(value);
}
function targetId(value: unknown): ExternalCodeAttempt['target_id'] {
  return value === 'engineer_qa_v1' || value === 'local_source_probe_v1' ||
    value === 'github_snapshot_codex_fixture_v1' || value === 'repository_code_v1' ? value : invalid('coding target');
}
function status(value: unknown): ExternalCodeStatus {
  return value === 'queued' || value === 'done' || value === 'failed' || value === 'unknown'
    ? value : invalid('coding attempt status');
}

function eligibility(value: unknown): ExternalCodeEligibility {
  const row = record(value, 'external code eligibility');
  const adapters = array(row.adapters, 'coding adapters').map((value) => {
    const item = record(value, 'coding adapter');
    return { adapter_id: adapterId(item.adapter_id), available: boolean(item.available, 'adapter availability'),
      reason: nullableString(item.reason, 'adapter reason') };
  });
  if (new Set(adapters.map((item) => item.adapter_id)).size !== adapters.length) invalid('coding adapters');
  return { eligible: boolean(row.eligible, 'external code eligibility'),
    reason: nullableString(row.reason, 'eligibility reason'), adapters };
}
function offlineEligibility(value: unknown): OfflineSourceProbeEligibility {
  const row = record(value, 'offline source probe eligibility');
  return { eligible: boolean(row.eligible, 'offline source probe eligibility'),
    reason: nullableString(row.reason, 'offline source probe reason') };
}

function artifact(value: unknown): ExternalCodeArtifact {
  const item = record(value, 'coding artifact');
  return { id: string(item.id, 'artifact ID'), kind: string(item.kind, 'artifact kind'),
    path: string(item.path, 'artifact path'), sha256: string(item.sha256, 'artifact SHA-256'),
    byte_size: nonnegativeInteger(item.byte_size, 'artifact size') };
}

function test(value: unknown): ExternalCodeTest {
  const item = record(value, 'coding test');
  if (item.status !== 'passed' && item.status !== 'failed') invalid('coding test status');
  if (typeof item.exit_code !== 'number' || !Number.isSafeInteger(item.exit_code)) invalid('coding test exit code');
  return { name: string(item.name, 'coding test name'), status: item.status, exit_code: item.exit_code };
}

function attempt(value: unknown, projectId: string, runId: string): ExternalCodeAttempt {
  const item = record(value, 'coding attempt');
  const parsed: ExternalCodeAttempt = {
    id: string(item.id, 'attempt ID'), project_id: string(item.project_id, 'attempt project ID'),
    run_id: string(item.run_id, 'attempt run ID'), adapter_id: attemptAdapterId(item.adapter_id),
    fixture_id: targetId(item.fixture_id), target_id: targetId(item.target_id),
    source_id: nullableString(item.source_id, 'source ID'),
    source_root_id: nullableString(item.source_root_id, 'source root ID'),
    source_relative_path: nullableString(item.source_relative_path, 'source path'),
    source_branch: nullableString(item.source_branch, 'source branch'),
    source_revision: nullableString(item.source_revision, 'source revision'),
    snapshot_attempt_id: nullableString(item.snapshot_attempt_id ?? null, 'snapshot attempt ID'),
    idempotency_key: nullableString(item.idempotency_key ?? null, 'idempotency key'),
    intent_sha256: nullableString(item.intent_sha256 ?? null, 'intent SHA-256'),
    snapshot_source_id: nullableString(item.snapshot_source_id ?? null, 'snapshot source ID'),
    snapshot_account_id: nullableString(item.snapshot_account_id ?? null, 'snapshot account ID'),
    snapshot_owner: nullableString(item.snapshot_owner ?? null, 'snapshot owner'),
    snapshot_repo: nullableString(item.snapshot_repo ?? null, 'snapshot repository'),
    snapshot_branch: nullableString(item.snapshot_branch ?? null, 'snapshot branch'),
    snapshot_revision: nullableString(item.snapshot_revision ?? null, 'snapshot revision'),
    snapshot_tree: nullableString(item.snapshot_tree ?? null, 'snapshot tree'),
    snapshot_path: nullableString(item.snapshot_path ?? null, 'snapshot path'),
    snapshot_sha256: nullableString(item.snapshot_sha256 ?? null, 'snapshot SHA-256'),
    snapshot_byte_size: item.snapshot_byte_size == null ? null : nonnegativeInteger(item.snapshot_byte_size, 'snapshot size'),
    snapshot_request_sha256: nullableString(item.snapshot_request_sha256 ?? null, 'snapshot request SHA-256'),
    snapshot_receipt_sha256: nullableString(item.snapshot_receipt_sha256 ?? null, 'snapshot receipt SHA-256'),
    request_sha256: string(item.request_sha256, 'request SHA-256'), status: status(item.status),
    reason_code: nullableString(item.reason_code, 'attempt reason'),
    provider_session_id: nullableString(item.provider_session_id, 'provider session ID'),
    baseline_commit: nullableString(item.baseline_commit, 'baseline commit'),
    branch: nullableString(item.branch, 'branch'), commit: nullableString(item.commit, 'commit'),
    diff_sha256: nullableString(item.diff_sha256, 'diff SHA-256'),
    tokens_used: item.tokens_used === null ? null : nonnegativeInteger(item.tokens_used, 'token count'),
    cost_usd: nullableDecimal(item.cost_usd, 'cost'),
    receipt_sha256: nullableString(item.receipt_sha256, 'receipt SHA-256'),
    created_at: string(item.created_at, 'attempt creation time'),
    started_at: nullableString(item.started_at, 'attempt start time'),
    finished_at: nullableString(item.finished_at, 'attempt finish time'),
    tests: array(item.tests, 'coding tests').map(test),
    artifacts: array(item.artifacts, 'coding artifacts').map(artifact),
  };
  if (parsed.project_id !== projectId || parsed.run_id !== runId) invalid('coding attempt scope');
  if (parsed.fixture_id !== parsed.target_id ||
    (parsed.target_id === 'repository_code_v1' ? parsed.adapter_id === 'offline_probe' ||
      !parsed.source_id || parsed.source_root_id !== 'workspace' || !parsed.source_branch ||
      !parsed.source_relative_path || parsed.source_relative_path.startsWith('/') ||
      parsed.source_relative_path.split('/').some((part) => !part || part === '.' || part === '..') ||
      !/^[0-9a-f]{40}$/.test(parsed.source_revision ?? '') || !parsed.idempotency_key ||
      !/^[0-9a-f]{64}$/.test(parsed.intent_sha256 ?? '') ||
      Object.entries(parsed).some(([name, value]) => name.startsWith('snapshot_') && value !== null) :
    parsed.target_id === 'github_snapshot_codex_fixture_v1' ? parsed.adapter_id !== 'codex_cli' ||
      !parsed.snapshot_attempt_id || !parsed.idempotency_key || !parsed.snapshot_source_id ||
      !parsed.snapshot_account_id || !parsed.snapshot_owner || !parsed.snapshot_repo || !parsed.snapshot_branch ||
      !/^[0-9a-f]{40}$/.test(parsed.snapshot_revision ?? '') ||
      !/^[0-9a-f]{40}$/.test(parsed.snapshot_tree ?? '') ||
      !/^[0-9a-f]{64}$/.test(parsed.snapshot_sha256 ?? '') ||
      parsed.snapshot_path !== `snapshots/${parsed.snapshot_sha256}.json` ||
      !parsed.snapshot_byte_size ||
      !/^[0-9a-f]{64}$/.test(parsed.snapshot_request_sha256 ?? '') ||
      !/^[0-9a-f]{64}$/.test(parsed.snapshot_receipt_sha256 ?? '') :
    parsed.adapter_id === 'offline_probe' ? parsed.target_id !== 'local_source_probe_v1' || !parsed.source_id ||
      !parsed.source_branch || !/^[0-9a-f]{40}$/.test(parsed.source_revision ?? '') :
      parsed.target_id !== 'engineer_qa_v1' || parsed.source_id !== null)) invalid('coding attempt target');
  return parsed;
}

export function parseExternalCodeDetail(value: unknown, projectId: string, workId?: string, runId?: string): ExternalCodeDetail {
  const row = record(value, 'external code detail');
  const run = parseRun(row.run);
  if (run.mode !== 'external_code' || run.project_id !== projectId ||
      (workId && run.work_order_id !== workId) || (runId && run.id !== runId)) invalid('external code run scope');
  const attempts = array(row.attempts, 'coding attempts').map((item) => attempt(item, projectId, run.id));
  if (attempts.length === 0) invalid('coding attempts');
  return { run, attempts };
}

const projectPath = (projectId: string): string => `/api/projects/${encodeURIComponent(projectId)}`;
const workPath = (projectId: string, workId: string): string =>
  `${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/external-code`;
const runPath = (projectId: string, runId: string): string =>
  `${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/external-code`;
const offlinePath = (projectId: string, workId: string): string =>
  `${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/offline-source-probe`;

export async function getExternalCodeEligibility(projectId: string, workId: string, signal?: AbortSignal): Promise<ExternalCodeEligibility> {
  return eligibility(await request(`${workPath(projectId, workId)}/eligibility`, { signal }));
}

export async function launchExternalCode(projectId: string, workId: string, adapterId: CodingAdapterId): Promise<ExternalCodeDetail> {
  return parseExternalCodeDetail(await request(`${workPath(projectId, workId)}/runs`, {
    method: 'POST', body: JSON.stringify({ adapter_id: adapterId }),
  }), projectId, workId);
}
export async function getOfflineSourceProbeEligibility(projectId: string, workId: string, signal?: AbortSignal): Promise<OfflineSourceProbeEligibility> {
  return offlineEligibility(await request(`${offlinePath(projectId, workId)}/eligibility`, { signal }));
}

export async function launchOfflineSourceProbe(projectId: string, workId: string, sourceId: string): Promise<ExternalCodeDetail> {
  const saved = parseExternalCodeDetail(await request(`${offlinePath(projectId, workId)}/runs`, {
    method: 'POST', body: JSON.stringify({ source_id: sourceId }),
  }), projectId, workId);
  if (!saved.attempts.some((item) => item.adapter_id === 'offline_probe' && item.source_id === sourceId)) invalid('offline source probe attempt');
  return saved;
}

export async function getExternalCode(projectId: string, runId: string, signal?: AbortSignal): Promise<ExternalCodeDetail> {
  return parseExternalCodeDetail(await request(runPath(projectId, runId), { signal }), projectId, undefined, runId);
}

export async function getExternalCodeProgress(
  projectId: string, runId: string, attemptId: string, after = 0, signal?: AbortSignal,
): Promise<ExternalCodeProgress> {
  if (!Number.isSafeInteger(after) || after < 0 || after > 24) invalid('coding progress cursor');
  const path = `${runPath(projectId, runId)}/attempts/${encodeURIComponent(attemptId)}/progress?after=${after}`;
  const row = record(await request(path, { signal }), 'coding progress');
  if (row.project_id !== projectId || row.run_id !== runId || row.attempt_id !== attemptId ||
      row.coverage !== 'partial' || typeof row.terminal !== 'boolean') invalid('coding progress scope');
  const latest = nonnegativeInteger(row.latest_sequence, 'coding progress sequence');
  if (latest > 24 || latest < after) invalid('coding progress sequence');
  const items = array(row.items, 'coding progress items').map((value) => {
    const item = record(value, 'coding progress item');
    const sequence = nonnegativeInteger(item.sequence, 'coding progress item sequence');
    if (sequence <= after || sequence > latest || sequence > 24) invalid('coding progress item sequence');
    return { ...parseProviderProgress(item), sequence };
  });
  if (items.length !== latest - after ||
      items.some((item, index) => item.sequence !== after + index + 1)) {
    invalid('coding progress order');
  }
  return { project_id: projectId, run_id: runId, attempt_id: attemptId,
    coverage: 'partial', latest_sequence: latest, terminal: row.terminal, items };
}

export async function reconcileExternalCode(projectId: string, runId: string): Promise<ExternalCodeDetail> {
  return parseExternalCodeDetail(await request(`${runPath(projectId, runId)}/reconcile`, { method: 'POST' }), projectId, undefined, runId);
}

export async function getExternalCodeArtifact(projectId: string, runId: string, artifactId: string): Promise<ExternalCodeArtifactPreview> {
  const row = record(await request(`${runPath(projectId, runId)}/artifacts/${encodeURIComponent(artifactId)}`), 'coding artifact preview');
  return { kind: string(row.kind, 'artifact kind'), sha256: string(row.sha256, 'artifact SHA-256'),
    byte_size: nonnegativeInteger(row.byte_size, 'artifact size'), text: string(row.text, 'artifact text') };
}
