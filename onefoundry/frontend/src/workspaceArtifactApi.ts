import { ApiError, request } from './api';

export type WorkspaceArtifactKind = 'text' | 'markdown' | 'csv' | 'video';
export type WorkspaceArtifactStatus = 'pending' | 'ready' | 'unknown';

export interface WorkspaceArtifact {
  id: string;
  kind: WorkspaceArtifactKind;
  mime_type: string;
  sha256: string;
  byte_size: number;
  status: WorkspaceArtifactStatus;
  created_at: string;
  finished_at: string | null;
}

export interface WorkspaceArtifactPreview {
  playback_verified?: boolean;
  id: string;
  kind: WorkspaceArtifactKind;
  mime_type: string;
  sha256: string;
  byte_size: number;
  content: string;
}

function invalid(field: string): never {
  throw new ApiError(`The server returned invalid workspace artifact ${field}.`, null);
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid(field);
  return value as Record<string, unknown>;
}

function string(value: unknown, field: string): string {
  return typeof value === 'string' ? value : invalid(field);
}

function kind(value: unknown): WorkspaceArtifactKind {
  return value === 'text' || value === 'markdown' || value === 'csv' || value === 'video' ? value : invalid('kind');
}

function digest(value: unknown): string {
  const sha = string(value, 'hash');
  return /^[0-9a-f]{64}$/.test(sha) ? sha : invalid('hash');
}

function size(value: unknown, artifactKind: WorkspaceArtifactKind): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1 && (artifactKind === 'video' || value <= 65536)
    ? value : invalid('size');
}

function mime(value: unknown, artifactKind: WorkspaceArtifactKind): string {
  const expected = { text: 'text/plain', markdown: 'text/markdown', csv: 'text/csv', video: 'video/webm' }[artifactKind];
  return value === expected ? expected : invalid('type');
}

function summary(value: unknown): WorkspaceArtifact {
  const row = record(value, 'summary');
  const artifactKind = kind(row.kind);
  const status = row.status === 'pending' || row.status === 'ready' || row.status === 'unknown'
    ? row.status : invalid('status');
  const finished = row.finished_at === null ? null : string(row.finished_at, 'finish time');
  if ((status === 'pending') !== (finished === null)) invalid('finish state');
  return {
    id: string(row.id, 'ID'), kind: artifactKind, mime_type: mime(row.mime_type, artifactKind),
    sha256: digest(row.sha256), byte_size: size(row.byte_size, artifactKind), status,
    created_at: string(row.created_at, 'creation time'), finished_at: finished,
  };
}

function path(projectId: string, runId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/workspace-artifacts`;
}

export async function listWorkspaceArtifacts(
  projectId: string, runId: string, signal?: AbortSignal,
): Promise<WorkspaceArtifact[]> {
  const body = await request(path(projectId, runId), { signal });
  if (!Array.isArray(body)) invalid('list');
  const items = body.map(summary);
  if (new Set(items.map((item) => item.id)).size !== items.length) invalid('duplicate ID');
  return items;
}

export async function getWorkspaceArtifact(
  projectId: string, runId: string, saved: WorkspaceArtifact, signal?: AbortSignal,
): Promise<WorkspaceArtifactPreview> {
  if (saved.status !== 'ready') throw new ApiError('This artifact has no verified saved content.', null);
  const row = record(await request(`${path(projectId, runId)}/${encodeURIComponent(saved.id)}`, { signal }), 'preview');
  const artifactKind = kind(row.kind);
  const preview: WorkspaceArtifactPreview = {
    id: string(row.id, 'preview ID'), kind: artifactKind,
    mime_type: mime(row.mime_type, artifactKind), sha256: digest(row.sha256),
    byte_size: size(row.byte_size, artifactKind), content: string(row.content, 'content'),
  };
  if (artifactKind === 'video') {
    if (typeof row.playback_verified !== 'boolean') invalid('playback state');
    preview.playback_verified = row.playback_verified;
  }
  if (preview.id !== saved.id || preview.kind !== saved.kind || preview.mime_type !== saved.mime_type ||
    preview.sha256 !== saved.sha256 || preview.byte_size !== saved.byte_size) invalid('preview identity');
  return preview;
}
