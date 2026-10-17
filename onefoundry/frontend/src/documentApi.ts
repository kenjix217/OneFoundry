import { ApiError, request } from './api';

export interface DocumentSource {
  id: string;
  connector_account_id: string;
  provider: 'onedrive' | 'gdrive';
  label: string;
  folder_ref: string;
  folder_name: string | null;
  enabled: boolean;
  status: 'unverified' | 'ready' | 'failed';
  last_error_code: string | null;
  last_poll_at: string | null;
  version: number;
}

function bad(): never {
  throw new ApiError('The server returned an unexpected folder response.', null);
}

function source(value: unknown): DocumentSource {
  if (!value || typeof value !== 'object' || Array.isArray(value)) bad();
  const row = value as Record<string, unknown>;
  const text = (key: string) => { if (typeof row[key] !== 'string') bad(); return row[key] as string; };
  const maybe = (key: string) => row[key] === null ? null : text(key);
  const provider = text('provider');
  const status = text('status');
  if ((provider !== 'onedrive' && provider !== 'gdrive') || !['unverified', 'ready', 'failed'].includes(status)) bad();
  if (typeof row.enabled !== 'boolean' || !Number.isSafeInteger(row.version)) bad();
  return { id: text('id'), connector_account_id: text('connector_account_id'), provider, label: text('label'),
    folder_ref: text('folder_ref'), folder_name: maybe('folder_name'), enabled: row.enabled, status: status as DocumentSource['status'],
    last_error_code: maybe('last_error_code'), last_poll_at: maybe('last_poll_at'), version: row.version as number };
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/documents/sources`;

export async function listDocumentSources(projectId: string, signal?: AbortSignal): Promise<DocumentSource[]> {
  const value = await request(base(projectId), { signal });
  if (!Array.isArray(value)) bad();
  return value.map(source);
}

export async function createDocumentSource(projectId: string, fields: { label: string; provider: 'onedrive' | 'gdrive';
  connector_account_id: string; folder_ref: string }): Promise<DocumentSource> {
  return source(await request(base(projectId), { method: 'POST', body: JSON.stringify(fields) }));
}

export async function probeDocumentSource(projectId: string, id: string): Promise<DocumentSource> {
  return source(await request(`${base(projectId)}/${encodeURIComponent(id)}/probe`, { method: 'POST', body: '{}' }));
}

export async function setDocumentSourceEnabled(projectId: string, id: string, enabled: boolean, expectedVersion: number): Promise<DocumentSource> {
  return source(await request(`${base(projectId)}/${encodeURIComponent(id)}/enabled`, {
    method: 'POST', body: JSON.stringify({ enabled, expected_version: expectedVersion }),
  }));
}

export async function removeDocumentSource(projectId: string, id: string, expectedVersion: number): Promise<void> {
  const response = await fetch(`${base(projectId)}/${encodeURIComponent(id)}?expected_version=${expectedVersion}`, {
    method: 'DELETE', credentials: 'same-origin',
  });
  if (response.status !== 204) throw new ApiError(`Removing the folder failed (${response.status}). Refresh before retrying.`, response.status);
}

export interface DocumentWrite {
  id: string;
  file_name: string;
  mime_type: string;
  state: 'pending' | 'approved' | 'rejected' | 'dispatching' | 'done' | 'failed' | 'unknown';
  error_code: string | null;
  provider_file_id: string | null;
  readback: { verified?: boolean } | null;
}

function write(value: unknown): DocumentWrite {
  if (!value || typeof value !== 'object' || Array.isArray(value)) bad();
  const row = value as Record<string, unknown>;
  const text = (key: string) => { if (typeof row[key] !== 'string') bad(); return row[key] as string; };
  const state = text('state');
  if (!['pending', 'approved', 'rejected', 'dispatching', 'done', 'failed', 'unknown'].includes(state)) bad();
  return { id: text('id'), file_name: text('file_name'), mime_type: text('mime_type'), state: state as DocumentWrite['state'],
    error_code: row.error_code === null ? null : text('error_code'),
    provider_file_id: row.provider_file_id === null ? null : text('provider_file_id'),
    readback: row.readback && typeof row.readback === 'object' ? row.readback as { verified?: boolean } : null };
}

const writesBase = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/documents/writes`;

export async function listDocumentWrites(projectId: string, signal?: AbortSignal): Promise<DocumentWrite[]> {
  const value = await request(writesBase(projectId), { signal });
  if (!Array.isArray(value)) bad();
  return value.map(write);
}

export async function dispatchDocumentWrite(projectId: string, id: string): Promise<DocumentWrite> {
  return write(await request(`${writesBase(projectId)}/${encodeURIComponent(id)}/dispatch`, { method: 'POST', body: '{}' }));
}

export interface DocumentAction extends DocumentWrite {
  operation: 'replace_file' | 'edit_cells' | 'share_file';
}

export async function listDocumentActions(projectId: string, signal?: AbortSignal): Promise<DocumentAction[]> {
  const value = await request(`/api/projects/${encodeURIComponent(projectId)}/documents/actions`, { signal });
  if (!Array.isArray(value)) bad();
  return value.map((item: unknown) => {
    if (!item || typeof item !== 'object') bad();
    const row = item as Record<string, unknown>;
    if (!['replace_file', 'edit_cells', 'share_file'].includes(String(row.operation))) bad();
    return { ...write({ ...row, mime_type: '', provider_file_id: row.file_id }), operation: row.operation as DocumentAction['operation'] };
  });
}

export async function dispatchDocumentAction(projectId: string, id: string): Promise<void> {
  await request(`/api/projects/${encodeURIComponent(projectId)}/documents/actions/${encodeURIComponent(id)}/dispatch`, { method: 'POST', body: '{}' });
}

export interface DocumentMatch { file_id: string; name: string; revision: string }
export async function searchDocuments(projectId: string, sourceId: string, query: string): Promise<{ files: DocumentMatch[]; truncated: boolean }> {
  const value = await request(`/api/projects/${encodeURIComponent(projectId)}/documents/search`, { method: 'POST', body: JSON.stringify({ source_id: sourceId, query }) });
  if (!value || typeof value !== 'object') bad();
  const row = value as Record<string, unknown>;
  if (!Array.isArray(row.files) || typeof row.truncated !== 'boolean') bad();
  const files = row.files.map((item: unknown) => {
    if (!item || typeof item !== 'object') bad();
    const file = item as Record<string, unknown>;
    if (typeof file.file_id !== 'string' || typeof file.name !== 'string' || typeof file.revision !== 'string') bad();
    return { file_id: file.file_id, name: file.name, revision: file.revision };
  });
  return { files, truncated: row.truncated };
}

export async function readDocumentRange(projectId: string, sourceId: string, fileId: string, sheet: string, address: string): Promise<unknown[][]> {
  const value = await request(`/api/projects/${encodeURIComponent(projectId)}/documents/range`, { method: 'POST', body: JSON.stringify({ source_id: sourceId, file_id: fileId, sheet, address }) });
  if (!value || typeof value !== 'object') bad();
  const values = (value as Record<string, unknown>).values;
  if (!Array.isArray(values) || values.some((row: unknown) => !Array.isArray(row))) bad();
  return values as unknown[][];
}
