import { ApiError, request } from './api';

export type InputScope = 'project' | 'work_order' | 'staged';

export interface InputFileMeta {
  id: string;
  project_id: string;
  display_name: string;
  extension: string;
  mime_type: string;
  sha256: string;
  byte_size: number;
  status: 'pending' | 'ready_text' | 'ready_visual' | 'rejected';
  reason_code: string | null;
  extractor_version: string | null;
}

export interface PendingInputFile { id: string; file: File }

const allowed = new Set(['txt', 'md', 'csv', 'tsv', 'json', 'yaml', 'yml', 'xml', 'html', 'htm',
  'pdf', 'docx', 'xlsx', 'pptx', 'jpg', 'jpeg', 'png', 'webp', 'doc', 'xls', 'ppt', 'rtf', 'odt', 'ods', 'odp']);

function path(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/input-files`;
}

function parse(value: unknown, projectId: string): InputFileMeta {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ApiError('Invalid file readback.', null);
  const row = value as Record<string, unknown>;
  if (row.project_id !== projectId || typeof row.id !== 'string' || typeof row.display_name !== 'string' ||
      typeof row.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(row.sha256) ||
      typeof row.byte_size !== 'number' || !Number.isSafeInteger(row.byte_size) || row.byte_size < 1 ||
      !['pending', 'ready_text', 'ready_visual', 'rejected'].includes(String(row.status))) {
    throw new ApiError('Invalid file readback.', null);
  }
  return row as unknown as InputFileMeta;
}

export function chooseInputFiles(files: FileList | null, limit: number): PendingInputFile[] {
  const selected = Array.from(files ?? []);
  if (selected.length > limit) throw new ApiError(`Choose up to ${limit} files.`, null);
  for (const file of selected) {
    const extension = file.name.split('.').at(-1)?.toLowerCase() ?? '';
    if (!allowed.has(extension)) throw new ApiError(`${file.name}: unsupported file format.`, null);
    if (!file.size || file.size > 25 * 1024 * 1024) throw new ApiError(`${file.name}: file must be 1 to 25 MiB.`, null);
  }
  return selected.map((file) => ({ id: crypto.randomUUID(), file }));
}

export async function getInputFile(projectId: string, id: string): Promise<InputFileMeta> {
  return parse(await request(`${path(projectId)}/${encodeURIComponent(id)}`), projectId);
}

export async function listInputFiles(projectId: string, orderId?: string): Promise<InputFileMeta[]> {
  const query = orderId ? `?work_order_id=${encodeURIComponent(orderId)}` : '';
  const rows: unknown = await request(path(projectId) + query);
  if (!Array.isArray(rows)) throw new ApiError('Invalid file list readback.', null);
  return rows.map((row) => parse(row, projectId));
}

function responseError(body: unknown, status: number): ApiError {
  const detail = typeof body === 'object' && body !== null && 'detail' in body ?
    (body as { detail: unknown }).detail : null;
  return new ApiError(typeof detail === 'string' ? detail : `File upload failed (${status}).`, status);
}

async function postInputFile(projectId: string, pending: PendingInputFile, scope: InputScope,
  orderId?: string): Promise<InputFileMeta> {
  const params = new URLSearchParams({ scope });
  if (orderId) params.set('work_order_id', orderId);
  const response = await fetch(`${path(projectId)}?${params}`, {
    method: 'POST', body: pending.file, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/octet-stream', 'X-File-Name': encodeURIComponent(pending.file.name),
      'X-Client-File-Id': pending.id },
  });
  let body: unknown;
  try { body = await response.json(); }
  catch { throw new ApiError(`File upload returned HTTP ${response.status} without saved state.`, response.status); }
  if (!response.ok) throw responseError(body, response.status);
  const saved = parse(body, projectId);
  if (saved.id !== pending.id) throw new ApiError('File upload ID did not match its saved owner.', null);
  return saved;
}

async function fileHash(file: File): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function exactReadback(projectId: string, pending: PendingInputFile): Promise<InputFileMeta | null> {
  let saved: InputFileMeta;
  try { saved = await getInputFile(projectId, pending.id); }
  catch (error) { if (error instanceof ApiError && error.status === 404) return null; throw error; }
  if (saved.display_name !== pending.file.name || saved.byte_size !== pending.file.size ||
      saved.sha256 !== await fileHash(pending.file)) {
    throw new ApiError(`${pending.file.name}: saved file identity differs from this selection.`, null);
  }
  return saved;
}

export async function uploadInputFiles(projectId: string, pending: PendingInputFile[], scope: InputScope,
  orderId?: string): Promise<InputFileMeta[]> {
  const saved: InputFileMeta[] = [];
  for (const item of pending) {
    let row = await exactReadback(projectId, item);
    if (!row) {
      try { row = await postInputFile(projectId, item, scope, orderId); }
      catch (error) {
        row = await exactReadback(projectId, item);
        if (!row) throw error;
      }
    }
    saved.push(row);
  }
  return saved;
}

export async function checkInputIdsReady(projectId: string, ids: string[]): Promise<InputFileMeta[]> {
  const files = await Promise.all(ids.map((id) => getInputFile(projectId, id)));
  const rejected = files.find((item) => item.status === 'rejected');
  if (rejected) throw new ApiError(`${rejected.display_name}: processing failed (${(rejected.reason_code ?? 'unknown').replaceAll('_', ' ')}).`, null);
  const processing = files.find((item) => item.status === 'pending');
  if (processing) throw new ApiError(`${processing.display_name} is still processing. Check files and retry Send.`, null);
  return files;
}
