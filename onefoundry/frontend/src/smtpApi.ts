import { ApiError, request } from './api';

export interface SmtpAccount {
  id: string; label: string; host: string; port: number; security: 'ssl' | 'starttls';
  username: string; from_address: string; status: 'unverified' | 'ready' | 'failed';
  enabled: boolean; last_error_code: string | null; last_probe_at: string | null;
  version: number; created_at: string; credential_present: boolean;
}

export interface OutboundEmail {
  id: string; account_id: string; run_id: string; work_order_id: string; approval_id: string;
  to_address: string; subject: string; body: string;
  state: 'pending' | 'approved' | 'rejected' | 'dispatching' | 'accepted' | 'failed' | 'unknown';
  reason_code: string | null; attempted_at: string | null; finished_at: string | null; created_at: string;
}

export interface NewSmtpAccount {
  label: string; host: string; port: number; security: 'ssl' | 'starttls';
  username: string; from_address: string; password: string;
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/mail/outgoing`;
function invalid(): never { throw new ApiError('The server returned an invalid outgoing mail response.', null); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}
function str(row: Record<string, unknown>, key: string): string {
  if (typeof row[key] !== 'string') invalid();
  return row[key] as string;
}
function nullable(row: Record<string, unknown>, key: string): string | null {
  return row[key] === null ? null : str(row, key);
}
function account(value: unknown): SmtpAccount {
  const row = object(value);
  const security = str(row, 'security'); const status = str(row, 'status');
  if ((security !== 'ssl' && security !== 'starttls') ||
      (status !== 'unverified' && status !== 'ready' && status !== 'failed') ||
      !Number.isSafeInteger(row.port) || !Number.isSafeInteger(row.version) ||
      typeof row.enabled !== 'boolean' || typeof row.credential_present !== 'boolean') invalid();
  return { id: str(row, 'id'), label: str(row, 'label'), host: str(row, 'host'), port: row.port as number,
    security, username: str(row, 'username'), from_address: str(row, 'from_address'), status,
    enabled: row.enabled, last_error_code: nullable(row, 'last_error_code'),
    last_probe_at: nullable(row, 'last_probe_at'), version: row.version as number,
    created_at: str(row, 'created_at'), credential_present: row.credential_present };
}
function email(value: unknown): OutboundEmail {
  const row = object(value); const state = str(row, 'state');
  if (!['pending','approved','rejected','dispatching','accepted','failed','unknown'].includes(state)) invalid();
  return { id: str(row, 'id'), account_id: str(row, 'account_id'), run_id: str(row, 'run_id'),
    work_order_id: str(row, 'work_order_id'), approval_id: str(row, 'approval_id'),
    to_address: str(row, 'to_address'), subject: str(row, 'subject'), body: str(row, 'body'),
    state: state as OutboundEmail['state'], reason_code: nullable(row, 'reason_code'),
    attempted_at: nullable(row, 'attempted_at'), finished_at: nullable(row, 'finished_at'),
    created_at: str(row, 'created_at') };
}
export async function listSmtpAccounts(projectId: string, signal?: AbortSignal): Promise<SmtpAccount[]> {
  const value = await request(`${base(projectId)}/accounts`, { signal });
  if (!Array.isArray(value)) invalid(); return value.map(account);
}
export async function createSmtpAccount(projectId: string, fields: NewSmtpAccount): Promise<SmtpAccount> {
  return account(await request(`${base(projectId)}/accounts`, { method: 'POST', body: JSON.stringify(fields) }));
}
export async function probeSmtpAccount(projectId: string, id: string): Promise<SmtpAccount> {
  return account(await request(`${base(projectId)}/accounts/${encodeURIComponent(id)}/probe`, { method: 'POST', body: '{}' }));
}
export async function enableSmtpAccount(projectId: string, id: string, enabled: boolean, expectedVersion: number): Promise<SmtpAccount> {
  return account(await request(`${base(projectId)}/accounts/${encodeURIComponent(id)}/enabled`,
    { method: 'POST', body: JSON.stringify({ enabled, expected_version: expectedVersion }) }));
}
export async function listOutboundEmails(projectId: string, signal?: AbortSignal): Promise<OutboundEmail[]> {
  const value = await request(`${base(projectId)}/emails`, { signal });
  if (!Array.isArray(value)) invalid(); return value.map(email);
}
export async function proposeOutboundEmail(projectId: string, fields: {
  account_id: string; run_id: string; to_address: string; subject: string; body: string;
}): Promise<{email_id: string; approval_id: string; state: string}> {
  const row = object(await request(`${base(projectId)}/emails`, { method: 'POST', body: JSON.stringify(fields) }));
  return { email_id: str(row, 'email_id'), approval_id: str(row, 'approval_id'), state: str(row, 'state') };
}
