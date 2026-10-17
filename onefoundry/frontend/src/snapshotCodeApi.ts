import { ApiError, request } from './api';
import { parseExternalCodeDetail, type ExternalCodeDetail } from './externalCodeApi';

export interface SavedFixtureSnapshot {
  snapshot_attempt_id: string;
  approval_id: string;
  source_id: string;
  account_id: string;
  owner: string;
  repo: string;
  branch: string;
  saved_revision: string;
  source_tree: string;
  snapshot_path: string;
  snapshot_sha256: string;
  snapshot_byte_size: number;
  receipt_sha256: string;
}

export interface SnapshotCodeEligibility {
  eligible: boolean;
  reason: string | null;
  snapshots: SavedFixtureSnapshot[];
}

function invalid(field: string): never { throw new ApiError(`Invalid saved snapshot ${field}.`, null); }
function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid('record');
  return value as Record<string, unknown>;
}
function string(value: unknown, field: string): string {
  return typeof value === 'string' && value.length > 0 ? value : invalid(field);
}
function sha(value: unknown, length: number, field: string): string {
  const result = string(value, field);
  return new RegExp(`^[0-9a-f]{${length}}$`).test(result) ? result : invalid(field);
}
function snapshot(value: unknown): SavedFixtureSnapshot {
  const row = record(value);
  const parsed = {
    snapshot_attempt_id: string(row.snapshot_attempt_id, 'attempt ID'),
    approval_id: string(row.approval_id, 'approval ID'),
    source_id: string(row.source_id, 'source ID'),
    account_id: string(row.account_id, 'account ID'),
    owner: string(row.owner, 'owner'), repo: string(row.repo, 'repository'),
    branch: string(row.branch, 'branch'), saved_revision: sha(row.saved_revision, 40, 'revision'),
    source_tree: sha(row.source_tree, 40, 'tree'),
    snapshot_path: string(row.snapshot_path, 'path'),
    snapshot_sha256: sha(row.snapshot_sha256, 64, 'hash'),
    snapshot_byte_size: row.snapshot_byte_size,
    receipt_sha256: sha(row.receipt_sha256, 64, 'receipt hash'),
  };
  if (parsed.snapshot_path !== `snapshots/${parsed.snapshot_sha256}.json` ||
      typeof parsed.snapshot_byte_size !== 'number' || !Number.isSafeInteger(parsed.snapshot_byte_size) ||
      parsed.snapshot_byte_size < 1) invalid('path or size');
  return parsed as SavedFixtureSnapshot;
}

const path = (projectId: string, orderId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(orderId)}/github-snapshot-codex`;

export async function getSnapshotCodeEligibility(projectId: string, orderId: string, signal?: AbortSignal): Promise<SnapshotCodeEligibility> {
  const row = record(await request(`${path(projectId, orderId)}/eligibility`, { signal }));
  if (typeof row.eligible !== 'boolean' || (row.reason !== null && typeof row.reason !== 'string') ||
      !Array.isArray(row.snapshots)) invalid('eligibility');
  const snapshots = row.snapshots.map(snapshot);
  if (new Set(snapshots.map((item) => item.snapshot_attempt_id)).size !== snapshots.length) invalid('duplicate attempt');
  return { eligible: row.eligible, reason: row.reason, snapshots };
}

export async function launchSnapshotCode(projectId: string, orderId: string, snapshotId: string, idempotencyKey: string): Promise<ExternalCodeDetail> {
  const detail = parseExternalCodeDetail(await request(`${path(projectId, orderId)}/runs`, {
    method: 'POST', body: JSON.stringify({ snapshot_attempt_id: snapshotId, idempotency_key: idempotencyKey }),
  }), projectId, orderId);
  if (!detail.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1' &&
    attempt.snapshot_attempt_id === snapshotId && attempt.idempotency_key === idempotencyKey)) invalid('coding launch binding');
  return detail;
}
