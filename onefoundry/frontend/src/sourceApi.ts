import { ApiError, request } from './api';
import { clearSessionKey, readSessionKey } from './brandStorage';

export interface SourceRoot { id: string; label: string; available: boolean }
export interface LocalRepositoryCandidate {
  relative_path: string; display_name: string; branch: string; revision: string; dirty: boolean;
}
export interface LocalRepositoryDiscovery {
  available: boolean; truncated: boolean; repositories: LocalRepositoryCandidate[];
}
export interface ProjectSource {
  id: string; project_id: string; kind: 'local' | 'github'; display_name: string;
  root_id: string | null; relative_path: string | null; account_id: string | null;
  owner: string | null; repo: string | null; branch: string; revision: string;
  availability: string;
}
export interface GitHubAccount { id: string; project_id: string; label: string; credential_present: boolean }
export interface GitHubRepo { full_name: string; default_branch: string; private: boolean; html_url: string }
export interface GitHubSourceSnapshot {
  project_id: string; source_id: string; owner: string; repo: string; branch: string;
  saved_revision: string; current_revision: string; matches_saved_revision: boolean; checked_at: string;
}
export interface GitHubStageProposal {
  approval_id: string; decision: 'pending' | 'approved' | 'rejected';
  source_current: boolean; stage_status: 'not_launched' | 'queued' | 'done' | 'unknown';
  mode: 'offline_synthetic_fixture'; attempt_id: string | null; receipt_sha256: string | null;
  source_tree: string | null; file_count: number | null; total_bytes: number | null; reason_code: string | null;
  source: { project_id: string; source_id: string; account_id: string; owner: string;
    repo: string; branch: string; saved_revision: string; kind: 'github_stage_v1' };
}
export interface GitHubFixtureFetch {
  approval_id: string; decision: 'pending' | 'approved' | 'rejected';
  source_current: boolean; fetch_status: 'not_launched' | 'queued' | 'done' | 'unknown';
  mode: 'synthetic_local_https_verification_only'; action: 'verify_local_https_fixture';
  expires_at: string; expired: boolean; idempotency_key: string;
  attempt_id: string | null; receipt_sha256: string | null; source_revision: string | null;
  source_tree: string | null; file_count: number | null; total_bytes: number | null;
  reason_code: string | null;
  source: { project_id: string; source_id: string; account_id: string; owner: string;
    repo: string; branch: string; saved_revision: string; kind: 'github_fixture_fetch_v2' };
}
export interface GitHubFixtureSnapshot {
  approval_id: string; decision: 'pending' | 'approved' | 'rejected';
  source_current: boolean; snapshot_status: 'not_launched' | 'queued' | 'done' | 'unknown';
  mode: 'synthetic_local_https_snapshot_fixture_asserted_commit';
  action: 'materialize_local_https_fixture_snapshot';
  expires_at: string; expired: boolean; idempotency_key: string;
  attempt_id: string | null; receipt_sha256: string | null; source_revision: string | null;
  source_tree: string | null; file_count: number | null; total_bytes: number | null;
  snapshot_path: string | null; snapshot_sha256: string | null; snapshot_byte_size: number | null;
  reason_code: string | null;
  source: { project_id: string; source_id: string; account_id: string; owner: string;
    repo: string; branch: string; saved_revision: string; kind: 'github_fixture_snapshot_v3' };
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('The server returned an unexpected response.', null);
  return value as Record<string, unknown>;
}
function string(value: unknown): string {
  if (typeof value !== 'string') throw new ApiError('The server returned an unexpected response.', null);
  return value;
}
function optionalString(value: unknown): string | null {
  return value == null ? null : string(value);
}
function list(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new ApiError('The server returned an unexpected list.', null);
  return value;
}
function root(value: unknown): SourceRoot {
  const item = object(value);
  if (typeof item.available !== 'boolean') throw new ApiError('Invalid local source availability.', null);
  return { id: string(item.id), label: string(item.label), available: item.available };
}
function source(value: unknown): ProjectSource {
  const item = object(value);
  if (item.kind !== 'local' && item.kind !== 'github') throw new ApiError('Unknown project source kind.', null);
  const revision = string(item.revision);
  if (!/^[0-9a-f]{40}$/i.test(revision)) throw new ApiError('Invalid saved source revision.', null);
  return {
    id: string(item.id), project_id: string(item.project_id), kind: item.kind,
    display_name: string(item.display_name), root_id: optionalString(item.root_id),
    relative_path: optionalString(item.relative_path), account_id: optionalString(item.account_id),
    owner: optionalString(item.owner), repo: optionalString(item.repo), branch: string(item.branch),
    revision, availability: string(item.availability),
  };
}
function account(value: unknown): GitHubAccount {
  const item = object(value);
  if (typeof item.credential_present !== 'boolean') throw new ApiError('Invalid GitHub connection state.', null);
  return { id: string(item.id), project_id: string(item.project_id), label: string(item.label), credential_present: item.credential_present };
}
function repo(value: unknown): GitHubRepo {
  const item = object(value);
  if (typeof item.private !== 'boolean') throw new ApiError('Invalid GitHub repository state.', null);
  return { full_name: string(item.full_name), default_branch: string(item.default_branch), private: item.private, html_url: string(item.html_url) };
}
function githubSnapshot(value: unknown, projectId: string, sourceId: string): GitHubSourceSnapshot {
  const item = object(value);
  const saved = string(item.saved_revision);
  const current = string(item.current_revision);
  if (item.project_id !== projectId || item.source_id !== sourceId ||
    !/^[0-9a-f]{40}$/.test(saved) || !/^[0-9a-f]{40}$/.test(current) ||
    typeof item.matches_saved_revision !== 'boolean' || item.matches_saved_revision !== (saved === current) ||
    typeof item.checked_at !== 'string' || Number.isNaN(Date.parse(item.checked_at))) {
    throw new ApiError('Invalid GitHub source snapshot.', null);
  }
  return { project_id: projectId, source_id: sourceId, owner: string(item.owner), repo: string(item.repo),
    branch: string(item.branch), saved_revision: saved, current_revision: current,
    matches_saved_revision: item.matches_saved_revision, checked_at: string(item.checked_at) };
}

const projectPath = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}`;

export async function listSourceRoots(signal?: AbortSignal): Promise<SourceRoot[]> {
  return list(await request('/api/source-roots', { signal })).map(root);
}
export async function discoverLocalRepositories(signal?: AbortSignal): Promise<LocalRepositoryDiscovery> {
  const item = object(await request('/api/source-roots/workspace/repositories', { signal }));
  if (typeof item.available !== 'boolean' || typeof item.truncated !== 'boolean') throw new ApiError('Invalid local repository discovery state.', null);
  const repositories = list(item.repositories).map((value): LocalRepositoryCandidate => {
    const candidate = object(value);
    const revision = string(candidate.revision);
    if (!/^[0-9a-f]{40}$/i.test(revision) || typeof candidate.dirty !== 'boolean') throw new ApiError('Invalid local repository candidate.', null);
    return { relative_path: string(candidate.relative_path), display_name: string(candidate.display_name), branch: string(candidate.branch), revision, dirty: candidate.dirty };
  });
  return { available: item.available, truncated: item.truncated, repositories };
}
export async function listProjectSources(projectId: string, signal?: AbortSignal): Promise<ProjectSource[]> {
  return list(await request(`${projectPath(projectId)}/sources`, { signal })).map(source);
}
export async function addLocalSource(projectId: string, rootId: string, relativePath: string): Promise<ProjectSource> {
  return source(await request(`${projectPath(projectId)}/sources/local`, {
    method: 'POST', body: JSON.stringify({ root_id: rootId, relative_path: relativePath }),
  }));
}
export async function listGitHubAccounts(projectId: string, signal?: AbortSignal): Promise<GitHubAccount[]> {
  return list(await request(`${projectPath(projectId)}/github/accounts`, { signal })).map(account);
}
export async function addGitHubAccount(projectId: string, label: string, token: string): Promise<GitHubAccount> {
  return account(await request(`${projectPath(projectId)}/github/accounts`, {
    method: 'POST', body: JSON.stringify({ label, token }),
  }));
}
export async function listGitHubRepos(projectId: string, accountId: string): Promise<GitHubRepo[]> {
  return list(await request(`${projectPath(projectId)}/github/accounts/${encodeURIComponent(accountId)}/repos`)).map(repo);
}
export async function addGitHubSource(projectId: string, accountId: string, fullName: string, branch: string): Promise<ProjectSource> {
  const [owner, repoName] = fullName.split('/');
  if (!owner || !repoName || fullName.split('/').length !== 2) throw new ApiError('Choose a repository from the saved list.', null);
  return source(await request(`${projectPath(projectId)}/sources/github`, {
    method: 'POST', body: JSON.stringify({ account_id: accountId, owner, repo: repoName, branch }),
  }));
}

export async function checkGitHubSourceSnapshot(projectId: string, sourceId: string): Promise<GitHubSourceSnapshot> {
  return githubSnapshot(await request(`${projectPath(projectId)}/tools/github/source-snapshot`, {
    method: 'POST', body: JSON.stringify({ source_id: sourceId }),
  }), projectId, sourceId);
}

export async function proposeGitHubStage(projectId: string, runId: string, sourceId: string): Promise<string> {
  const value = object(await request(`${projectPath(projectId)}/tools/github/stage-proposals`, {
    method: 'POST', body: JSON.stringify({ run_id: runId, source_id: sourceId }),
  }));
  if (value.stage_status !== 'not_launched') throw new ApiError('Unexpected GitHub stage status.', null);
  const approval = object(value.approval);
  const source = object(value.source);
  if (source.project_id !== projectId || source.source_id !== sourceId || approval.status !== 'pending') {
    throw new ApiError('GitHub stage proposal readback mismatched.', null);
  }
  return string(approval.id);
}

export async function proposeGitHubFixtureFetch(projectId: string, runId: string, sourceId: string): Promise<string> {
  const storageKey = `onefoundry.fixture-fetch-v2.${projectId}.${runId}.${sourceId}`;
  let idempotencyKey: string;
  try {
    const saved = readSessionKey(storageKey);
    idempotencyKey = saved && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(saved)
      ? saved : crypto.randomUUID();
    sessionStorage.setItem(storageKey, idempotencyKey);
  } catch {
    throw new ApiError('Browser session storage is unavailable; fixture proposal cannot safely retry.', null);
  }
  const value = object(await request(`${projectPath(projectId)}/tools/github/fixture-fetch-v2`, {
    method: 'POST', body: JSON.stringify({ run_id: runId, source_id: sourceId, idempotency_key: idempotencyKey }),
  }));
  const approval = object(value.approval);
  const source = object(value.source);
  if (value.fetch_status !== 'not_launched' || approval.status !== 'pending' ||
      source.kind !== 'github_fixture_fetch_v2' || source.project_id !== projectId || source.source_id !== sourceId) {
    throw new ApiError('Fixture fetch proposal readback mismatched.', null);
  }
  return string(approval.id);
}

export function confirmGitHubFixtureFetchProposal(projectId: string, runId: string, sourceId: string): void {
  clearSessionKey(`onefoundry.fixture-fetch-v2.${projectId}.${runId}.${sourceId}`);
}

export async function proposeGitHubFixtureSnapshot(projectId: string, runId: string, sourceId: string): Promise<string> {
  const storageKey = `onefoundry.fixture-snapshot-v3.${projectId}.${runId}.${sourceId}`;
  let idempotencyKey: string;
  try {
    const saved = readSessionKey(storageKey);
    idempotencyKey = saved && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(saved)
      ? saved : crypto.randomUUID();
    sessionStorage.setItem(storageKey, idempotencyKey);
  } catch {
    throw new ApiError('Browser session storage is unavailable; fixture snapshot proposal cannot safely retry.', null);
  }
  const value = object(await request(`${projectPath(projectId)}/tools/github/fixture-snapshot-v3`, {
    method: 'POST', body: JSON.stringify({ run_id: runId, source_id: sourceId, idempotency_key: idempotencyKey }),
  }));
  const approval = object(value.approval);
  const source = object(value.source);
  if (value.snapshot_status !== 'not_launched' || approval.status !== 'pending' ||
      source.kind !== 'github_fixture_snapshot_v3' || source.project_id !== projectId || source.source_id !== sourceId) {
    throw new ApiError('Fixture snapshot proposal readback mismatched.', null);
  }
  return string(approval.id);
}

export function confirmGitHubFixtureSnapshotProposal(projectId: string, runId: string, sourceId: string): void {
  clearSessionKey(`onefoundry.fixture-snapshot-v3.${projectId}.${runId}.${sourceId}`);
}

function githubFixtureSnapshot(value: unknown, projectId: string, approvalId: string): GitHubFixtureSnapshot {
  const item = object(value);
  const source = object(item.source);
  if (item.approval_id !== approvalId || source.project_id !== projectId ||
      source.kind !== 'github_fixture_snapshot_v3' || item.action !== 'materialize_local_https_fixture_snapshot' ||
      item.mode !== 'synthetic_local_https_snapshot_fixture_asserted_commit' ||
      !['not_launched', 'queued', 'done', 'unknown'].includes(string(item.snapshot_status)) ||
      !['pending', 'approved', 'rejected'].includes(string(item.decision)) ||
      typeof item.source_current !== 'boolean' || typeof item.expired !== 'boolean' ||
      Number.isNaN(Date.parse(string(item.expires_at))) ||
      (item.snapshot_status === 'done' &&
        (typeof item.snapshot_sha256 !== 'string' || typeof item.snapshot_path !== 'string' ||
         typeof item.snapshot_byte_size !== 'number' || typeof item.source_tree !== 'string'))) {
    throw new ApiError('Fixture snapshot status readback mismatched.', null);
  }
  return item as unknown as GitHubFixtureSnapshot;
}

export async function getGitHubFixtureSnapshot(projectId: string, approvalId: string): Promise<GitHubFixtureSnapshot> {
  return githubFixtureSnapshot(await request(`${projectPath(projectId)}/tools/github/fixture-snapshot-v3/${encodeURIComponent(approvalId)}`), projectId, approvalId);
}

export async function dispatchGitHubFixtureSnapshot(projectId: string, approvalId: string): Promise<GitHubFixtureSnapshot> {
  return githubFixtureSnapshot(await request(`${projectPath(projectId)}/tools/github/fixture-snapshot-v3/${encodeURIComponent(approvalId)}/dispatch`, {
    method: 'POST',
  }), projectId, approvalId);
}

function githubFixtureFetch(value: unknown, projectId: string, approvalId: string): GitHubFixtureFetch {
  const item = object(value);
  const source = object(item.source);
  if (item.approval_id !== approvalId || source.project_id !== projectId ||
      source.kind !== 'github_fixture_fetch_v2' || item.action !== 'verify_local_https_fixture' ||
      item.mode !== 'synthetic_local_https_verification_only' ||
      !['not_launched', 'queued', 'done', 'unknown'].includes(string(item.fetch_status)) ||
      !['pending', 'approved', 'rejected'].includes(string(item.decision)) ||
      typeof item.source_current !== 'boolean' || typeof item.expired !== 'boolean' ||
      Number.isNaN(Date.parse(string(item.expires_at)))) {
    throw new ApiError('Fixture fetch status readback mismatched.', null);
  }
  return item as unknown as GitHubFixtureFetch;
}

export async function getGitHubFixtureFetch(projectId: string, approvalId: string): Promise<GitHubFixtureFetch> {
  return githubFixtureFetch(await request(`${projectPath(projectId)}/tools/github/fixture-fetch-v2/${encodeURIComponent(approvalId)}`), projectId, approvalId);
}

export async function dispatchGitHubFixtureFetch(projectId: string, approvalId: string): Promise<GitHubFixtureFetch> {
  return githubFixtureFetch(await request(`${projectPath(projectId)}/tools/github/fixture-fetch-v2/${encodeURIComponent(approvalId)}/dispatch`, {
    method: 'POST',
  }), projectId, approvalId);
}

export async function getGitHubStageProposal(projectId: string, approvalId: string): Promise<GitHubStageProposal> {
  const value = object(await request(`${projectPath(projectId)}/tools/github/stage-proposals/${encodeURIComponent(approvalId)}`));
  return githubStageStatus(value, projectId, approvalId);
}

function githubStageStatus(value: Record<string, unknown>, projectId: string, approvalId: string): GitHubStageProposal {
  const source = object(value.source);
  if (value.approval_id !== approvalId || source.project_id !== projectId
      || typeof value.source_current !== 'boolean'
      || !['pending', 'approved', 'rejected'].includes(string(value.decision))
      || !['not_launched', 'queued', 'done', 'unknown'].includes(string(value.stage_status))
      || value.mode !== 'offline_synthetic_fixture' || source.kind !== 'github_stage_v1'
      || (value.stage_status === 'not_launched' && value.attempt_id != null)
      || (value.stage_status !== 'not_launched' && typeof value.attempt_id !== 'string')
      || (value.stage_status === 'done' && (typeof value.receipt_sha256 !== 'string' || typeof value.source_tree !== 'string'))
      || (value.stage_status === 'unknown' && typeof value.reason_code !== 'string')) {
    throw new ApiError('GitHub stage proposal readback mismatched.', null);
  }
  return value as unknown as GitHubStageProposal;
}

export async function dispatchGitHubStage(projectId: string, approvalId: string): Promise<GitHubStageProposal> {
  const value = object(await request(`${projectPath(projectId)}/tools/github/stage-proposals/${encodeURIComponent(approvalId)}/dispatch`, { method: 'POST' }));
  const stage = githubStageStatus(value, projectId, approvalId);
  if (stage.stage_status === 'not_launched' || stage.decision !== 'approved') {
    throw new ApiError('Offline stage dispatch readback was not confirmed. Check saved status before retrying.', null);
  }
  return stage;
}

async function remove(path: string): Promise<void> {
  let response: Response;
  try { response = await fetch(path, { method: 'DELETE', credentials: 'same-origin' }); }
  catch { throw new ApiError('Could not reach OneFoundry. Check saved state before retrying.', null); }
  if (response.status === 204) return;
  let detail: unknown;
  try { detail = (await response.json() as { detail?: unknown }).detail; } catch { /* Response may not be JSON. */ }
  throw new ApiError(typeof detail === 'string' ? detail : `Request failed (${response.status}). Check saved state before retrying.`, response.status);
}

export async function removeProjectSource(projectId: string, sourceId: string): Promise<void> {
  await remove(`${projectPath(projectId)}/sources/${encodeURIComponent(sourceId)}`);
}

export async function removeGitHubAccount(projectId: string, accountId: string): Promise<void> {
  await remove(`${projectPath(projectId)}/github/accounts/${encodeURIComponent(accountId)}`);
}
