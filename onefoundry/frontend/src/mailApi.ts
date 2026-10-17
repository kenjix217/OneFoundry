import { ApiError, request } from './api';

export interface MailAccount {
  id: string;
  label: string;
  protocol: 'imap' | 'gmail' | 'graph';
  host: string | null;
  port: number | null;
  username: string | null;
  folder: string;
  enabled: boolean;
  status: 'unverified' | 'ready' | 'failed';
  last_error_code: string | null;
  capabilities: string[];
  last_probe_at: string | null;
  last_poll_at: string | null;
  version: number;
  credential_present: boolean;
}

export interface NewMailAccount {
  label: string;
  host: string;
  port: number;
  username: string;
  folder: string;
  password: string;
}

function bad(): never {
  throw new ApiError('The server returned an unexpected mailbox response.', null);
}

function account(value: unknown): MailAccount {
  if (!value || typeof value !== 'object' || Array.isArray(value)) bad();
  const row = value as Record<string, unknown>;
  const text = (key: string) => { if (typeof row[key] !== 'string') bad(); return row[key] as string; };
  const maybe = (key: string) => row[key] === null ? null : text(key);
  const status = text('status');
  if (status !== 'unverified' && status !== 'ready' && status !== 'failed') bad();
  if (typeof row.enabled !== 'boolean' || typeof row.credential_present !== 'boolean' || !Array.isArray(row.capabilities)) bad();
  if ((row.port !== null && !Number.isSafeInteger(row.port)) || !Number.isSafeInteger(row.version)) bad();
  const protocol = text('protocol');
  if (protocol !== 'imap' && protocol !== 'gmail' && protocol !== 'graph') bad();
  return { id: text('id'), label: text('label'), protocol, host: maybe('host'), port: row.port as number | null, username: maybe('username'),
    folder: text('folder'), enabled: row.enabled, status, last_error_code: maybe('last_error_code'),
    capabilities: row.capabilities.map(String), last_probe_at: maybe('last_probe_at'), last_poll_at: maybe('last_poll_at'),
    version: row.version as number, credential_present: row.credential_present };
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/mail/accounts`;

export async function listMailAccounts(projectId: string, signal?: AbortSignal): Promise<MailAccount[]> {
  const value = await request(base(projectId), { signal });
  if (!Array.isArray(value)) bad();
  return value.map(account);
}

export async function createMailAccount(projectId: string, fields: NewMailAccount): Promise<MailAccount> {
  return account(await request(base(projectId), { method: 'POST', body: JSON.stringify(fields) }));
}

export async function createApiMailAccount(projectId: string, label: string, protocol: 'gmail' | 'graph', connectorAccountId: string): Promise<MailAccount> {
  return account(await request(`${base(projectId)}/api`, {
    method: 'POST', body: JSON.stringify({ label, protocol, connector_account_id: connectorAccountId }),
  }));
}

export async function probeMailAccount(projectId: string, accountId: string): Promise<MailAccount> {
  return account(await request(`${base(projectId)}/${encodeURIComponent(accountId)}/probe`, { method: 'POST', body: '{}' }));
}

export async function setMailEnabled(projectId: string, accountId: string, enabled: boolean, expectedVersion: number): Promise<MailAccount> {
  return account(await request(`${base(projectId)}/${encodeURIComponent(accountId)}/enabled`, {
    method: 'POST', body: JSON.stringify({ enabled, expected_version: expectedVersion }),
  }));
}

export async function removeMailAccount(projectId: string, accountId: string, expectedVersion: number): Promise<void> {
  const response = await fetch(`${base(projectId)}/${encodeURIComponent(accountId)}?expected_version=${expectedVersion}`, {
    method: 'DELETE', credentials: 'same-origin',
  });
  if (response.status !== 204) throw new ApiError(`Removing the mailbox failed (${response.status}). Refresh before retrying.`, response.status);
}
